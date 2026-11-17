"""Testes das páginas do painel (E9) com FakeEvolution e FakeFrentes: QR só no clique, /enviar com token, escape de HTML.
Quem chama: unittest (testes.test_paginas).
O que não faz: não sobe app/server.py nem toca Evolution/Frentes reais.
"""
import hashlib
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.banco import Banco  # noqa: E402
from app.conexao import Conexao  # noqa: E402
from app.cursor import Cursor  # noqa: E402
from app.erros import EnvioIncerto  # noqa: E402
from app.evolution_client import EvolutionClient  # noqa: E402
from app.frentes_ponte import FrentesPonte  # noqa: E402
from app.http import Requisicao  # noqa: E402
from app.paginas import conexao, conversas, demandas, enviar, frentes, metricas, saude  # noqa: E402
from testes.fakes import FakeEvolution, FakeFrentes, imagem_jpeg_minima  # noqa: E402
from testes.util_contrato import RelogioFalso  # noqa: E402

TOKEN = "tok-envio-teste"
XSS = '<script>alert("x")</script>'


def texto(r) -> str:
    return r.corpo.decode("utf-8") if isinstance(r.corpo, bytes) else r.corpo


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dados = Path(self.tmp.name) / "dados"
        self.fe = FakeEvolution().iniciar()
        self.addCleanup(self.fe.parar)
        self.ff = FakeFrentes().iniciar()
        self.addCleanup(self.ff.parar)
        self.evo = EvolutionClient(self.fe.url, self.fe.instancia, self.fe.apikey)
        self.banco = Banco(str(self.dados / "vigia.db"))
        self.ponte = FrentesPonte(self.ff.url, self.ff.token)
        self.cursor = Cursor.carregar(self.dados / "cursor.json")
        self.relogio = RelogioFalso()
        self.conexao = Conexao(self.evo, self.banco, relogio=self.relogio, cursor=self.cursor,
                               estado_arq=self.dados / "conexao.json")
        self.ctx = SimpleNamespace(banco=self.banco, ponte=self.ponte, conexao=self.conexao, evo=self.evo,
                                   config={"dados_dir": str(self.dados)},
                                   tokens=SimpleNamespace(envio=TOKEN, painel="p"))

    def req(self, metodo, caminho, corpo=None, query=None, cab=None):
        return Requisicao(metodo, caminho, query or {}, corpo or {}, {}, self.ctx, cabecalhos=cab or {})

    def classificar(self, **kw):
        base = dict(msg_id="m1", conversa="5592999@s.whatsapp.net", contato="Ana", ts=int(time.time()),
                    classe="demanda", resumo="pede orçamento", confianca="alta")
        base.update(kw)
        return self.banco.gravar_classificacao(**base)


