"""Contrato executável da estação evolution_client (E2): app/evolution_client.py com FakeEvolution.
Quem chama: unittest.
O que não faz: não fala com a Evolution real.
"""
import base64
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from testes.fakes import FakeEvolution  # noqa: E402
from testes.util_contrato import _importar  # noqa: E402


class TestContratoEvolutionClient(unittest.TestCase):
    def setUp(self):
        self.mod = _importar("app.evolution_client", "evolution_client")
        self.erros = _importar("app.erros", "F0")
        self.fake = FakeEvolution().iniciar()
        self.addCleanup(self.fake.parar)
        self.c = self.mod.EvolutionClient(self.fake.url, self.fake.instancia, self.fake.apikey)

    def test_estado_nunca_gera_qr(self):
        self.fake.estado = "open"
        e = self.c.estado()
        self.assertEqual(e["state"], "open")
        self.assertIn("reason_code", e)
        self.assertIn("reason_em", e)
        self.assertEqual(self.fake.qrs_gerados, 0)
        self.assertFalse([c for c in self.fake.chamadas if "/instance/connect/" in c[1]])

    def test_estado_traz_reason_code(self):
        self.fake.estado = "open"
        self.fake.reason_code = 428
        self.assertEqual(self.c.estado()["reason_code"], 428)

    def test_gerar_qr_uma_chamada(self):
        qr = self.c.gerar_qr()
        self.assertTrue(qr["base64"])
        self.assertTrue(qr["code"])
        self.assertEqual(self.fake.qrs_gerados, 1)

    def test_midia_texto_levanta_midia_invalida(self):
        self.fake.midia_base64_texto = base64.b64encode(b"isto e texto").decode()
        with self.assertRaises(self.erros.MidiaInvalida):
            self.c.midia_base64({"id": "x", "remoteJid": "1@s.whatsapp.net", "fromMe": False})

    def test_midia_valida(self):
        b = self.c.midia_base64({"id": "x", "remoteJid": "1@s.whatsapp.net", "fromMe": False})
        self.assertTrue(b.startswith(b"\xff\xd8\xff"))

    def test_logout_idempotente(self):
        self.fake.estado = "connecting"
        self.c.logout()
        self.assertEqual(self.fake.estado, "close")
        self.c.logout()

    def test_enviar_texto_devolve_id(self):
        i = self.c.enviar_texto("5592000000000", "oi")
        self.assertTrue(i.startswith("3EB0"))
        self.assertEqual(len(self.fake.enviadas), 1)
        self.assertEqual(self.fake.enviadas[0]["number"], "5592000000000")
        self.assertEqual(self.fake.enviadas[0]["text"], "oi")

    def test_apikey_errada_nao_autorizado(self):
        c = self.mod.EvolutionClient(self.fake.url, self.fake.instancia, "errada")
        with self.assertRaises(self.erros.NaoAutorizado):
            c.estado()

    def test_servidor_fora_evolution_indisponivel(self):
        porta_morta = FakeEvolution().iniciar()
        url = porta_morta.url
        porta_morta.parar()
        c = self.mod.EvolutionClient(url, "i", "k", timeout=2.0)
        with self.assertRaises(self.erros.EvolutionIndisponivel):
            c.estado()

    def test_erros_herdam_de_vigiaerro(self):
        for n in ("EvolutionIndisponivel", "NaoAutorizado", "MidiaInvalida"):
            self.assertTrue(issubclass(getattr(self.erros, n), self.erros.VigiaErro))


if __name__ == "__main__":
    unittest.main()
