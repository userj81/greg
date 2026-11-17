"""Páginas /demandas (estilo Papel, pedido 39): a caixa de entrada é a tela inicial. Lista (380 px) à esquerda com o
período e a classe como pull-downs, item aberto à direita; revisão pelo teclado (↑↓ ↩ ⌫ F L ⌘Z) e pelos botões; Classe
e Frente em pop-ups; desfazer pelo toast; medição da revisão no rodapé do estado vazio.
D-58 (pedido 46): a tela é REVISÃO, não portão. A IA classifica e lança sozinha; nada fica "a confirmar". Aqui o dono
só desfaz o que não era (Recusar cancela o item ou impede a subida), adianta um provisório (Subir agora) ou corrige
classe e frente, se quiser.
Quem chama: app/server.py (via ROTAS; chaves com {id} são padrões que o servidor precisa casar). O JS da página chama as
mesmas rotas com X-Vigia-Auto: 1 e recebe JSON em vez do redirecionamento.
O que não faz: não lê a Evolution, não apaga nada no Frentes, não grava texto de conversa em log.
"""
import json
import logging
import re
import urllib.parse
from datetime import date, datetime, time as hora_t, timedelta
from pathlib import Path
from types import SimpleNamespace

from app import lancamento
from app.classes import CLASSES, validar_classe
from app.erros import VigiaErro, causa
from app.http import Resposta, json_resposta
from app.paginas import _comum as c
from app.paginas.conexao import TOAST_LENDO, TOAST_PAUSADO, proxima_leitura, toast_ler

log = logging.getLogger("vigia.painel.demandas")
LIMITE = 200
IMAGENS = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
MSG_RECUSA = "o item foi cancelado no Frentes (dá para reabrir lá)"
MSG_RECUSA_PROVISORIA = "não chegou a subir para o Frentes"   # D-57: cartão provisório recusado
MOTIVO_SUBSTITUICAO = {"atualizada": "atualizado", "cancelada": "evento cancelado", "consolidada": "fundido no fechamento do dia"}
PERIODOS = (("hoje", "Hoje"), ("ontem", "Ontem"), ("7dias", "Últimos 7 dias"), ("tudo", "Tudo"))
PERIODO_TEXTO = {"hoje": "hoje", "ontem": "ontem", "7dias": "nos últimos 7 dias", "tudo": ""}
CLASSES_ITEM = ("atencao", "demanda", "lembrar", "lembrando")   # classes que viram item no Frentes
_VOLTA_OK = re.compile(r"^[A-Za-z0-9=&%._@+-]{0,300}$")
_LINHA_MD = re.compile(r"^\[[^\]]*\] \[id:([^\]]*)\] \[[^\]]*\] \[([^\]]*)\] (.*)$")


def confirmar_item(ponte, item_id: int) -> None:
    """PATCH confirmado=1 no Frentes. FrentesPonte não tem método público para isso (ver contratos_incompativeis)."""
    if hasattr(ponte, "confirmar"):
        ponte.confirmar(item_id)
    else:
        ponte._chamar("PATCH", f"/api/itens/{int(item_id)}", corpo={"confirmado": 1})


def _intervalo_dia(dia: str):
    d = datetime.strptime(dia, "%Y-%m-%d").date()
    ini = datetime.combine(d, hora_t.min)
    return int(ini.timestamp()), int((ini + timedelta(days=1)).timestamp()) - 1


def _intervalo_periodo(periodo: str):
    hoje = datetime.combine(date.today(), hora_t.min)
    if periodo == "hoje":
        return int(hoje.timestamp()), None
    if periodo == "ontem":
        return int((hoje - timedelta(days=1)).timestamp()), int(hoje.timestamp()) - 1
    if periodo == "7dias":
        return int((hoje - timedelta(days=6)).timestamp()), None
    return None, None


def _provisoria(r: dict) -> bool:
    """D-57: ainda vai subir ao Frentes (no fechamento do dia, ou agora se o dono mandar)."""
    return lancamento.eh_provisoria(r)


def _substituida(r: dict) -> bool:
    return r.get("substituida_por") is not None


def _lancada(r: dict) -> bool:
    """D-58: a IA já lançou no Frentes e o dono ainda não mexeu: classe que vira item (lembrando não: o item é de outro
    cartão), com item, sem decisão, não substituída. É o cartão que ele pode desfazer com Recusar."""
    return (r.get("item_id") is not None and not r.get("decisao") and not _substituida(r)
            and lancamento.classe_efetiva(r) in lancamento.CLASSES_ITEM)


def _segurada(r: dict) -> bool:
    """D-58: a IA segurou este cartão no fechamento do dia (era só conversa) e ele não subiu. Deixa de valer quando o
    Dono rotula com classe que vira item (aí o cartão sobe)."""
    return (bool(r.get("rebaixada_em")) and not _substituida(r) and r.get("item_id") is None
            and lancamento.classe_efetiva(r) not in lancamento.CLASSES_ITEM)


def _recusavel(r: dict) -> bool:
    """Tem o botão Recusar: provisório (não sobe) ou já lançado pela IA (cancela o item). D-58: com qualquer confiança."""
    return _provisoria(r) or _lancada(r)


def _atrasado(r: dict) -> bool:
    prazo = str(r.get("prazo") or "")[:10]
    try:
        return bool(prazo) and date.fromisoformat(prazo) < date.today() and r.get("decisao") != "recusado"
    except ValueError:
        return False


def _link_conversa(r: dict) -> str:
    """Abre a conversa no painel Conversas."""
    return "/conversas?c=" + urllib.parse.quote(str(r.get("conversa", "")), safe="")


def _veredito(r: dict) -> str:
    """Rotulada (D-45): o que o dono disse. '' se ainda não rotulou."""
    curada = r.get("classe_curada")
    if not curada:
        return ""
    if curada == r.get("classe"):
        return '<span class="meta-linha" title="rótulo seu">✓ certa</span>'
    return f'<span class="meta-linha" title="rótulo seu">✗ era {c.rotulo_classe(curada)}</span>'


# ------------------------------------------------------------------ filtros

