"""Ajudantes das páginas do painel (estilo Papel, pedido 39): a casca da janela (lateral + painéis), toolbar de 52 px,
rodapé de status de 26 px, botões, menus pull-down e pop-up, escape, query, datas, templates string.Template e checagem
de origem.
Quem chama: app/paginas/*.py.
O que não faz: não define ROTAS (o servidor ignora módulos iniciados por "_"), não autentica, não loga texto de conversa.
"""
import html as _html
import json
import logging
import re
import string
import urllib.parse
from datetime import date, datetime, time as hora_t
from pathlib import Path

from app import alertas, inicio, layout, preferencias
from app.http import Resposta, html as html_resposta
from app.erros import causa

log = logging.getLogger("vigia.painel")
TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
# Seis destinos: os cinco do guia Papel (08/11/2026) + Histórico (pedido 46, D-58), no fim para não mudar ⌘1–⌘5.
# Conexão virou o indicador no rodapé da lateral. Ajustes fica na engrenagem ao lado dele (pedido 40: só ⌘, e o menu do
# app não eram achados), em ⌘, e no menu do app. Enviar continua só como POST.
NAV = (("/demandas", "Demandas"), ("/conversas", "Conversas"), ("/frentes", "Frentes"), ("/envios", "Envios"),
       ("/contatos", "Contatos"), ("/historico", "Histórico"))
_SVG = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="{w}" stroke-linecap="round" '
        'stroke-linejoin="round" aria-hidden="true">{p}</svg>')
# Traço no espírito dos SF Symbols (tray.full, bubble.left, list.bullet, paperplane, person.2); SVG inline porque a
# CSP é default-src 'none'.
ICONES = {
    "/demandas": _SVG.format(w="1.6", p='<path d="M3 13h5l2 3h4l2-3h5"/><path d="M5 6h14l2 7v6a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-6z"/>'),
    "/conversas": _SVG.format(w="1.6", p='<path d="M12 4c-5 0-9 3.1-9 7 0 2 1 3.8 2.7 5.1L5 20l4-1.6c1 .3 1.9.4 3 .4 5 0 9-3.1 9-7s-4-7.8-9-7.8z"/>'),
    "/frentes": _SVG.format(w="1.6", p='<path d="M4 7h16M4 12h16M4 17h10"/>'),
    "/envios": _SVG.format(w="1.6", p='<path d="M21 3L3 10.5l7.5 2L13 20z"/><path d="M21 3l-10.5 9.5"/>'),
    # relógio com seta de volta (clock.arrow.circlepath): o que já aconteceu
    "/historico": _SVG.format(w="1.6", p='<path d="M3.5 12a8.5 8.5 0 1 0 2.6-6.1"/><path d="M3.5 4.5v4.2h4.2"/><path d="M12 7.5V12l3 1.8"/>'),
    "/contatos": _SVG.format(w="1.6", p='<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.5-3.5 3-5.5 6.5-5.5s6 2 6.5 5.5"/><path d="M16 5a3 3 0 0 1 0 6"/><path d="M18 14.5c2 .6 3.3 2.3 3.5 5.5"/>'),
}
# ícones de controle (toolbar, menus, avatares)
_ICONE = {
    "chevron": _SVG.format(w="2.5", p='<path d="M6 9l6 6 6-6"/>'),
    "popup": '<svg viewBox="0 0 10 10" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 4l2-2 2 2M3 6l2 2 2-2"/></svg>',
    "lupa": _SVG.format(w="2.2", p='<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>'),
    "mais": '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="19" cy="12" r="2"/></svg>',
    "voltar": _SVG.format(w="2.2", p='<path d="M15 18l-6-6 6-6"/>'),
    "nova": _SVG.format(w="1.8", p='<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>'),
    "pessoa": _SVG.format(w="1.8", p='<circle cx="12" cy="8.5" r="4"/><path d="M4 20.5c.6-4 3.7-6.5 8-6.5s7.4 2.5 8 6.5"/>'),
    "grupo": _SVG.format(w="1.8", p='<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.5-3.5 3-5.5 6.5-5.5s6 2 6.5 5.5"/><path d="M16 5a3 3 0 0 1 0 6"/><path d="M18 14.5c2 .6 3.3 2.3 3.5 5.5"/>'),
    "touchid": _SVG.format(w="2", p='<path d="M12 11c-1.5 0-2 1.5-2 3s.5 4 2 6"/><path d="M8 7a4 4 0 0 1 8 0v2"/><path d="M5 11a7 7 0 0 1 14 0c0 3-1 6-2 8"/><path d="M12 14v3"/>'),
    "mais-simples": _SVG.format(w="2.2", p='<path d="M12 5v14M5 12h14"/>'),
    "check": _SVG.format(w="2.4", p='<path d="M5 12l4 4L19 6"/>'),
    "relogio": _SVG.format(w="2", p='<circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/>'),
    "tocar": '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M7 5v14l12-7z"/></svg>',
    "enviar": _SVG.format(w="2", p='<path d="M21 3L3 10.5l7.5 2L13 20z"/><path d="M21 3l-10.5 9.5"/>'),
    "fechar": _SVG.format(w="2.2", p='<path d="M6 6l12 12M18 6L6 18"/>'),
    "aba": _SVG.format(w="1.8", p='<path d="M4 7h16M4 12h16M4 17h10"/>'),
    # olho riscado (eye-off): modo de gravação no rodapé da lateral (pedido 42)
    "olho-riscado": _SVG.format(w="1.7", p='<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><path d="M1 1l22 22"/>'),
    # engrenagem (gearshape): Ajustes no rodapé da lateral
    "engrenagem": _SVG.format(w="1.7", p='<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>'),
}
_BASE_RESERVA = ("<!doctype html><html lang=\"pt-BR\"><head><meta charset=\"utf-8\"><title>$titulo</title></head>"
                 "<body><nav>$nav</nav>$conteudo</body></html>")