class TestConexao(Base):
    def test_get_mostra_estado_sem_gerar_qr(self):
        r = conexao.ROTAS["/conexao"](self.req("GET", "/conexao"))
        self.assertEqual(r.status, 200)
        self.assertIn("Gerar QR", texto(r))
        self.assertEqual(self.fe.qrs_gerados, 0)
        self.assertNotIn("QR Code do WhatsApp\" width=\"260\" height=\"260\" src=\"data:", texto(r))

    def test_get_e_tick_nao_geram_qr_mesmo_repetidos(self):
        for _ in range(5):
            conexao.ROTAS["/conexao"](self.req("GET", "/conexao"))
            conexao.ROTAS["/conexao/tick"](self.req("GET", "/conexao/tick"))
        self.assertEqual(self.fe.qrs_gerados, 0)

    def test_post_qr_mostra_imagem_e_contagem_e_duplo_clique_nao_gasta(self):
        r = conexao.ROTAS["/conexao/qr"](self.req("POST", "/conexao/qr"))
        self.assertEqual(r.status, 303)
        self.assertEqual(dict(r.cabecalhos)["Location"], "/conexao")
        t = texto(conexao.ROTAS["/conexao"](self.req("GET", "/conexao")))
        self.assertIn('src="data:image/png;base64,', t)
        self.assertIn('data-ttl="60"', t)
        self.assertIn('id="qr-msg" class="alerta" role="status"', t)
        conexao.ROTAS["/conexao/qr"](self.req("POST", "/conexao/qr"))
        self.assertEqual(self.fe.qrs_gerados, 1)

    def test_tick_devolve_json_com_ttl_enquanto_gerando(self):
        conexao.ROTAS["/conexao/qr"](self.req("POST", "/conexao/qr"))
        r = conexao.ROTAS["/conexao/tick"](self.req("GET", "/conexao/tick"))
        d = json.loads(texto(r))
        self.assertEqual(d["estado"], "gerando")
        self.assertGreater(d["ttl"], 0)
        self.assertEqual(self.fe.qrs_gerados, 1)

    def test_renovacao_limitada_a_3_qrs_e_depois_expira_sem_gastar_mais(self):
        conexao.ROTAS["/conexao/qr"](self.req("POST", "/conexao/qr"))
        tick = lambda: json.loads(texto(conexao.ROTAS["/conexao/tick"](self.req("GET", "/conexao/tick"))))
        for esperado in (2, 3):
            self.relogio.avancar(61)
            d = tick()
            self.assertEqual(self.fe.qrs_gerados, esperado)
            self.assertEqual(d["estado"], "renovando")
            self.assertGreater(d["ttl"], 0)
        self.relogio.avancar(61)
        d = tick()
        self.assertEqual((d["estado"], d["ttl"]), ("expirado", 0))
        for _ in range(120):
            self.relogio.avancar(5)
            tick()
        self.assertEqual(self.fe.qrs_gerados, 3)
        self.assertIn("expirado", texto(conexao.ROTAS["/conexao"](self.req("GET", "/conexao"))))

    def test_conectado_grava_pareado_em_via_tick(self):
        conexao.ROTAS["/conexao/qr"](self.req("POST", "/conexao/qr"))
        self.fe.estado = "open"
        d = json.loads(texto(conexao.ROTAS["/conexao/tick"](self.req("GET", "/conexao/tick"))))
        self.assertEqual(d["estado"], "conectado")
        self.assertIsNotNone(Cursor.carregar(self.dados / "cursor.json").pareado_em)

    def test_teto_diario_recusa_com_429_sem_gastar(self):
        self.conexao.teto_dia = 0
        r = conexao.ROTAS["/conexao/qr"](self.req("POST", "/conexao/qr"))
        self.assertEqual(r.status, 429)
        self.assertEqual(self.fe.qrs_gerados, 0)

    def test_post_de_origem_externa_e_metodo_errado(self):
        r = conexao.ROTAS["/conexao/qr"](self.req("POST", "/conexao/qr", cab={"origin": "http://evil.example"}))
        self.assertEqual(r.status, 403)
        r = conexao.ROTAS["/conexao/qr"](self.req("POST", "/conexao/qr", cab={"host": "evil.example"}))
        self.assertEqual(r.status, 403)
        r = conexao.ROTAS["/conexao/qr"](self.req("GET", "/conexao/qr"))
        self.assertEqual(r.status, 405)
        self.assertEqual(self.fe.qrs_gerados, 0)

    def test_origem_host_e_sec_fetch_site(self):
        from app.paginas._comum import origem_ok
        ok = lambda **cab: origem_ok(self.req("POST", "/x", cab=cab))
        h = "127.0.0.1:8120"
        self.assertTrue(ok(host=h, origin="http://" + h))
        self.assertTrue(ok(host="localhost:8120", referer="http://localhost:8120/conexao"))
        self.assertTrue(ok(**{"host": h, "sec-fetch-site": "same-origin", "origin": "http://" + h}))
        self.assertFalse(ok(**{"host": h, "sec-fetch-site": "same-site"}))
        self.assertFalse(ok(**{"host": h, "sec-fetch-site": "cross-site", "origin": "http://" + h}))
        self.assertFalse(ok(host=h, origin="http://127.0.0.1:9999"))   # porta diferente do Host
        self.assertFalse(ok(host=h, origin="http://localhost:8120"))   # nome diferente do Host
        self.assertFalse(ok(host=h, origin="null"))
        self.assertFalse(ok(host="evil.example:8120", origin="http://evil.example:8120"))
        self.assertFalse(ok(host=h, referer="http://evil.example/"))

    def test_tick_exige_origem(self):
        r = conexao.ROTAS["/conexao/tick"](self.req("GET", "/conexao/tick", cab={"origin": "http://evil.example"}))
        self.assertEqual(r.status, 403)
        r = conexao.ROTAS["/conexao/tick"](self.req("GET", "/conexao/tick", cab={"sec-fetch-site": "cross-site"}))
        self.assertEqual(r.status, 403)

    def test_sessao_abandonada_expira_no_tick_com_logout_e_um_unico_qr(self):
        conexao.ROTAS["/conexao/qr"](self.req("POST", "/conexao/qr"))
        self.fe.estado = "connecting"
        self.relogio.avancar(3 * 3600)
        t = texto(conexao.ROTAS["/conexao"](self.req("GET", "/conexao")))
        self.assertEqual(self.fe.qrs_gerados, 1)
        self.assertNotIn("data:image/png", t)
        d = json.loads(texto(conexao.ROTAS["/conexao/tick"](self.req("GET", "/conexao/tick"))))
        self.assertEqual(d["estado"], "expirado")
        self.assertEqual(d["motivo"], "tick_atrasado")
        self.assertIn("logout_confirmado", d)
        self.assertEqual(self.fe.qrs_gerados, 1)
        self.assertIn(("DELETE", f"/instance/logout/{self.fe.instancia}"), self.fe.chamadas)
        self.assertIn("QR expirou", texto(conexao.ROTAS["/conexao"](self.req("GET", "/conexao"))))

    def test_logout_nao_confirmado_vira_aviso_vermelho(self):
        t = texto(conexao.ROTAS["/conexao"](self.req("GET", "/conexao", query={"logout": "0"})))
        self.assertIn("ficou presa tentando conectar", t)
        self.assertIn('form="f-reiniciar">Reiniciar Evolution</button>', t)   # pedido 75: o erro traz o botão
        self.fe.estado = "connecting"
        self.assertIn("ficou presa tentando conectar", texto(conexao.ROTAS["/conexao"](self.req("GET", "/conexao"))))

    def test_aviso_socket_morto_e_ultimo_ciclo_do_monitor(self):
        agora = int(time.time())
        self.dados.mkdir(parents=True, exist_ok=True)
        (self.dados / "conexao.json").write_text(json.dumps(
            {"evento": "socket_morto", "reason_code": 428, "atualizado_em": agora}), encoding="utf-8")
        t = texto(conexao.ROTAS["/conexao"](self.req("GET", "/conexao")))
        self.assertIn("travou (428)", t)
        self.assertIn("Reiniciar Evolution", t)
        self.assertIn("Último ciclo do monitor", t)
        self.assertNotIn("monitor ainda não rodou", t)

    def test_contador_ruim_no_teto_e_botao_explica(self):
        self.conexao.teto_dia = 1
        self.banco.registrar_qr("clique")
        t = texto(conexao.ROTAS["/conexao"](self.req("GET", "/conexao")))
        self.assertIn('id="qrs-hoje" class="ruim"', t)
        self.assertIn("teto do dia", t)
        self.assertIn("disabled", t)

    def test_segurar_semana_grava_marca_e_a_tela_mostra_ate_quando(self):
        from app.semana import semana_fechada, segura
        r = conexao.ROTAS["/conexao/segurar"](self.req("POST", "/conexao/segurar"))
        self.assertEqual(r.status, 303)
        self.assertTrue(segura(self.dados, semana_fechada()))
        t = texto(conexao.ROTAS["/conexao"](self.req("GET", "/conexao")))
        self.assertIn("segurada até", t)
        self.assertIn(semana_fechada(), t)
        r = conexao.ROTAS["/conexao/segurar"](self.req("POST", "/conexao/segurar", cab={"origin": "http://evil.example"}))
        self.assertEqual(r.status, 403)
        self.assertEqual(conexao.ROTAS["/conexao/segurar"](self.req("GET", "/conexao/segurar")).status, 405)

    def test_pausar_alterna_arquivo(self):
        h = conexao.ROTAS["/conexao/pausar"]
        self.assertEqual(h(self.req("POST", "/conexao/pausar")).status, 303)
        self.assertTrue((self.dados / "PAUSADO").exists())
        self.assertIn("Retomar", texto(conexao.ROTAS["/conexao"](self.req("GET", "/conexao"))))
        h(self.req("POST", "/conexao/pausar"))
        self.assertFalse((self.dados / "PAUSADO").exists())

    def test_reiniciar_chama_executor_injetado(self):
        chamadas = []
        self.ctx.executor = lambda cmd, cwd=None: chamadas.append(cmd)
        r = conexao.ROTAS["/conexao/reiniciar"](self.req("POST", "/conexao/reiniciar"))
        self.assertEqual(r.status, 303)
        self.assertEqual(chamadas, [["bin/evolution.sh", "restart"]])

    def test_evolution_fora_nao_derruba_a_pagina(self):
        self.fe.parar()
        r = conexao.ROTAS["/conexao"](self.req("GET", "/conexao"))
        self.assertEqual(r.status, 200)
        self.assertIn("indisponível", texto(r))

    # --- pedido 41: Ler agora, Parear de novo, última mensagem, avisos da sonda/silêncio ---
    def test_ler_agora_dispara_o_launchagent_pelo_executor_e_volta_para_demandas(self):
        chamadas = []
        self.ctx.executor = lambda cmd, cwd=None: chamadas.append(cmd)
        r = conexao.ROTAS["/conexao/ler-agora"](self.req("POST", "/conexao/ler-agora", {"volta": "/demandas"}))
        self.assertEqual((r.status, dict(r.cabecalhos)["Location"]), (303, "/demandas?lendo=1"))
        self.assertEqual(len(chamadas), 1)
        self.assertEqual(chamadas[0][:2], ["launchctl", "kickstart"])
        self.assertTrue(chamadas[0][2].endswith("/" + conexao.LABEL_COLETOR))
        r = conexao.ROTAS["/conexao/ler-agora"](self.req("POST", "/conexao/ler-agora", {"volta": "/qualquer"}))
        self.assertEqual(dict(r.cabecalhos)["Location"], "/conexao?lendo=1")   # volta só para destinos conhecidos
        self.assertEqual(conexao.ROTAS["/conexao/ler-agora"](self.req("GET", "/conexao/ler-agora")).status, 405)

    def test_ler_agora_pausado_nao_dispara(self):
        self.dados.mkdir(parents=True, exist_ok=True)
        (self.dados / "PAUSADO").write_text("")
        chamadas = []
        self.ctx.executor = lambda cmd, cwd=None: chamadas.append(cmd)
        r = conexao.ROTAS["/conexao/ler-agora"](self.req("POST", "/conexao/ler-agora"))
        self.assertEqual((r.status, dict(r.cabecalhos)["Location"]), (303, "/conexao?lendo=0"))
        self.assertEqual(chamadas, [])
        t = texto(conexao.ROTAS["/conexao"](self.req("GET", "/conexao", query={"lendo": "0"})))
        self.assertIn("Leituras pausadas. Retome", t)
        self.assertNotIn('form="f-ler"', t)                                   # item do menu some com PAUSADO

    def test_ler_agora_json_e_falha_do_executor(self):
        self.ctx.executor = lambda cmd, cwd=None: None
        r = conexao.ROTAS["/conexao/ler-agora"](self.req("POST", "/conexao/ler-agora", cab={"x-vigia-auto": "1"}))
        self.assertEqual(r.status, 200)
        self.assertEqual(json.loads(texto(r))["disparado"], True)

        def falha(cmd, cwd=None):
            raise OSError("launchctl sumiu")
        self.ctx.executor = falha
        r = conexao.ROTAS["/conexao/ler-agora"](self.req("POST", "/conexao/ler-agora"))
        self.assertEqual(r.status, 502)
        self.assertIn("Não deu para disparar", texto(r))

    def test_desparear_faz_logout_e_libera_o_qr(self):
        self.fe.estado = "open"
        t = texto(conexao.ROTAS["/conexao"](self.req("GET", "/conexao")))
        self.assertIn("Parear de novo…", t)
        r = conexao.ROTAS["/conexao/desparear"](self.req("POST", "/conexao/desparear"))
        self.assertEqual((r.status, dict(r.cabecalhos)["Location"]), (303, "/conexao?despareado=1"))
        self.assertIn(("DELETE", f"/instance/logout/{self.fe.instancia}"), self.fe.chamadas)
        self.assertEqual(self.conexao.estado, "ocioso")
        self.fe.estado = "close"
        t = texto(conexao.ROTAS["/conexao"](self.req("GET", "/conexao", query={"despareado": "1"})))
        self.assertIn("Sessão do WhatsApp encerrada.", t)
        self.assertNotIn("Parear de novo…", t)                                 # só aparece com a conexão open
        self.assertEqual(conexao.ROTAS["/conexao/desparear"](self.req("GET", "/conexao/desparear")).status, 405)

    def test_ultima_mensagem_recebida_na_pagina(self):
        t = texto(conexao.ROTAS["/conexao"](self.req("GET", "/conexao")))
        self.assertIn("Última mensagem recebida", t)
        self.assertIn("Ainda não conferida", t)
        self.dados.mkdir(parents=True, exist_ok=True)
        (self.dados / "ultima-mensagem.json").write_text(json.dumps({"ultima_em": int(time.time()) - 7 * 3600 - 30}))
        t = texto(conexao.ROTAS["/conexao"](self.req("GET", "/conexao")))
        self.assertIn('<b class="ruim">', t)
        self.assertIn("há 7 h 00", t)

    def test_avisos_de_sonda_e_silencio(self):
        agora = time.time()
        aviso = conexao._avisos_monitor(self.conexao, {"evento": "socket_zumbi", "motivo": "sonda_falhou_reiniciada",
                                                       "reiniciado_em": agora, "atualizado_em": agora}, agora)
        self.assertIn("reiniciada sozinha", aviso)
        self.assertIn("aviso-inline", aviso)
        aviso = conexao._avisos_monitor(self.conexao, {"evento": "socket_zumbi", "motivo": "sonda_falhou_persistente"}, agora)
        self.assertIn("Parear de novo", aviso)
        self.assertIn("erro-inline", aviso)
        aviso = conexao._avisos_monitor(self.conexao, {"evento": "silencio", "motivo": "sem_mensagem_7h"}, agora)
        self.assertIn("há 7 h", aviso)
        self.assertIn("Aparelhos conectados", aviso)
        self.assertEqual(conexao._avisos_monitor(self.conexao, {"evento": "ok"}, agora), "")

    def test_indicador_fica_ambar_com_silencio_ou_socket_surdo(self):   # pedido 41: "verdinho e não atualizava"
        def indicador(monitor):
            self.dados.mkdir(exist_ok=True)
            (self.dados / "conexao.json").write_text(json.dumps({"ultimo_estado": "open", **monitor}), encoding="utf-8")
            t = texto(conexao.ROTAS["/conexao"](self.req("GET", "/conexao")))
            return next(l for l in t.splitlines() if 'class="conexao-estado' in l)
        linha = indicador({"evento": "silencio", "motivo": "sem_mensagem_7h"})
        self.assertIn('class="conexao-estado alerta', linha)
        self.assertIn("Conectado, sem mensagem há 7 h", linha)
        for evento in ("socket_zumbi", "socket_morto"):
            linha = indicador({"evento": evento, "motivo": "sonda_falhou_reiniciada"})
            self.assertIn('class="conexao-estado alerta', linha)
            self.assertIn("Conectado, socket sem resposta", linha)
        linha = indicador({"evento": "ok", "motivo": "repareado"})
        self.assertIn('class="conexao-estado ok', linha)
        self.assertIn(">Conectado<", linha)

    def test_indicador_mostra_a_hora_do_ultimo_ciclo_e_a_lateral_tem_json_ao_vivo(self):   # pedido 43
        self.dados.mkdir(exist_ok=True)
        (self.dados / "conexao.json").write_text(json.dumps({"ultimo_estado": "open", "evento": "ok"}), encoding="utf-8")
        em = int(time.time())
        (self.dados / "ultima-leitura.json").write_text(json.dumps({"em": em, "status": "sem_novidade", "fila": 0,
                                                                     "novas": 0}), encoding="utf-8")
        hora = time.strftime("%H:%M", time.localtime(em))
        t = texto(conexao.ROTAS["/conexao"](self.req("GET", "/conexao")))
        self.assertIn(f"Conectado, lido às {hora}", t)        # sem lote nenhum no banco: a hora é a do ciclo
        self.assertIn("fetch('/conexao/lateral'", t)
        self.assertIn("setInterval(atualizarLateral, 30000)", t)
        r = conexao.ROTAS["/conexao/lateral"](self.req("GET", "/conexao/lateral"))
        self.assertEqual(r.status, 200)
        self.assertEqual(json.loads(texto(r)), {"classe": "ok", "texto": f"Conectado, lido às {hora}", "demandas": "", "avisos": ""})
        self.classificar()
        r = conexao.ROTAS["/conexao/lateral"](self.req("GET", "/conexao/lateral"))
        self.assertEqual(json.loads(texto(r))["demandas"], "1")
        self.assertEqual(conexao.ROTAS["/conexao/lateral"](self.req("POST", "/conexao/lateral")).status, 405)

    def test_lateral_tem_o_botao_de_modo_de_gravacao(self):   # pedido 42
        t = texto(conexao.ROTAS["/conexao"](self.req("GET", "/conexao")))
        self.assertIn('class="lateral-ajustes lateral-gravar vigia-gravar" aria-pressed="false"', t)
        self.assertIn("Modo de gravação (⌘⇧B)", t)
        self.assertNotIn("$gravar", t)
        self.assertIn("'#vigia-gravar, .vigia-gravar'", t)
        self.assertIn("Greg · modo de gravação", t)

    def test_modo_de_gravacao_esconde_midia_com_borrao_forte(self):   # pedido 42, complemento: fotos e vídeos
        t = texto(conexao.ROTAS["/conexao"](self.req("GET", "/conexao")))
        regra = next(l for l in t.splitlines() if l.startswith(":root[data-gravacao] :is(.zap-img"))
        for pedaco in (".dm-mini", "video", "blur(30px)", "clip-path: inset(0 round 10px)", "pointer-events: none"):
            self.assertIn(pedaco, regra)
        self.assertIn(":root[data-gravacao] img.avatar { filter: blur(14px)", t)


