"""Ajudantes dos testes de contrato: importa módulo de estação ou falha com mensagem legível.
Quem chama: testes/test_contrato_*.py.
O que não faz: não esconde erro de importação de dentro do módulo (só a ausência do módulo vira falha).
"""
import importlib
import importlib.util
import sys
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

# Cliente HTTP que ignora proxy de ambiente (os fakes são 127.0.0.1).
ABRIDOR = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _importar(nome: str, estacao: str | None = None):
    """Importa `nome` (ex.: 'app.banco'); se o módulo não existe, levanta AssertionError legível."""
    try:
        return importlib.import_module(nome)
    except ModuleNotFoundError as e:
        faltou = e.name or ""
        if faltou and (nome == faltou or nome.startswith(faltou + ".")):
            alvo = nome.replace(".", "/") + ".py"
            raise AssertionError(
                f"estação {estacao or nome.split('.')[-1]} ainda não entregou {alvo}") from None
        raise


def _importar_arquivo(relativo: str, estacao: str):
    """Carrega um script de bin/ (ex.: 'bin/coletor.py') como módulo; ausente = falha legível."""
    caminho = RAIZ / relativo
    if not caminho.exists():
        raise AssertionError(f"estação {estacao} ainda não entregou {relativo}")
    nome = "vigia_" + caminho.stem.replace("-", "_")
    spec = importlib.util.spec_from_file_location(nome, caminho)
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[nome] = modulo
    spec.loader.exec_module(modulo)
    return modulo


def _arquivo(relativo: str, estacao: str) -> Path:
    """Caminho de um arquivo da estação; ausente = falha legível."""
    caminho = RAIZ / relativo
    if not caminho.exists():
        raise AssertionError(f"estação {estacao} ainda não entregou {relativo}")
    return caminho


class RelogioFalso:
    """Relógio injetável: chamar devolve o instante atual; avancar(s) soma segundos."""

    def __init__(self, inicio: float | None = None):
        import time
        self.agora = time.time() if inicio is None else inicio

    def __call__(self) -> float:
        return self.agora

    def avancar(self, segundos: float) -> None:
        self.agora += segundos
