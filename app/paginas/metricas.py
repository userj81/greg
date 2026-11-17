"""Página /metricas (fase A, D-45): o que o Greg mede sobre si mesmo — precisão da classificação (rótulos do dono),
custo e duração dos ciclos, quedas da conexão, QRs, envios e itens no Frentes. HTML, ou JSON com Accept/`?formato=json`.
Quem chama: app/server.py (via ROTAS). O que não faz: não mostra texto de mensagem nem contato, só contagens;
não chama Evolution nem Frentes (lê só o vigia.db).
"""
import time

from app.classes import CLASSES
from app.http import json_resposta
from app.paginas import _comum as c

DIAS_PADRAO, DIAS_MAX = 14, 90
META_ROTULOS = 100        # meta da fase A: 100 classificações rotuladas (docs/specs/PLANO-FASE-A.md)
JANELAS = (7, 14, 30, 90)


def _dias(req) -> int:
    v = c.query(req, "dias", str(DIAS_PADRAO))
    try:
        return max(1, min(DIAS_MAX, int(v)))
    except ValueError:
        return DIAS_PADRAO


def _quer_json(req) -> bool:
    cab = req.cabecalhos or {}
    return "json" in str(cab.get("accept") or cab.get("Accept") or "") or c.query(req, "formato") == "json"


def coletar(banco, dias: int) -> dict:
    """Números do período (só vigia.db). Tudo contagem/soma: nenhum texto de conversa."""
    por_dia = banco.metricas_lotes_por_dia(dias)
    cur = banco.metricas_curadoria(dias)
    eventos = banco.eventos_conexao(dias=dias)
    qrs = banco.qrs_por_dia(dias)
    envios = banco.envios_por_dia(dias)
    soma = {k: sum(int(r[k] or 0) for r in por_dia)
            for k in ("lotes", "conversas", "novas", "itens", "atencao", "tokens_in", "tokens_out")}
    custo = sum(float(r["custo_usd"] or 0) for r in por_dia)
    com_custo = sum(int(r["com_custo"] or 0) for r in por_dia)
    duracoes = [float(r["duracao_ms"]) for r in por_dia if r.get("duracao_ms")]
    n_dias = max(1, len(por_dia))
    quedas = sum(1 for e in eventos if e["evento"] in ("queda", "socket_morto"))
    precisao = round(100 * cur["acertos"] / cur["curadas"]) if cur["curadas"] else None
    return {
        "dias": dias, "gerado_em": int(time.time()),
        "ciclos": {**soma, "dias_com_lote": len(por_dia), "custo_usd": round(custo, 4),
                   "lotes_com_custo": com_custo,
                   "custo_usd_dia": round(custo / n_dias, 4) if com_custo else None,
                   "tokens_dia": round((soma["tokens_in"] + soma["tokens_out"]) / n_dias),
                   "duracao_ms_media": round(sum(duracoes) / len(duracoes)) if duracoes else None},
        "precisao": {"pct": precisao, "meta_rotulos": META_ROTULOS, **{k: cur[k] for k in
                     ("total", "curadas", "acertos", "itens", "confirmados", "recusados", "matriz", "por_classe")}},
        "conexao": {"quedas": quedas, "eventos": eventos, "qrs": sum(int(r["n"]) for r in qrs), "qrs_por_dia": qrs},
        "envios": {"total": sum(int(r["n"]) for r in envios), "api": sum(int(r["api"] or 0) for r in envios),
                   "por_dia": envios},
        "por_dia": por_dia,
    }


# ----------------------------------------------------------------------------- HTML

def _usd(v) -> str:
    return "–" if v is None else f"US$ {v:.4f}".replace(".", ",")


def _seg(ms) -> str:
    return "–" if not ms else f"{ms / 1000:.0f} s"


def _n(v) -> str:
    return f"{int(v or 0):,}".replace(",", ".")


def _pl(v, um: str, muitos: str) -> str:
    return f"{_n(v)} {um if int(v or 0) == 1 else muitos}"


def _tabela_dias(por_dia: list[dict]) -> str:
    if not por_dia:
        return c.vazio("Nenhum ciclo com Claude no período", "Sem mensagem nova não há chamada nem token")
    linhas = "".join(
        f"<tr><td>{c.esc(r['dia'])}</td><td class=\"num\">{_n(r['lotes'])}</td><td class=\"num\">{_n(r['conversas'])}</td><td class=\"num\">{_n(r['novas'])}</td>"
        f"<td class=\"num\">{_n(r['itens'])}</td><td class=\"num\">{_n(r['atencao'])}</td><td class=\"num\">{_n(r['tokens_in'])}</td><td class=\"num\">{_n(r['tokens_out'])}</td>"
        f"<td class=\"num\">{_usd(r['custo_usd']) if r['com_custo'] else '–'}</td><td class=\"num\">{_seg(r['duracao_ms'])}</td></tr>"
        for r in por_dia)
    return ('<table class="tabela"><thead><tr><th>Dia</th><th class="num">Ciclos</th><th class="num">Conversas</th><th class="num">Novas</th>'
            '<th class="num">Itens</th><th class="num">Atenção</th><th class="num">Tokens in</th><th class="num">Tokens out</th>'
            '<th class="num">Custo nocional</th><th class="num">Duração média</th></tr></thead>'
            f"<tbody>{linhas}</tbody></table>")


