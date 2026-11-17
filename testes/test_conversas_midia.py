"""Pedido 50: mídia na conversa. Ver (foto grande, vídeo, documento, baixar) e enviar (foto, vídeo, arquivo, áudio de
voz) pelo próprio Greg, com a mesma trava do texto. Evolution pelo FakeEvolution; banco da Evolution por psql falso."""
import base64
import hashlib
import http.client
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import server as srv_mod  # noqa: E402
from app.paginas import conversas  # noqa: E402
from testes.fakes import imagem_jpeg_minima  # noqa: E402
from testes.test_conversas_zap import AGORA, ANA, G1, LID, TOKEN, BaseZap, k  # noqa: E402
from testes.test_paginas import texto  # noqa: E402

MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64
PDF = b"%PDF-1.4\n<script>alert(1)</script>\n%%EOF"
OGG = b"OggS" + b"\x00" * 60
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40


class BaseMidia(BaseZap):
    def setUp(self):
        super().setUp()
        self.psql.msgs[ANA] = [
            ["v1", k(ANA, mid="V1"), "Ana Paula", "videoMessage",
             json.dumps({"videoMessage": {"seconds": 32, "caption": "olha"}}), str(AGORA), "READ"],
            ["d1", k(ANA, mid="D1"), "Ana Paula", "documentMessage",
             json.dumps({"documentMessage": {"fileName": 'orça "mento".pdf'}}), str(AGORA - 10), "READ"],
            ["i1", k(ANA, mid="I1"), "Ana Paula", "imageMessage",
             json.dumps({"imageMessage": {}}), str(AGORA - 20), "READ"],
            ["s1", k(ANA, mid="S1"), "Ana Paula", "stickerMessage",
             json.dumps({"stickerMessage": {}}), str(AGORA - 30), "READ"],
        ]
        self.psql.chaves["v1"] = [k(ANA, mid="V1"), "videoMessage"]
        self.psql.chaves["d1"] = [k(ANA, mid="D1"), "documentMessage"]
        self.psql.chaves["i1"] = [k(ANA, mid="I1"), "imageMessage"]
        self.fe.midia_por_id = {"V1": MP4, "D1": PDF}

    def midia(self, tipo, dados, para=ANA, cab=None, **query):
        q = dict(para=para, tipo=tipo, **query)
        req = self.req("POST", "/conversas/enviar-midia", {"_bruto": dados, "_tipo": "application/octet-stream"}, query=q, cab=cab)
        return conversas.ROTAS["/conversas/enviar-midia"](req)


class TestVer(BaseMidia):
    def test_foto_abre_grande_video_toca_e_tudo_tem_baixar(self):
        t = texto(self.get("/conversas", c=ANA))
        self.assertIn('data-ampliar="/conversas/midia?m=i1"', t)          # a foto é um botão que abre o diálogo
        self.assertIn('<dialog class="zap-ampliada" id="zap-ampliada"', t)
        self.assertIn('<video class="zap-video" preload="none" playsinline poster="/conversas/midia?m=v1&amp;capa=1" '
                      'src="/conversas/midia?m=v1">', t)
        self.assertIn('data-tocar', t)
        self.assertIn('data-expandir', t)
        self.assertIn('href="/conversas/midia?m=v1&amp;baixar=1" download', t)
        self.assertIn('href="/conversas/midia?m=d1&amp;n=', t)
        self.assertNotIn("abra no celular", t)                              # vídeo e documento não mandam mais ao celular
        self.assertNotIn('data-ampliar="/conversas/midia?m=s1"', t)       # figurinha não amplia

    def test_video_serve_mp4_com_faixa_e_cache(self):
        r = self.get("/conversas/midia", m="v1")
        self.assertEqual((r.status, r.tipo, r.corpo), (200, "video/mp4", MP4))
        self.assertTrue(list((self.dados / "caixa" / ".midia").glob("v1.mp4")))
        n = len(self.fe.midia_corpos)
        req = self.req("GET", "/conversas/midia", query={"m": "v1"}, cab={"range": "bytes=0-3"})
        r = conversas.ROTAS["/conversas/midia"](req)
        self.assertEqual((r.status, len(r.corpo)), (206, 4))
        self.assertEqual(len(self.fe.midia_corpos), n)                     # veio do cache
        self.fe.midia_por_id["V1"] = b"<html>nao e video</html>"
        (self.dados / "caixa" / ".midia" / "v1.mp4").unlink()
        self.assertEqual(self.get("/conversas/midia", m="v1").status, 404)   # sem caixa ftyp não é servido

    def test_documento_so_sai_como_download_com_nome_seguro(self):
        r = self.get("/conversas/midia", m="d1", n='../../orça "mento"\r\nX: 1.pdf')
        cab = dict(r.cabecalhos)
        self.assertEqual((r.status, r.tipo, r.corpo), (200, "application/octet-stream", PDF))
        self.assertEqual(cab["X-Content-Type-Options"], "nosniff")
        disp = cab["Content-Disposition"]
        self.assertTrue(disp.startswith("attachment; filename*=UTF-8''"))
        for ruim in ('"', "\r", "\n", "/", "%2F", "%0D", "%0A", "%22"):
            self.assertNotIn(ruim, disp.split("''", 1)[1])
        self.assertIn("attachment", dict(self.get("/conversas/midia", m="d1").cabecalhos)["Content-Disposition"])  # do cache também

    def test_baixar_foto_vira_anexo_e_sem_baixar_abre_na_pagina(self):
        r = self.get("/conversas/midia", m="i1")
        self.assertEqual(r.tipo, "image/jpeg")
        self.assertNotIn("Content-Disposition", dict(r.cabecalhos))
        r = self.get("/conversas/midia", m="i1", baixar="1")
        self.assertIn(".jpg", dict(r.cabecalhos)["Content-Disposition"])


