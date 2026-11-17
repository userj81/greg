"""Frentes dentro do Greg (D-37): todas as frentes em cartões com a árvore lateral por seção (estrutura do app
Frentes), o detalhe de cada frente com os itens em grupos, e as ações do Frentes (jogar, feito, reabrir, mover,
confirmar) pela ponte com token. Mantém o que a página já fazia: itens do WhatsApp a confirmar e o contexto do ciclo.
Quem chama: app/server.py (via ROTAS; "{id}" é padrão numérico casado pelo servidor).
O que não faz: não guarda item (o dono do dado é o Frentes) e não decide frente; Frentes fora do ar vira aviso, não 500.
"""
import logging
import re
from pathlib import Path
from urllib.parse import quote
from datetime import date

from app import anexos, secoes
from app.erros import VigiaErro, causa
from app.lotes import contexto_do_ciclo
from app.http import Resposta, json_resposta
from app.paginas import _comum as c
from app.paginas.demandas import confirmar_item

log = logging.getLogger("vigia.painel.frentes")

# Mesmas regras do app Frentes (~/frentes/app/server.py), para a árvore e as fichas baterem com o original — com uma
# diferença pedida (pedido 41, 09/11/2026): tipo `pessoal` tem seção própria "Pessoal" (no Frentes cai em Interno), onde
# ficam as frentes de vida pessoal (Família, Saúde, a dele) para marcar conversas em Contatos.
SECAO_DE_TIPO = {"cliente": "Clientes", "contrato": "Contratos", "empresa": "Negócios",
                 "projeto": "Projetos", "problema": "Projetos", "interno": "Interno", "pessoal": "Pessoal"}
ORDEM_SECOES = ("Clientes", "Contratos", "Negócios", "Projetos", "Interno", "Pessoal")
ICONE_TIPO = {"tarefa": "▸", "prazo": "◷", "nota": "▪", "decisao": "✓", "pendencia": "◌", "risco": "△", "ideia": "◇"}
ROTULO_TIPO = {"tarefa": "tarefa", "prazo": "compromisso", "nota": "nota", "decisao": "decisão",
               "pendencia": "pendência", "risco": "risco", "ideia": "ideia"}
ROTULO_GRUPO = {"atrasados": "Atrasados", "hoje": "Hoje", "confirmar": "A confirmar", "proximos": "Próximos",
                "sem_prazo": "Sem data", "feitos": "Feitos"}
PRIORIDADE = {1: "Alta", 2: "Normal", 3: "Baixa"}
_DIAS = ("seg", "ter", "qua", "qui", "sex", "sáb", "dom")
_VOLTA_OK = re.compile(r"/frentes(/(\d+|inbox|secao/\d+))?")
FEITOS_NA_TELA = 30


class _UmaVez:
    """Embrulha a ponte para a página: estado() e itens_abertos() vão ao Frentes uma vez só por requisição,
    mesmo quando contexto_do_ciclo os pede de novo por dentro de contexto_markdown()."""

    def __init__(self, ponte):
        self._p = ponte
        self._cache = {}

    def estado(self):
        if "estado" not in self._cache:
            self._cache["estado"] = self._p.estado()
        return self._cache["estado"]

    def itens_abertos(self, **kw):
        chave = ("itens", tuple(sorted(kw.items())))
        if chave not in self._cache:
            self._cache[chave] = self._p.itens_abertos(**kw)
        return self._cache[chave]

    def contexto_markdown(self):
        return type(self._p).contexto_markdown(self)

    def __getattr__(self, nome):
        return getattr(self._p, nome)


# ------------------------------------------------------------------ pedaços de HTML (estilo Papel, pedido 39)

_CSS = """<style>
.fr-lista .corpo { padding: 4px 0; }
.fr-no { display: flex; align-items: center; gap: 8px; padding: 5px 14px; color: inherit; text-decoration: none; cursor: default; }
.fr-no:hover { background: var(--hover); text-decoration: none; }
.fr-no.ativo { background: var(--selecao); }
.fr-no .nome { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fr-no .n { color: var(--meta); font: var(--t-meta) var(--fonte); font-variant-numeric: tabular-nums; }
.fr-no .n.ruim { color: var(--destrutivo); font-weight: 600; }
.ficha { flex: none; width: 22px; height: 22px; border-radius: 5px; background: var(--ficha-fundo); color: var(--ficha-cor); font: 600 .7692rem/1 var(--fonte); letter-spacing: .02em; display: inline-grid; place-items: center; vertical-align: middle; }
.ficha svg { width: 13px; height: 13px; }
.ficha.grande { width: 28px; height: 28px; border-radius: 6px; font-size: .8462rem; }
.fr-captura { display: flex; gap: 8px; margin: 16px 0 18px; }
.fr-captura .campo { flex: 1; }
.fr-nova { display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0 18px; }
.fr-nova .campo[name="nome"] { flex: 1 1 220px; }
.fr-frentes .fr-nome { display: inline-flex; align-items: center; gap: 8px; font-weight: 600; color: inherit; text-decoration: none; }
.fr-frentes .fr-nome:hover { text-decoration: underline; }
.fr-frentes tr.parada td { color: var(--meta); }
.fr-frentes .pilula + .pilula { margin-left: 6px; }
.fr-grupo { margin: 0 0 12px; }
.fr-grupo > summary { list-style: none; display: flex; align-items: center; gap: 6px; padding: 6px 0; font: var(--t-rotulo) var(--fonte); color: var(--rotulo); cursor: default; }
.fr-grupo > summary::-webkit-details-marker { display: none; }
.fr-grupo > summary svg, .fr-contexto > summary svg { width: 12px; height: 12px; transition: transform .12s; transform: rotate(-90deg); }
.fr-grupo[open] > summary svg, .fr-contexto[open] > summary svg { transform: none; }
.fr-grupo > summary .n { color: var(--meta); font-weight: 400; }
.fr-grupo > summary .dica { color: var(--meta); font-weight: 400; margin-left: 4px; }
.fr-itens { list-style: none; margin: 0; padding: 0; }
.fr-item { display: flex; align-items: center; gap: 8px; padding: 5px 0; min-height: 32px; border-top: 1px solid var(--fio-fraco); }
.fr-item:first-child { border-top: 0; }
.fr-item form:not([hidden]) { display: contents; }
.fr-item .texto { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.fr-item.feito .texto { color: var(--meta); text-decoration: line-through; }
.fr-item .de { display: inline-block; margin-left: 6px; color: var(--meta); font: var(--t-meta) var(--fonte); text-decoration: none; }
.fr-tipo { flex: none; width: 12px; text-align: center; color: var(--meta); }
.fr-check { flex: none; width: 16px; height: 16px; padding: 0; border-radius: 50%; border: 1.5px solid var(--meta); background: transparent; color: var(--sobre-acento); font: 700 10px/13px var(--fonte); cursor: default; }
.fr-check:hover { border-color: var(--acento); }
.fr-check.feito { background: var(--acento); border-color: var(--acento); }
.fr-id { color: var(--rotulo); font: var(--t-meta) var(--fonte); font-variant-numeric: tabular-nums; }
.fr-item .menu > summary.btn { height: 22px; padding: 0 7px; font: var(--t-meta) var(--fonte); }
.fr-contexto { margin-top: 20px; }
.fr-contexto > summary { list-style: none; display: flex; align-items: center; gap: 6px; font: var(--t-rotulo) var(--fonte); color: var(--rotulo); cursor: default; padding: 6px 0; }
.fr-contexto > summary::-webkit-details-marker { display: none; }
.fr-contexto pre { margin-top: 6px; }
.fr-nota { margin: 0 0 14px; }
.fr-kv { margin-bottom: 4px; }
/* arquivadas (pedido 41): grupo fechado no fim da árvore; cada linha tem o botão de voltar */
.fr-arquivadas { margin-top: 8px; border-top: 1px solid var(--fio-fraco); padding-top: 4px; }
.fr-arquivadas > summary { list-style: none; display: flex; align-items: center; gap: 6px; padding: 6px 14px; font: var(--t-rotulo) var(--fonte); color: var(--rotulo); cursor: default; }
.fr-arquivadas > summary::-webkit-details-marker { display: none; }
.fr-arquivadas > summary svg { width: 12px; height: 12px; transition: transform .12s; transform: rotate(-90deg); }
.fr-arquivadas[open] > summary svg { transform: none; }
.fr-arquivadas > summary .n { color: var(--meta); font-weight: 400; }
.fr-arq { display: flex; align-items: center; gap: 8px; padding: 4px 14px 4px 14px; color: var(--meta); }
.fr-arq .nome { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fr-arq .ficha { opacity: .6; }
.fr-arq form { display: contents; }
.fr-arq .btn { height: 22px; padding: 0 7px; font: var(--t-meta) var(--fonte); }
.fr-arquivada-aviso { margin-bottom: 14px; }
/* seções dentro da frente (pedido 79): pastas na frente, e cada seção abre na sua própria janela */
.fr-pastas { margin: 0 0 18px; border: 1px solid var(--fio-fraco); border-radius: 8px; overflow: hidden; }
.fr-pasta { display: flex; align-items: center; gap: 10px; padding: 9px 12px; color: inherit; text-decoration: none; cursor: default; border-top: 1px solid var(--fio-fraco); }
.fr-pasta:first-child { border-top: 0; }
.fr-pasta:hover { background: var(--hover); text-decoration: none; }
.fr-pasta > svg:first-child { flex: none; width: 18px; height: 18px; color: var(--acento); }
.fr-pasta .nome { font-weight: 600; white-space: nowrap; }
.fr-pasta .desc { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--meta); font: var(--t-meta) var(--fonte); }
.fr-pasta .n { margin-left: auto; color: var(--meta); font: var(--t-meta) var(--fonte); font-variant-numeric: tabular-nums; }
.fr-pasta > svg:last-child { flex: none; width: 12px; height: 12px; transform: rotate(-90deg); color: var(--meta); }
.fr-grupo.grupo-dodia { margin-bottom: 16px; }
.fr-item a.fr-onde:hover { text-decoration: underline; }
.fr-sem-secao { margin: 0 0 4px; font: var(--t-rotulo) var(--fonte); color: var(--rotulo); }
.fr-trilha { display: flex; align-items: center; gap: 6px; margin: 0 0 12px; color: var(--meta); font: var(--t-meta) var(--fonte); }
.fr-trilha a { color: inherit; }
/* arquivos da seção (pedido 80): grade de miniaturas; clicar na imagem abre grande sem sair da página (:target) */
.fr-arquivos { margin: 22px 0 8px; }
.fr-arquivos > h3 { display: flex; align-items: center; gap: 8px; margin: 0 0 8px; font: var(--t-rotulo) var(--fonte); color: var(--rotulo); }
.fr-arquivos > h3 .n { color: var(--meta); font-weight: 400; }
.fr-arquivos > h3 .esp { flex: 1; }
.fr-arq-estado { color: var(--meta); font: var(--t-meta) var(--fonte); font-weight: 400; }
.fr-arq-estado.ruim { color: var(--destrutivo); }
.fr-grade { display: grid; grid-template-columns: repeat(auto-fill, minmax(168px, 1fr)); gap: 12px; }
.fr-cartao { min-width: 0; border: 1px solid var(--fio-fraco); border-radius: 8px; overflow: hidden; display: flex; flex-direction: column; }
.fr-cartao .previa { display: grid; place-items: center; height: 120px; background: var(--hover); color: var(--meta); text-decoration: none; }
.fr-cartao .previa img, .fr-cartao .previa video { width: 100%; height: 120px; object-fit: cover; display: block; }
.fr-cartao .previa b { font: 700 .8462rem/1 var(--fonte); letter-spacing: .04em; }
.fr-cartao .pe { display: flex; align-items: center; gap: 6px; padding: 6px 8px; }
.fr-cartao .pe .nome { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font: var(--t-meta) var(--fonte); }
.fr-cartao .pe .tam { color: var(--meta); font: var(--t-mini) var(--fonte); white-space: nowrap; }
.fr-cartao .pe form { display: contents; }
.fr-cartao .pe .btn { height: 20px; padding: 0 6px; font: var(--t-mini) var(--fonte); }
.fr-luz { display: none; position: fixed; inset: 0; z-index: 50; background: rgba(0, 0, 0, .82); align-items: center; justify-content: center; padding: 24px; }
.fr-luz:target { display: flex; }
.fr-luz img, .fr-luz video { max-width: 100%; max-height: 100%; border-radius: 6px; }
.fr-luz .fechar { position: absolute; inset: 0; }
.fr-luz img, .fr-luz video { position: relative; }
.fr-secao-editar, .fr-secao-nova { margin: 6px 0 0; }
.fr-secao-editar > summary, .fr-secao-nova > summary { list-style: none; display: inline-flex; cursor: default; }
.fr-secao-editar > summary::-webkit-details-marker, .fr-secao-nova > summary::-webkit-details-marker { display: none; }
.fr-secao-form { display: flex; flex-wrap: wrap; gap: 8px; margin: 8px 0 4px; }
.fr-secao-form .campo[name="nome"] { flex: 1 1 160px; }
.fr-secao-form .campo[name="descricao"] { flex: 3 1 260px; }
.fr-secao-nova { margin: 0 0 18px; }
/* linha do tempo da frente (pedido 86): só leitura, quando cada item abriu e fechou, por seção */
.fr-linha { margin-top: 20px; }
.fr-lt-sec { margin: 10px 0 2px; font: var(--t-rotulo) var(--fonte); color: var(--rotulo); }
.fr-lt { list-style: none; margin: 0; padding: 0; }
.fr-lt li { display: flex; gap: 12px; padding: 4px 0; border-top: 1px solid var(--fio-fraco); }
.fr-lt li:first-child { border-top: 0; }
.fr-lt .quando { flex: none; width: 176px; white-space: nowrap; color: var(--meta); font: var(--t-meta) var(--fonte); font-variant-numeric: tabular-nums; }
.fr-lt .texto { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.fr-lt li.fechado .texto { color: var(--meta); }
@media (max-width: 560px) { .fr-lt li { flex-direction: column; gap: 0; } .fr-lt .quando { width: auto; } }
</style>"""