_MESES = ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro",
          "novembro", "dezembro")

# rótulo das classes da IA na tela (a chave no banco fica sem acento). "lembrando" é estado de "lembrar" (guia Papel #35).
ROTULO_CLASSE = {"atencao": "atenção", "sem_acao": "sem ação", "lembrando": "lembrar, segunda cobrança"}


def rotulo_classe(classe) -> str:
    """Texto da classe na tela, já escapado."""
    return esc(ROTULO_CLASSE.get(classe, classe))


def esc(valor) -> str:
    """Escapa qualquer dado vindo de mensagem, Frentes ou Evolution para HTML (aspas incluídas)."""
    return _html.escape("" if valor is None else str(valor), quote=True)


def icone(nome: str) -> str:
    return _ICONE.get(nome, "")


def query(req, chave: str, padrao: str = "") -> str:
    """Valor de querystring como texto aparado (aceita str ou lista, como o servidor entregar)."""
    v = (req.query or {}).get(chave, padrao)
    if isinstance(v, (list, tuple)):
        v = v[0] if v else padrao
    return str(v).strip()


def hora(ts) -> str:
    """Epoch em segundos -> 'dd/mm HH:MM' local; inválido -> '-'."""
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%d/%m %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return "-"


def hora_curta(ts) -> str:
    """Epoch -> 'HH:MM' se for hoje, senão 'dd/mm' (lista de itens); inválido -> '-'."""
    try:
        d = datetime.fromtimestamp(int(ts))
    except (TypeError, ValueError, OSError, OverflowError):
        return "-"
    return d.strftime("%H:%M") if d.date() == date.today() else d.strftime("%d/%m")


def quando_longo(ts) -> str:
    """Epoch -> 'Hoje às 16:23' · 'Ontem às 9:10' · '3 de novembro às 14:02' (meta do detalhe)."""
    try:
        d = datetime.fromtimestamp(int(ts))
    except (TypeError, ValueError, OSError, OverflowError):
        return "-"
    hoje = date.today()
    hh = f"{d.hour}:{d.minute:02d}"
    if d.date() == hoje:
        return f"Hoje às {hh}"
    if (hoje - d.date()).days == 1:
        return f"Ontem às {hh}"
    return f"{d.day} de {_MESES[d.month - 1]} às {hh}"


def data_longa(iso) -> str:
    """'2026-11-20' -> '20 de novembro' (ano só quando não é o corrente); inválida -> texto como veio, escapado."""
    s = str(iso or "")[:10]
    try:
        d = date.fromisoformat(s)
    except ValueError:
        return esc(iso)
    sufixo = "" if d.year == date.today().year else f" de {d.year}"
    return f"{d.day} de {_MESES[d.month - 1]}{sufixo}"


