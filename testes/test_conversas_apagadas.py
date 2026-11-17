"""D-63 (pedido 50): mensagem que o remetente apagou vira "Mensagem apagada" e o conteúdo some do painel; "Apagar para
todos" nas mensagens próprias, com a mesma trava. Evolution pelo FakeEvolution; banco por psql falso."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import whatsapp as wa  # noqa: E402
from app.paginas import conversas  # noqa: E402
from testes.fakes import imagem_jpeg_minima  # noqa: E402
from testes.test_conversas_zap import AGORA, ANA, G1, LID, BaseZap, k, linha_lista  # noqa: E402
from testes.test_paginas import texto  # noqa: E402

RAIZ = Path(__file__).resolve().parents[1]


class BaseApagadas(BaseZap):
    def setUp(self):
        super().setUp()
        cit = json.dumps({"id": "S1", "de": ANA, "msg": {"conversation": "segredo que apaguei"}})
        self.psql.msgs[ANA] = [
            ["x3", k(ANA, True, "X3"), "", "conversation", json.dumps({"conversation": "ok, entendi"}), str(AGORA), "READ", cit, "false"],
            ["x2", k(ANA, mid="X2"), "Ana Paula", "imageMessage", json.dumps({"imageMessage": {"caption": "legenda secreta"}}),
             str(AGORA - 10), "EDITED", "", "true"],
            ["x1", k(ANA, mid="S1"), "Ana Paula", "conversation", json.dumps({"conversation": "segredo que apaguei"}),
             str(AGORA - 20), "EDITED", "", "true"],
            ["x0", k(ANA, mid="X0"), "Ana Paula", "conversation", json.dumps({"conversation": "texto corrigido"}),
             str(AGORA - 30), "EDITED", "", "false"],
        ]
        self.psql.reacoes[ANA] = [[k(ANA, True, "R1"), "S1", "👍", ""]]
        ch = self.psql.chaves
        ch["x1"] = [k(ANA, mid="S1"), "conversation", ANA, str(AGORA - 20), json.dumps({"conversation": "segredo que apaguei"}), "true"]
        ch["x2"] = [k(ANA, mid="X2"), "imageMessage", ANA, str(AGORA - 10), "{}", "true"]
        ch["x3"] = [k(ANA, True, "X3"), "conversation", ANA, str(AGORA), json.dumps({"conversation": "ok, entendi"}), "false"]
        ch["x0"] = [k(ANA, mid="X0"), "conversation", ANA, str(AGORA - 30), "{}", "false"]
        self.banco.gravar_classificacao(msg_id="x1", conversa=ANA, contato="Ana Paula", ts=AGORA - 20, classe="demanda",
                                        resumo="Resumo do segredo", confianca="alta")


class TestVer(BaseApagadas):
    def test_conteudo_some_da_conversa(self):
        t = texto(self.get("/conversas", c=ANA)).split("<script>(function")[0]
        self.assertEqual(t.count('<div class="zap-apagada">Mensagem apagada</div>'), 2)
        for vazou in ("segredo que apaguei", "legenda secreta", "/conversas/midia?m=x2", "Resumo do segredo", 'data-id="x1"'):
            self.assertNotIn(vazou, t)
        self.assertIn('<a class="zap-citada" href="#m-x1"><b>Ana Paula</b><span>Mensagem apagada</span></a>', t)
        self.assertNotIn("zap-reacoes", t.split("</style>")[-1])                 # a reação na apagada também some
        self.assertIn("texto corrigido", t)                                       # editada de verdade continua
        self.assertEqual(t.count('<span class="zap-editada">editada</span>'), 1)

    def test_lista_nao_mostra_a_previa(self):
        linha = linha_lista(ANA, AGORA, "x1", k(ANA, mid="S1"), "Ana Paula", "conversation",
                            {"conversation": "segredo que apaguei"}, 0, "Ana Paula", "") + ["true"]
        self.psql.lista = [linha]
        t = texto(self.get("/conversas"))
        self.assertIn("Mensagem apagada", t)
        self.assertNotIn("segredo que apaguei", t)

    def test_midia_citar_reagir_e_encaminhar_recusam_a_apagada(self):
        self.fe.midia_por_id = {"X2": imagem_jpeg_minima()} if hasattr(self.fe, "midia_por_id") else {}
        self.assertEqual(self.get("/conversas/midia", m="x2").status, 404)
        self.liberar()
        self.assertEqual(self.post("/conversas/enviar", {"para": ANA, "texto": "oi", "citar": "x1"}).status, 400)
        self.assertEqual(self.post("/conversas/reagir", {"para": ANA, "id": "x1", "emoji": "👍"}).status, 400)
        self.assertEqual(self.post("/conversas/encaminhar", {"de": ANA, "id": "x1", "para": G1}).status, 400)
        self.assertEqual(self.fe.enviadas + self.fe.reacoes_enviadas, [])

    def test_cache_da_midia_apagada_e_removido_ao_abrir_a_conversa(self):
        cache = conversas.c.dados_dir(self.ctx) / "caixa" / ".midia"
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "x2.jpg").write_bytes(imagem_jpeg_minima())
        self.get("/conversas", c=ANA)
        self.assertFalse((cache / "x2.jpg").exists())

    def test_gatilho_so_anota_edicao_sem_mudanca_de_conteudo(self):
        sql = (RAIZ / "infra/sql/004_apagadas.sql").read_text(encoding="utf-8")
        self.assertIn("NEW.message IS NOT DISTINCT FROM OLD.message", sql)
        self.assertIn("AFTER UPDATE ON public.\"Message\"", sql)
        self.assertIn("EXCEPTION WHEN others", sql)                       # nunca derruba a gravação da Evolution
        self.assertIn("GRANT SELECT ON vigia.apagadas TO vigia_ro", sql)
        self.assertNotIn("ALTER TABLE public", sql)
        for consulta in (wa.sql_lista(1), wa.sql_mensagens(ANA, 1), wa.sql_chave("x1", 1)):
            self.assertIn("LEFT JOIN vigia.apagadas ap ON ap.message_id = m.id", consulta)
            self.assertIn("= 'DELETED'", consulta)                        # a que o próprio dono apagou para todos


class TestApagarParaTodos(BaseApagadas):
    def test_so_mensagem_propria_tem_o_botao(self):
        t = texto(self.get("/conversas", c=ANA)).split("<script>(function")[0]
        bolha = lambda mid: t.split(f'id="m-{mid}"', 1)[1].split("</button></span></div>", 1)[0]
        self.assertIn("data-apagar", bolha("x3"))
        self.assertNotIn("data-apagar", bolha("x0"))

    def test_apaga_com_a_trava_e_registra_so_o_hash(self):
        self.assertEqual(self.post("/conversas/apagar", {"para": ANA, "id": "x3"}).status, 423)
        self.liberar()
        r = self.post("/conversas/apagar", {"para": ANA, "id": "x3"})
        self.assertEqual(r.status, 200, texto(r))
        self.assertEqual(self.fe.apagadas_enviadas, [{"remoteJid": ANA, "fromMe": True, "id": "X3"}])
        envio = self.banco._ler("SELECT para, por FROM envios ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual((envio["para"], envio["por"]), (ANA, "painel:conversa:apagar"))

    def test_recusas(self):
        self.liberar()
        self.assertEqual(self.post("/conversas/apagar", {"para": ANA, "id": "x0"}).status, 400)      # não é minha
        self.assertEqual(self.post("/conversas/apagar", {"para": G1, "id": "x3"}).status, 400)       # outra conversa
        self.assertEqual(self.post("/conversas/apagar", {"para": LID, "id": "x3"}).status, 400)
        self.assertEqual(self.post("/conversas/apagar", {"para": ANA}).status, 400)
        self.assertEqual(self.fe.apagadas_enviadas, [])
        self.fe.recusar_envio = True
        self.assertEqual(self.post("/conversas/apagar", {"para": ANA, "id": "x3"}).status, 502)


if __name__ == "__main__":
    unittest.main()
