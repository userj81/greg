"""Bloqueios (D-41, pedidos 21 e 22; seletor D-46, pedido 29): conversa que o Greg não guarda, não mostra e a IA não lê.
Quem chama: app/server.py (via ROTAS). GET /bloqueios = página "Fora do Greg": abas grupos · contatos · sem número
salvo, vindas da agenda que a Evolution sincronizou (tabela Contact, só SELECT, via vigia_ro), busca na própria página
e marcação múltipla; POST /bloqueios = bloquear (um alvo ou os vários marcados) e desbloquear. O cartão em /ajustes e
o botão "Bloquear" no topo de uma conversa aberta continuam.
O que não faz: não sai do grupo nem bloqueia o contato no WhatsApp (isso é no celular); não entra em grupo pelo link
(o convite só é LIDO, `inviteInfo`); nunca chama fetchAllGroups (D-40); desbloquear não traz de volta o que já foi apagado.
"""
import logging
import urllib.parse

from app import whatsapp as wa
from app.erros import VigiaErro, causa
from app.paginas import _comum as c

log = logging.getLogger("vigia.bloqueios")

_VOLTAS = ("/ajustes", "/conversas", "/bloqueios")
TIPO_PADRAO = "grupos"
ROTULOS = {"grupos": "Grupos", "contatos": "Contatos", "outros": "Sem número salvo"}
MAX_POR_VEZ = 200   # bloqueios num POST: cada um é um subprocesso psql (~0,1 s); acima disso, marcar menos e repetir


def _nome(ctx, jid: str, *, agenda: str = "", apelidos: dict | None = None, grupos: dict | None = None) -> str:
    """Nome como o resto do Greg mostra: apelido > agenda (Contact.pushName) > número, ou '~Nome' para quem escreve
    sem número salvo (@lid). GRUPO nunca usa a agenda (pedido 65): a Evolution grava em Contact.pushName do grupo o nome
    de quem mandou a última mensagem, não o assunto (medido em 13/11: 41 grupos com os dois nomes, nenhum igual). O
    nome do grupo é só o assunto guardado no cache (D-40, sem consultar aqui); sem ele, 'Grupo'."""
    from app.paginas import conversas
    ap = (apelidos if apelidos is not None else ctx.banco.apelidos()).get(jid, "")
    if jid.endswith("@g.us"):
        cache = grupos if grupos is not None else conversas.servicos(ctx).grupos.nomes([])
        return wa.nome_da_conversa(jid, apelido=ap, grupo=(cache.get(jid) or {}).get("subject", ""))
    if jid.endswith("@lid"):
        return wa.nome_da_conversa(jid, apelido=ap, push=agenda)
    return wa.nome_da_conversa(jid, apelido=ap, contato=agenda)


def lista(ctx) -> list[str] | None:
    """jids bloqueados em ordem; None se o banco não respondeu."""
    psql = getattr(ctx, "psql", None)
    if psql is None:
        return None
    try:
        return sorted(wa.bloqueados(psql))
    except (VigiaErro, ValueError) as e:
        log.warning("bloqueios indisponíveis: %s", causa(e))
        return None


def _tipo_de(jid: str) -> str:
    if jid.endswith("@g.us"):
        return "grupo"
    return "sem número salvo" if jid.endswith("@lid") else "contato"


def _nomes_agenda(ctx, jids) -> dict:
    """{jid: pushName} da agenda para jids já conhecidos (os bloqueados). Banco fora → {} e o nome cai no número/'Grupo'."""
    psql = getattr(ctx, "psql", None)
    if psql is None or not jids:
        return {}
    try:
        return wa.nomes_agenda(psql, jids)
    except (VigiaErro, ValueError) as e:
        log.warning("nomes da agenda indisponíveis: %s", causa(e))
        return {}


def _nomes_dos_bloqueados(ctx, jids, *, ja: dict | None = None, apelidos: dict | None = None,
                          grupos: dict | None = None) -> dict:
    """jid → nome final de cada bloqueado: o que a página já resolveu (`ja`), senão agenda por jid, senão o padrão."""
    from app.paginas import conversas
    faltam = [j for j in jids if j not in (ja or {})]
    ag = _nomes_agenda(ctx, faltam)
    apelidos = apelidos if apelidos is not None else ctx.banco.apelidos()
    grupos = grupos if grupos is not None else conversas.servicos(ctx).grupos.nomes([])
    nomes = dict(ja or {})
    for j in faltam:
        nomes[j] = _nome(ctx, j, agenda=ag.get(j, ""), apelidos=apelidos, grupos=grupos)
    return nomes


