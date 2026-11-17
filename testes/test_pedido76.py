"""Pedido 76 (D-77): o Greg abre sempre no tema claro e a pessoa escolhe a aparência no primeiro passo do início.
Quem chama: unittest. Não toca o preferencias.json real (ARQUIVO aponta para a pasta temporária do teste)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import preferencias as pf  # noqa: E402
from testes.test_pedido69_telas import BaseInicio  # noqa: E402

RAIZ = Path(__file__).resolve().parents[1]


class _ComArquivo:
    def _arquivo(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        antes = pf.ARQUIVO
        pf.ARQUIVO = Path(tmp.name) / "preferencias.json"
        self.addCleanup(lambda: setattr(pf, "ARQUIVO", antes))
        return pf.ARQUIVO


class TestPadraoClaro(_ComArquivo, unittest.TestCase):
    def test_sem_preferencia_gravada_o_greg_abre_no_claro(self):
        self._arquivo()
        self.assertEqual(pf.ler()["tema"], "claro")
        self.assertEqual(pf.atributos_html(pf.ler()), ' data-tema="claro"')
        self.assertEqual(pf.validar({"tema": "inventado"})["tema"], "claro")

    def test_as_tres_opcoes_continuam_e_claro_vem_primeiro(self):
        self.assertEqual(list(pf.TEMAS), ["claro", "escuro", "sistema"])
        self.assertEqual(pf.atributos_html(pf.validar({"tema": "sistema"})), "")

    def test_trocar_muda_uma_preferencia_e_mantem_as_outras(self):
        arq = self._arquivo()
        pf.gravar({"fonte": 15, "tema": "claro", "acento": "azul", "envio_min": 60, "retencao_dias": 90})
        pf.trocar("tema", "escuro")
        self.assertEqual(json.loads(arq.read_text()),
                         {"fonte": 15, "tema": "escuro", "acento": "azul", "envio_min": 60, "retencao_dias": 90,
                          "leitura_min": 30, "leitura_de": 0, "leitura_ate": 0})   # pedido 81: três chaves novas, no padrão

    def test_telas_nativas_nao_trocam_sozinhas_para_o_escuro(self):
        for nome in ("main.swift", "instalador.swift"):
            self.assertNotIn("prefers-color-scheme", (RAIZ / "macapp" / "Vigia" / nome).read_text(encoding="utf-8"))


class TestEscolhaNoInicio(_ComArquivo, BaseInicio):
    def setUp(self):
        super().setUp()
        self._arquivo()

    def test_primeiro_passo_mostra_as_tres_opcoes_com_o_claro_marcado(self):
        html = self.get("/inicio/boas-vindas").corpo
        self.assertIn('action="/inicio/tema"', html)
        for valor in ("claro", "escuro", "sistema"):
            self.assertIn(f'name="tema" value="{valor}"', html)
        self.assertIn('value="claro" class="btn primario" aria-pressed="true"', html)
        self.assertIn('data-tema="claro"', html)

    def test_escolher_grava_e_a_tela_volta_no_tema_escolhido(self):
        r = self.post("/inicio/tema", {"tema": "escuro"})
        self.assertEqual(r.status, 303)
        self.assertEqual(pf.ler()["tema"], "escuro")
        html = self.get("/inicio/boas-vindas").corpo
        self.assertIn('data-tema="escuro"', html)
        self.assertIn('value="escuro" class="btn primario" aria-pressed="true"', html)

    def test_valor_invalido_e_recusado_e_nada_muda(self):
        self.assertEqual(self.post("/inicio/tema", {"tema": "roxo"}).status, 400)
        self.assertEqual(pf.ler()["tema"], "claro")


del BaseInicio      # só as classes deste arquivo rodam aqui
