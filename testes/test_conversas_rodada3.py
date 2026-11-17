"""Pedido 50, rodada 3: localização, contato e enquete na conversa; histórico além de 200 mensagens; editar mensagem
própria (até 15 min) e encaminhar (reenvio como mensagem nova). Evolution pelo FakeEvolution; banco por psql falso."""
import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import whatsapp as wa  # noqa: E402
from app.paginas import conversas  # noqa: E402
from testes.fakes import imagem_jpeg_minima  # noqa: E402
from testes.test_conversas_zap import AGORA, ANA, G1, LID, MARCOS, BaseZap, k  # noqa: E402
from testes.test_paginas import texto  # noqa: E402

VCARD = "BEGIN:VCARD\nVERSION:3.0\nFN:Bruno Lima\nitem1.TEL;waid=14155550102:+1 415 555 0102\nTEL;type=HOME:<script>\nEND:VCARD"


class BaseR3(BaseZap):
    def setUp(self):
        super().setUp()
        self.agora = int(time.time())
        self.psql.msgs[ANA] = [
            ["p1", k(ANA, mid="P1"), "Ana Paula", "pollCreationMessageV3",
             json.dumps({"pollCreationMessageV3": {"name": "Que dia?", "options": [{"optionName": "Segunda"}, {"optionName": "<b>Terça</b>"}]}}),
             str(AGORA), "READ"],
            ["c1", k(ANA, mid="C1"), "Ana Paula", "contactMessage",
             json.dumps({"contactMessage": {"displayName": "Bruno Lima", "vcard": VCARD}}), str(AGORA - 10), "READ"],
            ["l1", k(ANA, mid="L1"), "Ana Paula", "locationMessage",
             json.dumps({"locationMessage": {"degreesLatitude": -3.130278, "degreesLongitude": -60.023333, "name": "Teatro", "address": "Centro"}}),
             str(AGORA - 20), "READ"],
            ["l2", k(ANA, mid="L2"), "Ana Paula", "locationMessage",
             json.dumps({"locationMessage": {"degreesLatitude": "x\"><script>", "degreesLongitude": 999}}), str(AGORA - 30), "READ"],
            ["e1", k(ANA, True, "E1"), "", "conversation", json.dumps({"conversation": "chego as 19h"}), str(self.agora - 60), "READ"],
            ["e0", k(ANA, True, "E0"), "", "conversation", json.dumps({"conversation": "antiga"}), str(self.agora - 3600), "READ"],
            ["f1", k(ANA, mid="F1"), "Ana Paula", "imageMessage", json.dumps({"imageMessage": {"caption": "a arte"}}), str(AGORA - 40), "READ"],
        ]
        ch = self.psql.chaves
        ch["e1"] = [k(ANA, True, "E1"), "conversation", ANA, str(self.agora - 60), json.dumps({"conversation": "chego as 19h"})]
        ch["e0"] = [k(ANA, True, "E0"), "conversation", ANA, str(self.agora - 3600), json.dumps({"conversation": "antiga"})]
        ch["f1"] = [k(ANA, mid="F1"), "imageMessage", ANA, str(AGORA - 40), json.dumps({"imageMessage": {"caption": "a arte"}})]
        ch["p1"] = [k(ANA, mid="P1"), "pollCreationMessageV3", ANA, str(AGORA), json.dumps({"pollCreationMessageV3": {"name": "Que dia?"}})]
        ch["t1"] = [k(ANA, mid="T1"), "conversation", ANA, str(AGORA), json.dumps({"conversation": "texto da Ana"})]


class TestVer(BaseR3):
    def test_localizacao_contato_e_enquete_aparecem(self):
        t = texto(self.get("/conversas", c=ANA)).split("<script>(function")[0]
        self.assertIn('href="https://www.google.com/maps?q=-3.130278,-60.023333" target="_blank" rel="noopener noreferrer">Abrir no mapa</a>', t)
        self.assertIn("<small>Teatro · Centro</small>", t)
        self.assertEqual(t.count("Abrir no mapa"), 1)                     # coordenada inválida: sem link, sem injeção
        self.assertNotIn('x"><script>', t)
        self.assertIn("Bruno Lima<small>+1 415 555 0102</small>", t)      # só telefone com cara de telefone
        self.assertIn("<li>Segunda</li><li>&lt;b&gt;Terça&lt;/b&gt;</li>", t)
        self.assertIn("os votos aparecem só no celular", t)
        self.assertNotIn("abra no celular", t)

    def test_previa_dos_tipos_novos(self):
        p = lambda m: wa.previa(wa.conteudo(m))
        self.assertEqual(p({"liveLocationMessage": {"degreesLatitude": 1, "degreesLongitude": 2}}), "Localização ao vivo")
        self.assertEqual(p({"contactsArrayMessage": {"contacts": [{"displayName": "A"}, {"displayName": "B"}]}}), "Contato · A, B")
        self.assertEqual(p({"pollCreationMessage": {"name": "Dia?"}}), "Enquete · Dia?")

    def test_historico_sobe_um_degrau_por_clique_ate_o_teto(self):
        self.assertEqual([conversas.limite_fio(x) for x in ("", "abc", "1", "200", "201", "400", "999999", "-5")],
                         [200, 200, 200, 200, 400, 400, 2000, 200])
        self.assertNotIn("zap-anteriores\"", texto(self.get("/conversas", c=ANA)).split("<script>(function")[0])
        base = self.psql.msgs[ANA][0]
        self.psql.msgs[ANA] = [[f"h{i}", k(ANA, mid=f"H{i}"), "Ana Paula", "conversation", json.dumps({"conversation": "oi"}), str(AGORA - i), "READ"]
                               for i in range(200)]
        t = texto(self.get("/conversas", c=ANA))
        self.assertIn('<button type="button" class="zap-anteriores" data-n="400">Ver mensagens anteriores</button>', t)
        self.get("/conversas", c=ANA, n="400")
        self.assertIn("LIMIT 400)", self.psql.consultas[-1])
        self.psql.msgs[ANA] = self.psql.msgs[ANA] * 10
        self.assertIn("mostra as 2000 mensagens mais recentes", texto(self.get("/conversas", c=ANA, n="2000")))
        del base


