"""Tipos de requisição e resposta trocados entre app/server.py e os handlers de app/paginas/*.
Quem chama: o servidor (monta Requisicao) e cada página (devolve Resposta).
O que não faz: não abre socket nem decide autenticação; isso é do servidor.
"""
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Requisicao:
    """Requisição já decodificada.

    `cabecalhos` é um acréscimo opcional da F0 (default vazio): o contrato de /enviar exige o
    header X-Vigia-Envio e sem este campo o handler não teria como lê-lo. Chaves em minúsculas.
    """
    metodo: str
    caminho: str
    query: dict = field(default_factory=dict)
    corpo: dict = field(default_factory=dict)
    cookies: dict = field(default_factory=dict)
    ctx: Any = None
    cabecalhos: dict = field(default_factory=dict)


@dataclass
class Resposta:
    """Resposta que o servidor serializa."""
    status: int
    corpo: bytes | str
    tipo: str = "text/html; charset=utf-8"
    cabecalhos: list = field(default_factory=list)


def json_resposta(obj, status: int = 200) -> Resposta:
    """Serializa `obj` em JSON UTF-8 (sem escapar acentos)."""
    texto = json.dumps(obj, ensure_ascii=False)
    return Resposta(status, texto, "application/json; charset=utf-8")


def html(texto: str, status: int = 200) -> Resposta:
    """Resposta HTML UTF-8."""
    return Resposta(status, texto)
