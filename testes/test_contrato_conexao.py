"""Contrato executável da estação conexao (E8): app/conexao.py, máquina de estado do QR e monitor.
Quem chama: unittest. Relógio fake; FakeEvolution; Banco SQLite temporário.
O que não faz: não dorme de verdade.
"""
import inspect
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from testes.fakes import FakeEvolution  # noqa: E402
from testes.util_contrato import RelogioFalso, _importar  # noqa: E402


class TestContratoConexao(unittest.TestCase):
    def setUp(self):
        self.mod = _importar("app.conexao", "conexao")
        evo_mod = _importar("app.evolution_client", "evolution_client")
        banco_mod = _importar("app.banco", "banco")
        self.cursor_mod = _importar("app.cursor", "banco")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.antigo_cwd = os.getcwd()
        os.chdir(self.tmp.name)  # cursor padrão (dados/cursor.json) cai aqui
        self.addCleanup(os.chdir, self.antigo_cwd)
        os.makedirs("dados", exist_ok=True)
        self.fake = FakeEvolution().iniciar()
        self.addCleanup(self.fake.parar)
        self.evo = evo_mod.EvolutionClient(self.fake.url, self.fake.instancia, self.fake.apikey)
        self.banco = banco_mod.Banco(str(Path(self.tmp.name) / "vigia.db"))
        self.rel = RelogioFalso()
        self.cursor_arq = Path(self.tmp.name) / "dados" / "cursor.json"
        kw = {}
        if "cursor" in inspect.signature(self.mod.Conexao.__init__).parameters:
            self.cursor = self.cursor_mod.Cursor.carregar(self.cursor_arq)
            kw["cursor"] = self.cursor
        else:
            self.cursor = None
        self.c = self.mod.Conexao(self.evo, self.banco, qr_segundos=60, qr_max=3, relogio=self.rel, **kw)

    def _pareado_em(self):
        if self.cursor is not None:
            return self.cursor.pareado_em
        return self.cursor_mod.Cursor.carregar(self.cursor_arq).pareado_em

    def test_estado_nunca_incrementa_qr(self):
        self.fake.estado = "open"
        for _ in range(10):
            self.evo.estado()
        self.c.tick()
        self.assertEqual(self.fake.qrs_gerados, 0)
        self.assertEqual(self.c.estado, "ocioso")

    def test_tres_qrs_depois_expira_com_logout(self):
        r = self.c.iniciar_qr()
        for k in ("base64", "expira_em", "restantes"):
            self.assertIn(k, r)
        self.assertEqual(self.fake.qrs_gerados, 1)
        self.assertEqual(self.c.estado, "gerando")
        self.rel.avancar(61)
        self.c.tick()
        self.assertEqual(self.fake.qrs_gerados, 2)
        self.rel.avancar(61)
        self.c.tick()
        self.assertEqual(self.fake.qrs_gerados, 3)
        self.rel.avancar(61)
        self.c.tick()
        self.assertEqual(self.c.estado, "expirado")
        self.assertIn(("DELETE", f"/instance/logout/{self.fake.instancia}"), self.fake.chamadas)
        self.assertEqual(self.fake.qrs_gerados, 3)
        for _ in range(120):  # 600 s de ticks a cada 5 s
            self.rel.avancar(5)
            self.c.tick()
        self.assertEqual(self.fake.qrs_gerados, 3)
        self.assertEqual(self.banco.qrs_hoje(), 3)
        self.assertEqual(self.c.estado, "expirado")

    def test_tick_antes_de_vencer_nao_renova(self):
        self.c.iniciar_qr()
        for _ in range(11):
            self.rel.avancar(5)
            self.c.tick()
        self.assertEqual(self.fake.qrs_gerados, 1)

    def test_open_no_meio_conecta_e_grava_pareado_em(self):
        self.c.iniciar_qr()
        self.rel.avancar(10)
        self.fake.estado = "open"
        self.c.tick()
        self.assertEqual(self.c.estado, "conectado")
        self.assertIsNotNone(self._pareado_em())
        self.rel.avancar(300)
        self.c.tick()
        self.assertEqual(self.fake.qrs_gerados, 1)

    def test_monitorar_avisa_so_na_transicao(self):
        self.fake.estado = "open"
        r = self.c.monitorar()
        self.assertEqual((r["evento"], r["avisar"]), ("ok", False))
        self.fake.estado = "close"
        r = self.c.monitorar()
        self.assertEqual((r["evento"], r["avisar"]), ("queda", True))
        r = self.c.monitorar()
        self.assertFalse(r["avisar"])
        self.assertEqual(self.fake.qrs_gerados, 0)

    def test_monitorar_detecta_428(self):
        self.fake.estado = "open"
        self.fake.reason_code = 428
        r = self.c.monitorar()
        self.assertEqual(r["evento"], "socket_morto")

    def test_reiniciar_usa_executor(self):
        chamadas = []
        self.c.reiniciar(lambda cmd, **kw: chamadas.append(cmd))
        self.assertEqual(len(chamadas), 1)
        self.assertIn("restart", " ".join(map(str, chamadas[0])))


if __name__ == "__main__":
    unittest.main()
