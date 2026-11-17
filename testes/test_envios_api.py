"""D-44 (pedido 10, R-15): API local de envio. Fila única com cadência e tetos, bloqueado nunca recebe, falha pausa,
reinício interrompe (texto só em memória), idempotência retoma, token vale como sessão só em /envios*, coletor sem acesso.
Fila testada passo a passo (processar_uma) com relógio falso; a thread só no servidor real e no /enviar legado."""
import http.client
import json
import os
import re
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import fila_envio as fe_mod  # noqa: E402
from app import server as srv_mod  # noqa: E402
from app.erros import EnvioIncerto  # noqa: E402
from app.paginas import _comum as c  # noqa: E402
from app.paginas import enviar, envios  # noqa: E402
from testes.test_conversas_zap import ANA, G1, MARCOS, PAR, AGORA, PsqlFalso, k, linha_lista  # noqa: E402
from testes.test_paginas import TOKEN, Base, texto  # noqa: E402

RAIZ = Path(__file__).resolve().parents[1]
NOVO = "5592900009999@s.whatsapp.net"
NOVO2 = "5592900008888@s.whatsapp.net"


class BaseFila(Base):
    """ctx com psql falso (ANA e MARCOS conversaram; G1 é grupo), fila determinística (sem thread) e Evolution falsa."""

    def setUp(self):
        super().setUp()
        self.dados.mkdir(parents=True, exist_ok=True)
        (self.dados / "cursor.json").write_text(json.dumps({"ultimo_seq": 0, "pareado_em": PAR}))
        self.psql = PsqlFalso()
        self.psql.lista = [
            linha_lista(ANA, AGORA, "a1", k(ANA, mid="A"), "Ana Paula", "conversation", {"conversation": "oi"}, 1, "Ana Paula", ""),
            linha_lista(MARCOS, AGORA - 60, "m1", k(MARCOS, mid="M"), "", "conversation", {"conversation": "ok"}, 0, "", ""),
        ]
        self.ctx.psql = self.psql
        self.fe.numeros = {"5592900009999": True, "5592900008888": False}
        self.fila = self.nova_fila()

    def nova_fila(self, **limites):
        f = fe_mod.Fila(self.ctx, limites={**fe_mod.PADRAO, **limites}, relogio=self.relogio, aleatorio=lambda lo, hi: lo)
        self.ctx.fila = f
        return f

    def criar(self, destinos, texto_msg="Oi {nome}, tudo bem?", origem="teste", **kw):
        return self.fila.criar(origem, texto_msg, [d if isinstance(d, dict) else {"para": d} for d in destinos], **kw)

    def rodar(self, passos=20):
        """Faz o papel da thread: processar_uma até não sobrar destinatário na fila (ou `passos` vezes), avançando o
        relógio falso pela espera que a fila pediu (cadência, conexão)."""
        esperas = []
        for _ in range(passos):
            e = self.fila.processar_uma()
            esperas.append(e)
            if self.banco.proximo_destino_envio() is None:
                break
            if e > 0:
                self.relogio.avancar(e)
        return esperas

    def envios_db(self):
        return self.banco._ler("SELECT para, hash_texto, por FROM envios ORDER BY id")


