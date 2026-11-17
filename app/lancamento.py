"""Lançamento adiado e substituição de cartões (D-57, pedido 45) e correção do item já lançado (D-58, pedido 46):
quando um cartão sobe ao Frentes, como, e como a IA corrige o que já subiu.
Quem chama: app/ponte/classificar.py (na hora, para atenção e prazo em 24 h; `--substitui`), app/ponte/consolidar.py
(fechamento do dia fundindo num cartão já lançado), app/paginas/demandas.py (Subir agora, Frente e Classe sobem o cartão
provisório), app/consolidacao.py (fechamento do dia) e app/lotes.py (estado do cartão no contexto da IA).
O que não faz: não decide classe nem frente, não fala com o Frentes além de `capturar`/`alterar`, não loga texto de
mensagem. D-58: nada nasce "a confirmar"; a confiança é só registro.
"""
from datetime import date, datetime, time as hora_t, timedelta

from app import secoes
from app.classes import CLASSES_ITEM, mapa_frentes, texto_item
from app.erros import FrentesIndisponivel

HORA_FECHAMENTO_PADRAO = "21:00"   # [horarios].diario ausente ou inválido
JANELA_PRAZO_DIAS = 1              # prazo até amanhã sobe na hora (decisão reversível, D-57)
MARCA_CORRECAO = " · atualizado: "  # D-58: a correção da IA entra depois das palavras do dono, sem apagá-las
LIMITE_TEXTO_CORRIGIDO = 400
_MESMA = object()


def classe_efetiva(reg: dict) -> str:
    """O rótulo do dono (classe_curada), senão a classe da IA: é ela que decide se o cartão vira item."""
    return str(reg.get("classe_curada") or reg.get("classe") or "")


def eh_provisoria(reg: dict) -> bool:
    """Cartão que ainda vai subir ao Frentes: classe efetiva que vira item, sem item, não lançado, sem decisão do dono e
    não substituído."""
    return (classe_efetiva(reg) in CLASSES_ITEM and reg.get("item_id") is None and not reg.get("lancado_em")
            and not reg.get("decisao") and reg.get("substituida_por") is None)


def texto_do_cartao(contato, resumo, prazo=None, classe: str = "item") -> str:
    """O texto do item no Frentes: `WA contato: resumo` (+ `(prazo AAAA-MM-DD)` quando o prazo não está no resumo).
    É o mesmo texto que a ponte sempre mandou; serve também para saber se o dono editou o item (texto diferente)."""
    texto = texto_item(contato or "?", resumo or classe)
    if prazo and str(prazo) not in texto:
        texto = texto_item(contato or "?", f"{resumo or classe} (prazo {prazo})")
    return texto


def _uma_linha(s) -> str:
    return " ".join(str(s or "").split())


def editado_pelo_dono(item: dict, velho: dict) -> bool:
    """O texto do item no Frentes difere do que a ponte escreveu para o cartão `velho`: o dono mexeu nas palavras."""
    esperado = texto_do_cartao(velho.get("contato"), velho.get("resumo"), velho.get("prazo"), str(velho.get("classe") or "item"))
    return _uma_linha(item.get("texto")) != esperado


def corrigir_item(ponte, item: dict, velho: dict, contato, resumo, prazo) -> str:
    """D-58 ("ela corrige, ela lança uma nova versão"): o item ABERTO do cartão `velho` ganha a versão nova.
    Texto que a ponte escreveu → trocado inteiro ("atualizado"). Texto que o dono editou → as palavras dele ficam e a
    correção entra depois (` · atualizado: …`; só uma: a anterior é trocada) ("corrigido_editado"). Prazo novo, se veio,
    vale nos dois casos. Não abre nem fecha item. Frentes fora levanta FrentesIndisponivel antes de qualquer gravação."""
    classe = str(velho.get("classe") or "item")
    if editado_pelo_dono(item, velho):
        base = _uma_linha(item.get("texto")).split(MARCA_CORRECAO)[0].rstrip()
        nota = _uma_linha(resumo) or classe
        if prazo and str(prazo) not in nota:
            nota = f"{nota} (prazo {prazo})"
        texto = base + MARCA_CORRECAO + nota
        if len(texto) > LIMITE_TEXTO_CORRIGIDO:
            texto = texto[:LIMITE_TEXTO_CORRIGIDO - 1].rstrip() + "…"
        rotulo = "corrigido_editado"
    else:
        texto, rotulo = texto_do_cartao(contato, resumo, prazo, classe), "atualizado"
    campos = {"texto": texto}
    if prazo:
        campos["prazo"] = prazo
    ponte.alterar(int(item["id"]), **campos)
    return rotulo