class TestDemandas(Base):
    def test_lista_escapa_html_de_mensagem(self):
        self.classificar(contato=XSS, resumo=XSS)
        t = texto(demandas.ROTAS["/demandas"](self.req("GET", "/demandas")))
        self.assertNotIn("<script>alert", t)
        self.assertIn("&lt;script&gt;", t)

    def test_filtros_classe_conversa_dia(self):
        agora = int(time.time())
        self.classificar(msg_id="a", classe="atencao", resumo="urgente A")
        self.classificar(msg_id="b", classe="demanda", resumo="demanda B", conversa="outra@s.whatsapp.net")
        self.classificar(msg_id="c", classe="demanda", resumo="velha C", ts=agora - 5 * 86400)
        self.classificar(msg_id="d", classe="sem_acao", resumo="nada D")
        for i in range(250):  # sem_acao em massa não pode empurrar o resto para fora do limite
            self.classificar(msg_id=f"sa{i}", classe="sem_acao", resumo="nada X", ts=agora + 10 + i)
        get = lambda q: texto(demandas.ROTAS["/demandas"](self.req("GET", "/demandas", query=q)))
        t = get({})
        self.assertIn("urgente A", t)
        self.assertNotIn("nada D", t)
        t = get({"classe": "atencao"})
        self.assertIn("urgente A", t)
        self.assertNotIn("demanda B", t)
        t = get({"conversa": "outra@s.whatsapp.net"})
        self.assertIn("demanda B", t)
        self.assertNotIn("urgente A", t)
        hoje = time.strftime("%Y-%m-%d")
        t = get({"dia": hoje})
        self.assertIn("urgente A", t)
        self.assertNotIn("velha C", t)
        self.assertIn("nada X", get({"classe": "sem_acao"}))
        self.assertIn("Dia inválido", get({"dia": "ontem"}))
        self.assertIn("Classe inválida", get({"classe": "zzz"}))

    def test_confirmar_cartao_ja_lancado_e_409_nao_ha_o_que_confirmar(self):   # D-58
        item = self.ff.semear_item(texto="WA Ana: x", origem="whatsapp")
        cid = self.classificar(item_id=item["id"], confianca="baixa")
        r = demandas.ROTAS["/demandas/{id}/confirmar"](self.req("POST", f"/demandas/{cid}/confirmar"))
        self.assertEqual(r.status, 409)
        self.assertEqual(self.ff.patches, [])
        self.assertIsNone(self.banco.classificacao(cid)["decisao"])

    def test_recusar_grava_decisao_e_cancela_no_frentes(self):   # D-49: recusa não deixa lixo aberto no Frentes
        item = self.ff.semear_item(texto="WA Ana: x", origem="whatsapp", confirmado=0)
        cid = self.classificar(confianca="baixa", item_id=item["id"])
        r = demandas.ROTAS["/demandas/{id}/recusar"](self.req("POST", f"/demandas/{cid}/recusar"))
        self.assertEqual(r.status, 303)
        self.assertEqual(self.banco.classificacao(cid)["decisao"], "recusado")
        self.assertEqual(self.ff.patches, [{"id": item["id"], "corpo": {"status": "cancelado"}}])
        self.assertEqual(self.ff.itens[item["id"]]["confirmado"], 0)
        t = texto(demandas.ROTAS["/demandas"](self.req("GET", "/demandas", query={"recusado": "1"})))
        self.assertIn("o item foi cancelado no Frentes (dá para reabrir lá)", t)

    def test_nada_a_confirmar_recusar_desfaz_o_lancado_e_subir_agora_so_no_provisorio(self):   # D-58 (+ D-57)
        item = self.ff.semear_item(texto="WA Ana: item alto", origem="whatsapp")
        alta = self.classificar(msg_id="alta", confianca="alta", resumo="item alto", item_id=item["id"])   # já no Frentes
        item_b = self.ff.semear_item(texto="WA Ana: item baixo", origem="whatsapp")
        baixa = self.classificar(msg_id="baixa", confianca="baixa", resumo="item baixo", item_id=item_b["id"])   # idem, IA em dúvida
        prov = self.classificar(msg_id="prov", confianca="baixa", resumo="item provisorio")                # sobe às 21:00
        nota = self.classificar(msg_id="nota", classe="lembrando", confianca="baixa", resumo="cobrou", item_id=item["id"])
        get = lambda i: texto(demandas.ROTAS["/demandas"](self.req("GET", "/demandas", query={"item": str(i)})))
        for i in (baixa, alta):                      # lançado pela IA, com qualquer confiança: só dá para desfazer
            t = get(i)
            self.assertNotIn(f"/demandas/{i}/confirmar", t)
            self.assertIn(f"/demandas/{i}/recusar", t)
            self.assertIn("No Frentes", t)
            self.assertNotIn("a confirmar", t)
            self.assertNotIn(">Confirmar</button>", t)
            self.assertIn("4 demandas, 1 sobe às 21:00", t)
        self.assertIn("A IA lançou com confiança baixa", get(baixa))
        self.assertNotIn("confiança baixa", get(alta))
        t = get(nota)                                # lembrando: o item é de outro cartão, nada a recusar aqui
        self.assertNotIn(f"/demandas/{nota}/recusar", t)
        self.assertEqual(demandas.ROTAS["/demandas/{id}/recusar"](self.req("POST", f"/demandas/{nota}/recusar")).status, 409)
        self.assertEqual(self.ff.itens[item["id"]]["status"], "aberto")
        t = get(prov)
        self.assertIn(f"/demandas/{prov}/confirmar", t)
        self.assertIn(">Subir agora</button>", t)
        self.assertIn(">provisório</span>", t)
        self.assertIn("Sobe para o Frentes às 21:00", t)
        self.assertIn("Sobe o cartão nesta frente agora", t)   # pop-up Frente também no provisório
        r = demandas.ROTAS["/demandas/{id}/recusar"](self.req("POST", f"/demandas/{alta}/recusar"))   # desfaz o que a IA lançou
        self.assertEqual(r.status, 303)
        self.assertEqual(self.banco.classificacao(alta)["decisao"], "recusado")
        self.assertEqual(self.ff.itens[item["id"]]["status"], "cancelado")
        r = demandas.ROTAS["/demandas/{id}/desfazer"](self.req("POST", f"/demandas/{alta}/desfazer"))  # e o desfazer reabre
        self.assertEqual((r.status, self.ff.itens[item["id"]]["status"]), (303, "aberto"))

    def test_subir_agora_lanca_no_frentes_confirmado_e_grava_decisao(self):   # D-57
        cid = self.classificar(confianca="alta", frente_id=1, resumo="pede orçamento")
        r = demandas.ROTAS["/demandas/{id}/confirmar"](self.req("POST", f"/demandas/{cid}/confirmar"))
        self.assertEqual(r.status, 303)
        self.assertEqual(len(self.ff.capturas), 1)
        cap = self.ff.capturas[0]
        self.assertEqual((cap["frente_id"], cap["tipo"], cap["prioridade"], cap["texto"]), (1, "tarefa", 2, "WA Ana: pede orçamento"))
        reg = self.banco.classificacao(cid)
        self.assertEqual(reg["decisao"], "confirmado")
        self.assertIsNotNone(reg["item_id"])
        self.assertIsNotNone(reg["lancado_em"])
        self.assertEqual(self.ff.itens[reg["item_id"]]["confirmado"], 1)
        self.assertEqual(self.ff.patches, [])      # confirmado pelo dono: não nasce "a confirmar"
        self.assertEqual(self.banco.provisorias(), [])
        # pelo JS (JSON): mesmo com confiança baixa, confirmar sobe confirmado e avisa `lancado`
        cid2 = self.classificar(msg_id="m2", confianca="baixa")
        r = demandas.ROTAS["/demandas/{id}/confirmar"](self.req("POST", f"/demandas/{cid2}/confirmar", cab={"X-Vigia-Auto": "1"}))
        j = json.loads(texto(r))
        self.assertEqual((r.status, j["ok"], j["decisao"], j["lancado"]), (200, True, "confirmado", True))
        self.assertEqual((len(self.ff.capturas), self.ff.patches), (2, []))
        # Frentes fora: 502 e o cartão continua provisório (sobe no fechamento)
        cid3 = self.classificar(msg_id="m3", confianca="alta")
        self.ff.parar()
        r = demandas.ROTAS["/demandas/{id}/confirmar"](self.req("POST", f"/demandas/{cid3}/confirmar"))
        self.assertEqual(r.status, 502)
        reg = self.banco.classificacao(cid3)
        self.assertEqual((reg["decisao"], reg["item_id"]), (None, None))

    def test_recusar_provisorio_nao_chama_frentes_e_desfazer_devolve(self):   # D-57
        cid = self.classificar(confianca="alta")
        r = demandas.ROTAS["/demandas/{id}/recusar"](self.req("POST", f"/demandas/{cid}/recusar"))
        self.assertEqual((r.status, dict(r.cabecalhos)["Location"]), (303, "/demandas?recusado=2"))
        self.assertEqual((self.ff.capturas, self.ff.patches), ([], []))
        self.assertEqual(self.banco.classificacao(cid)["decisao"], "recusado")
        self.assertEqual(self.banco.provisorias(), [])
        t = texto(demandas.ROTAS["/demandas"](self.req("GET", "/demandas", query={"recusado": "2"})))
        self.assertIn("não chegou a subir para o Frentes", t)
        r = demandas.ROTAS["/demandas/{id}/desfazer"](self.req("POST", f"/demandas/{cid}/desfazer"))
        self.assertEqual(r.status, 303)
        self.assertEqual(len(self.banco.provisorias()), 1)
        self.assertEqual(self.ff.patches, [])

    def test_substituidos_filtro_restaurar_e_marca_atualizado(self):   # D-57
        agora = int(time.time())
        a = self.classificar(msg_id="a", resumo="reunião 14h", ts=agora)
        b = self.classificar(msg_id="b", resumo="reunião 15h", ts=agora + 1)
        self.banco.substituir(a, b, "atualizada")
        get = lambda q: texto(demandas.ROTAS["/demandas"](self.req("GET", "/demandas", query=q)))
        lateral = lambda: json.loads(texto(conexao.ROTAS["/conexao/lateral"](self.req("GET", "/conexao/lateral"))))["demandas"]
        t = get({})
        self.assertIn(f'data-id="{b}"', t)
        self.assertNotIn(f'data-id="{a}"', t)
        self.assertIn(">atualizado</span>", t)
        self.assertIn("Antes: “reunião 14h”", t)
        self.assertIn(f"substituidos=1&amp;item={a}", t)
        self.assertEqual(lateral(), "1")
        t = get({"substituidos": "1"})
        self.assertIn(f'data-id="{a}"', t)
        self.assertNotIn(f'data-id="{b}"', t)
        self.assertIn(">substituído</span>", t)
        self.assertIn(f"/demandas/{a}/restaurar", t)
        self.assertIn("Atualizado às", t)
        self.assertIn("1 substituído", t)
        self.assertNotIn(f"/demandas/{a}/confirmar", t)
        h = demandas.ROTAS["/demandas/{id}/restaurar"]
        self.assertEqual(h(self.req("POST", f"/demandas/{b}/restaurar")).status, 409)
        self.assertEqual(h(self.req("POST", "/demandas/999/restaurar")).status, 404)
        r = h(self.req("POST", f"/demandas/{a}/restaurar"))
        self.assertEqual((r.status, dict(r.cabecalhos)["Location"]), (303, f"/demandas?restaurado=1&item={a}"))
        self.assertIsNone(self.banco.classificacao(a)["substituida_por"])
        t = get({"restaurado": "1"})
        self.assertIn(f'data-id="{a}"', t)
        self.assertIn("Restaurado. O cartão voltou às Demandas.", t)
        self.assertNotIn(">atualizado</span>", t)
        self.assertEqual(lateral(), "2")

    def test_desfazer_um_subir_agora_nao_devolve_o_item_para_a_confirmar(self):   # D-58
        cid = self.classificar(confianca="baixa", frente_id=1)
        demandas.ROTAS["/demandas/{id}/confirmar"](self.req("POST", f"/demandas/{cid}/confirmar"))
        item = self.banco.classificacao(cid)["item_id"]
        r = demandas.ROTAS["/demandas/{id}/desfazer"](self.req("POST", f"/demandas/{cid}/desfazer"))
        self.assertEqual(r.status, 303)
        self.assertEqual((self.ff.patches, self.ff.itens[item]["confirmado"]), ([], 1))
        reg = self.banco.classificacao(cid)
        self.assertEqual((reg["decisao"], reg["item_id"]), (None, item))
        t = texto(demandas.ROTAS["/demandas"](self.req("GET", "/demandas", query={"item": str(cid)})))
        self.assertIn(f"/demandas/{cid}/recusar", t)          # continua lançado: dá para Recusar
        self.assertNotIn(f"/demandas/{cid}/confirmar", t)

    def test_restaurar_cartao_cancelado_pela_ia_reabre_o_item_e_feito_fica_feito(self):   # D-58
        h = demandas.ROTAS["/demandas/{id}/restaurar"]
        casos = []
        for n, status in enumerate(("cancelado", "feito")):
            item = self.ff.semear_item(texto=f"WA Ana: reuniao {n}", origem="whatsapp", status=status)
            velho = self.classificar(msg_id=f"v{n}", classe="lembrar", item_id=item["id"], resumo=f"reuniao {n}")
            novo = self.classificar(msg_id=f"n{n}", classe="aviso", resumo=f"cancelou {n}")
            self.banco.substituir(velho, novo, "cancelada")
            casos.append((velho, item["id"]))
        (v_cancelado, i_cancelado), (v_feito, i_feito) = casos
        self.assertEqual(h(self.req("POST", f"/demandas/{v_cancelado}/restaurar")).status, 303)
        self.assertEqual(self.ff.itens[i_cancelado]["status"], "aberto")
        self.assertEqual(h(self.req("POST", f"/demandas/{v_feito}/restaurar")).status, 303)
        self.assertEqual(self.ff.itens[i_feito]["status"], "feito")
        self.assertEqual(self.ff.patches, [{"id": i_cancelado, "corpo": {"status": "aberto"}}])
        outro = self.ff.semear_item(texto="WA Ana: z", origem="whatsapp", status="cancelado")
        v = self.classificar(msg_id="vz", classe="lembrar", item_id=outro["id"])
        self.banco.substituir(v, self.classificar(msg_id="nz", classe="aviso"), "cancelada")
        self.ff.parar()
        self.assertEqual(h(self.req("POST", f"/demandas/{v}/restaurar")).status, 502)   # Frentes fora: nada gravado
        self.assertIsNotNone(self.banco.classificacao(v)["substituida_por"])

    def test_frente_no_provisorio_sobe_na_frente_escolhida_e_confirma(self):   # D-57
        cid = self.classificar(confianca="alta")
        r = demandas.ROTAS["/demandas/{id}/frente"](self.req("POST", f"/demandas/{cid}/frente", corpo={"frente": "2"}))
        self.assertEqual(r.status, 303)
        self.assertEqual(self.ff.capturas[0]["frente_id"], 2)
        reg = self.banco.classificacao(cid)
        self.assertEqual((reg["frente_id"], reg["decisao"]), (2, "confirmado"))
        self.assertIsNotNone(reg["item_id"])
        self.assertEqual(self.ff.itens[reg["item_id"]]["frente_id"], 2)
        self.assertEqual(self.banco.vinculo(reg["conversa"]), {"frente_id": 2, "origem": "dono"})
        av = self.classificar(msg_id="m2", classe="aviso")   # sem item e sem ser provisório: 404 como antes
        self.assertEqual(demandas.ROTAS["/demandas/{id}/frente"](self.req("POST", f"/demandas/{av}/frente", corpo={"frente": "2"})).status, 404)

    def test_rotular_provisorio_com_classe_que_vira_item_sobe_com_essa_classe(self):   # D-57
        cid = self.classificar(classe="demanda", confianca="alta")
        r = demandas.ROTAS["/demandas/{id}/curar"](self.req("POST", f"/demandas/{cid}/curar", corpo={"classe": "atencao"}))
        self.assertEqual(r.status, 303)
        self.assertEqual((self.ff.capturas[0]["tipo"], self.ff.capturas[0]["prioridade"]), ("tarefa", 1))
        reg = self.banco.classificacao(cid)
        self.assertEqual((reg["classe_curada"], reg["decisao"]), ("atencao", "confirmado"))
        self.assertIsNotNone(reg["item_id"])
        cid2 = self.classificar(msg_id="m2")          # rotular como aviso não sobe nada e tira o cartão dos provisórios
        demandas.ROTAS["/demandas/{id}/curar"](self.req("POST", f"/demandas/{cid2}/curar", corpo={"classe": "aviso"}))
        self.assertEqual(len(self.ff.capturas), 1)
        self.assertEqual(self.banco.provisorias(), [])

    def test_cartao_segurado_pela_ia_mostra_a_marca_e_subir_assim_mesmo_sobe(self):   # D-58
        cid = self.classificar(classe="demanda", confianca="alta", resumo="perguntou do video")
        self.banco.rebaixar(cid, "aviso", "o dono ja respondeu na conversa")
        t = texto(demandas.ROTAS["/demandas"](self.req("GET", "/demandas", query={"item": str(cid)})))
        self.assertIn('<span class="pilula neutra segurado">segurado</span>', t)
        self.assertIn("A IA segurou no fechamento do dia (era demanda): o dono ja respondeu na conversa", t)
        self.assertIn('<span class="meta-linha">Não subiu</span>', t)
        self.assertRegex(t, rf'<button type="submit" class="btn" form="f-classe-{cid}" name="classe" value="demanda"[^>]*>Subir assim mesmo</button>')
        self.assertNotIn(f'action="/demandas/{cid}/recusar"', t)        # não há o que recusar: não subiu nem vai subir
        self.assertNotIn(f'action="/demandas/{cid}/confirmar"', t)
        self.assertEqual((self.ff.capturas, self.banco.provisorias()), ([], []))
        r = demandas.ROTAS["/demandas/{id}/curar"](self.req("POST", f"/demandas/{cid}/curar", corpo={"classe": "demanda"}))
        self.assertEqual(r.status, 303)
        reg = self.banco.classificacao(cid)
        self.assertEqual((reg["classe"], reg["classe_original"], reg["classe_curada"], reg["decisao"]),
                         ("aviso", "demanda", "demanda", "confirmado"))
        self.assertEqual((len(self.ff.capturas), self.ff.itens[reg["item_id"]]["confirmado"]), (1, 1))
        t = texto(demandas.ROTAS["/demandas"](self.req("GET", "/demandas", query={"item": str(cid)})))
        self.assertNotIn("pilula neutra segurado", t)
        self.assertNotIn("A IA segurou", t)
        self.assertIn('<span class="meta-linha">No Frentes</span>', t)
        with self.assertRaises(ValueError):                              # rotulado por ele: a IA não segura de novo
            self.banco.rebaixar(cid, "sem_acao", "x")
        self.assertEqual(self.banco.classificacao(cid)["classe_curada"], "demanda")

    def test_aviso_que_o_dono_rotula_como_demanda_sobe_na_hora(self):   # D-58: quem decide que vira item é ele
        cid = self.classificar(classe="aviso")
        r = demandas.ROTAS["/demandas/{id}/curar"](self.req("POST", f"/demandas/{cid}/curar", corpo={"classe": "demanda"}))
        self.assertEqual(r.status, 303)
        reg = self.banco.classificacao(cid)
        self.assertEqual((reg["classe"], reg["classe_curada"], reg["decisao"]), ("aviso", "demanda", "confirmado"))
        self.assertIsNotNone(reg["item_id"])
        self.assertEqual(len(self.ff.capturas), 1)

    def test_confirmar_inexistente_404_e_frentes_fora_nao_grava(self):
        h = demandas.ROTAS["/demandas/{id}/confirmar"]
        self.assertEqual(h(self.req("POST", "/demandas/999/confirmar")).status, 404)
        cid = self.classificar(confianca="baixa")          # provisório: "Subir agora" precisa do Frentes
        self.ff.parar()
        self.assertEqual(h(self.req("POST", f"/demandas/{cid}/confirmar")).status, 502)
        self.assertIsNone(self.banco.classificacao(cid)["decisao"])
        self.assertEqual(len(self.banco.provisorias()), 1)

    def test_miniatura_serve_so_de_dentro_da_caixa(self):
        lote = self.dados / "caixa" / "2026-11-06_1000" / "ana"
        lote.mkdir(parents=True)
        (lote / "foto.jpg").write_bytes(imagem_jpeg_minima())
        (lote / "mensagens.json").write_text(json.dumps({"m1": {"conversa": "x"}}), encoding="utf-8")
        fora = Path(self.tmp.name) / "segredo.jpg"
        fora.write_bytes(imagem_jpeg_minima())
        ok = self.classificar(msg_id="m1", imagem="foto.jpg")
        trav = self.classificar(msg_id="m2", imagem="../../../../segredo.jpg")
        abs_fora = self.classificar(msg_id="m3", imagem=str(fora))
        abs_dentro = self.classificar(msg_id="m4", imagem=str(lote / "foto.jpg"))
        get = lambda i: demandas.ROTAS["/demandas/miniatura"](self.req("GET", "/demandas/miniatura", query={"id": str(i)}))
        r = get(ok)
        self.assertEqual((r.status, r.tipo), (200, "image/jpeg"))
        self.assertEqual(r.corpo, imagem_jpeg_minima())
        self.assertEqual(get(trav).status, 404)
        # manifesto com `lote`: a classificação que o traz procura direto na pasta do lote
        outro = self.dados / "caixa" / "2026-11-05_0900" / "bia"
        outro.mkdir(parents=True)
        (outro / "foto.jpg").write_bytes(imagem_jpeg_minima() + b"\x00")
        (outro / "mensagens.json").write_text(json.dumps({"m9": {"conversa": "y", "lote": "2026-11-05_0900"}}), encoding="utf-8")
        nove = self.classificar(msg_id="m9", imagem="foto.jpg")
        self.assertEqual(get(nove).corpo, imagem_jpeg_minima() + b"\x00")
        self.assertEqual(demandas._achar_imagem(self.banco, {"msg_id": "m9", "imagem": "foto.jpg",
                                                              "lote": "2026-11-05_0900"},
                                                 self.dados / "caixa").parent, outro.resolve())
        # lote com barra não vira caminho: cai na busca geral e continua dentro da caixa
        self.assertEqual(demandas._achar_imagem(self.banco, {"msg_id": "m9", "imagem": "foto.jpg",
                                                              "lote": "../x"}, self.dados / "caixa").parent, outro.resolve())
        self.assertEqual(get(abs_fora).status, 404)
        self.assertEqual(get(abs_dentro).status, 200)
        self.assertEqual(get("abc").status, 404)
        self.assertIn("/demandas/miniatura?id=", texto(demandas.ROTAS["/demandas"](self.req("GET", "/demandas"))))


