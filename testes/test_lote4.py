"""Pedido 63, lote 4 — erro com saída (auditoria T3, T5, T6) e docs/PROBLEMAS.md gerado do catálogo."""
import unittest
from pathlib import Path

from app import problemas
from app.erros import EvolutionIndisponivel, NaoAutorizado, VigiaErro
from app.paginas import _comum, conversas, demandas
from testes.test_server import TestServidor as _BaseServidor

RAIZ = Path(__file__).resolve().parents[1]


@unittest.skipUnless((RAIZ / "docs" / "PROBLEMAS.md").is_file(),
                     "docs/PROBLEMAS.md só existe no repositório privado; a edição pública não o leva")
class TestProblemasMd(unittest.TestCase):
    def test_arquivo_bate_com_o_catalogo(self):
        arq = (RAIZ / "docs" / "PROBLEMAS.md").read_text(encoding="utf-8")
        self.assertEqual(arq, problemas.markdown(),
                         "docs/PROBLEMAS.md divergiu: rode python3 -m app.problemas > docs/PROBLEMAS.md")

    def test_todo_problema_e_toda_peca_aparecem(self):
        md = problemas.markdown()
        for p in problemas.CATALOGO.values():
            self.assertIn(f"(`{p.codigo}`)", md)
            self.assertIn(p.peca, problemas.ROTULO_PECA_DOC)
            for passo in p.passos:
                self.assertIn(passo, md)

    def test_readme_aponta(self):
        self.assertIn("docs/PROBLEMAS.md", (RAIZ / "README.md").read_text(encoding="utf-8"))
        self.assertIn("PROBLEMAS.md", (RAIZ / "docs" / "README.md").read_text(encoding="utf-8"))


class TestT5MotivoDaFalha(unittest.TestCase):
    def test_tres_causas_tres_frases(self):
        fora = conversas._motivo_da_falha(EvolutionIndisponivel("x"))
        chave = conversas._motivo_da_falha(NaoAutorizado("x"))
        recusa = conversas._motivo_da_falha(VigiaErro("x"))
        self.assertEqual(len({fora, chave, recusa}), 3)
        self.assertIn("não respondeu", fora)
        self.assertIn("chave", chave)
        self.assertIn("recusou este envio", recusa)
        for frase in (fora, chave, recusa):
            self.assertIn("nada foi enviado", frase)

    def test_frase_antiga_sumiu(self):
        self.assertNotIn("recusou ou está fora", conversas._JS)
        import inspect
        fonte = inspect.getsource(conversas).replace(inspect.getsource(conversas._motivo_da_falha), "")
        self.assertNotIn("recusou ou está fora", fonte)


class TestT6Teto(unittest.TestCase):
    def test_rodape_avisa_quando_bate_no_teto(self):
        import inspect
        fonte = inspect.getsource(demandas)
        self.assertIn("no_teto = len(linhas) >= LIMITE", fonte)
        self.assertIn("estreite o período ou a classe para ver as outras", fonte)


class TestT3PaginaDeErro(unittest.TestCase):
    setUp = _BaseServidor.setUp
    _http = _BaseServidor._http
    NAV = {"Accept": "text/html,application/xhtml+xml", "Sec-Fetch-Mode": "navigate"}

    def test_navegacao_para_rota_inexistente_ganha_casca_e_saida(self):
        status, cab, corpo = self._http("GET", "/rota-que-nao-existe", cab=self.NAV)
        html = corpo.decode("utf-8") if isinstance(corpo, bytes) else corpo
        self.assertEqual(status, 404)
        self.assertIn("text/html", cab.get("Content-Type", ""))
        self.assertIn("erro-pagina", html)
        self.assertIn("Não encontrado", html)
        self.assertIn("history.back()", html)
        self.assertIn("Tentar de novo", html)
        self.assertNotIn('href="/avisos">Saúde do Greg', html.split("erro-pagina", 1)[1].split("</div></div>", 1)[0])

    def test_pedido_da_propria_pagina_passa_cru(self):
        status, cab, corpo = self._http("GET", "/rota-que-nao-existe", cab={"X-Vigia-Auto": "1", "Accept": "text/html"})
        self.assertEqual(status, 404)
        self.assertNotIn(b"erro-pagina", corpo if isinstance(corpo, bytes) else corpo.encode())

    def test_sem_sessao_o_404_nao_mostra_a_casca(self):
        status, cab, corpo = self._http("GET", "/rota-que-nao-existe", cab=self.NAV, cookie=False)
        self.assertEqual(status, 404)
        self.assertIn("text/plain", cab.get("Content-Type", ""))
        self.assertNotIn(b"erro-pagina", corpo)

    def test_erro_do_servidor_oferece_saude_e_post_nao_oferece_repetir(self):
        r = _comum.pagina_erro(self.ctx, 500, "o Greg teve um erro interno (E-abc123)", pode_repetir=False)
        html = r.corpo.decode("utf-8") if isinstance(r.corpo, bytes) else r.corpo
        self.assertEqual(r.status, 500)
        self.assertIn("O Greg teve um erro interno (E-abc123).", html)
        self.assertIn('href="/avisos">Saúde do Greg', html)
        self.assertNotIn("Tentar de novo", html)

    def test_mensagem_e_escapada(self):
        r = _comum.pagina_erro(self.ctx, 400, "<script>alert(1)</script>")
        html = r.corpo.decode("utf-8") if isinstance(r.corpo, bytes) else r.corpo
        self.assertNotIn("<script>alert(1)</script>", html)


del _BaseServidor
