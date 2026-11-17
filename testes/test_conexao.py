"""Testes da estação E8: máquina de estado do QR, teto diário, monitor (queda/428) e CLI --monitorar.
Quem chama: unittest (python3 -m unittest testes.test_conexao).
O que não faz: não dorme de verdade (relógio falso), não toca Evolution real; FakeEvolution em 127.0.0.1.
"""
import io
import json
import os
import sqlite3
from unittest import mock
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import conexao as mod  # noqa: E402
from app import lotes as lotes_mod  # noqa: E402
from app.banco import Banco  # noqa: E402
from app.cursor import HISTORICO_S, Cursor  # noqa: E402
from app.erros import EvolutionIndisponivel, QRIndisponivel, VigiaErro  # noqa: E402
from app.evolution_client import EvolutionClient  # noqa: E402
from testes.fakes import FakeEvolution  # noqa: E402
from testes.util_contrato import RelogioFalso  # noqa: E402

RAIZ = Path(__file__).resolve().parents[1]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.fake = FakeEvolution().iniciar()
        self.addCleanup(self.fake.parar)
        self.evo = EvolutionClient(self.fake.url, self.fake.instancia, self.fake.apikey)
        self.banco = Banco(str(self.dir / "vigia.db"))
        self.rel = RelogioFalso()
        self.cursor = Cursor.carregar(self.dir / "dados" / "cursor.json")
        self.arq_monitor = self.dir / "dados" / "conexao.json"
        self.desp = None            # D-54: despertar que o monitor enxerga: None, epoch (= pleno) ou (epoch, "darkwake")
        self.pausas = []
        self.c = self.nova()

    def nova(self, **kw):
        kw.setdefault("cursor", self.cursor)
        kw.setdefault("estado_arq", self.arq_monitor)
        kw.setdefault("despertar", lambda: self.desp)
        kw.setdefault("dormir", self.pausas.append)
        return mod.Conexao(self.evo, self.banco, qr_segundos=60, qr_max=3, relogio=self.rel, **kw)

    def origens(self):
        con = sqlite3.connect(self.banco.caminho)
        try:
            return [r[0] for r in con.execute("SELECT origem FROM qr_eventos ORDER BY id")]
        finally:
            con.close()

    def conexoes(self):
        return [c for c in self.fake.chamadas if c[1].startswith("/instance/connect/")]

    def logouts(self):
        return [c for c in self.fake.chamadas if c[0] == "DELETE"]


class TestMaquinaQR(Base):
    def test_ocioso_nao_gera_qr_nem_muda_com_open(self):
        for st in ("close", "connecting", "open"):
            self.fake.estado = st
            for _ in range(20):
                self.rel.avancar(5)
                r = self.c.tick()
        self.assertEqual(self.c.estado, "ocioso")
        self.assertEqual(self.fake.qrs_gerados, 0)
        self.assertEqual(self.banco.qrs_hoje(), 0)
        self.assertEqual(self.conexoes(), [])

    def test_clique_gera_exatamente_um_qr_e_registra_clique(self):
        r = self.c.iniciar_qr()
        self.assertEqual(self.fake.qrs_gerados, 1)
        self.assertEqual(self.origens(), ["clique"])
        self.assertEqual(self.c.estado, "gerando")
        self.assertTrue(r["base64"].startswith("data:image/png;base64,"))
        self.assertEqual(r["restantes"], 2)
        self.assertEqual(r["expira_em"], self.rel() + 60)
        self.assertEqual(r["qrs_hoje"], 1)

    def test_duplo_clique_com_qr_vivo_nao_gasta_outro(self):
        a = self.c.iniciar_qr()
        self.rel.avancar(20)
        b = self.c.iniciar_qr()
        self.assertEqual(self.fake.qrs_gerados, 1)
        self.assertEqual(a["base64"], b["base64"])
        self.assertEqual(self.origens(), ["clique"])

    def test_renovacoes_registram_origem_e_mudam_estado(self):
        self.c.iniciar_qr()
        self.rel.avancar(61)
        r = self.c.tick()
        self.assertEqual(self.c.estado, "renovando")
        self.assertTrue(r["qr_novo"])
        self.assertEqual(r["restantes"], 1)
        self.rel.avancar(61)
        r = self.c.tick()
        self.assertEqual(r["restantes"], 0)
        self.assertEqual(self.origens(), ["clique", "renovacao", "renovacao"])
        self.assertEqual(self.fake.qrs_gerados, 3)

    def test_tick_com_qr_vivo_nao_renova_e_nao_marca_novo(self):
        self.c.iniciar_qr()
        for _ in range(11):
            self.rel.avancar(5)
            r = self.c.tick()
            self.assertFalse(r["qr_novo"])
        self.assertEqual(self.fake.qrs_gerados, 1)
        self.assertEqual(self.c.estado, "gerando")

    def test_tres_qrs_expira_com_logout_e_nao_gera_mais(self):
        self.c.iniciar_qr()
        for _ in range(3):
            self.rel.avancar(61)
            r = self.c.tick()
        self.assertEqual(self.c.estado, "expirado")
        self.assertTrue(r["logout_confirmado"])
        self.assertEqual(len(self.logouts()), 1)
        self.assertEqual(self.fake.estado, "close")
        for _ in range(120):
            self.rel.avancar(5)
            self.c.tick()
        self.assertEqual(self.fake.qrs_gerados, 3)
        self.assertEqual(self.banco.qrs_hoje(), 3)
        self.assertEqual(self.c.estado, "expirado")
        self.assertEqual(len(self.logouts()), 1)

    def test_apos_expirar_novo_clique_recomeca_contagem(self):
        self.c.iniciar_qr()
        for _ in range(3):
            self.rel.avancar(61)
            self.c.tick()
        r = self.c.iniciar_qr()
        self.assertEqual(self.fake.qrs_gerados, 4)
        self.assertEqual(self.c.estado, "gerando")
        self.assertEqual(r["restantes"], 2)

    def test_open_no_meio_conecta_fecha_qr_e_grava_pareado_em(self):
        self.c.iniciar_qr()
        self.rel.avancar(61)
        self.c.tick()                       # renovando
        self.rel.avancar(10)
        self.fake.estado = "open"
        r = self.c.tick()
        self.assertEqual(self.c.estado, "conectado")
        self.assertEqual(r["base64"], "")
        self.assertEqual(self.cursor.pareado_em, int(self.rel()))
        self.assertEqual(Cursor.carregar(self.cursor.caminho).pareado_em, int(self.rel()))
        self.rel.avancar(600)
        self.c.tick()
        self.assertEqual(self.fake.qrs_gerados, 2)
        self.assertEqual(self.logouts(), [])

    def test_pareamento_novo_regrava_pareado_em_e_ultimo_seq_do_disco_sobrevive(self):
        """D-53 (pedido 43): QR lido = pareamento novo → pareado_em de agora (janela de 1 dia acompanha); ultimo_seq que o
        coletor gravou em outro processo não se perde. "Já conectado" sem QR não mexe no pareado_em."""
        outro = Cursor.carregar(self.cursor.caminho)    # o coletor, em outro processo
        outro.pareado_em = 1000
        outro.ultimo_seq = 77
        outro.salvar()
        self.c.iniciar_qr()
        self.fake.estado = "open"
        self.rel.avancar(5)
        self.c.tick()
        d = Cursor.carregar(self.cursor.caminho)
        self.assertEqual((d.pareado_em, d.ultimo_seq), (int(self.rel()), 77))
        self.assertEqual((self.cursor.pareado_em, self.cursor.ultimo_seq), (int(self.rel()), 77))
        self.assertEqual(d.desde, int(self.rel()) - HISTORICO_S)
        # já conectado: clique em Gerar QR com a instância open não emite QR e não regrava
        marcado = d.pareado_em
        self.rel.avancar(3600)
        self.c.estado = "ocioso"
        self.assertTrue(self.c.iniciar_qr().get("ja_conectado"))
        self.assertEqual(Cursor.carregar(self.cursor.caminho).pareado_em, marcado)

    def test_conectado_nunca_gera_qr_e_volta_a_ocioso_se_cair(self):
        self.c.iniciar_qr()
        self.fake.estado = "open"
        self.c.tick()
        self.assertEqual(self.c.estado, "conectado")
        self.fake.estado = "close"
        for _ in range(30):
            self.rel.avancar(5)
            self.c.tick()
        self.assertEqual(self.c.estado, "ocioso")
        self.assertEqual(self.fake.qrs_gerados, 1)

    def test_clique_com_instancia_ja_open_nao_gasta_qr(self):
        self.fake.estado = "open"
        r = self.c.iniciar_qr()
        self.assertEqual(self.fake.qrs_gerados, 0)
        self.assertEqual(self.c.estado, "conectado")
        self.assertTrue(r["ja_conectado"])
        self.assertEqual(self.banco.qrs_hoje(), 0)

    def test_expirado_com_open_vira_conectado_sem_qr(self):
        self.c.iniciar_qr()
        for _ in range(3):
            self.rel.avancar(61)
            self.c.tick()
        self.fake.estado = "open"
        self.c.tick()
        self.assertEqual(self.c.estado, "conectado")
        self.assertEqual(self.fake.qrs_gerados, 3)

    def test_logout_que_nao_pega_e_reenviado_ate_3_vezes(self):
        class EvoTeimosa(EvolutionClient):
            def logout(self):                    # logout "engolido": a Evolution segue em connecting
                self.chamado = getattr(self, "chamado", 0) + 1

        evo = EvoTeimosa(self.fake.url, self.fake.instancia, self.fake.apikey)
        c = mod.Conexao(evo, self.banco, qr_segundos=60, qr_max=3, relogio=self.rel, cursor=self.cursor,
                        estado_arq=self.arq_monitor)
        self.fake.estado = "connecting"
        c.iniciar_qr()
        for _ in range(3):
            self.rel.avancar(61)
            r = c.tick()
        self.assertEqual(c.estado, "expirado")
        self.assertFalse(r["logout_confirmado"])
        for _ in range(20):
            self.rel.avancar(5)
            c.tick()
        self.assertEqual(evo.chamado, mod.LOGOUT_TENTATIVAS)
        self.assertEqual(self.fake.qrs_gerados, 3)

    def test_estado_nunca_consome_qr(self):
        for _ in range(10):
            self.evo.estado()
        self.assertEqual(self.fake.qrs_gerados, 0)


