"""Conexão do Greg: máquina de estado do QR (R-11), contador diário e monitor de queda/socket morto.
Quem chama: app/paginas/conexao.py (iniciar_qr, tick, reiniciar), bin/coletor.py e bin/vigia.sh (monitorar).
O que não faz: não cria instância, não gera QR fora de iniciar_qr/renovação, não ultrapassa o teto diário e não loga texto de mensagem.
"""
import fcntl
import inspect
import json
import logging
import os
import sqlite3
import sys
import tempfile
import threading
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from app import aviso, lotes, saude, segredos
import functools
import re
import subprocess

from app.banco import Banco
from app.cursor import Cursor
from app.erros import EvolutionIndisponivel, NaoAutorizado, QRIndisponivel, VigiaErro, TetoQR, causa
from app.evolution_client import EvolutionClient

log = logging.getLogger("vigia.conexao")

RAIZ = Path(__file__).resolve().parents[1]

ESTADOS = ("ocioso", "gerando", "renovando", "expirado", "conectado")
TETO_DIA_PADRAO = 10
JANELA_428_S = 600          # open com reason_code mais novo que isso = socket morto (caso 428)
LOGOUT_TENTATIVAS = 3       # reenvios de logout quando a Evolution continua em "connecting"
ATRASO_TICK_S = 15          # tick que chega mais tarde que isso após o vencimento do QR expira, nunca renova
PRAZO_FALHA_S = 30          # prazo curto de um clique que falhou depois do GET /instance/connect
MARGEM_428_S = 60           # margem na comparação reason_em x rodada anterior do monitor
SESSAO_QR_FOLGA_S = 60      # o monitor só trata "connecting" como abandonado 60 s depois do fim da sessão de QR
CONNECTING_AVISO_S = 3600   # número pareado em connecting há mais que isso: o aviso se repete a cada ciclo (D-24)
EXECUTOR_TIMEOUT_S = 120
# Socket zumbi (pedido 41, 09/11/2026): a Evolution dizia "open" 17 h depois do socket morrer com o Mac dormindo.
# Sonda ativa = foto do próprio número pelo socket; silêncio = nenhuma mensagem gravada em Message há horas.
SONDA_TENTATIVAS = 2            # a sonda só condena o socket depois de falhar duas vezes seguidas
SONDA_PAUSA_S = 3               # pausa entre as duas tentativas
REINICIO_SONDA_INTERVALO_S = 7200   # no máximo um reinício automático por socket zumbi a cada 2 h
ZUMBI_AVISOS_MAX = 2            # avisa no reinício e uma vez se persistir; depois cala até a sonda voltar
# D-54 (pedido 43): o Greg mora num portátil e a premissa é RECUPERAR AO LIGAR. No ciclo do despertar pleno (tampa aberta,
# Mac ligado) o monitor espera a Evolution reconectar antes de julgar; se o socket continuar surdo, reinicia já, sem aviso,
# e o vigia.sh lê de novo. Despertar de manutenção (DarkWake, o Mac segue dormindo para o dono) não julga nem recupera.
DESPERTAR_ESPERA_S = 120        # quanto esperar a reconexão no ciclo do despertar pleno
DESPERTAR_PAUSA_S = 20          # pausa entre as reconferências dentro dessa espera
_RE_CAPACIDADES = re.compile(r"Current System Capabilities are:\s*(.*)")
SILENCIO_AVISO_S = 6 * 3600     # conexão "open" sem mensagem nova há 6 h (dentro do horário) vira aviso
SILENCIO_HORAS = range(9, 22)   # hora local em que o silêncio é suspeito (de madrugada é normal)
SILENCIO_REPETE_S = 86400       # o aviso de silêncio se repete no máximo uma vez por dia
# D-56 (pedido 44): canário de entrega. "open" + sonda ok NÃO provam que o WhatsApp está ENTREGANDO (09/11: o servidor
# fechava o stream com a instância aberta e o Postgres ficou parado 2 h 35). Calada há CANARIO_SILENCIO_S dentro de
# SILENCIO_HORAS, a conexão é posta à prova: o bot da agência manda um texto curto ao próprio número; se ele não aparece
# no Postgres em CANARIO_ESPERA_S, reinicia a Evolution e confere de novo; ainda nada = "entrega_travada" (parear de novo).
CANARIO_SILENCIO_S = 2 * 3600
CANARIO_ESPERA_S = 120
CANARIO_PAUSA_S = 20
CANARIO_REPETE_S = SILENCIO_AVISO_S     # no máximo um teste a cada 6 h: num dia inteiro calado saem 2 mensagens do bot
CANARIO_PREFIXO = "Vigia: teste de entrega "
_ESTADOS_EVO = ("open", "close", "connecting")
_NUM = ("instavel_desde", "connecting_desde", "atualizado_em", "reiniciado_em", "qr_ate", "ultimo_logout_em",
        "zumbi_avisos", "silencio_avisado", "sonda_ok_em", "canario_em", "canario_ok_em")
_BOOL = ("queda_avisada", "logout_pendente", "sonda_foto_vista")
# chaves que o monitor escreve (o painel escreve qr_ate e reiniciado_em; logout_pendente é dos dois)
_CHAVES_MONITOR = ("ultimo_estado", "instavel_desde", "connecting_desde", "queda_avisada", "reason_visto",
                   "morto_avisado", "atualizado_em", "evento", "motivo", "reason_code", "ultimo_logout_em",
                   "sonda", "sonda_ok_em", "sonda_foto_vista", "zumbi_avisos", "silencio_avisado",
                   "canario_em", "canario_ok_em")




