"""Página Histórico (pedido 46, D-58): o que cada conversa trouxe e em que pé ficou, pelas análises que cada leitura grava.
À esquerda, as conversas do período agrupadas pela situação atual (aguardando você · aguardando o contato · encerradas);
à direita, a linha do tempo do período (de todas, ou só da conversa escolhida).
Rotas: GET /historico[?dias=1|7|30][&c=<jid>]
Quem chama: app/server.py (via ROTAS).
O que não faz: não lê mensagem crua, não chama Evolution, Frentes nem Claude, não escreve nada (só leitura do vigia.db
por app/analises.py); não loga nome nem resumo.
"""
import logging
import urllib.parse
from datetime import date, datetime

from app import analises
from app.paginas import _comum as c
from app.erros import causa

log = logging.getLogger("vigia.painel.historico")
LIMITE = 500                       # análises na linha do tempo; acima disso o rodapé avisa
PERIODOS = ((1, "24 horas"), (7, "7 dias"), (30, "30 dias"))
DIAS_PADRAO = 7
SECOES = (("aguardando_dono", "Aguardando você"), ("aguardando_contato", "Aguardando o contato"), ("encerradas", "Encerradas"))
# situação de uma análise que JÁ NÃO é a última da conversa: passou, por isso no passado
ANTES = {"aguarda_dono": "aguardava você", "aguarda_contato": "aguardava o contato", "encerrada": "encerrada"}
AGORA = {"aguarda_contato": "aguarda o contato", "encerrada": "encerrada"}


def _dias(req) -> int:
    bruto = c.query(req, "dias")
    return int(bruto) if bruto.isdigit() and int(bruto) in {d for d, _ in PERIODOS} else DIAS_PADRAO


def _href(dias: int, conversa: str = "") -> str:
    q = []
    if dias != DIAS_PADRAO:
        q.append(f"dias={int(dias)}")
    if conversa:
        q.append("c=" + urllib.parse.quote(conversa, safe=""))
    return "/historico" + ("?" + "&".join(q) if q else "")


def _pulldown_periodo(dias: int, conversa: str) -> str:
    itens = "".join(c.item_menu(rot, href=_href(d, conversa), marcado=d == dias) for d, rot in PERIODOS)
    return c.pulldown(c.esc(dict(PERIODOS)[dias]), itens, id_="hi-periodo", aria="Período")


def _busca_de(a: dict) -> str:
    return c.esc((str(a.get("contato") or "") + " " + str(a.get("resumo") or "")).lower())


def _item_conversa(a: dict, dias: int, sel: bool) -> str:
    """Uma conversa na lista: nome + quando foi a última leitura, e o resumo dela em até 2 linhas."""
    return (f'<a class="item-lista hi-it{" ativo" if sel else ""}" href="{c.esc(_href(dias, str(a["conversa"])))}"'
            f' data-busca="{_busca_de(a)}"{" aria-current=\"true\"" if sel else ""}>'
            f'<div class="cab"><span class="nome">{c.esc(a["contato"])}</span><time>{c.hora_curta(a["ts"])}</time></div>'
            f'<div class="texto">{c.esc(a["resumo"])}</div></a>')


def _lista(h: dict, dias: int, conversa: str) -> str:
    n = len(h["analises"])
    tudo = (f'<a class="item-lista hi-tudo{"" if conversa else " ativo"}" href="{c.esc(_href(dias))}"'
            f'{"" if conversa else " aria-current=\"true\""}><div class="cab"><span>Todas as conversas</span></div>'
            f'<div class="meta meta-linha">{n} {"leitura" if n == 1 else "leituras"} no período</div></a>')
    partes = [tudo]
    for chave, titulo in SECOES:
        itens = h[chave]
        if not itens:
            continue
        partes.append(f'<div class="secao-lista">{c.esc(titulo)} <span class="hi-n">{len(itens)}</span></div>')
        partes += [_item_conversa(a, dias, str(a["conversa"]) == conversa) for a in itens]
    return f'<div class="itens">{"".join(partes)}</div>'


def _rotulo_dia(dia: date) -> str:
    hoje = date.today()
    base = f"{analises.DIAS_SEMANA[dia.weekday()]}, {c.data_longa(dia.isoformat())}"
    if dia == hoje:
        return "Hoje, " + base
    if (hoje - dia).days == 1:
        return "Ontem, " + base
    return base[0].upper() + base[1:]


