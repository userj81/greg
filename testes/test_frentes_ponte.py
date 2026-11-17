"""Testes próprios de app/frentes_ponte.py contra FakeFrentes.
Quem chama: unittest.
O que não faz: não toca o Frentes real nem a rede fora de 127.0.0.1.
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.erros import FrentesIndisponivel, FrentesRecusou, NaoAutorizado, VigiaErro  # noqa: E402
from app.frentes_ponte import FrentesPonte  # noqa: E402
from testes.fakes import FakeFrentes  # noqa: E402


class TestFrentesPonte(unittest.TestCase):
    def setUp(self):
        self.fake = FakeFrentes().iniciar()
        self.addCleanup(self.fake.parar)
        self.p = FrentesPonte(self.fake.url + "/", self.fake.token)

    def test_token_errado_401(self):
        with self.assertRaises(NaoAutorizado):
            FrentesPonte(self.fake.url, "errado").estado()
        self.assertEqual(self.fake.negadas, 1)

    def test_token_vazio_nao_chega_ao_servidor(self):
        with self.assertRaises(NaoAutorizado):
            FrentesPonte(self.fake.url, "").frentes()
        self.assertEqual(self.fake.chamadas, [])

    def test_servidor_fora(self):
        morto = FakeFrentes().iniciar()
        url = morto.url
        morto.parar()
        with self.assertRaises(FrentesIndisponivel):
            FrentesPonte(url, "t", timeout=2.0).estado()

    def test_erros_sao_vigiaerro(self):
        self.assertTrue(issubclass(FrentesIndisponivel, VigiaErro))
        self.assertTrue(issubclass(NaoAutorizado, VigiaErro))

    def test_captura_vazia_vira_recusou_sem_texto_na_mensagem(self):
        with self.assertRaises(FrentesRecusou) as c:
            self.p.capturar("   ", frente_id=1)
        self.assertIn("400", str(c.exception))
        self.assertFalse(issubclass(FrentesRecusou, FrentesIndisponivel))
        self.assertTrue(issubclass(FrentesRecusou, VigiaErro))

    def test_capturar_usa_post_itens_com_campos(self):
        r = self.p.capturar("a", frente_id=2, tipo="prazo", prazo="2026-11-10", prioridade=1)
        self.assertEqual(self.fake.chamadas[-1], ("POST", "/api/itens"))
        item = r["itens"][0]
        self.assertEqual((item["frente_id"], item["tipo"], item["prazo"], item["prioridade"]),
                         (2, "prazo", "2026-11-10", 1))
        self.assertNotIn("frente_invalida", r)

    def test_capturar_frente_inexistente_vai_ao_inbox(self):
        r = self.p.capturar("a", frente_id=99)
        self.assertTrue(r["frente_invalida"])
        self.assertIsNone(r["itens"][0]["frente_id"])
        self.assertIsNone(self.fake.capturas[-1]["frente_id"])

    def test_capturar_none_vai_ao_inbox_sem_flag(self):
        r = self.p.capturar("a", frente_id=None)
        self.assertIsNone(r["itens"][0]["frente_id"])
        self.assertNotIn("frente_invalida", r)

    def test_anotar_nunca_manda_nota(self):
        item = self.p.capturar("x", frente_id=1)["itens"][0]
        self.assertEqual(self.p.anotar(item["id"], "nota"), {})
        self.assertEqual(self.fake.patches, [])
        self.p.anotar(item["id"], "nota", prioridade=1)
        self.assertEqual(self.fake.patches[-1]["corpo"], {"prioridade": 1})

    def test_itens_abertos_uma_chamada_inclui_inbox(self):
        self.fake.semear_item(texto="a", frente_id=1, origem="whatsapp")
        self.fake.semear_item(texto="i", frente_id=None, origem="whatsapp")
        antes = len(self.fake.chamadas)
        r = self.p.itens_abertos(origem="whatsapp")
        self.assertEqual(len(self.fake.chamadas) - antes, 1)
        self.assertEqual({i["texto"] for i in r}, {"a", "i"})

    def test_404_vira_recusou(self):
        with self.assertRaises(FrentesRecusou):
            self.p._chamar("GET", "/api/nao-existe")

    def test_redirect_nao_e_seguido(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:1/")
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        with self.assertRaises(FrentesIndisponivel):
            FrentesPonte(f"http://127.0.0.1:{srv.server_address[1]}", "t", timeout=2).frentes()

    def test_estado_shape_real(self):
        self.assertEqual(set(self.p.estado()), {"gerado_em", "hoje", "frentes", "inbox", "agenda", "totais"})

    def test_itens_abertos_so_abertos(self):
        self.fake.semear_item(texto="a", frente_id=1, origem="whatsapp")
        self.fake.semear_item(texto="fechado", frente_id=1, origem="whatsapp", status="feito")
        self.assertEqual([i["texto"] for i in self.p.itens_abertos()], ["a"])

    def test_itens_abertos_janela_de_dias(self):
        velho = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        novo = datetime.now(timezone.utc).isoformat()
        self.fake.semear_item(texto="velho", frente_id=1, origem="whatsapp", criado_em=velho)
        self.fake.semear_item(texto="novo", frente_id=1, origem="whatsapp", criado_em=novo)
        self.assertEqual([i["texto"] for i in self.p.itens_abertos(origem="whatsapp")], ["novo"])

    def test_shape_do_markdown(self):
        self.fake.semear_item(texto="cobrar   Maria", frente_id=1, origem="whatsapp")
        self.fake.semear_item(texto="manual", frente_id=2, origem="manual")
        self.fake.semear_item(texto="solto", frente_id=None, origem="whatsapp")
        md = self.p.contexto_markdown()
        linhas = md.splitlines()
        self.assertEqual(linhas[0], "## Frentes")
        self.assertIn("- id 1 · Agência · trabalho · apelidos: agencia, exemplo · abertos: 1", linhas)
        self.assertIn("- id 2 · Casa · pessoal · apelidos: casa · abertos: 1", linhas)
        self.assertIn("## Itens do WhatsApp (14 dias)", linhas)
        self.assertIn("- #1 [Agência] cobrar Maria", linhas)
        self.assertIn("- #3 [Inbox] solto", linhas)
        self.assertNotIn("manual", "\n".join(linhas[linhas.index("## Itens do WhatsApp (14 dias)"):]))
        self.assertTrue(md.endswith("\n"))

    def test_markdown_sem_itens(self):
        self.assertIn("- (nenhum)", self.p.contexto_markdown())


class TestCriarEAlterarFrente(unittest.TestCase):
    """Pedido 41: criar frente (Família) e arquivar/desarquivar pela ponte."""

    def setUp(self):
        self.fake = FakeFrentes().iniciar()
        self.addCleanup(self.fake.parar)
        self.p = FrentesPonte(self.fake.url, self.fake.token)

    def ids(self):
        return [f["id"] for f in self.p.frentes()]

    def test_criar_frente_devolve_id_e_aparece_nas_ativas(self):
        r = self.p.criar_frente("  Família  ", tipo="pessoal", apelidos=["familia", "família"], contexto="gente de casa")
        f = r["frente"]
        self.assertEqual((f["nome"], f["tipo"], f["apelidos"], f["status"]), ("Família", "pessoal", ["familia", "família"], "ativa"))
        self.assertIn(r["id"], self.ids())
        self.assertEqual(self.fake.chamadas[-2], ("POST", "/api/frentes"))

    def test_arquivar_some_das_ativas_e_desarquivar_volta(self):
        self.assertIn(2, self.ids())
        self.p.alterar_frente(2, status="arquivada")
        self.assertNotIn(2, self.ids())
        self.assertEqual(self.fake.frentes_patches[-1], {"id": 2, "corpo": {"status": "arquivada"}})
        self.p.alterar_frente(2, status="ativa")
        self.assertIn(2, self.ids())

    def test_alterar_sem_campo_permitido_nao_chama(self):
        self.assertEqual(self.p.alterar_frente(2, bobagem=1), {})
        self.assertEqual(self.fake.chamadas, [])

    def test_recusas_viram_frentesrecusou(self):
        with self.assertRaises(FrentesRecusou):
            self.p.criar_frente("Agência")                      # 409 nome repetido
        with self.assertRaises(FrentesRecusou):
            self.p.criar_frente("")                             # 400
        with self.assertRaises(FrentesRecusou):
            self.p.alterar_frente(2, status="qualquer")         # 400 status inválido
        with self.assertRaises(FrentesRecusou):
            self.p.alterar_frente(999, status="ativa")          # 404


if __name__ == "__main__":
    unittest.main()