class TestTeto(Base):
    def test_clique_recusado_no_teto_sem_gerar_qr(self):
        c = self.nova(teto_dia=10)
        for _ in range(10):
            self.banco.registrar_qr("clique")
        with self.assertRaises(mod.TetoQR) as cm:
            c.iniciar_qr()
        self.assertIsInstance(cm.exception, VigiaErro)
        self.assertEqual(self.fake.qrs_gerados, 0)
        self.assertEqual(self.banco.qrs_hoje(), 10)
        self.assertEqual(c.estado, "ocioso")

    def test_ate_o_teto_menos_um_ainda_gera(self):
        c = self.nova(teto_dia=10)
        for _ in range(9):
            self.banco.registrar_qr("clique")
        c.iniciar_qr()
        self.assertEqual(self.fake.qrs_gerados, 1)
        self.assertEqual(self.banco.qrs_hoje(), 10)

    def test_teto_no_meio_da_renovacao_expira_e_faz_logout(self):
        c = self.nova(teto_dia=3)
        self.banco.registrar_qr("clique")   # 1 já gasto hoje
        c.iniciar_qr()                      # 2
        self.rel.avancar(61)
        c.tick()                            # 3 (renovação), ainda < teto antes de emitir
        self.rel.avancar(61)
        r = c.tick()                        # 3 >= teto: não renova
        self.assertEqual(c.estado, "expirado")
        self.assertEqual(r["motivo"], "teto")
        self.assertEqual(self.fake.qrs_gerados, 2)
        self.assertEqual(self.banco.qrs_hoje(), 3)
        self.assertEqual(len(self.logouts()), 1)

    def test_teto_vem_do_config(self):
        c = mod.Conexao.de_config(self.evo, self.banco, {"qr": {"segundos": 30, "maximo": 2, "teto_dia": 4}},
                                  cursor=self.cursor, relogio=self.rel, estado_arq=self.arq_monitor)
        self.assertEqual((c.qr_segundos, c.qr_max, c.teto_dia), (30, 2, 4))
        c = mod.Conexao.de_config(self.evo, self.banco, {}, cursor=self.cursor, relogio=self.rel)
        self.assertEqual((c.qr_segundos, c.qr_max, c.teto_dia), (60, 3, 10))


class TestMonitor(Base):
    def test_ok_e_queda_so_na_transicao(self):
        self.fake.estado = "open"
        r = self.c.monitorar()
        self.assertEqual((r["evento"], r["avisar"]), ("ok", False))
        self.fake.estado = "close"
        r = self.c.monitorar()
        self.assertEqual((r["evento"], r["avisar"]), ("queda", True))
        for _ in range(3):
            r = self.c.monitorar()
            self.assertEqual((r["evento"], r["avisar"]), ("queda", False))
        self.assertEqual(self.fake.qrs_gerados, 0)

    def test_reconectar_e_cair_de_novo_avisa_de_novo(self):
        self.fake.estado = "open"
        self.c.monitorar()
        self.fake.estado = "close"
        self.assertTrue(self.c.monitorar()["avisar"])
        self.fake.estado = "open"
        r = self.c.monitorar()
        self.assertEqual((r["evento"], r["avisar"], r["motivo"]), ("ok", False, "reconectado"))
        self.fake.estado = "close"
        self.assertTrue(self.c.monitorar()["avisar"])

    def test_estado_persiste_entre_processos(self):
        self.fake.estado = "open"
        self.c.monitorar()
        self.fake.estado = "close"
        outra = self.nova()                    # novo processo, mesmo dados/conexao.json
        self.assertTrue(outra.monitorar()["avisar"])
        self.assertFalse(self.nova().monitorar()["avisar"])
        dados = json.loads(self.arq_monitor.read_text(encoding="utf-8"))
        self.assertEqual(dados["ultimo_estado"], "close")

    def test_arquivo_de_estado_corrompido_nao_derruba(self):
        self.arq_monitor.parent.mkdir(parents=True, exist_ok=True)
        self.arq_monitor.write_text("{não é json", encoding="utf-8")
        self.fake.estado = "open"
        self.assertEqual(self.c.monitorar()["evento"], "ok")

    def test_nunca_pareado_e_fechado_nao_e_queda(self):
        self.fake.estado = "close"
        r = self.c.monitorar()
        self.assertEqual((r["evento"], r["avisar"], r["motivo"]), ("ok", False, "nao_pareado"))

    def test_primeira_leitura_fechada_de_numero_pareado_avisa_uma_vez(self):
        self.cursor.pareado_em = 1000
        self.cursor.salvar()
        self.fake.estado = "close"
        r = self.c.monitorar()
        self.assertEqual((r["evento"], r["avisar"]), ("queda", True))
        self.assertFalse(self.c.monitorar()["avisar"])

    def test_connecting_vindo_de_open_espera_ate_10_min(self):
        self.fake.estado = "open"
        self.c.monitorar()
        self.fake.estado = "connecting"
        r = self.c.monitorar()
        self.assertEqual((r["evento"], r["avisar"], r["motivo"]), ("ok", False, "reconectando"))
        self.rel.avancar(1800)                 # próximo ciclo: segue connecting
        r = self.c.monitorar()
        self.assertEqual((r["evento"], r["avisar"]), ("queda", True))
        self.rel.avancar(1800)
        self.assertFalse(self.c.monitorar()["avisar"])

    def test_connecting_que_volta_a_open_nao_avisa(self):
        self.fake.estado = "open"
        self.c.monitorar()
        self.fake.estado = "connecting"
        self.c.monitorar()
        self.fake.estado = "open"
        r = self.c.monitorar()
        self.assertEqual((r["evento"], r["avisar"]), ("ok", False))

    def test_socket_morto_428_avisa_uma_vez_enquanto_recente(self):
        self.fake.estado = "open"
        self.c.monitorar()
        self.fake.reason_code = 428
        r = self.c.monitorar()
        self.assertEqual((r["evento"], r["avisar"], r["reason_code"]), ("socket_morto", True, 428))
        r = self.c.monitorar()
        self.assertEqual((r["evento"], r["avisar"]), ("socket_morto", False))
        self.rel.avancar(601)
        r = self.c.monitorar()                 # ainda dentro da margem da rodada anterior, mas já avisado
        self.assertEqual((r["evento"], r["avisar"]), ("socket_morto", False))
        r = self.c.monitorar()                 # a rodada anterior agora é posterior ao reason_em
        self.assertEqual((r["evento"], r["avisar"]), ("ok", False))

    def test_open_com_reason_antigo_e_ok(self):
        self.fake.estado = "open"
        self.fake.reason_code = 428
        self.fake.reason_em = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
        self.assertEqual(self.c.monitorar()["evento"], "ok")

    def test_open_sem_data_do_reason_nao_vira_alarme_falso(self):
        self.fake.estado = "open"
        self.fake.reason_code = 428
        self.fake.reason_em = None
        self.assertEqual(self.c.monitorar()["evento"], "ok")

    def test_reconexao_apos_queda_com_reason_recente_nao_e_socket_morto(self):
        self.fake.estado = "open"
        self.c.monitorar()
        self.fake.estado = "close"
        self.fake.reason_code = 408
        self.assertEqual(self.c.monitorar()["evento"], "queda")
        self.fake.estado = "open"              # reconectou sozinho; reason_code (recente) continua lá
        for _ in range(3):
            r = self.c.monitorar()
            self.assertEqual((r["evento"], r["avisar"]), ("ok", False))

    def test_repareamento_401_com_estado_open_e_volta_nao_socket_morto(self):
        """Pedido 41 (09/11 11:30): 'Parear de novo…' desloga (401) e o dono pareia entre dois ciclos; o monitor só vê
        open → open com um 401 recente e avisava 'socket morto'. É volta ('repareado'), sem aviso."""
        self.fake.estado = "open"
        self.c.monitorar()
        self.fake.reason_code = 401                  # logout + QR novo entre dois ciclos: estado continua "open"
        r = self.c.monitorar()
        self.assertEqual((r["evento"], r["avisar"], r["motivo"]), ("ok", False, "repareado"))
        for _ in range(3):
            r = self.c.monitorar()
            self.assertEqual((r["evento"], r["avisar"]), ("ok", False))
        eventos = [(e["evento"], e["estado"], e["motivo"]) for e in self.banco.eventos_conexao()]
        self.assertEqual(eventos, [("voltou", "open", "repareado")])
        self.fake.reason_code = 428                  # um 428 novo com a conexão "open" continua sendo socket morto
        r = self.c.monitorar()
        self.assertEqual((r["evento"], r["avisar"]), ("socket_morto", True))

    def test_repareamento_401_regrava_pareado_em_so_se_o_painel_nao_viu(self):   # D-53
        self.cursor.pareado_em = 1000                # pareamento antigo, anterior ao 401
        self.cursor.salvar()
        self.fake.estado = "open"
        self.c.monitorar()
        self.fake.reason_code = 401
        self.assertEqual(self.c.monitorar()["motivo"], "repareado")
        d = Cursor.carregar(self.cursor.caminho)
        self.assertEqual((d.pareado_em, self.cursor.pareado_em), (int(self.rel()), int(self.rel())))
        # o painel já marcou um pareado_em depois do 401 (QR lido pelo tick): o monitor não mexe
        depois = int(mod._para_epoch(self.fake.reason_em)) + 60
        self.cursor.pareado_em = depois
        self.cursor.salvar()
        self.fake.reason_code = 401                  # 401 novo (reason_em de agora), ainda antes do pareado_em gravado?
        novo_em = int(mod._para_epoch(self.fake.reason_em))
        self.cursor.pareado_em = novo_em + 60
        self.cursor.salvar()
        self.assertEqual(self.c.monitorar()["motivo"], "repareado")
        self.assertEqual(Cursor.carregar(self.cursor.caminho).pareado_em, novo_em + 60)

    def test_monitorar_nunca_gera_qr(self):
        for st in ("open", "close", "connecting", "close"):
            self.fake.estado = st
            self.c.monitorar()
        self.assertEqual(self.fake.qrs_gerados, 0)
        self.assertEqual(len(self.logouts()), 1)      # só o connecting sem pareamento e sem sessão de QR (D-23)

    def test_para_epoch(self):
        self.assertEqual(mod._para_epoch(None), None)
        self.assertEqual(mod._para_epoch("lixo"), None)
        self.assertEqual(mod._para_epoch(1_700_000_000), 1_700_000_000)
        self.assertEqual(mod._para_epoch(1_700_000_000_000), 1_700_000_000)
        self.assertEqual(mod._para_epoch("2026-11-06T12:00:00Z"), datetime(2026, 11, 6, 12, tzinfo=timezone.utc).timestamp())
        self.assertEqual(mod._para_epoch("2026-11-06T12:00:00"), datetime(2026, 11, 6, 12, tzinfo=timezone.utc).timestamp())

    def test_reiniciar_chama_executor_com_restart(self):
        chamadas = []
        self.c.estado = "gerando"
        self.c.reiniciar(lambda cmd, **kw: chamadas.append((cmd, kw)))
        self.assertEqual(chamadas[0][0], ["bin/evolution.sh", "restart"])
        self.assertEqual(chamadas[0][1]["cwd"], str(RAIZ))
        self.assertEqual(chamadas[0][1]["timeout"], 120)
        self.assertEqual(self.c.estado, "ocioso")
        self.assertEqual(len(self.logouts()), 1)      # qr em andamento: logout antes do restart


class TestCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.raiz = Path(self.tmp.name)
        self.fake = FakeEvolution().iniciar()
        self.addCleanup(self.fake.parar)

    def escrever_env(self, url=None, chave=None):
        (self.raiz / ".env").write_text(
            f"EVOLUTION_URL={url or self.fake.url}\nEVOLUTION_INSTANCE={self.fake.instancia}   # nome\n"
            f"EVOLUTION_INSTANCE_APIKEY={chave or self.fake.apikey}\n", encoding="utf-8")
        (self.raiz / "config.toml").write_text("[qr]\nteto_dia = 10\n", encoding="utf-8")

    def rodar(self, *args):
        saida = io.StringIO()
        antes = {k: os.environ.pop(k, None) for k in ("EVOLUTION_URL", "EVOLUTION_INSTANCE", "EVOLUTION_INSTANCE_APIKEY",
                                                      "DONO_WHATSAPP")}   # testes/__init__ põe DONO_WHATSAPP no ambiente
        try:
            codigo = mod.main(["--raiz", str(self.raiz), *args], saida=saida)
        finally:
            for k, v in antes.items():
                if v is not None:
                    os.environ[k] = v
        return codigo, saida.getvalue()

    def test_monitorar_ok_imprime_json_exit_0(self):
        self.escrever_env()
        self.fake.estado = "open"
        codigo, txt = self.rodar("--monitorar")
        self.assertEqual(codigo, 0)
        r = json.loads(txt)
        self.assertEqual((r["evento"], r["avisar"]), ("ok", False))
        self.assertTrue((self.raiz / "dados" / "conexao.json").exists())

    def test_B4_monitorar_respeita_vigia_dados(self):
        self.escrever_env()
        self.fake.estado = "open"
        outro = self.raiz / "outros-dados"
        os.environ["VIGIA_DADOS"] = str(outro)
        self.addCleanup(os.environ.pop, "VIGIA_DADOS", None)
        codigo, _ = self.rodar("--monitorar")
        self.assertEqual(codigo, 0)
        self.assertTrue((outro / "conexao.json").exists())
        self.assertFalse((self.raiz / "dados" / "conexao.json").exists())

    def test_B4_dados_dir_precedencia_env_config_padrao(self):
        self.assertEqual(mod.dados_dir("/r", {}), Path("/r/dados"))
        self.assertEqual(mod.dados_dir("/r", {"dados_dir": "/c"}), Path("/c"))
        os.environ["VIGIA_DADOS"] = "/e"
        self.addCleanup(os.environ.pop, "VIGIA_DADOS", None)
        self.assertEqual(mod.dados_dir("/r", {"dados_dir": "/c"}), Path("/e"))

    def test_monitorar_queda_entre_execucoes_avisa_uma_vez(self):
        self.escrever_env()
        self.fake.estado = "open"
        self.rodar("--monitorar")
        self.fake.estado = "close"
        r1 = json.loads(self.rodar("--monitorar")[1])
        r2 = json.loads(self.rodar("--monitorar")[1])
        self.assertEqual((r1["evento"], r1["avisar"]), ("queda", True))
        self.assertEqual((r2["evento"], r2["avisar"]), ("queda", False))
        self.assertEqual(self.fake.qrs_gerados, 0)

    def test_evolution_fora_do_ar_exit_1(self):
        self.escrever_env(url="http://127.0.0.1:1")
        codigo, txt = self.rodar("--monitorar")
        self.assertEqual(codigo, 1)
        self.assertEqual(json.loads(txt)["evento"], "erro")

    def test_apikey_errada_exit_1(self):
        self.escrever_env(chave="errada")
        codigo, txt = self.rodar("--monitorar")
        self.assertEqual(codigo, 1)
        self.assertEqual(json.loads(txt)["erro"], "NaoAutorizado")
        self.assertNotIn("errada", txt)

    def test_sem_env_exit_2(self):
        codigo, txt = self.rodar("--monitorar")
        self.assertEqual(codigo, 2)
        self.assertEqual(json.loads(txt)["evento"], "nao_rodou")

    def test_sem_flag_nao_roda_nada(self):
        self.escrever_env()
        codigo, _ = self.rodar()
        self.assertEqual(codigo, 2)
        self.assertEqual(self.fake.chamadas, [])

    def test_python_dash_m_real(self):
        self.escrever_env()
        self.fake.estado = "open"
        env = {k: v for k, v in os.environ.items() if not k.startswith("EVOLUTION_")}
        env["PYTHONPATH"] = str(RAIZ)
        p = subprocess.run([sys.executable, "-m", "app.conexao", "--monitorar", "--raiz", str(self.raiz)],
                           capture_output=True, text=True, env=env, cwd=self.tmp.name, stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["evento"], "ok")


class _EvoFalha(EvolutionClient):
    """EvolutionClient com falhas injetáveis; as chamadas boas vão ao FakeEvolution de verdade."""
    gerar_apos_get = None     # exceção levantada DEPOIS do GET /instance/connect real
    gerar_a_partir_de = 2     # nº da chamada de gerar_qr a partir da qual falha
    logout_erro = None
    n_gerar = 0

    def gerar_qr(self):
        self.n_gerar += 1
        if self.gerar_apos_get is not None and self.n_gerar >= self.gerar_a_partir_de:
            super().gerar_qr()
            raise self.gerar_apos_get
        return super().gerar_qr()

    def logout(self):
        if self.logout_erro is not None:
            raise self.logout_erro
        return super().logout()