def _pl(n: int, um: str, muitos: str) -> str:
    return f"{n} {um if n == 1 else muitos}"


def iniciais(nome) -> str:
    """Duas letras da ficha (mesma regra do Frentes: primeira letra das duas primeiras palavras)."""
    palavras = [p for p in re.split(r"[^0-9A-Za-zÀ-ÖØ-öø-ÿ]+", str(nome or "")) if p]
    if not palavras:
        return "??"
    if len(palavras) >= 2:
        return (palavras[0][0] + palavras[1][0]).upper()
    return palavras[0][:2].upper()


def ficha(nome, tipo, grande: bool = False) -> str:
    """Monograma cinza da frente (duas letras). Sem cor por tipo: no Papel a hierarquia vem do texto, não do quadrado."""
    return f'<span class="ficha{" grande" if grande else ""}" aria-hidden="true">{c.esc(iniciais(nome))}</span>'


def data_amigavel(iso, hoje: str) -> tuple[str, str]:
    """(rótulo, tipo) de um prazo AAAA-MM-DD: "ruim" vira pílula vermelha, "alerta" pílula âmbar, "" texto cinza."""
    if not iso:
        return "", ""
    d = str(iso)[:10]
    dd, mm = d[8:10], d[5:7]
    if d < hoje:
        return f"atrasado, {dd}/{mm}", "ruim"
    if d == hoje:
        return "hoje", "alerta"
    try:
        dias = (date.fromisoformat(d) - date.fromisoformat(hoje)).days
        alvo = date.fromisoformat(d)
    except ValueError:
        return f"{dd}/{mm}", ""
    if dias == 1:
        return "amanhã", "alerta"
    if dias <= 6:
        return f"{_DIAS[alvo.weekday()]} {dd}/{mm}", ""
    return f"{dd}/{mm}", ""


def prazo_html(rotulo: str, tipo: str) -> str:
    """Só atrasado (vermelho) e hoje/amanhã (âmbar) viram pílula; o resto é data cinza."""
    if not rotulo:
        return ""
    if tipo == "ruim":
        return c.pilula(rotulo, "atrasado")
    if tipo == "alerta":
        return c.pilula(rotulo)
    return f'<span class="meta">{c.esc(rotulo)}</span>'


def _ordem_grito(f: dict) -> tuple:
    """Quem grita primeiro: atrasado, urgente, com itens, parada (igual ao Frentes)."""
    grita = 0 if f.get("n_atrasados") else 1 if f.get("n_urgentes") else 2 if f.get("n_abertos") else 3
    return (grita, f.get("prioridade") or 9, -(f.get("n_abertos") or 0), str(f.get("nome", "")).lower())


def secao_de(f: dict) -> str:
    return SECAO_DE_TIPO.get(f.get("tipo"), "Interno")


def _arquivadas(banco) -> list[dict]:
    """Frentes arquivadas por aqui (vigia.db); banco sem o método ou com erro = lista vazia, nunca 500."""
    fn = getattr(banco, "frentes_arquivadas", None)
    if not callable(fn):
        return []
    try:
        return list(fn())
    except Exception as e:  # noqa: BLE001
        log.debug("arquivadas ilegíveis: %s", causa(e))
        return []


def html_arquivadas(arquivadas: list[dict], volta: str = "/frentes") -> str:
    """Grupo "Arquivadas (N)" no fim da árvore (fechado): nome apagado + "Desarquivar" em cada linha. Vazio = nada."""
    if not arquivadas:
        return ""
    linhas = "".join(
        f'<div class="fr-arq">{ficha(a.get("nome"), a.get("tipo"))}<span class="nome">{c.esc(a.get("nome"))}</span>'
        f'<form method="post" action="/frentes/{int(a["frente_id"])}/desarquivar"><input type="hidden" name="volta" value="{c.esc(volta)}">'
        f'<button type="submit" class="btn">Desarquivar</button></form></div>'
        for a in arquivadas)
    return (f'<details class="fr-arquivadas"><summary>{c.icone("chevron")}Arquivadas <span class="n">{len(arquivadas)}</span></summary>'
            f'{linhas}</details>')


def html_arvore(est: dict, ativo: str = "", arquivadas: list[dict] = ()) -> str:
    """Árvore do painel da lista: "Todas as frentes", Inbox (se tiver algo), as frentes por seção (monograma e contagem)
    e, no fim, "Arquivadas (N)" fechado (pedido 41)."""
    def no(href, ficha_html, nome, n, alerta=False):
        cls = ' class="fr-no ativo" aria-current="page"' if href == ativo else ' class="fr-no"'
        num = f'<span class="n{" ruim" if alerta else ""}">{c.esc(n)}</span>' if n else ""
        return f'<a href="{href}"{cls}>{ficha_html}<span class="nome">{c.esc(nome)}</span>{num}</a>'

    tot = est.get("totais") or {}
    partes = [no("/frentes", f'<span class="ficha" aria-hidden="true">{c.icone("aba")}</span>', "Todas as frentes",
                 tot.get("abertos") or "")]
    n_inbox = len(est.get("inbox") or [])
    if n_inbox:
        partes.append(no("/frentes/inbox", '<span class="ficha" aria-hidden="true">IN</span>', "Inbox", n_inbox, alerta=True))
    secoes: dict[str, list[str]] = {}
    for f in sorted(est.get("frentes") or [], key=_ordem_grito):
        secoes.setdefault(secao_de(f), []).append(
            no(f"/frentes/{int(f['id'])}", ficha(f.get("nome"), f.get("tipo")), f.get("nome"),
               f.get("n_abertos") or "", alerta=bool(f.get("n_atrasados"))))
    for nome in ORDEM_SECOES:
        if nome in secoes:
            partes.append(f'<div class="secao-lista">{c.esc(nome)}</div>' + "".join(secoes[nome]))
    partes.append(html_arquivadas(list(arquivadas or ()), ativo if _VOLTA_OK.fullmatch(ativo or "") else "/frentes"))
    return '<div class="arvore" aria-label="Frentes">' + "".join(partes) + "</div>"


def html_lista(est: dict, ativo: str = "", fora: bool = False, arquivadas: list[dict] = ()) -> str:
    """Painel da lista (380 px). `fora`: o Frentes não respondeu."""
    if fora:
        corpo, status = c.vazio("Frentes fora do ar", "A lista volta quando o app responder"), ""
    else:
        frentes = est.get("frentes") or []
        tot = est.get("totais") or {}
        corpo = html_arvore(est, ativo, arquivadas)
        status = c.esc(f'{_pl(len(frentes), "frente", "frentes")}, {_pl(int(tot.get("abertos") or 0), "item em aberto", "itens em aberto")}')
    return c.painel(corpo, toolbar_html=c.toolbar(c.titulo("Frentes"), c.espaco()), status_html=c.rodape(status),
                    classe="lista fr-lista", aria="Frentes")