class TestAjudantes(unittest.TestCase):
    def test_limites_de_config_com_faixas(self):
        lim = fe_mod.limites_de_config({"envio": {"teto_lote": 999, "teto_dia_novos": -5, "intervalo_s": [90, 45],
                                                 "intervalo_novos_s": "x"}})
        self.assertEqual(lim["teto_lote"], 50)
        self.assertEqual(lim["teto_dia_novos"], 0)
        self.assertEqual(lim["intervalo_s"], (45, 90))
        self.assertEqual(lim["intervalo_novos_s"], (90, 180))
        self.assertEqual(fe_mod.limites_de_config(None), fe_mod.PADRAO)

    def test_normalizar_destino(self):
        self.assertEqual(fe_mod.normalizar_destino("92 90000-1111"), "5592900001111@s.whatsapp.net")
        self.assertEqual(fe_mod.normalizar_destino("+1 415 555 0100"), "14155550100@s.whatsapp.net")
        self.assertEqual(fe_mod.normalizar_destino(G1), G1)
        for ruim in ("", None, "123", "x@lid", "abc@s.whatsapp.net!", 5592900001111):
            with self.assertRaises(ValueError, msg=ruim):
                fe_mod.normalizar_destino(ruim)

    def test_texto_final_com_e_sem_nome(self):
        self.assertEqual(fe_mod.texto_final("Oi {nome}, tudo bem?", "Ana"), "Oi Ana, tudo bem?")
        self.assertEqual(fe_mod.texto_final("Oi {nome}, tudo bem?", ""), "Oi, tudo bem?")
        self.assertEqual(fe_mod.texto_final("{nome} , veja isto.\n  {nome}", ""), ", veja isto.\n")
        self.assertEqual(fe_mod.texto_final("sem marcador", ""), "sem marcador")


class TestCriar(BaseFila):
    def test_validacoes_400(self):
        casos = [dict(origem="Maiusc"), dict(origem="a" * 41), dict(texto_msg=""), dict(texto_msg="x" * 4001),
                 dict(destinos=[]), dict(destinos=[ANA] * 21), dict(destinos=["x@lid"]), dict(destinos=[ANA, ANA]),
                 dict(destinos=["12"]), dict(destinos=[5]), dict(idempotencia="com espaço")]
        for caso in casos:
            kw = dict(destinos=[ANA])
            kw.update(caso)
            with self.assertRaises(fe_mod.PedidoInvalido, msg=caso) as cm:
                self.criar(**kw)
            self.assertEqual(cm.exception.status, 400, caso)
        self.assertEqual(self.banco.lotes_envio(), [])

    def test_conhecido_novo_grupo_nome_e_bloqueado(self):
        self.banco.definir_apelido(MARCOS, "Marcão")
        self.psql.bloqueados = [NOVO2]
        lote = self.criar([ANA, MARCOS, G1, NOVO, {"para": NOVO2, "nome": "Z"}])
        d = {x["para"]: x for x in lote["destinatarios"]}
        self.assertTrue(d[ANA]["conhecido"] and d[MARCOS]["conhecido"] and d[G1]["conhecido"])
        self.assertFalse(d[NOVO]["conhecido"])
        self.assertEqual(d[ANA]["nome"], "Ana Paula")        # nome do WhatsApp porque o texto tem {nome}
        self.assertEqual(d[MARCOS]["nome"], "Marcão")        # apelido do Greg vence
        self.assertEqual(d[NOVO]["nome"], "")
        self.assertEqual((d[NOVO2]["estado"], d[NOVO2]["motivo"]), ("pulado", "bloqueado"))
        self.assertEqual(lote["estado"], "na_fila")
        self.assertFalse(lote["repetido"])

    def test_teto_do_dia_429_com_restante_e_reinicio(self):
        self.fila = self.nova_fila(teto_dia_novos=1)
        self.criar([NOVO])
        with self.assertRaises(fe_mod.PedidoInvalido) as cm:
            self.criar([NOVO2], texto_msg="outro")
        self.assertEqual(cm.exception.status, 429)
        self.assertEqual(cm.exception.extra["restante"], 0)
        self.assertRegex(cm.exception.extra["reinicia_em"], r"\d{4}-\d{2}-\d{2} 00:00")
        self.criar([ANA], texto_msg="conhecido não entra no teto de novos")

    def test_idempotencia_repete_sem_duplicar_e_409_com_texto_diferente(self):
        a = self.criar([ANA], idempotencia="k1")
        b = self.criar([ANA], idempotencia="k1")
        self.assertEqual((a["id"], b["id"], b["repetido"]), (a["id"], a["id"], True))
        with self.assertRaises(fe_mod.PedidoInvalido) as cm:
            self.criar([ANA], texto_msg="diferente", idempotencia="k1")
        self.assertEqual(cm.exception.status, 409)
        self.assertEqual(len(self.banco.lotes_envio()), 1)

    def test_lote_so_de_bloqueados_ja_nasce_concluido(self):
        self.psql.bloqueados = [ANA]
        lote = self.criar([ANA])
        self.assertEqual(lote["estado"], "concluido")
        self.assertEqual(self.fila.processar_uma(), fe_mod.ESPERA_OCIOSA_S)
        self.assertEqual(self.fe.enviadas, [])


