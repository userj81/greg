"""API local de envio (pedido 10, R-15, D-44) e a tela /envios que só acompanha a fila.
Rotas: POST /envios (token X-Vigia-Envio; cria um lote) · GET /envios (HTML, ou JSON com Accept: application/json) ·
GET /envios/{id} (estado) · POST /envios/{id}/parar · POST /envios/{id}/retomar (token, ou trava do Touch ID aberta).
Quem chama: app/server.py (via ROTAS; nas rotas /envios* o token de envio vale como sessão), bin/vigia-enviar, as
ferramentas do dono (Claude Code, automações) e app/paginas/enviar.py (reusa fila_de).
O que não faz: não envia nada fora da fila (app/fila_envio), não grava nem loga texto, não aceita pedido sem token.
"""
import hmac
import logging
import threading

import json

from app import agendados, alertas, fila_envio, whatsapp as wa
from app.erros import VigiaErro, causa
from app.http import json_resposta
from app.paginas import _comum as c

log = logging.getLogger("vigia.painel.envios")
_TRAVA = threading.Lock()
ESPERA_ENVIAR_S = 25.0


def fila_de(ctx) -> fila_envio.Fila:
    """Uma Fila por contexto (o servidor cria a sua em montar_ctx; testes e contextos antigos criam aqui)."""
    with _TRAVA:
        f = getattr(ctx, "fila", None)
        if f is None:
            f = fila_envio.Fila(ctx, limites=fila_envio.limites_de_config(getattr(ctx, "config", None)))
            f.iniciar()
            ctx.fila = f
    return f


def token_ok(req) -> bool:
    esperado = getattr(getattr(req.ctx, "tokens", None), "envio", "") or ""
    cab = req.cabecalhos or {}
    recebido = cab.get("x-vigia-envio") or cab.get("X-Vigia-Envio") or ""
    if not esperado or not recebido:
        return False
    return hmac.compare_digest(str(recebido).encode("utf-8"), str(esperado).encode("utf-8"))


def _quer_json(req) -> bool:
    cab = req.cabecalhos or {}
    return ("json" in str(cab.get("accept") or cab.get("Accept") or "") or c.query(req, "formato") == "json"
            or bool(cab.get("x-vigia-envio") or cab.get("X-Vigia-Envio")))


def _erro(e: fila_envio.PedidoInvalido):
    return json_resposta({"erro": e.mensagem, **e.extra}, e.status)


# ------------------------------------------------------------------ API

def envios(req):
    if (e := c.metodo_errado(req, "GET", "HEAD", "POST")):
        return e
    if req.metodo.upper() == "POST":
        return criar_post(req)
    fila = fila_de(req.ctx)
    if _quer_json(req):
        return json_resposta({"resumo": fila.resumo(), "lotes": fila.listar(50)})
    return c.pagina("Envios", html_pagina(req.ctx, fila), "/envios", ctx=req.ctx, toast=_toast(req))


def criar_post(req):
    if not token_ok(req):
        return json_resposta({"erro": "token de envio ausente ou errado"}, 401)
    if not c.origem_ok(req):
        return c.proibido()
    corpo = req.corpo if isinstance(req.corpo, dict) else {}
    conferir = corpo.get("conferir", True)
    try:
        lote = fila_de(req.ctx).criar(corpo.get("origem"), corpo.get("texto"), corpo.get("destinatarios"),
                                      corpo.get("idempotencia"), conferir=(conferir is not False))
    except fila_envio.PedidoInvalido as e:
        return _erro(e)
    return json_resposta(lote, 200 if lote.get("repetido") else 202)


def _lote_id(req, padrao: str) -> int | None:
    return c.id_da_rota(req, padrao)


def estado_get(req):
    if (e := c.metodo_errado(req, "GET", "HEAD")):
        return e
    lid = _lote_id(req, r"/envios/(\d+)")
    est = fila_de(req.ctx).estado(lid) if lid is not None else None
    if est is None:
        return json_resposta({"erro": "lote inexistente"}, 404)
    return json_resposta(est)


