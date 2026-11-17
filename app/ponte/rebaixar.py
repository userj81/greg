"""Comando `vigia-ponte rebaixar` (D-58): no fechamento do dia, a IA segura o cartão PROVISÓRIO que era só conversa.
`rebaixar <id> <aviso|sem_acao> 'motivo curto'`: a classe do cartão vira aviso (informa, não pede nada) ou sem_acao
(ruído) e ele NÃO sobe ao Frentes; a classe que a leitura tinha dado fica guardada e o cartão continua no histórico.
Quem chama: bin/vigia-ponte, acionado pelo Claude do fechamento (prompts/consolidar.md, app/consolidacao.py).
O que não faz: não toca o Frentes (o cartão ainda não subiu), não mexe em cartão com item, decidido, substituído ou
rotulado pelo dono, não apaga nada.
"""
from app.banco import CLASSES_QUIETAS
from app.classes import validar_classe
from app.ponte import _comum


def configurar(sub):
    p = sub.add_parser("rebaixar", help="fecha o dia: o cartão provisório que era só conversa não sobe ao Frentes")
    p.add_argument("id", type=int, help="id do cartão provisório")
    p.add_argument("classe", help="aviso (informa, não pede nada) ou sem_acao (ruído)")
    p.add_argument("motivo", help="por que não sobe, em uma linha curta")
    return p


def executar(args, *, banco, ponte) -> dict:
    try:
        classe = validar_classe(args.classe)
    except ValueError:
        raise ValueError("classe inválida (use aviso ou sem_acao)") from None
    if classe not in CLASSES_QUIETAS:
        raise ValueError("rebaixar só para aviso ou sem_acao")
    motivo = _comum.validar_resumo(args.motivo)
    if not motivo:
        raise ValueError("informe o motivo em uma linha")
    reg = banco.classificacao(int(args.id))
    if reg is None:
        raise ValueError("cartão não existe")
    antes = reg.get("classe")
    banco.rebaixar(int(args.id), classe, motivo)
    return {"ok": True, "id": int(args.id), "classe": classe, "era": reg.get("classe_original") or antes}