def _filtros(req):
    """Lê classe, conversa, dia e período da query; devolve o estado dos filtros, o intervalo e a string `volta`."""
    classe, conversa, dia = c.query(req, "classe"), c.query(req, "conversa"), c.query(req, "dia")
    periodo_pedido = c.query(req, "periodo")
    substituidos = c.query(req, "substituidos") == "1"   # D-57: "Substituídos" no pull-down de classes
    erro = ""
    if classe and classe not in CLASSES:
        erro, classe = "Classe inválida; filtro ignorado.", ""
    desde = ate = None
    periodo = ""
    if dia:
        try:
            desde, ate = _intervalo_dia(dia)
        except ValueError:
            erro, dia = "Dia inválido (use AAAA-MM-DD); filtro ignorado.", ""
    explicito = periodo_pedido in dict(PERIODOS)
    if not dia:
        periodo = periodo_pedido if explicito else ("tudo" if conversa else "hoje")
        desde, ate = _intervalo_periodo(periodo)
    partes = [("classe", classe), ("dia", dia), ("periodo", periodo if explicito and not dia else ""), ("conversa", conversa),
              ("substituidos", "1" if substituidos else "")]
    volta = urllib.parse.urlencode([(k, v) for k, v in partes if v])
    return SimpleNamespace(classe=classe, conversa=conversa, dia=dia, periodo=periodo, desde=desde, ate=ate, erro=erro,
                           volta=volta, partes=dict(partes), substituidos=substituidos)


def _href(f, **troca) -> str:
    d = dict(f.partes)
    d.update(troca)
    q = urllib.parse.urlencode([(k, v) for k, v in d.items() if v])
    return "/demandas" + (f"?{q}" if q else "")


def _periodo_texto(f) -> str:
    if f.dia:
        return f"em {c.data_longa(f.dia)}"
    return PERIODO_TEXTO.get(f.periodo, "")


def _rotulo_periodo(f) -> str:
    if f.dia:
        return c.data_longa(f.dia)
    return dict(PERIODOS).get(f.periodo, "Tudo")


def _pulldown_classes(f) -> str:
    atual = "Substituídos" if f.substituidos else (c.rotulo_classe(f.classe).capitalize() if f.classe else "Todas as classes")
    itens = c.item_menu("Todas as classes", href=_href(f, classe="", substituidos=""), marcado=not f.classe and not f.substituidos)
    itens += "".join(c.item_menu(c.ROTULO_CLASSE.get(k, k), href=_href(f, classe=k, substituidos=""), marcado=f.classe == k and not f.substituidos)
                     for k in CLASSES if k != "sem_acao")
    itens += c.separador_menu() + c.item_menu(c.ROTULO_CLASSE["sem_acao"], href=_href(f, classe="sem_acao", substituidos=""),
                                              marcado=f.classe == "sem_acao" and not f.substituidos)
    # D-57: cartões que outro cartão substituiu (data mudou, evento cancelado, fundidos no fechamento); voltam com Restaurar
    itens += c.item_menu("Substituídos", href=_href(f, classe="", substituidos="1"), marcado=f.substituidos)
    return c.pulldown(f"<span>{c.esc(atual)}</span>", itens, id_="dm-filtro-classe", direita=True, aria="Classe")


def _pulldown_periodo(f) -> str:
    itens = "".join(c.item_menu(rot, href=_href(f, periodo=k, dia=""), marcado=(not f.dia and f.periodo == k)) for k, rot in PERIODOS)
    return c.pulldown(f"<span>{c.esc(_rotulo_periodo(f))}</span>", itens, id_="dm-filtro-periodo", aria="Período")


# ------------------------------------------------------------------ lista e detalhe

def _pilula(r: dict) -> str:
    """Uma pílula só, por prioridade: substituído (cinza) > segurado (cinza) > atrasado (vermelha) > provisório (cinza).
    D-58: não existe mais "a confirmar"; "segurado" = a IA não deixou subir no fechamento do dia."""
    if _substituida(r):
        return c.pilula("substituído", "neutra")
    if _segurada(r):
        return c.pilula("segurado", "neutra segurado")
    if _atrasado(r):
        return c.pilula("atrasado", "atrasado")
    if _provisoria(r):
        return c.pilula("provisório", "neutra provisorio")
    return ""


def _item_lista(r: dict, volta: str, sel: bool, atualizado: bool = False) -> str:
    """Uma linha da lista: nome 13/600 + hora à direita, resumo em até 2 linhas, classe (ponto + palavra) e a pílula
    'atrasado' (vermelha), 'provisório' ou 'substituído' (cinza, D-57). `atualizado` = este cartão substituiu outro (marca
    discreta). Selecionada = fundo --selecao."""
    i = int(r["id"])
    href = "/demandas?" + (f"{volta}&" if volta else "") + f"item={i}"
    pil = _pilula(r)
    nome, resumo = str(r.get("contato") or "?"), str(r.get("resumo") or "")
    busca = c.esc((nome + " " + resumo).lower())
    marca = '<span class="dm-atualizado" title="substituiu um cartão anterior desta conversa">atualizado</span>' if atualizado else ""
    return (f'<a class="item-lista dm-it{" ativo" if sel else ""}" href="{c.esc(href)}" data-id="{i}" data-busca="{busca}"'
            f'{" aria-current=\"true\"" if sel else ""}>'
            f'<div class="cab"><span class="nome">{c.esc(nome)}</span><time>{c.hora_curta(r.get("ts"))}</time></div>'
            f'<div class="texto">{c.esc(resumo)}</div>'
            f'<div class="meta">{c.classe_html(r.get("classe", ""))}{pil}{marca}</div></a>')