def _linha_frente(f: dict) -> str:
    abertos, feitos = int(f.get("n_abertos") or 0), int(f.get("n_feitos") or 0)
    resumo = "; ".join(" ".join(str(d.get("texto", "")).split())[:70] for d in (f.get("destaques") or [])[:3])
    sinais = []
    if f.get("n_atrasados"):
        sinais.append(c.pilula(_pl(int(f["n_atrasados"]), "atrasado", "atrasados"), "atrasado"))
    if f.get("n_confirmar"):
        sinais.append(c.pilula(f'{int(f["n_confirmar"])} a confirmar'))
    if not sinais:
        sinais.append(f'<span class="meta">{"em dia" if abertos else "parada"}</span>')
    resumo_html = f'<div class="meta">{c.esc(resumo)}</div>' if resumo else ""
    return (f'<tr{"" if abertos else " class=\"parada\""}><td><a class="fr-nome" href="/frentes/{int(f["id"])}">'
            f'{ficha(f.get("nome"), f.get("tipo"))}{c.esc(f.get("nome"))}</a>{resumo_html}</td>'
            f'<td class="meta">{c.esc(f.get("tipo") or "")}</td><td class="num">{abertos}</td><td class="num">{feitos}</td>'
            f'<td>{"".join(sinais)}</td></tr>')


def _hidden_volta(volta: str) -> str:
    return f'<input type="hidden" name="volta" value="{c.esc(volta)}">'


def html_item(it: dict, hoje: str, frentes: list[dict], volta: str, feito: bool = False,
              secoes: list[dict] = (), na_secao=None, mostrar_secao: bool = False) -> str:
    """`mostrar_secao` (pedido 82): o item aparece fora da pasta dele, então diz de que seção vem, com link para ela."""
    iid = int(it["id"])
    hv = _hidden_volta(volta)
    acao = "reabrir" if feito else "feito"
    rot = "Reabrir" if feito else "Marcar como feito"
    check = (f'<form method="post" action="/frentes/item/{iid}/{acao}">{hv}'
             f'<button type="submit" class="fr-check{" feito" if feito else ""}" aria-label="{rot}" title="{rot}">{"✓" if feito else ""}</button></form>')
    tipo = it.get("tipo") or "tarefa"
    de = '<span class="de">do WhatsApp</span>' if it.get("origem") == "whatsapp" else ""
    if mostrar_secao and na_secao is not None:
        nome_secao = next((x.get("nome") for x in secoes if int(x["id"]) == na_secao), None)
        if nome_secao:
            de += f'<a class="de fr-onde" href="/frentes/secao/{int(na_secao)}">seção {c.esc(nome_secao)}</a>'
    rot_prazo, cls = ("", "") if feito else data_amigavel(it.get("prazo"), hoje)
    confirmar = "" if (feito or it.get("confirmado", 1)) else (
        f'<form method="post" action="/frentes/item/{iid}/confirmar">{hv}<button type="submit" class="btn sm">Confirmar</button></form>')
    mover = ""
    if not feito:
        destinos = [(str(int(f["id"])), f.get("nome")) for f in frentes if f.get("id") != it.get("frente_id")]
        if it.get("frente_id") is not None:
            destinos.append(("inbox", "Inbox"))
        itens = "".join(f'<button type="submit" role="menuitem" form="fr-mv-{iid}" name="frente_id" value="{c.esc(v)}">'
                        f'<span>{c.esc(n)}</span></button>' for v, n in destinos)
        form_secao = ""
        if secoes:   # pedido 79: as seções desta frente vêm antes das outras frentes
            alvos = [(str(int(x["id"])), f'Seção: {x["nome"]}') for x in secoes if int(x["id"]) != na_secao]
            if na_secao is not None:
                alvos.append(("0", "Sem seção"))
            itens = "".join(f'<button type="submit" role="menuitem" form="fr-sc-{iid}" name="secao_id" value="{c.esc(v)}">'
                            f'<span>{c.esc(n)}</span></button>' for v, n in alvos) + "<hr>" + itens
            form_secao = f'<form method="post" action="/frentes/item/{iid}/secao" id="fr-sc-{iid}" hidden>{hv}</form>'
        mover = (f'<form method="post" action="/frentes/item/{iid}/mover" id="fr-mv-{iid}" hidden>{hv}</form>' + form_secao
                 + c.pulldown("<span>Mover</span>", itens, id_=f"fr-mover-{iid}", direita=True,
                              aria="Mover para outra seção ou frente" if secoes else "Mover para outra frente"))
    return (f'<li class="fr-item{" feito" if feito else ""}">{check}'
            f'<span class="fr-tipo" title="{c.esc(ROTULO_TIPO.get(tipo, tipo))}" role="img" aria-label="{c.esc(ROTULO_TIPO.get(tipo, tipo))}">'
            f'{c.esc(ICONE_TIPO.get(tipo, "·"))}</span>'
            f'<span class="texto">{c.esc(it.get("texto"))}{de}</span>{prazo_html(rot_prazo, cls)}{confirmar}{mover}'
            f'<span class="fr-id">#{iid}</span></li>')


def agrupar(abertos: list[dict], hoje: str) -> dict[str, list[dict]]:
    """Mesma regra do Frentes: atrasado e hoje ficam no grupo deles (urgência ganha de procedência);
    'a confirmar' puxa só de próximos e sem data."""
    g = {k: [] for k in ("atrasados", "hoje", "confirmar", "proximos", "sem_prazo")}
    for it in sorted(abertos, key=lambda i: (str(i.get("prazo") or "9999"), i.get("prioridade") or 9, i.get("id", 0))):
        p = str(it.get("prazo") or "")[:10]
        if p and p < hoje:
            g["atrasados"].append(it)
        elif p and p == hoje:
            g["hoje"].append(it)
        elif not it.get("confirmado", 1):
            g["confirmar"].append(it)
        elif p:
            g["proximos"].append(it)
        else:
            g["sem_prazo"].append(it)
    return g


def _grupo(chave: str, rotulo: str, itens_html: str, total: int, aberto: bool = True, dica: str = "") -> str:
    d = f'<span class="dica">{c.esc(dica)}</span>' if dica else ""
    return (f'<details class="fr-grupo grupo-{chave}"{" open" if aberto else ""}><summary>{c.icone("chevron")}{c.esc(rotulo)}'
            f'<span class="n">{total}</span>{d}</summary><ul class="fr-itens">{itens_html}</ul></details>')


def html_grupos(abertos, feitos, hoje, frentes, volta, secs: list[dict] = (), na_secao=None,
                vazio: tuple = ("Nada nesta frente ainda", "Jogue algo na caixa acima")) -> str:
    grupos = agrupar(abertos, hoje)
    feitos = sorted(feitos, key=lambda i: str(i.get("concluido_em") or ""), reverse=True)
    grupos["feitos"] = feitos[:FEITOS_NA_TELA]
    partes = []
    for chave in ("atrasados", "hoje", "confirmar", "proximos", "sem_prazo", "feitos"):
        itens = grupos[chave]
        if not itens:
            continue
        total = len(feitos) if chave == "feitos" else len(itens)
        dica = "o Greg ou o ditado anotou e ficou em dúvida" if chave == "confirmar" else ""
        linhas = "".join(html_item(i, hoje, frentes, volta, feito=(chave == "feitos"), secoes=secs, na_secao=na_secao)
                         for i in itens)
        partes.append(_grupo(chave, ROTULO_GRUPO[chave], linhas, total, aberto=(chave != "feitos"), dica=dica))
    return "".join(partes) or (c.vazio(*vazio) if vazio else "")


def _vence_ate_hoje(it: dict, hoje: str) -> bool:
    p = str(it.get("prazo") or "")[:10]
    return bool(p) and p <= hoje


def html_do_dia(abertos, hoje, frentes, volta, secs: list[dict], onde: dict) -> str:
    """Pedido 82 (D-85): no topo da frente com seções, o que está atrasado ou vence hoje, de todas as seções e de fora
    delas, numa lista só (atrasados primeiro, pela mesma regra de agrupar()). Cada item diz de que seção vem e pode ser
    concluído ali; ele não muda de seção. Sem nenhum item assim, não há bloco."""
    g = agrupar(abertos, hoje)
    itens = g["atrasados"] + g["hoje"]
    if not itens:
        return ""
    ids = {int(x["id"]) for x in secs}
    linhas = []
    for it in itens:
        sid = onde.get(it.get("id"))
        linhas.append(html_item(it, hoje, frentes, volta, secoes=secs, na_secao=sid if sid in ids else None,
                                mostrar_secao=True))
    return _grupo("dodia", "Hoje", "".join(linhas), len(itens), dica="atrasados e o que vence hoje, de todas as seções")


def _form_nova_secao(fid: int, volta: str) -> str:
    return (f'<details class="fr-secao-nova"><summary class="btn">Nova seção</summary>'
            f'<form class="fr-secao-form" method="post" action="/frentes/{int(fid)}/secoes">{_hidden_volta(volta)}'
            f'<input class="campo" name="nome" type="text" required maxlength="{secoes.NOME_MAX}" aria-label="Nome da nova seção" '
            'placeholder="Nome. Ex.: Site, Vídeos, Financeiro" autocomplete="off">'
            f'<input class="campo" name="descricao" type="text" maxlength="{secoes.DESCRICAO_MAX}" '
            'aria-label="O que entra nesta seção" placeholder="O que entra aqui (opcional; ajuda a IA a escolher)" autocomplete="off">'
            f'{c.botao("Criar seção", tipo="submit")}</form></details>')