def duracao(segundos) -> str:
    """Segundos -> '2 min 40' · '45 s' · '1 h 05' (medição da triagem)."""
    try:
        s = max(0, int(segundos))
    except (TypeError, ValueError):
        return "-"
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60:02d}"
    return f"{s // 3600} h {(s % 3600) // 60:02d}"


def dados_dir(ctx) -> Path:
    """Pasta dados/ (config 'dados_dir', padrão 'dados' relativo ao diretório do servidor)."""
    cfg = getattr(ctx, "config", None) or {}
    return Path(cfg.get("dados_dir", "dados"))


def carregar_template(nome: str) -> string.Template:
    return string.Template((TEMPLATES / nome).read_text(encoding="utf-8"))


# ------------------------------------------------------------------ casca da janela (Papel)

def toolbar(*itens: str, classe: str = "") -> str:
    """Toolbar de 52 px de um painel: título à esquerda, `espaco()` empurra o resto para a direita."""
    cls = f' class="toolbar {esc(classe)}"' if classe else ' class="toolbar"'
    return f"<header{cls}>{''.join(itens)}</header>"


def titulo(texto: str) -> str:
    return f'<b class="titulo">{esc(texto)}</b>'


def espaco() -> str:
    return '<span class="espaco"></span>'


def rodape(texto_html: str, id_: str = "") -> str:
    """Rodapé de status de 26 px: contagem, próxima leitura ou um aviso de uma linha (HTML já escapado)."""
    i = f' id="{esc(id_)}"' if id_ else ""
    return f'<div class="status"{i} role="status">{texto_html}</div>'


def painel(corpo: str, *, toolbar_html: str = "", status_html: str = "", classe: str = "detalhe", aria: str = "",
           id_: str = "", corpo_classe: str = "", corpo_id: str = "", corpo_attrs: str = "") -> str:
    """Um painel: coluna de cima a baixo com toolbar, corpo rolável e rodapé de status. `classe`: "lista" (380 px),
    "lista estreita" (300 px, Conversas) ou "detalhe" (o resto da janela)."""
    a = f' aria-label="{esc(aria)}"' if aria else ""
    i = f' id="{esc(id_)}"' if id_ else ""
    cc = f' class="corpo {esc(corpo_classe)}"' if corpo_classe else ' class="corpo"'
    ci = f' id="{esc(corpo_id)}"' if corpo_id else ""
    ca = f" {corpo_attrs}" if corpo_attrs else ""
    return f'<section class="painel {esc(classe)}"{a}{i}>{toolbar_html}<div{cc}{ci}{ca}>{corpo}</div>{status_html}</section>'


def miolo(conteudo: str, largo: bool = False) -> str:
    """Área de detalhe com o padding do guia (20 24) e largura máxima 640 px (`largo`: sem teto, para tabelas)."""
    return f'<div class="miolo{" largo" if largo else ""}">{conteudo}</div>'


def botao(rotulo: str, *, classe: str = "", tipo: str = "button", attrs: str = "", icone_html: str = "") -> str:
    cls = f'btn {classe}'.strip()
    return f'<button type="{tipo}" class="{cls}"{(" " + attrs) if attrs else ""}>{icone_html}{esc(rotulo)}</button>'


def botao_icone(nome: str, rotulo: str, *, attrs: str = "", href: str = "") -> str:
    """Ícone de toolbar sem moldura (fundo só no hover). Com `href` vira link."""
    if href:
        return f'<a class="btn icone" href="{esc(href)}" aria-label="{esc(rotulo)}" title="{esc(rotulo)}"{(" " + attrs) if attrs else ""}>{icone(nome)}</a>'
    return f'<button type="button" class="btn icone" aria-label="{esc(rotulo)}" title="{esc(rotulo)}"{(" " + attrs) if attrs else ""}>{icone(nome)}</button>'


