"""Comando `vigia-ponte frentes`: lista as frentes do Frentes (id, nome, tipo, apelidos) e as seções de cada uma.
Quem chama: bin/vigia-ponte (o Claude relê as frentes quando precisa).
O que não faz: não escreve em lugar nenhum.
"""


def configurar(sub):
    return sub.add_parser("frentes", help="lista as frentes")


def executar(args, *, banco, ponte) -> dict:
    por_frente = {}
    for s in (banco.secoes() if callable(getattr(banco, "secoes", None)) else []):
        por_frente.setdefault(s["frente_id"], []).append({"id": s["id"], "nome": s["nome"], "descricao": s["descricao"]})
    frentes = [{"id": f.get("id"), "nome": f.get("nome"), "tipo": f.get("tipo"),
                "apelidos": f.get("apelidos") or [], "secoes": por_frente.get(f.get("id"), [])} for f in ponte.frentes()]
    return {"ok": True, "frentes": frentes}