def _editar_secao(sec: dict, volta: str, n_arquivos: int = 0) -> str:
    sid = int(sec["id"])
    dica_apagar = ("Os itens voltam para Sem seção; nenhum é apagado" if not n_arquivos else
                   f"Os itens voltam para Sem seção; {_pl(n_arquivos, 'arquivo será apagado', 'arquivos serão apagados')} de verdade")
    return (f'<details class="fr-secao-editar"><summary class="btn sm">Editar seção</summary>'
            f'<form class="fr-secao-form" method="post" action="/frentes/secao/{sid}/alterar">{_hidden_volta(volta)}'
            f'<input class="campo" name="nome" type="text" required maxlength="{secoes.NOME_MAX}" aria-label="Nome da seção" '
            f'value="{c.esc(sec.get("nome"))}" autocomplete="off">'
            f'<input class="campo" name="descricao" type="text" maxlength="{secoes.DESCRICAO_MAX}" aria-label="O que entra nesta seção" '
            f'placeholder="O que entra aqui (ajuda a IA a escolher)" value="{c.esc(sec.get("descricao"))}" autocomplete="off">'
            f'{c.botao("Salvar", tipo="submit")}'
            f'<button type="submit" class="btn destrutivo" form="fr-sa-{sid}" title="{c.esc(dica_apagar)}">'
            f'Apagar seção</button></form>'
            f'<form method="post" action="/frentes/secao/{sid}/apagar" id="fr-sa-{sid}" hidden>{_hidden_volta(volta)}</form></details>')


_PASTA = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
          'stroke-linejoin="round" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h4l2 2.5h8a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2H5'
          'a2 2 0 0 1-2-2z"/></svg>')


def html_pastas(secs: list[dict], por: dict, hoje: str, n_arquivos: dict = {}) -> str:
    """As seções da frente como pastas (pedido 79): cada linha abre a janela da seção. Mostra quantos itens em aberto
    há dentro e, se houver, quantos estão atrasados."""
    linhas = []
    for sec in secs:
        sid = int(sec["id"])
        itens = por.get(sid, [])
        atras = sum(1 for i in itens if i.get("prazo") and str(i["prazo"])[:10] < hoje)
        sinal = c.pilula(_pl(atras, "atrasado", "atrasados"), "atrasado") if atras else ""
        desc = f'<span class="desc">{c.esc(sec.get("descricao"))}</span>' if sec.get("descricao") else ""
        if n_arquivos.get(sid):
            sinal += f'<span class="desc" style="flex:none">{_pl(n_arquivos[sid], "arquivo", "arquivos")}</span>'
        linhas.append(f'<a class="fr-pasta" href="/frentes/secao/{sid}">{_PASTA}<span class="nome">{c.esc(sec.get("nome"))}</span>'
                      f'{desc}{sinal}<span class="n">{len(itens)}</span>{c.icone("chevron")}</a>')
    return f'<nav class="fr-pastas" aria-label="Seções desta frente">{"".join(linhas)}</nav>'


def html_secoes(abertos, feitos, hoje, frentes, volta, secs: list[dict], onde: dict, fid: int,
                n_arquivos: dict = {}) -> str:
    """Frente com seções: em cima, o que é do dia em todas as seções (pedido 82); depois as pastas; embaixo, só o
    que está sem seção e não é do dia, nos grupos de prazo de sempre (o do dia já está no bloco de cima)."""
    por, soltos = secoes.distribuir(abertos, secs, onde)
    ids = {int(x["id"]) for x in secs}
    feitos_soltos = [i for i in feitos if onde.get(i.get("id")) not in ids]
    soltos = [i for i in soltos if not _vence_ate_hoje(i, hoje)]
    resto = html_grupos(soltos, feitos_soltos, hoje, frentes, volta, secs=secs, vazio=())
    titulo = '<h3 class="fr-sem-secao">Sem seção</h3>' if resto else ""
    return html_do_dia(abertos, hoje, frentes, volta, secs, onde) + html_pastas(secs, por, hoje, n_arquivos) + titulo + resto


def _dia(iso) -> str | None:
    s = str(iso or "")[:10]
    return s if len(s) == 10 else None


def linha_do_tempo(abertos, feitos, secs, onde) -> list[dict]:
    """Pedido 86 (4): os itens da frente por seção, com o dia em que cada um abriu e, se já foi feito, o dia em que
    fechou. Só leitura. Seções na ordem da frente e "sem seção" por último; dentro de cada uma, o fato mais recente
    (abriu ou fechou) primeiro. O Frentes de verdade grava `concluido_em`; o embutido, `feito_em`."""
    nomes = {int(s["id"]): s.get("nome") for s in secs}
    grupos: dict = {}
    for it, fechado in [(i, False) for i in abertos] + [(i, True) for i in feitos]:
        sid = onde.get(it.get("id"))
        sid = int(sid) if sid is not None and int(sid) in nomes else None
        grupos.setdefault(sid, []).append({
            "id": it.get("id"), "texto": " ".join(str(it.get("texto") or "").split()),
            "abriu": _dia(it.get("criado_em")),
            "fechou": _dia(it.get("concluido_em") or it.get("feito_em")) if fechado else None})
    ordem = [int(s["id"]) for s in secs if int(s["id"]) in grupos] + ([None] if None in grupos else [])
    return [{"secao_id": sid, "secao": nomes.get(sid),
             "itens": sorted(grupos[sid], key=lambda x: (x["fechou"] or x["abriu"] or "", x["abriu"] or ""), reverse=True)}
            for sid in ordem]


def _dia_curto(iso: str | None, hoje: str) -> str:
    if not iso:
        return "?"
    return f"{iso[8:10]}/{iso[5:7]}" + ("" if iso[:4] == hoje[:4] else f"/{iso[2:4]}")


def html_linha_do_tempo(grupos: list[dict], hoje: str) -> str:
    """Bloco recolhido no fim da frente (pedido 86): nada aqui muda o item; marcar, mover e editar seguem nos grupos."""
    total = sum(len(g["itens"]) for g in grupos)
    if not total:
        return ""
    com_secao = any(g["secao_id"] is not None for g in grupos)
    partes = []
    for g in grupos:
        if com_secao:
            partes.append(f'<h4 class="fr-lt-sec">{c.esc(g["secao"] or "Sem seção")}</h4>')
        linhas = []
        for i in g["itens"]:
            quando = f'abriu {_dia_curto(i["abriu"], hoje)}' + (f' · fechou {_dia_curto(i["fechou"], hoje)}' if i["fechou"] else "")
            linhas.append(f'<li class="{"fechado" if i["fechou"] else "aberto"}"><span class="quando">{quando}</span>'
                          f'<span class="texto">{c.esc(i["texto"])}</span></li>')
        partes.append(f'<ol class="fr-lt">{"".join(linhas)}</ol>')
    return (f'<details class="fr-grupo fr-linha"><summary>{c.icone("chevron")}Linha do tempo<span class="n">{total}</span>'
            f'<span class="dica">quando cada item abriu e fechou</span></summary>{"".join(partes)}</details>')


def _form_captura(volta: str, frente_id=None, dica: str = "Fala aqui. Ex.: Casa, trocar o filtro da água sexta às 15h",
                  botao: str = "Jogar", secao_id=None) -> str:
    fid = f'<input type="hidden" name="frente_id" value="{int(frente_id)}">' if frente_id is not None else ""
    if secao_id is not None:
        fid += f'<input type="hidden" name="secao_id" value="{int(secao_id)}">'
    return (f'<form class="fr-captura" method="post" action="/frentes/captura">{_hidden_volta(volta)}{fid}'
            f'<input class="campo" name="texto" type="text" required maxlength="2000" aria-label="Jogar algo nas frentes" '
            f'placeholder="{c.esc(dica)}" autocomplete="off">{c.botao(botao, classe="primario", tipo="submit")}</form>')


TIPOS_NOVA = (("cliente", "Cliente"), ("projeto", "Projeto"), ("empresa", "Negócio"), ("contrato", "Contrato"),
              ("interno", "Interno"), ("pessoal", "Pessoal"))


def _form_nova_frente() -> str:
    """Criar frente pelo painel (pedido 47): sem isso, o Frentes embutido nascia vazio e sem como povoar."""
    opcoes = "".join(f'<option value="{v}">{c.esc(n)}</option>' for v, n in TIPOS_NOVA)
    return ('<form class="fr-nova" method="post" action="/frentes/nova">'
            '<input class="campo" name="nome" type="text" required maxlength="80" aria-label="Nome da nova frente" '
            'placeholder="Nova frente. Ex.: Padaria do Bairro, Casa, Site novo" autocomplete="off">'
            f'<select class="campo" name="tipo" aria-label="Tipo da frente">{opcoes}</select>'
            f'{c.botao("Criar frente", tipo="submit")}</form>')


def _erro(e: Exception, volta: str = "/frentes") -> str:
    return c.erro_inline(f"Frentes indisponível ({type(e).__name__}). Confira se o app Frentes está no ar.",
                         f'<a class="btn" href="{c.esc(volta)}">Tentar de novo</a>')


def _contexto_html(contexto: str) -> str:
    return (f'<details class="fr-contexto"><summary>{c.icone("chevron")}Contexto que o Claude recebe</summary>'
            f'<pre>{c.esc(contexto or "(indisponível)")}</pre></details>')


# ------------------------------------------------------------------ páginas

