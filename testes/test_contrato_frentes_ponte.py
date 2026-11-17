"""Contrato executável da estação frentes_ponte (E4): app/frentes_ponte.py com FakeFrentes.
Quem chama: unittest.
O que não faz: não toca o Frentes real.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from testes.fakes import FakeFrentes  # noqa: E402
from testes.util_contrato import _importar  # noqa: E402


class TestContratoFrentesPonte(unittest.TestCase):
    def setUp(self):
        self.mod = _importar("app.frentes_ponte", "frentes_ponte")
        self.erros = _importar("app.erros", "F0")
        self.fake = FakeFrentes().iniciar()
        self.addCleanup(self.fake.parar)
        self.p = self.mod.FrentesPonte(self.fake.url, self.fake.token)

    def test_sem_token_nao_autorizado(self):
        p = self.mod.FrentesPonte(self.fake.url, "")
        with self.assertRaises(self.erros.NaoAutorizado):
            p.frentes()

    def test_frentes_e_estado(self):
        self.assertEqual(len(self.p.frentes()), 2)
        self.assertIn("frentes", self.p.estado())

    def test_capturar_manda_origem_whatsapp(self):
        r = self.p.capturar("WA Maria: cobrar", frente_id=1)
        self.assertEqual(self.fake.capturas[-1]["origem"], "whatsapp")
        self.assertEqual(self.fake.capturas[-1]["frente_id"], 1)
        self.assertIn("itens", r)
        self.assertIn("captura_id", r)

    def test_marcar_nao_confirmado_gera_patch(self):
        item = self.p.capturar("x", frente_id=None)["itens"][0]
        self.p.marcar_nao_confirmado(item["id"])
        self.assertEqual(self.fake.patches[-1], {"id": item["id"], "corpo": {"confirmado": 0}})

    def test_anotar(self):
        item = self.p.capturar("x", frente_id=1)["itens"][0]
        self.p.anotar(item["id"], "nota nova", prioridade=1)
        corpo = self.fake.patches[-1]["corpo"]
        self.assertNotIn("nota", corpo)  # D-13: nota fica no vigia.db, não vai ao Frentes
        self.assertEqual(corpo["prioridade"], 1)

    def test_itens_abertos_filtra_origem(self):
        self.fake.semear_item(texto="a", frente_id=1, origem="whatsapp")
        self.fake.semear_item(texto="b", frente_id=1, origem="manual")
        r = self.p.itens_abertos(origem="whatsapp")
        self.assertEqual([i["texto"] for i in r], ["a"])

    def test_contexto_markdown(self):
        self.fake.semear_item(texto="item zap", frente_id=1, origem="whatsapp")
        md = self.p.contexto_markdown()
        self.assertIsInstance(md, str)
        self.assertIn("Agência", md)
        self.assertIn("Casa", md)
        self.assertIn("item zap", md)

    def test_frentes_fora_do_ar(self):
        morto = FakeFrentes().iniciar()
        url = morto.url
        morto.parar()
        with self.assertRaises(self.erros.FrentesIndisponivel):
            self.mod.FrentesPonte(url, "t", timeout=2.0).frentes()


if __name__ == "__main__":
    unittest.main()