def _html_fora(ctx, jids, volta: str, nomes: dict) -> str:
    """Lista dos bloqueados (avatar 24, nome, tipo em cinza, Desbloquear). `nomes`: jid → nome já resolvido.
    Desbloquear é direto (sem modal: o toast confirma); só vale para o que chegar depois."""
    if jids is None:
        return '<span class="bl-vazio">O banco da Evolution não respondeu agora.</span>'
    if not jids:
        return '<span class="bl-vazio">Nenhuma conversa bloqueada.</span>'
    linhas = []
    for jid in jids:
        nome = nomes.get(jid) or _nome(ctx, jid)
        linhas.append(
            f'<div class="bl-linha">{c.avatar(jid.endswith("@g.us"), "pp")}<span class="bl-nome">{c.esc(nome)}</span>'
            f'<span class="meta-linha">{_tipo_de(jid)}</span>'
            f'<form method="post" action="/bloqueios">'
            f'<input type="hidden" name="acao" value="desbloquear"><input type="hidden" name="c" value="{c.esc(jid)}">'
            f'<input type="hidden" name="volta" value="{volta}">{c.botao("Desbloquear", classe="sm", tipo="submit")}'
            f'</form></div>')
    return f'<div class="bl-linhas">{"".join(linhas)}</div>'


def html_grupo(ctx, aviso: str = "") -> str:
    """Linha "Conversas bloqueadas" do grupo Privacidade de /ajustes: lista dos bloqueados, campo para bloquear por link
    ou número (botão +) e o caminho para o seletor /bloqueios."""
    bl = lista(ctx)
    nomes = _nomes_dos_bloqueados(ctx, bl or [])
    novo = (f'<form class="bl-novo" method="post" action="/bloqueios">'
            f'<input type="hidden" name="acao" value="bloquear"><input type="hidden" name="volta" value="/ajustes">'
            f'<input class="campo" name="alvo" maxlength="300" required placeholder="Link de convite do grupo ou número com DDD" '
            f'aria-label="Link de convite do grupo ou número com DDD">'
            f'<button type="submit" class="btn icone" aria-label="Bloquear" title="Bloquear">{c.icone("mais-simples")}</button></form>'
            f'<div class="bl-escolher"><a class="btn" href="/bloqueios">Escolher na lista…</a>'
            f'<span class="meta-linha">da agenda: grupos e contatos, vários de uma vez</span></div>')
    conteudo = f'<div class="bl-linhas">{aviso}{_html_fora(ctx, bl, "/ajustes", nomes)}{novo}</div>'
    return (f'<div class="opt" id="bloqueios"><div class="l">Conversas bloqueadas<span>O Greg não lê nem classifica</span></div>'
            f'<div class="r">{conteudo}</div></div>')


# ------------------------------------------------------------------ página /bloqueios (pedido 29)

def _tipo(req, forcado: str | None = None) -> str:
    t = forcado if forcado is not None else c.query(req, "tipo")
    return t if t in ROTULOS else TIPO_PADRAO


def _aviso_get(req) -> tuple[str, str]:
    """(erro inline, toast) do que acabou de acontecer: sucesso vira toast; falha do banco fica inline."""
    fora = c.query(req, "fora")
    if fora.isdigit():
        n = int(fora)
        txt = (f"{n} conversa(s) agora fora do Greg: o que já estava guardado delas foi apagado e o que chegar não entra."
               if n else "Nenhuma conversa saiu.")
        if c.query(req, "falhou") == "1":
            return c.erro_inline(f"{txt} O banco parou de confirmar depois disso; as demais marcadas seguem dentro. Tente de novo."), ""
        return "", txt
    if c.query(req, "desbloqueado") == "1":
        return "", "Desbloqueada. O que chegar daqui para a frente volta a ser guardado."
    return "", ""


def _linha(i: int, jid: str, nome: str, fora: bool) -> str:
    """Uma conversa da aba: caixa, nome (contato ganha o número embaixo; grupo e @lid não repetem o que a aba já diz).
    Grupo cujo assunto ainda não está no cache diz isso, em vez de aparecer com nome de participante (pedido 65)."""
    sub = wa.numero_formatado(jid) if jid.endswith("@s.whatsapp.net") else ""
    if jid.endswith("@g.us") and nome == "Grupo":
        sub = "nome ainda não carregado"
    busca = c.esc(f"{nome} {sub}".strip().lower())
    caixa = f'<input type="checkbox" id="f{i}" name="j:{c.esc(jid)}" value="1"' + (" disabled" if fora else "") + ">"
    estado = c.pilula("fora", "atrasado") if fora else '<span class="meta-linha">dentro</span>'
    sub_html = f'<div class="meta-linha">{c.esc(sub)}</div>' if sub else ""
    return (f'<tr data-busca="{busca}"><td class="fora-marca">{caixa}</td>'
            f'<td><label for="f{i}">{c.esc(nome)}</label>{sub_html}</td><td>{estado}</td></tr>')