def _pasta_da_mensagem(reg: dict, raiz: Path):
    """Pasta da conversa dentro de dados/caixa que tem o msg_id no mensagens.json; None se o lote já foi limpo."""
    lote = str(reg.get("lote") or "")
    padrao = f"{lote}/*/mensagens.json" if lote and "/" not in lote and not lote.startswith(".") else "*/*/mensagens.json"
    for manifesto in sorted(raiz.glob(padrao), reverse=True):
        try:
            dados = json.loads(manifesto.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(dados, dict) and reg.get("msg_id") in dados:
            return manifesto.parent
    return None


def _trecho(reg: dict, raiz: Path) -> tuple[str, str, str] | None:
    """(quem, HH:MM, texto) da mensagem original, lido de conversa.md do lote; None quando o lote já foi limpo
    (fechamento semanal) ou a linha não está lá. Só leitura local; nada é logado."""
    msg_id = str(reg.get("msg_id") or "")
    if not msg_id:
        return None
    try:
        pasta = _pasta_da_mensagem(reg, raiz)
        if pasta is None:
            return None
        for linha in (pasta / "conversa.md").read_text(encoding="utf-8").splitlines():
            m = _LINHA_MD.match(linha)
            if m and m.group(1) == msg_id:
                texto = m.group(3).strip()
                if not texto:
                    return None
                return m.group(2), linha[1:6], (texto[:297] + "…") if len(texto) > 300 else texto
    except (OSError, ValueError) as e:
        log.debug("trecho indisponível: %s", causa(e))
    return None


def _detalhe(r: dict, volta: str, frentes: list | None, trecho=None, anteriores: list | None = None,
             fechamento: str = "21:00", novo: dict | None = None) -> str:
    """O item aberto: nome + pílula e as ações à direita na mesma linha; título 17/600 (o resumo); meta 12 com hora,
    prazo e 'Abrir conversa'; trecho da mensagem na bolha cinza; miniatura; e, no rodapé da área, os pop-ups Classe
    e Frente. D-57: `anteriores` = cartões que este substituiu ("Atualizado às… Antes: …"); `novo` = o cartão que
    substituiu este (então a ação é Restaurar); `fechamento` = hora em que o provisório sobe ao Frentes."""
    i = int(r["id"])
    pil = _pilula(r)
    if _substituida(r):
        motivo = MOTIVO_SUBSTITUICAO.get(str(r.get("substituicao_motivo") or ""), "substituído")
        quando = c.esc(str(r.get("substituida_em") or "")[11:16])
        alvo = (f' por <a href="{c.esc("/demandas?item=" + str(r["substituida_por"]))}">{c.esc(str(novo.get("resumo") or "#" + str(r["substituida_por"])))}</a>'
                if novo else f' por <a href="{c.esc("/demandas?item=" + str(r["substituida_por"]))}">#{int(r["substituida_por"])}</a>')
        acoes = (f'<span class="meta-linha">{c.esc(motivo.capitalize())} às {quando}{alvo}</span>'
                 f'<form method="post" action="/demandas/{i}/restaurar" class="dm-acao" data-decisao="restaurado">'
                 f'<input type="hidden" name="volta" value="{c.esc(volta)}">{c.botao("Restaurar", tipo="submit")}</form>')
    elif _provisoria(r):
        acoes = (f'<form method="post" action="/demandas/{i}/recusar" class="dm-acao" data-decisao="recusado"'
                 f' title="Não era demanda: não sobe para o Frentes">{c.botao("Recusar", tipo="submit")}</form>'
                 f'<form method="post" action="/demandas/{i}/confirmar" class="dm-acao" data-decisao="confirmado"'
                 f' title="Provisório: sobe sozinho para o Frentes às {c.esc(fechamento)}; aqui sobe agora">'
                 f'{c.botao("Subir agora", classe="primario", tipo="submit")}</form>')
    elif _lancada(r):   # D-58: a IA já lançou; nada a confirmar, só dá para desfazer
        acoes = ('<span class="meta-linha">No Frentes</span>'
                 f'<form method="post" action="/demandas/{i}/recusar" class="dm-acao" data-decisao="recusado"'
                 f' title="Não era demanda: cancela o item no Frentes (dá para reabrir lá)">{c.botao("Recusar", tipo="submit")}</form>')
    elif _segurada(r):   # D-58: a IA segurou no fechamento; um clique devolve a classe original e sobe o cartão
        era = str(r.get("classe_original") or "demanda")
        acoes = ('<span class="meta-linha">Não subiu</span>'
                 + c.botao("Subir assim mesmo", tipo="submit",
                           attrs=f'form="f-classe-{i}" name="classe" value="{c.esc(era)}"'
                                 f' title="Marca como {c.rotulo_classe(era)} e sobe para o Frentes agora"'))
    elif r.get("decisao") == "confirmado":
        acoes = '<span class="meta-linha">No Frentes</span>'
    elif r.get("decisao") == "recusado":
        acoes = f'<span class="meta-linha">Recusada: {c.esc(MSG_RECUSA if r.get("item_id") is not None else MSG_RECUSA_PROVISORIA)}</span>'
    elif r.get("item_id") is not None:
        acoes = '<span class="meta-linha">No Frentes</span>'
    else:
        acoes = ""
    meta = c.esc(c.quando_longo(r.get("ts")))
    if r.get("prazo"):
        meta += f", prazo {c.data_longa(r['prazo'])}"
    if _provisoria(r):
        meta += f". Sobe para o Frentes às {c.esc(fechamento)}"
    elif _lancada(r) and r.get("confianca") == "baixa":
        meta += ". A IA lançou com confiança baixa"
    elif _segurada(r):
        meta += f". A IA segurou no fechamento do dia (era {c.rotulo_classe(r.get('classe_original') or 'demanda')})"
        if r.get("rebaixada_motivo"):
            meta += f": {c.esc(r['rebaixada_motivo'])}"
    meta += f'. <a href="{c.esc(_link_conversa(r))}">Abrir conversa</a>'
    antes = ""
    for a in (anteriores or []):
        quando = c.esc(str(a.get("substituida_em") or "")[11:16])
        estado = c.esc(str(a.get("resumo") or ""))
        if a.get("prazo"):
            estado += f", prazo {c.esc(c.data_longa(a['prazo']))}"
        antes += (f'<p class="meta-linha dm-antes">Atualizado às {quando}. Antes: “{estado}”'
                  f' <a href="{c.esc("/demandas?substituidos=1&item=" + str(a["id"]))}">ver</a></p>')
    citacao = ""
    if trecho:
        quem, hh, texto = trecho
        citacao = f'<div class="citacao">{c.esc(texto)}<small>{c.esc(quem)}, {c.esc(hh)}</small></div>'
    mini = (f'<img class="dm-mini" loading="lazy" alt="imagem da mensagem" src="/demandas/miniatura?id={i}">'
            if r.get("imagem") else "")
    atual = r.get("classe_curada") or r.get("classe") or ""
    itens = "".join(c.item_menu(c.ROTULO_CLASSE.get(k, k), attrs=f'form="f-classe-{i}" name="classe" value="{k}"', marcado=k == atual)
                    for k in CLASSES)
    classe = (f'<form id="f-classe-{i}" method="post" action="/demandas/{i}/curar" class="dm-popup" hidden>'
              f'<input type="hidden" name="volta" value="{c.esc(volta)}"></form>'
              + c.popup("Classe", c.ROTULO_CLASSE.get(atual, atual), itens, id_="dm-classe",
                        titulo_="Marca a classe certa (L). Classe que vira item sobe agora o cartão que ainda não subiu."))
    frente = ""
    if (r.get("item_id") is not None or _provisoria(r)) and frentes:   # D-57: no provisório, escolher a frente sobe o cartão nela
        fid = r.get("frente_id")
        nome_atual = next((str(f.get("nome") or "?") for f in frentes if f.get("id") == fid), "Caixa de entrada")
        ops = "".join(c.item_menu(str(f.get("nome") or "?"), attrs=f'form="f-frente-{i}" name="frente" value="{int(f["id"])}"',
                                  marcado=f.get("id") == fid)
                      for f in sorted(frentes, key=lambda f: str(f.get("nome", "")).lower()))
        dica = ("Sobe o cartão nesta frente agora (F). Vale para as próximas mensagens desta conversa."
                if _provisoria(r) else "Move para esta frente (F). Vale para as próximas mensagens desta conversa.")
        frente = (f'<form id="f-frente-{i}" method="post" action="/demandas/{i}/frente" class="dm-popup" hidden>'
                  f'<input type="hidden" name="volta" value="{c.esc(volta)}"></form>'
                  + c.popup("Frente", nome_atual, ops, id_="dm-frente", titulo_=dica))
    return (f'<div class="miolo dm-det" data-id="{i}">'
            f'<div class="dm-cab"><div class="dm-nome"><span>{c.esc(r.get("contato") or "?")}</span>{pil}</div><div class="acoes">{acoes}</div></div>'
            f'<h2 class="dm-titulo">{c.esc(r.get("resumo"))}</h2>'
            f'<p class="meta-linha dm-meta">{meta}</p>{antes}{citacao}{mini}'
            f'<div class="dm-rodape">{classe}{_veredito(r)}{frente}</div></div>')


def _fechamento(ctx) -> str:
    return lancamento.rotulo_fechamento(getattr(ctx, "config", None) or {})


def _detalhe_de(ctx, reg: dict, volta: str) -> str:
    """Monta o detalhe com o que o cartão precisa: frentes (se tem item ou é provisório), trecho, substituídos e o novo."""
    frentes = _frentes(ctx) if (reg.get("item_id") is not None or _provisoria(reg)) else None
    banco = ctx.banco
    anteriores = banco.substituidas_de(reg["id"]) if hasattr(banco, "substituidas_de") else []
    novo = banco.classificacao(reg["substituida_por"]) if reg.get("substituida_por") is not None else None
    return _detalhe(reg, volta, frentes, _trecho(reg, c.dados_dir(ctx) / "caixa"), anteriores=anteriores,
                    fechamento=_fechamento(ctx), novo=novo)


def _frentes(ctx) -> list | None:
    try:
        return [f for f in ctx.ponte.frentes() if f.get("id") is not None]
    except (VigiaErro, OSError, ValueError) as e:
        log.warning("frentes indisponíveis: %s", causa(e))
        return None


def _nome_frente(frentes, fid) -> str:
    if frentes is None or fid is None:
        return ""
    return next((str(f.get("nome") or "") for f in frentes if f.get("id") == fid), "")


def _status_lista(linhas: list, fechamento: str = "21:00") -> str:
    n = len(linhas)
    if not n:
        return "Nenhuma demanda"
    if linhas and all(_substituida(r) for r in linhas):
        return f"{n} {'substituído' if n == 1 else 'substituídos'}"
    p = sum(1 for r in linhas if _provisoria(r))
    texto = f"{n} {'demanda' if n == 1 else 'demandas'}"
    if p:
        texto += f", {p} {'sobe' if p == 1 else 'sobem'} às {fechamento}"
    return texto


def _status_triagem(banco) -> str:
    """Rodapé do estado vazio: quantas decisões o dono tomou hoje e em quanto tempo (medição pedida pelo guia Papel;
    com o D-58 a revisão é opcional, então o número tende a zero)."""
    try:
        t = banco.triagem_hoje()
    except Exception as e:  # noqa: BLE001
        log.debug("triagem_hoje falhou: %s", causa(e))
        return "Nenhuma demanda hoje"
    if not t["n"]:
        return "Nenhuma demanda hoje"
    media = f" em {c.duracao(t['media_s'])}" if t.get("media_s") is not None else ""
    return f"{t['n']} {'triada' if t['n'] == 1 else 'triadas'} hoje{media}"


def _status_precisao(banco) -> str:
    try:
        p = banco.precisao_hoje()
    except Exception as e:  # noqa: BLE001
        log.debug("precisao_hoje falhou: %s", causa(e))
        return ""
    if not p["total"]:
        return "Nenhuma classe rotulada hoje"
    return f"A IA acertou {p['acertos']} de {p['total']} {'classe' if p['total'] == 1 else 'classes'} hoje"


def _vazio_lista(f, banco) -> str:
    if f.substituidos:
        return c.vazio(f"Nenhum cartão substituído {_periodo_texto(f)}".strip(),
                       "Cartão substituído é o que outro cartão da mesma conversa atualizou, cancelou ou absorveu no fechamento do dia.")
    if f.classe:
        return c.vazio(f"Nenhuma demanda de {c.ROTULO_CLASSE.get(f.classe, f.classe)} {_periodo_texto(f)}".strip())
    if f.conversa:
        return c.vazio("Nenhuma demanda desta conversa")
    extra = ""   # D-58: não há mais "N a confirmar em dias anteriores" (nada espera o dono)
    if f.periodo in ("hoje", ""):
        # pedido 41 (tempo real): "Ler agora" dispara o ciclo sem esperar a meia hora (POST /conexao/ler-agora)
        ler = ('<form method="post" action="/conexao/ler-agora" class="dm-ler-agora"><input type="hidden" name="volta" value="/demandas">'
               '<button type="submit" class="btn">Ler agora</button></form>')
        return f'<div class="vazio"><b>Nada novo</b><span>{c.esc(proxima_leitura_segura(banco))}</span>{extra}{ler}</div>'
    return c.vazio(f"Nenhuma demanda {_periodo_texto(f)}".strip())


def proxima_leitura_segura(banco) -> str:
    """proxima_leitura precisa do ctx; o estado vazio da lista só tem o banco — reconstrói pelo último lote."""
    try:
        lotes = banco.lotes(1)
    except Exception as e:  # noqa: BLE001
        log.debug("lotes ilegível: %s", causa(e))
        return ""
    if not lotes:
        return "Leitura ainda não rodou"
    from app import agenda_leitura       # pedido 81: com intervalo ou horário escolhido em Ajustes, vale a agenda
    pela_agenda = agenda_leitura.proxima_fora_do_padrao(Path(getattr(banco, "caminho", "") or ".").parent)
    if pela_agenda:
        return pela_agenda
    try:
        d = datetime.strptime(str(lotes[0].get("criado_em"))[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ""
    prox = d + timedelta(minutes=30)
    return "Próxima leitura em instantes" if prox < datetime.now() else f"Próxima leitura às {prox.hour}:{prox.minute:02d}"


_CSS = """<style>
.dm-avisos:not(:empty) { display: flex; flex-direction: column; gap: 8px; padding: 10px 14px 4px; }
.dm-cab { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 10px; min-height: var(--controle); }
.dm-nome { display: flex; align-items: center; gap: 8px; min-width: 0; font: 600 1.1538rem/1.3 var(--fonte); }
.dm-nome > span:first-child { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dm-titulo { font: var(--t-titulo-item) var(--fonte); margin: 0 0 6px; overflow-wrap: anywhere; }
.dm-meta { margin-bottom: 14px; }
.dm-det .citacao { margin-bottom: 14px; }
.dm-mini { display: block; max-width: 320px; max-height: 240px; border-radius: var(--raio-grupo); margin: 0 0 14px; box-shadow: 0 0 0 .5px var(--grupo-borda); }
.dm-rodape { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; padding-top: 14px; margin-top: 4px; border-top: 1px solid var(--fio-fraco); }
.dm-rodape:empty { display: none; }
.pilula.neutra { background: var(--fundo-2); color: var(--meta); box-shadow: inset 0 0 0 .5px var(--fio-fraco); }
.dm-atualizado { font: 500 .8462rem/1 var(--fonte); color: var(--meta); white-space: nowrap; }
.dm-antes { margin: -8px 0 14px; }
.dm-antes a { margin-left: 4px; }
.acoes .meta-linha + form { margin-left: 8px; }
</style>"""

_JS = """<script>
(function () {
  var lista = document.getElementById('dm-lista'), slot = document.getElementById('dm-detalhe-slot');
  if (!lista || !slot) return;
  var volta = lista.getAttribute('data-volta') || '';
  var vazioDetalhe = (document.getElementById('dm-vazio-detalhe') || {}).innerHTML || '';
  var historico = [];
  function linhas() { return Array.prototype.slice.call(lista.querySelectorAll('a.item-lista[data-id]:not([hidden])')); }
  function atual() { return lista.querySelector('a.item-lista[aria-current="true"]'); }
  function marcar(a) {
    linhas().forEach(function (x) { x.classList.toggle('ativo', x === a); if (x === a) x.setAttribute('aria-current', 'true'); else x.removeAttribute('aria-current'); });
    if (a && a.scrollIntoView) a.scrollIntoView({ block: 'nearest' });
  }
  function abrir(a, empurrar) {
    if (!a) { slot.innerHTML = vazioDetalhe; return; }
    marcar(a);
    var id = a.getAttribute('data-id');
    fetch('/demandas/' + id + '/detalhe' + (volta ? '?volta=' + encodeURIComponent(volta) : ''), { credentials: 'same-origin' })
      .then(function (r) { if (!r.ok) throw new Error('http'); return r.text(); })
      .then(function (h) { slot.innerHTML = h; window.vigiaOffline(false); if (empurrar !== false) history.replaceState(null, '', a.getAttribute('href')); })
      .catch(function () { location.href = a.getAttribute('href'); });
  }
  function mover(delta) {
    var ls = linhas(); if (!ls.length) return;
    var i = ls.indexOf(atual()), j = i < 0 ? 0 : Math.min(ls.length - 1, Math.max(0, i + delta));
    if (j !== i) abrir(ls[j]);
  }
  function proximo(depois) {
    var ls = linhas(), i = ls.indexOf(depois);
    return ls[i + 1] || (i > 0 ? ls[i - 1] : null);
  }
  function statusLista() {
    var ls = linhas(), n = ls.length, p = ls.filter(function (a) { return a.querySelector('.pilula.provisorio'); }).length;
    var s = document.getElementById('dm-status-lista'); if (!s) return;
    var hora = lista.getAttribute('data-fechamento') || '21:00';
    s.textContent = n ? (n + (n === 1 ? ' demanda' : ' demandas') + (p ? ', ' + p + (p === 1 ? ' sobe' : ' sobem') + ' às ' + hora : '')) : 'Nenhuma demanda';
  }
  function decidir(a, decisao) {
    if (!a) return;
    var form = slot.querySelector('form.dm-acao[data-decisao="' + decisao + '"]');
    if (!form) return;
    var id = a.getAttribute('data-id');
    window.vigiaPost(form.getAttribute('action'), {}).then(function (j) {
      if (!j.ok) { window.vigiaToast(j.erro || 'Não deu para gravar a decisão.'); return; }
      var some = decisao === 'recusado' || decisao === 'restaurado';
      if (decisao === 'recusado') historico.push(id);   // auditoria D1 (D-66): só a recusa se desfaz de verdade
      var prox = some ? proximo(a) : null;
      if (some) { a.remove(); } else { var p = a.querySelector('.pilula.provisorio'); if (p) p.remove(); }
      statusLista();
      if (prox) abrir(prox); else if (some) abrir(null); else abrir(a, false);
      if (decisao === 'restaurado') { window.vigiaToast(j.toast || 'Restaurado.'); return; }
      if (decisao !== 'recusado') { window.vigiaToast(j.toast || 'Subiu para o Frentes.'); return; }   // D1: subir não tem volta pelo toast
      window.vigiaToast(j.toast || 'Recusada.', { desfazer: function () { desfazer(id); } });
    }).catch(function () { window.vigiaToast('O painel não respondeu.'); });
  }
  function desfazer(id) {
    window.vigiaPost('/demandas/' + id + '/desfazer', {}).then(function (j) {
      if (j.ok) location.replace('/demandas?' + (volta ? volta + '&' : '') + 'item=' + id);
      else window.vigiaToast(j.erro || 'Não deu para desfazer.');
    }).catch(function () { window.vigiaToast('O painel não respondeu.'); });
  }
  window.vigiaDesfazer = function () { var id = historico.pop(); if (!id) return false; desfazer(id); return true; };
  lista.addEventListener('click', function (e) {
    var a = e.target.closest('a.item-lista[data-id]'); if (!a || e.metaKey || e.ctrlKey || e.shiftKey) return;
    e.preventDefault(); abrir(a);
  });
  slot.addEventListener('submit', function (e) {
    var f = e.target; if (!f.classList || !f.classList.contains('dm-acao')) return;
    e.preventDefault(); decidir(atual(), f.getAttribute('data-decisao'));
  });
  slot.addEventListener('click', function (e) {
    var b = e.target.closest('button[name][form]'); if (!b) return;
    var f = document.getElementById(b.getAttribute('form')); if (!f || !f.classList.contains('dm-popup')) return;
    e.preventDefault();
    var dados = { volta: volta }; dados[b.name] = b.value;
    var a = atual();
    window.vigiaPost(f.getAttribute('action'), dados).then(function (j) {
      if (!j.ok) { window.vigiaToast(j.erro || 'Não deu para gravar.'); return; }
      if (j.toast) window.vigiaToast(j.toast);
      if (a) {
        if (b.name === 'classe' && j.classe) { var cl = a.querySelector('.classe'); if (cl) { cl.className = 'classe ' + (j.classe === 'lembrando' ? 'lembrar' : j.classe); cl.textContent = j.rotulo || j.classe; } }
        if (j.confirmado) { var p = a.querySelector('.pilula.provisorio, .pilula.segurado'); if (p) p.remove(); statusLista(); }
        abrir(a, false);
      }
    }).catch(function () { window.vigiaToast('O painel não respondeu.'); });
  });
  document.addEventListener('keydown', function (e) {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    var t = e.target, emCampo = t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable);
    if (emCampo) { if (e.key === 'Escape') t.blur(); return; }
    if (document.querySelector('details.menu[open]')) return;
    var a = atual();
    if (e.key === 'ArrowDown' || e.key === 'j') { e.preventDefault(); mover(1); }
    else if (e.key === 'ArrowUp' || e.key === 'k') { e.preventDefault(); mover(-1); }
    else if (e.key === 'Enter') { e.preventDefault(); decidir(a, 'confirmado'); }
    else if (e.key === 'Backspace' || e.key === 'Delete') { e.preventDefault(); decidir(a, 'recusado'); }
    else if (e.key === 'f' || e.key === 'F' || e.key === 'l' || e.key === 'L') {
      var m = slot.querySelector(e.key === 'f' || e.key === 'F' ? '#dm-frente' : '#dm-classe');
      if (m) { e.preventDefault(); m.open = true; var b1 = m.querySelector('.menu-itens .on') || m.querySelector('.menu-itens button'); if (b1) b1.focus(); }
    }
  });
  var busca = document.getElementById('dm-busca');
  if (busca) busca.addEventListener('input', function () {
    var q = busca.value.trim().toLowerCase();
    lista.querySelectorAll('a.item-lista[data-id]').forEach(function (a) { a.hidden = !!q && (a.getAttribute('data-busca') || '').indexOf(q) < 0; });
    statusLista();
    var v = document.getElementById('dm-sem-busca'); if (v) v.hidden = !(q && !linhas().length);
  });
})();
</script>"""


def demandas_get(req):
    if (e := c.metodo_errado(req, "GET", "HEAD")):
        return e
    ctx = req.ctx
    f = _filtros(req)
    linhas = ctx.banco.classificacoes(conversa=f.conversa or None, classe=f.classe or None, desde=f.desde, ate=f.ate,
                                      limite=LIMITE, excluir_classe=None if f.classe else "sem_acao",
                                      substituidas=True if f.substituidos else False)
    no_teto = len(linhas) >= LIMITE   # T6: a consulta bateu no teto; o rodapé diz que há mais do que aparece
    linhas = [r for r in linhas if r.get("decisao") != "recusado"]   # recusada sai da lista (vive no Frentes, cancelada)
    anteriores = ctx.banco.anteriores_de([r["id"] for r in linhas]) if (linhas and not f.substituidos) else {}
    toast = ""
    if c.query(req, "recusado") == "2":
        toast = f"Recusada: {MSG_RECUSA_PROVISORIA}."
    elif c.query(req, "recusado"):
        toast = f"Recusada: {MSG_RECUSA}."
    elif c.query(req, "confirmado"):
        toast = "Subiu para o Frentes."
    elif c.query(req, "desfeito"):
        toast = "Decisão desfeita."
    elif c.query(req, "restaurado"):
        toast = "Restaurado. O cartão voltou às Demandas."
    elif c.query(req, "aviso") == "nao_subiu":   # D2: classe gravada, cartão não subiu (caminho sem JavaScript)
        toast = "Classe gravada. Não subiu: o Frentes não respondeu. Sobe no fechamento do dia."
    elif c.query(req, "lendo") == "1":
        toast = toast_ler(ctx)
    elif c.query(req, "lendo") == "0":
        toast = TOAST_PAUSADO
    pedido = c.query(req, "item")
    sel = next((r for r in linhas if pedido and str(r.get("id")) == pedido), None) or (linhas[0] if linhas else None)
    avisos = c.erro_inline(f.erro) if f.erro else ""
    if linhas:
        corpo_lista = (f'<div class="dm-avisos">{avisos}</div><div class="itens">'
                       + "".join(_item_lista(r, f.volta, r is sel, atualizado=bool(anteriores.get(r["id"]))) for r in linhas)
                       + '</div><div class="vazio" id="dm-sem-busca" hidden><b>Nada com esse texto</b></div>')
    else:
        corpo_lista = f'<div class="dm-avisos">{avisos}</div>' + _vazio_lista(f, ctx.banco)
    status_lista = _status_lista(linhas, _fechamento(ctx)) if linhas else _status_triagem(ctx.banco)
    if no_teto and linhas:
        status_lista += f". Mostrando as {LIMITE} mais recentes; estreite o período ou a classe para ver as outras"
    if f.conversa:
        status_lista = f'Só esta conversa. <a href="{c.esc(_href(f, conversa=""))}">Ver todas</a>'
    detalhe = _detalhe_de(ctx, sel, f.volta) if sel else c.vazio("Nenhuma demanda selecionada")
    status_det = proxima_leitura(ctx) if sel else _status_precisao(ctx.banco)
    lista = c.painel(corpo_lista, toolbar_html=c.toolbar(c.titulo("Demandas"), c.espaco(), _pulldown_classes(f)),
                     status_html=c.rodape(status_lista, "dm-status-lista"), classe="lista", aria="Demandas",
                     corpo_id="dm-lista", corpo_attrs=f'data-volta="{c.esc(f.volta)}" data-fechamento="{c.esc(_fechamento(ctx))}"')
    det = c.painel(detalhe, toolbar_html=c.toolbar(_pulldown_periodo(f), c.espaco(), c.busca("q", placeholder="Buscar", id_="dm-busca")),
                   status_html=c.rodape(c.esc(status_det), "dm-status-detalhe"), classe="detalhe", aria="Demanda aberta",
                   corpo_id="dm-detalhe-slot")
    modelo = f'<template id="dm-vazio-detalhe">{c.vazio("Nenhuma demanda selecionada")}</template>'
    return c.pagina("Demandas", lista + det + modelo + _CSS + _JS, "/demandas", ctx=ctx, toast=toast)


def detalhe_get(req):
    """Fragmento do item aberto (o JS troca o painel da direita sem recarregar ao andar com ↑↓)."""
    if (e := c.metodo_errado(req, "GET")):
        return e
    cid = c.id_da_rota(req, r"/demandas/(\d+)/detalhe")
    reg = req.ctx.banco.classificacao(cid) if cid is not None else None
    if reg is None:
        return c.texto_simples("classificação inexistente", 404)
    volta = c.query(req, "volta")
    if not _VOLTA_OK.match(volta):
        volta = ""
    return Resposta(200, _detalhe_de(req.ctx, reg, volta))


# ------------------------------------------------------------------ ações

def _falha(req, mensagem: str, status: int):
    if c.pediu_json(req):
        return json_resposta({"ok": False, "erro": mensagem}, status)
    return c.texto_simples(mensagem, status)


def _decidir(req, decisao: str):
    if (e := c.metodo_errado(req, "POST")):
        return e
    if not c.origem_ok(req):
        return c.proibido()
    cid = c.id_da_rota(req, r"/demandas/(\d+)/(?:confirmar|recusar)")
    reg = req.ctx.banco.classificacao(cid) if cid is not None else None
    if reg is None:
        return _falha(req, "classificação inexistente", 404)
    if reg.get("decisao") or not _recusavel(reg):  # uma decisão por cartão; substituído, aviso e lembrando não têm
        return _falha(req, "só cartão provisório ou lançado pela IA, ainda sem decisão", 409)
    lancou = False
    if decisao == "confirmado" and not _provisoria(reg):   # D-58: o que já subiu não tem o que confirmar
        return _falha(req, "já está no Frentes: não há o que confirmar", 409)
    if decisao == "confirmado":   # D-57: "Subir agora" adianta o fechamento do dia para este cartão
        try:
            lancamento.lancar(req.ctx.banco, req.ctx.ponte, reg)
            lancou = True
            reg = req.ctx.banco.classificacao(cid)
        except VigiaErro as e:
            log.warning("subir ao Frentes falhou: %s", causa(e))
            return _falha(req, "Frentes indisponível; nada foi gravado", 502)
    if decisao == "recusado" and reg.get("item_id") is not None:   # F4 do estudo: recusa não deixa lixo no Frentes
        try:
            req.ctx.ponte.alterar(reg["item_id"], status="cancelado")
        except VigiaErro as e:
            log.warning("cancelar no Frentes falhou: %s", causa(e))
            return _falha(req, "Frentes indisponível; nada foi gravado", 502)
    req.ctx.banco.decidir(cid, decisao)
    sem_item = reg.get("item_id") is None
    if c.pediu_json(req):
        nome = str(reg.get("contato") or "A demanda")
        if decisao == "confirmado":
            frente = _nome_frente(_frentes(req.ctx), reg.get("frente_id")) if reg.get("item_id") is not None else ""
            toast = f"{nome} foi para Frentes" + (f", {frente}" if frente else "") + "."
        else:
            msg = MSG_RECUSA_PROVISORIA if sem_item else MSG_RECUSA
            toast = f"{nome} recusada. {msg[0].upper() + msg[1:]}."
        return json_resposta({"ok": True, "id": cid, "decisao": decisao, "toast": toast, "lancado": lancou})
    if decisao == "recusado":
        return c.redirecionar("/demandas?recusado=2" if sem_item else "/demandas?recusado=1")
    return c.redirecionar("/demandas?confirmado=1")


def confirmar_post(req):
    return _decidir(req, "confirmado")


def recusar_post(req):
    return _decidir(req, "recusado")


def desfazer_post(req):
    """Desfazer (toast ou ⌘Z): a decisão volta a 'nenhuma'; no Frentes, a recusa reabre o item. Desfazer um "Subir
    agora" não tira o item do Frentes (D-58: nada volta a "a confirmar"): o cartão fica lançado e dá para Recusar.
    404 sem registro · 409 sem decisão a desfazer · 502 Frentes fora (nada gravado)."""
    if (e := c.metodo_errado(req, "POST")):
        return e
    if not c.origem_ok(req):
        return c.proibido()
    cid = c.id_da_rota(req, r"/demandas/(\d+)/desfazer")
    reg = req.ctx.banco.classificacao(cid) if cid is not None else None
    if reg is None:
        return _falha(req, "classificação inexistente", 404)
    if not reg.get("decisao"):
        return _falha(req, "nada a desfazer", 409)
    if reg.get("item_id") is not None and reg["decisao"] == "recusado":
        try:
            req.ctx.ponte.alterar(reg["item_id"], status="aberto")
        except VigiaErro as e:
            log.warning("desfazer no Frentes falhou: %s", causa(e))
            return _falha(req, "Frentes indisponível; nada foi gravado", 502)
    req.ctx.banco.desfazer_decisao(cid)
    if c.pediu_json(req):
        return json_resposta({"ok": True, "id": cid})
    return c.redirecionar(f"/demandas?desfeito=1&item={cid}")


def restaurar_post(req):
    """D-57: o cartão substituído volta às Demandas (o que o substituiu fica). D-58: se a IA tinha cancelado o item por
    causa de um `--cancela`, Restaurar o reabre no Frentes (só se ele está mesmo cancelado). 404 sem registro · 409 não
    substituído · 502 Frentes fora (nada gravado)."""
    if (e := c.metodo_errado(req, "POST")):
        return e
    if not c.origem_ok(req):
        return c.proibido()
    cid = c.id_da_rota(req, r"/demandas/(\d+)/restaurar")
    reg = req.ctx.banco.classificacao(cid) if cid is not None else None
    if reg is None:
        return _falha(req, "classificação inexistente", 404)
    if not _substituida(reg):
        return _falha(req, "cartão não está substituído", 409)
    if reg.get("substituicao_motivo") == "cancelada" and reg.get("item_id") is not None:
        try:
            if req.ctx.ponte.item_cancelado(reg["item_id"]):
                req.ctx.ponte.alterar(reg["item_id"], status="aberto")
        except VigiaErro as e:
            log.warning("reabrir no Frentes falhou: %s", causa(e))
            return _falha(req, "Frentes indisponível; nada foi gravado", 502)
    req.ctx.banco.restaurar(cid)
    if c.pediu_json(req):
        return json_resposta({"ok": True, "id": cid, "toast": "Restaurado. O cartão voltou às Demandas."})
    return c.redirecionar(f"/demandas?restaurado=1&item={cid}")


def frente_post(req):
    """D-49: move o item para a frente escolhida e grava o vínculo `dono` da conversa. D-57: num cartão provisório, sobe o
    cartão nessa frente agora (e a decisão `confirmado` fica gravada: foi o dono que mandou subir)."""
    if (e := c.metodo_errado(req, "POST")):
        return e
    if not c.origem_ok(req):
        return c.proibido()
    cid = c.id_da_rota(req, r"/demandas/(\d+)/frente")
    reg = req.ctx.banco.classificacao(cid) if cid is not None else None
    if reg is None or (reg.get("item_id") is None and not _provisoria(reg)):
        return _falha(req, "classificação sem item", 404)
    corpo = req.corpo if isinstance(req.corpo, dict) else {}
    bruto = str(corpo.get("frente") or "")
    frentes = _frentes(req.ctx)
    if frentes is None:
        return _falha(req, "Frentes indisponível; nada foi gravado", 502)
    if not bruto.isdigit() or int(bruto) not in {f["id"] for f in frentes}:
        return _falha(req, "frente inválida", 400)
    fid = int(bruto)
    banco = req.ctx.banco
    lancou = False
    try:
        if reg.get("item_id") is None:   # D-57: escolher a frente de um provisório sobe o cartão nela
            lancamento.lancar(banco, req.ctx.ponte, reg, frente_id=fid)
            reg = banco.classificacao(cid)
            lancou = True
        else:
            req.ctx.ponte.alterar(reg["item_id"], frente_id=fid, confirmado=1)
    except VigiaErro as e:
        log.warning("mover no Frentes falhou: %s", causa(e))
        return _falha(req, "Frentes indisponível; nada foi gravado", 502)
    banco.mudar_frente_da_classificacao(reg["item_id"], fid)
    banco.vincular(reg["conversa"], fid, "dono")
    confirmou = False
    if lancou and not reg.get("decisao"):   # D-58: mover um cartão já lançado é só mover; decisão só quando ele subiu aqui
        banco.decidir(cid, "confirmado")
        confirmou = True
    nome = _nome_frente(frentes, fid)
    if c.pediu_json(req):
        return json_resposta({"ok": True, "id": cid, "frente": nome, "confirmado": confirmou,
                              "toast": (f"Subiu para o Frentes, {nome}." if confirmou else f"Movida para {nome}.")})
    volta = str(corpo.get("volta") or "")
    if not _VOLTA_OK.match(volta):
        volta = ""
    return c.redirecionar("/demandas" + (f"?{volta}&item={cid}" if volta else f"?item={cid}"))


def curar_post(req):
    """Fase A (D-45): grava a classe certa segundo o dono (precisão em /metricas). Vale para qualquer classificação,
    quantas vezes quiser (o último rótulo fica). D-57/D-58: classe que vira item sobe agora o cartão que ainda não subiu
    (o provisório; o que a IA segurou no fechamento; o aviso que o dono diz que era demanda). Num cartão já lançado é só o
    rótulo (não há o que confirmar). 404 sem registro · 400 classe fora da lista."""
    if (e := c.metodo_errado(req, "POST")):
        return e
    if not c.origem_ok(req):
        return c.proibido()
    cid = c.id_da_rota(req, r"/demandas/(\d+)/curar")
    reg = req.ctx.banco.classificacao(cid) if cid is not None else None
    if reg is None:
        return _falha(req, "classificação inexistente", 404)
    corpo = req.corpo if isinstance(req.corpo, dict) else {}
    try:
        classe = validar_classe(str(corpo.get("classe") or ""))
    except ValueError:
        return _falha(req, "classe inválida", 400)
    req.ctx.banco.curar(cid, classe)
    reg = req.ctx.banco.classificacao(cid) or reg   # depois do rótulo: o cartão segurado pela IA volta a ser provisório
    confirmou = nao_subiu = False
    if _provisoria(reg) and classe in CLASSES_ITEM and classe != "lembrando":
        try:   # o dono disse que vira item: sobe agora, com essa classe
            lancamento.lancar(req.ctx.banco, req.ctx.ponte, reg, classe=classe)
            req.ctx.banco.decidir(cid, "confirmado")
            confirmou = True
        except VigiaErro as e:
            # Auditoria D2 (lote 3 do pedido 63): a classe ficou gravada, mas o cartão NÃO subiu. Antes a tela dizia
            # só "Classe: demanda.", igual ao caso em que deu certo. O cartão continua provisório e sobe no fechamento
            # do dia (D-57), ou num novo clique.
            log.warning("subir ao rotular falhou: %s", causa(e))
            nao_subiu = True
    rot = c.ROTULO_CLASSE.get(classe, classe)
    toast = (f"Classe gravada: {rot}. Não subiu: o Frentes não respondeu. Sobe no fechamento do dia." if nao_subiu
             else f"Classe: {rot}." + (" Subiu para o Frentes." if confirmou else ""))
    if c.pediu_json(req):
        return json_resposta({"ok": True, "id": cid, "classe": classe, "rotulo": rot, "confirmado": confirmou,
                              "nao_subiu": nao_subiu, "toast": toast})
    volta = str(corpo.get("volta") or "")
    if not _VOLTA_OK.match(volta):
        volta = ""
    if nao_subiu:   # sem JavaScript o aviso vai pela query, como os outros toasts que vêm de redirecionamento
        volta = (volta + "&" if volta else "") + "aviso=nao_subiu"
    return c.redirecionar("/demandas" + (f"?{volta}" if volta else ""))


def _achar_imagem(banco, reg: dict, raiz: Path):
    """Caminho da imagem dentro de raiz (dados/caixa): absoluto, ou via mensagens.json do lote do msg_id."""
    nome = str(reg.get("imagem") or "")
    if not nome:
        return None
    alvo = None
    if Path(nome).is_absolute():
        alvo = Path(nome)
    else:
        pasta = _pasta_da_mensagem(reg, raiz)
        if pasta is not None:
            alvo = pasta / Path(nome).name
    if alvo is None:
        return None
    try:
        alvo = alvo.resolve()
        if not alvo.is_relative_to(raiz.resolve()) or alvo.suffix.lower() not in IMAGENS or not alvo.is_file():
            return None
    except (OSError, ValueError):
        return None
    return alvo


def miniatura_get(req):
    """Serve a imagem da classificação ?id= só de dentro de dados/caixa (sem traversal)."""
    if (e := c.metodo_errado(req, "GET", "HEAD")):
        return e
    try:
        reg = req.ctx.banco.classificacao(int(c.query(req, "id")))
    except ValueError:
        reg = None
    if not reg:
        return c.texto_simples("sem imagem", 404)
    alvo = _achar_imagem(req.ctx.banco, reg, c.dados_dir(req.ctx) / "caixa")
    if alvo is None:
        return c.texto_simples("imagem indisponível (já limpa pelo fechamento semanal?)", 404)
    return Resposta(200, alvo.read_bytes(), IMAGENS[alvo.suffix.lower()],
                    [("Cache-Control", "private, max-age=300"), ("X-Content-Type-Options", "nosniff")])


ROTAS = {"/demandas": demandas_get, "/demandas/miniatura": miniatura_get,
         "/demandas/{id}/detalhe": detalhe_get,
         "/demandas/{id}/confirmar": confirmar_post, "/demandas/{id}/recusar": recusar_post,
         "/demandas/{id}/desfazer": desfazer_post, "/demandas/{id}/restaurar": restaurar_post,
         "/demandas/{id}/curar": curar_post, "/demandas/{id}/frente": frente_post}