class TestPassos(BaseFila):
    def test_envia_em_ordem_com_nome_registra_hash_e_nunca_o_texto(self):
        self.criar([ANA, {"para": MARCOS, "nome": "Marcos"}, G1], texto_msg="Oi {nome}, segue a proposta.")
        esperas = self.rodar()
        self.assertEqual([e["number"] for e in self.fe.enviadas], ["5592900001111", "5592900003333", G1])
        self.assertEqual([e["text"] for e in self.fe.enviadas],
                         ["Oi Ana Paula, segue a proposta.", "Oi Marcos, segue a proposta.", "Oi, segue a proposta."])
        reg = self.envios_db()
        self.assertEqual([r["por"] for r in reg], ["api:teste"] * 3)
        self.assertNotIn("proposta", json.dumps(reg))
        est = self.fila.estado(1)
        self.assertEqual(est["estado"], "concluido")
        self.assertEqual(est["contagem"]["enviados"], 3)
        self.assertIn("3 enviada(s)", est["motivo"])
        self.assertGreaterEqual(len(esperas), 3)
        self.assertEqual(self.fila.processar_uma(), fe_mod.ESPERA_OCIOSA_S)

    def test_cadencia_entre_envios_e_dobrada_para_novo(self):
        self.criar([ANA, MARCOS, NOVO, G1])
        self.assertEqual(self.fila.processar_uma(), 0.0)         # ANA sai na hora
        self.assertEqual(len(self.fe.enviadas), 1)
        e = self.fila.processar_uma()                              # MARCOS espera 45 s (lo do intervalo)
        self.assertGreater(e, 40)
        self.assertEqual(len(self.fe.enviadas), 1)
        self.relogio.avancar(45)
        self.fila.processar_uma()
        self.assertEqual(len(self.fe.enviadas), 2)
        self.relogio.avancar(45)
        self.fila.processar_uma()                                  # NOVO: conferido na Evolution e enviado
        self.assertEqual(len(self.fe.enviadas), 3)
        self.assertIn(("POST", f"/chat/whatsappNumbers/{self.fe.instancia}"), self.fe.chamadas)
        self.assertEqual(self.fila.processar_uma(), 60.0)         # a thread acorda no máximo a cada 60 s…
        self.assertGreater(self.fila.resumo()["proximo_em_s"], 85)  # …mas depois de um novo a pausa é 90 s
        self.relogio.avancar(90)
        self.fila.processar_uma()
        self.assertEqual(len(self.fe.enviadas), 4)
        self.assertEqual(self.fila.estado(1)["estado"], "concluido")

    def test_duas_origens_compartilham_a_cadencia(self):
        self.criar([ANA], origem="a")
        self.criar([MARCOS], origem="b")
        self.fila.processar_uma()
        self.assertGreater(self.fila.processar_uma(), 40)
        self.assertEqual(len(self.fe.enviadas), 1)
        self.assertEqual(self.fila.resumo()["na_fila"], 1)

    def test_novo_sem_whatsapp_e_pulado_e_conferir_false_nao_pergunta(self):
        self.criar([NOVO2])
        self.rodar()
        d = self.fila.estado(1)["destinatarios"][0]
        self.assertEqual((d["estado"], d["motivo"]), ("pulado", "sem WhatsApp"))
        self.assertEqual(self.fe.enviadas, [])
        self.fe.chamadas.clear()
        self.criar([NOVO2], texto_msg="legado", conferir=False)
        self.rodar()
        self.assertEqual(len(self.fe.enviadas), 1)
        self.assertNotIn(("POST", f"/chat/whatsappNumbers/{self.fe.instancia}"), self.fe.chamadas)

    def test_bloqueado_depois_de_criar_e_pulado_na_hora_de_mandar(self):
        self.criar([ANA, MARCOS])
        self.psql.bloqueados = [ANA]
        self.rodar()
        d = {x["para"]: x for x in self.fila.estado(1)["destinatarios"]}
        self.assertEqual(d[ANA]["estado"], "pulado")
        self.assertEqual(d[MARCOS]["estado"], "enviado")
        self.assertEqual(len(self.fe.enviadas), 1)

    def test_falha_pausa_o_lote_sem_registrar_e_retomar_segue(self):
        self.criar([ANA, MARCOS])
        self.fe.recusar_envio = True
        self.fila.processar_uma()
        est = self.fila.estado(1)
        self.assertEqual(est["estado"], "pausado")
        self.assertIn("recusou", est["motivo"])
        self.assertEqual(est["destinatarios"][0]["estado"], "fila")
        self.assertEqual(self.envios_db(), [])
        self.assertEqual(self.fila.processar_uma(), fe_mod.ESPERA_OCIOSA_S)   # pausado não anda sozinho
        self.fe.recusar_envio = False
        self.assertEqual(self.fila.retomar(1)["estado"], "na_fila")
        self.rodar()
        self.relogio.avancar(45)
        self.rodar()
        self.assertEqual(self.fila.estado(1)["estado"], "concluido")
        self.assertEqual(len(self.fe.enviadas), 2)

    def test_incerto_marca_registra_e_o_lote_segue(self):
        chamadas = []

        def incerto(numero, txt):
            chamadas.append(numero)
            if len(chamadas) == 1:
                raise EnvioIncerto("pode ter saído")
            return "ID2"
        self.ctx.evo = SimpleNamespace(enviar_texto=incerto)
        self.criar([ANA, MARCOS])
        self.rodar()
        self.relogio.avancar(45)
        self.rodar()
        est = self.fila.estado(1)
        self.assertEqual([d["estado"] for d in est["destinatarios"]], ["incerto", "enviado"])
        self.assertEqual([r["por"] for r in self.envios_db()], ["api:teste:incerto", "api:teste"])
        self.assertEqual(est["estado"], "concluido")
        self.assertIn("1 incerta(s)", est["motivo"])

    def _destino_falha(self, vezes):
        """SQLite travado nas próximas `vezes` gravações do estado de um destinatário; depois volta ao normal."""
        import sqlite3
        original, falta = self.banco.atualizar_destino_envio, [vezes]

        def atualizar(*a, **k):
            if falta[0] > 0:
                falta[0] -= 1
                raise sqlite3.OperationalError("database is locked")
            return original(*a, **k)
        self.banco.atualizar_destino_envio = atualizar
        return sqlite3.OperationalError

    def test_saiu_e_o_banco_nao_gravou_nao_manda_de_novo(self):
        # M2 (robustez 16/11): a mensagem saiu, o estado do destinatário não gravou; a fila não pode mandar a cada 5 s
        self.criar([ANA, MARCOS])
        erro = self._destino_falha(3)
        with self.assertRaises(erro):
            self.fila.processar_uma()
        self.assertEqual(len(self.fe.enviadas), 1)
        for _ in range(3):
            try:
                self.fila.processar_uma()
            except erro:
                pass
        self.assertEqual([e["number"] for e in self.fe.enviadas], ["5592900001111"])
        self.relogio.avancar(45)
        self.rodar()
        self.assertEqual([e["number"] for e in self.fe.enviadas], ["5592900001111", "5592900003333"])
        est = self.fila.estado(1)
        self.assertEqual([d["estado"] for d in est["destinatarios"]], ["enviado", "enviado"])
        self.assertEqual(est["estado"], "concluido")

    def test_incerto_e_o_banco_nao_gravou_nao_manda_de_novo(self):
        chamadas = []

        def incerto(numero, txt):
            chamadas.append(numero)
            raise EnvioIncerto("pode ter saído")
        self.ctx.evo = SimpleNamespace(enviar_texto=incerto)
        self.criar([ANA])
        erro = self._destino_falha(1)
        with self.assertRaises(erro):
            self.fila.processar_uma()
        self.rodar()
        self.assertEqual(len(chamadas), 1)
        self.assertEqual([d["estado"] for d in self.fila.estado(1)["destinatarios"]], ["incerto"])

    def test_conexao_fora_espera_e_open_libera(self):
        (self.dados / "conexao.json").write_text(json.dumps({"ultimo_estado": "close"}))
        self.criar([ANA])
        self.assertEqual(self.fila.processar_uma(), fe_mod.ESPERA_CONEXAO_S)
        self.assertEqual(self.fila.estado(1)["estado"], "aguardando_conexao")
        self.assertEqual(self.fe.enviadas, [])
        (self.dados / "conexao.json").write_text(json.dumps({"ultimo_estado": "open"}))
        self.rodar()
        self.assertEqual(self.fila.estado(1)["estado"], "concluido")

    def test_teto_do_dia_na_hora_de_mandar_pausa(self):
        self.fila = self.nova_fila(teto_dia_conhecidos=1)
        self.criar([ANA])
        self.rodar()
        # segundo lote passa na criação só porque o primeiro já saiu? não: hoje=1 >= teto → 429 na criação
        with self.assertRaises(fe_mod.PedidoInvalido):
            self.criar([MARCOS], texto_msg="2")
        # força a situação de corrida: lote criado antes do envio do primeiro
        self.fila = self.nova_fila(teto_dia_conhecidos=2)
        self.criar([MARCOS], texto_msg="2")
        self.fila.limites["teto_dia_conhecidos"] = 1
        self.relogio.avancar(45)
        self.fila.processar_uma()
        est = self.fila.estado(2)
        self.assertEqual(est["estado"], "pausado")
        self.assertIn("teto do dia", est["motivo"])

    def test_parar_cancela_o_resto_e_retomar_reenfileira(self):
        self.criar([ANA, MARCOS, G1])
        self.fila.processar_uma()
        est = self.fila.parar(1)
        self.assertEqual(est["estado"], "parado")
        self.assertEqual([d["estado"] for d in est["destinatarios"]], ["enviado", "cancelado", "cancelado"])
        self.assertEqual(self.fila.processar_uma(), fe_mod.ESPERA_OCIOSA_S)
        est = self.fila.retomar(1)
        self.assertEqual([d["estado"] for d in est["destinatarios"]], ["enviado", "fila", "fila"])
        self.assertIsNone(self.fila.parar(999))

    def test_reinicio_interrompe_e_so_a_idempotencia_retoma(self):
        self.criar([ANA, MARCOS], idempotencia="rm-1")
        self.fila.processar_uma()
        nova = fe_mod.Fila(self.ctx, relogio=self.relogio, aleatorio=lambda lo, hi: lo)   # "o painel reiniciou"
        self.ctx.fila = nova
        est = nova.estado(1)
        self.assertEqual(est["estado"], "interrompido")
        self.assertFalse(est["texto_em_memoria"])
        with self.assertRaises(fe_mod.PedidoInvalido) as cm:
            nova.retomar(1)
        self.assertEqual(cm.exception.status, 409)
        self.assertEqual(nova.processar_uma(), fe_mod.ESPERA_OCIOSA_S)
        self.fila = nova
        lote = self.criar([ANA, MARCOS], idempotencia="rm-1")      # a ferramenta repete o pedido
        self.assertEqual((lote["id"], lote["repetido"], lote["estado"]), (1, True, "na_fila"))
        self.relogio.avancar(45)
        self.rodar()
        self.assertEqual([e["number"] for e in self.fe.enviadas], ["5592900001111", "5592900003333"])  # ANA não repete

    def test_banco_da_evolution_fora_pausa_antes_de_mandar(self):
        self.criar([ANA])
        self.psql.falhar_bloqueados = True
        self.fila.processar_uma()
        self.assertEqual(self.fila.estado(1)["estado"], "pausado")
        self.assertEqual(self.fe.enviadas, [])

    def test_resumo(self):
        self.criar([ANA, NOVO])
        self.fila.processar_uma()
        r = self.fila.resumo()
        self.assertEqual((r["na_fila"], r["hoje"]["conhecidos"], r["hoje"]["novos"]), (1, 1, 0))
        self.assertGreater(r["proximo_em_s"], 40)
        self.assertEqual(r["conexao"], "desconhecida")


