"""Central de avisos do Greg (pedido 63): cada peça que falha abre um aviso e fecha quando volta.
Quem chama: bin/coletor.py, app/conexao.py e o painel (detectores, a partir do lote 2); as telas leem `abertos`.
Estado em dados/alertas.json (arquivo, não o vigia.db: o banco é uma das peças que falham; e o coletor e o painel são
processos diferentes). Escrita atômica sob trava de arquivo.
O que não faz: não notifica (devolve o que está na hora de notificar; quem chama decide o canal), não levanta exceção
(a central falhar nunca derruba quem avisou) e não guarda texto de conversa: só código, contagem, datas e um detalhe
técnico curto vindo de `erros.causa`.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import time
from pathlib import Path

from app.problemas import CATALOGO, leitura_parada

log = logging.getLogger("vigia.alertas")
ARQUIVO = "alertas.json"
REPETE_S = 24 * 3600      # D-66: a notificação repete uma vez por dia enquanto o problema durar
LIMIAR_PASSAGEIRO = 2     # D-66: falha passageira só avisa na segunda seguida


def _ler(dados: Path) -> dict:
    try:
        d = json.loads((dados / ARQUIVO).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _mudar(dados: Path, fn):
    """Lê, aplica fn(estado) e grava, tudo sob trava. Devolve o que fn devolver; em erro de disco, None."""
    try:
        dados.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(dados / (ARQUIVO + ".lock"), "w") as trava:
            fcntl.flock(trava, fcntl.LOCK_EX)
            estado = _ler(dados)
            r = fn(estado)
            tmp = dados / (ARQUIVO + ".tmp")
            tmp.write_text(json.dumps(estado, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, dados / ARQUIVO)
            return r
    except OSError as e:
        log.warning("central de avisos não gravou (%s)", type(e).__name__)
        return None


def abrir(dados: Path, codigo: str, *, detalhe: str = "", motivo: str = "", agora: float | None = None) -> bool:
    """Registra uma falha de `codigo`. Devolve True se o aviso NASCEU agora (não existia). Código fora do catálogo é
    recusado com linha no log: aviso sem texto para a pessoa não serve."""
    if codigo not in CATALOGO:
        log.error("aviso com código fora do catálogo: %s", codigo)
        return False
    t = int(agora if agora is not None else time.time())

    def fn(estado):
        a = estado.get(codigo)
        novo = not isinstance(a, dict)
        if novo:
            a = estado[codigo] = {"desde": t, "vezes": 0, "notificado_em": 0}
        a["vezes"] = int(a.get("vezes") or 0) + 1
        a["ultima"] = t
        a["detalhe"] = " ".join(str(detalhe).split())[:200]
        if motivo:
            a["motivo"] = str(motivo)[:40]     # pedido 85: código do catálogo, nunca texto livre
        else:
            a.pop("motivo", None)
        return novo

    novo = bool(_mudar(dados, fn))
    log.warning("aviso %s %s%s", "aberto" if novo else "repetido", codigo, f" ({detalhe})" if detalhe else "")
    return novo


def fechar(dados: Path, *codigos: str) -> list:
    """A peça voltou: fecha os avisos dados. Devolve os que estavam abertos."""
    def fn(estado):
        return [c for c in codigos if estado.pop(c, None) is not None]

    if not any(c in _ler(dados) for c in codigos):      # caminho comum (nada aberto): sem trava nem escrita
        return []
    fechados = _mudar(dados, fn) or []
    for c in fechados:
        log.info("aviso fechado %s", c)
    return fechados


def _visivel(p, a) -> bool:
    return p.imediato or int(a.get("vezes") or 0) >= LIMIAR_PASSAGEIRO


def abertos(dados: Path) -> list:
    """Avisos que a pessoa deve ver, mais antigos primeiro: [{codigo, peca, titulo, por_que, passos, link, desde,
    vezes}]. Falha passageira na primeira vez fica registrada mas não aparece."""
    saida = []
    for codigo, a in _ler(dados).items():
        p = CATALOGO.get(codigo)
        if p is None or not isinstance(a, dict) or not _visivel(p, a):
            continue
        p = _texto(p, a)
        saida.append({"codigo": codigo, "peca": p.peca, "titulo": p.titulo, "por_que": p.por_que,
                      "passos": list(p.passos), "link": p.link, "desde": int(a.get("desde") or 0),
                      "vezes": int(a.get("vezes") or 0)})
    return sorted(saida, key=lambda x: (x["desde"], x["codigo"]))


def a_notificar(dados: Path, *, agora: float | None = None) -> list:
    """Avisos visíveis que ainda não foram notificados ou cuja última notificação tem mais de um dia. Marca como
    notificados na mesma operação, para dois processos não notificarem o mesmo aviso."""
    t = int(agora if agora is not None else time.time())
    if not _ler(dados):
        return []

    def fn(estado):
        sai = []
        for codigo, a in estado.items():
            p = CATALOGO.get(codigo)
            if p is None or not isinstance(a, dict) or not _visivel(p, a):
                continue
            ultima = int(a.get("notificado_em") or 0)
            if not ultima or t - ultima >= REPETE_S:
                a["notificado_em"] = t
                sai.append(codigo)
        return sai

    estado = _ler(dados)
    return [_texto(CATALOGO[c], estado.get(c)) for c in (_mudar(dados, fn) or [])]


def _texto(p, a):
    """O texto do aviso: o do catálogo, salvo "leitura-parada" com motivo registrado (pedido 85)."""
    if p.codigo == "leitura-parada" and isinstance(a, dict) and a.get("motivo"):
        return leitura_parada(str(a["motivo"]))
    return p