def _tabela(tipo: str, agenda: list[dict], nomes: dict, fora: set, indisponivel: bool) -> str:
    if indisponivel:
        return c.erro_inline("O banco da Evolution não respondeu agora. Nada para escolher; tente em instantes.")
    if not agenda:
        return c.vazio(f'Nada em "{ROTULOS[tipo]}"', "na agenda sincronizada")
    linhas = "".join(_linha(i, r["jid"], nomes[r["jid"]], r["jid"] in fora) for i, r in enumerate(agenda))
    n = len(agenda)
    # a confirmação é em dois cliques no próprio botão (JS da página), não em modal (guia Papel)
    return (f'<form method="post" action="/bloqueios" id="form-fora">'
            f'<input type="hidden" name="acao" value="bloquear_varios"><input type="hidden" name="tipo" value="{tipo}">'
            f'<table class="tabela fora-lista"><thead><tr><th class="fora-marca"><span class="sr-only">Marcar</span></th><th>Nome</th><th>Agora</th></tr></thead>'
            f'<tbody>{linhas}</tbody></table>'
            f'<div class="vazio" id="fora-nada" hidden><b>Nada com esse nome</b><span>nesta lista</span></div>'
            f'<div class="fora-rodape"><button type="submit" class="btn destrutivo" id="fora-botao" disabled '
            f'data-rotulo="Deixar fora os marcados" data-confirma="Apagar o que está guardado e deixar fora?">Deixar fora os marcados</button>'
            f'<span class="meta-linha" id="fora-conta" role="status">Nenhum marcado</span>'
            f'<span class="meta-linha" id="fora-visiveis" role="status">{n} de {n}</span></div></form>')


def pagina_get(req, aviso: str = "", status: int = 200, tipo: str | None = None):
    from app.paginas import conversas
    ctx = req.ctx
    tipo = _tipo(req, tipo)
    erro_get, toast = _aviso_get(req)
    aviso = aviso or erro_get
    psql = getattr(ctx, "psql", None)
    bloq, agenda, contagem, indisponivel = None, [], {}, psql is None
    if psql is not None:
        try:
            bloq = sorted(wa.bloqueados(psql))
            contagem = wa.contagem_agenda(psql)
            agenda = wa.agenda(psql, tipo)
        except (VigiaErro, ValueError) as e:
            log.warning("agenda indisponível: %s", causa(e))
            indisponivel, bloq, agenda, contagem = True, None, [], {}
    apelidos = ctx.banco.apelidos()
    fora = set(bloq or [])
    # pedido 65: os grupos da aba que ainda não têm assunto guardado entram na fila de busca do D-40 (um por vez, com
    # pausa e teto por hora, em segundo plano); grupo bloqueado não é consultado (D-41)
    cache_grupos = conversas.servicos(ctx).grupos.nomes(
        [r["jid"] for r in agenda if r["jid"].endswith("@g.us") and r["jid"] not in fora])
    nomes = {r["jid"]: _nome(ctx, r["jid"], agenda=r["nome"], apelidos=apelidos, grupos=cache_grupos) for r in agenda}
    if bloq:   # bloqueado de outra aba: nome pela agenda, por jid, para a lista "Fora agora" não dizer só "Grupo"
        nomes = _nomes_dos_bloqueados(ctx, bloq, ja=nomes, apelidos=apelidos, grupos=cache_grupos)
    abas = []
    for t, rot in ROTULOS.items():
        atual = ' aria-current="page"' if t == tipo else ""
        n = contagem.get(t)
        abas.append(f'<a role="menuitem"{" class=\"on\"" if t == tipo else ""} href="/bloqueios?tipo={t}"{atual}>{rot} '
                    f'<span class="suave">{n if n is not None else "–"}</span></a>')
    n_atual = contagem.get(tipo)
    rotulo_tipo = f"{ROTULOS[tipo]}" + (f' <span class="suave">{n_atual}</span>' if n_atual is not None else "")
    corpo = c.render("bloqueios.html", aviso=aviso,
                     fora=_html_fora(ctx, bloq, "/bloqueios", nomes), n_fora=str(len(fora)) if bloq is not None else "–",
                     tabela=_tabela(tipo, agenda, nomes, fora, indisponivel))
    toolbar = c.toolbar(c.botao_icone("voltar", "Voltar para Ajustes", href="/ajustes"), c.titulo("Fora do Greg"), c.espaco(),
                        c.pulldown(f"<span>{rotulo_tipo}</span>", "".join(abas), id_="fora-tipo", direita=True, aria="Tipo de conversa"),
                        c.busca("q", placeholder="Buscar por nome ou número", id_="fora-busca"))
    painel = c.painel(corpo, toolbar_html=toolbar, status_html=c.rodape("Nada muda no celular; o Greg só deixa de guardar"),
                      aria="Fora do Greg", id_="fora")
    return c.pagina("Fora do Greg", painel, "/ajustes", status=status, ctx=ctx, toast=toast)


