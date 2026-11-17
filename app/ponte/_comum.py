"""Helpers compartilhados dos comandos da ponte (resumo, manifesto do lote, item aberto, prioridade).
Quem chama: app/ponte/classificar.py e app/ponte/atualizar.py. Prefixo _ = não é comando.
O que não faz: não grava em banco, não fala com rede além de ler itens abertos via `ponte`.
"""
import json
import os
from datetime import date
from pathlib import Path

RESUMO_MAX = 300
PROIBIDOS = ('"', "`", "$", "\n", "\r", ";", "|", "&", ">", "<")  # D-19/D-20: aspas simples são permitidas


def validar_resumo(resumo) -> str:
    """Recusa (ValueError) resumo longo ou com caractere proibido; nunca ecoa o conteúdo."""
    r = "" if resumo is None else str(resumo)
    if len(r) > RESUMO_MAX:
        raise ValueError(f"resumo acima de {RESUMO_MAX} caracteres")
    if any(c in r for c in PROIBIDOS):
        raise ValueError("resumo com caractere proibido")
    return " ".join(r.split())


def validar_prazo(prazo):
    if prazo is None or prazo == "":
        return None
    try:
        return date.fromisoformat(str(prazo)).isoformat()
    except ValueError:
        raise ValueError("prazo deve ser AAAA-MM-DD") from None


def procurar_no_manifesto(msg_id: str):
    """Com VIGIA_LOTE definido: varre <VIGIA_LOTE>/*/mensagens.json. Devolve (True, dados|None);
    sem VIGIA_LOTE: (False, None). Manifesto ilegível é ignorado."""
    base = os.environ.get("VIGIA_LOTE")
    if not base:
        return False, None
    pasta = Path(base)
    if not pasta.is_absolute() or not pasta.is_dir():
        return True, None
    for arq in sorted(pasta.glob("*/mensagens.json")):
        try:
            man = json.loads(arq.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(man, dict) and isinstance(man.get(msg_id), dict):
            return True, man[msg_id]
    return True, None


def item_whatsapp_aberto(ponte, item_id: int):
    """O item de origem whatsapp ainda aberto com esse id, ou None."""
    for it in ponte.itens_abertos(origem="whatsapp"):
        if it.get("id") == item_id:
            return it
    return None


def prioridade_a_enviar(item: dict, pedida):
    """Nunca rebaixa (1 é a mais alta): só devolve a prioridade se for mais alta que a atual."""
    if pedida is None:
        return None
    atual = item.get("prioridade")
    atual = 2 if atual not in (1, 2) else atual
    return int(pedida) if int(pedida) < atual else None
