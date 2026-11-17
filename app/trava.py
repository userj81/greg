"""Trava do ciclo (dados/vigia.lock) em Python, no MESMO formato do bin/vigia.sh: pasta criada com mkdir (atômico) e o
pid do dono em vigia.lock/pid. Quem usa: bin/transcrever.py, para o atrasado de áudio nunca rodar junto com o ciclo (dois
motores de transcrição ao mesmo tempo é o que o dono proibiu em 04/11/2026); enquanto a trava é dele, o ciclo do
LaunchAgent sai com 2 ("outro ciclo em andamento") e tenta na rodada seguinte.
Recolhe só trava de dono morto (pid que não existe) ou, sem pid legível, com mais de EXPIRA_S. pid de outro usuário
(EPERM) = existe: mantém. Solta só a que é sua (pid gravado == o próprio).
O que não faz: não espera (quem quer esperar chama pegar() de novo), não grava nada além do pid.
"""
import os
import shutil
import time
from pathlib import Path

from app.erros import VigiaErro

NOME = "vigia.lock"
EXPIRA_S = 3600


class TravaOcupada(VigiaErro):
    """Outro processo (ciclo ou atrasado) está com a trava; quem pediu não deve rodar agora."""


class Trava:
    def __init__(self, pasta_dados, *, pid: int | None = None, expira_s: int = EXPIRA_S):
        self.caminho = Path(pasta_dados) / NOME
        self.pid = int(pid if pid is not None else os.getpid())
        self.expira_s = expira_s
        self.minha = False

    def pegar(self) -> bool:
        """True se a trava agora é deste processo; False se outro dono vivo a tem."""
        self.caminho.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self._criar():
            return True
        if self._dono_morto():
            shutil.rmtree(self.caminho, ignore_errors=True)
            return self._criar()
        return False

    def soltar(self) -> None:
        if not self.minha:
            return
        arq = self.caminho / "pid"
        try:
            if arq.read_text(encoding="utf-8").strip() == str(self.pid):
                arq.unlink()
                self.caminho.rmdir()
        except OSError:
            pass
        self.minha = False

    def __enter__(self):
        if not self.pegar():
            raise TravaOcupada(f"{NOME} com dono {self._dono() or '?'}")
        return self

    def __exit__(self, *exc):
        self.soltar()
        return False

    def _criar(self) -> bool:
        try:
            self.caminho.mkdir(mode=0o700)
        except FileExistsError:
            return False
        (self.caminho / "pid").write_text(f"{self.pid}\n", encoding="utf-8")
        self.minha = True
        return True

    def _dono(self) -> str:
        try:
            return (self.caminho / "pid").read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _dono_morto(self) -> bool:
        dono = self._dono()
        if not dono.isdigit():
            try:
                idade = time.time() - self.caminho.stat().st_mtime
            except OSError:   # sumiu entre o mkdir que falhou e aqui: a próxima tentativa decide
                return False
            return idade > self.expira_s
        try:
            os.kill(int(dono), 0)
        except ProcessLookupError:
            return True
        except PermissionError:   # existe, de outro usuário (I-EPERM do vigia.sh): mantém
            return False
        return False