class TestRotas(BaseFila):
    def post(self, caminho, corpo=None, cab=None):
        return envios.ROTAS[caminho](self.req("POST", caminho, corpo or {}, cab=cab))

    def pedido(self, **extra):
        return {"origem": "claude-code", "texto": "Oi {nome}", "destinatarios": [{"para": ANA}], **extra}

    def test_post_sem_token_401_cross_site_403_ok_202(self):
        self.assertEqual(self.post("/envios", self.pedido()).status, 401)
        self.assertEqual(self.post("/envios", self.pedido(), cab={"x-vigia-envio": "errado"}).status, 401)
        self.assertEqual(self.post("/envios", self.pedido(), cab={"x-vigia-envio": TOKEN, "sec-fetch-site": "cross-site"}).status, 403)
        r = self.post("/envios", self.pedido(idempotencia="c1"), cab={"x-vigia-envio": TOKEN})
        self.assertEqual(r.status, 202, texto(r))
        d = json.loads(texto(r))
        self.assertEqual((d["origem"], d["estado"], d["n"]), ("claude-code", "na_fila", 1))
        r = self.post("/envios", self.pedido(idempotencia="c1"), cab={"x-vigia-envio": TOKEN})
        self.assertEqual(r.status, 200)
        self.assertTrue(json.loads(texto(r))["repetido"])
        r = self.post("/envios", self.pedido(destinatarios=[]), cab={"x-vigia-envio": TOKEN})
        self.assertEqual(r.status, 400)
        self.assertIn("erro", json.loads(texto(r)))
        self.assertEqual(self.fe.enviadas, [])   # fila sem thread: nada saiu ainda

    def test_estado_parar_retomar(self):
        self.post("/envios", self.pedido(destinatarios=[{"para": ANA}, {"para": MARCOS}]), cab={"x-vigia-envio": TOKEN})
        r = envios.ROTAS["/envios/{id}"](self.req("GET", "/envios/1"))
        self.assertEqual(r.status, 200)
        self.assertEqual(json.loads(texto(r))["contagem"]["fila"], 2)
        self.assertEqual(envios.ROTAS["/envios/{id}"](self.req("GET", "/envios/9")).status, 404)
        r = self.post("/envios/{id}/parar", cab=None)
        self.assertEqual(r.status, 404)   # sem id na rota de teste genérica
        req = self.req("POST", "/envios/1/parar")
        r = envios.ROTAS["/envios/{id}/parar"](req)
        self.assertEqual((r.status, dict(r.cabecalhos).get("Location")), (303, "/envios?aviso=parado&lote=1"))   # formulário da tela: volta com o toast
        self.assertEqual(self.fila.estado(1)["estado"], "parado")
        r = envios.ROTAS["/envios/{id}/retomar"](self.req("POST", "/envios/1/retomar"))
        self.assertEqual(r.status, 423)                                                     # sem token e trava fechada
        self.assertEqual(self.fila.estado(1)["estado"], "parado")
        r = envios.ROTAS["/envios/{id}/retomar"](self.req("POST", "/envios/1/retomar", cab={"x-vigia-envio": TOKEN}))
        self.assertEqual(r.status, 200)
        self.assertEqual(json.loads(texto(r))["estado"], "na_fila")
        from app.paginas import conversas
        self.fila.parar(1)
        conversas.servicos(self.ctx).liberacao.liberar(TOKEN, TOKEN)                        # Touch ID no app
        r = envios.ROTAS["/envios/{id}/retomar"](self.req("POST", "/envios/1/retomar"))
        self.assertEqual(r.status, 303)
        self.assertEqual(self.fila.estado(1)["estado"], "na_fila")

    def test_tela_e_json(self):
        self.post("/envios", self.pedido(texto="Proposta sigilosa 7731 para {nome}"), cab={"x-vigia-envio": TOKEN})
        t = texto(envios.ROTAS["/envios"](self.req("GET", "/envios")))
        for trecho in ('class="titulo">Envios', "claude-code", "na fila", "Como integrar", "vigia-enviar", 'href="/envios"', "Parar"):
            self.assertIn(trecho, t)
        self.assertNotIn("7731", t)   # o texto do lote nunca aparece na tela nem no JSON
        r = envios.ROTAS["/envios"](self.req("GET", "/envios", cab={"accept": "application/json"}))
        d = json.loads(texto(r))
        self.assertEqual(d["lotes"][0]["origem"], "claude-code")
        self.assertEqual(d["resumo"]["na_fila"], 1)
        self.assertNotIn("7731", texto(r))
        self.assertIn(("/envios", "Envios"), c.NAV)