def pulldown(rotulo_html: str, itens_html: str, *, id_: str = "", direita: bool = False, classe: str = "",
             aria: str = "") -> str:
    """Pull-down (abre ações ou filtros): botão com chevron cinza; os itens (`<a>`/`<button>`/`<hr>`) caem num menu.
    Feito com <details>: funciona sem JS; o JS da base fecha ao clicar fora e ao abrir outro."""
    i = f' id="{esc(id_)}"' if id_ else ""
    a = f' aria-label="{esc(aria)}"' if aria else ""
    return (f'<details class="menu {esc(classe)}"{i}><summary class="btn pulldown" aria-haspopup="menu">{rotulo_html}'
            f'<span class="chevron">{icone("chevron")}</span></summary>'
            f'<div class="menu-itens{" direita" if direita else ""}" role="menu"{a}>{itens_html}</div></details>')


def menu_icone(nome: str, rotulo: str, itens_html: str, *, id_: str = "") -> str:
    """Menu `···` (ou outro ícone) da toolbar."""
    i = f' id="{esc(id_)}"' if id_ else ""
    return (f'<details class="menu"{i}><summary class="btn icone" aria-haspopup="menu" aria-label="{esc(rotulo)}" '
            f'title="{esc(rotulo)}">{icone(nome)}</summary>'
            f'<div class="menu-itens direita" role="menu" aria-label="{esc(rotulo)}">{itens_html}</div></details>')


def popup(rotulo: str, valor: str, itens_html: str, *, id_: str = "", titulo_: str = "") -> str:
    """Pop-up (escolhe um valor): rótulo cinza + valor + caixa azul com ⌃⌄ (guia Papel)."""
    i = f' id="{esc(id_)}"' if id_ else ""
    t = f' title="{esc(titulo_)}"' if titulo_ else ""
    return (f'<details class="menu popup"{i}><summary class="btn" aria-haspopup="listbox"{t}>'
            f'<span class="valor-rotulo">{esc(rotulo)}</span><span class="valor">{esc(valor)}</span>'
            f'<span class="caixa">{icone("popup")}</span></summary>'
            f'<div class="menu-itens" role="listbox" aria-label="{esc(rotulo)}">{itens_html}</div></details>')


def item_menu(rotulo: str, *, href: str = "", attrs: str = "", atalho: str = "", marcado: bool = False,
              destrutivo: bool = False) -> str:
    """Item de menu: link (`href`) ou botão (`attrs` com form=/name=/value=). Atalho só aqui e em tooltip."""
    cls = " ".join(x for x in ("on" if marcado else "", "destrutivo" if destrutivo else "") if x)
    c = f' class="{cls}"' if cls else ""
    k = f"<kbd>{esc(atalho)}</kbd>" if atalho else ""
    conteudo = f"<span>{esc(rotulo)}</span>{k}"
    if href:
        return f'<a role="menuitem" href="{esc(href)}"{c}{(" " + attrs) if attrs else ""}>{conteudo}</a>'
    return f'<button type="submit" role="menuitem"{c}{(" " + attrs) if attrs else ""}>{conteudo}</button>'


def separador_menu() -> str:
    return "<hr>"


def busca(nome: str, valor: str = "", *, placeholder: str = "Buscar", id_: str = "busca", largura: str = "200px",
          attrs: str = "") -> str:
    """Campo de busca com a lupa dentro, à esquerda (na toolbar do detalhe: 200 px)."""
    return (f'<span class="busca-caixa">{icone("lupa")}<label for="{esc(id_)}" class="sr-only">{esc(placeholder)}</label>'
            f'<input id="{esc(id_)}" class="campo busca" type="search" name="{esc(nome)}" value="{esc(valor)}" '
            f'placeholder="{esc(placeholder)}" autocomplete="off" style="width:{esc(largura)}"{(" " + attrs) if attrs else ""}></span>')


def classe_html(classe: str, texto: str = "") -> str:
    """Ponto de 6 px + palavra cinza. Toda classe aparece assim; nunca pílula colorida."""
    base = "lembrar" if classe == "lembrando" else classe
    return f'<span class="classe {esc(base)}">{texto or rotulo_classe(classe)}</span>'


def pilula(texto: str, tipo: str = "") -> str:
    """Âmbar = pede você (Histórico: "aguarda você"); "atrasado" = vermelho; "neutra" (cinza) = estado do cartão."""
    return f'<span class="pilula{(" " + esc(tipo)) if tipo else ""}">{esc(texto)}</span>'