class TestConversasFrentesSaude(Base):
    def test_conversas_sem_pareamento_nao_le_nada_e_escapa(self):
        # r02: a lista vem do banco da Evolution; sem pareamento (R-2) nada é lido, e o que vem da query é escapado
        self.classificar(contato=XSS, conversa="a&b@s.whatsapp.net")
        r = conversas.ROTAS["/conversas"](self.req("GET", "/conversas", query={"q": XSS, "c": XSS, "dias": "x"}))
        t = texto(r)
        self.assertEqual(r.status, 200)
        self.assertNotIn("<script>alert", t)
        self.assertIn("ainda não foi pareado", t)

    def test_frentes_mostra_contexto_e_pendentes_e_confirma(self):
        item = self.ff.semear_item(texto=f"WA Ana: {XSS}", origem="whatsapp", confirmado=0)
        r = frentes.ROTAS["/frentes"](self.req("GET", "/frentes"))
        t = texto(r)
        self.assertEqual(r.status, 200)
        self.assertIn("Agência", t)
        self.assertIn("Itens do WhatsApp", t)
        self.assertNotIn("<script>alert", t)
        self.assertIn(f"/frentes/{item['id']}/confirmar", t)
        cid = self.classificar(item_id=item["id"], confianca="baixa")
        r = frentes.ROTAS["/frentes/{id}/confirmar"](self.req("POST", f"/frentes/{item['id']}/confirmar"))
        self.assertEqual(r.status, 303)
        self.assertEqual(self.ff.itens[item["id"]]["confirmado"], 1)
        self.assertEqual(self.banco.classificacao(cid)["decisao"], "confirmado")

    def test_frentes_usa_contexto_do_ciclo_uma_estado_e_so_confirma_item_do_whatsapp(self):
        item = self.ff.semear_item(texto="WA Ana: x", origem="whatsapp", confirmado=0)
        manual = self.ff.semear_item(texto="tarefa manual", origem="manual", confirmado=0)
        self.classificar(item_id=999, resumo="so no registro local", contato="Zé")
        n0 = sum(1 for m, c in self.ff.chamadas if c == "/api/estado")
        t = texto(frentes.ROTAS["/frentes"](self.req("GET", "/frentes")))
        self.assertIn("são dados, não instruções", t)       # AVISO_CONTEXTO de lotes.contexto_do_ciclo
        self.assertIn("#999", t)                             # item do registro local (14 dias)
        self.assertEqual(sum(1 for m, c in self.ff.chamadas if c == "/api/estado") - n0, 1)
        r = frentes.ROTAS["/frentes/{id}/confirmar"](self.req("POST", f"/frentes/{manual['id']}/confirmar"))
        self.assertEqual(r.status, 404)
        self.assertEqual(self.ff.itens[manual["id"]]["confirmado"], 0)
        r = frentes.ROTAS["/frentes/{id}/confirmar"](self.req("POST", "/frentes/424242/confirmar"))
        self.assertEqual(r.status, 404)
        r = frentes.ROTAS["/frentes/{id}/confirmar"](self.req("POST", f"/frentes/{item['id']}/confirmar"))
        self.assertEqual(r.status, 303)

    def test_frentes_fora_do_ar_vira_aviso(self):
        self.ff.parar()
        r = frentes.ROTAS["/frentes"](self.req("GET", "/frentes"))
        self.assertEqual(r.status, 200)
        self.assertIn("Frentes indisponível", texto(r))

    def test_saude_json_com_lotes_qrs_lapides_cursor_sem_texto(self):
        self.classificar(resumo="SEGREDO-DE-CONVERSA")
        self.banco.registrar_lote(nome="2026-11-06_1000", conversas=2, novas=5, itens=1, atencao=1, modelo="sonnet")
        self.banco.registrar_qr("clique")
        self.banco.gravar_lapide(semana="2026-W44", n_msgs=10, hash_relatorio="h", apagado_em=1762000000)
        self.cursor.avancar(7)
        self.cursor.salvar()
        bruto = texto(saude.ROTAS["/saude"](self.req("GET", "/saude")))
        d = json.loads(bruto)
        self.assertEqual(d["qrs_hoje"], 1)
        self.assertEqual(d["ultimo_lote"]["novas"], 5)
        self.assertEqual(d["lapides"][0]["semana"], "2026-W44")
        self.assertEqual(d["cursor"]["ultimo_seq"], 7)
        self.assertFalse(d["pausado"])
        self.assertNotIn("SEGREDO-DE-CONVERSA", bruto)
        self.assertEqual(self.fe.qrs_gerados, 0)