def frentes_get(req):
    if (e := c.metodo_errado(req, "GET", "HEAD")):
        return e
    ponte = _UmaVez(req.ctx.ponte)
    erro, est, contexto, pendentes = "", {}, "", []
    try:
        est = ponte.estado()
        contexto = contexto_do_ciclo(req.ctx.banco, ponte)  # o mesmo texto que o coletor entrega ao Claude
        pendentes = [i for i in ponte.itens_abertos(origem="whatsapp") if not i.get("confirmado", 1)]
    except VigiaErro as ex:
        log.warning("frentes indisponível: %s", causa(ex))
        erro = _erro(ex)
    tot = est.get("totais", {})
    frentes = sorted(est.get("frentes") or [], key=_ordem_grito)
    partes = [erro]
    if est:
        n_atr = int(tot.get("atrasados") or 0)
        n_conf = int(tot.get("confirmar", sum(f.get("n_confirmar") or 0 for f in frentes)) or 0)
        placar = c.kv([
            ("Frentes", f"<b>{c.esc(tot.get('frentes', len(frentes)))}</b>"),
            ("Em aberto", f"<b>{c.esc(tot.get('abertos', '–'))}</b>"),
            ("Atrasado", f'<b class="{"ruim" if n_atr else ""}">{n_atr}</b>'),
            ("A confirmar", f"<b>{n_conf}</b>"),
        ]).replace('<dl class="kv">', '<dl class="kv placar fr-kv">', 1)
        partes += [placar, _form_captura("/frentes")]
    if pendentes:
        linhas = "".join(
            f'<li class="fr-item"><span class="fr-tipo" role="img" aria-label="pendência">◌</span>'
            f'<span class="texto">{c.esc(i.get("texto"))}<span class="de">do WhatsApp</span></span>'
            f'<form method="post" action="/frentes/{int(i["id"])}/confirmar"><button type="submit" class="btn sm">Confirmar</button></form>'
            f'<span class="fr-id">#{int(i["id"])}</span></li>'
            for i in pendentes)
        partes.append(_grupo("confirmar", "Itens do WhatsApp a confirmar", linhas, len(pendentes)))
    if est:
        if frentes:
            tabela = ('<table class="tabela fr-frentes"><thead><tr><th>Frente</th><th>Tipo</th><th class="num">Em aberto</th>'
                      f'<th class="num">Feitos</th><th>Estado</th></tr></thead><tbody>{"".join(_linha_frente(f) for f in frentes)}</tbody></table>')
        else:
            tabela = c.vazio("Nenhuma frente ainda", "Crie a primeira aqui embaixo: um cliente, um projeto, a casa")
        partes += [c.rotulo("Frentes"), tabela, _form_nova_frente(), _contexto_html(contexto)]
    status = "" if not est else c.esc(f'{_pl(len(frentes), "frente", "frentes")}, {_pl(int(tot.get("abertos") or 0), "item em aberto", "itens em aberto")}')
    detalhe = c.painel(c.miolo("".join(partes), largo=True), toolbar_html=c.toolbar(c.titulo("Todas as frentes"), c.espaco()),
                       status_html=c.rodape(status), aria="Todas as frentes")
    toast, desfazer = _toast_arquivo(req)
    return c.pagina("Frentes", _CSS + html_lista(est, "/frentes", fora=not est, arquivadas=_arquivadas(req.ctx.banco)) + detalhe,
                    "/frentes", ctx=req.ctx, toast=toast, toast_desfazer=desfazer)


def _toast_arquivo(req) -> tuple[str, str]:
    """(texto, html do Desfazer) para ?arquivada=ID / ?desarquivada=ID depois do redirecionamento (D-28: toast, não modal)."""
    aid = c.query(req, "arquivada")
    if aid.isdigit():
        desfazer = (f'<form method="post" action="/frentes/{int(aid)}/desarquivar" class="toast-acao">'
                    f'<input type="hidden" name="volta" value="/frentes/{int(aid)}"><button type="submit">Desfazer</button></form>')
        return "Frente arquivada. Ela some das listas e da IA; volta em Arquivadas.", desfazer
    did = c.query(req, "desarquivada")
    if did.isdigit():
        return "Frente de volta às ativas.", ""
    an = c.query(req, "anotado")
    if an.isdigit():   # F1: depois de "jogar algo", diz em que frente caiu (0 = sem frente)
        nome = ""
        if int(an):
            try:
                nome = next((str(f.get("nome") or "") for f in req.ctx.ponte.frentes() if f.get("id") == int(an)), "")
            except VigiaErro:
                nome = ""
        return (f"Anotado em {nome}." if nome else ("Anotado no Frentes." if int(an) else "Anotado no Frentes, sem frente.")), ""
    return "", ""


def _pagina_arquivada(req, est: dict, arq: dict, arquivadas: list[dict]):
    """Detalhe de uma frente que o Greg arquivou: aviso + botão Desarquivar (o Frentes não devolve mais os itens dela)."""
    fid = int(arq["frente_id"])
    nome = str(arq.get("nome") or "Frente")
    volta = f"/frentes/{fid}"
    aviso = c.erro_inline(
        f"Frente arquivada em {c.esc(c.data_longa(str(arq.get('arquivada_em') or '')[:10]) or arq.get('arquivada_em') or '')}. "
        "Nem a lista nem a IA a usam; os itens continuam guardados no Frentes.",
        f'<form method="post" action="/frentes/{fid}/desarquivar"><input type="hidden" name="volta" value="{volta}">'
        f'<button type="submit" class="btn">Desarquivar</button></form>').replace('class="erro-inline"', 'class="erro-inline fr-arquivada-aviso"', 1)
    toolbar = c.toolbar(f'<div class="titulo-grupo">{ficha(nome, arq.get("tipo"), grande=True)}<b class="titulo">{c.esc(nome)}</b>'
                        f'<span class="sub">{c.esc(arq.get("tipo") or "")}, arquivada</span></div>', c.espaco())
    detalhe = c.painel(c.miolo(aviso, largo=True), toolbar_html=toolbar, status_html=c.rodape("Arquivada"), aria=nome)
    return c.pagina(nome, _CSS + html_lista(est, volta, arquivadas=arquivadas) + detalhe, "/frentes", ctx=req.ctx)


def _menu_frente(fid: int, volta: str) -> str:
    """Menu ··· do detalhe: Arquivar frente (ação direta + toast com Desfazer, D-28)."""
    itens = c.item_menu("Arquivar frente", attrs='form="f-arquivar"', destrutivo=True)
    form = (f'<form id="f-arquivar" method="post" action="/frentes/{fid}/arquivar" hidden>'
            f'<input type="hidden" name="volta" value="{c.esc(volta)}"></form>')
    return c.menu_icone("mais", "Mais ações", itens, id_="fr-menu") + form


def _detalhe(req, frente_id):
    """frente_id None = Inbox."""
    ponte = req.ctx.ponte
    volta = "/frentes/inbox" if frente_id is None else f"/frentes/{int(frente_id)}"
    arquivadas = _arquivadas(req.ctx.banco)
    try:
        est = ponte.estado()
        frentes = est.get("frentes") or []
        if frente_id is None:
            f = {"id": None, "nome": "Inbox", "tipo": "inbox", "apelidos": [], "prioridade": None,
                 "contexto": "O que chegou sem frente definida. Mova cada item para a frente certa."}
        else:
            f = next((x for x in frentes if x.get("id") == frente_id), None)
            if f is None:
                arq = next((a for a in arquivadas if int(a.get("frente_id", -1)) == int(frente_id)), None)
                if arq is not None:
                    return _pagina_arquivada(req, est, arq, arquivadas)
                nao = c.painel(c.vazio("Frente não encontrada", "Ela pode ter sido apagada no Frentes"),
                               toolbar_html=c.toolbar(c.botao_icone("voltar", "Todas as frentes", href="/frentes"), c.titulo("Frente"), c.espaco()),
                               status_html=c.rodape(""), aria="Frente não encontrada")
                return c.pagina("Frente não encontrada", _CSS + html_lista(est, arquivadas=arquivadas) + nao, "/frentes", 404, ctx=req.ctx)
        abertos = ponte.itens_da_frente(frente_id, "aberto")
        feitos = ponte.itens_da_frente(frente_id, "feito")
    except VigiaErro as ex:
        log.warning("frentes indisponível: %s", causa(ex))
        detalhe = c.painel(c.miolo(_erro(ex, volta)), toolbar_html=c.toolbar(c.titulo("Frentes"), c.espaco()),
                           status_html=c.rodape(""), aria="Frentes")
        return c.pagina("Frentes", _CSS + html_lista({}, volta, fora=True) + detalhe, "/frentes", ctx=req.ctx)
    hoje = str(est.get("hoje") or date.today().isoformat())
    prazos = sorted(str(i["prazo"])[:10] for i in abertos if i.get("prazo"))
    rot_prazo, cls_prazo = data_amigavel(prazos[0], hoje) if prazos else ("–", "")
    n_conf = sum(1 for i in abertos if not i.get("confirmado", 1))
    n_zap = sum(1 for i in abertos if i.get("origem") == "whatsapp")
    apelidos = ", ".join(f.get("apelidos") or [])
    sub = ", ".join(x for x in (c.esc(f.get("tipo") or ""), f"apelidos: {c.esc(apelidos)}" if apelidos else "") if x)
    props = c.kv([
        ("Prioridade", c.esc(PRIORIDADE.get(f.get("prioridade"), "–"))),
        ("Próximo prazo", prazo_html(rot_prazo, cls_prazo)),
        ("Em aberto", str(len(abertos))),
        ("A confirmar", str(n_conf)),
        ("Do WhatsApp", str(n_zap)),
    ]).replace('<dl class="kv">', '<dl class="kv fr-kv">', 1)
    contexto = f'<p class="meta-linha fr-nota">{c.esc(f.get("contexto"))}</p>' if f.get("contexto") else ""
    nome = str(f.get("nome") or "Frente")
    menu = _menu_frente(int(frente_id), volta) if frente_id is not None else ""
    toolbar = c.toolbar(f'<div class="titulo-grupo">{ficha(nome, f.get("tipo"), grande=True)}<b class="titulo">{c.esc(nome)}</b>'
                        f'<span class="sub">{sub}</span></div>', c.espaco(), menu)
    secs, onde = [], {}
    if frente_id is not None:
        try:
            secs, onde = req.ctx.banco.secoes(frente_id), req.ctx.banco.secao_dos_itens()
        except Exception as ex:  # noqa: BLE001  (sem as seções a frente abre como antes, tudo junto)
            log.warning("seções da frente indisponíveis: %s", causa(ex))
    nova_secao = _form_nova_secao(int(frente_id), volta) if frente_id is not None else ""
    n_arquivos = {}
    if secs:
        try:
            for a in req.ctx.banco.anexos():
                n_arquivos[a["secao_id"]] = n_arquivos.get(a["secao_id"], 0) + 1
        except Exception as ex:  # noqa: BLE001
            log.warning("contagem de arquivos indisponível: %s", causa(ex))
    lista = (html_secoes(abertos, feitos, hoje, frentes, volta, secs, onde, int(frente_id), n_arquivos) if secs
             else html_grupos(abertos, feitos, hoje, frentes, volta))
    dica_captura = ("Novo item sem seção. Ex.: mandar relatório sexta 15h" if secs
                    else "Novo item nesta frente. Ex.: mandar relatório sexta 15h")
    linha = html_linha_do_tempo(linha_do_tempo(abertos, feitos, secs, onde), hoje) if frente_id is not None else ""
    miolo = contexto + props + _form_captura(volta, frente_id, dica_captura, "Adicionar") + nova_secao + lista + linha
    status = c.esc(f'{_pl(len(abertos), "item em aberto", "itens em aberto")}, {_pl(len(feitos), "feito", "feitos")}'
                   + (f', {_pl(len(secs), "seção", "seções")}' if secs else ""))
    detalhe = c.painel(c.miolo(miolo, largo=True), toolbar_html=toolbar, status_html=c.rodape(status), aria=nome)
    toast, desfazer = _toast_arquivo(req)
    return c.pagina(nome, _CSS + html_lista(est, volta, arquivadas=arquivadas) + detalhe, "/frentes", ctx=req.ctx,
                    toast=toast, toast_desfazer=desfazer)