class TestEditar(BaseR3):
    def test_so_mensagem_propria_de_texto_e_recente_ganha_editar(self):
        t = texto(self.get("/conversas", c=ANA)).split("<script>(function")[0]
        bolha = lambda mid: t.split(f'id="m-{mid}"', 1)[1].split("</button></span></div>", 1)[0]
        self.assertIn("data-editar", bolha("e1"))
        self.assertNotIn("data-editar", bolha("e0"))        # passou dos 15 min
        self.assertNotIn("data-editar", bolha("f1"))        # não é minha
        self.assertIn("data-encaminhar", bolha("f1"))
        self.assertNotIn("data-encaminhar", bolha("p1"))    # enquete não se encaminha pelo Greg

    def test_editar_manda_a_chave_e_o_texto_novo(self):
        self.assertEqual(self.post("/conversas/editar", {"para": ANA, "id": "e1", "texto": "chego às 19h"}).status, 423)
        self.liberar()
        r = self.post("/conversas/editar", {"para": ANA, "id": "e1", "texto": "chego às 19h"})
        self.assertEqual(r.status, 200, texto(r))
        self.assertEqual(self.fe.edicoes_enviadas, [{"number": "5592900001111", "text": "chego às 19h",
                                                     "key": {"remoteJid": ANA, "fromMe": True, "id": "E1"}}])
        envio = self.banco._ler("SELECT por, hash_texto FROM envios ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual(envio["por"], "painel:conversa:edicao")
        self.assertNotIn("19h", envio["hash_texto"])

    def test_editar_recusa_fora_da_regra(self):
        self.liberar()
        casos = [({"para": ANA, "id": "e0", "texto": "x"}, "15 minutos"), ({"para": ANA, "id": "f1", "texto": "x"}, "enviada por você"),
                 ({"para": ANA, "id": "t1", "texto": "x"}, "enviada por você"), ({"para": ANA, "id": "e1", "texto": "  "}, "vazio"),
                 ({"para": G1, "id": "e1", "texto": "x"}, "não foi encontrada"), ({"para": LID, "id": "e1", "texto": "x"}, "Destino")]
        for corpo, trecho in casos:
            r = self.post("/conversas/editar", corpo)
            self.assertEqual(r.status, 400, corpo)
            self.assertIn(trecho, json.loads(texto(r))["mensagem"])
        self.assertEqual(self.fe.edicoes_enviadas, [])


class TestEncaminhar(BaseR3):
    def test_texto_vai_para_a_outra_conversa_como_mensagem_nova(self):
        self.assertEqual(self.post("/conversas/encaminhar", {"de": ANA, "id": "t1", "para": G1}).status, 423)
        self.liberar()
        r = self.post("/conversas/encaminhar", {"de": ANA, "id": "t1", "para": G1})
        self.assertEqual(r.status, 200, texto(r))
        self.assertEqual(self.fe.enviadas, [{"number": G1, "text": "texto da Ana"}])
        self.assertEqual(self.banco._ler("SELECT para, por FROM envios ORDER BY id DESC LIMIT 1")[0]["por"], "painel:conversa:encaminhar")

    def test_foto_e_baixada_e_reenviada_com_a_legenda(self):
        self.liberar()
        r = self.post("/conversas/encaminhar", {"de": ANA, "id": "f1", "para": MARCOS})
        self.assertEqual(r.status, 200, texto(r))
        m = self.fe.midias_enviadas[-1]
        self.assertEqual((m["number"], m["mediatype"], m["caption"]), ("5592900003333", "image", "a arte"))

    def test_recusas(self):
        self.liberar()
        self.assertEqual(self.post("/conversas/encaminhar", {"de": ANA, "id": "p1", "para": G1}).status, 400)     # enquete
        self.assertEqual(self.post("/conversas/encaminhar", {"de": G1, "id": "t1", "para": ANA}).status, 400)     # não é dessa conversa
        self.assertEqual(self.post("/conversas/encaminhar", {"de": ANA, "id": "t1", "para": LID}).status, 400)
        self.assertEqual(self.post("/conversas/encaminhar", {"de": ANA, "id": "t1"}).status, 400)
        self.assertEqual(self.fe.enviadas, [])
        self.fe.recusar_envio = True
        self.assertEqual(self.post("/conversas/encaminhar", {"de": ANA, "id": "t1", "para": G1}).status, 502)


if __name__ == "__main__":
    unittest.main()
