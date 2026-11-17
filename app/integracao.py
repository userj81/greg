"""Integração com o Claude Code (pedido 71, D-71): token próprio, chaves por área, pedidos que esperam o aval do dono
e registro de chamadas.
Quem chama: app/mcp.py (confere token e chave, registra), app/mcp_ferramentas.py (tetos e pedidos de aval) e
app/paginas/mcp.py (seção "Claude Code" dos Ajustes: liga chave, gera e revoga token, decide pedido).
O que não faz: não guarda o token (só o SHA-256 dele, em dados/integracao.json 0600), não grava texto de mensagem em
disco (o pedido que espera aval vive só na memória do painel e vence em 30 min; o registro leva ferramenta, hora e
resultado), e nenhuma função daqui é exposta como ferramenta: o Claude não liga chave nem gera token.
"""
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

# Chave -> (rótulo, o que libera). Todas nascem desligadas: a integração só existe quando o dono liga.
CHAVES = {
    "enviar": ("Enviar", "Mandar mensagem e arquivo, responder, reagir, encaminhar. Só com o envio liberado por você"),
    "ler": ("Ler conversas", "Ver conversas, mensagens, transcrições, fotos e documentos"),
    "demandas": ("Demandas e frentes", "Ver as demandas, concluir, recusar, mover, anotar na frente, ler agora"),
    "contatos": ("Contatos", "Dar nome, ligar contato a uma frente, deixar conversa fora, exportar contatos"),
}
VALIDADE_PEDIDO_S = 30 * 60
LIMITE_LOG = 2000
_TRAVA = threading.Lock()


def _dados(ctx) -> Path:
    return Path((getattr(ctx, "config", None) or {}).get("dados_dir", "dados"))


def _arquivo(ctx) -> Path:
    return _dados(ctx) / "integracao.json"


def _arquivo_log(ctx) -> Path:
    return _dados(ctx) / "integracao-log.jsonl"


def _padrao() -> dict:
    return {"token_sha256": "", "criado_em": 0, "chaves": {k: False for k in CHAVES}, "confirmar_novos": True}


def ler(ctx) -> dict:
    """Estado validado; arquivo ausente ou ilegível = tudo desligado e sem token."""
    p = _padrao()
    try:
        bruto = json.loads(_arquivo(ctx).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return p
    if not isinstance(bruto, dict):
        return p
    h = bruto.get("token_sha256")
    if isinstance(h, str) and len(h) == 64 and all(ch in "0123456789abcdef" for ch in h):
        p["token_sha256"] = h
    if isinstance(bruto.get("criado_em"), int):
        p["criado_em"] = bruto["criado_em"]
    chaves = bruto.get("chaves") if isinstance(bruto.get("chaves"), dict) else {}
    for k in CHAVES:
        p["chaves"][k] = chaves.get(k) is True
    p["confirmar_novos"] = bruto.get("confirmar_novos") is not False
    return p


def _gravar(ctx, estado: dict) -> None:
    arq = _arquivo(ctx)
    arq.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = arq.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, arq)


def gerar_token(ctx) -> str:
    """Token novo (o anterior deixa de valer). Devolve o valor UMA vez; em disco fica só o SHA-256."""
    token = secrets.token_hex(24)
    with _TRAVA:
        e = ler(ctx)
        e["token_sha256"] = hashlib.sha256(token.encode("utf-8")).hexdigest()
        e["criado_em"] = int(time.time())
        _gravar(ctx, e)
    return token


def revogar(ctx) -> None:
    """Apaga o token e desliga todas as chaves: a integração volta ao estado de fábrica."""
    with _TRAVA:
        _gravar(ctx, _padrao())


def definir(ctx, nome: str, ligado: bool) -> bool:
    """Liga ou desliga uma chave (ou `confirmar_novos`). False se o nome não existe."""
    with _TRAVA:
        e = ler(ctx)
        if nome == "confirmar_novos":
            e["confirmar_novos"] = bool(ligado)
        elif nome in CHAVES:
            e["chaves"][nome] = bool(ligado)
        else:
            return False
        _gravar(ctx, e)
    return True


def token_ok(ctx, recebido: str) -> bool:
    esperado = ler(ctx)["token_sha256"]
    if not esperado or not isinstance(recebido, str) or not recebido:
        return False
    return hmac.compare_digest(hashlib.sha256(recebido.encode("utf-8")).hexdigest(), esperado)


def token_do_cabecalho(valor) -> str:
    """'Bearer <token>' -> token; qualquer outra coisa -> ''."""
    partes = str(valor or "").strip().split(None, 1)
    return partes[1].strip() if len(partes) == 2 and partes[0].lower() == "bearer" else ""


def chave_ligada(ctx, nome: str) -> bool:
    return ler(ctx)["chaves"].get(nome, False)


# ------------------------------------------------------------------ registro (sem texto de mensagem)

