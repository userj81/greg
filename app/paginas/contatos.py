"""Contatos → frentes (D-49, pedido 34): área para marcar de qual frente é cada conversa, várias de uma vez.
Quem chama: app/server.py (via ROTAS). GET /contatos = abas (conversas ativas · sugestões · grupos · contatos · sem número
salvo · marcadas), busca na página, marcação múltipla; POST /contatos = mandar os marcados para uma frente, aceitar a
sugestão dos marcados, confirmar todas as sugestões, ou tirar a marcação.
A IA usa o vínculo como frente padrão da conversa (app/ponte/classificar.py); marcado pelo dono, o item nasce confirmado
na frente; sugestão pelo nome, nasce na frente "a confirmar".
O que não faz: não lê conteúdo de mensagem (só nome da agenda e assunto de grupo), não muda o que a IA lê (grupos seguem
a lista branca de config.toml), não mexe em item já criado no Frentes.
"""
import logging

from app import vinculos as vc
from app import whatsapp as wa
from app.erros import VigiaErro, causa
from app.paginas import _comum as c

log = logging.getLogger("vigia.contatos")

ROTULOS = {"ativas": "Conversas ativas", "sugestoes": "Sugestões", "grupos": "Grupos", "contatos": "Contatos",
           "outros": "Sem número salvo", "marcadas": "Marcadas"}
TIPO_PADRAO = "ativas"
MAX_POR_VEZ = 500
NENHUMA = "0"   # "conferido, sem frente": a sugestão não volta


def _frentes(ctx) -> list[dict] | None:
    try:
        return [f for f in ctx.ponte.frentes() if f.get("id") is not None]
    except (VigiaErro, OSError, ValueError) as e:
        log.warning("frentes indisponíveis: %s", causa(e))
        return None


def _tipo(req, forcado: str | None = None) -> str:
    t = forcado if forcado is not None else c.query(req, "tipo")
    return t if t in ROTULOS else TIPO_PADRAO


def _agenda_toda(ctx, psql) -> dict:
    """{jid: nome} das três abas da agenda + grupos do cache (assunto). Falha do banco SOBE."""
    from app.paginas import bloqueios, conversas
    apelidos = ctx.banco.apelidos()
    cache = conversas.servicos(ctx).grupos.nomes([])
    nomes = {}
    for t in ("grupos", "contatos", "outros"):
        for r in wa.agenda(psql, t):
            nomes[r["jid"]] = bloqueios._nome(ctx, r["jid"], agenda=r["nome"], apelidos=apelidos, grupos=cache)
    for jid, g in cache.items():
        if jid not in nomes and wa.JID_OK.match(jid):
            nomes[jid] = bloqueios._nome(ctx, jid, agenda=g.get("subject", ""), apelidos=apelidos, grupos=cache)
    return nomes


def _linhas_da_aba(tipo: str, agenda: dict, ativas: list[dict], vinc: dict, frentes: list[dict]) -> list[dict]:
    """[{jid, nome, vinc, sug}] na ordem da aba."""
    def item(jid, nome):
        return {"jid": jid, "nome": nome, "vinc": vinc.get(jid), "sug": vc.sugerir(nome, frentes)}
    if tipo == "ativas":
        return [item(a["jid"], a["nome"]) for a in ativas]
    if tipo in ("grupos", "contatos", "outros"):
        suf = wa.TIPOS_AGENDA[tipo]
        return [item(j, n) for j, n in agenda.items() if j.endswith(suf)]
    todos = dict(agenda)
    for a in ativas:
        todos.setdefault(a["jid"], a["nome"])
    for j in vinc:
        todos.setdefault(j, wa.numero_formatado(j) if j.endswith("@s.whatsapp.net") else j.split("@")[0])
    linhas = [item(j, n) for j, n in todos.items()]
    if tipo == "marcadas":
        return [x for x in linhas if (x["vinc"] or {}).get("origem") == "dono"]
    return [x for x in linhas if x["sug"] and (x["vinc"] or {}).get("origem") != "dono"]   # sugestoes


