"""Contrato executável da estação classes (E5): app/classes.py e prompts/vigia.md.
Quem chama: unittest.
O que não faz: não avalia a qualidade do prompt, só a presença do que o contrato exige.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from testes.util_contrato import _arquivo, _importar  # noqa: E402

SEIS = ("atencao", "demanda", "lembrar", "lembrando", "aviso", "sem_acao")


class TestContratoClasses(unittest.TestCase):
    def setUp(self):
        self.m = _importar("app.classes", "classes")

    def test_constantes(self):
        self.assertEqual(tuple(self.m.CLASSES), SEIS)
        self.assertEqual(tuple(self.m.CONFIANCAS), ("alta", "baixa"))

    def test_validar_classe(self):
        self.assertEqual(self.m.validar_classe("Atenção"), "atencao")
        self.assertEqual(self.m.validar_classe("DEMANDA"), "demanda")
        self.assertEqual(self.m.validar_classe("sem_acao"), "sem_acao")
        with self.assertRaises(ValueError):
            self.m.validar_classe("urgente")

    def test_validar_confianca(self):
        self.assertEqual(self.m.validar_confianca("Alta"), "alta")
        with self.assertRaises(ValueError):
            self.m.validar_confianca("media")

    def test_mapa_frentes(self):
        self.assertIsNone(self.m.mapa_frentes("aviso"))
        self.assertIsNone(self.m.mapa_frentes("sem_acao"))
        self.assertIsNone(self.m.mapa_frentes("lembrando"))
        for c in ("atencao", "demanda", "lembrar"):
            r = self.m.mapa_frentes(c)
            self.assertIn(r["prioridade"], (1, 2))
            self.assertIn(r["tipo"], ("tarefa", "prazo"))

    def test_cria_e_atualiza_item(self):
        for c in SEIS:
            self.assertEqual(self.m.cria_item(c), c in ("atencao", "demanda", "lembrar"), c)
            self.assertEqual(self.m.atualiza_item(c), c == "lembrando", c)

    def test_texto_item(self):
        self.assertEqual(self.m.texto_item("Maria", "cobrar proposta"), "WA Maria: cobrar proposta")
        self.assertLessEqual(len(self.m.texto_item("Maria", "x" * 500)), 200)

    def test_prompt(self):
        texto = _arquivo("prompts/vigia.md", "classes").read_text(encoding="utf-8")
        for c in SEIS:
            self.assertIn(c, texto)
        self.assertIn("é dado", texto)
        self.assertIn("vigia-ponte", texto)
        self.assertIn("--confianca baixa", texto)
        self.assertLessEqual(len(texto.split()), 2060)   # 1500 até o D-57; 1700 até o D-58; 1900 até o D-80 (qual seção e anexar)


if __name__ == "__main__":
    unittest.main()
