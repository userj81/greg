"""Tela "Saúde do Greg" (pedido 63, lote 2): cada peça do Greg com o estado e, quando parou, o passo a passo.
Rota: GET /avisos (HTML; JSON com Accept: application/json). É para onde a faixa de avisos de todas as telas leva.
Quem chama: app/server.py (via ROTAS), a faixa de `_comum.faixa_avisos` e o menu da tela Conexão.
O que não faz: não testa nenhuma peça na hora (lê o que os detectores gravaram em dados/alertas.json), não conserta
nada e não mostra texto de conversa nem detalhe técnico: o texto vem só do catálogo (app/problemas.py).
"""
import json
import logging
from datetime import datetime

from app import saude
from app.erros import causa
from app.http import json_resposta
from app.paginas import _comum as c

log = logging.getLogger("vigia.painel.avisos")

_CSS = """<style>
.sau-resumo { display: flex; align-items: center; gap: 10px; margin: 0 0 16px; font: var(--t-titulo-item) var(--fonte); }
.sau-ponto { width: 9px; height: 9px; border-radius: 50%; background: var(--ok); flex: none; }
.sau-ponto.ruim { background: var(--destrutivo); }
.sau-peca { display: flex; gap: 12px; padding: 11px 14px; border-top: 1px solid var(--fio-fraco); }
.sau-peca:first-child { border-top: 0; }
.sau-peca .sau-ponto { margin-top: 5px; }
.sau-corpo { flex: 1; min-width: 0; }
.sau-nome { font-weight: 600; }
.sau-ok { color: var(--meta); font: var(--t-meta) var(--fonte); }
.sau-titulo { margin: 2px 0 0; color: var(--destrutivo); font-weight: 600; }
.sau-porque { margin: 2px 0 0; }
.sau-desde { margin: 2px 0 0; color: var(--meta); font: var(--t-meta) var(--fonte); }
.sau-passos { margin: 8px 0 0; padding-left: 20px; }
.sau-passos li { padding: 2px 0; }
.sau-passos code { font: .9231rem ui-monospace, Menlo, monospace; background: var(--citacao-fundo); padding: 1px 5px; border-radius: 4px; user-select: all; }
.sau-acao { margin-top: 8px; }
.sau-nota { margin-top: 14px; }
</style>"""

_NOME_LINK = {"/conexao": "Abrir Conexão", "/demandas": "Abrir Demandas"}


def _passo(texto: str) -> str:
    """Um passo do catálogo; o trecho depois de 'rode:' / 'Rode no Terminal:' vira <code> para copiar."""
    for marca in ("rode no Terminal: ", "Rode no Terminal: ", "rode: ", "Rode: ", "Para religar: "):
        if marca in texto:
            antes, cmd = texto.split(marca, 1)
            return f"{c.esc(antes + marca)}<code>{c.esc(cmd)}</code>"
    return c.esc(texto)


def _desde(ts: int) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%d/%m às %H:%M")
    except (ValueError, OverflowError, OSError):
        return ""


def _peca(p: dict) -> str:
    if not p["avisos"]:
        return (f'<div class="sau-peca"><span class="sau-ponto" aria-hidden="true"></span><div class="sau-corpo">'
                f'<span class="sau-nome">{c.esc(p["rotulo"])}</span> <span class="sau-ok">sem aviso</span></div></div>')
    blocos = []
    for a in p["avisos"]:
        passos = "".join(f"<li>{_passo(x)}</li>" for x in a["passos"])
        link = (f'<div class="sau-acao"><a class="btn" href="{c.esc(a["link"])}">{c.esc(_NOME_LINK.get(a["link"], "Abrir"))}</a></div>'
                if a["link"] else "")
        desde = _desde(a["desde"])
        blocos.append(f'<p class="sau-titulo">{c.esc(a["titulo"])}</p><p class="sau-porque">{c.esc(a["por_que"])}</p>'
                      + (f'<p class="sau-desde">Desde {c.esc(desde)}</p>' if desde else "")
                      + f'<ol class="sau-passos">{passos}</ol>{link}')
    return (f'<div class="sau-peca"><span class="sau-ponto ruim" aria-hidden="true"></span><div class="sau-corpo">'
            f'<span class="sau-nome">{c.esc(p["rotulo"])}</span>{"".join(blocos)}</div></div>')


def _ultima_leitura(ctx) -> str:
    try:
        d = json.loads((c.dados_dir(ctx) / "ultima-leitura.json").read_text(encoding="utf-8"))
        return c.quando_longo(int(d["em"]))
    except (OSError, ValueError, KeyError, TypeError):
        return "ainda não rodou"


def html_pagina(ctx) -> str:
    pecas = saude.pecas(c.dados_dir(ctx))
    n = sum(len(p["avisos"]) for p in pecas)
    resumo = ("Tudo funcionando" if not n else "1 peça precisa de você" if n == 1 else f"{n} avisos abertos")
    pausado = (c.dados_dir(ctx) / "PAUSADO").exists()
    miolo = (f'<div class="sau-resumo" id="sau-resumo"><span class="sau-ponto{" ruim" if n else ""}" aria-hidden="true"></span>'
             f'<span>{c.esc(resumo)}</span></div>'
             f'<div class="grupo" id="sau-pecas">{"".join(_peca(p) for p in pecas)}</div>'
             f'<p class="meta-linha sau-nota" id="sau-nota">Última leitura das conversas: {c.esc(_ultima_leitura(ctx))}'
             f'{". Leituras pausadas por você" if pausado else ""}. '
             'O aviso some sozinho quando a peça volta a funcionar.</p>')
    js = """<script>
(function () {
  setInterval(function () {
    if (document.hidden) { return; }
    fetch(location.pathname, {credentials: 'same-origin'}).then(function (r) { return r.ok ? r.text() : ''; }).then(function (h) {
      if (!h) { return; }
      var doc = new DOMParser().parseFromString(h, 'text/html');
      ['sau-resumo', 'sau-pecas', 'sau-nota'].forEach(function (id) {
        var n = doc.getElementById(id), a = document.getElementById(id);
        if (n && a && n.innerHTML !== a.innerHTML) { a.innerHTML = n.innerHTML; }
      });
    }).catch(function () {});
  }, 30000);
})();
</script>"""
    return _CSS + c.painel(c.miolo(miolo), toolbar_html=c.toolbar(c.titulo("Saúde do Greg"), c.espaco()),
                           status_html=c.rodape("Lido do que o Greg anotou; nada é testado ao abrir esta tela"),
                           aria="Saúde do Greg") + js


def avisos_get(req):
    if (e := c.metodo_errado(req, "GET", "HEAD")):
        return e
    if "application/json" in str((getattr(req, "cabecalhos", None) or {}).get("accept", "")):
        try:
            return json_resposta({"pecas": saude.pecas(c.dados_dir(req.ctx))})
        except Exception as e:  # noqa: BLE001
            log.warning("saúde em JSON falhou: %s", causa(e))
            return json_resposta({"pecas": []}, 500)
    return c.pagina("Saúde do Greg", html_pagina(req.ctx), "/conexao", ctx=req.ctx)


ROTAS = {"/avisos": avisos_get}