class TestCorrecoesF4(Base):
    def setUp(self):
        super().setUp()
        self.evo = _EvoFalha(self.fake.url, self.fake.instancia, self.fake.apikey)
        self.c = self.nova()

    def arq(self):
        return json.loads(self.arq_monitor.read_text(encoding="utf-8"))

    def test_A_tick_atrasado_expira_sem_renovar(self):
        self.c.iniciar_qr()
        self.rel.avancar(3600)
        self.fake.estado = "close"
        r = self.c.tick()
        self.assertEqual(self.c.estado, "expirado")
        self.assertEqual(r["motivo"], "tick_atrasado")
        self.assertEqual(len(self.logouts()), 1)
        self.assertEqual(self.banco.qrs_hoje(), 1)
        self.assertEqual(len(self.conexoes()), 1)       # só o GET do clique

    def test_A2_tick_com_atraso_pequeno_ainda_renova(self):
        self.c.iniciar_qr()
        self.rel.avancar(60 + 10)
        self.c.tick()
        self.assertEqual(self.c.estado, "renovando")

    def _esgota(self):
        self.rel.avancar(61)
        for _ in range(200):
            self.rel.avancar(5)
            self.c.tick()

    def test_B_gerar_qr_falha_na_renovacao_limita_tentativas_e_faz_logout(self):
        self.evo.gerar_apos_get = QRIndisponivel("sem qr")
        self.c.iniciar_qr()
        self._esgota()
        self.assertEqual(self.c.estado, "expirado")
        self.assertLessEqual(len(self.conexoes()), self.c.qr_max)
        self.assertEqual(len(self.logouts()), 1)

    def test_C_registrar_qr_falha_na_renovacao(self):
        self.c.iniciar_qr()
        original = self.banco.registrar_qr

        def quebrado(origem):
            raise sqlite3.OperationalError("database is locked")
        self.banco.registrar_qr = quebrado
        self.addCleanup(setattr, self.banco, "registrar_qr", original)
        self._esgota()
        self.assertEqual(self.c.estado, "expirado")
        self.assertLessEqual(len(self.conexoes()), self.c.qr_max)
        self.assertEqual(len(self.logouts()), 1)

    def test_C2_falha_com_estado_open_conecta(self):
        class Aberto(QRIndisponivel):
            state = "open"
        self.evo.gerar_apos_get = Aberto("ja conectada")
        self.c.iniciar_qr()
        self.fake.estado = "connecting"
        self.rel.avancar(61)
        self.fake.estado = "open"                         # abriu entre o estado() do tick e o connect
        orig = self.evo.estado
        seq = iter(["connecting"])
        self.evo.estado = lambda: {"state": next(seq, "open"), "reason_code": None, "reason_em": None}
        self.addCleanup(setattr, self.evo, "estado", orig)
        self.c.tick()
        self.assertEqual(self.c.estado, "conectado")
        self.assertEqual(self.logouts(), [])

    def test_D_excecao_no_clique_depois_do_get_nao_volta_a_ocioso(self):
        self.fake.estado = "connecting"
        self.evo.gerar_apos_get = QRIndisponivel("sem qr")
        self.evo.gerar_a_partir_de = 1
        with self.assertRaises(QRIndisponivel):
            self.c.iniciar_qr()
        self.assertEqual(self.c.estado, "gerando")
        self.assertEqual(self.logouts(), [])
        self.rel.avancar(31)
        self.c.tick()
        self.assertEqual(self.c.estado, "expirado")
        self.assertEqual(len(self.logouts()), 1)
        self.assertEqual(self.fake.estado, "close")

    def test_E_reiniciar_com_qr_em_andamento_faz_logout_antes_do_restart(self):
        self.c.iniciar_qr()
        ordem = []
        self.c.reiniciar(lambda cmd, **kw: ordem.append(("restart", len([x for x in self.fake.chamadas if x[0] == "DELETE"]))))
        self.assertEqual(ordem, [("restart", 1)])
        self.assertEqual(self.c.estado, "ocioso")
        self.assertIn("reiniciado_em", self.arq())

    def test_E2_executor_com_codigo_de_saida_ruim_levanta(self):
        class R:
            returncode = 3
        with self.assertRaises(VigiaErro):
            self.c.reiniciar(lambda cmd, cwd=None: R())

    def test_E3_executor_sem_timeout_na_assinatura_funciona(self):
        self.assertEqual(self.c.reiniciar(lambda cmd, cwd=None: None), 0)

    def test_F_logout_que_falha_fica_pendente_e_o_monitor_resolve(self):
        self.fake.estado = "connecting"
        self.evo.logout_erro = EvolutionIndisponivel("fora")
        self.c.iniciar_qr()
        self.rel.avancar(3600)
        r = self.c.tick()
        self.assertFalse(r["logout_confirmado"])
        self.assertTrue(self.arq()["logout_pendente"])
        self.evo.logout_erro = None
        self.rel.avancar(1800)
        self.c.monitorar()
        self.assertEqual(len(self.logouts()), 1)
        self.assertFalse(self.arq()["logout_pendente"])

    def test_F2_pendente_com_open_so_limpa_a_flag(self):
        self.arq_monitor.parent.mkdir(parents=True, exist_ok=True)
        self.arq_monitor.write_text(json.dumps({"logout_pendente": True}), encoding="utf-8")
        self.fake.estado = "open"
        self.c.monitorar()
        self.assertEqual(self.logouts(), [])
        self.assertFalse(self.arq()["logout_pendente"])

    def test_G_nunca_pareado_em_connecting_sem_sessao_de_qr_faz_logout(self):
        self.fake.estado = "connecting"
        r = self.c.monitorar()
        self.assertEqual(r["evento"], "logout_sem_pareamento")
        self.assertEqual(len(self.logouts()), 1)
        self.assertEqual(self.arq()["evento"], "logout_sem_pareamento")

    def test_G_com_sessao_de_qr_ativa_nao_faz_logout(self):
        self.fake.estado = "connecting"
        self.c.iniciar_qr()                               # grava qr_ate no futuro
        self.rel.avancar(30)
        r = self.c.monitorar()
        self.assertNotEqual(r["evento"], "logout_sem_pareamento")
        self.assertEqual(self.logouts(), [])
        self.assertGreater(self.arq()["qr_ate"], self.rel())

    def test_G_sessao_de_qr_vencida_ha_mais_de_60s_libera_o_logout(self):
        self.fake.estado = "connecting"
        self.c.iniciar_qr()
        self.rel.avancar(60 * 3 + 15 + 61)
        self.assertEqual(self.c.monitorar()["evento"], "logout_sem_pareamento")

    def test_G2_pareado_em_connecting_repete_aviso_sem_logout(self):
        self.cursor.pareado_em = 1000
        self.cursor.salvar()
        self.fake.estado = "open"
        self.c.monitorar()
        self.fake.estado = "connecting"
        avisos = []
        for _ in range(6):                                # 3 h em ciclos de 30 min
            self.rel.avancar(1800)
            avisos.append(self.c.monitorar()["avisar"])
        self.assertEqual(avisos, [False] + [True] * 5)     # 1º ciclo: acabou de sair de open (espera 10 min)
        self.assertEqual(self.logouts(), [])

    def test_M1_qr_abandonado_com_numero_pareado_faz_um_logout(self):
        self.cursor.pareado_em = int(self.rel()) - 30 * 86400
        self.cursor.salvar()
        self.fake.estado = "connecting"
        self.c.iniciar_qr()                               # clique Gerar QR; aba fechada, ninguém chama tick()
        avisos = []
        for _ in range(8):
            self.rel.avancar(1800)
            avisos.append(self.c.monitorar())
        self.assertEqual(len(self.logouts()), 1)
        self.assertIsNone(self.arq().get("qr_ate"))
        self.assertNotIn("connecting_prolongado", [r["motivo"] for r in avisos])
        self.assertLessEqual(sum(r["avisar"] for r in avisos), 1)

    def test_M1_sessao_de_qr_viva_nao_desloga_pareado(self):
        self.cursor.pareado_em = int(self.rel()) - 86400
        self.cursor.salvar()
        self.fake.estado = "connecting"
        self.c.iniciar_qr()
        self.rel.avancar(30)
        self.c.monitorar()
        self.assertEqual(self.logouts(), [])

    def test_H_428_perdido_na_janela_fixa_agora_e_detectado(self):
        self.fake.estado = "open"
        self.c.monitorar()                                # rodada anterior
        self.rel.avancar(1800)
        self.fake.reason_code = 428
        self.fake.reason_em = (datetime.fromtimestamp(self.rel(), timezone.utc) - timedelta(seconds=660)).isoformat()
        r = self.c.monitorar()
        self.assertEqual((r["evento"], r["avisar"]), ("socket_morto", True))
        self.assertEqual(self.arq()["reason_code"], 428)

    def test_H2_reiniciado_em_gravado_e_428_velho_nao_volta(self):
        self.fake.estado = "open"
        self.c.monitorar()
        self.rel.avancar(100)
        self.fake.reason_code = 428
        self.assertTrue(self.c.monitorar()["avisar"])
        self.rel.avancar(100)
        self.c.reiniciar(lambda cmd, **kw: None)
        self.rel.avancar(100)
        r = self.c.monitorar()
        self.assertEqual((r["evento"], r["avisar"]), ("ok", False))

    def test_J_ttl_e_ultimo_monitor_sem_efeito(self):
        self.assertEqual(self.c.ttl(), 0)
        self.c.iniciar_qr()
        self.rel.avancar(20)
        self.assertEqual(self.c.ttl(), 40)
        n = len(self.fake.chamadas)
        self.assertEqual(self.c.ultimo_monitor().get("evento"), None)
        self.assertEqual(self.c.ttl(), 40)
        self.assertEqual(len(self.fake.chamadas), n)
        self.fake.estado = "open"
        self.c.monitorar()
        n = len(self.fake.chamadas)
        self.assertEqual(self.c.ultimo_monitor()["evento"], "ok")
        self.assertEqual(len(self.fake.chamadas), n)

    def test_K_de_config_limita_valores(self):
        c = mod.Conexao.de_config(self.evo, self.banco, {"qr": {"maximo": 9, "teto_dia": 50, "segundos": "x",
                                                                 "janela_428_s": 5}},
                                  cursor=self.cursor, relogio=self.rel, estado_arq=self.arq_monitor)
        self.assertEqual((c.qr_max, c.teto_dia, c.qr_segundos, c.janela_428_s), (3, 10, 60, 60))
        c = mod.Conexao.de_config(self.evo, self.banco, {"qr": {"maximo": 0, "segundos": 500, "teto_dia": True}},
                                  cursor=self.cursor, relogio=self.rel, estado_arq=self.arq_monitor)
        self.assertEqual((c.qr_max, c.qr_segundos, c.teto_dia), (1, 120, 10))

    def test_arquivo_com_tipos_errados_nao_derruba_o_monitor(self):
        self.arq_monitor.parent.mkdir(parents=True, exist_ok=True)
        self.arq_monitor.write_text(json.dumps({"ultimo_estado": 7, "atualizado_em": "x", "qr_ate": [1],
                                                "instavel_desde": "y", "reason_visto": "z"}), encoding="utf-8")
        self.fake.estado = "open"
        self.assertEqual(self.c.monitorar()["evento"], "ok")