class TestEnviar(BaseMidia):
    def test_travado_nao_envia_nada(self):
        r = self.midia("foto", imagem_jpeg_minima())
        self.assertEqual(r.status, 423)
        self.assertTrue(json.loads(texto(r))["liberar"])
        self.assertEqual((self.fe.midias_enviadas, self.fe.audios_enviados), ([], []))

    def test_foto_video_arquivo_e_voz(self):
        self.liberar()
        jpg = imagem_jpeg_minima()
        self.assertEqual(self.midia("foto", jpg, legenda="Ficou assim").status, 200)
        self.assertEqual(self.midia("video", MP4, para=G1).status, 200)
        self.assertEqual(self.midia("arquivo", PDF, nome="../contrato final.pdf").status, 200)
        self.assertEqual(self.midia("voz", OGG).status, 200)
        foto, video, doc = self.fe.midias_enviadas
        self.assertEqual((foto["number"], foto["mediatype"], foto["mimetype"], foto["caption"]),
                         ("5592900001111", "image", "image/jpeg", "Ficou assim"))
        self.assertEqual(base64.b64decode(foto["media"]), jpg)
        self.assertEqual((video["number"], video["mediatype"], video["mimetype"]), (G1, "video", "video/mp4"))
        self.assertNotIn("caption", video)
        self.assertEqual((doc["mediatype"], doc["fileName"], doc["mimetype"]), ("document", "_contrato final.pdf", "application/pdf"))
        (voz,) = self.fe.audios_enviados
        self.assertEqual((voz["number"], voz["encoding"], base64.b64decode(voz["audio"])), ("5592900001111", True, OGG))
        envios = self.banco._ler("SELECT para, hash_texto, por FROM envios ORDER BY id")
        self.assertEqual([e["por"] for e in envios], ["painel:conversa:foto", "painel:conversa:video",
                                                      "painel:conversa:arquivo", "painel:conversa:voz"])
        self.assertEqual(envios[0]["hash_texto"], hashlib.sha256(jpg).hexdigest())   # só o hash, nunca o arquivo
        self.assertNotIn("Ficou assim", json.dumps(envios))

    def test_recusas(self):
        self.liberar()
        self.assertEqual(self.midia("foto", b"GIF89a....").status, 415)      # foto só JPEG/PNG/WebP
        self.assertEqual(self.midia("foto", PNG).status, 200)
        self.assertEqual(self.midia("video", b"RIFFxxxxAVI ").status, 415)
        self.assertEqual(self.midia("voz", PDF).status, 415)
        self.assertEqual(self.midia("foto", b"").status, 400)
        self.assertEqual(self.midia("figurinha", PNG).status, 400)
        self.assertEqual(self.midia("foto", PNG, para=LID).status, 400)
        self.assertEqual(self.midia("foto", PNG, para="5592900001111").status, 400)
        self.assertEqual(self.midia("foto", PNG, legenda="x" * 1025).status, 400)
        self.assertEqual(self.midia("arquivo", b"x" * (conversas.LIMITE_MIDIA + 1), nome="a.bin").status, 413)
        self.assertEqual(self.midia("foto", PNG, cab={"sec-fetch-site": "cross-site"}).status, 403)
        req = self.req("POST", "/conversas/enviar-midia", {"para": ANA}, query={"para": ANA, "tipo": "foto"})
        self.assertEqual(conversas.ROTAS["/conversas/enviar-midia"](req).status, 400)   # JSON no lugar do arquivo
        self.ctx.tokens.envio = ""
        self.assertEqual(self.midia("foto", PNG).status, 503)
        self.assertEqual(len(self.fe.midias_enviadas), 1)

    def test_incerto_avisa_e_recusado_nao_registra(self):
        self.liberar()
        self.fe.falhar_midia = 500
        r = self.midia("voz", OGG)
        self.assertEqual(r.status, 202)
        self.assertTrue(json.loads(texto(r))["incerto"])
        self.fe.falhar_midia = 400
        self.assertEqual(self.midia("foto", PNG).status, 502)
        self.assertEqual([e["por"] for e in self.banco._ler("SELECT por FROM envios ORDER BY id")],
                         ["painel:conversa:voz:incerto"])

    def test_rodape_tem_anexar_e_gravar_so_quando_liberado(self):
        t = texto(self.get("/conversas", c=ANA))
        self.assertRegex(t, r'id="zap-anexar"[^>]* hidden>')
        self.assertRegex(t, r'id="zap-gravar"[^>]* hidden>')
        self.liberar()
        t = texto(self.get("/conversas", c=ANA))
        self.assertNotRegex(t, r'id="zap-anexar"[^>]* hidden>')
        self.assertIn('id="zap-arquivo"', t)


