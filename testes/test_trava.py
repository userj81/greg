"""Trava do ciclo em Python (app/trava.py), no formato do bin/vigia.sh: mkdir atômico + pid do dono."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app import trava
from app.trava import Trava, TravaOcupada


def _pid_inexistente() -> int:
    pid = 2**22 - 1
    while pid > 1000:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return pid
        except PermissionError:
            pass
        pid -= 1
    raise RuntimeError("não achei pid livre")


class TestTrava(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dados = Path(self.tmp.name) / "dados"

    def tearDown(self):
        self.tmp.cleanup()

    def test_pega_grava_pid_e_solta(self):
        t = Trava(self.dados, pid=4242)
        self.assertTrue(t.pegar())
        self.assertEqual((self.dados / "vigia.lock" / "pid").read_text().strip(), "4242")
        self.assertEqual(oct(self.dados.stat().st_mode & 0o777), "0o700")
        t.soltar()
        self.assertFalse((self.dados / "vigia.lock").exists())

    def test_dono_vivo_impede(self):
        Trava(self.dados).pegar()                       # este processo é o dono
        outra = Trava(self.dados, pid=4242)
        self.assertFalse(outra.pegar())
        self.assertFalse(outra.minha)
        outra.soltar()                                   # não é dela: mantém
        self.assertTrue((self.dados / "vigia.lock" / "pid").exists())

    def test_recolhe_dono_morto(self):
        Trava(self.dados, pid=_pid_inexistente()).pegar()
        t = Trava(self.dados, pid=4242)
        self.assertTrue(t.pegar())
        self.assertEqual((self.dados / "vigia.lock" / "pid").read_text().strip(), "4242")

    def test_eperm_e_dono_vivo(self):
        Trava(self.dados, pid=_pid_inexistente()).pegar()
        with mock.patch.object(trava.os, "kill", side_effect=PermissionError):
            self.assertFalse(Trava(self.dados, pid=4242).pegar())

    def test_sem_pid_legivel_so_recolhe_depois_de_expirar(self):
        pasta = self.dados / "vigia.lock"
        pasta.mkdir(parents=True)
        self.assertFalse(Trava(self.dados, pid=4242, expira_s=3600).pegar())
        antigo = time.time() - 7200
        os.utime(pasta, (antigo, antigo))
        self.assertTrue(Trava(self.dados, pid=4242, expira_s=3600).pegar())

    def test_contexto_levanta_quando_ocupada(self):
        Trava(self.dados).pegar()
        with self.assertRaises(TravaOcupada):
            with Trava(self.dados, pid=4242):
                self.fail("não deveria entrar")
        self.assertEqual((self.dados / "vigia.lock" / "pid").read_text().strip(), str(os.getpid()))

    def test_contexto_pega_e_solta(self):
        with Trava(self.dados, pid=4242) as t:
            self.assertTrue(t.minha)
            self.assertTrue((self.dados / "vigia.lock").is_dir())
        self.assertFalse((self.dados / "vigia.lock").exists())


if __name__ == "__main__":
    unittest.main()