def _matriz(prec: dict) -> str:
    if not prec["curadas"]:
        return c.vazio("Nenhuma classificação rotulada ainda", "Em Demandas, o pop-up Classe de cada item grava o rótulo")
    n = {(m["classe"], m["classe_curada"]): m["n"] for m in prec["matriz"]}
    cab = "".join(f"<th>{c.esc(c.rotulo_classe(k))}</th>" for k in CLASSES)
    linhas = ""
    for prev in CLASSES:
        celulas = "".join(
            f'<td class="{"ok" if prev == cur_ and n.get((prev, cur_)) else ""}">{n.get((prev, cur_), "") or "·"}</td>'
            for cur_ in CLASSES)
        linhas += f"<tr><td>{c.esc(c.rotulo_classe(prev))}</td>{celulas}</tr>"
    por_classe = "".join(
        f"<tr><td>{c.esc(c.rotulo_classe(r['classe']))}</td><td class=\"num\">{_n(r['n'])}</td><td class=\"num\">{_n(r['curadas'])}</td>"
        f"<td class=\"num\">{(str(round(100 * r['acertos'] / r['curadas'])) + '%') if r['curadas'] else '–'}</td></tr>"
        for r in prec["por_classe"])
    return (f'<p class="meta-linha legenda">Linha: o que a IA previu. Coluna: o que você rotulou. Diagonal: acerto.</p>'
            f'<table class="tabela matriz"><thead><tr><th>prevista</th>{cab}</tr></thead><tbody>{linhas}</tbody></table>'
            f'<p class="rotulo">Por classe prevista</p><table class="tabela"><thead><tr><th>Classe</th><th class="num">Previstas</th>'
            f'<th class="num">Rotuladas</th><th class="num">Precisão</th></tr></thead><tbody>{por_classe}</tbody></table>')


def _eventos(cx: dict) -> str:
    if not cx["eventos"]:
        return c.vazio("Nenhuma queda registrada no período")
    linhas = "".join(
        f"<tr><td>{c.esc(e['criado_em'])}</td><td>{c.esc(e['evento'])}</td><td>{c.esc(e['estado'])}</td>"
        f"<td class=\"meta\">{c.esc(e.get('motivo') or '')}{(', código ' + c.esc(e['reason_code'])) if e.get('reason_code') is not None else ''}</td></tr>"
        for e in cx["eventos"][:40])
    return ('<table class="tabela"><thead><tr><th>Quando</th><th>Evento</th><th>Estado</th><th>Motivo</th></tr></thead>'
            f"<tbody>{linhas}</tbody></table>")


def _janelas(dias: int) -> str:
    """Pull-down do período na toolbar (guia Papel: nada de links separados por ponto no título)."""
    itens = "".join(
        f'<a role="menuitem"{" class=" + chr(34) + "on" + chr(34) if d == dias else ""} href="/metricas?dias={d}"><span>Últimos {d} dias</span></a>'
        for d in JANELAS)
    return c.pulldown(f"<span>Últimos {dias} dias</span>", itens, id_="mt-janela", direita=True, aria="Período")


def metricas_get(req):
    if (e := c.metodo_errado(req, "GET", "HEAD")):
        return e
    dias = _dias(req)
    m = coletar(req.ctx.banco, dias)
    if _quer_json(req):
        return json_resposta(m)
    ci, pr, cx, en = m["ciclos"], m["precisao"], m["conexao"], m["envios"]
    pct = f"{pr['pct']}%" if pr["pct"] is not None else "–"
    cls_pct = "" if pr["pct"] is None else ("ok" if pr["pct"] >= 85 else "ruim")
    kv = c.kv([
        ("Precisão da IA", f'<b class="{cls_pct}">{pct}</b><span class="sub">{_pl(pr["acertos"], "acerto", "acertos")} em {_pl(pr["curadas"], "rotulada", "rotuladas")}, '
                           f'meta {_n(pr["meta_rotulos"])} rótulos, {_n(pr["total"])} classificadas</span>'),
        ("Custo nocional por dia", f'<b>{_usd(ci["custo_usd_dia"])}</b><span class="sub">{_n(ci["tokens_dia"])} tokens por dia, total '
                                   f'{_usd(ci["custo_usd"] if ci["lotes_com_custo"] else None)}, como se fosse API (a assinatura cobre)</span>'),
        ("Ciclos com Claude", f'<b>{_n(ci["lotes"])}</b><span class="sub">em {_pl(ci["dias_com_lote"], "dia", "dias")}, {_n(ci["novas"])} mensagens de '
                              f'{_n(ci["conversas"])} conversas, {_seg(ci["duracao_ms_media"])} por ciclo</span>'),
        ("Quedas da conexão", f'<b class="{"ok" if not cx["quedas"] else "ruim"}">{_n(cx["quedas"])}</b><span class="sub">{_pl(cx["qrs"], "QR gerado", "QRs gerados")} no período</span>'),
        ("Para o Frentes", f'<b>{_n(pr["itens"])}</b><span class="sub">{_n(pr["confirmados"])} confirmados, {_n(pr["recusados"])} recusados, {_n(ci["atencao"])} atenção</span>'),
        ("Envios", f'<b>{_n(en["total"])}</b><span class="sub">{_n(en["api"])} pela API das ferramentas</span>'),
    ])
    miolo = c.render("metricas.html", kv=kv, tabela_dias=_tabela_dias(m["por_dia"]), matriz=_matriz(pr), eventos=_eventos(cx))
    toolbar = c.toolbar(c.botao_icone("voltar", "Voltar para a Conexão", href="/conexao"), c.titulo("Métricas"), c.espaco(), _janelas(dias))
    painel = c.painel(miolo, toolbar_html=toolbar, aria="Métricas",
                      status_html=c.rodape(c.esc(f"JSON em /metricas?formato=json&dias={dias}. Só contagens: nenhum texto de mensagem ou contato")))
    return c.pagina("Métricas", painel, "/conexao", ctx=req.ctx)


ROTAS = {"/metricas": metricas_get}