def _situacao(a: dict) -> str:
    if not a["atual"]:
        return f'<span class="meta-linha">{c.esc(ANTES.get(a["situacao"], a["situacao"]))}</span>'
    if a["situacao"] == "aguarda_dono":
        return c.pilula("aguarda você")
    return f'<span class="meta-linha">{c.esc(AGORA.get(a["situacao"], a["situacao"]))}</span>'


def _linha_do_tempo(linhas: list[dict], com_nome: bool) -> str:
    """Dia a dia, mais recente primeiro. Com nome: hora · quem · situação, e o que houve embaixo, na largura toda.
    Sem nome (conversa escolhida, o nome já está no cabeçalho): hora · o que houve · situação."""
    partes, dia = [], None
    for a in linhas:
        d = datetime.fromtimestamp(a["ts"])
        if d.date() != dia:
            if dia is not None:
                partes.append("</div>")
            dia = d.date()
            partes.append(f'<div class="hi-dia"><p class="rotulo">{c.esc(_rotulo_dia(dia))}</p>')
        txt = f'<p class="hi-txt">{c.esc(a["resumo"])}</p>'
        miolo = f'<b class="hi-nome">{c.esc(a["contato"])}</b>{_situacao(a)}{txt}' if com_nome else txt + _situacao(a)
        partes.append(f'<div class="hi-linha" data-busca="{_busca_de(a)}"><time>{d.hour}:{d.minute:02d}</time>{miolo}</div>')
    if dia is not None:
        partes.append("</div>")
    return "".join(partes)


def _cabecalho_conversa(atual: dict) -> str:
    """Conversa escolhida: nome, em que pé está e o atalho para abrir a conversa."""
    pe = {"aguarda_dono": "Aguarda você", "aguarda_contato": "Aguarda o contato", "encerrada": "Encerrada"}.get(atual["situacao"], "")
    link = "/conversas?c=" + urllib.parse.quote(str(atual["conversa"]), safe="")
    return (f'<div class="hi-cab"><div class="hi-quem"><span>{c.esc(atual["contato"])}</span></div>'
            f'<div class="acoes"><a class="btn" href="{c.esc(link)}">Abrir conversa</a></div></div>'
            f'<p class="meta-linha hi-pe">{c.esc(pe)} desde {c.esc(c.quando_longo(atual["ts"]).lower())}.</p>')


def _status_cartoes(k: dict, cortado: bool, da_conversa: bool = False) -> str:
    """Rodapé do detalhe: os cartões do período (de todas as conversas ou só da escolhida)."""
    partes = [f'{k["no_frentes"]} no Frentes']
    if k["segurados"]:
        partes.append(f'{k["segurados"]} {"segurado" if k["segurados"] == 1 else "segurados"} pela IA')
    if k["provisorios"]:
        partes.append(f'{k["provisorios"]} ainda {"vai" if k["provisorios"] == 1 else "vão"} subir')
    corte = f". Mostrando as {LIMITE} leituras mais recentes" if cortado else ""
    return ("Cartões desta conversa: " if da_conversa else "Cartões no período: ") + ", ".join(partes) + corte


def _status_lista(h: dict) -> str:
    n, p = h["conversas"], len(h["aguardando_dono"])
    if not n:
        return "Nenhuma conversa no período"
    return f'{n} {"conversa" if n == 1 else "conversas"}' + (f', {p} {"aguarda" if p == 1 else "aguardam"} você' if p else "")


_CSS = """<style>
.hi-tudo { border-bottom: 1px solid var(--fio); }
.hi-n { font-weight: 400; margin-left: 2px; }
.hi-dia { margin-bottom: 18px; }
.hi-linha { display: grid; grid-template-columns: 44px 1fr auto; gap: 0 12px; align-items: baseline; padding: 7px 0; border-top: 1px solid var(--fio-fraco); }
.hi-dia > .rotulo + .hi-linha { border-top: 0; }
.hi-linha time { font: var(--t-meta) var(--fonte); color: var(--rotulo); font-variant-numeric: tabular-nums; }
.hi-nome { min-width: 0; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hi-txt { min-width: 0; color: var(--texto-2); overflow-wrap: anywhere; }
.hi-nome ~ .hi-txt { grid-column: 2 / -1; }
.hi-linha > .meta-linha, .hi-linha > .pilula { justify-self: end; white-space: nowrap; }
.hi-cab { display: flex; align-items: center; justify-content: space-between; gap: 12px; min-height: var(--controle); }
.hi-quem { display: flex; align-items: center; min-width: 0; font: var(--t-titulo-item) var(--fonte); }
.hi-quem > span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hi-pe { margin: 2px 0 18px; }
.hi-erro { padding: var(--pad-detalhe); }
</style>"""