def _responder_acao(req, est, aviso: str):
    if _quer_json(req):
        return json_resposta(est)
    return c.redirecionar(f"/envios?aviso={aviso}&lote={int(est['id'])}")


def parar_post(req):
    if (e := c.metodo_errado(req, "POST")):
        return e
    if not c.origem_ok(req):
        return c.proibido()
    lid = _lote_id(req, r"/envios/(\d+)/parar")
    est = fila_de(req.ctx).parar(lid) if lid is not None else None
    if est is None:
        return json_resposta({"erro": "lote inexistente"}, 404)
    return _responder_acao(req, est, "parado")


def retomar_post(req):
    """Retomar é envio (R-9): exige o token da ferramenta ou a trava do Touch ID aberta no app."""
    if (e := c.metodo_errado(req, "POST")):
        return e
    if not c.origem_ok(req):
        return c.proibido()
    if not token_ok(req):
        from app.paginas import conversas
        if not conversas.servicos(req.ctx).liberacao.liberada():
            return json_resposta({"erro": "retomar exige o token de envio ou o Touch ID", "liberar": True}, 423)
    lid = _lote_id(req, r"/envios/(\d+)/retomar")
    try:
        est = fila_de(req.ctx).retomar(lid) if lid is not None else None
    except fila_envio.PedidoInvalido as e:
        return _erro(e)
    if est is None:
        return json_resposta({"erro": "lote inexistente"}, 404)
    return _responder_acao(req, est, "retomado")


# ------------------------------------------------------------------ tela (estilo Papel, pedido 39)

# estado do lote → (rótulo, tipo): "atrasado" = pílula vermelha, "alerta" = pílula âmbar, "ok" = verde, "" = texto cinza
_ESTADO_ROTULO = {"na_fila": ("na fila", ""), "enviando": ("enviando", ""), "aguardando_conexao": ("aguardando conexão", "alerta"),
                  "pausado": ("pausado", "atrasado"), "parado": ("parado", ""), "interrompido": ("interrompido", "alerta"),
                  "concluido": ("concluído", "ok")}

_CSS = """<style>
.env-lotes td code { font: .9231rem ui-monospace, Menlo, monospace; }
.env-lotes td .acoes { gap: 6px; }
.env-lotes .pilula { vertical-align: middle; }
.env-lotes .ok { color: var(--ok); }
.env-pre { margin-top: 2px; }
.env-nota { margin-top: 8px; }
</style>"""

