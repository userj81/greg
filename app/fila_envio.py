"""Fila única de envio da API local (pedido 10, R-15, D-44): tudo que uma ferramenta do dono manda pelo Greg passa aqui,
em ordem, um por vez, com pausa aleatória entre envios e tetos conferidos no servidor.
Quem chama: app/paginas/envios.py (POST /envios, parar, retomar, estado), app/paginas/enviar.py (lote de 1) e
app/server.py (cria e inicia uma Fila por processo).
O que não faz: não grava texto em disco nem em log (só hash; o texto vive em memória enquanto o lote anda), não retoma
sozinha depois de reiniciar (R-9: retomar é pedido de fora), não insiste depois de uma falha (pausa o lote), não manda
para número bloqueado (D-41) e não é alcançável pelo coletor (sem token de envio no ambiente dele).
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import threading
import time
from datetime import date, datetime, time as hora_t, timedelta
from pathlib import Path

from app import whatsapp as wa
from app.cursor import Cursor
from app.erros import EnvioIncerto, VigiaErro, causa
from app.lotes import PostgresIndisponivel

log = logging.getLogger("vigia.fila_envio")

ORIGEM_OK = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
IDEM_OK = re.compile(r"^[A-Za-z0-9._:-]{1,120}$")
ATIVOS = ("na_fila", "enviando", "aguardando_conexao")
TERMINAIS = ("pausado", "parado", "interrompido", "concluido")
ESPERA_CONEXAO_S = 30.0
ESPERA_OCIOSA_S = 30.0
MOTIVO_REINICIO = "o painel reiniciou e o texto não fica em disco: repita o pedido com a mesma idempotencia"
PADRAO = {"teto_lote": 20, "teto_dia_conhecidos": 30, "teto_dia_novos": 10,
          "intervalo_s": (45, 90), "intervalo_novos_s": (90, 180)}


class PedidoInvalido(Exception):
    """Pedido recusado antes de entrar na fila: `status` HTTP, `mensagem` e campos extras para o JSON."""

    def __init__(self, status: int, mensagem: str, **extra):
        super().__init__(mensagem)
        self.status, self.mensagem, self.extra = int(status), mensagem, extra


def _limitar(v, lo, hi, padrao):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return padrao
    return min(max(n, lo), hi)


def _faixa(v, padrao):
    if isinstance(v, (list, tuple)) and len(v) == 2:
        lo, hi = _limitar(v[0], 0, 3600, padrao[0]), _limitar(v[1], 0, 3600, padrao[1])
        return (lo, hi) if lo <= hi else (hi, lo)
    return padrao


def limites_de_config(config: dict | None) -> dict:
    """[envio] do config.toml com faixas: teto_lote [1,50], tetos diários [0,200], intervalos [0,3600] s."""
    cfg = (config or {}).get("envio") or {}
    return {"teto_lote": _limitar(cfg.get("teto_lote"), 1, 50, PADRAO["teto_lote"]),
            "teto_dia_conhecidos": _limitar(cfg.get("teto_dia_conhecidos"), 0, 200, PADRAO["teto_dia_conhecidos"]),
            "teto_dia_novos": _limitar(cfg.get("teto_dia_novos"), 0, 200, PADRAO["teto_dia_novos"]),
            "intervalo_s": _faixa(cfg.get("intervalo_s"), PADRAO["intervalo_s"]),
            "intervalo_novos_s": _faixa(cfg.get("intervalo_novos_s"), PADRAO["intervalo_novos_s"])}


def normalizar_destino(bruto) -> str:
    """'para' do pedido -> jid. Aceita jid de pessoa/grupo ou dígitos (DDD+número ganha 55); @lid é recusado."""
    if not isinstance(bruto, str) or not bruto.strip():
        raise ValueError("destinatário vazio")
    txt = bruto.strip()
    if "@" in txt:
        if wa.JID_OK.match(txt) and not txt.endswith("@lid"):
            return txt
        raise ValueError("destinatário fora do padrão (jid de pessoa ou grupo)")
    d = wa.normalizar_numero(txt)
    if not (8 <= len(d) <= 15):
        raise ValueError("número fora do padrão: use DDD e número, ou +DDI para fora do Brasil")
    return f"{d}@s.whatsapp.net"


def texto_final(modelo: str, nome: str) -> str:
    """{nome} vira o nome; sem nome, o marcador sai e o espaço sobrando ao redor é limpo."""
    if "{nome}" not in modelo:
        return modelo
    if nome:
        return modelo.replace("{nome}", nome)
    t = modelo.replace("{nome}", "")
    t = re.sub(r" {2,}", " ", t)
    t = re.sub(r" +([,.!?;:])", r"\1", t)
    return "\n".join(linha.strip() for linha in t.split("\n"))


def _sha(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def _meia_noite_seguinte() -> str:
    return datetime.combine(date.today() + timedelta(days=1), hora_t.min).isoformat(sep=" ", timespec="minutes")


class Fila:
    """Uma por processo do painel. `ctx` traz banco, evo, psql (opcional) e config; lidos na hora de usar."""

    def __init__(self, ctx, *, limites: dict | None = None, relogio=time.time, aleatorio=random.uniform):
        self.ctx = ctx
        self.limites = {**PADRAO, **(limites or {})}
        self.relogio = relogio
        self.aleatorio = aleatorio
        self._textos: dict[int, str] = {}
        self._sem_conferir: set[int] = set()   # lotes legados (/enviar) não conferem o número na Evolution
        self._cond = threading.Condition()
        self._passo = threading.Lock()
        self._proximo_em = 0.0
        # M2 (robustez 16/11): destinatário que JÁ SAIU mas cujo estado não gravou (SQLite travado ou disco cheio).
        # Enquanto estiver aqui, o passo só tenta gravar de novo e nunca manda outra vez. Fica em memória: o reinício
        # interrompe os lotes (MOTIVO_REINICIO), então ninguém manda de novo depois dele.
        self._saiu: dict[int, tuple[str, str, str | None, str, str]] = {}
        self._thread: threading.Thread | None = None
        self._encerrar = False
        n = self.ctx.banco.interromper_lotes_envio(MOTIVO_REINICIO)
        if n:
            log.warning("%d lote(s) de envio interrompido(s) por reinício", n)

    # ------------------------------------------------------------------ thread

    def iniciar(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._laco, name="vigia-fila-envio", daemon=True)
        self._thread.start()

    def encerrar(self) -> None:
        with self._cond:
            self._encerrar = True
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _laco(self) -> None:
        while True:
            with self._cond:
                if self._encerrar:
                    return
            try:
                espera = self.processar_uma()
            except Exception as e:  # a fila nunca morre por um erro de um passo; nada do texto vai ao log
                log.error("passo da fila de envio falhou: %s", causa(e))
                espera = 5.0
            with self._cond:
                if self._encerrar:
                    return
                if espera > 0:
                    self._cond.wait(espera)

    def _acordar(self) -> None:
        with self._cond:
            self._cond.notify_all()

    # ------------------------------------------------------------------ leitura do ambiente

    def _dados(self) -> Path:
        return Path((getattr(self.ctx, "config", None) or {}).get("dados_dir", "dados"))

    def _estado_conexao(self) -> str | None:
        """ultimo_estado gravado pelo monitor em conexao.json (0 chamada à Evolution); None = sem informação."""
        try:
            d = json.loads((self._dados() / "conexao.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        e = d.get("ultimo_estado") if isinstance(d, dict) else None
        return e if isinstance(e, str) and e else None

    def _desde(self) -> int | None:
        try:
            return Cursor.carregar(self._dados() / "cursor.json", reparar=False).desde_tela
        except Exception:
            return None

    def _bloqueados(self) -> set[str]:
        psql = getattr(self.ctx, "psql", None)
        if psql is None:
            return set()
        return wa.bloqueados(psql)

    def _conversas(self) -> dict[str, dict] | None:
        """{jid: linha de wa.listar} dentro da janela de leitura (Cursor.desde); None se não dá para saber (sem psql, sem pareamento, banco fora)."""
        psql, par = getattr(self.ctx, "psql", None), self._desde()
        if psql is None or par is None:
            return None
        try:
            return {x["jid"]: x for x in wa.listar(psql, desde=par)}
        except (VigiaErro, ValueError) as e:
            log.warning("lista de conversas indisponível para a fila: %s", causa(e))
            return None

    # ------------------------------------------------------------------ criar

    def criar(self, origem, texto, destinatarios, idempotencia=None, *, conferir: bool = True) -> dict:
        """Valida, aplica tetos e grava o lote. Devolve estado(lote) + 'repetido' (True quando a idempotencia já existia).
        Levanta PedidoInvalido (400 dado ruim, 409 idempotencia com outro texto, 429 teto do dia)."""
        if not isinstance(origem, str) or not ORIGEM_OK.match(origem):
            raise PedidoInvalido(400, "origem inválida: letras minúsculas, dígitos e hífen, até 40")
        if not isinstance(texto, str) or not texto.strip() or len(texto) > wa.LIMITE_TEXTO:
            raise PedidoInvalido(400, f"texto vazio ou acima de {wa.LIMITE_TEXTO} caracteres")
        if idempotencia is not None and (not isinstance(idempotencia, str) or not IDEM_OK.match(idempotencia)):
            raise PedidoInvalido(400, "idempotencia inválida: até 120 caracteres de [A-Za-z0-9._:-]")
        teto_lote = self.limites["teto_lote"]
        if not isinstance(destinatarios, list) or not 1 <= len(destinatarios) <= teto_lote:
            raise PedidoInvalido(400, f"destinatarios: lista de 1 a {teto_lote} por lote")
        h = _sha(texto)
        banco = self.ctx.banco
        if idempotencia:
            ant = banco.lote_envio_por_idempotencia(origem, idempotencia)
            if ant is not None:
                if ant["hash_texto"] != h:
                    raise PedidoInvalido(409, "mesma idempotencia com texto diferente; use outra chave")
                self._textos.setdefault(ant["id"], texto)
                if ant["estado"] in ("pausado", "parado", "interrompido"):
                    self._retomar(ant["id"])
                return {**self.estado(ant["id"]), "repetido": True}
        vistos, pares = set(), []
        for item in destinatarios:
            if not isinstance(item, dict):
                raise PedidoInvalido(400, "cada destinatário é um objeto {para, nome?}")
            try:
                jid = normalizar_destino(item.get("para"))
            except ValueError as e:
                raise PedidoInvalido(400, str(e)) from None
            if jid in vistos:
                raise PedidoInvalido(400, "destinatário repetido no mesmo lote")
            vistos.add(jid)
            nome = item.get("nome")
            pares.append((jid, " ".join(str(nome).split())[:80] if isinstance(nome, str) else ""))
        try:
            bloq = self._bloqueados()
        except (VigiaErro, ValueError):
            bloq = set()
        apelidos = banco.apelidos()
        conversas = self._conversas()
        linhas = []
        for jid, nome in pares:
            conhecido = jid.endswith("@g.us") or jid in apelidos or (conversas is not None and jid in conversas)
            if not nome:
                nome = apelidos.get(jid, "")
            if not nome and "{nome}" in texto and conversas is not None and jid in conversas and not jid.endswith("@g.us"):
                x = conversas[jid]
                cand = wa.nome_da_conversa(jid, contato=x.get("contato", ""), push=x.get("push", ""))
                nome = "" if cand == wa.numero_formatado(jid) else cand
            linha = {"para": jid, "nome": nome, "conhecido": conhecido, "estado": "fila", "motivo": ""}
            if jid in bloq:
                linha.update(estado="pulado", motivo="bloqueado")
            linhas.append(linha)
        hoje, reserv = banco.envios_api_hoje(), banco.reservados_envio()
        for cat in ("conhecidos", "novos"):
            n = sum(1 for l in linhas if l["estado"] == "fila" and (l["conhecido"] == (cat == "conhecidos")))
            teto = self.limites[f"teto_dia_{cat}"]
            if n and hoje[cat] + reserv[cat] + n > teto:
                raise PedidoInvalido(429, f"teto do dia de {cat} ({teto}) seria ultrapassado",
                                     restante=max(0, teto - hoje[cat] - reserv[cat]), reinicia_em=_meia_noite_seguinte())
        lote_id = banco.criar_lote_envio(origem=origem, idempotencia=idempotencia, hash_texto=h, destinos=linhas)
        self._textos[lote_id] = texto
        if not conferir:
            self._sem_conferir.add(lote_id)
        if all(l["estado"] != "fila" for l in linhas):
            banco.atualizar_lote_envio(lote_id, "concluido", self._resumo_destinos(banco.destinos_envio(lote_id)))
        log.info("lote de envio %d criado origem=%s n=%d", lote_id, origem, len(linhas))
        self._acordar()
        return {**self.estado(lote_id), "repetido": False}

    # ------------------------------------------------------------------ estado

    @staticmethod
    def _contagem(destinos: list[dict]) -> dict:
        c = {"fila": 0, "enviados": 0, "incertos": 0, "pulados": 0, "cancelados": 0}
        chave = {"fila": "fila", "enviado": "enviados", "incerto": "incertos", "pulado": "pulados", "cancelado": "cancelados"}
        for d in destinos:
            k = chave.get(d["estado"])
            if k:
                c[k] += 1
        return c

    @classmethod
    def _resumo_destinos(cls, destinos: list[dict]) -> str:
        c = cls._contagem(destinos)
        partes = [f"{c['enviados']} enviada(s)"]
        if c["incertos"]:
            partes.append(f"{c['incertos']} incerta(s)")
        if c["pulados"]:
            partes.append(f"{c['pulados']} pulada(s)")
        if c["cancelados"]:
            partes.append(f"{c['cancelados']} cancelada(s)")
        return " · ".join(partes)

    def estado(self, lote_id: int) -> dict | None:
        lote = self.ctx.banco.lote_envio(lote_id)
        if lote is None:
            return None
        destinos = self.ctx.banco.destinos_envio(lote_id)
        return {"id": lote["id"], "origem": lote["origem"], "estado": lote["estado"], "motivo": lote["motivo"],
                "n": lote["n"], "idempotencia": lote["idempotencia"], "criado_em": lote["criado_em"],
                "atualizado_em": lote["atualizado_em"], "contagem": self._contagem(destinos),
                "texto_em_memoria": lote_id in self._textos,
                "destinatarios": [{"pos": d["pos"], "para": d["para"], "nome": d["nome"], "conhecido": bool(d["conhecido"]),
                                   "estado": d["estado"], "motivo": d["motivo"], "msg_id": d["msg_id"]} for d in destinos]}

    def listar(self, limite: int = 50) -> list[dict]:
        saida = []
        for lote in self.ctx.banco.lotes_envio(limite):
            destinos = self.ctx.banco.destinos_envio(lote["id"])
            saida.append({"id": lote["id"], "origem": lote["origem"], "estado": lote["estado"], "motivo": lote["motivo"],
                          "n": lote["n"], "criado_em": lote["criado_em"], "atualizado_em": lote["atualizado_em"],
                          "contagem": self._contagem(destinos), "texto_em_memoria": lote["id"] in self._textos,
                          "nomes": [self._rotulo(d) for d in destinos[:3]]})
        return saida

    @staticmethod
    def _rotulo(d: dict) -> str:
        if d["nome"]:
            return d["nome"]
        return "grupo" if d["para"].endswith("@g.us") else wa.numero_formatado(d["para"])

    def resumo(self) -> dict:
        banco = self.ctx.banco
        reserv = banco.reservados_envio()
        hoje = banco.envios_api_hoje()
        with self._passo:
            prox = max(0, int(self._proximo_em - self.relogio()))
        return {"na_fila": reserv["conhecidos"] + reserv["novos"], "hoje": hoje,
                "tetos": {k: v for k, v in self.limites.items()}, "proximo_em_s": prox,
                "conexao": self._estado_conexao() or "desconhecida"}

    def esperar(self, lote_id: int, timeout: float) -> dict | None:
        """Bloqueia até o lote sair dos estados ativos ou estourar o tempo; devolve estado(lote)."""
        fim = time.monotonic() + max(0.0, timeout)
        while True:
            est = self.estado(lote_id)
            if est is None or est["estado"] not in ATIVOS:
                return est
            resta = fim - time.monotonic()
            if resta <= 0:
                return est
            with self._cond:
                self._cond.wait(min(resta, 1.0))

    # ------------------------------------------------------------------ parar / retomar

    def parar(self, lote_id: int) -> dict | None:
        lote = self.ctx.banco.lote_envio(lote_id)
        if lote is None:
            return None
        if lote["estado"] in ATIVOS or lote["estado"] == "pausado":
            self.ctx.banco.mudar_destinos_envio(lote_id, "fila", "cancelado", "parado a pedido")
            self._lote(lote_id, "parado", "parado a pedido; o que já saiu, saiu")
        return self.estado(lote_id)

    def retomar(self, lote_id: int) -> dict | None:
        lote = self.ctx.banco.lote_envio(lote_id)
        if lote is None:
            return None
        if lote["estado"] in ("pausado", "parado", "interrompido"):
            if lote_id not in self._textos:
                raise PedidoInvalido(409, MOTIVO_REINICIO)
            self._retomar(lote_id)
        return self.estado(lote_id)

    def _retomar(self, lote_id: int) -> None:
        self.ctx.banco.mudar_destinos_envio(lote_id, "cancelado", "fila", "")
        if any(d["estado"] == "fila" for d in self.ctx.banco.destinos_envio(lote_id)):
            self._lote(lote_id, "na_fila", "retomado")
        else:
            self._lote(lote_id, "concluido", self._resumo_destinos(self.ctx.banco.destinos_envio(lote_id)))
        self._acordar()

    def _lote(self, lote_id: int, estado: str, motivo: str) -> None:
        self.ctx.banco.atualizar_lote_envio(lote_id, estado, motivo)
        self._acordar()

    # ------------------------------------------------------------------ um passo da fila

    def processar_uma(self) -> float:
        """Manda (ou pula) UM destinatário e devolve quantos segundos a thread deve esperar antes do próximo passo
        (0 = pode seguir). Chamado pela thread; os testes chamam direto."""
        with self._passo:
            return self._passo_interno()

    def _passo_interno(self) -> float:
        banco = self.ctx.banco
        d = banco.proximo_destino_envio()
        if d is None:
            return ESPERA_OCIOSA_S
        lote_id = d["lote_id"]
        if d["id"] in self._saiu:              # já saiu: só falta gravar o estado; nunca manda de novo
            self._fechar(d, *self._saiu[d["id"]])
            return 0.0
        texto = self._textos.get(lote_id)
        if texto is None:
            self._lote(lote_id, "interrompido", MOTIVO_REINICIO)
            return 0.0
        cx = self._estado_conexao()
        if cx is not None and cx != "open":
            if d["estado_lote"] != "aguardando_conexao":
                self._lote(lote_id, "aguardando_conexao", f"conexão com o WhatsApp está '{cx}'; a fila espera")
            return ESPERA_CONEXAO_S
        agora = self.relogio()
        if agora < self._proximo_em:
            return min(self._proximo_em - agora, 60.0)
        cat = "conhecidos" if d["conhecido"] else "novos"
        teto = self.limites[f"teto_dia_{cat}"]
        if banco.envios_api_hoje()[cat] >= teto:
            self._lote(lote_id, "pausado", f"teto do dia de {cat} ({teto}) atingido; retome amanhã")
            return 0.0
        if d["estado_lote"] != "enviando":
            self._lote(lote_id, "enviando", "")
        try:
            bloq = self._bloqueados()
        except (VigiaErro, ValueError) as e:
            log.warning("bloqueios indisponíveis na fila: %s", causa(e))
            self._lote(lote_id, "pausado", "banco da Evolution indisponível; nada saiu para este destinatário")
            return 0.0
        if d["para"] in bloq:
            banco.atualizar_destino_envio(d["id"], "pulado", "bloqueado")
            self._fechar_se_acabou(lote_id)
            return 0.0
        numero = d["para"] if d["para"].endswith("@g.us") else wa.digitos(d["para"])
        if lote_id not in self._sem_conferir and not d["conhecido"]:
            try:
                r = self.ctx.evo.numero_no_whatsapp(numero)
            except VigiaErro as e:
                log.warning("conferência de número falhou: %s", causa(e))
                r = None
            if r is None:
                self._lote(lote_id, "pausado", "a Evolution não respondeu à conferência do número; nada saiu")
                return 0.0
            if not r["exists"]:
                banco.atualizar_destino_envio(d["id"], "pulado", "sem WhatsApp")
                self._fechar_se_acabou(lote_id)
                return 0.0
        final = texto_final(texto, d["nome"])
        h, por = _sha(final), f"api:{d['origem']}"
        try:
            mid = self.ctx.evo.enviar_texto(numero, final)
        except EnvioIncerto:
            log.warning("envio incerto lote=%d pos=%d", lote_id, d["pos"])
            self._cadencia(d)
            self._fechar(d, "incerto", "a mensagem pode ter saído; confira no celular antes de repetir", None, h,
                         por + ":incerto")
            return 0.0
        except VigiaErro as e:
            log.warning("envio da fila falhou: %s", causa(e))
            from app.erros import EvolutionIndisponivel
            self._lote(lote_id, "pausado", ("a ponte com o WhatsApp não respondeu" if isinstance(e, EvolutionIndisponivel)
                                            else "o WhatsApp recusou este envio") + "; nada saiu para este destinatário")
            return 0.0
        else:
            log.info("envio lote=%d pos=%d id=%s", lote_id, d["pos"], mid)
            self._cadencia(d)
            self._fechar(d, "enviado", "", mid, h, por)
        return 0.0

    def _cadencia(self, d: dict) -> None:
        """Marca o próximo envio permitido. Vem logo depois do envio, antes de gravar: se a gravação falhar, a
        cadência entre destinatários continua valendo."""
        lo, hi = self.limites["intervalo_s" if d["conhecido"] else "intervalo_novos_s"]
        self._proximo_em = self.relogio() + float(self.aleatorio(lo, hi))

    def _fechar(self, d: dict, estado: str, motivo: str, msg_id: str | None, h: str, por: str) -> None:
        """Grava o estado de um destinatário que já saiu (ou ficou incerto). A guarda entra ANTES da gravação: se
        gravar falhar, a exceção sobe como antes (a thread loga e espera), mas o próximo passo só tenta gravar de novo.
        C3: o estado do destino vem ANTES do registro; se o registro falhar, o destino já não está na fila e não é
        mandado de novo no próximo passo."""
        self._saiu[d["id"]] = (estado, motivo, msg_id, h, por)
        self.ctx.banco.atualizar_destino_envio(d["id"], estado, motivo, msg_id=msg_id)
        self._saiu.pop(d["id"], None)
        self._registrar(d["para"], h, por)
        self._fechar_se_acabou(d["lote_id"])

    def _registrar(self, para: str, h: str, por: str) -> None:
        try:
            self.ctx.banco.registrar_envio(para=para, hash_texto=h, por=por)
        except Exception as e:  # noqa: BLE001
            log.error("envio da fila saiu mas o registro falhou (por=%s): %s", por, causa(e))

    def _fechar_se_acabou(self, lote_id: int) -> None:
        destinos = self.ctx.banco.destinos_envio(lote_id)
        if any(d["estado"] == "fila" for d in destinos):
            self._acordar()
            return
        self._sem_conferir.discard(lote_id)
        self._lote(lote_id, "concluido", self._resumo_destinos(destinos))