def lancado_aberto(reg: dict) -> bool:
    """Cartão que já está no Frentes e a IA ainda pode corrigir: classe efetiva que vira item, com item, não substituído e
    não recusado pelo dono. (Se o item ainda está aberto, só o Frentes sabe: quem chama confere.)"""
    return (classe_efetiva(reg) in CLASSES_ITEM and reg.get("item_id") is not None
            and reg.get("substituida_por") is None and reg.get("decisao") != "recusado")


def lanca_na_hora(classe: str, prazo, hoje: date | None = None) -> bool:
    """D-57: atenção sobe na hora; prazo até amanhã também; o resto espera o fechamento do dia (ou o dono mandar subir)."""
    if classe == "atencao":
        return True
    if not prazo:
        return False
    hoje = hoje or date.today()
    try:
        return date.fromisoformat(str(prazo)[:10]) <= hoje + timedelta(days=JANELA_PRAZO_DIAS)
    except ValueError:
        return False


def lancar(banco, ponte, reg: dict, *, frente_id=_MESMA, classe: str | None = None) -> dict:
    """Sobe o cartão ao Frentes agora (`POST /api/itens`) e grava item_id/lancado_em no SQLite.
    D-58: sobe sempre confirmado, com qualquer confiança (a IA decide; o painel é revisão, não portão). Frentes fora
    levanta FrentesIndisponivel e o cartão continua provisório (sobe no próximo fechamento).
    Devolve {"item_id", "frente", "frente_invalida"}."""
    classe = classe or classe_efetiva(reg)
    mapa = mapa_frentes(classe)
    if mapa is None:
        raise ValueError("classe não vira item")
    frente = reg.get("frente_id") if frente_id is _MESMA else frente_id
    prazo = reg.get("prazo")
    texto = texto_do_cartao(reg.get("contato"), reg.get("resumo"), prazo, classe)
    r = ponte.capturar(texto, frente_id=frente, tipo=mapa["tipo"], prazo=prazo, prioridade=mapa["prioridade"])
    invalida = bool((r or {}).get("frente_invalida"))
    if invalida:
        frente = None
    itens = (r or {}).get("itens") or []
    item_id = itens[0].get("id") if itens else None
    if item_id is None:
        raise FrentesIndisponivel("Frentes não devolveu o item criado")
    # D-58: nada nasce "a confirmar". O item sobe confirmado (padrão do Frentes para origem whatsapp) seja qual for a
    # confiança; `confianca` fica no vigia.db como registro (frente arquivada = Inbox + baixa), sem segurar nada.
    banco.marcar_lancado(reg["id"], item_id, frente_id=frente, confianca="baixa" if invalida else None)
    # pedido 79: a seção que a IA escolheu vale se ainda for da frente em que o item caiu (o cartão pode ter mudado
    # de frente na revisão). Falha aqui não derruba o lançamento: o item já subiu, só fica sem seção.
    secao = secoes.valida_para(banco, reg.get("secao_id"), frente)
    if secao is not None:
        try:
            banco.por_na_secao(int(item_id), secao, origem="ia")
        except Exception:  # noqa: BLE001
            secao = None
    return {"item_id": int(item_id), "frente": frente, "frente_invalida": invalida, "secao": secao}


def hora_fechamento(config: dict | None) -> hora_t:
    """[horarios].diario como `time`; inválido ou ausente = 21:00."""
    bruto = str(((config or {}).get("horarios") or {}).get("diario") or HORA_FECHAMENTO_PADRAO)
    try:
        return datetime.strptime(bruto.strip(), "%H:%M").time()
    except ValueError:
        return datetime.strptime(HORA_FECHAMENTO_PADRAO, "%H:%M").time()


def fechamento_do_dia(agora: datetime, config: dict | None) -> datetime:
    """O fechamento de hoje (data de `agora` + hora de [horarios].diario)."""
    return datetime.combine(agora.date(), hora_fechamento(config))


def rotulo_fechamento(config: dict | None) -> str:
    """'21:00' para as telas."""
    return hora_fechamento(config).strftime("%H:%M")