class TestEnviar(Base):
    PARA = "5592900000000"

    def post(self, corpo, token=TOKEN):
        cab = {"x-vigia-envio": token} if token is not None else {}
        return enviar.ROTAS["/enviar"](self.req("POST", "/enviar", corpo, cab=cab))

    def test_get_form_tem_campo_de_token_e_nao_guarda_nada(self):
        t = texto(enviar.ROTAS["/enviar"](self.req("GET", "/enviar")))
        self.assertIn('type="password"', t)
        # A base usa localStorage só para o modo de gravação (D-24, fora do <main>); a página de envio, nunca: o token
        # não é guardado em lugar nenhum.
        self.assertNotIn("localStorage", t[t.index("<main"):t.index("</main>")])
        self.assertNotIn(TOKEN, t)

    def test_form_desabilitado_sem_token_configurado_e_json_tratado(self):
        self.ctx.tokens.envio = ""
        t = texto(enviar.ROTAS["/enviar"](self.req("GET", "/enviar")))
        self.assertIn("token de envio não configurado", t)
        self.assertIn("disabled", t)
        self.ctx.tokens.envio = TOKEN
        t = texto(enviar.ROTAS["/enviar"](self.req("GET", "/enviar")))
        self.assertNotIn("token de envio não configurado", t)
        self.assertIn("r.ok", t)
        self.assertIn("content-type", t)

    def test_401_sem_token_e_com_token_errado_sem_enviar(self):
        self.assertEqual(self.post({"para": self.PARA, "texto": "oi"}, None).status, 401)
        self.assertEqual(self.post({"para": self.PARA, "texto": "oi"}, "errado").status, 401)
        self.ctx.tokens.envio = ""
        self.assertEqual(self.post({"para": self.PARA, "texto": "oi"}, "").status, 401)
        self.assertEqual(self.fe.enviadas, [])

    def test_400_numero_fora_do_padrao_ou_texto_vazio(self):
        for para in ("5592900000", "559290000000a", "+5592900000000", "5592900000000 ", 5592900000000, None):
            self.assertEqual(self.post({"para": para, "texto": "oi"}).status, 400, para)
        self.assertEqual(self.post({"para": self.PARA, "texto": "  "}).status, 400)
        self.assertEqual(self.post({"para": self.PARA, "texto": "x" * 4001}).status, 400)
        self.assertEqual(self.fe.enviadas, [])

    def test_ordem_401_antes_de_400(self):
        self.assertEqual(self.post({"para": "x", "texto": ""}, None).status, 401)

    def test_envio_ok_registra_so_o_hash(self):
        r = self.post({"para": self.PARA, "texto": "oi Dono"})
        self.assertEqual(r.status, 200)
        self.assertEqual(json.loads(texto(r))["enviadas"], 1)
        self.assertEqual(self.fe.enviadas[0]["number"], self.PARA)
        reg = self.banco._ler("SELECT * FROM envios")
        self.assertEqual(len(reg), 1)
        self.assertEqual(reg[0]["hash_texto"], hashlib.sha256("oi Dono".encode()).hexdigest())
        self.assertEqual(reg[0]["por"], "api:painel")   # D-44: /enviar passa pela fila
        self.assertNotIn("oi Dono", json.dumps(reg))

    def test_envio_incerto_registra_e_nao_convida_a_reenviar(self):
        def incerto(numero, texto):
            raise EnvioIncerto("pode ter sido enviada")
        self.ctx.evo = SimpleNamespace(enviar_texto=incerto)
        r = self.post({"para": self.PARA, "texto": "oi"})
        d = json.loads(texto(r))
        self.assertEqual(r.status, 202)
        self.assertTrue(d["incerto"])
        self.assertNotIn("tente de novo novamente", d["mensagem"])
        self.assertEqual(self.banco._ler("SELECT por FROM envios")[0]["por"], "api:painel:incerto")

    def test_evolution_fora_502_sem_registro(self):
        self.fe.parar()
        r = self.post({"para": self.PARA, "texto": "oi"})
        self.assertEqual(r.status, 502)
        self.assertEqual(self.banco._ler("SELECT * FROM envios"), [])


