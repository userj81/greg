"""Comando `vigia-ponte atualizar`: anota um item aberto do WhatsApp (nota no vigia.db; prioridade no Frentes).
Quem chama: bin/vigia-ponte (via prompts/vigia.md).
Só item aberto de origem whatsapp. Nunca rebaixa prioridade. A nota NÃO vai ao Frentes (D-13): fica numa
classificação `lembrando` gravada com o msg_id REAL da mensagem (`--msg`, obrigatório na CLI); conversa, contato e ts
vêm do manifesto do lote (VIGIA_LOTE) por msg_id, como no `classificar`. UNIQUE(msg_id) torna a chamada idempotente:
repetir o mesmo msg_id não cria linha nova nem toca o Frentes. Sem VIGIA_LOTE (uso manual) herda a conversa/contato
da última classificação do item.
O que não faz: não cria item, não toca a Evolution.
"""
import time

from app.ponte import _comum


def configurar(sub):
    p = sub.add_parser("atualizar", help="anota um item existente do WhatsApp")
    p.add_argument("item_id", type=int)
    p.add_argument("nota")
    p.add_argument("--prioridade", type=int, choices=(1, 2), default=None)
    p.add_argument("--msg", required=True, help="msg_id da mensagem que motivou a nota (o [id:...] do conversa.md)")
    return p


def executar(args, *, banco, ponte) -> dict:
    nota = _comum.validar_resumo(args.nota)
    if not nota:
        raise ValueError("nota vazia")
    prioridade = getattr(args, "prioridade", None)
    if prioridade is not None and int(prioridade) not in (1, 2):
        raise ValueError("prioridade deve ser 1 ou 2")
    item_id = int(args.item_id)
    item = _comum.item_whatsapp_aberto(ponte, item_id)
    if item is None:
        raise ValueError("item não está aberto entre os itens do WhatsApp")
    enviar = _comum.prioridade_a_enviar(item, prioridade)

    msg_id = str(getattr(args, "msg", None) or "").strip()
    if msg_id:
        com_lote, dados = _comum.procurar_no_manifesto(msg_id)
        if com_lote:
            if not dados:
                raise ValueError("msg_id fora do lote")
            conversa, contato, ts = dados.get("conversa"), dados.get("contato"), dados.get("ts")
            if not conversa or isinstance(ts, bool) or not isinstance(ts, int):
                raise ValueError("manifesto do lote inválido para este msg_id")
            contato = str(contato or "").strip() or "contato"
        else:
            origem = next((c for c in banco.classificacoes(limite=1000) if c.get("item_id") == item_id), None)
            conversa = (origem or {}).get("conversa") or "atualizar"
            contato = (origem or {}).get("contato") or "contato"
            ts = int(time.time())
        anterior = banco.classificacao_por_msg_id(msg_id)
        if anterior is not None:  # idempotência: não toca o Frentes
            return {"ok": True, "repetido": True, "item_id": item_id, "msg_id": msg_id, "prioridade": None}
    else:  # chamada programática antiga (a CLI exige --msg): id sintético, sem vínculo com a mensagem
        origem = next((c for c in banco.classificacoes(limite=1000) if c.get("item_id") == item_id), None)
        ts = int(time.time())
        msg_id = f"atualizar:{item_id}:{ts}"
        n = 0
        while banco.classificacao_por_msg_id(msg_id) is not None:  # mesma passada de segundo: não sobrescreve
            n += 1
            msg_id = f"atualizar:{item_id}:{ts}:{n}"
        conversa, contato = (origem or {}).get("conversa") or "atualizar", (origem or {}).get("contato") or "vigia"
    banco.gravar_classificacao(
        msg_id=msg_id, conversa=str(conversa), contato=contato, ts=ts, classe="lembrando", resumo=nota,
        confianca="alta", item_id=item_id)
    ponte.anotar(item_id, nota, prioridade=enviar)
    return {"ok": True, "repetido": False, "item_id": item_id, "msg_id": msg_id, "prioridade": enviar}