class TestServidorCorpoCru(unittest.TestCase):
    """O corpo cru de até 16 MB só é lido na rota de mídia e só com sessão; fora disso vale o limite de 64 KB."""

    def setUp(self):
        import threading
        from types import SimpleNamespace
        self.visto = []

        def eco(req):
            self.visto.append(req.corpo)
            return srv_mod.Resposta(200, "ok", "text/plain; charset=utf-8")

        self._rotas = dict(conversas.ROTAS)
        conversas.ROTAS["/conversas/enviar-midia"] = eco
        self.addCleanup(lambda: conversas.ROTAS.update(self._rotas))
        ctx = SimpleNamespace(config={}, tokens=SimpleNamespace(envio="e", painel="p"), executor=None)
        self.srv = srv_mod.criar_servidor(ctx, "p", porta=0)
        self.porta = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def _post(self, caminho, corpo, tipo, cookie=True):
        con = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=10)
        cab = {"Content-Type": tipo}
        if cookie:
            cab["Cookie"] = f"{srv_mod.COOKIE}={srv_mod.valor_cookie('p')}"
        try:
            con.request("POST", caminho, body=corpo, headers=cab)
            r = con.getresponse(); r.read()
            return r.status
        except (BrokenPipeError, ConnectionResetError):
            return 0
        finally:
            con.close()

    def test_so_a_rota_de_midia_com_sessao_le_corpo_grande(self):
        grande = b"\xff\xd8\xff" + b"x" * (srv_mod.LIMITE_CORPO + 100)
        self.assertEqual(self._post("/conversas/enviar-midia?para=x&tipo=foto", grande, "application/octet-stream"), 200)
        self.assertEqual(self.visto[-1]["_bruto"], grande)
        n = len(self.visto)
        self.assertIn(self._post("/conversas/enviar-midia", grande, "application/octet-stream", cookie=False), (400, 0))
        self.assertIn(self._post("/conversas/enviar", grande, "application/octet-stream"), (400, 0))
        self.assertIn(self._post("/conversas/enviar-midia", grande, "application/json"), (400, 0))
        self.assertIn(self._post("/conversas/enviar-midia", b"x" * (srv_mod.LIMITE_BRUTO + 1), "application/octet-stream"), (400, 0))
        self.assertEqual(len(self.visto), n)


if __name__ == "__main__":
    unittest.main()
