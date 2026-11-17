"""Contrato executável da estação paginas (E9): app/paginas/*.py e app/templates/conexao.html.
Quem chama: unittest. Handlers recebem Requisicao (app/http.py) e devolvem Resposta.
O que não faz: não sobe o servidor (app/server.py é costura do coordenador).
"""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from testes.fakes import FakeEvolution, FakeFrentes  # noqa: E402
from testes.util_contrato import RelogioFalso, _arquivo, _importar  # noqa: E402

MODULOS = ("conexao", "demandas", "conversas", "frentes", "saude", "enviar")
TOKEN_ENVIO = "tok-envio-teste"


class TestContratoPaginas(unittest.TestCase):
    def setUp(self):
        self.http = _importar("app.http", "F0")
        self.pag = {n: _importar(f"app.paginas.{n}", "paginas") for n in MODULOS}
        evo_mod = _importar("app.evolution_client", "evolution_client")
        banco_mod = _importar("app.banco", "banco")
        fp_mod = _importar("app.frentes_ponte", "frentes_ponte")
        con_mod = _importar("app.conexao", "conexao")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fe = FakeEvolution().iniciar()
        self.addCleanup(self.fe.parar)
        self.ff = FakeFrentes().iniciar()
        self.addCleanup(self.ff.parar)
        self.evo = evo_mod.EvolutionClient(self.fe.url, self.fe.instancia, self.fe.apikey)
        self.banco = banco_mod.Banco(str(Path(self.tmp.name) / "vigia.db"))
        self.ponte = fp_mod.FrentesPonte(self.ff.url, self.ff.token)
        cursor_mod = _importar("app.cursor", "banco")
        dados = Path(self.tmp.name)
        self.conexao = con_mod.Conexao(self.evo, self.banco, relogio=RelogioFalso(),
                                       cursor=cursor_mod.Cursor(dados / "cursor.json"), estado_arq=dados / "conexao.json")
        self.ctx = SimpleNamespace(banco=self.banco, ponte=self.ponte, conexao=self.conexao, evo=self.evo,
                                   config={}, tokens=SimpleNamespace(envio=TOKEN_ENVIO, painel="tok-painel"))

    def _req(self, metodo, caminho, corpo=None, cab=None):
        return self.http.Requisicao(metodo, caminho, {}, corpo or {}, {}, self.ctx, cabecalhos=cab or {})

    def test_todos_os_modulos_expoem_rotas(self):
        for n, m in self.pag.items():
            self.assertIsInstance(m.ROTAS, dict, n)
            self.assertTrue(m.ROTAS, n)
            for caminho, h in m.ROTAS.items():
                self.assertTrue(caminho.startswith("/"), caminho)
                self.assertTrue(callable(h), caminho)
        todas = {c for m in self.pag.values() for c in m.ROTAS}
        for c in ("/conexao", "/conexao/qr", "/conexao/tick", "/conexao/reiniciar", "/conexao/pausar",
                  "/demandas", "/conversas", "/frentes", "/saude", "/enviar"):
            self.assertIn(c, todas)

    def test_get_conexao_nao_gera_qr(self):
        r = self.pag["conexao"].ROTAS["/conexao"](self._req("GET", "/conexao"))
        self.assertEqual(r.status, 200)
        self.assertEqual(self.fe.qrs_gerados, 0)

    def test_post_qr_gera_uma_vez(self):
        r = self.pag["conexao"].ROTAS["/conexao/qr"](self._req("POST", "/conexao/qr"))
        self.assertIn(r.status, (200, 302, 303))
        self.assertEqual(self.fe.qrs_gerados, 1)

    def test_tick_nao_gera_qr_ocioso(self):
        self.pag["conexao"].ROTAS["/conexao/tick"](self._req("GET", "/conexao/tick"))
        self.assertEqual(self.fe.qrs_gerados, 0)

    def test_enviar_sem_header_401_e_com_header_200(self):
        h = self.pag["enviar"].ROTAS["/enviar"]
        corpo = {"para": "5592900000000", "texto": "oi"}
        r = h(self._req("POST", "/enviar", corpo))
        self.assertEqual(r.status, 401)
        r = h(self._req("POST", "/enviar", corpo, {"x-vigia-envio": "errado", "X-Vigia-Envio": "errado"}))
        self.assertEqual(r.status, 401)
        self.assertEqual(len(self.fe.enviadas), 0)
        r = h(self._req("POST", "/enviar", corpo, {"x-vigia-envio": TOKEN_ENVIO, "X-Vigia-Envio": TOKEN_ENVIO}))
        self.assertEqual(r.status, 200)
        self.assertEqual(len(self.fe.enviadas), 1)

    def test_enviar_get_form(self):
        r = self.pag["enviar"].ROTAS["/enviar"](self._req("GET", "/enviar"))
        self.assertEqual(r.status, 200)

    def test_saude_json_sem_cookie(self):
        r = self.pag["saude"].ROTAS["/saude"](self._req("GET", "/saude"))
        self.assertEqual(r.status, 200)
        self.assertIn("json", r.tipo)
        corpo = r.corpo.decode("utf-8") if isinstance(r.corpo, bytes) else r.corpo
        self.assertIsInstance(json.loads(corpo), dict)

    def test_template_conexao_sem_polling_de_qr(self):
        t = _arquivo("app/templates/conexao.html", "paginas").read_text(encoding="utf-8")
        self.assertNotRegex(t, r"setInterval[^;]*conexao/qr")
        for trecho in re.findall(r"setInterval\(.*?\)\s*;?", t, flags=re.S):
            self.assertNotIn("/conexao/qr", trecho)
        self.assertNotRegex(t, r"https?://(?!127\.0\.0\.1|localhost)")  # sem CDN nem fonte externa


if __name__ == "__main__":
    unittest.main()
