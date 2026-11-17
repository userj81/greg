"""Pedido 50, rodada 2: ver citação, reações, "editada" e link clicável; responder citando e reagir pelo próprio Greg,
com a mesma trava do texto. Evolution pelo FakeEvolution; banco da Evolution por psql falso."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import whatsapp as wa  # noqa: E402
from app.paginas import conversas  # noqa: E402
from testes.test_conversas_zap import AGORA, ANA, G1, LID, MARCOS, BaseZap, k  # noqa: E402
from testes.test_paginas import texto  # noqa: E402


def citada(mid, de, msg):
    return json.dumps({"id": mid, "de": de, "msg": msg})


class BaseRodada2(BaseZap):
    def setUp(self):
        super().setUp()
        self.psql.msgs[ANA] = [
            ["a3", k(ANA, True, "B3"), "", "conversation", json.dumps({"conversation": "Mando sim"}), str(AGORA), "READ",
             citada("B2", ANA, {"conversation": "Consegue mandar a arte até sexta?"})],
            ["a2", k(ANA, mid="B2"), "Ana Paula", "conversation",
             json.dumps({"conversation": "Consegue mandar a arte até sexta? Veja https://exemplo.com.br/arte?a=1&b=2."}),
             str(AGORA - 60), "EDITED", ""],
            ["a1", k(ANA, mid="B1"), "Ana Paula", "conversation", json.dumps({"conversation": "oi <b>www.site.com/x</b>"}),
             str(AGORA - 120), "READ", citada("ANTIGA", "5592999990000@s.whatsapp.net", {"imageMessage": {"caption": "foto velha"}})],
        ]
        self.psql.chaves["a2"] = [k(ANA, mid="B2"), "conversation", ANA]
        self.psql.chaves["g1m1"] = [k(G1, mid="A1", participant=ANA), "imageMessage", G1]
        # da mais nova para a mais antiga, como a consulta devolve
        self.psql.reacoes[ANA] = [
            [k(ANA, True, "R3"), "B2", "👍", ""],
            [k(ANA, mid="R2"), "B3", "", "Ana Paula"],          # a Ana tirou a reação dela
            [k(ANA, mid="R1"), "B3", "❤️", "Ana Paula"],
            [k(ANA, mid="R0"), "B2", "👍", "Ana Paula"],
        ]


class TestVer(BaseRodada2):
    def test_citacao_aponta_para_a_original_e_mostra_quem_escreveu(self):
        t = texto(self.get("/conversas", c=ANA))
        self.assertIn('<a class="zap-citada" href="#m-a2"><b>Ana Paula</b><span>Consegue mandar a arte até sexta?</span></a>', t)
        # original fora do fio: bloco sem link; no particular, quem não é o contato sou eu
        self.assertIn('<div class="zap-citada"><b>Você</b><span>Foto: foto velha</span></div>', t)

    def test_reacoes_agrupam_e_a_ultima_de_cada_pessoa_vale(self):
        r = wa.fio(self.psql, ANA, desde=1)[1]
        self.assertEqual(sorted(x["quem"] for x in r["B2"]), sorted(["eu", ANA]))
        self.assertNotIn("B3", r)                                   # reação tirada não aparece
        t = texto(self.get("/conversas", c=ANA))
        self.assertIn('class="zap-reacao minha" data-tirar-reacao title="Ana Paula, Você. Clique para tirar a sua."', t)
        self.assertIn("👍<span>2</span>", t)
        self.assertNotIn("❤️", t.split("<script")[0])

    def test_editada_e_link_clicavel_sem_abrir_brecha(self):
        t = texto(self.get("/conversas", c=ANA))
        self.assertIn('<span class="zap-editada">editada</span>', t)
        self.assertIn('<a href="https://exemplo.com.br/arte?a=1&amp;b=2" target="_blank" rel="noopener noreferrer">'
                      'https://exemplo.com.br/arte?a=1&amp;b=2</a>.', t)          # o ponto final fica fora do link
        self.assertIn('oi &lt;b&gt;<a href="https://www.site.com/x" target="_blank" rel="noopener noreferrer">www.site.com/x</a>&lt;/b&gt;', t)
        self.assertEqual(conversas.com_links('javascript:alert(1) "x" http://a'), 'javascript:alert(1) &quot;x&quot; http://a')

    def test_acoes_so_onde_da_para_enviar(self):
        self.assertIn("data-responder", texto(self.get("/conversas", c=ANA)))
        self.assertIn('data-id="a2" data-quem="Ana Paula"', texto(self.get("/conversas", c=ANA)))
        self.psql.msgs[LID] = [["l1", k(LID, mid="D1"), "Carla", "conversation", json.dumps({"conversation": "oi"}), str(AGORA), "READ"]]
        self.assertNotIn("data-responder", texto(self.get("/conversas", c=LID)).split("<script")[0])
        self.ctx.tokens.envio = ""
        self.assertNotIn("data-responder", texto(self.get("/conversas", c=ANA)).split("<script")[0])

    def test_consultas_validam_a_conversa(self):
        with self.assertRaises(ValueError):
            wa.sql_mensagens("x' OR 1=1 --", 1)
        q = wa.sql_mensagens(ANA, 1)
        self.assertIn("m.\"messageType\" = 'reactionMessage'", q)
        self.assertIn('"contextInfo"->>\'stanzaId\'', q)

    def test_reacoes_nao_custam_outra_consulta_e_nao_viram_mensagem(self):
        self.get("/conversas", c=ANA)
        self.assertEqual(len(self.psql.consultas), 3)               # listar, bloqueados, mensagens com as reações
        self.assertEqual([m["id"] for m in wa.mensagens(self.psql, ANA, desde=1)], ["a1", "a2", "a3"])


class TestResponderEReagir(BaseRodada2):
    def test_responder_citando_manda_a_chave_da_original(self):
        self.liberar()
        r = self.post("/conversas/enviar", {"para": ANA, "texto": "Mando sim", "citar": "a2"})
        self.assertEqual(r.status, 200)
        self.assertEqual(self.fe.enviadas[-1]["quoted"], {"key": {"remoteJid": ANA, "fromMe": False, "id": "B2"}})

    def test_citar_mensagem_de_outra_conversa_e_recusado(self):
        self.liberar()
        r = self.post("/conversas/enviar", {"para": ANA, "texto": "oi", "citar": "g1m1"})
        self.assertEqual(r.status, 400)
        self.assertEqual(self.fe.enviadas, [])
        self.assertEqual(self.post("/conversas/enviar", {"para": ANA, "texto": "oi", "citar": "x' --"}).status, 400)
        self.assertEqual(self.post("/conversas/enviar", {"para": ANA, "texto": "oi", "citar": "naoexiste"}).status, 400)

    def test_reagir_em_grupo_leva_o_participante(self):
        self.liberar()
        r = self.post("/conversas/reagir", {"para": G1, "id": "g1m1", "emoji": "🙏"})
        self.assertEqual(r.status, 200)
        self.assertEqual(self.fe.reacoes_enviadas, [{"key": {"remoteJid": G1, "fromMe": False, "id": "A1", "participant": ANA},
                                                     "reaction": "🙏"}])
        envio = self.banco._ler("SELECT para, por FROM envios ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual((envio["para"], envio["por"]), (G1, "painel:conversa:reacao"))

    def test_tirar_reacao_e_emoji_vazio(self):
        self.liberar()
        self.assertEqual(self.post("/conversas/reagir", {"para": ANA, "id": "a2", "emoji": ""}).status, 200)
        self.assertEqual(self.fe.reacoes_enviadas[-1]["reaction"], "")

    def test_reagir_respeita_a_trava_e_recusa_o_que_nao_e_emoji(self):
        self.assertEqual(self.post("/conversas/reagir", {"para": ANA, "id": "a2", "emoji": "👍"}).status, 423)
        self.liberar()
        for ruim in ("ok", "👍 a", 5, None, "👍" * 20):
            self.assertEqual(self.post("/conversas/reagir", {"para": ANA, "id": "a2", "emoji": ruim}).status, 400, ruim)
        self.assertEqual(self.post("/conversas/reagir", {"para": ANA, "id": "", "emoji": "👍"}).status, 400)
        self.assertEqual(self.post("/conversas/reagir", {"para": LID, "id": "a2", "emoji": "👍"}).status, 400)
        self.assertEqual(self.fe.reacoes_enviadas, [])
        self.fe.recusar_envio = True
        self.assertEqual(self.post("/conversas/reagir", {"para": ANA, "id": "a2", "emoji": "👍"}).status, 502)


if __name__ == "__main__":
    unittest.main()