def avatar(grupo: bool = False, classe: str = "", extra: str = "") -> str:
    """Silhueta cinza (pessoa) ou duas pessoas (grupo). Sem foto: a CSP não carrega imagem de fora e nada sai do Mac."""
    return f'<span class="avatar{(" " + esc(classe)) if classe else ""}" aria-hidden="true"{extra}>{icone("grupo" if grupo else "pessoa")}</span>'


def vazio(linha1: str, linha2: str = "") -> str:
    """Estado vazio: duas linhas de texto cinza centralizadas. Sem ícone, sem botão."""
    return f'<div class="vazio"><b>{esc(linha1)}</b>{f"<span>{esc(linha2)}</span>" if linha2 else ""}</div>'


def esqueleto(linhas: int = 4) -> str:
    """Carregando: esqueleto do próprio layout (barras cinza). Nunca spinner."""
    um = '<div class="esq-linha"><i style="width:38%"></i><i style="width:82%"></i></div>'
    return f'<div class="esqueleto" aria-hidden="true">{um * max(1, int(linhas))}</div>'


def erro_inline(mensagem: str, acao_html: str = "") -> str:
    """Erro de ação, inline, no lugar da ação, com "Tentar de novo" (a ação não se perde)."""
    return f'<div class="erro-inline" role="alert"><span>{esc(mensagem)}</span>{acao_html}</div>'


def aviso_leitura(ctx) -> str:
    """Pedido 62, agora pela central (pedido 63, lote 2): a leitura pela IA está parada por um motivo que só a pessoa
    resolve (login do Claude vencido, limite da conta, Claude ausente). Devolve a frase com o que fazer, ou ''.
    As telas não mostram mais este texto inline (a faixa de avisos mostra em todas); quem chama usa como sinal."""
    try:
        for a in alertas.abertos(dados_dir(ctx)):
            if a["codigo"] in ("ia-login", "ia-limite", "ia-ausente"):
                return f'{a["titulo"]}: {a["por_que"]} {" ".join(a["passos"])}'
    except Exception as e:  # noqa: BLE001
        log.debug("aviso de leitura indisponível: %s", causa(e))
    return ""


def faixa_avisos(ctx) -> str:
    """Miolo da faixa que aparece em TODAS as telas enquanto houver aviso aberto na central (pedido 63, lote 2): o
    mais antigo, quantos mais há e o link para a tela Saúde do Greg, que tem o passo a passo. '' = nada aberto."""
    if ctx is None:
        return ""
    try:
        ab = alertas.abertos(dados_dir(ctx))
    except Exception as e:  # noqa: BLE001  (a faixa nunca derruba a página)
        log.debug("faixa de avisos indisponível: %s", causa(e))
        return ""
    try:
        from app import integracao
        ped = len(integracao.pedidos(ctx).pendentes())
    except Exception as e:  # noqa: BLE001
        log.debug("pedidos do Claude indisponíveis para a faixa: %s", causa(e))
        ped = 0
    pedido = (f'<span class="mais">{ped} pedido{"s" if ped > 1 else ""} do Claude Code esperando</span>'
              '<a href="/ajustes#claude">Ver o pedido</a>') if ped else ""
    if not ab:
        if not ped:
            return ""
        return ('<b>O Claude Code pediu o seu aval para um envio</b>'
                '<span class="porque">Só sai se você permitir. O pedido vence em 30 minutos.</span>'
                '<a href="/ajustes#claude">Ver o pedido</a>')
    a, n = ab[0], len(ab) - 1
    mais = (f'<span class="mais">e mais {n} aviso{"s" if n > 1 else ""}</span>' if n else "") + pedido
    return (f'<b>{esc(a["titulo"])}</b><span class="porque">{esc(a["por_que"])}</span>{mais}'
            f'<a href="/avisos">O que fazer</a>')


def rotulo(texto: str) -> str:
    """Rótulo de seção 11/700 em caixa normal."""
    return f'<p class="rotulo">{esc(texto)}</p>'


def kv(pares) -> str:
    """Lista rótulo/valor (dl em grade): é para cá que vão os números que eram stat tiles. Valores já escapados."""
    return '<dl class="kv">' + "".join(f"<dt>{esc(k)}</dt><dd>{v}</dd>" for k, v in pares) + "</dl>"