_JS = """<script>
(function () {
  /* busca: filtra as conversas da lista e as linhas do tempo pelo texto (nome + resumo); dia sem linha visível some */
  var busca = document.getElementById('hi-busca'); if (!busca) return;
  var sem = document.getElementById('hi-sem-busca');
  busca.addEventListener('input', function () {
    var q = busca.value.trim().toLowerCase(), visiveis = 0;
    document.querySelectorAll('.hi-it[data-busca], .hi-linha[data-busca]').forEach(function (el) {
      var some = !!q && (el.getAttribute('data-busca') || '').indexOf(q) < 0;
      el.hidden = some;
      if (!some && el.classList.contains('hi-linha')) visiveis += 1;
    });
    document.querySelectorAll('.hi-dia').forEach(function (d) { d.hidden = !d.querySelector('.hi-linha:not([hidden])'); });
    if (sem) sem.hidden = !q || visiveis > 0;
  });
  busca.addEventListener('keydown', function (e) { if (e.key === 'Escape') { busca.value = ''; busca.dispatchEvent(new Event('input')); } });
})();
</script>"""


def historico_get(req):
    if (e := c.metodo_errado(req, "GET", "HEAD")):
        return e
    ctx = req.ctx
    dias, conversa = _dias(req), c.query(req, "c")
    try:
        geral = analises.montar(ctx.banco, dias=dias, limite=LIMITE)
        tem_algum = bool(geral["analises"]) or bool(ctx.banco.analises(limite=1))
        todas = geral["aguardando_dono"] + geral["aguardando_contato"] + geral["encerradas"]
        atual = next((a for a in todas if str(a["conversa"]) == conversa), None) if conversa else None
        cartoes = analises.cartoes_da_conversa(ctx.banco, conversa, dias=dias) if atual else geral["cartoes"]
    except Exception as e:  # banco ocupado ou ilegível: a tela diz o que houve e oferece tentar de novo; nunca o texto do erro
        log.warning("histórico ilegível: %s", causa(e))
        erro = c.erro_inline("Não deu para ler o histórico agora.", f'<a class="btn" href="{c.esc(_href(dias, conversa))}">Tentar de novo</a>')
        corpo = c.painel(f'<div class="hi-erro">{erro}</div>', toolbar_html=c.toolbar(c.titulo("Histórico"), c.espaco()),
                         status_html=c.rodape(""), classe="detalhe", aria="Histórico")
        return c.pagina("Histórico", corpo + _CSS, "/historico", ctx=ctx, status=500)

    if conversa and atual is None:
        conversa = ""                                  # conversa sem leitura no período (ou jid inventado): mostra tudo
    linhas = [a for a in geral["analises"] if str(a["conversa"]) == conversa] if conversa else geral["analises"]

    if not tem_algum:      # primeira vez: nenhuma leitura gravou histórico ainda
        corpo_lista = c.vazio("Nenhuma leitura registrada", "Cada leitura grava aqui o que a conversa trouxe e em que pé ficou")
        detalhe = c.vazio("O histórico começa na próxima leitura")
    elif not geral["analises"]:   # vazio: há histórico, mas não neste período
        corpo_lista = c.vazio("Nada neste período", "Escolha um período maior")
        detalhe = c.vazio("Nenhuma leitura neste período")
    else:
        corpo_lista = _lista(geral, dias, conversa)
        cab = _cabecalho_conversa(atual) if atual else ""
        detalhe = (c.miolo(cab + _linha_do_tempo(linhas, com_nome=not conversa))
                   + '<div class="vazio" id="hi-sem-busca" hidden><b>Nada com esse texto</b></div>')
    lista = c.painel(corpo_lista, toolbar_html=c.toolbar(c.titulo("Histórico"), c.espaco()),
                     status_html=c.rodape(c.esc(_status_lista(geral)), "hi-status-lista"), classe="lista", aria="Conversas do período")
    det = c.painel(detalhe, toolbar_html=c.toolbar(_pulldown_periodo(dias, conversa), c.espaco(),
                                                   c.busca("q", placeholder="Buscar", id_="hi-busca")),
                   status_html=c.rodape(c.esc(_status_cartoes(cartoes, geral["cortado"], bool(atual))), "hi-status-detalhe"), classe="detalhe",
                   aria="Linha do tempo")
    return c.pagina("Histórico", lista + det + _CSS + _JS, "/historico", ctx=ctx)


ROTAS = {"/historico": historico_get}