_ROTULO_DOC = {"application/pdf": "PDF"}


def _rotulo_doc(a: dict) -> str:
    if a.get("tipo") in _ROTULO_DOC:
        return _ROTULO_DOC[a["tipo"]]
    ext = str(a.get("nome") or "").rsplit(".", 1)[-1] if "." in str(a.get("nome") or "") else ""
    return ext.upper()[:5] if ext.isalnum() else "ARQUIVO"
_JS_ANEXAR = """<script>
(function () {
  var campo = document.getElementById('fr-arq-campo'), estado = document.getElementById('fr-arq-estado');
  if (!campo) return;
  var LIMITE = %d;
  function dizer(t, ruim) { estado.textContent = t; estado.className = 'fr-arq-estado' + (ruim ? ' ruim' : ''); }
  campo.addEventListener('change', function () {
    var fila = Array.prototype.slice.call(campo.files || []), total = fila.length, feitos = 0, falhas = [];
    if (!total) return;
    campo.disabled = true;
    (function proximo() {
      var arq = fila.shift();
      if (!arq) {
        if (falhas.length) { campo.disabled = false; campo.value = ''; dizer(falhas.join(' '), true); if (feitos) setTimeout(function () { location.reload(); }, 2500); }
        else location.reload();
        return;
      }
      dizer('Guardando ' + (feitos + falhas.length + 1) + ' de ' + total + '…');
      if (arq.size > LIMITE) { falhas.push(arq.name + ': passa de 100 MB.'); return proximo(); }
      fetch('/frentes/secao/anexar?secao=' + campo.dataset.secao + '&nome=' + encodeURIComponent(arq.name),
            {method: 'POST', headers: {'Content-Type': 'application/octet-stream'}, body: arq})
        .then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { return {ok: r.ok, j: j}; }); })
        .then(function (x) { if (x.ok && x.j.ok) feitos++; else falhas.push(arq.name + ': ' + (x.j.mensagem || 'não foi guardado.')); proximo(); })
        .catch(function () { falhas.push(arq.name + ': o Greg não respondeu.'); proximo(); });
    })();
  });
})();
</script>""" % anexos.LIMITE


def html_arquivos(sec: dict, fichas: list[dict], volta: str) -> str:
    """Grade "Arquivos" da janela da seção: imagem e vídeo com prévia, PDF como cartão; cada um com baixar e apagar."""
    sid = int(sec["id"])
    cartoes, luzes = [], []
    for a in fichas:
        aid, nome = int(a["id"]), c.esc(a.get("nome"))
        url = f"/frentes/anexo/{aid}"
        cls = anexos.classe(a.get("tipo") or "")
        if cls == "imagem":
            previa = f'<a class="previa" href="#ver-{aid}" aria-label="Abrir {nome}"><img src="{url}" alt="" loading="lazy"></a>'
            luzes.append(f'<div class="fr-luz" id="ver-{aid}"><a class="fechar" href="#arquivos" aria-label="Fechar"></a>'
                         f'<img src="{url}" alt="{nome}" loading="lazy"></div>')
        elif cls == "video":
            previa = f'<div class="previa"><video src="{url}" controls preload="metadata" aria-label="{nome}"></video></div>'
        else:
            previa = (f'<a class="previa" href="{url}?baixar=1" aria-label="Baixar {nome}">'
                      f'<b>{c.esc(_rotulo_doc(a))}</b></a>')
        cartoes.append(
            f'<div class="fr-cartao">{previa}<div class="pe"><span class="nome" title="{nome}'
            f'{" (veio de uma conversa)" if a.get("origem") == "ia" else ""}">{nome}</span>'
            f'<span class="tam">{c.esc(anexos.tamanho_humano(a.get("tamanho")))}</span>'
            f'<a class="btn" href="{url}?baixar=1" title="Baixar" aria-label="Baixar {nome}">↓</a>'
            f'<form method="post" action="{url}/apagar">{_hidden_volta(volta)}'
            f'<button type="submit" class="btn destrutivo" title="Apagar o arquivo (não tem volta)" aria-label="Apagar {nome}">×</button>'
            f'</form></div></div>')
    grade = (f'<div class="fr-grade">{"".join(cartoes)}</div>' if cartoes
             else '<p class="fr-arq-estado">Nenhum arquivo nesta seção. Qualquer arquivo, até 100 MB cada.</p>')
    return (f'<section class="fr-arquivos" id="arquivos"><h3>Arquivos<span class="n">{len(fichas)}</span>'
            f'<span class="fr-arq-estado" id="fr-arq-estado" role="status"></span><span class="esp"></span>'
            f'<label class="btn">Anexar arquivo<input id="fr-arq-campo" type="file" multiple hidden data-secao="{sid}">'
            f'</label></h3>'
            f'{grade}{"".join(luzes)}</section>{_JS_ANEXAR}')


def secao_get(req):
    """GET /frentes/secao/{id}: a janela da seção (pedido 79). Só os itens dela, nos grupos de prazo, com a caixa de
    novo item e o Editar seção. Volta para a frente pela trilha do topo."""
    if (e := c.metodo_errado(req, "GET", "HEAD")):
        return e
    sid = c.id_da_rota(req, r"/frentes/secao/(\d+)")
    sec = None if sid is None else req.ctx.banco.secao(sid)
    if sec is None:
        return c.texto_simples("seção inexistente", 404)
    fid, volta = int(sec["frente_id"]), f"/frentes/secao/{int(sec['id'])}"
    ponte = req.ctx.ponte
    arquivadas = _arquivadas(req.ctx.banco)
    try:
        est = ponte.estado()
        frentes = est.get("frentes") or []
        f = next((x for x in frentes if x.get("id") == fid), None)
        if f is None:
            return c.redirecionar(f"/frentes/{fid}")       # frente arquivada ou apagada: a página dela explica
        onde = req.ctx.banco.secao_dos_itens()
        abertos = [i for i in ponte.itens_da_frente(fid, "aberto") if onde.get(i.get("id")) == sec["id"]]
        feitos = [i for i in ponte.itens_da_frente(fid, "feito") if onde.get(i.get("id")) == sec["id"]]
    except VigiaErro as ex:
        log.warning("frentes indisponível: %s", causa(ex))
        detalhe = c.painel(c.miolo(_erro(ex, volta)), toolbar_html=c.toolbar(c.titulo("Frentes"), c.espaco()),
                           status_html=c.rodape(""), aria="Frentes")
        return c.pagina("Frentes", _CSS + html_lista({}, volta, fora=True) + detalhe, "/frentes", ctx=req.ctx)
    hoje = str(est.get("hoje") or date.today().isoformat())
    secs = req.ctx.banco.secoes(fid)
    nome, da_frente = str(sec.get("nome") or "Seção"), str(f.get("nome") or "Frente")
    trilha = (f'<nav class="fr-trilha" aria-label="Onde você está"><a href="/frentes/{fid}">{c.esc(da_frente)}</a>'
              f'<span aria-hidden="true">›</span><span>{c.esc(nome)}</span></nav>')
    descricao = f'<p class="meta-linha fr-nota">{c.esc(sec.get("descricao"))}</p>' if sec.get("descricao") else ""
    toolbar = c.toolbar(c.botao_icone("voltar", f"Voltar para {da_frente}", href=f"/frentes/{fid}"),
                        f'<div class="titulo-grupo"><b class="titulo">{c.esc(nome)}</b>'
                        f'<span class="sub">seção de {c.esc(da_frente)}</span></div>', c.espaco())
    try:
        # arquivo que a IA pediu e o coletor ainda não baixou: tenta agora, só os desta seção, sem segurar a página
        psql, evo = getattr(req.ctx, "psql", None), getattr(req.ctx, "evo", None)
        if psql is not None and evo is not None and any(p["secao_id"] == sec["id"] for p in req.ctx.banco.anexos_pendentes()):
            from app.paginas.conversas import desde_leitura
            desde = desde_leitura(req.ctx)
            if desde is not None:
                anexos.buscar_pendentes(req.ctx.banco, psql, evo, c.dados_dir(req.ctx), desde, limite=5)
        fichas = req.ctx.banco.anexos(sec["id"])
    except Exception as ex:  # noqa: BLE001  (sem a lista de arquivos a seção abre do mesmo jeito)
        log.warning("arquivos da seção indisponíveis: %s", causa(ex))
        fichas = []
    miolo = (trilha + descricao + _form_captura(volta, fid, f"Novo item em {nome}", "Adicionar", secao_id=sec["id"])
             + html_grupos(abertos, feitos, hoje, frentes, volta, secs=secs, na_secao=int(sec["id"]),
                           vazio=("Nada nesta seção ainda", "Jogue algo na caixa acima ou mova um item para cá"))
             + html_arquivos(sec, fichas, volta) + _editar_secao(sec, volta, len(fichas)))
    status = c.esc(f'{_pl(len(abertos), "item em aberto", "itens em aberto")}, {_pl(len(feitos), "feito", "feitos")}'
                   + (f', {_pl(len(fichas), "arquivo", "arquivos")}' if fichas else ""))
    detalhe = c.painel(c.miolo(miolo, largo=True), toolbar_html=toolbar, status_html=c.rodape(status), aria=nome)
    return c.pagina(f"{nome} · {da_frente}", _CSS + html_lista(est, f"/frentes/{fid}", arquivadas=arquivadas) + detalhe,
                    "/frentes", ctx=req.ctx)


def frente_get(req):
    if (e := c.metodo_errado(req, "GET", "HEAD")):
        return e
    fid = c.id_da_rota(req, r"/frentes/(\d+)")
    if fid is None:
        return c.texto_simples("frente inválida", 400)
    return _detalhe(req, fid)


def inbox_get(req):
    if (e := c.metodo_errado(req, "GET", "HEAD")):
        return e
    return _detalhe(req, None)