def registrar(ctx, ferramenta: str, chave: str, resultado: str) -> None:
    """Uma linha por chamada: hora, ferramenta, chave e resultado (ok | erro | recusado | aval). Nunca argumento."""
    linha = json.dumps({"ts": int(time.time()), "f": str(ferramenta)[:60], "c": str(chave)[:20], "r": str(resultado)[:20]})
    arq = _arquivo_log(ctx)
    with _TRAVA:
        try:
            arq.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(arq, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(linha + "\n")
            if arq.stat().st_size > LIMITE_LOG * 120:     # corta pela metade quando passa do tamanho
                linhas = arq.read_text(encoding="utf-8").splitlines()[-LIMITE_LOG // 2:]
                tmp = arq.with_suffix(".tmp")
                tmp.write_text("\n".join(linhas) + "\n", encoding="utf-8")
                os.chmod(tmp, 0o600)
                os.replace(tmp, arq)
        except OSError:
            pass


def registro(ctx, limite: int = 20) -> list[dict]:
    """Últimas chamadas, da mais nova para a mais antiga."""
    try:
        linhas = _arquivo_log(ctx).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    saida = []
    for ln in reversed(linhas[-max(1, int(limite)) * 3:]):
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if isinstance(d, dict) and isinstance(d.get("ts"), int):
            saida.append({"ts": d["ts"], "ferramenta": str(d.get("f", "")), "chave": str(d.get("c", "")),
                          "resultado": str(d.get("r", ""))})
        if len(saida) >= limite:
            break
    return saida


def contar_hoje(ctx, ferramentas) -> int:
    """Quantas chamadas com resultado ok hoje (dia local) entre as ferramentas dadas: é o teto dos envios diretos."""
    inicio = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    nomes = set(ferramentas)
    n = 0
    try:
        linhas = _arquivo_log(ctx).read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    for ln in linhas:
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("r") == "ok" and d.get("f") in nomes and int(d.get("ts") or 0) >= inicio:
            n += 1
    return n


# ------------------------------------------------------------------ pedidos que esperam o aval do dono (só memória)

class Pedidos:
    """Fila de pedidos do Claude que só o dono libera, na tela de Ajustes. Vive na memória: reiniciar o painel descarta."""

    def __init__(self, relogio=time.time):
        self.relogio = relogio
        self._trava = threading.Lock()
        self._itens: dict[int, dict] = {}
        self._seq = 0

    def criar(self, ferramenta: str, resumo: str, executar) -> int:
        """`resumo` é o que a tela mostra ao dono; `executar()` roda se ele permitir e devolve o resultado (dict)."""
        with self._trava:
            self._limpar()
            self._seq += 1
            self._itens[self._seq] = {"id": self._seq, "ferramenta": ferramenta, "resumo": str(resumo)[:400],
                                      "executar": executar, "criado": self.relogio(), "estado": "aguardando",
                                      "resultado": None}
            return self._seq

    def _limpar(self) -> None:
        agora = self.relogio()
        for pid, it in list(self._itens.items()):
            if it["estado"] == "aguardando" and agora - it["criado"] > VALIDADE_PEDIDO_S:
                it["estado"], it["executar"] = "vencido", None
            if agora - it["criado"] > 6 * 3600:
                del self._itens[pid]

    def pendentes(self) -> list[dict]:
        with self._trava:
            self._limpar()
            return [{k: it[k] for k in ("id", "ferramenta", "resumo", "criado")}
                    for it in self._itens.values() if it["estado"] == "aguardando"]

    def estado(self, pid: int) -> dict | None:
        with self._trava:
            self._limpar()
            it = self._itens.get(int(pid))
            return None if it is None else {"id": it["id"], "ferramenta": it["ferramenta"], "estado": it["estado"],
                                            "resultado": it["resultado"]}

    def decidir(self, pid: int, permitir: bool) -> dict | None:
        """Permite (roda agora) ou recusa. None se o pedido não existe ou já foi decidido."""
        with self._trava:
            self._limpar()
            it = self._itens.get(int(pid))
            if it is None or it["estado"] != "aguardando":
                return None
            executar, it["executar"] = it["executar"], None
            it["estado"] = "executando" if permitir else "recusado"
        if permitir:
            try:
                resultado = executar()
                estado = "feito"
            except Exception as e:  # o motivo fica com a classe do erro; nunca texto de mensagem
                resultado, estado = {"erro": f"falhou ({type(e).__name__})"}, "falhou"
            with self._trava:
                it["estado"], it["resultado"] = estado, resultado
        return {"id": it["id"], "estado": it["estado"], "resultado": it["resultado"]}


def pedidos(ctx) -> Pedidos:
    """Um por contexto (criado no primeiro uso)."""
    with _TRAVA:
        p = getattr(ctx, "pedidos_claude", None)
        if p is None:
            p = Pedidos()
            ctx.pedidos_claude = p
    return p
