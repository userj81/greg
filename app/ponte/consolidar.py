"""Comando `vigia-ponte consolidar` (D-57/D-58): no fechamento do dia, funde cartões repetidos da mesma conversa.
`consolidar <fica> --absorve ID [ID ...] [--resumo 'texto final'] [--prazo AAAA-MM-DD]`: os absorvidos (sempre
PROVISÓRIOS) passam a substituídos (motivo `consolidada`) pelo que fica; o que fica pode ganhar resumo e prazo finais.
D-58: o que fica pode ser um cartão que JÁ ESTÁ no Frentes (item aberto); aí `--resumo`/`--prazo` corrigem o item (versão
nova), em vez de subir um segundo item para o mesmo assunto.
Quem chama: bin/vigia-ponte, acionado pelo Claude do fechamento (prompts/consolidar.md, app/consolidacao.py).
O que não faz: não absorve cartão com item, decidido pelo dono ou já substituído; não cria, apaga nem fecha nada no
Frentes; não cria nem apaga cartão.
"""
from app import lancamento
from app.ponte import _comum


def configurar(sub):
    p = sub.add_parser("consolidar", help="fecha o dia: funde cartões provisórios repetidos da mesma conversa")
    p.add_argument("fica", type=int, help="id do cartão que fica")
    p.add_argument("--absorve", type=int, nargs="+", required=True, help="ids dos cartões absorvidos (mesma conversa)")
    p.add_argument("--resumo", default=None, help="resumo final do cartão que fica (opcional)")
    p.add_argument("--prazo", default=None, help="prazo final AAAA-MM-DD (opcional)")
    return p


def executar(args, *, banco, ponte) -> dict:
    fica = int(args.fica)
    absorve = sorted({int(i) for i in (getattr(args, "absorve", None) or [])})
    if not absorve:
        raise ValueError("informe ao menos um cartão em --absorve")
    if fica in absorve:
        raise ValueError("o cartão que fica não pode estar em --absorve")
    resumo = _comum.validar_resumo(args.resumo) if getattr(args, "resumo", None) else None
    prazo = _comum.validar_prazo(getattr(args, "prazo", None))
    principal = banco.classificacao(fica)
    if principal is None:
        raise ValueError("o cartão que fica não existe")
    lancado = lancamento.lancado_aberto(principal)   # D-58: já está no Frentes e pode ser corrigido
    if not lancado and not lancamento.eh_provisoria(principal):
        raise ValueError("o cartão que fica não é provisório nem está no Frentes")
    cartoes = []
    for i in absorve:
        c = banco.classificacao(i)
        if c is None:
            raise ValueError("cartão absorvido não existe")
        if c.get("conversa") != principal.get("conversa"):
            raise ValueError("cartões de conversas diferentes")
        if c.get("substituida_por") == fica:      # já absorvido por este mesmo cartão: repetir o comando é inócuo
            cartoes.append(c)
            continue
        if not lancamento.eh_provisoria(c):
            raise ValueError("cartão absorvido não é provisório")
        cartoes.append(c)
    frentes = None
    if lancado:   # o Frentes primeiro: fora do ar ou item já fechado = nada é gravado e o provisório sobe sozinho
        item = _comum.item_whatsapp_aberto(ponte, int(principal["item_id"]))
        if item is None:
            raise ValueError("o item do cartão que fica já foi fechado no Frentes; não funda nele")
        if resumo or prazo:
            frentes = lancamento.corrigir_item(ponte, dict(item, id=int(principal["item_id"])), principal,
                                               principal.get("contato"), resumo or principal.get("resumo"),
                                               prazo or principal.get("prazo"))
    if resumo or prazo:
        banco.atualizar_resumo_prazo(fica, resumo=resumo or None, prazo=prazo, lancado=lancado)
    for c in cartoes:
        banco.substituir(c["id"], fica, "consolidada")   # idempotente para o mesmo par
    saida = {"ok": True, "fica": fica, "absorvidos": [c["id"] for c in cartoes],
             "resumo_alterado": bool(resumo), "prazo_alterado": bool(prazo)}
    if lancado:
        saida["no_frentes"] = int(principal["item_id"])
        saida["frentes"] = frentes or "sem_mudanca"
    return saida
