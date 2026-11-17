"""Saúde do Greg (pedido 63, lote 2): liga cada peça à central de avisos (app/alertas.py).
Quem chama: bin/coletor.py (`do_coletor`), app/conexao.py (`do_monitor`), bin/fechar-dia.py e bin/fechar-semana.py
(`do_fechamento`), bin/vigia.sh (`python3 -m app.saude abrir|fechar|notificar`), app/server.py (thread `Vigia`: leitura
parada, a cada 5 min) e as telas (`pecas`, faixa de `_comum`).
O que faz: traduz o resultado de uma peça (exceção, evento do monitor, código de saída) em abrir ou fechar um aviso do
catálogo (app/problemas.py), notifica pelo macOS o que a central diz que está na hora, e mede "leitura parada".
O que não faz: não conserta nada, não decide texto (o catálogo decide), não lê conversa e não levanta exceção: avisar
nunca derruba quem avisou. No Docker não há notificação nem conferência do agendador (ficam a faixa e a tela).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from app import alertas
from app.erros import causa
from app.problemas import CATALOGO, MOTIVO_DESCONHECIDO, PECAS

log = logging.getLogger("vigia.saude")

LEITURA_PARADA_S = 75 * 60     # D-66: 75 minutos sem leitura boa, contando só o tempo com o Mac acordado
PASSO_VIGIA_S = 300.0
AGENTE = "io.vigia.vigia-whatsapp"
ROTULO_PECA = {"docker": "Docker", "evolution": "Ponte com o WhatsApp (Evolution)", "whatsapp": "WhatsApp",
               "banco": "Banco das mensagens", "ia": "Leitura pela IA", "agendador": "Leituras automáticas",
               "transcricao": "Transcrição de áudio", "fechamento": "Fechamento do dia e da semana", "envio": "Envios agendados"}

_IA = ("ia-login", "ia-limite", "ia-ausente", "ia-erro",
       "ia-chave", "ia-saldo", "ia-api-limite", "ia-teto", "ia-api-fora", "ia-api-erro")   # pedido 68: motor por API
_WHATS = ("whatsapp-caiu", "whatsapp-sem-resposta")
# evento do monitor de conexão -> aviso. "ok" fecha; o que não está aqui não mexe na central.
_EVENTOS = {"queda": "whatsapp-caiu", "logout_sem_pareamento": "whatsapp-caiu", "socket_morto": "whatsapp-sem-resposta",
            "socket_zumbi": "whatsapp-sem-resposta", "silencio": "whatsapp-sem-resposta",
            "entrega_travada": "whatsapp-sem-resposta", "erro": "whatsapp-sem-resposta"}


def _seguro(fn, *a, **k):
    try:
        return fn(*a, **k)
    except Exception as e:  # noqa: BLE001  (a central falhar nunca derruba o ciclo)
        log.warning("saúde: %s", causa(e))
        return None


# ------------------------------------------------------------------ juízes

def codigo_do_erro(e: BaseException) -> str | None:
    """O aviso que uma exceção do ciclo de leitura abre; None = não é falha de peça conhecida."""
    from app import lotes
    from app.erros import EvolutionIndisponivel, NaoAutorizado
    if getattr(e, "codigo_aviso", None) in CATALOGO:      # pedido 68: a IA por API diz qual aviso é o dela
        return e.codigo_aviso
    if isinstance(e, lotes.ClaudeSemLogin):
        return "ia-login"
    if isinstance(e, lotes.ErroClaude):
        t = str(e).lower()
        if "não executou (filenotfounderror" in t:
            return "ia-ausente"
        if re.search(r"usage limit|rate limit|limit reached|quota|credit balance", t):
            return "ia-limite"
        return "ia-erro"
    if isinstance(e, lotes.PostgresIndisponivel):
        return "banco-fora"
    if isinstance(e, lotes.NaoPareado):
        return "nao-pareado"
    if isinstance(e, (EvolutionIndisponivel, NaoAutorizado)):
        return "evolution-fora"
    return None


ERRO_LEITURA = "leitura-erro.json"   # pedido 85: {"motivo": código do catálogo ou "desconhecido", "em"}; só código


def _apagar(arq: Path) -> None:
    arq.unlink(missing_ok=True)


def _gravar_erro_leitura(dados: Path, motivo: str) -> None:
    tmp = dados / (ERRO_LEITURA + ".tmp")
    tmp.write_text(json.dumps({"motivo": motivo, "em": int(time.time())}), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, dados / ERRO_LEITURA)


def do_coletor(dados: Path, erro: BaseException | None = None, resumo: dict | None = None) -> None:
    """Fim de um ciclo real do coletor. Sem erro: a leitura rodou, então banco e pareamento estão de pé; os avisos
    da IA só fecham quando houve lote lido (status "ok"), porque ciclo sem novidade não chama o Claude."""
    dados = Path(dados)
    if erro is None:
        _seguro(_apagar, dados / ERRO_LEITURA)
        _seguro(alertas.fechar, dados, "banco-fora", "nao-pareado", "leitura-parada", "agendador-parado", "docker-parado")
        if isinstance(resumo, dict) and resumo.get("status") == "ok":     # só aqui o Claude respondeu de fato
            _seguro(alertas.fechar, dados, *_IA)
        t = (resumo or {}).get("transcricao") if isinstance(resumo, dict) else None
        if isinstance(t, dict) and t.get("audios"):
            if t.get("falhas") and not t.get("transcritos"):
                _seguro(alertas.abrir, dados, "transcricao-erro", detalhe=f"{int(t['falhas'])} falha(s) no ciclo")
            elif t.get("transcritos"):
                _seguro(alertas.fechar, dados, "transcricao-erro")
        return
    cod = codigo_do_erro(erro)
    _seguro(_gravar_erro_leitura, dados, cod or MOTIVO_DESCONHECIDO)    # pedido 85: o motivo que a leitura parada diz
    if cod is None:
        return
    if cod in _IA:      # um motivo de IA por vez: o de agora substitui o anterior
        _seguro(alertas.fechar, dados, *(c for c in _IA if c != cod))
    _seguro(alertas.abrir, dados, cod, detalhe=causa(erro))


def do_monitor(dados: Path, r: dict | None) -> None:
    """Resultado de um ciclo do monitor de conexão ({"evento", "motivo", ...})."""
    dados = Path(dados)
    ev = str((r or {}).get("evento") or "")
    if ev == "ok":
        _seguro(alertas.fechar, dados, *_WHATS, "evolution-fora")
        return
    cod = _EVENTOS.get(ev)
    if cod is None:
        return
    _seguro(alertas.fechar, dados, *(c for c in _WHATS if c != cod))
    _seguro(alertas.abrir, dados, cod, detalhe=str((r or {}).get("motivo") or (r or {}).get("erro") or ev)[:80])


def do_fechamento(dados: Path, qual: str, rc: int) -> None:
    """`qual` = "dia" ou "semana"; rc 0 fecha, 1 (falhou) abre; 2 (não rodou: outra peça está fora) não mexe."""
    cod = f"fechamento-{qual}-erro"
    if rc == 0:
        _seguro(alertas.fechar, Path(dados), cod)
    elif rc == 1:
        _seguro(alertas.abrir, Path(dados), cod, detalhe="saiu com código 1")


# ------------------------------------------------------------------ notificação

APPS_DO_GREG = ("~/Applications/Greg.app", "/Applications/Greg.app")


def notificar_pelo_app(titulo: str, texto: str) -> bool:
    """Pedido 78: pede ao Greg.app que emita a notificação, para ela sair com o nome e o ícone do Greg. True = saiu.
    False (app não instalado, versão antiga, permissão negada, sem resposta) = quem chamou usa a reserva (osascript)."""
    for caminho in APPS_DO_GREG:
        exe = Path(caminho).expanduser() / "Contents" / "MacOS" / "Vigia"
        if not exe.is_file():
            continue
        try:
            r = subprocess.run([str(exe), "--notificar", titulo[:120], texto[:300]], timeout=20, capture_output=True, check=False)
        except (OSError, subprocess.SubprocessError):
            return False
        return r.returncode == 0
    return False


def _notificar_mac(titulo: str, texto: str) -> None:
    """Notificação do macOS. Só texto fixo do catálogo, nunca conteúdo de conversa. No Docker ou sem osascript, nada."""
    if os.environ.get("VIGIA_CONTAINER") == "1" or os.environ.get("VIGIA_SEM_NOTIFICACAO") == "1" or not shutil.which("osascript"):
        return      # VIGIA_SEM_NOTIFICACAO: a suíte de testes nunca notifica de verdade (testes/__init__.py)
    seguro = lambda t: re.sub(r'[^\w .,:;()/áàâãéêíóôõúçÁÀÂÃÉÊÍÓÔÕÚÇ-]', "", t)[:200]
    if notificar_pelo_app(seguro(titulo), seguro(texto)):
        return
    try:
        subprocess.run(["osascript", "-e", f'display notification "{seguro(texto)}" with title "{seguro(titulo)}" sound name "Basso"'],
                       timeout=10, capture_output=True, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


NOTIFICAR = _notificar_mac      # os testes trocam por uma função que só anota


def notificar(dados: Path, *, agora: float | None = None) -> int:
    """Notifica o que a central diz que está na hora (aviso novo, ou aberto há mais de um dia desde a última).
    Devolve quantas notificações saíram."""
    devidos = _seguro(alertas.a_notificar, Path(dados), agora=agora) or []
    for p in devidos:
        _seguro(NOTIFICAR, "Greg: " + p.titulo, f"{p.por_que} {p.passos[0] if p.passos else ''}".strip())
    return len(devidos)


# ------------------------------------------------------------------ leitura parada

def _json(arq: Path) -> dict:
    try:
        d = json.loads(arq.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _agente_carregado() -> bool | None:
    """O LaunchAgent do ciclo está carregado? None = não dá para saber (Docker, sem launchctl, erro)."""
    if os.environ.get("VIGIA_CONTAINER") == "1" or not shutil.which("launchctl"):
        return None
    try:
        r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{AGENTE}"], capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.returncode == 0


def _despertar() -> int | None:
    from app.conexao import ultimo_despertar
    d = _seguro(ultimo_despertar)
    return d[0] if d else None


def conferir_leitura(dados: Path, *, agora: float | None = None, despertar=_despertar, agente=_agente_carregado,
                     inicio: float | None = None) -> str:
    """Um passo do vigia. Devolve o que concluiu: "pausado", "sem_pareamento", "em_dia", "outro_aviso" (já há um aviso
    que explica a parada), "agendador-parado" ou "leitura-parada" (aviso aberto).
    O relógio conta do último destes: última leitura, último despertar do Mac, início do painel. Assim, Mac que dormiu
    a noite toda não acorda com aviso; só avisa depois de 75 minutos ACORDADO sem leitura."""
    dados = Path(dados)
    t = float(agora if agora is not None else time.time())
    if (dados / "PAUSADO").exists():
        _seguro(alertas.fechar, dados, "leitura-parada", "agendador-parado")
        return "pausado"
    if not _json(dados / "cursor.json").get("pareado_em"):
        _seguro(alertas.fechar, dados, "leitura-parada", "agendador-parado")
        return "sem_pareamento"
    # pedido 81: fora do horário ou do intervalo escolhidos em Ajustes o ciclo roda e não lê; isso conta como ciclo vivo
    from app import agenda_leitura
    base = max(float(_json(dados / "ultima-leitura.json").get("em") or 0), agenda_leitura.ultima_passada(dados),
               float(despertar() or 0), float(inicio or 0))
    if t - base < LEITURA_PARADA_S:
        return "em_dia"       # quem fecha o aviso é a leitura boa (do_coletor), não o relógio
    abertos = {a["codigo"] for a in (_seguro(alertas.abertos, dados) or [])}
    if abertos - {"leitura-parada", "agendador-parado", "transcricao-erro", "fechamento-dia-erro", "fechamento-semana-erro",
                 "agendado-nao-saiu", "agendado-incerto"}:
        _seguro(alertas.fechar, dados, "leitura-parada")
        return "outro_aviso"
    if agente() is False:
        _seguro(alertas.fechar, dados, "leitura-parada")
        _seguro(alertas.abrir, dados, "agendador-parado", agora=t)
        return "agendador-parado"
    _seguro(alertas.fechar, dados, "agendador-parado")
    # pedido 85: a leitura boa apaga o registro; se ele existe, a última tentativa falhou e o motivo é dele
    motivo = str(_json(dados / ERRO_LEITURA).get("motivo") or "")
    _seguro(alertas.abrir, dados, "leitura-parada", detalhe=f"sem leitura há {int((t - base) // 60)} min",
            motivo=motivo, agora=t)
    return "leitura-parada"


class Vigia:
    """Thread do painel: a cada 5 min confere a leitura e notifica o que estiver na hora. É o que avisa quando o
    próprio ciclo (LaunchAgent) não roda, porque o painel é um processo à parte."""

    def __init__(self, dados: Path, relogio=time.time):
        self.dados = Path(dados)
        self.relogio = relogio
        self.inicio = relogio()
        self._cond = threading.Condition()
        self._thread: threading.Thread | None = None
        self._encerrar = False

    def passo(self) -> str:
        r = conferir_leitura(self.dados, agora=self.relogio(), inicio=self.inicio)
        notificar(self.dados, agora=self.relogio())
        return r

    def iniciar(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._laco, name="vigia-saude", daemon=True)
            self._thread.start()

    def encerrar(self) -> None:
        with self._cond:
            self._encerrar = True
            self._cond.notify_all()

    def _laco(self) -> None:
        while True:
            with self._cond:
                self._cond.wait(PASSO_VIGIA_S)
                if self._encerrar:
                    return
            _seguro(self.passo)


# ------------------------------------------------------------------ para a tela

def pecas(dados: Path) -> list[dict]:
    """Uma linha por peça, na ordem de PECAS: {"peca", "rotulo", "avisos": [aviso de alertas.abertos, ...]}."""
    abertos = _seguro(alertas.abertos, Path(dados)) or []
    return [{"peca": p, "rotulo": ROTULO_PECA.get(p, p), "avisos": [a for a in abertos if a["peca"] == p]} for p in PECAS]


# ------------------------------------------------------------------ CLI (bin/vigia.sh)

def main(argv=None) -> int:
    """python3 -m app.saude abrir <codigo> | fechar <codigo>... | fechamento <dia|semana> <rc> | notificar
    A pasta de dados vem de VIGIA_DADOS (padrão: dados/ ao lado deste código). Sai sempre em 0."""
    args = list(sys.argv[1:] if argv is None else argv)
    dados = Path(os.environ.get("VIGIA_DADOS") or Path(__file__).resolve().parent.parent / "dados")
    if len(args) >= 2 and args[0] == "abrir" and args[1] in CATALOGO:
        _seguro(alertas.abrir, dados, args[1])
    elif len(args) >= 2 and args[0] == "fechar":
        _seguro(alertas.fechar, dados, *[a for a in args[1:] if a in CATALOGO])
    elif len(args) == 3 and args[0] == "fechamento" and args[1] in ("dia", "semana") and args[2].lstrip("-").isdigit():
        do_fechamento(dados, args[1], int(args[2]))
    elif args[:1] == ["notificar"]:
        notificar(dados)
    else:
        print("uso: python3 -m app.saude abrir <codigo> | fechar <codigo>... | fechamento <dia|semana> <rc> | notificar", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