# ------------------------------------------------------------------ ações

def _volta(req) -> str:
    v = str((req.corpo or {}).get("volta") or "")
    return v if _VOLTA_OK.fullmatch(v) else "/frentes"


def _falha(req, volta: str, msg: str, texto: str = ""):
    # auditoria F1 (lote 5): o que a pessoa escreveu volta na página de falha, para copiar em vez de redigitar
    guardado = (f'<label class="meta" style="display:block;margin-top:12px">O que você escreveu (nada foi gravado):'
                f'<textarea class="campo" readonly rows="3" style="display:block;width:100%;margin-top:4px">{c.esc(texto)}</textarea></label>'
                if texto else "")
    detalhe = c.painel(c.miolo(c.erro_inline(msg, f'<a class="btn" href="{c.esc(volta)}">Voltar</a>') + guardado),
                       toolbar_html=c.toolbar(c.titulo("Frentes"), c.espaco()), status_html=c.rodape(""), aria="Frentes")
    return c.pagina("Frentes", _CSS + html_lista({}, volta, fora=True) + detalhe, "/frentes", 502, ctx=req.ctx)


def _pre(req):
    """Checagem comum das ações: POST e origem local."""
    if (e := c.metodo_errado(req, "POST")):
        return e
    if not c.origem_ok(req):
        return c.proibido()
    return None


def _espelhar_decisao(banco, item_id: int) -> None:
    """Espelha a decisão no vigia.db. Auditoria T6: procura pelo item, não nas 500 classificações mais recentes
    (item antigo ficava sem a decisão e nada avisava)."""
    ler = getattr(banco, "_ler", None)
    if callable(ler):
        ids = [r["id"] for r in ler("SELECT id FROM classificacoes WHERE item_id = ? AND (decisao IS NULL OR decisao = '')",
                                    (int(item_id),))]
    else:
        ids = [r["id"] for r in banco.classificacoes(limite=500) if r.get("item_id") == item_id and not r.get("decisao")]
    for i in ids:
        banco.decidir(i, "confirmado")


def captura_post(req):
    if (e := _pre(req)):
        return e
    volta = _volta(req)
    texto = " ".join(str((req.corpo or {}).get("texto") or "").split())
    if not texto:
        return c.redirecionar(volta)
    if len(texto) > 2000:
        return c.texto_simples("texto longo demais", 400)
    fid = str((req.corpo or {}).get("frente_id") or "")
    try:
        r = req.ctx.ponte.captura_livre(texto, frente_id=int(fid) if fid.isdigit() else None, origem="vigia")
    except VigiaErro as ex:
        log.warning("captura falhou: %s", causa(ex))
        return _falha(req, volta, "O Frentes não respondeu ou não aceitou o texto. Nada foi gravado.", texto)
    # F1: o toast diz onde caiu (o parser do Frentes pode escolher a frente pelo apelido no texto)
    itens = (r or {}).get("itens") if isinstance(r, dict) else None
    onde = itens[0].get("frente_id") if itens and isinstance(itens[0], dict) else (int(fid) if fid.isdigit() else None)
    sid = str((req.corpo or {}).get("secao_id") or "")
    if sid.isdigit() and itens and isinstance(itens[0], dict) and isinstance(itens[0].get("id"), int):
        # pedido 79: só entra na seção se caiu na frente dela (o texto pode ter levado o item para outra frente)
        try:
            alvo = secoes.valida_para(req.ctx.banco, int(sid), onde)
            if alvo is not None:
                req.ctx.banco.por_na_secao(itens[0]["id"], alvo)
        except Exception as ex:  # noqa: BLE001  (o item já foi anotado; só fica sem seção)
            log.warning("pôr o item na seção falhou: %s", causa(ex))
    return c.redirecionar(volta + ("&" if "?" in volta else "?") + "anotado=" + (str(int(onde)) if onde is not None else "0"))


def nova_frente_post(req):
    """POST /frentes/nova {nome, tipo}: cria a frente pela ponte (embutido ou externo) e abre a página dela.
    Nome vazio volta sem chamar; nome repetido ou Frentes fora = página de falha (502)."""
    if (e := _pre(req)):
        return e
    corpo = req.corpo or {}
    nome = " ".join(str(corpo.get("nome") or "").split())
    if not nome:
        return c.redirecionar("/frentes")
    if len(nome) > 80:
        return c.texto_simples("nome longo demais", 400)
    tipo = str(corpo.get("tipo") or "cliente")
    if tipo not in dict(TIPOS_NOVA):
        return c.texto_simples("tipo inválido", 400)
    try:
        r = req.ctx.ponte.criar_frente(nome, tipo=tipo)
    except VigiaErro as ex:
        log.warning("criar frente falhou: %s", causa(ex))
        return _falha(req, "/frentes", "O Frentes não criou a frente: o nome já existe ou ele está fora do ar.")
    fid = r.get("id")
    return c.redirecionar(f"/frentes/{int(fid)}" if isinstance(fid, int) else "/frentes")


def _acao_item(req, sufixo: str, campos_de):
    if (e := _pre(req)):
        return e
    volta = _volta(req)
    iid = c.id_da_rota(req, rf"/frentes/item/(\d+)/{sufixo}")
    if iid is None:
        return c.texto_simples("item inválido", 400)
    campos = campos_de(req)
    if campos is None:
        return c.redirecionar(volta)
    try:
        req.ctx.ponte.alterar(iid, **campos)
    except VigiaErro as ex:
        log.warning("ação %s falhou: %s", sufixo, causa(ex))
        return _falha(req, volta, "O Frentes recusou ou está fora do ar.")
    if campos.get("confirmado") == 1:
        _espelhar_decisao(req.ctx.banco, iid)
    return c.redirecionar(volta)


def _destino(req):
    v = str((req.corpo or {}).get("frente_id") or "").strip()
    if v == "inbox":
        return {"frente_id": None}
    return {"frente_id": int(v)} if v.isdigit() else None


def feito_post(req):
    return _acao_item(req, "feito", lambda r: {"status": "feito"})


def reabrir_post(req):
    return _acao_item(req, "reabrir", lambda r: {"status": "aberto"})


def confirmar_qualquer_post(req):
    return _acao_item(req, "confirmar", lambda r: {"confirmado": 1})


def mover_post(req):
    return _acao_item(req, "mover", _destino)


def _falha_secao(req, volta: str, msg: str):
    detalhe = c.painel(c.miolo(c.erro_inline(msg, f'<a class="btn" href="{c.esc(volta)}">Voltar</a>')),
                       toolbar_html=c.toolbar(c.titulo("Frentes"), c.espaco()), status_html=c.rodape(""), aria="Frentes")
    return c.pagina("Frentes", _CSS + html_lista({}, volta, fora=True) + detalhe, "/frentes", 400, ctx=req.ctx)


def secao_nova_post(req):
    """POST /frentes/{id}/secoes {nome, descricao}: cria a seção na frente (pedido 79). Nome repetido = página de falha."""
    if (e := _pre(req)):
        return e
    fid = c.id_da_rota(req, r"/frentes/(\d+)/secoes")
    if fid is None:
        return c.texto_simples("frente inválida", 400)
    volta = f"/frentes/{fid}"
    corpo = req.corpo or {}
    try:
        nome, descricao = secoes.limpar_nome(corpo.get("nome")), secoes.limpar_descricao(corpo.get("descricao"))
        if fid not in {f.get("id") for f in req.ctx.ponte.frentes()}:
            return c.texto_simples("frente não está entre as ativas", 404)
        req.ctx.banco.criar_secao(fid, nome, descricao)
    except ValueError as ex:
        return _falha_secao(req, volta, f"A seção não foi criada: {ex}.")
    except VigiaErro as ex:
        log.warning("criar seção falhou: %s", causa(ex))
        return _falha(req, volta, "O Frentes está fora do ar. A seção não foi criada.")
    return c.redirecionar(volta)


def _secao_da_rota(req, sufixo: str):
    sid = c.id_da_rota(req, rf"/frentes/secao/(\d+)/{sufixo}")
    return None if sid is None else req.ctx.banco.secao(sid)


def secao_alterar_post(req):
    """POST /frentes/secao/{id}/alterar {nome, descricao}: renomeia e troca a descrição que a IA lê."""
    if (e := _pre(req)):
        return e
    sec = _secao_da_rota(req, "alterar")
    if sec is None:
        return c.texto_simples("seção inexistente", 404)
    volta = _volta(req) if _volta(req) == f'/frentes/secao/{int(sec["id"])}' else f'/frentes/{int(sec["frente_id"])}'
    corpo = req.corpo or {}
    try:
        req.ctx.banco.alterar_secao(sec["id"], nome=secoes.limpar_nome(corpo.get("nome")),
                                    descricao=secoes.limpar_descricao(corpo.get("descricao")))
    except ValueError as ex:
        return _falha_secao(req, volta, f"A seção não foi alterada: {ex}.")
    return c.redirecionar(volta)


def secao_apagar_post(req):
    """POST /frentes/secao/{id}/apagar: apaga a seção; os itens dela voltam para "Sem seção" (nenhum item é apagado)."""
    if (e := _pre(req)):
        return e
    sec = _secao_da_rota(req, "apagar")
    if sec is None:
        return c.texto_simples("seção inexistente", 404)
    fichas = req.ctx.banco.anexos(sec["id"])
    if fichas and str((req.corpo or {}).get("confirmo") or "") != "1":
        # D-81: seção com arquivo só some depois de um segundo clique, porque aí o arquivo é apagado de verdade
        sid, volta = int(sec["id"]), f'/frentes/secao/{int(sec["id"])}'
        aviso = c.erro_inline(
            f'A seção "{sec["nome"]}" tem {_pl(len(fichas), "arquivo", "arquivos")}. Apagar a seção apaga '
            f'{"esse arquivo" if len(fichas) == 1 else "esses arquivos"} do Mac, sem volta. Os itens só voltam para Sem seção.',
            f'<form method="post" action="/frentes/secao/{sid}/apagar"><input type="hidden" name="confirmo" value="1">'
            f'<button type="submit" class="btn destrutivo">Apagar seção e arquivos</button></form>'
            f'<a class="btn" href="{volta}">Cancelar</a>')
        detalhe = c.painel(c.miolo(aviso), toolbar_html=c.toolbar(c.titulo("Apagar seção"), c.espaco()),
                           status_html=c.rodape(""), aria="Apagar seção")
        return c.pagina("Apagar seção", _CSS + html_lista({}, volta, fora=True) + detalhe, "/frentes", ctx=req.ctx)
    dados = c.dados_dir(req.ctx)
    for a in fichas:                       # o arquivo primeiro: se a ficha sumisse antes, o arquivo ficava órfão
        try:
            anexos.apagar(req.ctx.banco, dados, a)
        except OSError as ex:
            log.warning("arquivo da seção não apagado: %s", causa(ex))
    req.ctx.banco.apagar_secao(sec["id"])
    return c.redirecionar(f'/frentes/{int(sec["frente_id"])}')


