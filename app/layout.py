"""Janela dividida (pedido 48, D-59): largura da lateral, lateral recolhida e largura da lista de cada página.
É o que o dono arrasta nas divisórias do painel; fica em dados/layout.json para valer também no Greg.app, que não
guarda nada do navegador entre uma abertura e outra (WKWebView com sessão só em memória).
Quem chama: app/paginas/_comum.py (toda página aplica) e app/paginas/layout.py (POST /layout grava). O servidor aponta
ARQUIVO para dados/layout.json ao subir. Voltar ao padrão = duplo clique na divisória (a tela manda largura 0).
O que não faz: não guarda nada além dessas medidas, e nunca põe valor do usuário no CSS sem validar (só inteiros
dentro da faixa e chave de página no formato de rota).
"""
import json
import os
import re
import threading
from pathlib import Path

ARQUIVO: Path | None = None  # definido por app/server.py; None = padrões (testes)

LATERAL_MIN, LATERAL_MAX = 168, 360
LATERAL_AUTO = 0   # sem largura escolhida: a base decide (212 px, ou mais se o rodapé da lateral pedir)
LISTA_MIN, LISTA_MAX = 240, 900          # o padrão de cada lista vem da classe do painel (380 ou 300 px, no CSS)
PAGINAS_MAX = 24
_PAGINA = re.compile(r"^/[a-z][a-z-]{0,23}$")
PADRAO = {"lateral": LATERAL_AUTO, "recolhida": False, "listas": {}}
_cache: dict = {"mtime": None, "valor": None}
_trava = threading.Lock()   # o servidor atende em threads: ler-mudar-gravar é uma operação só


def _inteiro(valor, minimo: int, maximo: int):
    """Inteiro dentro da faixa, ou None (texto, fração absurda, fora da faixa)."""
    try:
        n = int(float(str(valor).strip()))
    except (TypeError, ValueError, OverflowError):
        return None
    return n if minimo <= n <= maximo else None


def _ligado(valor) -> bool:
    return valor is True or str(valor).strip().lower() in ("1", "true", "on", "sim")


def validar(bruto) -> dict:
    """Devolve um layout válido; o que vier fora do permitido cai no padrão."""
    p = {"lateral": LATERAL_AUTO, "recolhida": False, "listas": {}}
    if not isinstance(bruto, dict):
        return p
    lateral = _inteiro(bruto.get("lateral"), LATERAL_MIN, LATERAL_MAX)
    if lateral is not None:
        p["lateral"] = lateral
    p["recolhida"] = _ligado(bruto.get("recolhida"))
    listas = bruto.get("listas")
    if isinstance(listas, dict):
        for pagina, largura in list(listas.items())[:PAGINAS_MAX]:
            largura = _inteiro(largura, LISTA_MIN, LISTA_MAX)
            if isinstance(pagina, str) and _PAGINA.match(pagina) and largura is not None:
                p["listas"][pagina] = largura
    return p


def ler() -> dict:
    if ARQUIVO is None:
        return validar({})
    try:
        chave = (str(ARQUIVO), ARQUIVO.stat().st_mtime_ns)
    except OSError:
        return validar({})
    if _cache["mtime"] != chave:
        try:
            _cache["valor"] = validar(json.loads(ARQUIVO.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            _cache["valor"] = validar({})
        _cache["mtime"] = chave
    return validar(_cache["valor"])


def _gravar(p: dict) -> dict:
    if ARQUIVO is not None:
        ARQUIVO.parent.mkdir(parents=True, exist_ok=True)
        tmp = ARQUIVO.with_suffix(".tmp")
        tmp.write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, ARQUIVO)
    return p


def mudar(mudanca: dict) -> dict:
    """Aplica uma mudança vinda da tela por cima do que está guardado e grava (troca atômica, 0600).
    Chaves aceitas, todas opcionais: `lateral` (px; 0 = volta à automática) · `recolhida` (1/0) · `pagina` + `lista`
    (px da lista daquela página; 0 = volta ao padrão da tela). O que vier inválido é ignorado, nunca gravado.
    Sem ARQUIVO definido, só devolve o resultado."""
    with _trava:
        return _mudar(mudanca)


def _mudar(mudanca: dict) -> dict:
    p = ler()
    if "lateral" in mudanca:
        if str(mudanca["lateral"]).strip() in ("0", ""):
            p["lateral"] = LATERAL_AUTO
        else:
            p["lateral"] = _inteiro(mudanca["lateral"], LATERAL_MIN, LATERAL_MAX) or p["lateral"]
    if "recolhida" in mudanca:
        p["recolhida"] = _ligado(mudanca["recolhida"])
    pagina = str(mudanca.get("pagina", "")).strip()
    if "lista" in mudanca and _PAGINA.match(pagina):
        if str(mudanca["lista"]).strip() in ("0", ""):
            p["listas"].pop(pagina, None)
        else:
            largura = _inteiro(mudanca["lista"], LISTA_MIN, LISTA_MAX)
            if largura is not None and (pagina in p["listas"] or len(p["listas"]) < PAGINAS_MAX):
                p["listas"][pagina] = largura
    return _gravar(validar(p))


def atributos_html(p: dict) -> str:
    """Atributos do <html>: lateral recolhida e largura ESCOLHIDA da lateral (nada quando é a automática). Vão no próprio elemento
    (e não numa folha de estilo) porque o JS da base mexe na mesma variável ao arrastar e a remove no duplo clique."""
    a = ' data-lateral="recolhida"' if p["recolhida"] else ""
    if p["lateral"]:
        a += f' style="--lateral:{int(p["lateral"])}px"'
    return a


def atributos_paineis(p: dict, pagina: str = "") -> str:
    """Atributos de <div class="paineis">: a página (chave da largura da lista) e a largura guardada para ela. A mesma
    medida vale para as duas classes de lista (380 e 300 px de padrão, no CSS da base)."""
    if not _PAGINA.match(pagina or ""):
        return ""
    a = f' data-pagina="{pagina}"'
    largura = p["listas"].get(pagina)
    if largura:
        a += f' style="--lista:{int(largura)}px;--lista-estreita:{int(largura)}px"'
    return a
