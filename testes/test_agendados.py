"""Envio agendado (pedido 72, D-70): agenda com a trava aberta, guarda em dados/agendados, manda na hora, vence depois
da tolerância, cancela, não manda para bloqueado e não deixa texto em disco depois de resolvido."""
import json
import stat
import time
import unittest
from types import SimpleNamespace

from app import agendados
from app.erros import EnvioIncerto, VigiaErro
from app.paginas import conversas, envios
from testes.test_conversas_zap import ANA, G1, LID, BaseZap
from testes.test_paginas import texto

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class BaseAg(BaseZap):
    def setUp(self):
        super().setUp()
        self.agora = [int(time.time())]
        self.agenda = agendados.Agenda(self.dados / "agendados", relogio=lambda: self.agora[0])
        self.ctx.agenda = self.agenda
        self.ag = agendados.Agendador(self.ctx, self.agenda, relogio=lambda: self.agora[0])
        self.liberar()

    def em(self, s):
        return self.agora[0] + s

    def lista_crua(self):
        return (self.dados / "agendados" / "lista.json").read_text(encoding="utf-8")


class TestAgendar(BaseAg):
    def test_agenda_texto_guarda_e_mostra_na_faixa_com_cancelar(self):
        r = self.post("/conversas/agendar", {"para": ANA, "texto": "Segue a proposta amanhã cedo", "quando": self.em(3600)})
        self.assertEqual(r.status, 200, texto(r))
        d = json.loads(texto(r))
        self.assertIn("Segue a proposta", d["html"])
        self.assertIn(f'data-ag-cancelar="{d["agendado"]}"', d["html"])
        self.assertEqual(self.fe.enviadas, [])                      # nada sai na hora de agendar
        pasta = self.dados / "agendados"
        self.assertEqual(stat.S_IMODE(pasta.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((pasta / "lista.json").stat().st_mode), 0o600)
        self.assertIn("zap-agendados", texto(self.get("/conversas", c=ANA)))
        self.assertIn("Segue a proposta", texto(self.get("/conversas", c=ANA)))
        self.assertNotIn("Segue a proposta", texto(self.get("/conversas", c=G1)))   # só na conversa dela

    def test_recusas(self):
        ok = {"para": ANA, "texto": "oi", "quando": self.em(3600)}
        for muda in ({"para": LID}, {"texto": "  "}, {"quando": self.em(10)}, {"quando": "amanhã"},
                     {"quando": self.em(91 * 86400)}, {"texto": "x" * 4001}):
            self.assertEqual(self.post("/conversas/agendar", {**ok, **muda}).status, 400, muda)
        self.assertEqual(self.post("/conversas/agendar", ok, cab={"sec-fetch-site": "cross-site"}).status, 403)
        self.post("/envio/travar")
        self.assertEqual(self.post("/conversas/agendar", ok).status, 423)       # agendar pede a trava aberta
        self.assertFalse((self.dados / "agendados" / "lista.json").exists())

    def test_teto_de_pendentes(self):
        for i in range(agendados.TETO_PENDENTES):
            self.agenda.criar(ANA, self.em(3600), texto=f"m{i}")
        self.assertEqual(self.post("/conversas/agendar", {"para": ANA, "texto": "mais um", "quando": self.em(3600)}).status, 429)

    def test_agenda_arquivo_e_confere_o_formato(self):
        req = self.req("POST", "/conversas/agendar-midia", {"_bruto": PNG, "_tipo": "image/png"},
                       query={"para": ANA, "tipo": "foto", "nome": "arte.png", "legenda": "A arte", "quando": str(self.em(600))})
        r = conversas.ROTAS["/conversas/agendar-midia"](req)
        self.assertEqual(r.status, 200, texto(r))
        id_ = json.loads(texto(r))["agendado"]
        b = self.dados / "agendados" / f"{id_}.bin"
        self.assertEqual(b.read_bytes(), PNG)
        self.assertEqual(stat.S_IMODE(b.stat().st_mode), 0o600)
        req = self.req("POST", "/conversas/agendar-midia", {"_bruto": b"nao e imagem", "_tipo": "image/png"},
                       query={"para": ANA, "tipo": "foto", "quando": str(self.em(600))})
        self.assertEqual(conversas.ROTAS["/conversas/agendar-midia"](req).status, 415)
        req = self.req("POST", "/conversas/agendar-midia", {"_bruto": PNG}, query={"para": ANA, "tipo": "voz", "quando": str(self.em(600))})
        self.assertEqual(conversas.ROTAS["/conversas/agendar-midia"](req).status, 400)   # voz não se agenda

    def test_cancelar_apaga_texto_e_arquivo_e_nao_pede_trava(self):
        t = self.agenda.criar(ANA, self.em(600), texto="segredo do texto")
        a = self.agenda.criar(ANA, self.em(600), texto="legenda", tipo="foto", arquivo=PNG, nome="foto.png", mime="image/png")
        self.post("/envio/travar")
        for it in (t, a):
            r = self.post("/conversas/agendados/cancelar", {"id": str(it["id"])})
            self.assertEqual(r.status, 200, texto(r))
        self.assertNotIn("segredo do texto", self.lista_crua())
        self.assertFalse((self.dados / "agendados" / f"{a['id']}.bin").exists())
        self.assertEqual(self.post("/conversas/agendados/cancelar", {"id": t["id"]}).status, 409)
        self.agora[0] += 700
        self.assertEqual(self.ag.passo(), agendados.PASSO_S)        # cancelado não sai
        self.assertEqual(self.fe.enviadas, [])


class TestNaHora(BaseAg):
    def test_antes_da_hora_nao_sai_na_hora_sai_e_o_texto_some_do_disco(self):
        self.agenda.criar(ANA, self.em(600), texto="texto que vai sair")
        self.assertEqual(self.ag.passo(), agendados.PASSO_S)
        self.assertEqual(self.fe.enviadas, [])
        self.post("/envio/travar")                                  # a trava fechada não segura o agendado
        self.agora[0] += 601
        self.ag.passo()
        self.assertEqual([(e["number"], e["text"]) for e in self.fe.enviadas], [("5592900001111", "texto que vai sair")])
        self.assertNotIn("texto que vai sair", self.lista_crua())
        self.assertEqual(self.agenda.listar()[0]["estado"], "enviado")
        env = self.banco._ler("SELECT para, por FROM envios")
        self.assertEqual([(e["para"], e["por"]) for e in env], [(ANA, "painel:agendado:texto")])
        self.assertEqual(self.ag.passo(), agendados.PASSO_S)        # não manda duas vezes
        self.assertEqual(len(self.fe.enviadas), 1)

    def test_arquivo_sai_com_legenda_e_o_bin_e_apagado(self):
        chamadas = []
        self.ctx.evo = SimpleNamespace(enviar_midia=lambda *a, **k: chamadas.append((a, k)) or "MID1")
        it = self.agenda.criar(G1, self.em(120), texto="Proposta", tipo="arquivo", arquivo=b"%PDF-1.4 x", nome="proposta.pdf", mime="application/pdf")
        self.agora[0] += 121
        self.ag.passo()
        (num, especie, dados), k = chamadas[0]
        self.assertEqual((num, especie, dados, k["nome"], k["mime"], k["legenda"]),
                         (G1, "document", b"%PDF-1.4 x", "proposta.pdf", "application/pdf", "Proposta"))
        self.assertFalse((self.dados / "agendados" / f"{it['id']}.bin").exists())
        self.assertEqual(self.agenda.listar()[0]["msg_id"], "MID1")

    def test_falha_tenta_de_novo_e_vence_depois_de_uma_hora(self):
        def fora(*a, **k):
            raise VigiaErro("fora")
        evo_boa, self.ctx.evo = self.ctx.evo, SimpleNamespace(enviar_texto=fora)
        self.agenda.criar(ANA, self.em(120), texto="insistir")
        self.agora[0] += 121
        self.assertEqual(self.ag.passo(), agendados.PASSO_S)
        self.assertEqual(self.agenda.listar()[0]["estado"], "agendado")        # continua na agenda, com o texto
        self.agora[0] += agendados.TOLERANCIA_S + 1
        self.ctx.evo = evo_boa
        self.ag.passo()
        it = self.agenda.listar()[0]
        self.assertEqual((it["estado"], it["motivo"]), ("vencido", agendados.MOTIVO_VENCIDO))
        self.assertEqual(self.fe.enviadas, [])                                  # atrasado não sai
        self.assertNotIn("insistir", self.lista_crua())
        self.assertIn("Não saiu", texto(self.get("/conversas", c=ANA)))        # a conversa avisa

    def test_mac_desligado_na_hora_vence_ao_voltar(self):
        self.agenda.criar(ANA, self.em(120), texto="era para ontem")
        self.agora[0] += 86400
        self.ag.passo()
        self.assertEqual(self.agenda.listar()[0]["estado"], "vencido")
        self.assertEqual(self.fe.enviadas, [])

    def test_incerto_nao_repete(self):
        def incerto(*a, **k):
            raise EnvioIncerto("talvez")
        self.ctx.evo = SimpleNamespace(enviar_texto=incerto)
        self.agenda.criar(ANA, self.em(120), texto="uma vez só")
        self.agora[0] += 121
        self.ag.passo()
        self.assertEqual(self.agenda.listar()[0]["estado"], "incerto")
        self.assertEqual(self.banco._ler("SELECT por FROM envios")[0]["por"], "painel:agendado:texto:incerto")
        self.assertEqual(self.ag.passo(), agendados.PASSO_S)

    def _gravar_falha(self, vezes):
        """Disco cheio (ou lista travada) nas próximas `vezes` gravações da lista; depois volta ao normal."""
        original, falta = self.agenda._gravar, [vezes]

        def gravar(d):
            if falta[0] > 0:
                falta[0] -= 1
                raise OSError("No space left on device")
            return original(d)
        self.agenda._gravar = gravar

    def test_saiu_e_a_lista_nao_gravou_nao_manda_de_novo(self):
        # M1 (robustez 16/11): a mensagem saiu, a gravação do estado falhou; o laço não pode mandar a cada passo
        self.agenda.criar(ANA, self.em(120), texto="uma vez só")
        self.agora[0] += 121
        self._gravar_falha(3)
        with self.assertRaises(OSError):
            self.ag.passo()
        self.assertEqual(len(self.fe.enviadas), 1)
        for _ in range(4):                                           # cada passo do laço (a cada 20 s)
            self.agora[0] += agendados.PASSO_S
            try:
                self.ag.passo()
            except OSError:
                pass
        self.assertEqual(len(self.fe.enviadas), 1)                  # saiu uma vez só
        it = self.agenda.listar()[0]
        self.assertEqual(it["estado"], "enviado")                   # o estado gravou quando o disco voltou
        self.assertTrue(it["msg_id"])
        self.assertNotIn("uma vez só", self.lista_crua())
        self.assertEqual(self.ag.passo(), agendados.PASSO_S)

    def test_saiu_e_a_lista_nao_gravou_nao_vence_como_nao_saiu(self):
        # a guarda vale mesmo depois da tolerância: o que saiu não vira "vencido" (que diria "não saiu")
        self.agenda.criar(ANA, self.em(120), texto="saiu sim")
        self.agora[0] += 121
        self._gravar_falha(1)
        with self.assertRaises(OSError):
            self.ag.passo()
        self.agora[0] += agendados.TOLERANCIA_S + 1
        self.ag.passo()
        self.assertEqual(len(self.fe.enviadas), 1)
        self.assertEqual(self.agenda.listar()[0]["estado"], "enviado")

    def test_incerto_e_a_lista_nao_gravou_nao_manda_de_novo(self):
        chamadas = []

        def incerto(*a, **k):
            chamadas.append(1)
            raise EnvioIncerto("talvez")
        self.ctx.evo = SimpleNamespace(enviar_texto=incerto)
        self.agenda.criar(ANA, self.em(120), texto="talvez saiu")
        self.agora[0] += 121
        self._gravar_falha(1)
        with self.assertRaises(OSError):
            self.ag.passo()
        self.agora[0] += agendados.PASSO_S
        self.ag.passo()
        self.assertEqual(len(chamadas), 1)
        self.assertEqual(self.agenda.listar()[0]["estado"], "incerto")

    def test_bloqueado_nao_recebe_e_banco_fora_segura(self):
        self.agenda.criar(ANA, self.em(120), texto="não vai")
        self.agora[0] += 121
        self.psql.falhar_bloqueados = True
        self.ag.passo()
        self.assertEqual(self.agenda.listar()[0]["estado"], "agendado")        # sem saber, não manda
        self.psql.falhar_bloqueados = False
        self.psql.bloqueados = [ANA]
        self.ag.passo()
        self.assertEqual(self.agenda.listar()[0]["estado"], "falhou")
        self.assertEqual(self.fe.enviadas, [])

    def test_conexao_fechada_espera(self):
        (self.dados / "conexao.json").write_text(json.dumps({"ultimo_estado": "close"}))
        self.agenda.criar(ANA, self.em(120), texto="espera a conexão")
        self.agora[0] += 121
        self.assertEqual(self.ag.passo(), agendados.PASSO_S)
        self.assertEqual(self.fe.enviadas, [])
        (self.dados / "conexao.json").write_text(json.dumps({"ultimo_estado": "open"}))
        self.ag.passo()
        self.assertEqual(len(self.fe.enviadas), 1)

    def test_sobrevive_ao_reinicio(self):
        self.agenda.criar(ANA, self.em(120), texto="depois do reinício")
        outra = agendados.Agenda(self.dados / "agendados", relogio=lambda: self.agora[0])
        self.agora[0] += 121
        agendados.Agendador(self.ctx, outra, relogio=lambda: self.agora[0]).passo()
        self.assertEqual(len(self.fe.enviadas), 1)


class TestTelas(BaseAg):
    def test_envios_lista_agendados_sem_texto_depois_de_resolvido(self):
        self.agenda.criar(ANA, self.em(120), texto="prévia visível só enquanto agendado")
        h = envios.html_agendados(self.ctx)
        self.assertIn("prévia visível", h)
        self.assertIn("data-ag-cancelar", h)
        self.agora[0] += 121
        self.ag.passo()
        h = envios.html_agendados(self.ctx)
        self.assertNotIn("prévia visível", h)
        self.assertIn("Enviado", h)

    def test_quando_em_palavras(self):
        from datetime import datetime
        agora = datetime(2026, 11, 13, 16, 0)
        f = lambda *a: conversas.quando_agendado(int(datetime(*a).timestamp()), agora)
        self.assertEqual(f(2026, 11, 13, 18, 0), "hoje 18:00")
        self.assertEqual(f(2026, 11, 14, 9, 0), "amanhã 09:00")
        self.assertEqual(f(2026, 11, 16, 9, 0), "seg 16/11 09:00")

    def test_pagina_tem_relogio_e_seletor(self):
        t = texto(self.get("/conversas", c=ANA))
        self.assertIn('id="zap-agendar"', t)
        self.assertIn('id="zap-ag-quando"', t)
        js = conversas._JS
        self.assertIn("postJson('/conversas/agendar', {para: f.dataset.para, texto: t, quando: quando})", js)
        self.assertIn("'/conversas/agendar-midia?'", js)
        self.assertIn("/conversas/agendar-midia", __import__("app.server", fromlist=["x"]).CAMINHOS_BRUTOS)


if __name__ == "__main__":
    unittest.main()