class TestCLIErros(unittest.TestCase):
    setUp = TestCLI.setUp
    escrever_env = TestCLI.escrever_env
    rodar = TestCLI.rodar

    def test_I_cursor_corrompido_vira_json_exit_1_sem_traceback(self):
        self.escrever_env()
        (self.raiz / "dados").mkdir(parents=True, exist_ok=True)
        (self.raiz / "dados" / "cursor.json").write_text("{lixo", encoding="utf-8")
        codigo, txt = self.rodar("--monitorar")
        self.assertEqual(codigo, 1)
        linhas = txt.strip().splitlines()
        self.assertEqual(len(linhas), 1)
        r = json.loads(linhas[0])
        self.assertEqual((r["evento"], r["avisar"], r["erro"]), ("erro", False, "CursorCorrompido"))

    def test_I2_erro_inesperado_tambem_vira_json(self):
        self.escrever_env()
        (self.raiz / "dados").mkdir(parents=True, exist_ok=True)
        (self.raiz / "dados" / "vigia.db").mkdir()        # Banco não abre um diretório
        codigo, txt = self.rodar("--monitorar")
        self.assertEqual(codigo, 1)
        self.assertEqual(json.loads(txt)["evento"], "erro")


class TestEventosFaseA(Base):
    """D-45: conexao_eventos guarda só as transições (queda, volta), nunca a repetição a cada ciclo."""

    def eventos(self):
        return [(e["evento"], e["estado"], e["motivo"]) for e in reversed(self.banco.eventos_conexao())]

    def test_queda_e_volta_so_na_transicao(self):
        self.fake.estado = "open"
        self.c.monitorar()
        self.assertEqual(self.eventos(), [])              # primeiro open não é "volta"
        self.fake.estado = "close"
        for _ in range(4):
            self.c.monitorar()
        self.fake.estado = "open"
        self.c.monitorar()
        self.c.monitorar()
        self.assertEqual(self.eventos(), [("queda", "close", "caiu"), ("voltou", "open", "reconectado")])

    def pareado(self):
        self.cursor.pareado_em = int(self.rel()) - 86400
        self.cursor.salvar()

    def test_queda_no_primeiro_ciclo_depois_do_despertar_so_avisa_se_persistir(self):   # pedido 43: "caiu" das 14:40
        self.pareado()
        self.fake.estado = "open"
        self.c.monitorar()
        self.rel.avancar(1800)
        self.fake.estado = "close"
        self.desp = int(self.rel() - 40)
        r = self.c.monitorar()
        self.assertEqual((r["evento"], r["avisar"], r["motivo"], r["despertou_em"]), ("queda", False, "caiu_apos_despertar", self.desp))
        self.assertEqual(self.pausas, [mod.DESPERTAR_PAUSA_S] * (mod.DESPERTAR_ESPERA_S // mod.DESPERTAR_PAUSA_S))   # esperou
        self.assertEqual(self.eventos(), [])                   # ainda não é transição confirmada
        self.desp = None
        self.rel.avancar(1800)
        r = self.c.monitorar()
        self.assertEqual((r["evento"], r["avisar"], r["motivo"]), ("queda", True, "caiu"))
        self.fake.estado = "open"
        self.c.monitorar()
        self.assertEqual(self.eventos(), [("queda", "close", "caiu"), ("voltou", "open", "reconectado")])

    def test_queda_apos_despertar_que_reconecta_nao_avisa_nada(self):
        self.pareado()
        self.fake.estado = "open"
        self.c.monitorar()
        self.rel.avancar(1800)
        self.fake.estado = "close"
        self.desp = int(self.rel() - 40)
        self.assertFalse(self.c.monitorar()["avisar"])
        self.desp = None
        self.rel.avancar(1800)
        self.fake.estado = "open"
        r = self.c.monitorar()
        self.assertEqual((r["evento"], r["avisar"]), ("ok", False))
        self.assertEqual(self.eventos(), [("voltou", "open", "reconectado")])   # só a volta: a queda era o sono

    def test_despertar_pleno_espera_a_instancia_abrir_e_marca_recuperacao(self):   # D-54: ao ligar, recupera
        self.pareado()
        self.fake.estado = "open"
        self.c.monitorar()
        self.rel.avancar(1800)
        self.fake.estado = "close"

        def dormir(s):
            self.pausas.append(s)
            if len(self.pausas) == 3:                          # na 3ª reconferência a Evolution reabriu
                self.fake.estado = "open"
        c = self.nova(dormir=dormir)
        self.desp = int(self.rel() - 10)
        r = c.monitorar()
        self.assertEqual((r["evento"], r["avisar"], r["estado"], r["recuperacao"]), ("ok", False, "open", "reconectou"))
        self.assertEqual(self.pausas, [mod.DESPERTAR_PAUSA_S] * 3)
        self.assertEqual(self.eventos(), [])                   # para o histórico nada caiu

    def test_espera_de_reconexao_tolera_evolution_indisponivel_no_meio(self):   # D-54: a Evolution também pode estar subindo
        self.pareado()
        self.fake.estado = "open"
        self.c.monitorar()
        self.rel.avancar(1800)
        self.desp = int(self.rel() - 10)
        respostas = [{"state": "close"}, EvolutionIndisponivel("subindo"), EvolutionIndisponivel("subindo"), {"state": "open"}]
        with mock.patch.object(self.c.evo, "estado", side_effect=respostas):
            r = self.c.monitorar()
        self.assertEqual((r["evento"], r["estado"], r["recuperacao"]), ("ok", "open", "reconectou"))
        self.assertEqual(self.pausas, [mod.DESPERTAR_PAUSA_S] * 3)

    def test_despertar_escuro_com_instancia_fechada_nao_espera_nem_avisa(self):
        self.pareado()
        self.fake.estado = "open"
        self.c.monitorar()
        self.rel.avancar(1800)
        self.fake.estado = "close"
        self.desp = (int(self.rel() - 10), "darkwake")
        r = self.c.monitorar()
        self.assertEqual((r["evento"], r["avisar"], r["motivo"], r["despertar"]), ("queda", False, "caiu_apos_despertar", "darkwake"))
        self.assertEqual(self.pausas, [])

    def test_banco_fora_nao_derruba_o_monitor(self):
        self.fake.estado = "open"
        self.c.monitorar()
        self.fake.estado = "close"
        with mock.patch.object(self.banco, "registrar_evento_conexao", side_effect=sqlite3.OperationalError("locked")):
            r = self.c.monitorar()
        self.assertEqual((r["evento"], r["avisar"]), ("queda", True))


class TestSonda(Base):
    """Pedido 41: sonda ativa do socket (foto do próprio número) com a Evolution em "open"."""
    JID = "559200000000@s.whatsapp.net"

    def setUp(self):
        super().setUp()
        self.pausas = []
        self.fake.fotos[self.JID] = "https://pps.whatsapp.net/foto.jpg"
        self.c = self.nova(sonda_jid=self.JID, dormir=self.pausas.append)
        self.fake.estado = "open"

    def sondas(self):
        return [ch for ch in self.fake.chamadas if "fetchProfilePictureUrl" in ch[1]]

    def test_sem_jid_nao_sonda(self):
        c = self.nova()
        r = c.monitorar()
        self.assertEqual((r["evento"], r.get("sonda")), ("ok", None))
        self.assertEqual(self.sondas(), [])

    def test_sonda_ok_uma_chamada_sem_pausa_e_sem_aviso(self):
        r = self.c.monitorar()
        self.assertEqual((r["evento"], r["avisar"], r["sonda"]), ("ok", False, "ok"))
        self.assertEqual(len(self.sondas()), 1)
        self.assertEqual(self.pausas, [])

    def test_sem_foto_so_e_suspeito_depois_de_ja_ter_visto_foto(self):
        self.fake.fotos.clear()
        r = self.c.monitorar()                       # nunca viu foto: vazio não prova nada
        self.assertEqual((r["evento"], r["sonda"]), ("ok", "sem_foto"))
        self.fake.fotos[self.JID] = "https://pps.whatsapp.net/foto.jpg"
        self.assertEqual(self.c.monitorar()["sonda"], "ok")
        self.fake.fotos.clear()
        r = self.c.monitorar()
        self.assertEqual((r["evento"], r["motivo"], r["avisar"]), ("socket_zumbi", "sonda_sem_foto_persistente", True))

    def marcar_mensagem(self, atras_s):
        arq = self.dir / "dados" / "ultima-mensagem.json"
        arq.parent.mkdir(parents=True, exist_ok=True)
        arq.write_text(json.dumps({"ultima_em": int(self.rel() - atras_s), "conferido_em": int(self.rel())}))

    def test_sem_foto_com_mensagem_nova_desde_o_ultimo_ciclo_nao_condena(self):   # pedido 43 (13:36 e 15:15 de 09/11)
        self.c.monitorar()                                   # viu foto
        self.rel.avancar(1800)
        self.fake.fotos.clear()
        self.marcar_mensagem(60)                             # mensagem gravada depois do ciclo anterior = socket vivo
        r = self.c.monitorar()
        self.assertEqual((r["evento"], r["sonda"], r["avisar"], r["sonda_adiada"]), ("ok", "sem_foto", False, "mensagem_recente"))
        self.assertNotIn("despertou_em", r)
        self.rel.avancar(1800)                               # ciclo seguinte: nada novo e ainda sem foto -> aí condena
        r = self.c.monitorar()
        self.assertEqual((r["evento"], r["motivo"], r["avisar"]), ("socket_zumbi", "sonda_sem_foto_persistente", True))

    def test_mensagem_antiga_nao_salva_a_sonda(self):
        self.c.monitorar()
        self.rel.avancar(1800)
        self.fake.fotos.clear()
        self.marcar_mensagem(3600)                           # anterior ao ciclo anterior: não prova nada
        self.assertEqual(self.c.monitorar()["evento"], "socket_zumbi")

    def test_despertar_escuro_nao_condena_a_sonda_nem_espera(self):   # D-54: DarkWakes de 13:36 e 15:15 de 09/11
        self.c.monitorar()
        self.rel.avancar(1800)
        self.fake.sonda_falha = True
        self.desp = (int(self.rel() - 30), "darkwake")       # despertar de manutenção: o Mac segue dormindo
        r = self.c.monitorar()
        self.assertEqual((r["evento"], r["sonda"], r["avisar"], r["sonda_adiada"], r["despertou_em"], r["despertar"]),
                         ("ok", "falhou", False, "despertar_escuro", self.desp[0], "darkwake"))
        self.assertEqual(self.pausas, [mod.SONDA_PAUSA_S])    # só a pausa da própria sonda: nada de esperar
        self.assertNotIn("recuperacao", r)
        self.desp = None
        self.rel.avancar(1800)                               # sem despertar novo e ainda falhando -> zumbi
        r = self.c.monitorar()
        self.assertEqual((r["evento"], r["motivo"], r["avisar"]), ("socket_zumbi", "sonda_falhou_persistente", True))
        self.assertNotIn("despertou_em", r)

    def test_despertar_pleno_espera_a_sonda_voltar_e_marca_recuperacao(self):   # D-54: ao ligar, recupera
        self.c.monitorar()                                   # ciclo anterior viu foto
        self.rel.avancar(1800)
        self.fake.sonda_falha = True

        def dormir(s):
            self.pausas.append(s)
            if self.pausas.count(mod.DESPERTAR_PAUSA_S) == 2:   # na 2ª reconferência o socket voltou
                self.fake.sonda_falha = False
        c = self.nova(sonda_jid=self.JID, dormir=dormir)
        self.desp = int(self.rel() - 30)                     # epoch solto = despertar pleno
        r = c.monitorar()
        self.assertEqual((r["evento"], r["sonda"], r["avisar"], r["recuperacao"], r["despertar"]),
                         ("ok", "ok", False, "sonda_voltou", "wake"))
        self.assertEqual(self.pausas, [mod.SONDA_PAUSA_S, mod.DESPERTAR_PAUSA_S, mod.SONDA_PAUSA_S, mod.DESPERTAR_PAUSA_S])
        self.assertNotIn("sonda_adiada", r)
        self.assertEqual(self.eventos(), [])                 # nunca chegou a ser zumbi: nada em conexao_eventos

    def test_despertar_pleno_com_socket_surdo_reinicia_sem_avisar_e_pede_segunda_leitura(self):
        self.c.monitorar()
        self.rel.avancar(1800)
        self.fake.sonda_falha = True
        chamadas = []
        ex = lambda cmd, cwd=None, **kw: chamadas.append(cmd)  # noqa: E731
        self.desp = int(self.rel() - 30)
        r = self.c.monitorar(executor=ex)
        self.assertEqual((r["evento"], r["motivo"], r["avisar"], r["reiniciado"], r["recuperacao"]),
                         ("socket_zumbi", "sonda_falhou_apos_despertar_reiniciada", False, True, "reiniciada"))
        self.assertEqual(chamadas, [["bin/evolution.sh", "restart"]])
        self.assertEqual(self.pausas.count(mod.DESPERTAR_PAUSA_S), mod.DESPERTAR_ESPERA_S // mod.DESPERTAR_PAUSA_S)
        self.desp = None
        self.rel.avancar(1800)                               # ainda surdo, acordado: avisa (sem reiniciar de novo em 2 h)
        r = self.c.monitorar(executor=ex)
        self.assertEqual((r["evento"], r["motivo"], r["avisar"], len(chamadas)),
                         ("socket_zumbi", "sonda_falhou_persistente", True, 1))
        self.assertNotIn("recuperacao", r)

    def test_despertar_pleno_sem_executor_nao_reinicia_mas_marca_o_motivo(self):
        self.c.monitorar()
        self.rel.avancar(1800)
        self.fake.sonda_falha = True
        self.desp = int(self.rel() - 30)
        r = self.c.monitorar()
        self.assertEqual((r["evento"], r["motivo"], r["avisar"]), ("socket_zumbi", "sonda_falhou_apos_despertar", False))
        self.assertNotIn("recuperacao", r)

    def test_despertar_anterior_ao_ciclo_passado_nao_conta(self):
        self.c.monitorar()
        self.rel.avancar(1800)
        self.fake.sonda_falha = True
        self.desp = int(self.rel() - 3600)                   # acordou antes do ciclo anterior
        self.assertEqual(self.c.monitorar()["evento"], "socket_zumbi")

    def test_sonda_falhou_reinicia_pelo_executor_e_respeita_o_intervalo(self):
        self.c.monitorar()
        self.fake.sonda_falha = True
        chamadas = []
        ex = lambda cmd, cwd=None, **kw: chamadas.append(cmd)  # noqa: E731
        r = self.c.monitorar(executor=ex)
        self.assertEqual((r["evento"], r["motivo"], r["avisar"], r.get("reiniciado")),
                         ("socket_zumbi", "sonda_falhou_reiniciada", True, True))
        self.assertEqual(chamadas, [["bin/evolution.sh", "restart"]])
        self.assertEqual(self.pausas, [mod.SONDA_PAUSA_S])       # 2 tentativas, uma pausa
        self.rel.avancar(1800)
        r = self.c.monitorar(executor=ex)                        # dentro das 2 h: não reinicia de novo, avisa (2º)
        self.assertEqual((r["motivo"], r["avisar"], len(chamadas)), ("sonda_falhou_persistente", True, 1))
        self.rel.avancar(1800)
        r = self.c.monitorar(executor=ex)                        # teto de avisos do episódio
        self.assertEqual((r["evento"], r["avisar"], len(chamadas)), ("socket_zumbi", False, 1))
        self.rel.avancar(mod.REINICIO_SONDA_INTERVALO_S)
        r = self.c.monitorar(executor=ex)                        # passou o intervalo: novo reinício, novo aviso
        self.assertEqual((r["motivo"], r["avisar"], len(chamadas)), ("sonda_falhou_reiniciada", True, 2))
        self.fake.sonda_falha = False
        r = self.c.monitorar(executor=ex)
        self.assertEqual((r["evento"], r["avisar"]), ("ok", False))
        self.assertIn("voltou", [e[0] for e in self.eventos()])

    def test_sonda_falhou_sem_executor_avisa_duas_vezes_e_cala(self):
        self.c.monitorar()
        self.fake.sonda_falha = True
        rs = [self.c.monitorar() for _ in range(3)]
        self.assertEqual([r["motivo"] for r in rs], ["sonda_falhou_persistente"] * 3)
        self.assertEqual([r["avisar"] for r in rs], [True, True, False])
        self.assertEqual(self.logouts(), [])

    def test_reinicio_que_falha_avisa_com_motivo_proprio(self):
        self.c.monitorar()
        self.fake.sonda_falha = True

        def ex(cmd, cwd=None, **kw):
            raise OSError("sem docker")
        r = self.c.monitorar(executor=ex)
        self.assertEqual((r["evento"], r["motivo"], r["avisar"]), ("socket_zumbi", "sonda_falhou_reinicio_falhou", True))

    def eventos(self):
        con = sqlite3.connect(self.banco.caminho)
        try:
            return [tuple(r) for r in con.execute("SELECT evento, estado, motivo FROM conexao_eventos ORDER BY id")]
        finally:
            con.close()


class TestSilencio(Base):
    """Pedido 41: conexão "open" e sonda ok, mas nenhuma mensagem nova há horas = o caso de 08-09/11."""

    def setUp(self):
        super().setUp()
        self.fake.estado = "open"
        self.rel.agora = datetime(2026, 11, 9, 10, 0).timestamp()      # 10h local, dentro de SILENCIO_HORAS
        self.arq = self.dir / "dados" / "ultima-mensagem.json"

    def marcar(self, atras_s):
        self.arq.parent.mkdir(parents=True, exist_ok=True)
        self.arq.write_text(json.dumps({"ultima_em": int(self.rel.agora - atras_s), "conferido_em": int(self.rel.agora)}))

    def test_sem_marcador_ou_mensagem_recente_e_ok(self):
        self.assertEqual(self.c.monitorar()["evento"], "ok")
        self.marcar(3600)
        r = self.c.monitorar()
        self.assertEqual((r["evento"], r["avisar"]), ("ok", False))

    def test_silencio_no_primeiro_ciclo_depois_do_despertar_nao_avisa(self):   # pedido 43: o silêncio era o sono do Mac
        self.c.monitorar()
        self.rel.avancar(1800)
        self.marcar(7 * 3600)
        self.desp = int(self.rel.agora - 20)
        r = self.c.monitorar()
        self.assertEqual((r["evento"], r["avisar"], r["silencio_h"]), ("silencio", False, 7))   # âmbar no painel, sem WhatsApp
        self.desp = None
        self.rel.avancar(1800)
        r = self.c.monitorar()
        self.assertEqual((r["evento"], r["avisar"]), ("silencio", True))           # acordado e ainda calado: avisa

    def test_silencio_longo_avisa_uma_vez_por_dia_e_nunca_reinicia(self):
        self.marcar(7 * 3600)
        r = self.c.monitorar()
        self.assertEqual((r["evento"], r["motivo"], r["avisar"], r["silencio_h"]), ("silencio", "sem_mensagem_7h", True, 7))
        self.rel.avancar(1800)
        r = self.c.monitorar()
        self.assertEqual((r["evento"], r["avisar"]), ("silencio", False))
        self.rel.avancar(mod.SILENCIO_REPETE_S)
        r = self.c.monitorar()
        self.assertEqual((r["evento"], r["avisar"]), ("silencio", True))
        self.assertEqual((self.conexoes(), self.logouts()), ([], []))

    def test_fora_do_horario_segura_o_aviso_ate_as_9h(self):
        self.rel.agora = datetime(2026, 11, 9, 23, 0).timestamp()
        self.marcar(8 * 3600)
        r = self.c.monitorar()
        self.assertEqual((r["evento"], r["avisar"]), ("silencio", False))
        self.rel.avancar(10 * 3600)                                     # 9h do dia seguinte
        r = self.c.monitorar()
        self.assertEqual((r["evento"], r["avisar"]), ("silencio", True))

    def test_mensagem_nova_zera_e_o_proximo_silencio_avisa_de_novo(self):
        self.marcar(7 * 3600)
        self.assertTrue(self.c.monitorar()["avisar"])
        self.marcar(60)
        self.assertEqual(self.c.monitorar()["evento"], "ok")
        self.rel.avancar(7 * 3600)
        r = self.c.monitorar()
        self.assertEqual((r["evento"], r["avisar"]), ("silencio", True))

    def test_ultima_mensagem_em_le_o_marcador(self):
        self.assertIsNone(self.c.ultima_mensagem_em())
        self.marcar(120)
        self.assertEqual(self.c.ultima_mensagem_em(), int(self.rel.agora) - 120)
        self.arq.write_text("{quebrado")
        self.assertIsNone(self.c.ultima_mensagem_em())


class TestCLISonda(TestCLI):
    def test_dono_pessoal_no_env_liga_a_sonda(self):
        self.escrever_env()
        jid = "559200000000@s.whatsapp.net"
        self.fake.fotos[jid] = "https://pps.whatsapp.net/foto.jpg"
        with open(self.raiz / ".env", "a", encoding="utf-8") as f:
            f.write(f"DONO_WHATSAPP={jid}\n")
        self.fake.estado = "open"
        codigo, txt = self.rodar("--monitorar")
        self.assertEqual(codigo, 0)
        self.assertEqual(json.loads(txt)["sonda"], "ok")
        self.assertTrue(any("fetchProfilePictureUrl" in ch[1] for ch in self.fake.chamadas))

    def test_sem_dono_pessoal_monitor_segue_sem_sonda(self):
        self.escrever_env()
        self.fake.estado = "open"
        codigo, txt = self.rodar("--monitorar")
        self.assertEqual(codigo, 0)
        self.assertIsNone(json.loads(txt).get("sonda"))
        self.assertFalse(any("fetchProfilePictureUrl" in ch[1] for ch in self.fake.chamadas))


if __name__ == "__main__":
    unittest.main()


class TestUltimoDespertar(unittest.TestCase):
    """D-54: kern.waketime (quando) + pmset -g systemstate (pleno ou de manutenção)."""
    SYSCTL = "{ sec = 1794254557, usec = 588963 } Mon Nov  9 16:02:37 2026\n"
    PLENO = "Current System Capabilities are: CPU Graphics Audio Network \nCurrent Power State: 4\n"
    ESCURO = "Current System Capabilities are: CPU Network \nCurrent Power State: 4\n"

    @staticmethod
    def _run(sysctl, pmset):
        def run(cmd, **kw):
            if cmd[0].endswith("sysctl"):
                if isinstance(sysctl, Exception):
                    raise sysctl
                return mock.Mock(stdout=sysctl)
            if isinstance(pmset, Exception):
                raise pmset
            return mock.Mock(stdout=pmset)
        return run

    def test_despertar_pleno_e_escuro(self):
        with mock.patch.object(mod.subprocess, "run", side_effect=self._run(self.SYSCTL, self.PLENO)) as run:
            self.assertEqual(mod.ultimo_despertar(), (1794254557, "wake"))
        self.assertEqual([c.args[0] for c in run.call_args_list],
                         [["/usr/sbin/sysctl", "-n", "kern.waketime"], ["/usr/bin/pmset", "-g", "systemstate"]])
        with mock.patch.object(mod.subprocess, "run", side_effect=self._run(self.SYSCTL, self.ESCURO)):
            self.assertEqual(mod.ultimo_despertar(), (1794254557, "darkwake"))

    def test_sem_sysctl_vale_none_e_sem_pmset_vale_pleno(self):
        with mock.patch.object(mod.subprocess, "run", side_effect=self._run(OSError(), self.PLENO)):
            self.assertIsNone(mod.ultimo_despertar())
        with mock.patch.object(mod.subprocess, "run", side_effect=self._run("", self.PLENO)):
            self.assertIsNone(mod.ultimo_despertar())
        with mock.patch.object(mod.subprocess, "run", side_effect=self._run(subprocess.TimeoutExpired("sysctl", 3), self.PLENO)):
            self.assertIsNone(mod.ultimo_despertar())
        with mock.patch.object(mod.subprocess, "run", side_effect=self._run(self.SYSCTL, OSError())):
            self.assertEqual(mod.ultimo_despertar(), (1794254557, "wake"))
        with mock.patch.object(mod.subprocess, "run", side_effect=self._run(self.SYSCTL, "lixo")):
            self.assertEqual(mod.ultimo_despertar(), (1794254557, "wake"))


class TestCanario(Base):
    """D-56 (pedido 44): "open" + sonda ok não provam ENTREGA (09/11: Postgres parado 2 h 35 com a instância aberta e a
    sonda respondendo). Calada há 2 h dentro do horário, a conexão é posta à prova com um texto do bot ao próprio número."""

    def setUp(self):
        super().setUp()
        self.fake.estado = "open"
        self.rel.agora = datetime(2026, 11, 9, 10, 0).timestamp()      # 10h local, dentro de SILENCIO_HORAS
        self.arq = self.dir / "dados" / "ultima-mensagem.json"
        self.enviados = []
        self.envio_ok = True
        self.chega = True                   # o teste aparece no Postgres na primeira conferência
        self.chega_apos_reinicio = False    # ... ou só depois do reinício
        self.reinicios = []
        self.c = self.nova(canario_enviar=self._enviar, canario_chegou=self._chegou)
        self.cursor.pareado_em = int(self.rel()) - 86400
        self.cursor.salvar()

    def _enviar(self, texto):
        self.enviados.append(texto)
        return self.envio_ok

    def _chegou(self, texto, desde):
        if texto not in self.enviados:
            return False
        return self.chega or (self.chega_apos_reinicio and bool(self.reinicios))

    def executor(self, cmd, **kw):
        self.reinicios.append(cmd)
        return 0

    def marcar(self, atras_s):
        self.arq.parent.mkdir(parents=True, exist_ok=True)
        self.arq.write_text(json.dumps({"ultima_em": int(self.rel.agora - atras_s), "conferido_em": int(self.rel.agora)}))

    def estado(self):
        return json.loads(self.arq_monitor.read_text())

    def test_calada_ha_duas_horas_manda_o_teste_e_entrega_provada_segue_ok(self):
        self.marcar(int(2.5 * 3600))
        r = self.c.monitorar(executor=self.executor)
        self.assertEqual(len(self.enviados), 1)
        self.assertEqual(self.enviados[0], mod.CANARIO_PREFIXO + "10:00")
        self.assertEqual((r["evento"], r["avisar"], r["canario"]), ("ok", False, "chegou"))
        self.assertNotIn("reiniciado", r)
        self.assertNotIn("recuperacao", r)
        self.assertEqual(self.reinicios, [])
        self.assertEqual(self.pausas, [mod.CANARIO_PAUSA_S])
        e = self.estado()
        self.assertEqual((e["canario_em"], e["canario_ok_em"]), (int(self.rel()), int(self.rel())))

    def test_nao_repete_antes_de_seis_horas_e_entrega_provada_cala_o_aviso_de_silencio(self):
        self.marcar(int(2.5 * 3600))
        self.c.monitorar(executor=self.executor)
        self.rel.avancar(1800)
        self.marcar(3 * 3600)
        r = self.c.monitorar(executor=self.executor)
        self.assertEqual((len(self.enviados), r["evento"]), (1, "ok"))
        self.assertNotIn("canario", r)
        self.rel.avancar(mod.CANARIO_REPETE_S)
        self.marcar(int(9.5 * 3600))                        # 6 h+ calada: antes virava "silencio" com aviso
        r = self.c.monitorar(executor=self.executor)
        self.assertEqual((len(self.enviados), r["canario"], r["evento"], r["avisar"]), (2, "chegou", "ok", False))
        self.assertNotIn("silencio_h", r)
        self.assertEqual((self.conexoes(), self.logouts()), ([], []))

    def test_sem_silencio_fora_do_horario_recem_acordado_ou_sem_pareamento_nao_testa(self):
        self.marcar(3600)
        self.c.monitorar(executor=self.executor)
        self.rel.agora = datetime(2026, 11, 9, 23, 0).timestamp()
        self.marcar(3 * 3600)
        self.c.monitorar(executor=self.executor)
        self.rel.agora = datetime(2026, 11, 10, 10, 0).timestamp()
        self.marcar(3 * 3600)
        self.desp = int(self.rel.agora - 20)
        self.c.monitorar(executor=self.executor)
        self.desp = None
        sem_par = Cursor.carregar(self.dir / "dados" / "cursor-sem-pareamento.json")   # nunca pareado
        c2 = self.nova(cursor=sem_par, canario_enviar=self._enviar, canario_chegou=self._chegou)
        c2.monitorar(executor=self.executor)
        self.assertEqual(self.enviados, [])
        c = self.nova()                                      # Conexao do painel: sem ganchos, sem canário
        self.assertNotIn("canario", c.monitorar())
        self.assertEqual(self.enviados, [])

    def test_envio_falho_nao_conclui_nada_e_tenta_no_proximo_ciclo(self):
        self.envio_ok = False
        self.marcar(int(2.5 * 3600))
        r = self.c.monitorar(executor=self.executor)
        self.assertEqual((r["canario"], r["evento"], r["avisar"]), ("envio_falhou", "ok", False))
        self.assertNotIn("canario_em", self.estado())
        self.assertEqual(self.pausas, [])
        self.rel.avancar(1800)
        self.marcar(3 * 3600)
        self.c.monitorar(executor=self.executor)
        self.assertEqual(len(self.enviados), 2)
        self.rel.avancar(4 * 3600)                           # bot fora e 7 h calada: o aviso de silêncio (D-50) segue valendo
        self.marcar(7 * 3600)
        r = self.c.monitorar(executor=self.executor)
        self.assertEqual((r["evento"], r["avisar"], r["silencio_h"]), ("silencio", True, 7))

    def test_teste_que_nao_chega_reinicia_destrava_e_o_ciclo_le_de_novo(self):
        self.chega, self.chega_apos_reinicio = False, True
        self.marcar(int(2.5 * 3600))
        r = self.c.monitorar(executor=self.executor)
        self.assertEqual(self.reinicios, [["bin/evolution.sh", "restart"]])
        self.assertEqual((r["evento"], r["avisar"], r["canario"], r["recuperacao"], r["reiniciado"]),
                         ("ok", False, "chegou_apos_reinicio", "reiniciada", True))
        # 6 conferências sem chegar + 1 reconsulta do estado (já open) + 1 conferência que chega
        self.assertEqual(len(self.pausas), mod.CANARIO_ESPERA_S // mod.CANARIO_PAUSA_S + 2)
        ev = self.banco.eventos_conexao()
        self.assertEqual([(x["evento"], x["motivo"]) for x in ev], [("entrega_travada", "reiniciada_voltou")])
        self.assertIn("canario_ok_em", self.estado())

    def test_teste_que_nao_chega_nem_apos_reinicio_avisa_entrega_travada_no_maximo_a_cada_seis_horas(self):
        self.chega = False
        self.marcar(int(2.5 * 3600))
        r = self.c.monitorar(executor=self.executor)
        self.assertEqual((r["evento"], r["motivo"], r["avisar"], r["reiniciado"], r["canario"]),
                         ("entrega_travada", "entrega_travada_reiniciada", True, True, "travada"))
        self.assertEqual(len(self.reinicios), 1)
        self.assertEqual([(x["evento"], x["motivo"]) for x in self.banco.eventos_conexao()],
                         [("entrega_travada", "entrega_travada_reiniciada")])
        self.rel.avancar(1800)
        self.marcar(3 * 3600)
        r = self.c.monitorar(executor=self.executor)        # meia hora depois: sem teste novo, sem aviso novo
        self.assertEqual((len(self.enviados), r["evento"], r["avisar"]), (1, "ok", False))
        self.rel.avancar(mod.CANARIO_REPETE_S)
        self.marcar(int(9.5 * 3600))
        r = self.c.monitorar(executor=self.executor)        # 6 h depois: testa de novo, reinicia de novo, avisa de novo
        self.assertEqual((len(self.enviados), len(self.reinicios), r["evento"], r["avisar"]), (2, 2, "entrega_travada", True))

    def test_reinicio_recente_ou_sem_executor_so_avisa(self):
        self.chega = False
        self.c._mesclar(reiniciado_em=int(self.rel()) - 600)
        self.marcar(int(2.5 * 3600))
        r = self.c.monitorar(executor=self.executor)
        self.assertEqual((r["motivo"], r["avisar"], self.reinicios), ("entrega_travada_reinicio_recente", True, []))
        self.assertNotIn("reiniciado", r)
        self.rel.avancar(mod.CANARIO_REPETE_S)
        self.marcar(int(8.5 * 3600))
        r = self.c.monitorar()                               # sem executor: não há como reiniciar
        self.assertEqual((r["motivo"], r["avisar"], self.reinicios), ("entrega_travada", True, []))

    def test_reinicio_que_falha_e_postgres_fora(self):
        self.chega = False
        self.marcar(int(2.5 * 3600))
        r = self.c.monitorar(executor=lambda cmd, **kw: 1)
        self.assertEqual((r["motivo"], r["avisar"]), ("entrega_travada_reinicio_falhou", True))
        self.assertNotIn("reiniciado", r)

        def fora(texto, desde):
            raise lotes_mod.PostgresIndisponivel("psql fora")
        c = self.nova(canario_enviar=self._enviar, canario_chegou=fora)
        self.rel.avancar(mod.CANARIO_REPETE_S)
        self.marcar(int(8.5 * 3600))
        r = c.monitorar()                                    # Postgres fora conta como "não chegou", nunca levanta
        self.assertEqual((r["canario"], r["motivo"]), ("travada", "entrega_travada"))


class TestCanarioChegouSQL(unittest.TestCase):
    def test_consulta_conta_so_o_proprio_texto_recebido_desde_o_envio(self):
        consultas = []

        def psql(q):
            consultas.append(q)
            return [["1"]]
        self.assertTrue(lotes_mod.canario_chegou(psql, "Vigia: teste de entrega 10:00", 1794250800))
        q = consultas[0]
        self.assertIn('"messageTimestamp" >= 1794250680', q)             # 120 s de folga antes do envio
        self.assertIn("(key->>'fromMe') IS DISTINCT FROM 'true'", q)
        self.assertIn("message->>'conversation' = 'Vigia: teste de entrega 10:00'", q)
        self.assertFalse(lotes_mod.canario_chegou(lambda q: [["0"]], "x", 1))
        self.assertFalse(lotes_mod.canario_chegou(lambda q: [], "x", 1))
        consultas.clear()
        lotes_mod.canario_chegou(psql, "a'b", 1)
        self.assertIn("= 'a''b'", consultas[0])