class TestEnviarLegado(Base):
    """POST /enviar passa pela fila (thread real) e mantém a resposta síncrona."""

    def post(self, corpo):
        return enviar.ROTAS["/enviar"](self.req("POST", "/enviar", corpo, cab={"x-vigia-envio": TOKEN}))

    def test_envia_e_registra_como_api_painel(self):
        r = self.post({"para": "5592900000000", "texto": "oi"})
        self.addCleanup(self.ctx.fila.encerrar)
        self.assertEqual(r.status, 200, texto(r))
        d = json.loads(texto(r))
        self.assertEqual(d["enviadas"], 1)
        self.assertEqual(self.banco._ler("SELECT por FROM envios")[0]["por"], "api:painel")
        self.assertEqual(self.ctx.fila.estado(d["lote"])["estado"], "concluido")

    def test_recusa_vira_502_e_lote_parado(self):
        self.fe.recusar_envio = True
        r = self.post({"para": "5592900000000", "texto": "oi"})
        self.addCleanup(self.ctx.fila.encerrar)
        self.assertEqual(r.status, 502)
        self.assertEqual(self.ctx.fila.estado(json.loads(texto(r))["lote"])["estado"], "parado")
        self.assertEqual(self.banco._ler("SELECT por FROM envios"), [])


class TestServidor(unittest.TestCase):
    """Token de envio vale como sessão só em /envios*; o CLI real fala com o servidor real."""

    def setUp(self):
        base = Base()
        base.setUp()
        self.addCleanup(base.tmp.cleanup)
        self.addCleanup(base.fe.parar)
        self.addCleanup(base.ff.parar)
        self.base = base
        self.ctx = base.ctx
        self.ctx.executor = None
        self.ctx.tokens.painel = "tok-painel"
        self.srv = srv_mod.criar_servidor(self.ctx, "tok-painel", porta=0)
        self.porta = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.addCleanup(lambda: getattr(self.ctx, "fila", None) and self.ctx.fila.encerrar())

    def http(self, metodo, caminho, corpo=None, cab=None):
        con = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=10)
        cabs = {"Host": f"127.0.0.1:{self.porta}", "Accept": "application/json"}
        if corpo is not None:
            cabs["Content-Type"] = "application/json"
        cabs.update(cab or {})
        con.request(metodo, caminho, body=json.dumps(corpo).encode() if corpo is not None else None, headers=cabs)
        r = con.getresponse()
        t = r.read()
        con.close()
        return r.status, t

    def test_token_abre_so_envios(self):
        corpo = {"origem": "cli", "texto": "oi", "destinatarios": [{"para": "5592900000000"}], "conferir": False}
        st, _ = self.http("POST", "/envios", corpo)
        self.assertEqual(st, 401)
        st, t = self.http("POST", "/envios", corpo, cab={"X-Vigia-Envio": TOKEN})
        self.assertEqual(st, 202, t)
        lid = json.loads(t)["id"]
        st, t = self.http("GET", f"/envios/{lid}", cab={"X-Vigia-Envio": TOKEN})
        self.assertEqual(st, 200)
        st, _ = self.http("GET", "/conversas", cab={"X-Vigia-Envio": TOKEN})
        self.assertEqual(st, 401)   # o token não vira sessão fora de /envios

    def test_cli_cria_espera_e_consulta(self):
        env_tok = Path(self.base.tmp.name) / "envio.env"
        env_tok.write_text(f"VIGIA_TOKEN_ENVIO={TOKEN}\n")
        env_tok.chmod(0o600)
        amb = {**os.environ, "VIGIA_PORT": str(self.porta), "VIGIA_ENVIO_ENV": str(env_tok)}
        pedido = json.dumps({"origem": "cli-teste", "texto": "Oi {nome}", "conferir": False,
                             "destinatarios": [{"para": "92 90000-0000", "nome": "Ana"}]})
        r = subprocess.run([sys.executable, str(RAIZ / "bin" / "vigia-enviar"), "--esperar", "20"], input=pedido,
                           capture_output=True, text=True, env=amb, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        saida = json.loads(r.stdout)
        self.assertEqual(saida["estado"], "concluido")
        self.assertEqual(saida["destinatarios"][0]["estado"], "enviado")
        self.assertEqual(self.base.fe.enviadas[0]["text"], "Oi Ana")
        r = subprocess.run([sys.executable, str(RAIZ / "bin" / "vigia-enviar"), "fila"], capture_output=True, text=True,
                           env=amb, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["lotes"][0]["origem"], "cli-teste")
        amb["VIGIA_ENVIO_ENV"] = str(Path(self.base.tmp.name) / "nao-existe.env")
        r = subprocess.run([sys.executable, str(RAIZ / "bin" / "vigia-enviar"), "fila"], capture_output=True, text=True,
                           env=amb, timeout=30)
        self.assertEqual(r.returncode, 2)
        self.assertNotIn(TOKEN, r.stdout + r.stderr)


class TestColetorSemAcesso(unittest.TestCase):
    def test_coletor_e_ponte_nao_tocam_na_fila_nem_no_token(self):
        for rel in ("bin/coletor.py", "bin/vigia-ponte", "app/lotes.py", "app/ponte"):
            p = RAIZ / rel
            arquivos = list(p.glob("*.py")) if p.is_dir() else [p]
            for a in arquivos:
                t = a.read_text(encoding="utf-8")
                self.assertNotRegex(t, r"fila_envio|paginas\.envios|/envios", a)
        t = (RAIZ / "bin" / "coletor.py").read_text(encoding="utf-8")
        self.assertIn('"VIGIA_TOKEN_ENVIO"', t)   # continua fora do ambiente do coletor
        self.assertRegex((RAIZ / "app" / "lotes.py").read_text(encoding="utf-8"), re.escape("Read(~/.vigia-envio.env)"))
        # D-45: do Keychain o coletor pede SÓ as duas chaves dele; nunca o token de envio (R-9), nem leitura avulsa
        pedidos = re.findall(r"segredos\.completar\(env,\s*\(([^)]*)\)\)", t)
        self.assertEqual(len(pedidos), 1, "coletor deve chamar segredos.completar uma vez, com lista explícita")
        self.assertNotIn("VIGIA_TOKEN_ENVIO", pedidos[0])
        self.assertNotRegex(t, r"segredos\.ler\(")
        self.assertNotRegex((RAIZ / "bin" / "vigia-ponte").read_text(encoding="utf-8"), r"VIGIA_TOKEN_ENVIO|segredos\.ler\(")


if __name__ == "__main__":
    unittest.main()