def _para_epoch(valor) -> float | None:
    """Converte `disconnectionAt` (ISO 8601, epoch em s ou em ms) em epoch segundos; ilegível = None."""
    if valor is None or valor == "":
        return None
    if isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        v = float(valor)
        return v / 1000.0 if v > 1e11 else v
    texto = str(valor).strip()
    try:
        v = float(texto)
        return v / 1000.0 if v > 1e11 else v
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(texto.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _limitar(valor, lo: int, hi: int, padrao: int) -> int:
    """Inteiro de config limitado a [lo, hi]; ausente, bool ou não numérico = padrão."""
    if isinstance(valor, bool) or valor is None:
        return padrao
    try:
        n = int(float(valor))
    except (TypeError, ValueError, OverflowError):
        return padrao
    return max(lo, min(hi, n))


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def ultimo_despertar() -> tuple[int, str] | None:
    """(epoch, tipo) do último despertar do Mac. Epoch de `sysctl -n kern.waketime`; tipo "wake" (despertar pleno: o dono
    ligou ou abriu a tampa — `pmset -g systemstate` lista Graphics) ou "darkwake" (despertar de manutenção, sem Graphics:
    para o usuário o Mac continua dormindo). None fora do macOS ou se o sysctl falhar; sem systemstate, tipo "wake".

    Pedido 43 / D-54 (tarde de 09/11/2026): com a tampa fechada o Mac dorme e a Evolution some junto; os ciclos que rodam
    nos DarkWakes de 45 s encontram o socket reconectando, a sonda muda e a instância em `close` — não é zumbi nem queda.
    No despertar pleno a regra é outra: esperar a reconexão e, se o socket continuar surdo, recuperar já (reiniciar e ler).
    """
    try:
        r = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.waketime"], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"sec\s*=\s*(\d+)", r.stdout or "")
    if not m:
        return None
    tipo = "wake"
    try:
        s = subprocess.run(["/usr/bin/pmset", "-g", "systemstate"], capture_output=True, text=True, timeout=3)
        c = _RE_CAPACIDADES.search(s.stdout or "")
        if c and "Graphics" not in c.group(1):
            tipo = "darkwake"
    except (OSError, subprocess.SubprocessError):
        pass
    return int(m.group(1)), tipo


class Conexao:
    """Máquina de estado do QR + monitor. Singleton do processo do painel (tem estado em memória e uma trava);
    o monitor (`python3 -m app.conexao --monitorar`, via vigia.sh) roda em outro processo e só fala com ele
    pelo dados/conexao.json (flock em volta da leitura-modificação-escrita).

    Invariante (R-11, P-1, P-3): nenhuma saída de uma tentativa de QR acontece sem `open` ou `logout()`;
    nenhum QR sem clique "em dia"; nenhuma tentativa (falha inclusive) além de `qr_max` por clique.
    """

    def __init__(self, evo: EvolutionClient, banco: Banco, *, qr_segundos=60, qr_max=3, relogio=time.time,
                 cursor: Cursor | None = None, teto_dia: int = TETO_DIA_PADRAO,
                 estado_arq=None, janela_428_s: int = JANELA_428_S,
                 sonda_jid: str | None = None, dormir=time.sleep, ultima_arq=None, despertar=ultimo_despertar,
                 canario_enviar=None, canario_chegou=None):
        self.evo = evo
        self.banco = banco
        self.qr_segundos = qr_segundos
        self.qr_max = qr_max
        self.teto_dia = teto_dia
        self._relogio = relogio
        self._dormir = dormir
        self._despertar = despertar          # pedido 43: epoch do último despertar do Mac (ou None)
        # D-56: canário de entrega — enviar(texto) -> bool pelo bot da agência e chegou(texto, desde) -> bool no Postgres.
        # Só o monitor do vigia.sh injeta os dois (main); a Conexao do painel não manda nada (None = sem canário).
        self._canario_enviar = canario_enviar
        self._canario_chegou = canario_chegou
        self.janela_428_s = janela_428_s
        self.sonda_jid = sonda_jid or None      # próprio número (DONO_WHATSAPP): alvo da sonda do socket; None = sem sonda
        self.cursor = cursor if cursor is not None else Cursor.carregar("dados/cursor.json")
        self._estado_arq = Path(estado_arq if estado_arq is not None else "dados/conexao.json")
        # dados/ultima-mensagem.json, escrito pelo coletor a cada ciclo: {"ultima_em": epoch, "conferido_em": epoch}
        self._ultima_arq = Path(ultima_arq) if ultima_arq is not None else self._estado_arq.parent / "ultima-mensagem.json"
        self._trava = threading.RLock()
        self.estado = "ocioso"
        self._qr: dict | None = None      # QR vigente: {"base64","code","expira_em"}; base64 "" = tentativa sem QR
        self._emitidos = 0                # QRs exibidos no clique atual
        self._tentativas = 0              # chamadas a gerar_qr no clique atual (falha inclusive)
        self._qr_mostrado = False         # D-53: um QR saiu nesta sessão → o próximo open é pareamento NOVO (regrava pareado_em)
        self._logout_tentativas = 0

    @classmethod
    def de_config(cls, evo, banco, config: dict, **kw) -> "Conexao":
        """Monta a Conexao com [qr] do config.toml já carregado. Valores fora do intervalo são limitados:
        segundos [20,120] (padrão 60), maximo [1,3] (3), teto_dia [1,10] (10), janela_428_s [60,3600] (600)."""
        qr = (config or {}).get("qr", {})
        if not isinstance(qr, dict):
            qr = {}
        kw.setdefault("qr_segundos", _limitar(qr.get("segundos"), 20, 120, 60))
        kw.setdefault("qr_max", _limitar(qr.get("maximo"), 1, 3, 3))
        kw.setdefault("teto_dia", _limitar(qr.get("teto_dia"), 1, 10, TETO_DIA_PADRAO))
        kw.setdefault("janela_428_s", _limitar(qr.get("janela_428_s"), 60, 3600, JANELA_428_S))
        return cls(evo, banco, **kw)

    # ------------------------------------------------------------------ QR

    def _restantes(self) -> int:
        return max(0, self.qr_max - self._tentativas)

    def _resumo_qr(self, **extra) -> dict:
        qr = self._qr or {}
        r = {"estado": self.estado, "base64": qr.get("base64", ""), "expira_em": qr.get("expira_em"),
             "restantes": self._restantes(), "qrs_hoje": self.banco.qrs_hoje(), "qr_novo": False}
        r.update(extra)
        return r

    def ttl(self) -> int:
        """Segundos restantes do QR vigente (0 se não há QR exibido). Sem efeito: não fala com a Evolution."""
        qr = self._qr
        if self.estado not in ("gerando", "renovando") or not qr or not qr.get("base64"):
            return 0
        return max(0, min(int(self.qr_segundos), int(round(qr["expira_em"] - self._relogio()))))

    def qr_vigente(self) -> dict | None:
        """QR hoje exibível: {"base64","restantes","expira_em"} só com QR com base64 e não vencido; senão None.
        Sem efeito (não fala com a Evolution). `restantes` = tentativas que ainda sobram no clique."""
        qr = self._qr
        if self.estado not in ("gerando", "renovando") or not qr or not qr.get("base64"):
            return None
        if self._relogio() >= qr["expira_em"]:
            return None
        return {"base64": qr["base64"], "restantes": self._restantes(), "expira_em": qr["expira_em"]}

    def ultimo_monitor(self) -> dict:
        """Último resultado gravado pelo monitor em conexao.json, só leitura (nenhuma chamada à Evolution):
        {"evento","motivo","reason_code","ultimo_estado","atualizado_em","logout_pendente",...}; {} se não há."""
        return self._ler_monitor()

    def _emitir(self, origem: str) -> None:
        """Único ponto que consome QR: conta a tentativa ANTES (falha fechada), 1 gerar_qr() + 1 registro.
        Chamador já checou o teto. Levanta QRIndisponivel/EvolutionIndisponivel/erro do sqlite."""
        self._tentativas += 1
        qr = self.evo.gerar_qr()
        self.banco.registrar_qr(origem)
        self._emitidos += 1
        self._qr_mostrado = True          # a Evolution só gera QR sem sessão: o open que vier é pareamento novo
        self._qr = {"base64": qr.get("base64", ""), "code": qr.get("code", ""),
                    "expira_em": self._relogio() + self.qr_segundos}
        log.info("qr emitido origem=%s tentativas=%d hoje=%d", origem, self._tentativas, self.banco.qrs_hoje())

    def iniciar_qr(self) -> dict:
        """Clique em Gerar QR: 1 gerar_qr + registrar_qr("clique"). Recusa acima do teto diário (TetoQR).

        Devolve {"estado","base64","expira_em","restantes","qrs_hoje","qr_novo"} (+ "repetido" no duplo clique,
        + "ja_conectado" se a instância já está open). Levanta TetoQR, EvolutionIndisponivel, QRIndisponivel.
        Se a exceção vem depois do GET /instance/connect o estado fica `gerando` (sem QR, prazo curto) e o
        tick() faz o logout: nunca volta a `ocioso` com a Evolution em connecting.
        """
        with self._trava:
            agora = self._relogio()
            if self.estado in ("gerando", "renovando") and self._qr and agora < self._qr["expira_em"]:
                # duplo clique com QR ainda vivo: devolve o mesmo, não gasta outro (P-3)
                return self._resumo_qr(repetido=True)
            if self.evo.estado()["state"] == "open":
                # já conectado: QR não serve para nada (estado() não consome)
                self._conectar()
                return self._resumo_qr(ja_conectado=True)
            if self.banco.qrs_hoje() >= self.teto_dia:
                raise TetoQR(f"teto diário de QR atingido ({self.banco.qrs_hoje()}/{self.teto_dia}); tente amanhã")
            self._emitidos = 0
            self._tentativas = 0
            self._logout_tentativas = 0
            # sessão de QR em disco ANTES do GET: o monitor não faz logout enquanto ela vale
            self._mesclar(qr_ate=agora + self.qr_segundos * self.qr_max + ATRASO_TICK_S, logout_pendente=False)
            self.estado = "gerando"
            self._qr = {"base64": "", "code": "", "expira_em": agora + PRAZO_FALHA_S}
            try:
                self._emitir("clique")
            except QRIndisponivel as e:
                if getattr(e, "state", None) == "open":
                    self._conectar()
                    return self._resumo_qr(ja_conectado=True)
                raise
            return self._resumo_qr(qr_novo=True)

    def tick(self) -> dict:
        """Chamado pelo painel a cada 5 s: consulta estado(); open conecta; QR vencido renova ou expira.

        Devolve o resumo ({"estado","base64","expira_em","restantes","qrs_hoje","qr_novo","evolution"}) mais
        "motivo" e "logout_confirmado" quando expira. Tick atrasado (> 15 s após o vencimento) nunca renova.
        Levanta EvolutionIndisponivel se estado() falhar.
        """
        with self._trava:
            st = self.evo.estado()["state"]
            extra: dict = {"evolution": st}
            if self.estado in ("gerando", "renovando"):
                agora = self._relogio()
                if st == "open":
                    self._conectar()
                elif self._qr is not None and agora >= self._qr["expira_em"]:
                    if agora > self._qr["expira_em"] + ATRASO_TICK_S:
                        extra["motivo"] = "tick_atrasado"
                        extra.update(self._expirar())
                    elif not self._qr.get("base64"):
                        extra["motivo"] = "falha_qr"
                        extra.update(self._expirar())
                    elif self._restantes() > 0 and self.banco.qrs_hoje() < self.teto_dia:
                        try:
                            self._emitir("renovacao")
                            self.estado = "renovando"
                            extra["qr_novo"] = True
                        except QRIndisponivel as e:
                            if getattr(e, "state", None) == "open":
                                self._conectar()
                            else:
                                log.warning("renovacao falhou: QRIndisponivel")
                                extra["motivo"] = "falha_renovacao"
                                extra.update(self._expirar())
                        except Exception as e:  # EvolutionIndisponivel, sqlite3.Error...: falha fechada
                            log.warning("renovacao falhou: %s", causa(e))
                            extra["motivo"] = "falha_renovacao"
                            extra.update(self._expirar())
                    else:
                        extra["motivo"] = "teto" if self._restantes() > 0 else "limite_por_clique"
                        extra.update(self._expirar())
            elif self.estado == "expirado":
                if st == "open":
                    self._conectar()
                elif st == "connecting" and self._logout_tentativas < LOGOUT_TENTATIVAS:
                    extra.update(self._sair_de_connecting())
            elif self.estado == "conectado":
                if st != "open":
                    self.estado = "ocioso"
                    self._qr = None
            return self._resumo_qr(**extra)

    def _limpar_sessao(self) -> None:
        """Apaga qr_ate do disco (sessão de QR encerrada); falha de disco não derruba a máquina."""
        try:
            self._mesclar(qr_ate=None)
        except OSError as e:
            log.warning("não consegui limpar qr_ate: %s", causa(e))

    def _conectar(self) -> None:
        """open: grava pareado_em (epoch) se ainda não existe OU se um QR saiu nesta sessão (pareamento novo — D-53: a
        janela de leitura é pareado_em − 1 dia e acompanha cada pareamento; "já conectado" sem QR não mexe). Relê o disco
        antes (o coletor também grava o cursor) e SÓ DEPOIS fecha o QR; se o I/O do cursor falhar, o estado não muda e
        o próximo tick repete."""
        disco = Cursor.carregar(self.cursor.caminho)
        if disco.pareado_em is None or self._qr_mostrado:
            novo = disco.pareado_em is not None
            disco.pareado_em = int(self._relogio())
            disco.salvar()
            log.info("pareado_em gravado (%s)", "pareamento novo" if novo else "primeiro pareamento")
            self.pareou_em = disco.pareado_em   # pedido 75: o painel agenda a leitura automática a partir daqui
        self._qr_mostrado = False
        self.cursor.pareado_em = disco.pareado_em
        self.cursor.ultimo_seq = disco.ultimo_seq
        self.estado = "conectado"
        self._qr = None
        self._emitidos = 0
        self._tentativas = 0
        try:
            self._mesclar(qr_ate=None, logout_pendente=False)
        except OSError as e:
            log.warning("não consegui limpar a sessão de QR: %s", causa(e))

    def _restampar_pareado_em(self, depois_de: int | None) -> bool:
        """D-53, rede de segurança do monitor: a instância voltou a `open` depois de um 401 (logout) mais novo que o
        pareado_em gravado → houve pareamento que o painel não viu (QR lido fora da sessão do tick). Marca agora; o
        coletor passa a ler de agora − 1 dia. False se nada mudou ou se o cursor não pôde ser lido."""
        try:
            disco = Cursor.carregar(self.cursor.caminho)
        except Exception as e:  # noqa: BLE001  (cursor corrompido: a página Conexão mostra; o monitor segue)
            log.warning("pareado_em não regravado: %s", causa(e))
            return False
        if disco.pareado_em is not None and (depois_de is None or disco.pareado_em >= depois_de):
            return False
        disco.pareado_em = int(self._relogio())
        disco.salvar()
        self.cursor.pareado_em = disco.pareado_em
        self.cursor.ultimo_seq = max(self.cursor.ultimo_seq, disco.ultimo_seq)
        log.info("pareado_em regravado pelo monitor (repareado)")
        return True

    def _expirar(self) -> dict:
        """Fim da tentativa (limite, teto, falha ou atraso): estado=expirado, logout() para sair de connecting."""
        self.estado = "expirado"
        self._qr = None
        self._logout_tentativas = 0
        self._limpar_sessao()
        return self._sair_de_connecting()

    def _marcar_pendente(self, valor: bool) -> None:
        try:
            self._mesclar(logout_pendente=valor)
        except OSError as e:
            log.warning("não consegui gravar logout_pendente: %s", causa(e))

    def _sair_de_connecting(self) -> dict:
        """Relê o estado (open = conecta, sem logout), envia logout(); se o logout falhar grava
        logout_pendente=true em conexao.json (o monitor tenta de novo). Devolve {"logout_confirmado","evolution"}."""
        self._logout_tentativas += 1
        try:
            antes = self.evo.estado()["state"]
        except VigiaErro:
            antes = None
        if antes == "open":
            self._conectar()
            return {"logout_confirmado": False, "evolution": "open"}
        try:
            self.evo.logout()
        except VigiaErro as e:
            log.warning("logout falhou (%s); fica pendente para o monitor", causa(e))
            self._marcar_pendente(True)
            return {"logout_confirmado": False, "evolution": antes, "logout_pendente": True}
        self._marcar_pendente(False)
        try:
            depois = self.evo.estado()["state"]
        except VigiaErro:
            depois = None
        confirmado = depois is not None and depois != "connecting"
        if not confirmado:
            log.warning("logout enviado mas a Evolution segue em connecting (tentativa %d/%d)",
                        self._logout_tentativas, LOGOUT_TENTATIVAS)
        else:
            log.info("logout confirmado estado=%s", depois)
        return {"logout_confirmado": confirmado, "evolution": depois}

    # ------------------------------------------------------------------ monitor

    def _ler_monitor(self) -> dict:
        """conexao.json com os tipos validados (campo de tipo errado vira ausente); ilegível = {}."""
        try:
            d = json.loads(self._estado_arq.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(d, dict):
            return {}
        for k in _NUM:
            if k in d and _num(d[k]) is None:
                d[k] = None
        for k in _BOOL:
            if k in d and not isinstance(d[k], bool):
                d[k] = False
        if d.get("ultimo_estado") not in _ESTADOS_EVO:
            d["ultimo_estado"] = None
        for k in ("reason_visto", "morto_avisado"):
            if k in d and not isinstance(d[k], list):
                d[k] = None
        for k in ("evento", "motivo"):
            if k in d and not isinstance(d[k], str):
                d[k] = None
        return d

    def _gravar_monitor(self, d: dict) -> None:
        self._estado_arq.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._estado_arq.parent, prefix=self._estado_arq.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(d, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._estado_arq)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _mesclar(self, **mudancas) -> None:
        """Leitura-modificação-escrita de conexao.json sob flock (painel e monitor são processos distintos)."""
        self._estado_arq.parent.mkdir(parents=True, exist_ok=True)
        trava = self._estado_arq.with_name(self._estado_arq.name + ".lock")
        with open(trava, "a") as t:
            fcntl.flock(t, fcntl.LOCK_EX)
            try:
                d = self._ler_monitor()
                d.update(mudancas)
                self._gravar_monitor(d)
            finally:
                fcntl.flock(t, fcntl.LOCK_UN)

    def _logout_do_monitor(self) -> bool:
        """logout() a partir do monitor; True se a Evolution aceitou."""
        try:
            self.evo.logout()
            return True
        except VigiaErro as e:
            log.warning("logout do monitor falhou: %s", causa(e))
            return False

    def _sondar(self) -> str:
        """Sonda ativa do socket (pedido 41): a foto do próprio número só vem pelo socket do Baileys.

        "ok" = respondeu com URL (socket vivo) · "sem_foto" = respondeu vazio (a Evolution engole erro do socket e
        devolve null, então vazio só é suspeito se já houve foto antes) · "falhou" = timeout/5xx nas SONDA_TENTATIVAS
        · "" = sem sonda (sem jid ou cliente sem foto_perfil). Apikey recusada (NaoAutorizado) sobe: não é socket.
        """
        fn = getattr(self.evo, "foto_perfil", None)
        if not self.sonda_jid or not callable(fn):
            return ""
        resultado = "falhou"
        for i in range(SONDA_TENTATIVAS):
            try:
                url = fn(self.sonda_jid)
            except NaoAutorizado:
                raise
            except VigiaErro as e:
                log.warning("sonda do socket falhou (%d/%d): %s", i + 1, SONDA_TENTATIVAS, causa(e))
                resultado = "falhou"
            else:
                if url:
                    return "ok"
                resultado = "sem_foto"
            if i + 1 < SONDA_TENTATIVAS:
                self._dormir(SONDA_PAUSA_S)
        return resultado

    def _despertar_desde(self, base) -> tuple[int, str] | None:
        """(epoch, "wake"|"darkwake") do despertar do Mac se ele aconteceu depois do ciclo anterior (`base`); None sem
        base, sem leitura ou sem despertar novo. A leitura do sistema é cortesia: qualquer erro vale None (regra antiga).
        Um epoch solto vale despertar pleno."""
        if not base or not callable(self._despertar):
            return None
        try:
            w = self._despertar()
        except Exception:  # noqa: BLE001
            return None
        if isinstance(w, (int, float)) and not isinstance(w, bool):
            w = (w, "wake")
        if not (isinstance(w, tuple) and len(w) == 2 and isinstance(w[0], (int, float))):
            return None
        return (int(w[0]), "darkwake" if w[1] == "darkwake" else "wake") if w[0] > base else None

    def _esperar_reconexao(self) -> tuple[dict, str]:
        """D-54: no ciclo do despertar pleno com a instância fora de open, reconsulta o estado a cada DESPERTAR_PAUSA_S por
        até DESPERTAR_ESPERA_S até ela abrir. Devolve (json do estado, state) da última consulta."""
        e, st = {}, "close"
        for _ in range(DESPERTAR_ESPERA_S // DESPERTAR_PAUSA_S):
            self._dormir(DESPERTAR_PAUSA_S)
            try:
                e = self.evo.estado()
            except EvolutionIndisponivel as ex:     # a Evolution também pode estar subindo: conta como "ainda fechada"
                log.info("Evolution indisponível durante a espera de reconexão (%s)", causa(ex))
                e = {}
            st = e.get("state") or "close"
            if st == "open":
                break
        return e, st

    def _resondar(self) -> str:
        """D-54: no ciclo do despertar pleno, repete a sonda a cada DESPERTAR_PAUSA_S por até DESPERTAR_ESPERA_S até ela
        responder "ok". Devolve o último resultado ("ok", "sem_foto" ou "falhou")."""
        resultado = ""
        for _ in range(DESPERTAR_ESPERA_S // DESPERTAR_PAUSA_S):
            self._dormir(DESPERTAR_PAUSA_S)
            resultado = self._sondar()
            if resultado == "ok":
                break
        return resultado

    def _mensagem_desde(self, base) -> bool:
        """True se dados/ultima-mensagem.json aponta mensagem gravada depois do ciclo anterior (prova de socket vivo)."""
        u = self.ultima_mensagem_em()
        return bool(base) and u is not None and u > base

    def ultima_mensagem_em(self) -> int | None:
        """Epoch da última mensagem gravada em "Message", do dados/ultima-mensagem.json que o coletor escreve; None sem arquivo."""
        try:
            d = json.loads(self._ultima_arq.read_text(encoding="utf-8"))
            v = d.get("ultima_em") if isinstance(d, dict) else None
            return int(v) if isinstance(v, (int, float)) and v > 0 else None
        except (OSError, ValueError, TypeError):
            return None

    def _esperar_canario(self, texto: str, desde: int) -> bool:
        """D-56: espera o teste de entrega aparecer em "Message", reconferindo a cada CANARIO_PAUSA_S por até
        CANARIO_ESPERA_S. Postgres fora conta como "não chegou": o teste nunca trava nem derruba o monitor."""
        for _ in range(CANARIO_ESPERA_S // CANARIO_PAUSA_S):
            self._dormir(CANARIO_PAUSA_S)
            try:
                if self._canario_chegou(texto, desde):
                    return True
            except VigiaErro as e:
                log.warning("conferência do canário falhou: %s", causa(e))
        return False

    def _canario(self, agora, executor, m: dict, rc) -> dict:
        """D-56 (pedido 44): teste de entrega. Manda CANARIO_PREFIXO + HH:MM pelo bot da agência ao próprio número e espera
        a linha em "Message". Devolve {"canario": estado[, "evento", "motivo", "reiniciado", "recuperacao"]}:
        "envio_falhou" (bot/cofre fora: nada a concluir, tenta no próximo ciclo) · "chegou" (entrega provada) ·
        "chegou_apos_reinicio" (travada, reiniciou e destravou: o vigia.sh lê de novo neste ciclo) · "travada" (evento
        entrega_travada; o motivo diz se reiniciou, se o reinício falhou ou se era recente demais para reiniciar de novo).
        Nunca lê o conteúdo de nada: só conta se o próprio texto do teste apareceu."""
        texto = CANARIO_PREFIXO + time.strftime("%H:%M", time.localtime(agora))
        try:
            enviado = bool(self._canario_enviar(texto))
        except Exception as e:  # noqa: BLE001  (o envio é de outro módulo e cortesia: nunca derruba o monitor)
            log.warning("envio do canário falhou: %s", causa(e))
            enviado = False
        if not enviado:
            log.warning("canário não enviado")
            return {"canario": "envio_falhou"}
        m["canario_em"] = int(agora)
        if self._esperar_canario(texto, int(agora)):
            m["canario_ok_em"] = int(agora)
            log.info("canário chegou: entrega ok")
            return {"canario": "chegou"}
        log.warning("canário não chegou em %d s: entrega travada", CANARIO_ESPERA_S)
        travada = {"canario": "travada", "evento": "entrega_travada"}
        if executor is None:
            return {**travada, "motivo": "entrega_travada"}
        if agora - (m.get("reiniciado_em") or 0) < REINICIO_SONDA_INTERVALO_S:
            return {**travada, "motivo": "entrega_travada_reinicio_recente"}
        try:
            self.reiniciar(executor)
        except (VigiaErro, OSError, subprocess.SubprocessError) as ex:
            log.warning("reinício pelo canário falhou: %s", causa(ex))
            return {**travada, "motivo": "entrega_travada_reinicio_falhou"}
        _, st = self._esperar_reconexao()
        if st == "open" and self._esperar_canario(texto, int(agora)):
            m["canario_ok_em"] = int(self._relogio())
            self._registrar_evento("entrega_travada", st, "reiniciada_voltou", rc)
            log.info("canário chegou depois do reinício: entrega destravada")
            return {"canario": "chegou_apos_reinicio", "reiniciado": True, "recuperacao": "reiniciada"}
        return {**travada, "motivo": "entrega_travada_reiniciada", "reiniciado": True}

    def monitorar(self, executor=None) -> dict:
        """Ciclo do coletor (só pelo vigia.sh; o painel lê `ultimo_monitor()`): estado() + reason_code + sonda + silêncio.

        Devolve {"evento": "queda|socket_morto|socket_zumbi|silencio|logout_sem_pareamento|ok", "avisar": bool,
        "estado", "motivo", "reason_code", "qrs_hoje"[, "logout": bool][, "sonda"][, "reiniciado": True][, "silencio_h"]}.
        `avisar` é True só na transição (open→close; 1º estado fechado de um número já pareado; 1ª vez que um socket
        morto aparece) e, para número pareado em connecting há mais de 1 h, a cada ciclo (D-24, sem logout). Nunca
        pareado + connecting sem sessão de QR ativa → logout (D-23). Sessão de QR vencida (qr_ate+15 s) com estado ≠
        open → logout mesmo com número pareado (M1) e qr_ate zerado. 428 "recente" = reason_em depois da rodada
        anterior (margem 60 s).
        Pedido 41 (socket zumbi): com `sonda_jid`, estado open passa pela sonda (_sondar); sonda que falha duas vezes
        = "socket_zumbi": com `executor` e sem reinício nas últimas REINICIO_SONDA_INTERVALO_S, reinicia a Evolution
        (motivo "…_reiniciada") e avisa; senão "…_persistente", avisando até ZUMBI_AVISOS_MAX vezes por episódio.
        Open sem mensagem nova há SILENCIO_AVISO_S dentro de SILENCIO_HORAS = "silencio" (avisa 1×/dia; nunca reinicia).
        D-56 (pedido 44): open, pareado, acordado e calado há CANARIO_SILENCIO_S dentro de SILENCIO_HORAS, com os dois
        ganchos do canário injetados e sem teste nas últimas CANARIO_REPETE_S → `_canario`: entrega provada segue "ok";
        travada → "entrega_travada" (reinicia e reconfere; avisa) com `recuperacao=reiniciada` quando o reinício destrava.
        Grava evento/motivo/reason_code em conexao.json. Levanta EvolutionIndisponivel, NaoAutorizado,
        CursorCorrompido.
        """
        with self._trava:
            e = self.evo.estado()
            st = e.get("state") or "close"
            agora = self._relogio()
            m = self._ler_monitor()
            prev = m.get("ultimo_estado")
            evento_prev = m.get("evento")
            base = max(m.get("atualizado_em") or 0, m.get("reiniciado_em") or 0)
            # D-54 (pedido 43): o Mac acordou depois do ciclo anterior? (epoch, "wake"|"darkwake") ou None
            despertar = self._despertar_desde(base)
            desp = despertar[0] if despertar else None
            escuro = bool(despertar and despertar[1] == "darkwake")   # manutenção: o Mac segue dormindo, nada a julgar
            recuperacao = None
            pareado = Cursor.carregar(self.cursor.caminho).pareado_em is not None
            if desp is not None and not escuro and st != "open" and pareado:
                # despertar pleno com a instância fechada: a Evolution ainda está voltando — espera a reconexão (até
                # DESPERTAR_ESPERA_S) em vez de julgar a primeira leitura; a premissa do Greg é recuperar ao ligar
                e, st = self._esperar_reconexao()
                if st == "open":
                    recuperacao = "reconectou"
            rc = e.get("reason_code")
            voltou_da_sonda = False
            reiniciado = False
            repareado = False
            sonda = ""
            silencio_h = None
            canario = None
            chave = [rc, e.get("reason_em")] if rc is not None else None
            em = _para_epoch(e.get("reason_em"))
            if base:
                recente = em is not None and em > base - MARGEM_428_S
            else:
                recente = em is not None and (agora - em) < self.janela_428_s
            sonda_adiada = None
            evento, avisar, motivo = "ok", False, None
            ultimo = st
            pendente0 = bool(m.get("logout_pendente"))
            pendente = pendente0
            logout_feito = None
            qr_vencido = False

            if st == "connecting":
                m["connecting_desde"] = m.get("connecting_desde") or agora
            else:
                m["connecting_desde"] = None

            if st == "open":
                m["instavel_desde"] = None
                pendente = False
                if prev in ("close", "connecting"):
                    motivo = "reconectado"           # o reason_code recente é da queda que acabou de passar
                    m["reason_visto"] = chave
                elif rc == 401 and chave is not None and chave != m.get("reason_visto"):
                    # 401 = o WhatsApp deslogou esta sessão (Parear de novo…, ou aparelho removido no celular). Se a instância
                    # está "open" de novo, houve pareamento novo entre dois ciclos: é volta, não socket morto (falso alarme
                    # visto no repareamento de 09/11 11:28, pedido 41).
                    motivo = "repareado"
                    m["reason_visto"] = chave
                    repareado = True
                    self._restampar_pareado_em(int(em) if em is not None else None)   # D-53
                elif chave is not None and recente and chave != m.get("reason_visto"):
                    evento, motivo = "socket_morto", "reason_code_recente"
                    avisar = chave != m.get("morto_avisado")
                    if avisar:
                        m["morto_avisado"] = chave
                m["queda_avisada"] = False
                if evento == "ok":
                    sonda = self._sondar()
                    m["sonda"] = sonda or None
                    suspeita = sonda == "falhou" or (sonda == "sem_foto" and m.get("sonda_foto_vista"))
                    if sonda == "ok":
                        m["sonda_foto_vista"] = True
                        m["sonda_ok_em"] = int(agora)
                        voltou_da_sonda = evento_prev == "socket_zumbi"
                        m["zumbi_avisos"] = 0
                    elif suspeita and desp is not None and escuro:
                        # D-54: despertar de manutenção (DarkWake — para o dono o Mac segue dormindo): nada a julgar;
                        # a sonda só condena num ciclo acordado
                        sonda_adiada = "despertar_escuro"
                    elif suspeita and desp is not None:
                        # D-54: despertar pleno — dá à Evolution até DESPERTAR_ESPERA_S para o socket voltar, resondando.
                        # Voltou: segue como ok. Não voltou: reinicia já, sem aviso (reiniciar é a recuperação esperada ao
                        # ligar) e o vigia.sh lê de novo neste ciclo; o aviso só sai no próximo ciclo, se persistir.
                        sonda = self._resondar()
                        m["sonda"] = sonda or None
                        if sonda == "ok":
                            m["sonda_foto_vista"] = True
                            m["sonda_ok_em"] = int(agora)
                            voltou_da_sonda = evento_prev == "socket_zumbi"
                            m["zumbi_avisos"] = 0
                            recuperacao = "sonda_voltou"
                        else:
                            evento = "socket_zumbi"
                            motivo = ("sonda_falhou" if sonda == "falhou" else "sonda_sem_foto") + "_apos_despertar"
                            reiniciado_em = m.get("reiniciado_em") or 0
                            if executor is not None and agora - reiniciado_em >= REINICIO_SONDA_INTERVALO_S:
                                try:
                                    self.reiniciar(executor)
                                    motivo += "_reiniciada"
                                    reiniciado = True
                                    recuperacao = "reiniciada"
                                except (VigiaErro, OSError, subprocess.SubprocessError) as ex:
                                    log.warning("reinício pela sonda falhou: %s", causa(ex))
                                    motivo += "_reinicio_falhou"
                            m["zumbi_avisos"] = 0          # o aviso fica para o próximo ciclo, se persistir
                    elif suspeita and sonda == "sem_foto" and self._mensagem_desde(base):
                        # chegou mensagem desde o ciclo anterior: socket vivo por prova; a foto nula é ruído
                        sonda_adiada = "mensagem_recente"
                    elif suspeita:
                        evento = "socket_zumbi"
                        motivo = "sonda_falhou" if sonda == "falhou" else "sonda_sem_foto"
                        avisos = int(m.get("zumbi_avisos") or 0)
                        reiniciado_em = m.get("reiniciado_em") or 0
                        if executor is not None and agora - reiniciado_em >= REINICIO_SONDA_INTERVALO_S:
                            try:
                                self.reiniciar(executor)
                                motivo += "_reiniciada"
                                reiniciado = True
                            except (VigiaErro, OSError, subprocess.SubprocessError) as ex:
                                log.warning("reinício pela sonda falhou: %s", causa(ex))
                                motivo += "_reinicio_falhou"
                            avisar = True
                            m["zumbi_avisos"] = 1          # novo episódio de reinício: conta do zero
                        else:
                            motivo += "_persistente"
                            avisar = avisos < ZUMBI_AVISOS_MAX
                            if avisar:
                                m["zumbi_avisos"] = avisos + 1
                if evento == "ok":
                    ultima = self.ultima_mensagem_em()
                    if ultima is not None:
                        silencio = agora - ultima
                        hora = datetime.fromtimestamp(agora).hour
                        if (silencio >= CANARIO_SILENCIO_S and hora in SILENCIO_HORAS and desp is None and pareado
                                and self._canario_enviar is not None and self._canario_chegou is not None
                                and agora - (m.get("canario_em") or 0) >= CANARIO_REPETE_S):
                            # D-56: calada há 2 h num horário em que não devia — prova a entrega antes de confiar no "open"
                            k = self._canario(agora, executor, m, rc)
                            canario = k["canario"]
                            reiniciado = reiniciado or bool(k.get("reiniciado"))
                            recuperacao = k.get("recuperacao") or recuperacao
                            if k.get("evento"):
                                evento, motivo, avisar = k["evento"], k["motivo"], True
                        if canario in ("chegou", "chegou_apos_reinicio"):
                            # entrega provada: o silêncio é só gente calada (o próximo ciclo lê o teste e zera o relógio)
                            m["silencio_avisado"] = None
                        elif evento == "ok" and silencio >= SILENCIO_AVISO_S:
                            silencio_h = int(silencio // 3600)
                            ja = m.get("silencio_avisado") or 0
                            evento, motivo = "silencio", f"sem_mensagem_{silencio_h}h"
                            if (hora in SILENCIO_HORAS and desp is None      # pedido 43: recém-acordado não avisa
                                    and (not ja or ja < ultima or agora - ja >= SILENCIO_REPETE_S)):
                                avisar = True
                                m["silencio_avisado"] = int(agora)
                        elif silencio < SILENCIO_AVISO_S:
                            m["silencio_avisado"] = None
            else:
                qr_ate = m.get("qr_ate")
                sessao_qr = qr_ate is not None and agora <= qr_ate + SESSAO_QR_FOLGA_S
                # M1: sessão de QR vencida com a Evolution fora de open = QR abandonado: desloga MESMO com número
                # pareado (mesma regra do tick do painel) e encerra a sessão. Sem sessão de QR valem D-23/D-24.
                qr_vencido = qr_ate is not None and not sessao_qr
                if qr_vencido:
                    m["qr_ate"] = None
                if st == "connecting" and not pareado and prev != "open" and not sessao_qr:
                    # D-23: connecting sem pareamento (nunca visto open) e sem clique recente = tentativa abandonada
                    logout_feito = self._logout_do_monitor()
                    pendente = not logout_feito
                    evento, motivo = "logout_sem_pareamento", "connecting_sem_pareamento"
                else:
                    if (pendente or qr_vencido) and not sessao_qr:
                        logout_feito = self._logout_do_monitor()
                        pendente = not logout_feito
                    caiu = st == "close"
                    if st == "connecting" and prev in ("open", None):
                        desde = m.get("instavel_desde") or agora
                        m["instavel_desde"] = desde
                        if agora - desde < self.janela_428_s:
                            motivo, ultimo = "reconectando", prev   # pode ser só um vai-e-vem; espera
                        else:
                            caiu = True
                    elif st == "connecting":
                        caiu = True
                    if caiu:
                        m["reason_visto"] = chave
                        if not pareado and prev != "open" and not m.get("queda_avisada"):
                            motivo = "nao_pareado"               # nunca houve conexão: não é queda
                        else:
                            evento = "queda"
                            avisar = prev == "open" or not m.get("queda_avisada")
                            m["queda_avisada"] = True
                            motivo = "caiu" if avisar else "continua_fora"
                            if avisar and desp is not None:
                                # D-54: ciclo do despertar (a reconexão já foi esperada, se pleno) — a queda só avisa
                                # (e só entra em conexao_eventos) no próximo ciclo, se persistir
                                avisar, motivo = False, "caiu_apos_despertar"
                                m["queda_avisada"] = False
                            m["instavel_desde"] = None
                            if (not avisar and st == "connecting" and pareado
                                    and agora - m["connecting_desde"] >= CONNECTING_AVISO_S):
                                avisar, motivo = True, "connecting_prolongado"   # D-24: repete, sem logout
            m["ultimo_estado"] = ultimo
            m["atualizado_em"] = int(agora)
            m["evento"], m["motivo"], m["reason_code"] = evento, motivo, rc
            if logout_feito:
                m["ultimo_logout_em"] = int(agora)
            mud = {k: m.get(k) for k in _CHAVES_MONITOR if k in m}
            if qr_vencido:
                mud["qr_ate"] = None
            if pendente != pendente0:
                mud["logout_pendente"] = pendente
            self._mesclar(**mud)
            # Fase A (D-45): histórico de quedas e voltas. Só transições: a repetição ("continua_fora",
            # "connecting_prolongado" a cada ciclo) não entra, senão a tabela mede o relógio, não a conexão.
            if evento == "logout_sem_pareamento" or (evento != "ok" and avisar and motivo != "connecting_prolongado"):
                self._registrar_evento(evento, st, motivo, rc)
            elif st == "open" and prev in ("close", "connecting"):
                self._registrar_evento("voltou", st, motivo, rc)
            elif voltou_da_sonda:
                self._registrar_evento("voltou", st, "sonda_ok", rc)
            elif repareado:
                self._registrar_evento("voltou", st, "repareado", rc)
            log.info("monitor evento=%s avisar=%s estado=%s sonda=%s%s%s%s%s", evento, avisar, st, sonda or "-",
                     f" sonda_adiada={sonda_adiada}" if sonda_adiada else "",
                     f" despertar={despertar[1]}@{despertar[0]}" if despertar else "",
                     f" recuperacao={recuperacao}" if recuperacao else "",
                     f" canario={canario}" if canario else "")
            r = {"evento": evento, "avisar": avisar, "estado": st, "motivo": motivo,
                 "reason_code": rc, "qrs_hoje": self.banco.qrs_hoje()}
            if logout_feito is not None:
                r["logout"] = logout_feito
            if sonda:
                r["sonda"] = sonda
            if reiniciado:
                r["reiniciado"] = True
            if silencio_h is not None:
                r["silencio_h"] = silencio_h
            if canario:
                r["canario"] = canario
            if sonda_adiada:
                r["sonda_adiada"] = sonda_adiada
            if despertar is not None:
                r["despertou_em"], r["despertar"] = despertar
            if recuperacao:
                r["recuperacao"] = recuperacao
            return r

    def _registrar_evento(self, evento: str, estado: str, motivo, reason_code) -> None:
        """conexao_eventos (fase A). Banco indisponível não derruba o monitor: o evento já está em conexao.json e no log."""
        try:
            self.banco.registrar_evento_conexao(evento=evento, estado=estado, motivo=motivo, reason_code=reason_code)
        except sqlite3.Error as e:
            log.warning("evento de conexão %s não gravado: %s", evento, causa(e))

    def reiniciar(self, executor) -> int:
        """Reinicia a Evolution (caso 428): executor(["bin/evolution.sh", "restart"], cwd=raiz[, timeout=120]).

        Com QR em andamento (gerando/renovando) faz logout() ANTES do restart. Devolve o código de saída do
        executor (0 se ele não devolve nada); código != 0 levanta VigiaErro. Exceções do executor propagam.
        Grava reiniciado_em em conexao.json: o monitor reavalia o 428 no ciclo seguinte.
        """
        with self._trava:
            if self.estado in ("gerando", "renovando"):
                self._expirar()
            kw = {"cwd": str(RAIZ)}
            try:
                ps = inspect.signature(executor).parameters.values()
                if any(p.name == "timeout" or p.kind is p.VAR_KEYWORD for p in ps):
                    kw["timeout"] = EXECUTOR_TIMEOUT_S
            except (TypeError, ValueError):
                pass
            r = executor(["bin/evolution.sh", "restart"], **kw)
            codigo = getattr(r, "returncode", r) if r is not None else 0
            if isinstance(codigo, bool) or not isinstance(codigo, int):
                codigo = 0
            if codigo != 0:
                raise VigiaErro(f"reinício da Evolution terminou com código {codigo}")
            self.estado = "ocioso"
            self._qr = None
            self._emitidos = 0
            self._tentativas = 0
            try:
                self._mesclar(reiniciado_em=int(self._relogio()), qr_ate=None)
            except OSError as e:
                log.warning("não consegui gravar reiniciado_em: %s", causa(e))
            log.info("evolution reiniciada")
            return codigo


# ---------------------------------------------------------------------- CLI

def dados_dir(raiz, config: dict | None = None) -> Path:
    """Pasta de dados: VIGIA_DADOS do ambiente > `dados_dir` do config.toml > <raiz>/dados (mesma ordem do vigia.sh)."""
    bruto = os.environ.get("VIGIA_DADOS") or (config or {}).get("dados_dir")
    return Path(bruto) if bruto else Path(raiz) / "dados"


def _ler_env(caminho: Path) -> dict:
    """Lê .env simples (KEY=VALOR, comentário depois de ' #'); o ambiente do processo tem precedência."""
    out: dict = {}
    try:
        linhas = caminho.read_text(encoding="utf-8").splitlines()
    except OSError:
        linhas = []
    for linha in linhas:
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        k, v = linha.split("=", 1)
        v = v.split(" #", 1)[0].split("\t#", 1)[0].strip().strip('"').strip("'")
        out[k.strip()] = v
    for k in ("EVOLUTION_URL", "EVOLUTION_INSTANCE", "EVOLUTION_INSTANCE_APIKEY", "DONO_WHATSAPP"):
        if os.environ.get(k):
            out[k] = os.environ[k]
    return out


def main(argv=None, *, saida=None, relogio=time.time) -> int:
    """`python3 -m app.conexao --monitorar [--raiz DIR]`: roda monitorar(), imprime JSON; exit 0 ok · 1 falhou · 2 não rodou."""
    import argparse
    ap = argparse.ArgumentParser(prog="python3 -m app.conexao", description="Monitor de conexão do Greg (WhatsApp).")
    ap.add_argument("--monitorar", action="store_true", help="roda um ciclo do monitor e imprime o evento em JSON")
    ap.add_argument("--raiz", default=str(RAIZ), help="raiz do projeto (.env, config.toml, dados/); padrão: a deste código")
    args = ap.parse_args(argv)
    saida = saida or sys.stdout
    if not args.monitorar:
        ap.print_help(saida)
        return 2
    raiz = Path(args.raiz)
    env = segredos.completar(_ler_env(raiz / ".env"), ("EVOLUTION_INSTANCE_APIKEY",))  # D-45: Keychain opt-in, desligado por padrão
    faltam = [k for k in ("EVOLUTION_URL", "EVOLUTION_INSTANCE", "EVOLUTION_INSTANCE_APIKEY") if not env.get(k)]
    if faltam:
        print(json.dumps({"evento": "nao_rodou", "avisar": False, "faltam": faltam}), file=saida)
        return 2
    try:
        try:
            config = tomllib.loads((raiz / "config.toml").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            config = {}
        evo = EvolutionClient(env["EVOLUTION_URL"], env["EVOLUTION_INSTANCE"], env["EVOLUTION_INSTANCE_APIKEY"])
        dados = dados_dir(raiz, config)
        banco = Banco(str(dados / "vigia.db"))
        cursor = Cursor.carregar(dados / "cursor.json")
        # D-56: o canário manda pelo bot (app.aviso, DONO_WHATSAPP/AVISO_INSTANCIA exportados pelo vigia.sh) e confere no
        # Postgres pelo mesmo psql-ro-stdin do coletor (a query vai pelo stdin). Sem os dois, o monitor roda sem canário.
        psql = lotes.psql_via_script(raiz / "bin" / "evolution.sh", "psql-ro-stdin", 20.0)
        con = Conexao.de_config(evo, banco, config, cursor=cursor, relogio=relogio,
                                estado_arq=dados / "conexao.json", sonda_jid=env.get("DONO_WHATSAPP") or None,
                                ultima_arq=dados / "ultima-mensagem.json",
                                canario_enviar=aviso.enviar, canario_chegou=functools.partial(lotes.canario_chegou, psql))
        # O reinício automático (socket zumbi) roda bin/evolution.sh restart com a saída capturada: o stdout deste
        # processo é UMA linha JSON que o vigia.sh parseia.
        r = con.monitorar(executor=functools.partial(subprocess.run, capture_output=True))
        texto = json.dumps(r, ensure_ascii=False)
        saude.do_monitor(dados, r)          # pedido 63, lote 2: a central de avisos sabe do WhatsApp
        saude.notificar(dados)
    except Exception as e:  # saída é SEMPRE uma linha JSON; nunca traceback (o vigia.sh parseia o stdout)
        log.warning("monitor falhou: %s", causa(e))
        saude.do_monitor(dados_dir(raiz), {"evento": "erro", "erro": type(e).__name__})
        print(json.dumps({"evento": "erro", "avisar": False, "erro": type(e).__name__}), file=saida)
        return 1
    print(texto, file=saida)
    return 0


if __name__ == "__main__":
    sys.exit(main())
