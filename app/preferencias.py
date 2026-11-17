"""Preferências do painel (pedido 12; estilo Papel, pedido 39): aparência, tempo do envio liberado, prazo de limpeza
e, desde o pedido 81, de quanto em quanto tempo e em que horário o Greg lê.
O Papel é o único estilo (guia de 08/11/2026): sem seletor de estilo, sem papel de parede, sem tom do vidro e sem cor
de destaque própria (o acento vem do Mac, `AccentColor`, com o azul do sistema como reserva).
Quem chama: app/paginas/_comum.py (toda página aplica) e app/paginas/ajustes.py (lê e grava); o servidor aponta ARQUIVO
para dados/preferencias.json ao subir.
O que não faz: não guarda nada fora das chaves de PADRAO, e nunca põe valor do usuário no CSS sem validar.
"""
import json
import os
from pathlib import Path

ARQUIVO: Path | None = None  # definido por app/server.py; None = padrões (testes)

# Corpo em px: 13 é a métrica do macOS (guia Papel); os tokens de tipografia da base são em rem sobre este valor.
# Valor de preferencias.json anterior ao Papel (13..18, corpo 14 por padrão) que não está aqui cai no padrão.
FONTES = {12: "Pequena", 13: "Padrão", 14: "Grande", 15: "Muito grande"}
# Pedido 76 (D-77): o Greg abre sempre no claro; escuro ou "Igual ao Mac" só por escolha da pessoa.
TEMAS = {"claro": "Claro", "escuro": "Escuro", "sistema": "Igual ao Mac"}
# Cor de destaque (pedido 66): "mac" segue o acento do sistema (no navegador, o azul da base); as outras são fixas.
# Par (claro, escuro). Medido: branco sobre a bolha de saída e texto de acento sobre o fundo >= 4,5:1 nos dois temas
# (testes/test_ajustes.py refaz a conta).
ACENTOS = {"mac": ("Igual ao Mac", None), "azul": ("Azul", ("#007AFF", "#0A84FF")), "roxo": ("Roxo", ("#7A4FD6", "#8E63E6")),
           "verde": ("Verde", ("#1B8046", "#2A9654")), "laranja": ("Laranja", ("#C4570B", "#D9691A")),
           "rosa": ("Rosa", ("#D02F68", "#E0457B")), "grafite": ("Grafite", ("#5B6472", "#7A8494"))}
# Tempo que o envio fica liberado depois do Touch ID (pedido 70), em minutos. Padrão: 8 horas, um dia de trabalho.
ENVIO_MIN = {10: "10 minutos", 60: "1 hora", 240: "4 horas", 480: "8 horas", 1440: "24 horas"}
# Por quanto tempo as mensagens ficam guardadas antes da limpeza (pedido 67), em dias. A limpeza roda uma vez por
# semana, então o prazo é o mínimo: a semana só é apagada depois que a mensagem mais nova dela passou do prazo.
# 0 = não apagar (pedido 74): a limpeza não roda para nenhuma semana enquanto esta opção estiver marcada.
NAO_APAGAR = 0
RETENCAO_DIAS = {7: "7 dias", 14: "14 dias", 30: "30 dias", 90: "90 dias", NAO_APAGAR: "Não apagar"}
# De quanto em quanto tempo o Greg lê (pedido 81, D-82), em minutos. O agendador dispara de 30 em 30; com intervalo
# maior, o disparo que cai dentro de um intervalo já lido passa sem ler (app/agenda_leitura.py).
LEITURA_MIN = {30: "30 minutos", 60: "1 hora", 120: "2 horas", 240: "4 horas"}
# Horário de leitura: da hora `leitura_de` à hora `leitura_ate` (0 a 23). Horas iguais = o dia todo.
HORAS = {h: f"{h:02d}:00" for h in range(24)}
PADRAO = {"fonte": 13, "tema": "claro", "acento": "mac", "envio_min": 480, "retencao_dias": 7,
          "leitura_min": 30, "leitura_de": 0, "leitura_ate": 0}
_cache: dict = {"mtime": None, "valor": dict(PADRAO)}


# arquivo anterior ao Papel (sempre tinha "estilo"; vidro/fundo também o denunciam): a escala era 13..18 com corpo 14
_CHAVES_ANTIGAS = ("estilo", "vidro", "fundo", "fundo_claro", "fundo_escuro")
_FONTE_ANTIGA = {13: 12, 14: 13, 15: 13, 16: 14, 18: 15}