class TestTemplates(unittest.TestCase):
    def test_sem_cdn_sem_polling_de_qr_em_nenhum_template(self):
        import re
        for arq in sorted((Path(__file__).resolve().parents[1] / "app" / "templates").glob("*.html")):
            t = arq.read_text(encoding="utf-8")
            self.assertNotRegex(t, r"https?://", arq.name)
            # o único relógio permitido é o da lateral (pedido 43: indicador + contador a cada 30 s); QR nunca é polled
            self.assertEqual(re.findall(r"setInterval\([^)]*\)", t),
                             ["setInterval(atualizarLateral, 30000)"] if arq.name == "_base.html" else [], arq.name)
            self.assertNotRegex(t, r"fetch\(\s*['\"]/conexao/qr", arq.name)
            self.assertNotRegex(t, r"<(?:link|script)[^>]+(?:src|href)=['\"]https?:", arq.name)


class TestRotulagemFaseA(Base):
    """D-45: /demandas/{id}/curar grava a classe certa; a linha mostra o select (não rotulada) ou o veredito."""

    def test_item_aberto_tem_popup_classe_com_a_prevista_marcada(self):
        cid = self.classificar(classe="demanda")
        t = texto(demandas.ROTAS["/demandas"](self.req("GET", "/demandas", query={"classe": "demanda"})))
        self.assertIn(f'action="/demandas/{cid}/curar"', t)
        # Papel (ds05): pop-up Classe no rodapé do item aberto; a prevista vem marcada (✓) e o valor mostra ela
        self.assertIn(f'form="f-classe-{cid}" name="classe" value="demanda"', t)
        self.assertRegex(t, r'<button type="submit" role="menuitem" class="on" form="f-classe-\d+" name="classe" value="demanda"')
        self.assertIn('<span class="valor-rotulo">Classe</span><span class="valor">demanda</span>', t)
        self.assertIn('name="volta" value="classe=demanda"', t)

    def test_curar_grava_e_volta_para_o_filtro(self):
        cid = self.classificar(classe="demanda")
        r = demandas.ROTAS["/demandas/{id}/curar"](self.req("POST", f"/demandas/{cid}/curar",
                                                            corpo={"classe": "aviso", "volta": "classe=demanda&dia=2026-11-07"}))
        self.assertEqual((r.status, dict(r.cabecalhos).get("Location")), (303, "/demandas?classe=demanda&dia=2026-11-07"))
        reg = self.banco.classificacao(cid)
        self.assertEqual((reg["classe"], reg["classe_curada"]), ("demanda", "aviso"))
        self.assertTrue(reg["curado_em"])
        self.assertIsNone(reg["decisao"])   # rotular não é confirmar/recusar
        t = texto(demandas.ROTAS["/demandas"](self.req("GET", "/demandas", query={"classe": "demanda"})))
        self.assertIn("✗ era aviso", t)   # rótulo da classe como o selo mostra (ROTULO_CLASSE só traduz atencao/sem_acao)
        self.assertIn(f'action="/demandas/{cid}/curar"', t)   # Papel: o pop-up fica (o último rótulo vence)
        self.assertIn('<span class="valor-rotulo">Classe</span><span class="valor">aviso</span>', t)

    def test_curar_certa_mostra_check_e_volta_suja_e_descartada(self):
        cid = self.classificar(classe="lembrar")
        r = demandas.ROTAS["/demandas/{id}/curar"](self.req("POST", f"/demandas/{cid}/curar",
                                                            corpo={"classe": "Lembrar", "volta": "classe=x\" onload=\"alert(1)"}))
        self.assertEqual((r.status, dict(r.cabecalhos).get("Location")), (303, "/demandas"))
        self.assertEqual(self.banco.classificacao(cid)["classe_curada"], "lembrar")
        t = texto(demandas.ROTAS["/demandas"](self.req("GET", "/demandas", query={"classe": "lembrar"})))
        self.assertIn("✓ certa", t)

    def test_curar_recusa_classe_invalida_e_id_inexistente(self):
        cid = self.classificar()
        r = demandas.ROTAS["/demandas/{id}/curar"](self.req("POST", f"/demandas/{cid}/curar", corpo={"classe": "urgente"}))
        self.assertEqual(r.status, 400)
        self.assertIsNone(self.banco.classificacao(cid)["classe_curada"])
        r = demandas.ROTAS["/demandas/{id}/curar"](self.req("POST", "/demandas/999/curar", corpo={"classe": "aviso"}))
        self.assertEqual(r.status, 404)
        r = demandas.ROTAS["/demandas/{id}/curar"](self.req("GET", f"/demandas/{cid}/curar"))
        self.assertEqual(r.status, 405)


