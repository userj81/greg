"""Leitura do histórico de análise (D-58, pedido 46): o que cada conversa trouxe e em que pé ficou, para responder
"o que aconteceu essa semana?", "quem está falando comigo?" e "ficou alguém sem resposta?".
Quem chama: bin/historico.py (bin/vigia.sh historico) e a página /historico do painel (app/paginas/historico.py).
Quem grava as linhas: o Claude da leitura, uma por conversa por lote, com `vigia-ponte historico` (app/ponte/historico.py).
O que não faz: não lê mensagem crua (só a tabela `analises` e a contagem de cartões do vigia.db), não chama Evolution,
Frentes nem Claude, não escreve nada.
"""
import time
from datetime import datetime

from app import lancamento

DIAS_PADRAO = 7
DIAS_MAX = 90
LIMITE = 5000   # análises lidas por consulta (a página pede menos e avisa quando corta)
ROTULO_SITUACAO = {"aguarda_dono": "aguarda você", "aguarda_contato": "aguarda o contato", "encerrada": "encerrada"}
DIAS_SEMANA = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")


def _filtra(linhas: list[dict], conversa: str | None) -> list[dict]:
    """`conversa` = pedaço do nome do contato (sem diferenciar maiúscula) ou o jid inteiro."""
    if not conversa:
        return linhas
    alvo = conversa.strip().casefold()
    return [a for a in linhas if alvo in str(a.get("contato") or "").casefold() or alvo == str(a.get("conversa") or "").casefold()]


def _linha(a: dict, atual: bool = True) -> dict:
    """`atual` = é a análise mais recente da conversa (a que diz em que pé ela está hoje)."""
    return {"quando": datetime.fromtimestamp(int(a["ts"])).strftime("%Y-%m-%d %H:%M"), "ts": int(a["ts"]),
            "contato": a.get("contato") or "?", "conversa": a.get("conversa"), "situacao": a.get("situacao"),
            "resumo": a.get("resumo") or "", "atual": atual}


def _cartoes(banco, desde: int, conversas: set | None) -> dict:
    """Contagem dos cartões do período: quantos subiram ao Frentes, quantos a IA segurou, quantos ainda vão subir."""
    regs = banco.classificacoes(desde=desde, limite=5000, substituidas=None)
    if conversas is not None:
        regs = [r for r in regs if r.get("conversa") in conversas]
    vivos = [r for r in regs if r.get("substituida_por") is None]
    return {"total": len(vivos),
            "no_frentes": sum(1 for r in vivos if r.get("item_id") is not None and r.get("decisao") != "recusado"),
            "segurados": sum(1 for r in vivos if r.get("rebaixada_em") and r.get("item_id") is None
                             and lancamento.classe_efetiva(r) not in lancamento.CLASSES_ITEM),
            "provisorios": sum(1 for r in vivos if lancamento.eh_provisoria(r)),
            "recusados": sum(1 for r in vivos if r.get("decisao") == "recusado"),
            "avisos": sum(1 for r in vivos if not r.get("rebaixada_em") and lancamento.classe_efetiva(r) == "aviso")}


def cartoes_da_conversa(banco, conversa: str, *, dias: int = DIAS_PADRAO, agora: float | None = None) -> dict:
    """A mesma contagem de cartões do período, só de UMA conversa (jid exato). Usada pela página /historico."""
    dias = max(1, min(DIAS_MAX, int(dias)))
    agora = time.time() if agora is None else agora
    return _cartoes(banco, int(agora) - dias * 86400, {conversa})


def montar(banco, *, dias: int = DIAS_PADRAO, agora: float | None = None, conversa: str | None = None,
           limite: int = LIMITE) -> dict:
    """O histórico dos últimos `dias` dias (1 a 90). Devolve um dicionário pronto para JSON:
    periodo · analises (mais recente primeiro; `atual` marca a última de cada conversa) · aguardando_dono /
    aguardando_contato / encerradas (a ÚLTIMA análise de cada conversa no período é que diz o pé em que ela ficou) ·
    conversas (quantas apareceram) · cartoes (contagens) · cortado (havia mais análises que `limite`)."""
    dias = max(1, min(DIAS_MAX, int(dias)))
    agora = time.time() if agora is None else agora
    desde = int(agora) - dias * 86400
    limite = max(1, int(limite))
    brutas = banco.analises(desde=desde, limite=limite + 1)
    linhas = _filtra(brutas[:limite], conversa)
    ultimas = _filtra(banco.ultima_analise_por_conversa(desde=desde, limite=LIMITE), conversa)
    atuais = {a.get("id") for a in ultimas}
    jids = {a.get("conversa") for a in linhas}

    def de(situacao):
        return [_linha(a) for a in ultimas if a.get("situacao") == situacao]

    return {"periodo": {"dias": dias, "de": datetime.fromtimestamp(desde).strftime("%Y-%m-%d"),
                        "ate": datetime.fromtimestamp(agora).strftime("%Y-%m-%d")},
            "conversas": len(jids),
            "analises": [_linha(a, a.get("id") in atuais) for a in linhas],
            "aguardando_dono": de("aguarda_dono"), "aguardando_contato": de("aguarda_contato"), "encerradas": de("encerrada"),
            "cartoes": _cartoes(banco, desde, jids if conversa else None),
            "cortado": len(brutas) > limite}


def _br(dia_iso: str) -> str:
    d = datetime.strptime(dia_iso, "%Y-%m-%d")
    return f"{d:%d/%m} ({DIAS_SEMANA[d.weekday()]})"


def texto(h: dict, *, so_pendentes: bool = False) -> str:
    """O histórico em texto corrido, para ler no terminal ou para o Claude ler e responder ao dono."""
    p = h["periodo"]
    n = len(h["analises"])
    saida = [f"Histórico de análise · últimos {p['dias']} dia(s) ({_br(p['de'])} a {_br(p['ate'])}) · "
             f"{n} análise(s) em {h['conversas']} conversa(s)", ""]
    if not n:
        saida.append("Nenhuma análise gravada neste período. O histórico é gravado a cada leitura desde 10/11/2026 (D-58).")
        return "\n".join(saida) + "\n"
    for titulo, chave in (("Aguardando você", "aguardando_dono"), ("Aguardando o contato", "aguardando_contato")):
        itens = h[chave]
        saida.append(f"{titulo} ({len(itens)})")
        saida += [f"- {a['contato']} · desde {_br(a['quando'][:10])} {a['quando'][11:]} · {a['resumo']}" for a in itens] or ["- ninguém"]
        saida.append("")
    if not so_pendentes:
        saida.append("Dia a dia")
        dia = None
        for a in h["analises"]:
            if a["quando"][:10] != dia:
                dia = a["quando"][:10]
                saida.append(_br(dia))
            saida.append(f"  {a['quando'][11:]} · {a['contato']} · {a['resumo']} [{ROTULO_SITUACAO.get(a['situacao'], a['situacao'])}]")
        saida.append("")
    c = h["cartoes"]
    saida.append(f"Cartões no período: {c['total']} · {c['no_frentes']} no Frentes · {c['segurados']} segurados pela IA · "
                 f"{c['provisorios']} ainda vão subir · {c['avisos']} avisos · {c['recusados']} recusados por você")
    return "\n".join(saida) + "\n"
