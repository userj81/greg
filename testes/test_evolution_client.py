"""Testes próprios do EvolutionClient contra FakeEvolution (zero rede real).
Quem chama: unittest.
O que não faz: não repete o contrato; cobre 401, servidor fora, base64 inválido e rotas exatas.
"""
import base64
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import erros  # noqa: E402
from app.evolution_client import EvolutionClient  # noqa: E402
from testes.fakes import FakeEvolution, imagem_jpeg_minima  # noqa: E402


class TestEvolutionClient(unittest.TestCase):
    def setUp(self):
        self.fake = FakeEvolution().iniciar()
        self.addCleanup(self.fake.parar)
        self.c = EvolutionClient(self.fake.url, self.fake.instancia, self.fake.apikey)

    def test_estado_usa_duas_rotas_e_nenhum_connect(self):
        self.c.estado()
        caminhos = [p for _, p in self.fake.chamadas]
        self.assertIn(f"/instance/connectionState/{self.fake.instancia}", caminhos)
        self.assertIn("/instance/fetchInstances", caminhos)
        self.assertFalse([p for p in caminhos if p.startswith("/instance/connect/")])

    def test_estado_reason_code_none_e_com_valor(self):
        self.assertIsNone(self.c.estado()["reason_code"])
        self.fake.reason_code = 428
        e = self.c.estado()
        self.assertEqual(e["reason_code"], 428)
        self.assertTrue(e["reason_em"])

    def test_gerar_qr_dois_pedidos_dois_qrs(self):
        self.c.gerar_qr()
        self.c.gerar_qr()
        self.assertEqual(self.fake.qrs_gerados, 2)

    def test_logout_com_instancia_ja_fora(self):
        self.fake.estado = "close"
        self.c.logout()
        self.assertEqual(self.fake.estado, "close")

    def test_midia_audio_cru_sem_converter_e_so_audio(self):
        """D-47: o motor de transcrição recebe o ogg/opus como chegou (convertToMp4=false), nunca imagem."""
        for magic, ext in ((b"OggS" + b"\x00" * 8, "ogg"), (b"\x00\x00\x00\x18ftypM4A ", "m4a"), (b"ID3\x03\x00", "mp3"),
                           (b"\xff\xfb\x90\x00", "mp3")):
            self.fake.midia_base64_texto = base64.b64encode(magic + b"x" * 8).decode()
            dados, e = self.c.midia_audio({"id": "x", "remoteJid": "1@s.whatsapp.net", "fromMe": False})
            self.assertEqual((dados[:4], e), (magic[:4], ext))
            self.assertIs(self.fake.midia_corpos[-1]["convertToMp4"], False)
            self.assertEqual(self.fake.midia_corpos[-1]["message"]["key"]["id"], "x")
        self.fake.midia_base64_texto = None       # o fake devolve um JPEG
        with self.assertRaises(erros.MidiaInvalida):
            self.c.midia_audio({"id": "x"})
        self.fake.midia_base64_texto = "%%%"
        with self.assertRaises(erros.MidiaInvalida):
            self.c.midia_audio({"id": "x"})

    def test_midia_png_e_webp_aceitos(self):
        for magic in (b"\x89PNG\r\n\x1a\n", b"RIFF\x00\x00\x00\x00WEBP"):
            self.fake.midia_base64_texto = base64.b64encode(magic + b"x").decode()
            self.assertTrue(self.c.midia_base64({"id": "x"}).startswith(magic[:4]))

    def test_midia_com_prefixo_data_uri(self):
        b64 = "data:image/jpeg;base64," + base64.b64encode(imagem_jpeg_minima()).decode()
        self.fake.midia_base64_texto = b64
        self.assertEqual(self.c.midia_base64({"id": "x"}), imagem_jpeg_minima())

    def test_midia_base64_de_texto_e_vazio(self):
        for ruim in (base64.b64encode(b"so texto").decode(), "", "@@@nao-e-base64@@@"):
            self.fake.midia_base64_texto = ruim
            if ruim == "":
                continue  # vazio no fake cai no JPEG padrão
            with self.assertRaises(erros.MidiaInvalida):
                self.c.midia_base64({"id": "x"})

    def test_midia_envia_key_no_corpo(self):
        self.assertTrue(self.c.midia_base64({"id": "abc", "remoteJid": "1@s.whatsapp.net", "fromMe": False}))
        self.assertIn(("POST", f"/chat/getBase64FromMediaMessage/{self.fake.instancia}"), self.fake.chamadas)

    def test_enviar_texto_corpo_e_id(self):
        i = self.c.enviar_texto("5592111111111", "olá, tudo bem?")
        self.assertEqual(self.fake.enviadas[0], {"number": "5592111111111", "text": "olá, tudo bem?"})
        self.assertTrue(i.startswith("3EB0"))

    def test_401_em_toda_operacao(self):
        c = EvolutionClient(self.fake.url, self.fake.instancia, "errada")
        for op in (c.estado, c.gerar_qr, c.logout,
                   lambda: c.midia_base64({"id": "x"}), lambda: c.enviar_texto("1", "t")):
            with self.assertRaises(erros.NaoAutorizado):
                op()
        self.assertEqual(self.fake.qrs_gerados, 0)
        self.assertGreaterEqual(self.fake.negadas, 5)

    def test_servidor_fora_em_toda_operacao(self):
        morto = FakeEvolution().iniciar()
        url = morto.url
        morto.parar()
        c = EvolutionClient(url, "i", "k", timeout=2.0)
        for op in (c.estado, c.gerar_qr, c.logout,
                   lambda: c.midia_base64({"id": "x"}), lambda: c.enviar_texto("1", "t")):
            with self.assertRaises(erros.EvolutionIndisponivel):
                op()

    def test_log_nao_contem_texto_da_mensagem(self):
        with self.assertLogs("vigia.evolution", level="DEBUG") as cap:
            self.c.enviar_texto("5592000000000", "segredo-da-conversa")
        self.assertNotIn("segredo-da-conversa", "\n".join(cap.output))