_JS = """<script>
(function () {
  /* a tela só acompanha a fila: a cada 15 s busca a página de novo e troca o que mudou (sem piscar a tela inteira) */
  function atualizar() {
    if (document.visibilityState !== 'visible') { return; }
    fetch(location.pathname, {credentials: 'same-origin'}).then(function (r) { return r.ok ? r.text() : ''; }).then(function (h) {
      window.vigiaOffline(false);
      if (!h) { return; }
      var doc = new DOMParser().parseFromString(h, 'text/html');
      ['env-kv', 'env-agendados', 'env-lotes', 'env-status'].forEach(function (id) {
        var n = doc.getElementById(id), a = document.getElementById(id);
        if (n && a && n.innerHTML !== a.innerHTML) { a.innerHTML = n.innerHTML; }
      });
    }).catch(function () { window.vigiaOffline(true); });
  }
  document.addEventListener('click', function (ev) {
    var b = ev.target.closest && ev.target.closest('[data-liberar-envio]'); if (!b) { return; }
    function pronto(ok, motivo) {
      if (ok) { window.vigiaToast('Envio liberado. Agora dá para retomar.'); atualizar(); return; }
      window.vigiaToast({cancelado: 'Envio não liberado: a confirmação do Touch ID foi cancelada.',
                         sem_touch: 'Envio não liberado: este Mac não tem Touch ID nem senha para confirmar.',
                         sem_token: 'Envio não liberado: o token de envio não está configurado neste Mac.',
                         recusado: 'Envio não liberado: o painel recusou o token de envio.',
                         rede: 'Envio não liberado: o painel não respondeu.'}[motivo] || 'Envio não liberado.');
    }
    var h = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.vigia;
    if (h) {
      window.vigiaEnvioLiberado = function (ok, motivo) { window.vigiaEnvioLiberado = null; pronto(ok, motivo); };
      h.postMessage({acao: 'liberarEnvio'});
      return;
    }
    var tk = window.prompt('Token de envio:');
    if (!tk) { return; }
    fetch('/envio/liberar', {method: 'POST', credentials: 'same-origin',
                             headers: {'Content-Type': 'application/json', 'X-Vigia-Envio': tk, 'Accept': 'application/json'}, body: '{}'})
      .then(function (r) { pronto(r.status === 200, 'recusado'); }).catch(function () { pronto(false, 'rede'); });
  });
  document.addEventListener('click', function (ev) {
    var b = ev.target.closest && ev.target.closest('[data-ag-cancelar]'); if (!b) { return; }
    b.disabled = true;
    window.vigiaPost('/conversas/agendados/cancelar', {id: parseInt(b.getAttribute('data-ag-cancelar'), 10)}).then(function (j) {
      window.vigiaToast(j && j.cancelado ? 'Envio cancelado.' : ((j && (j.mensagem || j.erro)) || 'Não consegui cancelar.'));
      atualizar();
    }).catch(function () { b.disabled = false; window.vigiaToast('O painel não respondeu; o envio continua agendado.'); });
  });
  setInterval(atualizar, 15000);
})();
</script>"""

_EXEMPLO = ("echo '{\"origem\":\"claude-code\",\n \"texto\":\"Oi {nome}, a proposta está pronta.\",\n"
            " \"destinatarios\":[{\"para\":\"5592900001111\"}]}' \\\n| ~/vigia-whatsapp/bin/vigia-enviar --esperar")


def _estado_html(estado: str) -> str:
    rot, tipo = _ESTADO_ROTULO.get(estado, (estado, ""))
    if tipo == "atrasado":
        return c.pilula(rot, "atrasado")
    if tipo == "alerta":
        return c.pilula(rot)
    if tipo == "ok":
        return f'<span class="ok">{c.esc(rot)}</span>'
    return f'<span class="meta">{c.esc(rot)}</span>'


def _linha(l: dict, pode_retomar: bool) -> str:
    cont = l["contagem"]
    partes = []
    if cont["enviados"]:
        partes.append(f'{cont["enviados"]} enviada' + ("s" if cont["enviados"] != 1 else ""))
    if cont["incertos"]:
        partes.append(f'{cont["incertos"]} incerta' + ("s" if cont["incertos"] != 1 else ""))
    if cont["pulados"]:
        partes.append(f'{cont["pulados"]} pulada' + ("s" if cont["pulados"] != 1 else ""))
    if cont["fila"]:
        partes.append(f'{cont["fila"]} na fila')
    if cont["cancelados"]:
        partes.append(f'{cont["cancelados"]} cancelada' + ("s" if cont["cancelados"] != 1 else ""))
    quem = ", ".join(c.esc(n) for n in l["nomes"]) + (f" e mais {l['n'] - len(l['nomes'])}" if l["n"] > len(l["nomes"]) else "")
    motivo = f'<div class="meta">{c.esc(l["motivo"])}</div>' if l["motivo"] and l["estado"] != "concluido" else ""
    acoes = []
    if l["estado"] in ("na_fila", "enviando", "aguardando_conexao", "pausado"):
        acoes.append(f'<form method="post" action="/envios/{int(l["id"])}/parar"><button type="submit" class="btn sm">Parar</button></form>')
    if l["estado"] in ("pausado", "parado", "interrompido") and cont["fila"] + cont["cancelados"]:
        if l["texto_em_memoria"] and pode_retomar:
            acoes.append(f'<form method="post" action="/envios/{int(l["id"])}/retomar"><button type="submit" class="btn sm primario">Retomar</button></form>')
        elif l["texto_em_memoria"]:
            # auditoria E1 (lote 5): a tela dizia "pede Touch ID" e não oferecia o botão
            acoes.append('<button type="button" class="btn sm" data-liberar-envio>Liberar o envio para retomar</button>')
        else:
            acoes.append('<span class="meta">Texto saiu da memória: a ferramenta repete o pedido</span>')
    return (f'<tr><td class="meta">{c.esc(l["criado_em"][5:16])}</td><td><code>{c.esc(l["origem"])}</code></td>'
            f'<td>{quem}<div class="meta">{", ".join(partes)}</div></td>'
            f'<td>{_estado_html(l["estado"])}{motivo}</td><td><div class="acoes">{"".join(acoes)}</div></td></tr>')