def validar(bruto: dict) -> dict:
    """Devolve preferências válidas; o que vier fora do permitido (ou chave que não existe mais) cai no padrão."""
    p = dict(PADRAO)
    try:
        fonte = int(str(bruto.get("fonte", "")).strip())
        if any(k in bruto for k in _CHAVES_ANTIGAS):
            fonte = _FONTE_ANTIGA.get(fonte, fonte)
        if fonte in FONTES:
            p["fonte"] = fonte
    except ValueError:
        pass
    tema = str(bruto.get("tema", "")).strip()
    if tema in TEMAS:
        p["tema"] = tema
    try:
        envio = int(str(bruto.get("envio_min", "")).strip())
        if envio in ENVIO_MIN:
            p["envio_min"] = envio
    except ValueError:
        pass
    try:
        retencao = int(str(bruto.get("retencao_dias", "")).strip())
        if retencao in RETENCAO_DIAS:
            p["retencao_dias"] = retencao
    except ValueError:
        pass
    acento = str(bruto.get("acento", "")).strip()
    if acento in ACENTOS:
        p["acento"] = acento
    try:
        leitura = int(str(bruto.get("leitura_min", "")).strip())
        if leitura in LEITURA_MIN:
            p["leitura_min"] = leitura
    except ValueError:
        pass
    try:   # as duas horas valem juntas: uma só fora da lista devolve o horário ao padrão (o dia todo)
        de, ate = int(str(bruto.get("leitura_de", "")).strip()), int(str(bruto.get("leitura_ate", "")).strip())
        if de in HORAS and ate in HORAS:
            p["leitura_de"], p["leitura_ate"] = de, ate
    except ValueError:
        pass
    return p


def ler() -> dict:
    if ARQUIVO is None:
        return dict(PADRAO)
    try:
        chave = (str(ARQUIVO), ARQUIVO.stat().st_mtime_ns)
    except OSError:
        return dict(PADRAO)
    if _cache["mtime"] != chave:
        try:
            _cache["valor"] = validar(json.loads(ARQUIVO.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            _cache["valor"] = dict(PADRAO)
        _cache["mtime"] = chave
    return dict(_cache["valor"])


def gravar(bruto: dict) -> dict:
    """Valida e grava (troca atômica, 0600). Sem ARQUIVO definido, só devolve o validado."""
    p = validar(bruto)
    if ARQUIVO is not None:
        ARQUIVO.parent.mkdir(parents=True, exist_ok=True)
        tmp = ARQUIVO.with_suffix(".tmp")
        tmp.write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, ARQUIVO)
    return p


def trocar(chave: str, valor) -> dict:
    """Muda UMA preferência e mantém as outras (gravar() sozinho devolveria ao padrão o que não viesse no pedido)."""
    p = ler()
    p[chave] = valor
    return gravar(p)


def atributos_html(p: dict) -> str:
    """Atributos do <html>: só o tema forçado (o CSS reage a data-tema)."""
    return "" if p["tema"] == "sistema" else f' data-tema="{p["tema"]}"'


def css(p: dict) -> str:
    """Estilo que sobrepõe a base: só o tamanho da letra, e só quando difere do padrão. Sempre com id: /ajustes troca o
    conteúdo deste <style> na prévia viva sem recarregar."""
    regra = "" if p["fonte"] == PADRAO["fonte"] else f"html {{ font-size:{int(p['fonte'])}px; }}"
    return f'<style id="prefs">{regra}{css_acento(p.get("acento", "mac"))}</style>'


def css_acento(acento: str) -> str:
    """Tokens de acento para uma cor fixa; "mac" (ou valor desconhecido) não emite nada e vale a base. As derivações são
    as mesmas que a base faz com AccentColor; :root três vezes para ganhar de :root[data-app="mac"]. Só cor da tabela
    ACENTOS entra no CSS, nunca valor do usuário."""
    par = (ACENTOS.get(acento) or ("", None))[1]
    if not par:
        return ""
    a = f"light-dark({par[0]}, {par[1]})"
    return (":root:root:root { "
            f"--acento: {a}; --acento-topo: color-mix(in srgb, {a}, white 12%); "
            f"--acento-texto: light-dark(color-mix(in srgb, {par[0]}, black 8%), color-mix(in srgb, {par[1]}, white 22%)); "
            f"--acento-anel: light-dark(color-mix(in srgb, {par[0]} 45%, black 10%), color-mix(in srgb, {par[1]} 55%, black 20%)); "
            f"--selecao: light-dark(color-mix(in srgb, {par[0]} 16%, transparent), color-mix(in srgb, {par[1]} 24%, transparent)); "
            f"--foco: light-dark(color-mix(in srgb, {par[0]} 50%, transparent), color-mix(in srgb, {par[1]} 60%, transparent)); "
            f"--bolha-saida: light-dark(color-mix(in srgb, {par[0]} 92%, black), color-mix(in srgb, {par[1]} 86%, black)); "
            "--classe-demanda: var(--acento-texto); }")
