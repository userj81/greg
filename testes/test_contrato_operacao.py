"""Contrato executável da estação operacao (E11): bin/vigia.sh, LaunchAgents, instalador e RUNBOOK.
Quem chama: unittest. Olha arquivos por caminho relativo à raiz do repositório.
O que não faz: não instala LaunchAgent nem chama launchctl.
"""
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from testes.util_contrato import RAIZ, _arquivo  # noqa: E402

PLIST_DIR = "infra/launchagents"
PYTHON = "/opt/homebrew/bin/python3"


def _plist(nome: str) -> dict:
    caminho = _arquivo(f"{PLIST_DIR}/{nome}", "operacao")
    if shutil.which("plutil"):
        r = subprocess.run(["plutil", "-lint", str(caminho)], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, f"plutil -lint falhou em {nome}: {r.stdout}{r.stderr}"
    with open(caminho, "rb") as f:
        return plistlib.load(f)


def _cal(d: dict) -> list:
    cal = d.get("StartCalendarInterval")
    return cal if isinstance(cal, list) else [cal] if cal else []


def _tem_cal(d: dict, hour: int, minute: int | None = None) -> bool:
    """Emenda F4 (9)/D-32: jobs semanais rodam TODO dia (sem Weekday); o ciclo usa StartCalendarInterval :00/:30."""
    return any(c.get("Hour") == hour and (minute is None or c.get("Minute") == minute) and "Weekday" not in c
               for c in _cal(d))


class TestContratoOperacao(unittest.TestCase):
    def test_plist_whatsapp(self):
        d = _plist("io.vigia.vigia-whatsapp.plist")
        self.assertEqual(d["Label"], "io.vigia.vigia-whatsapp")
        args = d["ProgramArguments"]
        self.assertEqual(args[0], "/bin/bash")
        self.assertTrue(args[1].startswith("/") and args[1].endswith("bin/vigia.sh"))
        # D-32 / FATO corrigido: StartInterval perdido durante o sono não dispara ao acordar; o ciclo usa calendário.
        self.assertNotIn("StartInterval", d)
        minutos = sorted(c.get("Minute") for c in _cal(d) if set(c) == {"Minute"})
        self.assertEqual(minutos, [0, 30])
        self.assertIs(d["RunAtLoad"], True)
        self.assertEqual(d["ProcessType"], "Background")
        self.assertIs(d["LowPriorityIO"], True)
        self.assertNotIn("KeepAlive", d)

    def test_plist_painel(self):
        d = _plist("io.vigia.vigia-painel.plist")
        args = d["ProgramArguments"]
        self.assertEqual(args[0], PYTHON)
        self.assertTrue(any(a.endswith("app/server.py") for a in args))
        self.assertIn("-P", args)  # F4 E11: app/http.py sombrearia a stdlib sem -P
        self.assertIs(d["KeepAlive"], True)
        self.assertIs(d["RunAtLoad"], True)

    def test_plist_semanal(self):
        nomes = sorted(p.name for p in (RAIZ / PLIST_DIR).glob("io.vigia.vigia-semanal*.plist")) \
            if (RAIZ / PLIST_DIR).exists() else []
        if not nomes:
            _arquivo(f"{PLIST_DIR}/io.vigia.vigia-semanal.plist", "operacao")
        ds = [_plist(n) for n in nomes]
        self.assertTrue(any(_tem_cal(d, 20, 5) for d in ds), "falta Hour 20 Minute 5 diário (relatório)")
        self.assertTrue(any(_tem_cal(d, 20, 35) for d in ds), "falta Hour 20 Minute 35 diário (limpeza)")
        todos = [a for d in ds for a in d["ProgramArguments"]]
        self.assertTrue(any(a.endswith("bin/vigia.sh") for a in todos), "semanal passa pelo vigia.sh (colima, lock)")
        self.assertIn("semanal", todos)
        self.assertIn("--executar-limpeza", todos)
        for d in ds:
            self.assertNotIn("KeepAlive", d)

    def test_vigia_sh_help_e_estrutura(self):
        sh = _arquivo("bin/vigia.sh", "operacao")
        r = subprocess.run(["bash", str(sh), "--help"], capture_output=True, text=True, timeout=30,
                           stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0)
        t = sh.read_text(encoding="utf-8")
        for trecho in ("set -euo pipefail", "vigia.lock", "mkdir", "PAUSADO", "colima status", "colima start",
                       "evolution.sh", "coletor.py", "--monitorar", "vigia-whatsapp.log"):
            self.assertIn(trecho, t)

    def test_vigia_sh_pausado_sai_0_sem_chamar_coletor(self):
        sh = _arquivo("bin/vigia.sh", "operacao")
        with tempfile.TemporaryDirectory() as tmp:
            raiz = Path(tmp)
            (raiz / "bin").mkdir()
            (raiz / "dados").mkdir()
            copia = raiz / "bin" / "vigia.sh"
            shutil.copy(sh, copia)
            (raiz / "dados" / "PAUSADO").write_text("")
            env = dict(os.environ, HOME=tmp, VIGIA_COLETOR="/usr/bin/false")
            r = subprocess.run(["bash", str(copia)], cwd=tmp, env=env, capture_output=True, text=True,
                               timeout=60, stdin=subprocess.DEVNULL)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_instalador(self):
        t = _arquivo("bin/instalar-launchagent.sh", "operacao").read_text(encoding="utf-8")
        for trecho in ("--remover", "launchctl bootout", "launchctl bootstrap", "gui/", "LaunchAgents", "launchctl print"):
            self.assertIn(trecho, t)

    def test_runbook(self):
        t = _arquivo("docs/RUNBOOK.md", "operacao").read_text(encoding="utf-8").lower()
        for trecho in ("subir", "parear", "qr", "saúde", "pausar", "reparear", "428", "restaurar", "desinstalar"):
            self.assertIn(trecho, t)


if __name__ == "__main__":
    unittest.main()
