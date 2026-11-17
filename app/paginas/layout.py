"""POST /layout (pedido 48, D-59): guarda o que o dono arrastou nas divisórias do painel.
Quem chama: o JS da casca (app/templates/_base.html), com fetch e X-Vigia-Auto, ao soltar uma divisória, ao recolher ou
mostrar a lateral e ao dar duplo clique para voltar ao padrão.
O que não faz: não desenha tela (não há GET), não guarda nada fora de app/layout.py, não aceita POST de outra origem.
"""
import json

from app import layout
from app.http import Resposta
from app.paginas import _comum as c


def layout_post(req):
    if (e := c.metodo_errado(req, "POST")):
        return e
    if not c.origem_ok(req):
        return c.proibido()
    d = dict(req.corpo or {})
    mudanca = {k: d[k] for k in ("lateral", "recolhida", "pagina", "lista") if k in d}
    p = layout.mudar(mudanca)
    return Resposta(200, json.dumps({"ok": True, "lateral": p["lateral"], "recolhida": p["recolhida"]}),
                    "application/json; charset=utf-8", [("Cache-Control", "no-store")])


ROTAS = {"/layout": layout_post}
