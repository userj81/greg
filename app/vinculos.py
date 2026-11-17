"""Vínculo conversa → frente (D-49): sugestão pelo nome da agenda, semeadura e aprendizado pelo que o dono faz no Frentes.
Quem chama: bin/coletor.py (semear + aprender a cada ciclo), app/paginas/contatos.py (página de marcar), app/lotes.py
(cabeçalho do lote) e app/ponte/classificar.py (frente padrão do item).
O que não faz: não fala com a rede além da ponte do Frentes recebida; não lê conteúdo de mensagem (só nome de contato
e assunto de grupo); `auto` nunca sobrescreve o que o dono marcou.
"""
import logging
import re
import unicodedata
from app.erros import causa

log = logging.getLogger("vigia.vinculos")

# Termo de frente que sozinho NÃO basta para vincular: primeiro nome comum, palavra genérica ou que aparece em nome de
# contato fora da frente (medido na agenda em 08/11/2026: "Bella Massas", "Helena Costa - Época", "Rei Suplementos",
# "Agência Ponto Certo"). Casar só com estes vira sugestão FRACA: aparece na página, não entra no vínculo.
FRACOS = frozenset({
    "eu", "meu", "minha", "dono", "pessoal", "saude", "nutri", "dieta", "exames", "academia", "treino", "suplementos",
    "sistema", "painel", "estoque", "despesas", "frentes", "agencia", "proposta", "folha", "infra", "servidor", "proxy",
    "recepcao", "evolution", "laboratorio", "coordenador", "loja", "clinica",
    "ana", "maria", "joao", "jose", "helena", "paulo", "carlos", "pedro", "lucas", "marcos", "bella", "rei",
})
MIN_TERMO = 3


def normalizar(s) -> str:
    s = "".join(c for c in unicodedata.normalize("NFD", str(s or "")) if unicodedata.category(c) != "Mn")
    return " ".join(re.sub(r"[^0-9a-z]+", " ", s.lower()).split())


def _termos(frente: dict) -> list[str]:
    vistos, saida = set(), []
    for t in [frente.get("nome") or ""] + list(frente.get("apelidos") or []):
        n = normalizar(t)
        if len(n) >= MIN_TERMO and n not in vistos:
            vistos.add(n)
            saida.append(n)
    return saida


def sugerir(nome: str, frentes: list[dict]) -> dict | None:
    """{"frente_id", "termo", "forca": "forte"|"fraca"} ou None. Casa palavra inteira do nome com nome/apelido da
    frente. Forte = termo de várias palavras ou fora de FRACOS. Entre fortes de frentes diferentes vence a que não é
    tipo `empresa` (a agência atende o cliente: o grupo "VELTA - EXEMPLO MKT" é do cliente), depois o termo mais longo;
    empate de verdade entre frentes = sem sugestão."""
    alvo = f" {normalizar(nome)} "
    if not alvo.strip():
        return None
    achados = []
    for f in frentes or []:
        melhor = None
        for t in _termos(f):
            if f" {t} " in alvo:
                forte = " " in t or t not in FRACOS
                chave = (forte, len(t))
                if melhor is None or chave > melhor[0]:
                    melhor = (chave, t)
        if melhor:
            (forte, tam), termo = melhor
            achados.append({"frente_id": f.get("id"), "termo": termo, "forca": "forte" if forte else "fraca",
                            "_ordem": (forte, f.get("tipo") != "empresa", tam)})
    if not achados:
        return None
    achados.sort(key=lambda a: a["_ordem"], reverse=True)
    if len(achados) > 1 and achados[0]["_ordem"] == achados[1]["_ordem"] \
            and achados[0]["frente_id"] != achados[1]["frente_id"]:
        return None
    a = achados[0]
    return {"frente_id": a["frente_id"], "termo": a["termo"], "forca": a["forca"]}


def semear(banco, nomes: dict, frentes: list[dict]) -> int:
    """Para cada {jid: nome} da agenda, grava `auto` quando a sugestão é forte. Nunca toca vínculo do dono.
    Sugestão que deixou de existir (frente renomeada) apaga o `auto`. Devolve quantos mudaram."""
    atuais = banco.vinculos()
    mudou = 0
    for jid, nome in nomes.items():
        s = sugerir(nome, frentes)
        atual = atuais.get(jid)
        if s and s["forca"] == "forte":
            if banco.vincular(jid, s["frente_id"], "auto"):
                mudou += 1
        elif atual is not None and atual["origem"] == "auto":
            banco.desvincular(jid)
            mudou += 1
    return mudou


def frente_da_conversa(banco, conversa: str, jid_alt: str | None = None) -> dict | None:
    """Vínculo com frente (não nulo) da conversa ou do jid alternativo (@lid ↔ número)."""
    vinc = getattr(banco, "vinculo", None)
    if not callable(vinc):
        return None
    for j in (conversa, jid_alt):
        if j:
            v = vinc(j)
            if v is not None:
                return v if v.get("frente_id") is not None else None
    return None


def aprender_do_frentes(banco, ponte) -> int:
    """Item do WhatsApp que o dono moveu de frente NO Frentes vira vínculo `dono` da conversa (D-49, sem clique a mais).
    Compara a frente atual do item com a gravada na classificação; diferente e não nula = o dono mudou."""
    try:
        abertos = ponte.itens_abertos(origem="whatsapp", dias=30)
    except Exception as e:   # Frentes fora não derruba o ciclo
        log.warning("aprender: Frentes indisponível (%s)", causa(e))
        return 0
    por_item = {}
    for c in banco.classificacoes(limite=2000):
        if c.get("item_id") is not None and c.get("classe") != "lembrando":
            por_item.setdefault(c["item_id"], c)
    aprendidos = 0
    for it in abertos:
        c = por_item.get(it.get("id"))
        fid = it.get("frente_id")
        if c is None or fid is None or fid == c.get("frente_id"):
            continue
        banco.vincular(c["conversa"], fid, "dono")
        banco.mudar_frente_da_classificacao(it["id"], fid)
        aprendidos += 1
    return aprendidos