_AG_ESTADO = {"agendado": ("Agendado", ""), "enviado": ("Enviado", "ok"), "incerto": ("Pode ter saído", "alerta"),
              "vencido": ("Não saiu", "atrasado"), "falhou": ("Não saiu", "atrasado"), "cancelado": ("Cancelado", "")}


def _nomes_agendados(ctx, itens: list[dict]) -> dict:
    """{jid: nome para a tela}: o apelido, o assunto do grupo guardado (D-67) ou o número formatado."""
    try:
        apelidos = ctx.banco.apelidos()
    except Exception as e:
        log.warning("apelidos indisponíveis em Envios: %s", causa(e))
        apelidos = {}
    try:
        grupos = json.loads((c.dados_dir(ctx) / "grupos.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        grupos = {}
    nomes = {}
    for it in itens:
        j = it["para"]
        if j.endswith("@g.us"):
            g = grupos.get(j) if isinstance(grupos, dict) else None
            nomes[j] = (g.get("subject") if isinstance(g, dict) else "") or "Grupo"
        else:
            nomes[j] = apelidos.get(j) or wa.numero_formatado(j)
    return nomes


def html_agendados(ctx) -> str:
    """Pedido 72: tudo que está agendado (com Cancelar) e os últimos resolvidos, sem texto."""
    from app.paginas import conversas
    try:
        itens = agendados.da(ctx).listar()
    except (VigiaErro, OSError, ValueError) as e:
        log.warning("agendados indisponíveis em Envios: %s", causa(e))
        return c.vazio("Não consegui ler os envios agendados", "Tente recarregar a página")
    itens = [i for i in itens if i["estado"] == "agendado"] + [i for i in itens if i["estado"] != "agendado"][:10]
    if not itens:
        return c.vazio("Nada agendado", "Na conversa, escreva a mensagem e clique no relógio ao lado de Enviar")
    nomes, linhas = _nomes_agendados(ctx, itens), []
    for it in itens:
        rot, tipo = _AG_ESTADO.get(it["estado"], (it["estado"], ""))
        est = (c.pilula(rot, "atrasado") if tipo == "atrasado" else c.pilula(rot) if tipo == "alerta"
               else f'<span class="ok">{c.esc(rot)}</span>' if tipo == "ok" else f'<span class="meta">{c.esc(rot)}</span>')
        motivo = f'<div class="meta">{c.esc(it["motivo"])}</div>' if it["motivo"] else ""
        oque = conversas.oque_agendado(it) if it["estado"] == "agendado" else {"texto": "Mensagem"}.get(it["tipo"], conversas._ROTULO_AG.get(it["tipo"], "Arquivo"))
        acao = (f'<button type="button" class="btn sm" data-ag-cancelar="{int(it["id"])}">Cancelar</button>' if it["estado"] == "agendado" else "")
        linhas.append(f'<tr><td>{c.esc(conversas.quando_agendado(it["quando"]))}</td>'
                      f'<td><a href="/conversas?c={c.esc(it["para"])}">{c.esc(nomes[it["para"]])}</a></td>'
                      f'<td class="env-ag-oque">{c.esc(oque)}</td><td>{est}{motivo}</td><td><div class="acoes">{acao}</div></td></tr>')
    return ('<table class="tabela env-lotes"><thead><tr><th>Quando</th><th>Para</th><th>O quê</th><th>Estado</th><th></th></tr></thead>'
            f'<tbody>{"".join(linhas)}</tbody></table>')


def _toast(req) -> str:
    aviso, lote = c.query(req, "aviso"), c.query(req, "lote")
    if not lote.isdigit():
        return ""
    if aviso == "parado":
        return f"Lote {int(lote)} parado. O que já saiu, saiu."
    if aviso == "retomado":
        return f"Lote {int(lote)} de volta à fila."
    return ""


def html_pagina(ctx, fila: fila_envio.Fila) -> str:
    r = fila.resumo()
    lotes = fila.listar(50)
    from app.paginas import conversas
    pode_retomar = conversas.servicos(ctx).liberacao.liberada()
    cx = r["conexao"]
    kv = c.kv([
        ("Na fila", f'<b>{r["na_fila"]}</b><span class="sub">{c.esc(f"próximo envio em {r["proximo_em_s"]} s" if r["na_fila"] else "nada pendente")}</span>'),
        ("Hoje, conhecidos", f'<b>{r["hoje"]["conhecidos"]}</b> de {r["tetos"]["teto_dia_conhecidos"]}<span class="sub">quem já conversou com você</span>'),
        ("Hoje, novos", f'<b>{r["hoje"]["novos"]}</b> de {r["tetos"]["teto_dia_novos"]}<span class="sub">pausa dobrada entre eles</span>'),
        ("Conexão", f'<b class="{"ok" if cx == "open" else "ruim"}">{c.esc("Aberta" if cx == "open" else cx)}</b><span class="sub">lida do monitor, sem chamar a Evolution</span>'),
    ])
    if lotes:
        tabela = ('<table class="tabela env-lotes"><thead><tr><th>Quando</th><th>Origem</th><th>Destinatários</th><th>Estado</th><th></th></tr></thead>'
                  f'<tbody>{"".join(_linha(l, pode_retomar) for l in lotes)}</tbody></table>')
    else:
        tabela = c.vazio("Nenhum lote ainda", "O que as suas ferramentas mandarem pela API local aparece aqui")
    lo, hi = r["tetos"]["intervalo_s"]
    lo2, hi2 = r["tetos"]["intervalo_novos_s"]
    miolo = (f'<div id="env-kv">{kv}</div>{c.rotulo("Agendados")}<div id="env-agendados">{html_agendados(ctx)}</div>{c.rotulo("Lotes")}<div id="env-lotes">{tabela}</div>'
             f'{c.rotulo("Como integrar")}<pre class="env-pre">{c.esc(_EXEMPLO)}</pre>'
             '<p class="meta-linha env-nota">O JSON vai pelo stdin e o token sai de ~/.vigia-envio.env; nada aparece no ps. '
             'Contrato completo em docs/API-ENVIO.md.</p>')
    alertas.fechar(c.dados_dir(ctx), "agendado-nao-saiu", "agendado-incerto")   # a pessoa viu a lista: o aviso cumpriu o papel
    status = c.rodape(c.esc(f"Um envio por vez, com pausa de {lo} a {hi} s ({lo2} a {hi2} s para número novo)"), "env-status")
    return _CSS + c.painel(c.miolo(miolo, largo=True), toolbar_html=c.toolbar(c.titulo("Envios"), c.espaco()),
                           status_html=status, aria="Envios") + _JS


ROTAS = {"/envios": envios, "/envios/{id}": estado_get, "/envios/{id}/parar": parar_post, "/envios/{id}/retomar": retomar_post}