def _estado(x: dict, nomes_fr: dict) -> tuple[str, str]:
    """(html do estado, frente sugerida para 'aceitar' ou '')."""
    v, s = x["vinc"], x["sug"]
    if v and v["origem"] == "dono":
        if v["frente_id"] is None:
            return '<span class="meta-linha">sem frente (conferido)</span>', ""
        return f'<span class="ct-frente">{c.esc(nomes_fr.get(v["frente_id"], f"frente {v["frente_id"]}"))}</span>', ""
    if s:
        rot = "sugestão" if s["forca"] == "forte" else "talvez"
        return (f'<span class="ct-sug ct-{s["forca"]}">{rot}: {c.esc(nomes_fr.get(s["frente_id"], "?"))}</span>',
                str(s["frente_id"]))
    return '<span class="meta-linha">–</span>', ""


def _tabela(tipo, linhas, nomes_fr, frentes, indisponivel) -> tuple[str, str]:
    """(corpo, barra). A barra fica fora do corpo rolável (rodapé do painel); os controles dela apontam para o
    formulário da tabela pelo atributo form."""
    if indisponivel:
        return c.erro_inline("O banco da Evolution ou o Frentes não respondeu agora.",
                             f'<a class="btn" href="/contatos?tipo={tipo}">Tentar de novo</a>'), ""
    if not linhas:
        return c.vazio(f'Nada em "{ROTULOS[tipo]}"'), ""
    tr = []
    for i, x in enumerate(linhas):
        jid = x["jid"]
        sub = wa.numero_formatado(jid) if jid.endswith("@s.whatsapp.net") else ("grupo" if jid.endswith("@g.us") else "")
        estado, sug = _estado(x, nomes_fr)
        busca = c.esc(vc.normalizar(f"{x['nome']} {sub} {estado}"))
        sub_html = f'<div class="meta-linha">{c.esc(sub)}</div>' if sub else ""
        tr.append(f'<tr data-busca="{busca}"><td class="fora-marca"><input type="checkbox" id="k{i}" '
                  f'name="j:{c.esc(jid)}" value="{c.esc(sug or "1")}"></td>'
                  f'<td><label for="k{i}" class="ct-nome">{c.esc(x["nome"])}</label>{sub_html}</td><td>{estado}</td></tr>')
    opcoes = "".join(f'<option value="{c.esc(str(f["id"]))}">{c.esc(f.get("nome", "?"))}</option>'
                     for f in sorted(frentes, key=lambda f: vc.normalizar(f.get("nome"))))
    n = len(linhas)
    corpo = (f'<form method="post" action="/contatos" id="form-ct"><input type="hidden" name="tipo" value="{tipo}">'
             f'<table class="tabela ct-lista"><thead><tr><th class="fora-marca"><span class="sr-only">Marcar</span></th><th>Nome</th>'
             f'<th>Frente</th></tr></thead><tbody>{"".join(tr)}</tbody></table></form>'
             f'<div id="fora-nada" hidden>{c.vazio("Nada com esse nome nesta lista")}</div>')
    barra = (f'<div class="ct-barra"><span id="fora-conta" role="status">Nenhum marcado</span>'
             f'<span class="meta-linha" id="fora-visiveis" role="status">{n} de {n}</span>'
             f'<button type="button" class="btn sm" id="ct-todos">Marcar os visíveis</button>'
             f'<select class="campo" name="frente" form="form-ct" aria-label="Frente para os marcados"><option value="">Escolha a frente…</option>'
             f'{opcoes}<option value="{NENHUMA}">Nenhuma frente (conferido)</option></select>'
             f'<button type="submit" form="form-ct" name="acao" value="marcar" class="btn primario ct-acao" disabled>Mandar marcados</button>'
             f'<button type="submit" form="form-ct" name="acao" value="aceitar" class="btn ct-acao" disabled>Aceitar sugestão</button>'
             f'<button type="submit" form="form-ct" name="acao" value="limpar" class="btn ct-acao" disabled>Tirar</button></div>'
             + _JS_K1)
    return corpo, barra


