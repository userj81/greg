"""Envio agendado (pedido 72, D-70): uma mensagem ou um arquivo que a pessoa escreve agora e o Greg manda na hora marcada.
Quem chama: app/paginas/conversas.py (agendar, cancelar, faixa da conversa), app/paginas/envios.py (lista) e
app/server.py (cria e inicia um Agendador por processo).
Onde guarda: `dados/agendados/` (0700) com `lista.json` (0600) e um `<id>.bin` (0600) por arquivo anexado.
É a única exceção à regra "o texto de envio não vai para o disco" (D-44): sem guardar, não há o que mandar depois.
O texto e o arquivo saem do disco no instante em que o item deixa de estar agendado (enviado, cancelado, vencido,
falhou); fica só o hash, o destino, a hora e o estado.
O que não faz: não manda para bloqueado (D-41), não insiste depois da tolerância (o item vence e a tela avisa), não
repete um envio incerto, não agenda para vários de uma vez e não escreve texto nem nome de arquivo em log.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path

from app import alertas, whatsapp as wa
from app.erros import EnvioIncerto, VigiaErro, causa

log = logging.getLogger("vigia.agendados")

TOLERANCIA_S = 3600          # passou disso da hora marcada sem conseguir mandar: vence, não manda atrasado
ANTECEDENCIA_S = 60          # o mais cedo que se agenda
HORIZONTE_S = 90 * 86400     # o mais longe que se agenda
TETO_PENDENTES = 50
GUARDA_TERMINAIS = 50        # quantos itens já resolvidos a lista mantém (sem texto)
PASSO_S = 20.0
ENTRE_ENVIOS_S = 4.0
TIPOS = ("texto", "foto", "video", "arquivo")
TERMINAIS = ("enviado", "incerto", "vencido", "falhou", "cancelado")
MOTIVO_VENCIDO = "passou mais de 1 hora do horário e o Greg não conseguiu mandar"


class AgendaInvalida(Exception):
    """Pedido recusado; `status` é o HTTP e a mensagem pode ir para a tela."""

    def __init__(self, status: int, mensagem: str):
        super().__init__(mensagem)
        self.status = status


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class Agenda:
    """Lista em disco, uma por processo do painel. Toda leitura e escrita passa pela mesma trava."""

    def __init__(self, pasta: Path, relogio=time.time):
        self.pasta = Path(pasta)
        self.relogio = relogio
        self._trava = threading.RLock()

    # ------------------------------------------------------------------ disco

    def _arq(self) -> Path:
        return self.pasta / "lista.json"

    def _bin(self, id_: int) -> Path:
        return self.pasta / f"{int(id_)}.bin"

    def _ler(self) -> dict:
        try:
            d = json.loads(self._arq().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"proximo": 1, "itens": []}
        if not isinstance(d, dict) or not isinstance(d.get("itens"), list):
            raise VigiaErro("lista de agendados ilegível")
        return d

    def _gravar(self, d: dict) -> None:
        self.pasta.mkdir(parents=True, exist_ok=True)
        os.chmod(self.pasta, 0o700)
        tmp = self._arq().with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, self._arq())

    def _apagar_bin(self, id_: int) -> None:
        try:
            self._bin(id_).unlink()
        except FileNotFoundError:
            pass

    @staticmethod
    def _publico(it: dict) -> dict:
        """O item sem o texto: é o que as telas recebem (a prévia vai à parte, curta)."""
        return {k: v for k, v in it.items() if k != "texto"}

    # ------------------------------------------------------------------ criar, listar, cancelar

    def criar(self, para: str, quando: int, *, texto: str = "", tipo: str = "texto", arquivo: bytes | None = None,
              nome: str = "", mime: str = "") -> dict:
        agora = int(self.relogio())
        if tipo not in TIPOS:
            raise AgendaInvalida(400, "Tipo de envio desconhecido.")
        if not isinstance(quando, int) or quando < agora + ANTECEDENCIA_S:
            raise AgendaInvalida(400, "Escolha um horário pelo menos 1 minuto à frente.")
        if quando > agora + HORIZONTE_S:
            raise AgendaInvalida(400, "O Greg agenda até 90 dias à frente.")
        if tipo == "texto":
            if not texto.strip():
                raise AgendaInvalida(400, "Escreva a mensagem antes de agendar.")
            h = _sha(texto.encode("utf-8"))
        else:
            if not arquivo:
                raise AgendaInvalida(400, "Arquivo vazio.")
            h = _sha(arquivo)
        with self._trava:
            d = self._ler()
            if sum(1 for i in d["itens"] if i["estado"] == "agendado") >= TETO_PENDENTES:
                raise AgendaInvalida(429, f"Já há {TETO_PENDENTES} envios agendados. Cancele algum ou espere saírem.")
            id_ = int(d.get("proximo") or 1)
            it = {"id": id_, "para": para, "quando": quando, "tipo": tipo, "texto": texto, "nome": nome, "mime": mime,
                  "tamanho": len(arquivo) if arquivo else 0, "hash": h, "estado": "agendado", "motivo": "",
                  "criado_em": agora, "resolvido_em": 0, "msg_id": ""}
            if arquivo:
                self.pasta.mkdir(parents=True, exist_ok=True)
                os.chmod(self.pasta, 0o700)
                fd = os.open(self._bin(id_), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(arquivo)
            d["itens"].append(it)
            d["proximo"] = id_ + 1
            self._gravar(d)
        log.info("envio agendado id=%d tipo=%s", id_, tipo)
        return self._publico(it)

    def listar(self, para: str | None = None, so_pendentes: bool = False) -> list[dict]:
        """Agendados primeiro (o mais próximo em cima), depois os resolvidos (o mais recente em cima). Cada item leva
        `previa` (até 80 caracteres do texto ou da legenda) enquanto está agendado; resolvido não tem mais texto."""
        with self._trava:
            itens = self._ler()["itens"]
        saida = []
        for it in itens:
            if para is not None and it["para"] != para:
                continue
            if so_pendentes and it["estado"] != "agendado":
                continue
            p = self._publico(it)
            p["previa"] = " ".join((it.get("texto") or "").split())[:80]
            saida.append(p)
        saida.sort(key=lambda i: (0, i["quando"], i["id"]) if i["estado"] == "agendado" else (1, -i["resolvido_em"], -i["id"]))
        return saida

    def cancelar(self, id_: int) -> dict | None:
        """Cancela um item ainda agendado; None se não existe ou já foi resolvido."""
        return self._resolver(id_, "cancelado", "", so_se_agendado=True)

    def _resolver(self, id_: int, estado: str, motivo: str, *, msg_id: str = "", so_se_agendado: bool = False) -> dict | None:
        with self._trava:
            d = self._ler()
            achado = next((i for i in d["itens"] if i["id"] == id_), None)
            if achado is None or (so_se_agendado and achado["estado"] != "agendado"):
                return None
            achado.update(estado=estado, motivo=motivo, texto="", msg_id=msg_id, resolvido_em=int(self.relogio()))
            self._apagar_bin(id_)
            fim = sorted((i for i in d["itens"] if i["estado"] in TERMINAIS), key=lambda i: (i["resolvido_em"], i["id"]))
            sobra = {i["id"] for i in fim[:-GUARDA_TERMINAIS]} if len(fim) > GUARDA_TERMINAIS else set()
            d["itens"] = [i for i in d["itens"] if i["id"] not in sobra]
            self._gravar(d)
            return self._publico(achado)

    # ------------------------------------------------------------------ para o agendador

    def devido(self) -> dict | None:
        """O próximo item cuja hora chegou (com texto), ou None."""
        agora = int(self.relogio())
        with self._trava:
            dev = [i for i in self._ler()["itens"] if i["estado"] == "agendado" and i["quando"] <= agora]
        return min(dev, key=lambda i: (i["quando"], i["id"])) if dev else None

    def bytes_de(self, id_: int) -> bytes | None:
        try:
            return self._bin(id_).read_bytes()
        except OSError:
            return None


class Agendador:
    """Thread do painel que manda o que venceu a hora. `ctx` traz evo, banco, psql (opcional) e config."""

    def __init__(self, ctx, agenda: Agenda, relogio=time.time):
        self.ctx = ctx
        self.agenda = agenda
        self.relogio = relogio
        self._cond = threading.Condition()
        self._thread: threading.Thread | None = None
        self._encerrar = False
        # M1 (robustez 16/11): item que JÁ SAIU mas cujo estado não gravou (disco cheio, lista travada). Enquanto
        # estiver aqui, o passo só tenta gravar de novo e nunca manda outra vez. Fica em memória: depois de um
        # reinício com o disco ainda cheio, o item pode sair uma vez a mais (antes saía a cada passo por até 1 h).
        self._saiu: dict[int, tuple[str, str, str, str | None]] = {}

    def iniciar(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._laco, name="vigia-agendados", daemon=True)
            self._thread.start()

    def encerrar(self) -> None:
        with self._cond:
            self._encerrar = True
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def acordar(self) -> None:
        with self._cond:
            self._cond.notify_all()

    def _laco(self) -> None:
        while True:
            try:
                espera = self.passo()
            except Exception as e:   # a thread não morre por um item; nada do texto vai ao log
                log.error("passo dos agendados falhou: %s", causa(e))
                espera = PASSO_S
            with self._cond:
                if self._encerrar:
                    return
                self._cond.wait(espera)
                if self._encerrar:
                    return

    def _estado_conexao(self) -> str | None:
        dados = Path((getattr(self.ctx, "config", None) or {}).get("dados_dir", "dados"))
        try:
            d = json.loads((dados / "conexao.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        e = d.get("ultimo_estado") if isinstance(d, dict) else None
        return e if isinstance(e, str) and e else None

    def _registrar(self, para: str, h: str, por: str) -> None:
        """O registro roda depois que a mensagem saiu: se falhar, o item é resolvido do mesmo jeito. Deixar a exceção
        subir manteria o item agendado e o próximo passo mandaria de novo."""
        try:
            self.ctx.banco.registrar_envio(para=para, hash_texto=h, por=por)
        except Exception as e:  # noqa: BLE001
            log.error("agendado saiu mas o registro falhou (por=%s): %s", por, causa(e))

    def _avisar(self, codigo: str) -> None:
        """O agendado não saiu (ou ficou incerto) e pode não haver ninguém na tela: abre o aviso na central e notifica
        na hora. Só o código vai para a central; nada do texto nem do destino. Nunca derruba o passo."""
        try:
            from app import saude
            dados = Path((getattr(self.ctx, "config", None) or {}).get("dados_dir", "dados"))
            alertas.abrir(dados, codigo)
            saude.notificar(dados)
        except Exception as e:  # noqa: BLE001
            log.error("aviso do agendado não saiu: %s", causa(e))

    def passo(self) -> float:
        """Trata UM item devido e devolve quantos segundos esperar até o próximo passo. Os testes chamam direto."""
        it = self.agenda.devido()
        if it is None:
            return PASSO_S
        id_, para = it["id"], it["para"]
        if id_ in self._saiu:                  # já saiu: só falta gravar o estado; nunca manda de novo
            self._fechar(id_, *self._saiu[id_])
            return ENTRE_ENVIOS_S
        atraso = int(self.relogio()) - it["quando"]
        if atraso > TOLERANCIA_S:
            self.agenda._resolver(id_, "vencido", MOTIVO_VENCIDO)
            self._avisar("agendado-nao-saiu")
            log.warning("envio agendado id=%d venceu sem sair", id_)
            return ENTRE_ENVIOS_S
        cx = self._estado_conexao()
        if cx is not None and cx != "open":
            return PASSO_S                     # espera a conexão voltar; se passar da tolerância, vence
        psql = getattr(self.ctx, "psql", None)
        if psql is not None:
            try:
                bloq = wa.bloqueados(psql)
            except (VigiaErro, ValueError) as e:
                log.warning("bloqueios indisponíveis para os agendados: %s", causa(e))
                return PASSO_S                 # sem saber quem está bloqueado, não manda (D-41)
            if para in bloq:
                self.agenda._resolver(id_, "falhou", "essa conversa está fora do Greg; nada foi enviado")
                self._avisar("agendado-nao-saiu")
                return ENTRE_ENVIOS_S
        numero = para if para.endswith("@g.us") else wa.digitos(para)
        evo, tipo, por = self.ctx.evo, it["tipo"], f"painel:agendado:{it['tipo']}"
        if tipo == "texto":
            acao = lambda: evo.enviar_texto(numero, it["texto"])
        else:
            dados = self.agenda.bytes_de(id_)
            if not dados:
                self.agenda._resolver(id_, "falhou", "o arquivo guardado não foi encontrado; nada foi enviado")
                self._avisar("agendado-nao-saiu")
                return ENTRE_ENVIOS_S
            especie = {"foto": "image", "video": "video", "arquivo": "document"}[tipo]
            acao = lambda: evo.enviar_midia(numero, especie, dados, nome=it["nome"], mime=it["mime"], legenda=it["texto"])
        try:
            mid = acao()
        except EnvioIncerto:
            self._registrar(para, it["hash"], por + ":incerto")
            log.warning("envio agendado id=%d incerto", id_)
            self._fechar(id_, "incerto", "pode ter saído; confira no celular antes de repetir", "", "agendado-incerto")
        except VigiaErro as e:
            log.warning("envio agendado id=%d falhou, tenta de novo: %s", id_, causa(e))
            return PASSO_S                     # nada saiu: tenta de novo até a tolerância
        else:
            self._registrar(para, it["hash"], por)
            log.info("envio agendado id=%d saiu", id_)
            self._fechar(id_, "enviado", "", mid or "", None)
        return ENTRE_ENVIOS_S

    def _fechar(self, id_: int, estado: str, motivo: str, msg_id: str, aviso: str | None) -> None:
        """Grava o estado de um item que já saiu (ou ficou incerto). A guarda entra ANTES da gravação: se gravar
        falhar, a exceção sobe como antes (o laço loga e espera), mas o próximo passo só tenta gravar de novo."""
        self._saiu[id_] = (estado, motivo, msg_id, aviso)
        self.agenda._resolver(id_, estado, motivo, msg_id=msg_id)
        self._saiu.pop(id_, None)
        if aviso:
            self._avisar(aviso)


def da(ctx) -> Agenda:
    """A Agenda do processo (criada no primeiro uso; o servidor cria junto com o Agendador)."""
    a = getattr(ctx, "agenda", None)
    if a is None:
        dados = Path((getattr(ctx, "config", None) or {}).get("dados_dir", "dados"))
        a = ctx.agenda = Agenda(dados / "agendados")
    return a