def anexar_post(req):
    """POST /frentes/secao/anexar?secao=ID&nome=NOME com o arquivo cru no corpo (o servidor já o gravou em
    dados/anexos/.chegando/). Confere o tipo pelo conteúdo e guarda. Resposta em JSON: a tela envia um por vez."""
    corpo = req.corpo if isinstance(req.corpo, dict) else {}
    chegou = corpo.get("_arquivo")
    chegou = Path(str(chegou)) if chegou else None

    def recusa(status: int, msg: str):
        if chegou is not None:
            chegou.unlink(missing_ok=True)
        return json_resposta({"ok": False, "mensagem": msg}, status)
    if (e := _pre(req)):
        if chegou is not None:
            chegou.unlink(missing_ok=True)
        return e
    dados = c.dados_dir(req.ctx)
    try:
        anexos.varrer_chegando(dados)
    except OSError:
        pass
    sid = c.query(req, "secao")
    sec = req.ctx.banco.secao(int(sid)) if sid.isdigit() else None
    if sec is None:
        return recusa(404, "Essa seção não existe mais.")
    if chegou is None or anexos.pasta_chegando(dados).resolve() != chegou.resolve().parent or not chegou.is_file():
        return recusa(400, "O arquivo não chegou.")
    try:
        ficha = anexos.guardar(req.ctx.banco, dados, sec["id"], chegou, c.query(req, "nome"))
    except anexos.AnexoRecusado as ex:
        return recusa(415, str(ex))
    except OSError as ex:
        log.warning("guardar arquivo da seção falhou: %s", causa(ex))
        return recusa(507, "O Mac não conseguiu guardar o arquivo (disco cheio ou sem permissão).")
    return json_resposta({"ok": True, "id": ficha["id"], "nome": ficha["nome"]})


def anexo_get(req):
    """GET /frentes/anexo/{id}: imagem e vídeo abrem na página (com Range, lendo só o trecho pedido); PDF e qualquer
    pedido com ?baixar=1 saem como download. Nada aqui é interpretado como página na origem do painel."""
    if (e := c.metodo_errado(req, "GET", "HEAD")):
        return e
    aid = c.id_da_rota(req, r"/frentes/anexo/(\d+)")
    ficha = None if aid is None else req.ctx.banco.anexo(aid)
    arq = None if ficha is None else anexos.caminho(c.dados_dir(req.ctx), ficha)
    if arq is None:
        return c.texto_simples("não encontrado", 404)
    tipo = ficha["tipo"] if ficha["tipo"] in anexos.EXT else "application/octet-stream"
    baixar = anexos.classe(tipo) == "documento" or c.query(req, "baixar") == "1"
    status, corpo, cab = anexos.fatia(arq, "" if baixar else (req.cabecalhos or {}).get("range"))
    cab = list(cab) + [("X-Content-Type-Options", "nosniff"), ("Cache-Control", "private, max-age=3600")]
    if baixar:
        cab.append(("Content-Disposition", "attachment; filename*=UTF-8''" + quote(anexos.nome_seguro(ficha["nome"], tipo), safe="")))
    return Resposta(status, corpo, tipo, cab)


def anexo_apagar_post(req):
    """POST /frentes/anexo/{id}/apagar: apaga o arquivo do Mac (sem lixeira) e volta para a seção."""
    if (e := _pre(req)):
        return e
    aid = c.id_da_rota(req, r"/frentes/anexo/(\d+)/apagar")
    ficha = None if aid is None else req.ctx.banco.anexo(aid)
    if ficha is None:
        return c.texto_simples("arquivo inexistente", 404)
    anexos.apagar(req.ctx.banco, c.dados_dir(req.ctx), ficha)
    return c.redirecionar(f'/frentes/secao/{int(ficha["secao_id"])}#arquivos')


def item_secao_post(req):
    """POST /frentes/item/{id}/secao {secao_id}: põe o item na seção; "0" ou vazio tira de qualquer seção. A seção tem
    de ser da frente em que o item está (404 se o item não está aberto, 400 se a seção é de outra frente)."""
    if (e := _pre(req)):
        return e
    iid = c.id_da_rota(req, r"/frentes/item/(\d+)/secao")
    if iid is None:
        return c.texto_simples("item inválido", 400)
    volta = _volta(req)
    v = str((req.corpo or {}).get("secao_id") or "").strip()
    if v in ("", "0"):
        req.ctx.banco.por_na_secao(iid, None)
        return c.redirecionar(volta)
    if not v.isdigit():
        return c.texto_simples("seção inválida", 400)
    sec = req.ctx.banco.secao(int(v))
    if sec is None:
        return c.texto_simples("seção inexistente", 404)
    try:
        item = next((i for i in req.ctx.ponte.itens_da_frente(sec["frente_id"], "aberto") if i.get("id") == iid), None)
    except VigiaErro as ex:
        log.warning("mover para a seção falhou: %s", causa(ex))
        return _falha(req, volta, "O Frentes está fora do ar. O item não mudou de seção.")
    if item is None:
        return c.texto_simples("o item não está aberto na frente desta seção", 400)
    req.ctx.banco.por_na_secao(iid, sec["id"])
    return c.redirecionar(volta)


def confirmar_post(req):
    """Confirmação restrita a item do WhatsApp (contrato F3): item de outra origem responde 404 aqui."""
    if (e := _pre(req)):
        return e
    item_id = c.id_da_rota(req, r"/frentes/(\d+)/confirmar")
    if item_id is None:
        return c.texto_simples("item inválido", 400)
    try:
        abertos = {int(i.get("id", -1)) for i in req.ctx.ponte.itens_abertos(origem="whatsapp")}
        if item_id not in abertos:
            return c.texto_simples("item não é um item aberto do WhatsApp no Frentes", 404)
        confirmar_item(req.ctx.ponte, item_id)
    except VigiaErro as ex:
        log.warning("confirmar item falhou: %s", causa(ex))
        return c.texto_simples("Frentes recusou ou está indisponível", 502)
    _espelhar_decisao(req.ctx.banco, item_id)
    return c.redirecionar("/frentes")


def arquivar_post(req):
    """Arquiva a frente no Frentes (status arquivada) e registra em vigia.db para listar/desarquivar. Pedido 41.
    Volta para /frentes?arquivada=ID (toast com Desfazer). 404 se a frente não está entre as ativas; 502 Frentes fora."""
    if (e := _pre(req)):
        return e
    fid = c.id_da_rota(req, r"/frentes/(\d+)/arquivar")
    if fid is None:
        return c.texto_simples("frente inválida", 400)
    try:
        f = next((x for x in req.ctx.ponte.frentes() if x.get("id") == fid), None)
        if f is None:
            return c.texto_simples("frente não está entre as ativas", 404)
        req.ctx.ponte.alterar_frente(fid, status="arquivada")
    except VigiaErro as ex:
        log.warning("arquivar frente falhou: %s", causa(ex))
        return _falha(req, f"/frentes/{fid}", "O Frentes recusou ou está fora do ar. Nada foi arquivado.")
    try:
        req.ctx.banco.arquivar_frente(fid, f.get("nome") or f"frente {fid}", f.get("tipo") or "")
    except Exception as ex:  # noqa: BLE001  (o Frentes já arquivou; sem o registro local só falta o atalho de volta)
        log.warning("registro local da frente arquivada falhou: %s", causa(ex))
    return c.redirecionar(f"/frentes?arquivada={fid}")


def desarquivar_post(req):
    """status ativa no Frentes + apaga o registro local. Volta para `volta` (padrão /frentes: é de onde vêm o Desfazer
    do toast e o grupo Arquivadas; a página da frente arquivada manda a própria URL) com ?desarquivada=ID."""
    if (e := _pre(req)):
        return e
    fid = c.id_da_rota(req, r"/frentes/(\d+)/desarquivar")
    if fid is None:
        return c.texto_simples("frente inválida", 400)
    volta = _volta(req)
    try:
        req.ctx.ponte.alterar_frente(fid, status="ativa")
    except VigiaErro as ex:
        log.warning("desarquivar frente falhou: %s", causa(ex))
        return _falha(req, "/frentes", "O Frentes recusou ou está fora do ar. A frente continua arquivada.")
    try:
        req.ctx.banco.desarquivar_frente(fid)
    except Exception as ex:  # noqa: BLE001
        log.warning("registro local da frente desarquivada falhou: %s", causa(ex))
    sep = "&" if "?" in volta else "?"
    return c.redirecionar(f"{volta}{sep}desarquivada={fid}")


ROTAS = {
    "/frentes": frentes_get,
    "/frentes/inbox": inbox_get,
    "/frentes/captura": captura_post,
    "/frentes/nova": nova_frente_post,
    "/frentes/{id}": frente_get,
    "/frentes/{id}/arquivar": arquivar_post,
    "/frentes/{id}/desarquivar": desarquivar_post,
    "/frentes/{id}/confirmar": confirmar_post,
    "/frentes/item/{id}/feito": feito_post,
    "/frentes/item/{id}/reabrir": reabrir_post,
    "/frentes/item/{id}/confirmar": confirmar_qualquer_post,
    "/frentes/item/{id}/mover": mover_post,
    "/frentes/item/{id}/secao": item_secao_post,
    "/frentes/{id}/secoes": secao_nova_post,
    "/frentes/secao/anexar": anexar_post,
    "/frentes/anexo/{id}": anexo_get,
    "/frentes/anexo/{id}/apagar": anexo_apagar_post,
    "/frentes/secao/{id}": secao_get,
    "/frentes/secao/{id}/alterar": secao_alterar_post,
    "/frentes/secao/{id}/apagar": secao_apagar_post,
}
