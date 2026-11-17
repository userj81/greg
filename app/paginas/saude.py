"""Página /saude: JSON de estado (conexão, cursor, lotes, QRs do dia, lápides, pausa), sem cookie.
Quem chama: app/server.py (via ROTAS), monitores locais e o painel.
O que não faz: não chama Evolution nem Frentes, não devolve texto de conversa nem hash/segredo, só ids e contagens.
"""
import json
import time

from app.cursor import Cursor
from app.erros import VigiaErro
from app.http import json_resposta
from app.paginas import _comum as c


def _cursor(ctx) -> dict:
    cur = getattr(ctx.conexao, "cursor", None)
    try:
        if cur is not None:
            cur = Cursor.carregar(cur.caminho, reparar=False)  # relê; /saude não tem login: leitura pura (B8)
    except VigiaErro as e:
        return {"erro": type(e).__name__, "estado": "corrompido"}
    if cur is None:
        return {"ultimo_seq": None, "pareado_em": None, "desde": None}
    return {"ultimo_seq": cur.ultimo_seq, "pareado_em": cur.pareado_em, "desde": cur.desde}


def _monitor(ctx) -> dict:
    try:
        d = json.loads((c.dados_dir(ctx) / "conexao.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: d.get(k) for k in ("ultimo_estado", "atualizado_em")} if isinstance(d, dict) else {}


def saude_get(req):
    if (e := c.metodo_errado(req, "GET", "HEAD")):
        return e
    ctx = req.ctx
    cx = ctx.conexao
    lotes = [{k: l.get(k) for k in ("nome", "conversas", "novas", "itens", "atencao", "modelo", "criado_em")}
             for l in ctx.banco.lotes(5)]
    lapides = [{"semana": l["semana"], "n_msgs": l["n_msgs"], "apagado_em": l["apagado_em"]}
               for l in ctx.banco.lapides()[-8:]]
    return json_resposta({
        "gerado_em": int(time.time()),
        "conexao": {"estado": cx.estado, "monitor": _monitor(ctx)},
        "pausado": (c.dados_dir(ctx) / "PAUSADO").exists(),
        "qrs_hoje": ctx.banco.qrs_hoje(), "teto_qr_dia": getattr(cx, "teto_dia", None),
        "cursor": _cursor(ctx),
        "lotes": lotes, "ultimo_lote": lotes[0] if lotes else None,
        "lapides": lapides,
    })


ROTAS = {"/saude": saude_get}
