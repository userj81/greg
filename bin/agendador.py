#!/usr/bin/env python3
"""Agendador do Greg no contêiner: faz o papel dos LaunchAgents do Mac (infra/launchagents/), que não existem no Docker.
- ciclo de leitura nos minutos :00 e :30 (`bin/vigia.sh ciclo`), e na hora em que o painel pede "Ler agora"
  (arquivo dados/LER-AGORA, criado por app/paginas/conexao.py no modo contêiner);
- relatório da semana às 20:05 (`bin/vigia.sh semanal`) e limpeza às 20:35 (`bin/vigia.sh semanal --executar-limpeza`);
  é o próprio vigia.sh que decide se hoje é dia de fechar a semana, como no Mac.
Quem chama: bin/docker-entrypoint.sh (em segundo plano). Um comando por vez; o vigia.sh tem trava própria.
O que não faz: não lê mensagem, não guarda estado próprio e não imprime nada além do comando que rodou e o código.
"""
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
DADOS = Path(os.environ.get("VIGIA_DADOS") or RAIZ / "dados")
GATILHO = DADOS / "LER-AGORA"
AGENDA = (  # (hora ou None = toda hora, minuto, argumentos do vigia.sh)
    (None, 0, ["ciclo"]),
    (None, 30, ["ciclo"]),
    (20, 5, ["semanal"]),
    (20, 35, ["semanal", "--executar-limpeza"]),
)


def devidos(agora: datetime, ultimo: dict) -> list[list[str]]:
    """Comandos da AGENDA cujo horário é este minuto e que ainda não rodaram neste minuto."""
    chave_min = agora.strftime("%Y-%m-%d %H:%M")
    for velha in [k for k in ultimo if k[0] != chave_min]:   # só o minuto corrente importa
        del ultimo[velha]
    saida = []
    for hora, minuto, args in AGENDA:
        if agora.minute == minuto and (hora is None or agora.hour == hora):
            chave = (chave_min, tuple(args))
            if chave not in ultimo:
                ultimo[chave] = True
                saida.append(args)
    return saida


def rodar(args: list[str]) -> int:
    ini = time.monotonic()
    r = subprocess.run(["/bin/bash", str(RAIZ / "bin" / "vigia.sh"), *args], cwd=RAIZ)
    print(f"agendador: vigia.sh {' '.join(args)} -> {r.returncode} em {time.monotonic() - ini:.0f}s", flush=True)
    return r.returncode


def main() -> int:
    ultimo: dict = {}
    print("agendador: ciclo :00 e :30 · semanal 20:05 · limpeza 20:35 · Ler agora pelo painel", flush=True)
    rodar(["ciclo"])   # como o RunAtLoad do LaunchAgent: lê ao subir
    while True:
        if GATILHO.exists():
            try:
                GATILHO.unlink()
            except OSError:
                pass
            rodar(["ciclo"])
        for args in devidos(datetime.now(), ultimo):
            rodar(args)
        time.sleep(5)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