def _toast(req) -> str:
    n = c.query(req, "feitos")
    if n.isdigit():
        n = int(n)
        return f"{n} conversa{'s' if n != 1 else ''} atualizada{'s' if n != 1 else ''}. A IA usa isso a partir do próximo ciclo."
    return ""


def pagina_get(req, aviso: str = "", status: int = 200, tipo: str | None = None):
    """`aviso`: HTML já escapado (erro inline) mostrado no topo; o sucesso da volta (?feitos=N) vira toast."""
    from app.paginas import conversas
    ctx = req.ctx
    tipo = _tipo(req, tipo)
    psql = getattr(ctx, "psql", None)
    frentes = _frentes(ctx)
    indisponivel = psql is None or frentes is None
    agenda, ativas = {}, []
    if not indisponivel:
        try:
            agenda = _agenda_toda(ctx, psql) if tipo != "ativas" else {}
            ativas = conversas.carregar(ctx)[0] if tipo in ("ativas", "sugestoes", "marcadas") else []
        except (VigiaErro, ValueError) as e:
            log.warning("agenda indisponível: %s", causa(e))
            indisponivel = True
    frentes = frentes or []
    nomes_fr = {f["id"]: f.get("nome", "?") for f in frentes}
    vinc = ctx.banco.vinculos()
    linhas = [] if indisponivel else _linhas_da_aba(tipo, agenda, ativas, vinc, frentes)
    n_auto = sum(1 for v in vinc.values() if v["origem"] == "auto" and v["frente_id"] is not None)
    n_dono = sum(1 for v in vinc.values() if v["origem"] == "dono")
    # as abas viram um pull-down na toolbar (guia Papel: segmentado só em Ajustes)
    itens = []
    for t, rot in ROTULOS.items():
        atual = ' aria-current="page"' if t == tipo else ""
        on = ' class="on"' if t == tipo else ""
        extra = {"sugestoes": n_auto, "marcadas": n_dono}.get(t)
        cont = f' <span class="suave">{extra}</span>' if extra is not None else ""
        itens.append(f'<a role="menuitem"{on} href="/contatos?tipo={t}"{atual}>{rot}{cont}</a>')
    rotulo_atual = ROTULOS[tipo] + (f" ({ {'sugestoes': n_auto, 'marcadas': n_dono}[tipo] })" if tipo in ("sugestoes", "marcadas") else "")
    filtro = c.pulldown(f"<span>{c.esc(rotulo_atual)}</span>", "".join(itens), id_="ct-tipo", direita=True, aria="Lista")
    confirmar_todas = ""
    if tipo == "sugestoes" and n_auto:
        # dois cliques no mesmo botão em vez de modal (JS da página)
        confirmar_todas = (f'<form method="post" action="/contatos" class="ct-todas" id="ct-todas">'
                           f'<input type="hidden" name="acao" value="confirmar_sugestoes">'
                           f'<button type="submit" class="btn" id="ct-todas-botao" data-rotulo="Confirmar as {n_auto} sugestões fortes" '
                           f'data-confirma="Confirmar: os itens dessas conversas passam a nascer confirmados">Confirmar as {n_auto} sugestões fortes</button></form>')
    corpo, barra = _tabela(tipo, linhas, nomes_fr, frentes, indisponivel)
    toolbar = c.toolbar(c.titulo("Contatos"), c.espaco(), filtro,
                        c.busca("q", placeholder="Buscar", id_="fora-busca", attrs='form="form-busca-ct"') if linhas else "")
    miolo = c.render("contatos.html", aviso=aviso, confirmar_todas=confirmar_todas, tabela=corpo)
    pagina = (f'<section class="painel detalhe" aria-label="Contatos">{toolbar}<div class="corpo">{miolo}</div>{barra}'
              f'{c.rodape("Marcada por você, o item nasce confirmado na frente. Sugestão pelo nome fica a confirmar até você aceitar")}</section>')
    return c.pagina("Contatos", pagina, "/contatos", status=status, ctx=ctx, toast=_toast(req) if status == 200 else "")