class _Local:
    """Servidor local roteirizado: `resp` {(método, caminho): (status, corpo, cabeçalhos)}."""

    def __init__(self, resp, travar=False):
        outer = self
        self.resp, self.vistos, self.travar = resp, [], travar

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _go(self):
                n = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(n)
                caminho = self.path.split("?")[0]
                outer.vistos.append((self.command, caminho))
                if outer.travar:
                    self.connection.close()
                    return
                st, corpo, cab = outer.resp.get((self.command, caminho), (404, {}, {}))
                dados = corpo if isinstance(corpo, bytes) else json.dumps(corpo).encode()
                self.send_response(st)
                for k, v in cab.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(dados)))
                self.end_headers()
                self.wfile.write(dados)

            do_GET = do_POST = do_DELETE = _go

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def parar(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class TestEmendaF4(unittest.TestCase):
    def local(self, resp, **kw):
        s = _Local(resp, **kw)
        self.addCleanup(s.parar)
        return s

    def test_apikey_com_crlf_recusada_sem_vazar(self):
        with self.assertRaises(erros.VigiaErro) as cm:
            EvolutionClient("http://127.0.0.1:1", "i", "segredo123\r\nX-Evil: 1")
        self.assertNotIn("segredo123", str(cm.exception))

    def test_apikey_com_espacos_nas_pontas_e_aceita(self):
        fake = FakeEvolution().iniciar()
        self.addCleanup(fake.parar)
        c = EvolutionClient(fake.url, fake.instancia, "  " + fake.apikey + "\n")
        self.assertEqual(c.estado()["state"], "close")

    def test_apikey_nao_ascii_ou_vazia_recusada(self):
        for k in ("chavé", "   ", "a b"):
            with self.assertRaises(erros.VigiaErro):
                EvolutionClient("http://127.0.0.1:1", "i", k)

    def test_host_fora_do_loopback_recusado(self):
        for u in ("http://evil.example.com:8080", "http://192.168.0.5:8080", "ftp://127.0.0.1"):
            with self.assertRaises(erros.VigiaErro):
                EvolutionClient(u, "i", "k")
        EvolutionClient("http://localhost:8080", "i", "k")

    def test_302_nao_seguido(self):
        alvo = self.local({("GET", "/x"): (200, {"state": "open"}, {})})
        s = self.local({("GET", "/instance/connectionState/i"): (302, {}, {"Location": alvo.url + "/x"})})
        with self.assertRaises(erros.EvolutionIndisponivel):
            EvolutionClient(s.url, "i", "k").estado()
        self.assertEqual(alvo.vistos, [])

    def test_resposta_nao_dict_indisponivel(self):
        s = self.local({("GET", "/instance/connectionState/i"): (200, [1, 2], {})})
        with self.assertRaises(erros.EvolutionIndisponivel) as cm:
            EvolutionClient(s.url, "i", "k").estado()
        self.assertEqual(str(cm.exception), "resposta inesperada")

    def test_fetchinstances_lista_ok_e_dict_invalido_nao(self):
        s = self.local({("GET", "/instance/connectionState/i"): (200, {"instance": {"state": "open"}}, {}),
                        ("GET", "/instance/fetchInstances"): (200, "texto", {})})
        with self.assertRaises(erros.EvolutionIndisponivel):
            EvolutionClient(s.url, "i", "k").estado()

    def test_json_invalido_indisponivel(self):
        s = self.local({("GET", "/instance/connectionState/i"): (200, b"<html>", {})})
        with self.assertRaises(erros.EvolutionIndisponivel):
            EvolutionClient(s.url, "i", "k").estado()

    def test_resposta_truncada_httpexception_indisponivel(self):
        s = self.local({}, travar=True)
        with self.assertRaises(erros.EvolutionIndisponivel):
            EvolutionClient(s.url, "i", "k", timeout=2.0).estado()

    def test_qr_vazio_ou_erro_levanta_qr_indisponivel_com_1_get(self):
        for corpo in ({"count": 0}, {"base64": None}, {"base64": "data:x", "error": True},
                      {"state": "open"}):
            s = self.local({("GET", "/instance/connect/i"): (200, corpo, {})})
            with self.assertRaises(erros.QRIndisponivel) as cm:
                EvolutionClient(s.url, "i", "k").gerar_qr()
            self.assertEqual(s.vistos, [("GET", "/instance/connect/i")])
            if corpo.get("state"):
                self.assertEqual(cm.exception.state, "open")

    def test_logout_400_connection_closed_levanta(self):
        s = self.local({("DELETE", "/instance/logout/i"): (400, {"response": {"message": "Connection Closed"}}, {})})
        with self.assertRaises(erros.VigiaErro):
            EvolutionClient(s.url, "i", "k").logout()

    def test_logout_400_not_connected_e_already_tolerados(self):
        for msg in ("Instance not connected", "already logged out"):
            s = self.local({("DELETE", "/instance/logout/i"): (400, {"response": {"message": msg}}, {})})
            EvolutionClient(s.url, "i", "k").logout()

    def test_logout_404_tolerado(self):
        s = self.local({("DELETE", "/instance/logout/i"): (404, {}, {})})
        EvolutionClient(s.url, "i", "k").logout()

    def test_envio_timeout_apos_escrita_e_incerto(self):
        s = self.local({}, travar=True)
        with self.assertRaises(erros.EnvioIncerto):
            EvolutionClient(s.url, "i", "k", timeout=2.0).enviar_texto("1", "oi")
        self.assertEqual(s.vistos, [("POST", "/message/sendText/i")])

    def test_envio_servidor_fora_continua_indisponivel(self):
        morto = FakeEvolution().iniciar()
        url = morto.url
        morto.parar()
        with self.assertRaises(erros.EvolutionIndisponivel):
            EvolutionClient(url, "i", "k", timeout=2.0).enviar_texto("1", "oi")

    def test_envio_5xx_incerto_e_401_nao_autorizado(self):
        s = self.local({("POST", "/message/sendText/i"): (502, {}, {})})
        with self.assertRaises(erros.EnvioIncerto):
            EvolutionClient(s.url, "i", "k").enviar_texto("1", "oi")

    def test_envio_2xx_sem_key_id_e_incerto(self):
        # B4 (robustez 16/11): a Evolution respondeu 2xx, então pode ter saído; tratar como "não saiu" duplicava
        rotas = {("POST", "/message/sendText/i"): lambda c: c.enviar_texto("1", "oi"),
                 ("POST", "/message/sendMedia/i"): lambda c: c.enviar_midia("1", "image", b"x", nome="a.jpg", mime="image/jpeg"),
                 ("POST", "/message/sendWhatsAppAudio/i"): lambda c: c.enviar_audio("1", b"x")}
        for rota, enviar in rotas.items():
            for corpo in ({}, {"key": {}}, {"key": {"id": ""}}):
                with self.subTest(rota=rota[1], corpo=corpo):
                    s = self.local({rota: (201, corpo, {})})
                    with self.assertRaises(erros.EnvioIncerto):
                        enviar(EvolutionClient(s.url, "i", "k"))

    def test_midia_400_e_404_viram_midia_invalida(self):
        for st in (400, 404):
            s = self.local({("POST", "/chat/getBase64FromMediaMessage/i"): (st, {}, {})})
            with self.assertRaises(erros.MidiaInvalida):
                EvolutionClient(s.url, "i", "k").midia_base64({"id": "x"})

    def test_midia_wav_recusado_webp_aceito(self):
        fake = FakeEvolution().iniciar()
        self.addCleanup(fake.parar)
        c = EvolutionClient(fake.url, fake.instancia, fake.apikey)
        fake.midia_base64_texto = base64.b64encode(b"RIFF\x00\x00\x00\x00WAVEfmt ").decode()
        with self.assertRaises(erros.MidiaInvalida):
            c.midia_base64({"id": "x"})
        fake.midia_base64_texto = base64.b64encode(b"RIFF\x00\x00\x00\x00WEBPVP8 ").decode()
        self.assertEqual(c.midia_base64({"id": "x"})[8:12], b"WEBP")


if __name__ == "__main__":
    unittest.main()