def toast_html(texto: str, desfazer_html: str = "") -> str:
    """Toast que o servidor manda pronto (depois de um redirecionamento); o JS da base esconde em 8 s."""
    return f'<div class="toast" role="status" data-toast><span>{esc(texto)}</span>{desfazer_html}</div>'


# ------------------------------------------------------------------ lateral: contagem e indicador de conexão

def _n_demandas(banco) -> str:
    meia_noite = int(datetime.combine(date.today(), hora_t.min).timestamp())
    n = len(banco.classificacoes(desde=meia_noite, excluir_classe="sem_acao", limite=500))
    return str(n) if n else ""


def estado_conexao(ctx) -> tuple[str, str]:
    """(classe, texto) do indicador: lido do estado da máquina de QR, do último ciclo do monitor (conexao.json) e do
    último lote; nunca chama a Evolution (isso é da página Conexão). Falha vira indicador neutro, não 500."""
    cx = getattr(ctx, "conexao", None)
    banco = getattr(ctx, "banco", None)
    estado = str(getattr(cx, "estado", "") or "")
    monitor = {}
    try:
        monitor = (cx.ultimo_monitor() if cx is not None and hasattr(cx, "ultimo_monitor") else {}) or {}
    except Exception as e:  # noqa: BLE001  (indicador nunca derruba a página)
        log.debug("monitor ilegível: %s", causa(e))
    ultimo = monitor.get("ultimo_estado")
    if estado in ("gerando", "renovando"):
        return "alerta", "Aguardando o QR"
    if estado == "expirado" or (estado in ("", "ocioso") and ultimo in ("close", "connecting")):
        return "ruim", "Desconectado"
    if estado == "conectado" or (estado in ("", "ocioso") and ultimo == "open"):
        # pedido 41: "estava verdinho e não atualizava" — conexão aberta com socket surdo ou silêncio longo fica âmbar, não verde
        ev, motivo = monitor.get("evento"), str(monitor.get("motivo") or "")
        if ev == "silencio":
            h = re.search(r"sem_mensagem_(\d+)h", motivo)
            return "alerta", f"Conectado, sem mensagem há {h.group(1)} h" if h else "Conectado, sem mensagem nova"
        if ev in ("socket_zumbi", "socket_morto"):
            return "alerta", "Conectado, socket sem resposta"
        if ev == "entrega_travada":          # D-56: o teste de entrega do bot não chegou — aberta, mas nada entra
            return "ruim", "Conectado, entrega travada"
        try:
            if (dados_dir(ctx) / "PAUSADO").exists():
                return "ok", "Conectado, leitura pausada"
        except OSError:
            pass
        return "ok", "Conectado" + _rotulo_lido(ctx, banco)
    return "", "Conexão"


def _rotulo_lido(ctx, banco) -> str:
    """", lido às HH:MM" (hoje) ou ", lido em DD/MM" com a hora do último CICLO do coletor (dados/ultima-leitura.json,
    pedido 43): ciclo sem novidade não grava lote, e o indicador ficava preso no último lote com mensagem nova ("lido em
    08/11" com o coletor rodando). Sem o arquivo (coletor antigo), cai no último lote."""
    quando = None
    try:
        em = json.loads((dados_dir(ctx) / "ultima-leitura.json").read_text(encoding="utf-8")).get("em")
        quando = datetime.fromtimestamp(int(em)).strftime("%Y-%m-%d %H:%M") if em else None
    except (OSError, ValueError, TypeError, AttributeError) as e:
        log.debug("ultima-leitura.json ilegível: %s", causa(e))
    if quando is None:
        try:
            lotes = banco.lotes(1) if banco is not None else []
            quando = str(lotes[0].get("criado_em") or "") if lotes else None
        except Exception as e:  # noqa: BLE001
            log.debug("último lote ilegível: %s", causa(e))
    if not quando or len(quando) < 10:
        return ""
    if quando[:10] == date.today().isoformat():
        return f", lido às {quando[11:16]}"
    return f", lido em {quando[8:10]}/{quando[5:7]}"


