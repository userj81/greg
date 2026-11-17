"""Comando `vigia-ponte historico` (D-58): a IA registra UMA linha de histórico por conversa lida — o que aconteceu ali
nesta leitura e em que pé a conversa ficou. É o que depois responde "o que aconteceu essa semana?" e "quem ficou sem
resposta minha?" (bin/historico.py), inclusive para as conversas que não viraram nada no Frentes.
`historico <msg_id> 'resumo' --situacao aguarda_dono|aguarda_contato|encerrada`: `<msg_id>` é o id da ÚLTIMA mensagem
`[novo]` da conversa; conversa, contato e hora vêm do manifesto do lote (VIGIA_LOTE), como no `classificar`.
Sem VIGIA_LOTE (uso manual/teste) exige --conversa e --ts. Repetir o mesmo msg_id não duplica.
Quem chama: bin/vigia-ponte (acionado pelo Claude agendado do coletor, via prompts/vigia.md).
O que não faz: não cria cartão nem item, não toca o Frentes nem a Evolution, não guarda texto literal de mensagem (o
resumo passa pela mesma validação do `classificar`).
"""
import os
from pathlib import Path

from app.banco import SITUACOES
from app.ponte import _comum


def configurar(sub):
    p = sub.add_parser("historico", help="registra a linha de histórico de uma conversa lida (o que houve e em que pé ficou)")
    p.add_argument("msg_id", help="id da última mensagem [novo] da conversa")
    p.add_argument("resumo", help="o que aconteceu na conversa nesta leitura, em uma linha")
    p.add_argument("--situacao", default=None, help="aguarda_dono | aguarda_contato | encerrada (obrigatório)")
    p.add_argument("--conversa", default=None, help="só sem VIGIA_LOTE (uso manual)")
    p.add_argument("--contato", default=None, help="só sem VIGIA_LOTE (uso manual)")
    p.add_argument("--ts", type=int, default=None, help="só sem VIGIA_LOTE (uso manual)")
    return p


def _g(args, nome, padrao=None):
    return getattr(args, nome, padrao)


def executar(args, *, banco, ponte) -> dict:
    msg_id = str(args.msg_id).strip()
    if not msg_id:
        raise ValueError("msg_id vazio")
    situacao = str(_g(args, "situacao") or "").strip().lower().replace("-", "_")
    if situacao not in SITUACOES:
        raise ValueError("informe --situacao aguarda_dono, aguarda_contato ou encerrada")
    resumo = _comum.validar_resumo(args.resumo)
    if not resumo:
        raise ValueError("resumo vazio")
    com_lote, dados = _comum.procurar_no_manifesto(msg_id)
    lote = None
    if com_lote:
        if not dados:
            raise ValueError("msg_id fora do lote")
        conversa, contato, ts = dados.get("conversa"), dados.get("contato"), dados.get("ts")
        if not conversa or isinstance(ts, bool) or not isinstance(ts, int):
            raise ValueError("manifesto do lote inválido para este msg_id")
        contato = str(contato or "").strip() or "contato"
        lote = Path(os.environ.get("VIGIA_LOTE") or "").name or None
    else:
        conversa, ts = _g(args, "conversa"), _g(args, "ts")
        if not conversa or ts is None:
            raise ValueError("sem VIGIA_LOTE: informe --conversa e --ts")
        contato = (_g(args, "contato") or "").strip() or "contato"
    aid, criou = banco.gravar_analise(msg_id=msg_id, conversa=str(conversa), contato=contato, ts=int(ts), resumo=resumo,
                                      situacao=situacao, lote=lote)
    return {"ok": True, "repetido": not criou, "id": aid, "msg_id": msg_id, "situacao": situacao}
