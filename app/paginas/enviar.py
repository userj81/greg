"""Página /enviar: formulário (GET) e envio de texto (POST) que só parte com o token interativo em X-Vigia-Envio.
Desde a D-44 o POST vira um lote de 1 na fila da API (app/fila_envio): herda teto, cadência e registro por origem
("api:painel"), e espera o resultado até ESPERA_S para manter a resposta síncrona de antes.
Quem chama: app/server.py (via ROTAS). Envio é sempre solicitação do dono, nunca automática (R-9).
O que não faz: não guarda o texto (só hash em banco.registrar_envio), não loga texto, não reenvia em caso incerto,
não confere o número na Evolution (quem chama já sabe o número; a API nova confere por padrão).
"""
import logging
import re

from app.fila_envio import PedidoInvalido
from app.http import json_resposta
from app.paginas import _comum as c
from app.paginas import envios

log = logging.getLogger("vigia.painel.enviar")
PADRAO_PARA = re.compile(r"55\d{11}")  # 55 DD NNNNNNNNN
LIMITE_TEXTO = 4000
ESPERA_S = 25.0


def _token_ok(req) -> bool:
    return envios.token_ok(req)


def enviar(req):
    if (e := c.metodo_errado(req, "GET", "HEAD", "POST")):
        return e
    if req.metodo.upper() != "POST":
        configurado = bool(getattr(getattr(req.ctx, "tokens", None), "envio", "") or "")
        miolo = c.render("enviar.html", limite=LIMITE_TEXTO, desabilitado="" if configurado else "disabled",
                         aviso_config="" if configurado else
                         c.erro_inline("token de envio não configurado: o formulário fica desabilitado."))
        painel = c.painel(miolo, toolbar_html=c.toolbar(c.titulo("Enviar mensagem"), c.espaco()), aria="Enviar mensagem",
                          status_html=c.rodape("Pelo seu número pessoal. O token não fica guardado no navegador"))
        return c.pagina("Enviar mensagem", painel, "/enviar", ctx=req.ctx)
    if not _token_ok(req):
        return json_resposta({"enviadas": 0, "mensagem": "token de envio ausente ou errado"}, 401)
    if not c.origem_ok(req):
        return c.proibido()
    corpo = req.corpo if isinstance(req.corpo, dict) else {}
    para, texto = corpo.get("para"), corpo.get("texto")
    if not isinstance(para, str) or not PADRAO_PARA.fullmatch(para):
        return json_resposta({"enviadas": 0, "mensagem": "número fora do padrão 55DDNNNNNNNNN"}, 400)
    if not isinstance(texto, str) or not texto.strip() or len(texto) > LIMITE_TEXTO:
        return json_resposta({"enviadas": 0, "mensagem": f"texto vazio ou acima de {LIMITE_TEXTO} caracteres"}, 400)
    fila = envios.fila_de(req.ctx)
    try:
        lote = fila.criar("painel", texto, [{"para": para}], conferir=False)
    except PedidoInvalido as e:
        return json_resposta({"enviadas": 0, "mensagem": e.mensagem, **e.extra}, e.status)
    est = fila.esperar(lote["id"], timeout=ESPERA_S) or lote
    d = est["destinatarios"][0]
    if d["estado"] == "enviado":
        return json_resposta({"enviadas": 1, "id": d["msg_id"], "lote": est["id"]})
    if d["estado"] == "incerto":
        log.warning("envio incerto")
        return json_resposta({"enviadas": 0, "incerto": True, "lote": est["id"],
                              "mensagem": "A mensagem pode ter sido enviada. Confira no celular antes de tentar de novo."}, 202)
    if d["estado"] == "pulado":
        return json_resposta({"enviadas": 0, "lote": est["id"], "mensagem": f"não enviado: {d['motivo']}"}, 409)
    if est["estado"] == "pausado":
        fila.parar(est["id"])   # lote de 1: nada a retomar; o motivo fica no registro do lote
        log.warning("envio falhou: %s", est["motivo"])
        return json_resposta({"enviadas": 0, "lote": est["id"],
                              "mensagem": "Evolution recusou ou está indisponível; nada foi registrado."}, 502)
    return json_resposta({"enviadas": 0, "lote": est["id"], "estado": est["estado"],
                          "mensagem": f"na fila da API; acompanhe em /envios/{est['id']}"}, 202)


ROTAS = {"/enviar": enviar}