def estado_lateral(ctx) -> dict:
    """{"classe","texto","demandas"} da lateral: o que `_lateral` desenha e o que GET /conexao/lateral devolve para o JS da
    base atualizar a cada 30 s sem recarregar (pedido 43). `demandas` = "" ou o número como texto, igual ao contador."""
    n = ""
    if ctx is not None and getattr(ctx, "banco", None) is not None:
        try:
            n = _n_demandas(ctx.banco)
        except Exception as e:  # noqa: BLE001
            log.debug("contagem da lateral falhou: %s", causa(e))
    classe, texto = estado_conexao(ctx) if ctx is not None else ("", "Conexão")
    return {"classe": classe, "texto": texto, "demandas": n, "avisos": faixa_avisos(ctx)}


def _lateral(ctx, ativo: str) -> dict:
    est = estado_lateral(ctx)
    n, classe, texto = est["demandas"], est["classe"], est["texto"]
    nav = "".join(
        f'<a href="{c}"' + (' class="ativo" aria-current="page"' if c == ativo else "")
        + f'>{ICONES.get(c, "")}<span>{esc(r)}</span>' + (f'<span class="n">{n}</span>' if c == "/demandas" and n else "") + "</a>"
        for c, r in NAV)
    atual = ' aria-current="page"' if ativo == "/conexao" else ""
    cls = " ".join(x for x in ("conexao-estado", classe, "ativo" if ativo == "/conexao" else "") if x)
    indicador = (f'<a class="{cls}" href="/conexao"{atual} title="Conexão"><span class="ponto"></span>'
                 f'<span>{esc(texto)}</span></a>')
    # engrenagem de Ajustes ao lado do indicador; acesa também em /bloqueios, que é uma sub-tela de Ajustes
    aj_ativo = ativo in ("/ajustes", "/bloqueios")
    ajustes = (f'<a class="lateral-ajustes{" ativo" if aj_ativo else ""}" href="/ajustes" title="Ajustes (⌘,)" '
               f'aria-label="Ajustes"{" aria-current=\"page\"" if aj_ativo else ""}>{icone("engrenagem")}</a>')
    # modo de gravação (pedido 42): botão visível no rodapé, além de ⌘⇧B, Ajustes e o menu do app; o JS da base sincroniza
    # aria-pressed com data-gravacao (estado só no navegador)
    gravar = ('<button type="button" class="lateral-ajustes lateral-gravar vigia-gravar" aria-pressed="false" '
              f'title="Modo de gravação (⌘⇧B)" aria-label="Modo de gravação">{icone("olho-riscado")}</button>')
    return {"nav": nav, "indicador": indicador, "ajustes": ajustes, "gravar": gravar}


def pagina(titulo_: str, conteudo: str, ativo: str = "", status: int = 200, lateral: str = "", classe_main: str = "",
           *, ctx=None, toast: str = "", toast_desfazer: str = "") -> Resposta:
    """Embrulha `conteudo` (HTML já escapado pelo chamador) no _base.html; sem o arquivo, usa um base mínimo.
    `conteudo` é um ou dois `painel()`; conteúdo solto ganha um painel de detalhe com a toolbar do título.
    `ctx`: contexto da requisição, para a contagem de Demandas e o indicador de conexão da lateral.
    `toast`: aviso de uma linha mostrado por 8 s (depois de um redirecionamento). `lateral`/`classe_main`: compatibilidade."""
    if '<section class="painel' not in conteudo:
        conteudo = painel(miolo(conteudo), toolbar_html=toolbar(titulo(titulo_)), status_html=rodape(""), aria=titulo_)
    pref = preferencias.ler()
    lay = layout.ler()
    try:
        base = carregar_template("_base.html")
    except OSError:
        base = string.Template(_BASE_RESERVA)
    lat = _lateral(ctx, ativo)
    return html_resposta(base.safe_substitute(
        titulo=esc(titulo_), nav=lat["nav"], indicador=lat["indicador"], ajustes=lat["ajustes"], gravar=lat["gravar"],
        lateral=lateral, avisos=faixa_avisos(ctx), roteiro=inicio.roteiro_html(),
        conteudo=conteudo,
        toast=toast_html(toast, toast_desfazer) if toast else "",
        html_attrs=preferencias.atributos_html(pref) + layout.atributos_html(lay),
        paineis_attrs=layout.atributos_paineis(lay, ativo), preferencias_css=preferencias.css(pref)), status)