# auditoria K1 (lote 5): mandar os marcados sem escolher a frente recarregava a página e apagava todas as marcas
_JS_K1 = """<script>
(function () {
  var f = document.getElementById('form-ct'); if (!f) { return; }
  f.addEventListener('submit', function (ev) {
    var sel = document.querySelector('select[name="frente"][form="form-ct"]');
    if (!ev.submitter || ev.submitter.value !== 'marcar' || !sel || sel.value !== '') { return; }
    ev.preventDefault();
    window.vigiaToast('Escolha a frente antes de mandar os marcados. As marcas continuam.');
    sel.focus();
  });
})();
</script>"""


def _erro(req, msg: str, tipo: str):
    return pagina_get(req, aviso=c.erro_inline(msg), status=400, tipo=tipo)


def contatos_post(req):
    if (e := c.metodo_errado(req, "POST")):
        return e
    if not c.origem_ok(req):
        return c.proibido()
    ctx = req.ctx
    d = dict(req.corpo or {})
    acao = str(d.get("acao") or "")
    tipo = _tipo(req, str(d.get("tipo") or ""))
    banco = ctx.banco
    if acao == "confirmar_sugestoes":
        feitos = 0
        for jid, v in banco.vinculos().items():
            if v["origem"] == "auto" and v["frente_id"] is not None:
                banco.vincular(jid, v["frente_id"], "dono")
                feitos += 1
        return c.redirecionar(f"/contatos?tipo=marcadas&feitos={feitos}")
    if acao not in ("marcar", "aceitar", "limpar"):
        return c.texto_simples("pedido inválido", 400)
    marcados = {k[2:]: str(v) for k, v in d.items() if isinstance(k, str) and k.startswith("j:")}
    if not marcados:
        return _erro(req, "Marque ao menos uma conversa.", tipo)
    if len(marcados) > MAX_POR_VEZ:
        return _erro(req, f"No máximo {MAX_POR_VEZ} por vez; marque menos e repita.", tipo)
    if any(not wa.JID_OK.match(j) for j in marcados):
        return c.texto_simples("conversa inválida", 400)
    frentes = _frentes(ctx)
    if frentes is None:
        return _erro(req, "O Frentes não respondeu agora. Nada mudou; tente de novo.", tipo)
    validas = {f["id"] for f in frentes}
    feitos = 0
    if acao == "limpar":
        for jid in marcados:
            banco.desvincular(jid)
            feitos += 1
    elif acao == "marcar":
        bruto = str(d.get("frente") or "")
        if bruto == NENHUMA:
            fid = None
        elif bruto.isdigit() and int(bruto) in validas:
            fid = int(bruto)
        else:
            return _erro(req, "Escolha a frente antes de mandar os marcados.", tipo)
        for jid in marcados:
            banco.vincular(jid, fid, "dono")
            feitos += 1
    else:   # aceitar: o valor da caixa é a frente sugerida na linha
        for jid, sug in marcados.items():
            if sug.isdigit() and int(sug) in validas:
                banco.vincular(jid, int(sug), "dono")
                feitos += 1
        if not feitos:
            return _erro(req, "Nenhum dos marcados tinha sugestão. Escolha a frente e use Mandar marcados.", tipo)
    return c.redirecionar(f"/contatos?tipo={tipo}&feitos={feitos}")


def contatos_rota(req):
    if req.metodo.upper() in ("GET", "HEAD"):
        return pagina_get(req)
    return contatos_post(req)


ROTAS = {"/contatos": contatos_rota}
