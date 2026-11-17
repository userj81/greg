"""Classes do vigia (atencao, demanda, lembrar, lembrando, aviso, sem_acao) e regras de mapeamento para o Frentes.
Quem chama: bin/vigia-ponte (app/ponte/*) e o painel. Só stdlib.
O que não faz: não acessa banco, rede nem Frentes; só valida e decide.
"""
import unicodedata

CLASSES = ("atencao", "demanda", "lembrar", "lembrando", "aviso", "sem_acao")
CONFIANCAS = ("alta", "baixa")
LIMITE_TEXTO = 200

_MAPA = {
    "atencao": {"prioridade": 1, "tipo": "tarefa"},
    "demanda": {"prioridade": 2, "tipo": "tarefa"},
    "lembrar": {"prioridade": 2, "tipo": "prazo"},
}
CLASSES_ITEM = tuple(_MAPA)   # as que viram item no Frentes (D-57: sobem na hora ou no fechamento do dia)


def _normalizar(s: str) -> str:
    if not isinstance(s, str):
        raise ValueError(f"valor inválido: {s!r}")
    sem_acento = "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    )
    return sem_acento.strip().lower().replace(" ", "_").replace("-", "_")


def validar_classe(s: str) -> str:
    n = _normalizar(s)
    if n not in CLASSES:
        raise ValueError(f"classe inválida: {s!r} (use {', '.join(CLASSES)})")
    return n


def validar_confianca(s: str) -> str:
    n = _normalizar(s)
    if n not in CONFIANCAS:
        raise ValueError(f"confiança inválida: {s!r} (use alta ou baixa)")
    return n


def mapa_frentes(classe: str) -> dict | None:
    r = _MAPA.get(validar_classe(classe))
    return dict(r) if r else None


def cria_item(classe: str) -> bool:
    return validar_classe(classe) in _MAPA


def atualiza_item(classe: str) -> bool:
    return validar_classe(classe) == "lembrando"


def texto_item(contato: str, resumo: str) -> str:
    contato = " ".join(str(contato if contato is not None else "").split())[:40].rstrip() or "?"
    resumo = " ".join(str(resumo if resumo is not None else "").split())
    if not resumo:
        raise ValueError("resumo vazio")
    texto = f"WA {contato}: {resumo}"
    if len(texto) > LIMITE_TEXTO:
        texto = texto[: LIMITE_TEXTO - 1].rstrip() + "…"
    return texto