class TestMetricasFaseA(Base):
    """D-45: /metricas junta precisão (rótulos), custo/duração dos ciclos, quedas, QRs, envios e itens do Frentes."""

    def semear(self):
        a = self.classificar(msg_id="a", classe="demanda", resumo="orçamento sigiloso 7731", contato="Fulano Sigiloso")
        b = self.classificar(msg_id="b", classe="aviso")
        self.classificar(msg_id="c", classe="sem_acao")
        self.banco.curar(a, "demanda")   # acerto
        self.banco.curar(b, "sem_acao")  # erro
        self.banco.registrar_lote(nome="l1", conversas=2, novas=3, itens=1, atencao=0, tokens_in=1000, tokens_out=200,
                                  modelo="sonnet", custo_usd=0.0456, duracao_ms=12000, turnos=3)
        self.banco.registrar_lote(nome="l2", conversas=1, novas=1, itens=0, atencao=0, tokens_in=500, tokens_out=100, modelo="sonnet")
        self.banco.registrar_evento_conexao(evento="queda", estado="close", motivo="caiu", reason_code=428)
        self.banco.registrar_evento_conexao(evento="voltou", estado="open", motivo="reconectado")
        self.banco.registrar_qr("clique")
        self.banco.registrar_envio(para="x@s.whatsapp.net", hash_texto="h", por="api:claude-code")

    def test_html_mostra_os_numeros_e_nenhum_texto_de_conversa(self):
        self.semear()
        r = metricas.ROTAS["/metricas"](self.req("GET", "/metricas"))
        self.assertEqual(r.status, 200)
        t = texto(r)
        for esperado in ("Métricas", "50%", "1 acerto em 2 rotuladas", "meta 100 rótulos", "3 classificadas",
                         "US$ 0,0456", "1.800 tokens por dia", "Quedas da conexão", "reconectado", "1 QR gerado",
                         "Linha: o que a IA previu", "/metricas?dias=30"):
            self.assertIn(esperado, t)
        for proibido in ("7731", "Sigiloso", "x@s.whatsapp.net"):
            self.assertNotIn(proibido, t)

    def test_json_tem_a_forma_e_respeita_a_janela(self):
        self.semear()
        r = metricas.ROTAS["/metricas"](self.req("GET", "/metricas", query={"formato": "json", "dias": "500"}))
        m = json.loads(texto(r))
        self.assertEqual(m["dias"], 90)
        self.assertEqual((m["precisao"]["pct"], m["precisao"]["curadas"], m["precisao"]["acertos"], m["precisao"]["total"]), (50, 2, 1, 3))
        self.assertEqual(m["precisao"]["matriz"], [{"classe": "aviso", "classe_curada": "sem_acao", "n": 1},
                                                  {"classe": "demanda", "classe_curada": "demanda", "n": 1}])
        self.assertEqual((m["ciclos"]["lotes"], m["ciclos"]["custo_usd"], m["ciclos"]["lotes_com_custo"], m["ciclos"]["duracao_ms_media"]),
                         (2, 0.0456, 1, 12000))
        self.assertEqual((m["conexao"]["quedas"], len(m["conexao"]["eventos"]), m["conexao"]["qrs"]), (1, 2, 1))
        self.assertEqual((m["envios"]["total"], m["envios"]["api"]), (1, 1))
        sem_hora = {k: v for k, v in m.items() if k != "gerado_em"}   # a hora em segundos pode conter 7731 por acaso
        self.assertNotIn("7731", json.dumps(sem_hora, ensure_ascii=False))
        m = json.loads(texto(metricas.ROTAS["/metricas"](self.req("GET", "/metricas", cab={"Accept": "application/json"}, query={"dias": "abc"}))))
        self.assertEqual(m["dias"], 14)

    def test_vazio_orienta_a_rotular(self):
        t = texto(metricas.ROTAS["/metricas"](self.req("GET", "/metricas")))
        self.assertIn("Nenhuma classificação rotulada ainda", t)
        self.assertIn("Nenhum ciclo com Claude no período", t)
        self.assertIn("Nenhuma queda registrada", t)


if __name__ == "__main__":
    unittest.main()