def pagina_erro(ctx, status: int, mensagem: str, *, pode_repetir: bool = False) -> Resposta:
    """Auditoria T3 (lote 4 do pedido 63): erro que ia para a janela como texto puro ou JSON cru ganha a casca do
    painel (lateral, faixas), a frase que a rota devolveu e uma saída: Voltar, Tentar de novo (só em GET, para não
    reenviar um formulário) e, em erro do servidor, a tela Saúde do Greg. Quem chama é o servidor, num lugar só."""
    msg = " ".join(str(mensagem or "").split())[:300] or "Não deu certo."
    msg = msg[0].upper() + msg[1:] + ("" if msg[-1] in ".!?" else ".")
    titulo_ = "Não encontrado" if status == 404 else "Não deu certo"
    botoes = '<button type="button" class="btn primario" onclick="history.back()">Voltar</button>'
    if pode_repetir:
        botoes += '<button type="button" class="btn" onclick="location.reload()">Tentar de novo</button>'
    if status >= 500:
        botoes += '<a class="btn" href="/avisos">Saúde do Greg</a>'
    corpo = (f'<div class="vazio erro-pagina" role="alert"><b>{esc(titulo_)}</b><span>{esc(msg)}</span>'
             f'<div class="acoes" style="justify-content:center;margin-top:12px">{botoes}</div></div>')
    return pagina(titulo_, painel(corpo, toolbar_html=toolbar(titulo(titulo_)), status_html=rodape(""), aria=titulo_),
                  status=status, ctx=ctx)


def render(nome: str, **campos) -> str:
    """Preenche templates/<nome> com campos JÁ escapados (safe_substitute)."""
    return carregar_template(nome).safe_substitute(**campos)


def redirecionar(destino: str) -> Resposta:
    return Resposta(303, "", cabecalhos=[("Location", destino)])


def texto_simples(msg: str, status: int) -> Resposta:
    return Resposta(status, msg, "text/plain; charset=utf-8")


_LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _cab(cab: dict, nome: str):
    """Cabeçalho sem diferenciar maiúsculas (o servidor entrega minúsculo; testes podem mandar capitalizado)."""
    for k, v in cab.items():
        if str(k).lower() == nome:
            return v
    return None


def pediu_json(req) -> bool:
    """Ação chamada pelo JS da página (fetch com X-Vigia-Auto: 1): responde JSON em vez de redirecionar."""
    return _cab(req.cabecalhos or {}, "x-vigia-auto") == "1"


def _netloc_loopback(netloc: str) -> bool:
    try:
        return urllib.parse.urlsplit("//" + netloc).hostname in _LOOPBACK
    except ValueError:
        return False


def origem_ok(req) -> bool:
    """Defesa contra CSRF/DNS rebinding em POST e no GET com efeito (/conexao/tick).
    Host, se vier, tem de ser loopback. Se vier Sec-Fetch-Site, só vale 'same-origin'. Origin/Referer, se vierem,
    têm de ter netloc (host:porta) IGUAL ao Host e loopback ('null' recusa). Cliente sem nenhum desses
    cabeçalhos (curl, teste) passa: o cookie SameSite=Strict e o token seguram o resto."""
    cab = req.cabecalhos or {}
    host = _cab(cab, "host")
    host = str(host).strip().lower() if host else ""
    if host and not _netloc_loopback(host):
        return False
    sfs = _cab(cab, "sec-fetch-site")
    if sfs is not None and str(sfs).strip().lower() != "same-origin":
        return False
    for chave in ("origin", "referer"):
        v = _cab(cab, chave)
        if not v:
            continue
        try:
            partes = urllib.parse.urlsplit(str(v).strip())
        except ValueError:
            return False
        if partes.scheme != "http" or not _netloc_loopback(partes.netloc):
            return False
        if host and partes.netloc.lower() != host:
            return False
    return True


def proibido() -> Resposta:
    return texto_simples("origem não permitida", 403)


def id_da_rota(req, padrao: str) -> int | None:
    """Extrai o {id} numérico de /demandas/{id}/confirmar etc.; None se não casar."""
    m = re.fullmatch(padrao, req.caminho.split("?", 1)[0])
    return int(m.group(1)) if m else None


def metodo_errado(req, *permitidos: str):
    """None se o método é permitido; senão 405 (o servidor despacha por caminho, não por método)."""
    if req.metodo.upper() in permitidos:
        return None
    return Resposta(405, "método não permitido", "text/plain; charset=utf-8", [("Allow", ", ".join(permitidos))])