def _erro_pagina(req, msg: str, tipo: str):
    return pagina_get(req, aviso=c.erro_inline(msg), status=400, tipo=tipo)


def _bloquear_varios(req, d: dict, rodar, volta: str = "/bloqueios"):
    """Chaves `j:<jid>`=1 marcadas na página (uma chave por jid: o servidor guarda só o primeiro valor de chave repetida).
    Para na primeira falha do banco e diz quantos saíram; o que não saiu segue dentro.
    `volta="/conversas"` (modo Selecionar da lista, D-48): volta à lista com `fora=N[&falhou=1]`."""
    tipo = _tipo(req, str(d.get("tipo") or ""))
    jids = [k[2:] for k, v in d.items() if isinstance(k, str) and k.startswith("j:") and str(v) in ("1", "on")]
    if not jids:
        return _erro_pagina(req, "Marque ao menos uma conversa antes de confirmar.", tipo)
    if len(jids) > MAX_POR_VEZ:
        return _erro_pagina(req, f"No máximo {MAX_POR_VEZ} conversas por vez; marque menos e repita.", tipo)
    if any(not wa.JID_OK.match(j) for j in jids):
        return c.texto_simples("conversa inválida", 400)
    feitos, falhou = 0, False
    for jid in jids:
        try:
            rodar("bloquear", jid)
            feitos += 1
        except (VigiaErro, ValueError) as e:
            log.warning("bloquear em lote parou em %d de %d: %s", feitos, len(jids), causa(e))
            falhou = True
            break
    sufixo = "&falhou=1" if falhou else ""
    if volta == "/conversas":
        return c.redirecionar(f"/conversas?fora={feitos}{sufixo}")
    return c.redirecionar(f"/bloqueios?tipo={tipo}&fora={feitos}{sufixo}")


def bloqueios_post(req):
    if (e := c.metodo_errado(req, "POST")):
        return e
    if not c.origem_ok(req):
        return c.proibido()
    ctx = req.ctx
    d = dict(req.corpo or {})
    acao = str(d.get("acao") or "")
    volta = str(d.get("volta") or "/ajustes")
    if volta not in _VOLTAS:
        volta = "/ajustes"
    rodar = getattr(ctx, "bloqueio", None)
    if acao not in ("bloquear", "desbloquear", "bloquear_varios") or rodar is None:
        return c.texto_simples("pedido inválido", 400)
    if acao == "bloquear_varios":
        return _bloquear_varios(req, d, rodar, volta)
    jid = str(d.get("c") or "")
    if jid:
        if not wa.JID_OK.match(jid):
            return c.texto_simples("conversa inválida", 400)
    elif acao == "bloquear":
        try:
            jid, nome = wa.alvo_do_bloqueio(str(d.get("alvo") or ""), getattr(ctx, "evo", None))
        except ValueError as e:
            return _erro(req, str(e))
        except VigiaErro as e:
            log.warning("convite não lido: %s", causa(e))
            return _erro(req, "A Evolution não respondeu ao ler o convite. Tente de novo em instantes.")
        if nome:
            from app.paginas import conversas
            conversas.servicos(ctx).grupos.lembrar(jid, nome)
    else:
        return c.texto_simples("conversa inválida", 400)
    try:
        rodar(acao, jid)
    except (VigiaErro, ValueError) as e:
        log.warning("%s falhou: %s", acao, causa(e))
        return _erro(req, "O banco não confirmou. Nada mudou; tente de novo.")
    if volta == "/conversas":
        # D-48: "fora=1" faz a lista dizer que a conversa saiu (ela some da lista; o aviso é a única pista).
        return c.redirecionar("/conversas?fora=1" if acao == "bloquear" else f"/conversas?c={urllib.parse.quote(jid, safe='@.')}")
    if volta == "/bloqueios":
        return c.redirecionar("/bloqueios?fora=1" if acao == "bloquear" else "/bloqueios?desbloqueado=1")
    return c.redirecionar(f"/ajustes?{'bloqueado' if acao == 'bloquear' else 'desbloqueado'}=1#bloqueios")


def _erro(req, msg: str):
    from app.paginas import ajustes
    return ajustes.ajustes_get(req, bloqueio_aviso=c.erro_inline(msg), status=400)


def bloqueios_rota(req):
    """GET/HEAD: página do seletor. POST: bloquear/desbloquear. Outro método: 405 (pelo bloqueios_post)."""
    if req.metodo.upper() in ("GET", "HEAD"):
        return pagina_get(req)
    return bloqueios_post(req)


ROTAS = {"/bloqueios": bloqueios_rota}
