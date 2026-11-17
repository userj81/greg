"""Pedido 81 (D-82): intervalo e horário de leitura escolhidos em Ajustes.
O agendador continua disparando de 30 em 30 minutos; app/agenda_leitura.py decide se o disparo lê.
Quem chama: unittest. Nada sai do Mac: relógio falso, pasta temporária, Evolution e Frentes falsos em 127.0.0.1.
"""
import io
import json
import logging
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import agenda_leitura as ag  # noqa: E402
from app import alertas, saude  # noqa: E402
from app import preferencias as pf  # noqa: E402
from app.paginas import ajustes, conexao, demandas  # noqa: E402
from testes.test_mcp import chamar  # noqa: E402
from testes.test_paginas import Base, texto  # noqa: E402
from testes.util_contrato import _importar_arquivo  # noqa: E402

DIA = datetime(2026, 11, 16, 0, 0)      # uma segunda-feira qualquer; a regra não olha o dia da semana


def simular(inicio: datetime, disparos: int, intervalo: int, de: int, ate: int, ultima=None) -> list[str]:
    """Agendador de mentira: um disparo a cada 30 min a partir de `inicio`. Devolve a hora de cada disparo que leu."""
    lidos, t = [], inicio
    for _ in range(disparos):
        ler, _motivo = ag.decidir(t + timedelta(seconds=4), ultima, intervalo, de, ate)   # o ciclo começa segundos depois
        if ler:
            lidos.append(t.strftime("%H:%M"))
            ultima = (t + timedelta(seconds=40)).timestamp()                              # a leitura termina depois
        t += timedelta(minutes=30)
    return lidos


class TestCadaOpcao(unittest.TestCase):
    def test_padrao_le_em_todo_disparo_como_sempre(self):
        self.assertTrue(ag.e_padrao(30, 0, 0))
        self.assertEqual(len(simular(DIA, 48, 30, 0, 0)), 48)
        agora = DIA + timedelta(hours=10, minutes=10)       # leu há 1 minuto e disparou de novo (RunAtLoad): lê
        self.assertEqual(ag.decidir(agora, (agora - timedelta(minutes=1)).timestamp(), 30, 0, 0), (True, "no horário"))

    def test_uma_hora(self):
        self.assertEqual(simular(DIA, 48, 60, 0, 0), [f"{h:02d}:00" for h in range(24)])

    def test_duas_horas(self):
        self.assertEqual(simular(DIA, 48, 120, 0, 0), [f"{h:02d}:00" for h in range(0, 24, 2)])

    def test_quatro_horas(self):
        self.assertEqual(simular(DIA, 48, 240, 0, 0), ["00:00", "04:00", "08:00", "12:00", "16:00", "20:00"])

    def test_todas_as_opcoes_da_tela_tem_regra(self):
        self.assertEqual(list(pf.LEITURA_MIN), [30, 60, 120, 240])
        for intervalo in pf.LEITURA_MIN:
            self.assertEqual(len(simular(DIA, 48, intervalo, 0, 0)), 24 * 60 // intervalo, intervalo)

    def test_quem_nao_leu_no_intervalo_diz_por_que(self):
        agora = DIA + timedelta(hours=10, minutes=30, seconds=3)
        ler, motivo = ag.decidir(agora, (DIA + timedelta(hours=10, seconds=50)).timestamp(), 120, 0, 0)
        self.assertEqual((ler, motivo), (False, "já leu neste intervalo (2 horas)"))


class TestHorario(unittest.TestCase):
    def test_trinta_minutos_das_8_as_18_inclui_a_leitura_das_18(self):
        lidos = simular(DIA, 48, 30, 8, 18)
        self.assertEqual((lidos[0], lidos[-1], len(lidos)), ("08:00", "18:00", 21))
        self.assertNotIn("07:30", lidos)
        self.assertNotIn("18:30", lidos)

    def test_uma_hora_das_8_as_18(self):
        self.assertEqual(simular(DIA, 48, 60, 8, 18), [f"{h:02d}:00" for h in range(8, 19)])

    def test_intervalo_conta_da_hora_inicial(self):
        self.assertEqual(simular(DIA, 48, 120, 9, 18), ["09:00", "11:00", "13:00", "15:00", "17:00"])
        self.assertEqual(simular(DIA, 48, 240, 9, 18), ["09:00", "13:00", "17:00"])

    def test_virada_de_dentro_para_fora_e_de_fora_para_dentro(self):
        for hora, minuto, esperado in ((7, 59, False), (8, 0, True), (18, 0, True), (18, 29, True), (18, 30, False)):
            agora = DIA + timedelta(hours=hora, minutes=minuto)
            self.assertEqual(ag.no_horario(agora, 8, 18), esperado, (hora, minuto))
        ler, motivo = ag.decidir(DIA + timedelta(hours=7, minutes=30), None, 30, 8, 18)
        self.assertEqual((ler, motivo), (False, "fora do horário de leitura (das 8h às 18h)"))

    def test_faixa_que_atravessa_a_meia_noite(self):
        for hora, minuto, esperado in ((21, 30, False), (22, 0, True), (23, 59, True), (0, 0, True), (3, 30, True),
                                       (6, 0, True), (6, 30, False), (12, 0, False)):
            self.assertEqual(ag.no_horario(DIA + timedelta(hours=hora, minutes=minuto), 22, 6), esperado, (hora, minuto))
        # 2 h das 22 às 6: os trechos contam das 22h e não quebram na meia-noite
        self.assertEqual(simular(DIA + timedelta(hours=12), 48, 120, 22, 6), ["22:00", "00:00", "02:00", "04:00", "06:00"])
        self.assertEqual(simular(DIA + timedelta(hours=12), 48, 60, 22, 6),
                         ["22:00", "23:00", "00:00", "01:00", "02:00", "03:00", "04:00", "05:00", "06:00"])

    def test_virada_do_dia_com_o_dia_todo(self):
        ultima = (DIA + timedelta(hours=20, seconds=30)).timestamp()            # leu às 20h, intervalo de 4 h
        self.assertFalse(ag.decidir(DIA + timedelta(hours=23, minutes=30), ultima, 240, 0, 0)[0])
        self.assertTrue(ag.decidir(DIA + timedelta(days=1, seconds=3), ultima, 240, 0, 0)[0])

    def test_horas_iguais_e_o_dia_todo(self):
        for h in (0, 7, 23):
            self.assertTrue(all(ag.no_horario(DIA + timedelta(minutes=30 * i), h, h) for i in range(48)), h)
        self.assertEqual(ag.texto_horario(7, 7), "o dia todo")


class TestMacQueVoltou(unittest.TestCase):
    """O Mac ficou desligado ou dormindo e o ciclo dispara fora da hora cheia (RunAtLoad, despertar)."""

    def test_volta_num_intervalo_ainda_nao_lido_e_le_na_hora(self):
        ultima = (DIA + timedelta(hours=8, seconds=40)).timestamp()             # última leitura às 8h, intervalo de 4 h
        volta = DIA + timedelta(hours=13, minutes=10)
        self.assertEqual(ag.decidir(volta, ultima, 240, 0, 0), (True, "intervalo novo"))
        lida = (volta + timedelta(seconds=50)).timestamp()
        self.assertFalse(ag.decidir(DIA + timedelta(hours=13, minutes=30), lida, 240, 0, 0)[0])
        self.assertTrue(ag.decidir(DIA + timedelta(hours=16, seconds=2), lida, 240, 0, 0)[0])   # e volta à hora cheia

    def test_volta_no_mesmo_intervalo_ja_lido_e_nao_le_de_novo(self):
        ultima = (DIA + timedelta(hours=8, seconds=40)).timestamp()
        self.assertFalse(ag.decidir(DIA + timedelta(hours=10, minutes=40), ultima, 240, 0, 0)[0])

    def test_volta_dias_depois(self):
        ultima = (DIA - timedelta(days=3)).timestamp()
        self.assertTrue(ag.decidir(DIA + timedelta(hours=9, minutes=17), ultima, 120, 0, 0)[0])

    def test_volta_fora_do_horario_espera_o_horario(self):
        ultima = (DIA - timedelta(days=3)).timestamp()
        self.assertFalse(ag.decidir(DIA + timedelta(hours=6, minutes=17), ultima, 60, 8, 18)[0])
        self.assertTrue(ag.decidir(DIA + timedelta(hours=8, seconds=5), ultima, 60, 8, 18)[0])

    def test_nunca_leu(self):
        self.assertTrue(ag.decidir(DIA + timedelta(hours=9, minutes=17), None, 240, 0, 0)[0])


class TestForaDaAgenda(unittest.TestCase):
    def test_ler_agora_vale_fora_do_intervalo_e_do_horario(self):
        agora = DIA + timedelta(hours=3, minutes=12)
        ultima = (agora - timedelta(minutes=5)).timestamp()
        self.assertFalse(ag.decidir(agora, ultima, 240, 8, 18)[0])
        self.assertEqual(ag.decidir(agora, ultima, 240, 8, 18, pedida=True), (True, "leitura pedida"))

    def test_segunda_leitura_da_recuperacao_vale_so_dentro_do_horario(self):
        dentro, fora = DIA + timedelta(hours=10, minutes=31), DIA + timedelta(hours=20, minutes=31)
        ultima = (dentro - timedelta(minutes=1)).timestamp()
        self.assertFalse(ag.decidir(dentro, ultima, 120, 8, 18)[0])
        self.assertTrue(ag.decidir(dentro, ultima, 120, 8, 18, recuperacao=True)[0])
        self.assertFalse(ag.decidir(fora, ultima, 120, 8, 18, recuperacao=True)[0])

    def test_fila_que_sobrou_nao_espera_o_intervalo_mas_espera_o_horario(self):
        dentro, fora = DIA + timedelta(hours=10, minutes=30, seconds=3), DIA + timedelta(hours=19)
        ultima = (DIA + timedelta(hours=10, seconds=50)).timestamp()
        self.assertFalse(ag.decidir(dentro, ultima, 240, 8, 18)[0])
        self.assertTrue(ag.decidir(dentro, ultima, 240, 8, 18, pendente=True)[0])
        self.assertFalse(ag.decidir(fora, ultima, 240, 8, 18, pendente=True)[0])


class TestProxima(unittest.TestCase):
    def test_proxima_leitura_pelo_intervalo_e_pelo_horario(self):
        agora = DIA + timedelta(hours=10, minutes=12)
        ultima = (DIA + timedelta(hours=10, seconds=40)).timestamp()
        self.assertEqual(ag.texto_proxima(agora, ag.proxima(agora, ultima, 120, 0, 0)), "Próxima leitura às 12:00")
        self.assertEqual(ag.texto_proxima(agora, ag.proxima(agora, ultima, 30, 0, 0)), "Próxima leitura às 10:30")
        noite = DIA + timedelta(hours=19, minutes=5)
        self.assertEqual(ag.texto_proxima(noite, ag.proxima(noite, ultima, 60, 8, 18)), "Próxima leitura amanhã às 8:00")
        madrugada = DIA + timedelta(hours=2)
        self.assertEqual(ag.texto_proxima(madrugada, ag.proxima(madrugada, None, 60, 8, 18)), "Próxima leitura às 8:00")

    def test_textos_da_escolha(self):
        self.assertEqual(ag.texto_escolha(30, 0, 0), "A cada 30 minutos")
        self.assertEqual(ag.texto_escolha(120, 8, 18), "A cada 2 horas, das 8h às 18h")


class ComPasta(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.dados = Path(self._t.name) / "dados"
        self.dados.mkdir()

    def escolher(self, **k):
        (self.dados / "preferencias.json").write_text(json.dumps({**pf.PADRAO, **k}), encoding="utf-8")


class TestEstadoEmDisco(ComPasta):
    def test_sem_arquivo_ilegivel_ou_fora_da_lista_cai_no_padrao(self):
        self.assertEqual(ag.escolha(self.dados), (30, 0, 0))
        (self.dados / "preferencias.json").write_text("{quebrado", encoding="utf-8")
        self.assertEqual(ag.escolha(self.dados), (30, 0, 0))
        (self.dados / "preferencias.json").write_text("[1, 2]", encoding="utf-8")
        self.assertEqual(ag.escolha(self.dados), (30, 0, 0))
        self.escolher(leitura_min=45, leitura_de=8, leitura_ate=99)
        self.assertEqual(ag.escolha(self.dados), (30, 0, 0))
        self.escolher(leitura_min=120, leitura_de=8, leitura_ate=18)
        self.assertEqual(ag.escolha(self.dados), (120, 8, 18))

    def test_arquivo_de_antes_do_pedido_continua_no_padrao(self):
        (self.dados / "preferencias.json").write_text(json.dumps({"fonte": 14, "tema": "escuro", "retencao_dias": 30}))
        self.assertEqual(ag.escolha(self.dados), (30, 0, 0))
        self.assertEqual(pf.validar({"fonte": 14})["leitura_min"], 30)

    def test_sinal_vale_uma_vez_e_por_dez_minutos(self):
        self.assertFalse(ag.consumir_pedido(self.dados))
        ag.pedir(self.dados)
        self.assertEqual(os.stat(self.dados / ag.SINAL).st_mode & 0o777, 0o600)
        self.assertTrue(ag.consumir_pedido(self.dados))
        self.assertFalse(ag.consumir_pedido(self.dados))                       # um disparo só
        ag.pedir(self.dados)
        self.assertFalse(ag.consumir_pedido(self.dados, agora=time.time() + ag.SINAL_VALE_S + 5))   # sinal velho
        self.assertFalse((self.dados / ag.SINAL).exists())                     # e não fica para o disparo seguinte

    def test_decisao_do_disparo_junta_escolha_ultima_leitura_e_sinal(self):
        t = (DIA + timedelta(hours=20, minutes=30, seconds=3)).timestamp()
        self.assertTrue(ag.deste_disparo(self.dados, agora=t)[0])               # padrão: lê
        self.escolher(leitura_min=60, leitura_de=8, leitura_ate=18)
        self.assertEqual(ag.deste_disparo(self.dados, agora=t), (False, "fora do horário de leitura (das 8h às 18h)"))
        ag.pedir(self.dados)
        os.utime(self.dados / ag.SINAL, (t - 20, t - 20))
        self.assertEqual(ag.deste_disparo(self.dados, agora=t), (True, "leitura pedida"))
        self.assertFalse(ag.deste_disparo(self.dados, agora=t)[0])
        self.assertTrue(ag.deste_disparo(self.dados, agora=t, forcar=True)[0])
        dentro = (DIA + timedelta(hours=10, minutes=30, seconds=3)).timestamp()
        (self.dados / "ultima-leitura.json").write_text(json.dumps({"em": int(dentro) - 1700, "pendente": False}))
        self.assertFalse(ag.deste_disparo(self.dados, agora=dentro)[0])
        self.assertTrue(ag.deste_disparo(self.dados, agora=dentro, recuperacao=True)[0])
        (self.dados / "ultima-leitura.json").write_text(json.dumps({"em": int(dentro) - 1700, "pendente": True}))
        self.assertTrue(ag.deste_disparo(self.dados, agora=dentro)[0])


class TestColetor(ComPasta):
    def setUp(self):
        super().setUp()
        self.col = _importar_arquivo("bin/coletor.py", "coletor")
        raiz = logging.getLogger("vigia")
        antes = list(raiz.handlers)
        self.addCleanup(lambda: setattr(raiz, "handlers", antes))
        antigo = os.umask(0o022)
        self.addCleanup(os.umask, antigo)

    def rodar(self, argv=(), **env):
        saida = io.StringIO()
        amb = {"VIGIA_DADOS": str(self.dados), "VIGIA_ENV": str(self.dados / ".env"), **env}
        with mock.patch.dict(os.environ, amb), redirect_stdout(saida):
            os.environ.pop("VIGIA_LER_AGORA", None) if "VIGIA_LER_AGORA" not in env else None
            os.environ.pop("VIGIA_SEGUNDA_LEITURA", None) if "VIGIA_SEGUNDA_LEITURA" not in env else None
            rc = self.col.main(list(argv))
        return rc, saida.getvalue()

    def fora_do_horario(self):
        h = datetime.now().hour
        self.escolher(leitura_min=60, leitura_de=(h + 2) % 24, leitura_ate=(h + 3) % 24)

    def test_fora_do_horario_o_ciclo_passa_sem_ler_e_registra_que_rodou(self):
        self.fora_do_horario()
        with mock.patch.object(self.col, "ler_env", side_effect=AssertionError("não devia chegar à leitura")):
            rc, saida = self.rodar()
        self.assertEqual(rc, 0)
        r = json.loads(saida)
        self.assertEqual(r["status"], "fora_da_agenda")
        self.assertIn("fora do horário de leitura", r["motivo"])
        self.assertFalse((self.dados / "ultima-leitura.json").exists())         # "lido às HH:MM" não mente
        self.assertAlmostEqual(ag.ultima_passada(self.dados), time.time(), delta=5)

    def test_ler_agora_passa_pela_agenda(self):
        """Com o sinal (ou --agora, ou VIGIA_LER_AGORA=1) o coletor segue para a leitura mesmo fora do horário."""
        class Chegou(Exception):
            pass
        self.fora_do_horario()
        casos = [((), {}, True), (("--agora",), {}, False), ((), {"VIGIA_LER_AGORA": "1"}, False)]
        for argv, env, com_sinal in casos:
            if com_sinal:
                ag.pedir(self.dados)
            with mock.patch.object(self.col, "ler_env", side_effect=Chegou):
                with self.assertRaises(Chegou, msg=(argv, env)):
                    self.rodar(argv, **env)
        self.assertFalse((self.dados / ag.SINAL).exists())

    def test_dry_run_e_sem_claude_nao_passam_pela_agenda_nem_gastam_o_sinal(self):
        class Chegou(Exception):
            pass
        self.fora_do_horario()
        ag.pedir(self.dados)
        with mock.patch.object(self.col, "ler_env", side_effect=Chegou):
            for argv in (("--dry-run",), ("--sem-claude",)):
                with self.assertRaises(Chegou):
                    self.rodar(argv)
        self.assertTrue((self.dados / ag.SINAL).exists())
        self.assertFalse((self.dados / ag.PASSADA).exists())

    def test_padrao_nao_muda_nada_o_ciclo_le(self):
        class Chegou(Exception):
            pass
        with mock.patch.object(self.col, "ler_env", side_effect=Chegou):
            with self.assertRaises(Chegou):
                self.rodar()
        self.assertFalse((self.dados / ag.PASSADA).exists())

    def test_leitura_que_deixou_fila_marca_pendente(self):
        base = dict(dados=self.dados, config={"coletor": {"limite_fila": 500}}, agora=lambda: 1_800_000_000)
        for resumo, esperado in (({"status": "ok", "fila": 40, "novas": 3, "cortado": False}, False),
                                 ({"status": "ok", "fila": 40, "novas": 3, "cortado": True}, True),
                                 ({"status": "sem_novidade", "fila": 500, "novas": 0}, True),
                                 ({"status": "sem_novidade", "fila": 0, "novas": 0}, False)):
            with mock.patch.object(self.col, "_ciclo", return_value=resumo):
                self.col.ciclo(**base)
            gravado = json.loads((self.dados / "ultima-leitura.json").read_text())
            self.assertEqual((gravado["pendente"], gravado["em"]), (esperado, 1_800_000_000), resumo)
            self.assertEqual(ag.ultima_leitura(self.dados), (1_800_000_000.0, esperado))


class TestVigiaDeLeituraParada(ComPasta):
    """O aviso "leitura parada" (75 min acordado sem leitura) não pode disparar porque a pessoa escolheu ler menos."""

    def setUp(self):
        super().setUp()
        (self.dados / "cursor.json").write_text(json.dumps({"ultimo_seq": 0, "pareado_em": 1}))
        self.t = 1_800_000_000
        antigo = saude.NOTIFICAR
        saude.NOTIFICAR = lambda titulo, texto: None
        self.addCleanup(setattr, saude, "NOTIFICAR", antigo)

    def conferir(self):
        return saude.conferir_leitura(self.dados, agora=self.t, despertar=lambda: None, agente=lambda: True)

    def test_ciclo_que_rodou_e_nao_leu_conta_como_vivo(self):
        (self.dados / "ultima-leitura.json").write_text(json.dumps({"em": self.t - 3 * 3600, "status": "ok"}))
        ag.marcar_passada(self.dados, self.t - 20 * 60, "já leu neste intervalo (4 horas)")
        self.assertEqual(self.conferir(), "em_dia")
        self.assertEqual(alertas.abertos(self.dados), [])

    def test_ciclo_que_parou_de_rodar_continua_avisando(self):
        (self.dados / "ultima-leitura.json").write_text(json.dumps({"em": self.t - 6 * 3600, "status": "ok"}))
        ag.marcar_passada(self.dados, self.t - 80 * 60, "fora do horário de leitura (das 8h às 18h)")
        self.assertEqual(self.conferir(), "leitura-parada")
        self.assertEqual([a["codigo"] for a in alertas.abertos(self.dados)], ["leitura-parada"])


class TestTelas(Base):
    def setUp(self):
        super().setUp()
        self.dados.mkdir(parents=True, exist_ok=True)
        antigo = pf.ARQUIVO
        pf.ARQUIVO = self.dados / "preferencias.json"
        self.addCleanup(setattr, pf, "ARQUIVO", antigo)

    def test_ajustes_mostra_as_duas_preferencias_e_as_duas_frases(self):
        t = texto(ajustes.ROTAS["/ajustes"](self.req("GET", "/ajustes")))
        self.assertIn('<p class="rotulo">Leitura</p>', t)
        for rotulo in pf.LEITURA_MIN.values():
            self.assertIn(f">{rotulo}</label>", t)
        self.assertIn('name="leitura_min" value="30" checked', t)
        self.assertIn('<select name="leitura_de"', t)
        self.assertIn('<select name="leitura_ate"', t)
        self.assertEqual(t.count('<option value="0" selected>00:00</option>'), 2)
        self.assertIn("Intervalo maior gasta menos da sua conta de IA", t)
        self.assertIn("O fechamento do dia continua às 21h, uma passada de IA por dia", t)

    def test_ajustes_grava_e_recusa_o_que_esta_fora_da_lista(self):
        corpo = {**{k: str(v) for k, v in pf.PADRAO.items()}, "leitura_min": "120", "leitura_de": "8", "leitura_ate": "18"}
        self.assertEqual(ajustes.ROTAS["/ajustes"](self.req("POST", "/ajustes", corpo)).status, 303)
        self.assertEqual(ag.escolha(self.dados), (120, 8, 18))
        t = texto(ajustes.ROTAS["/ajustes"](self.req("GET", "/ajustes")))
        self.assertIn('name="leitura_min" value="120" checked', t)
        self.assertIn('<option value="8" selected>08:00</option>', t)
        self.assertIn('<option value="18" selected>18:00</option>', t)
        corpo.update(leitura_min="45", leitura_de="8", leitura_ate="25")
        ajustes.ROTAS["/ajustes"](self.req("POST", "/ajustes", corpo))
        self.assertEqual(ag.escolha(self.dados), (30, 0, 0))

    def test_trocar_outra_preferencia_mantem_a_leitura_e_vice_versa(self):
        """Regra do pedido 76 (trocar muda uma preferência e mantém as outras) vale para as três chaves novas."""
        corpo = {**{k: str(v) for k, v in pf.PADRAO.items()}, "leitura_min": "60", "leitura_de": "8", "leitura_ate": "0"}
        ajustes.ROTAS["/ajustes"](self.req("POST", "/ajustes", corpo))          # das 8h à meia-noite
        self.assertEqual(ag.escolha(self.dados), (60, 8, 0))
        pf.trocar("tema", "escuro")
        self.assertEqual(ag.escolha(self.dados), (60, 8, 0))
        pf.trocar("leitura_min", 240)
        self.assertEqual((pf.ler()["tema"], ag.escolha(self.dados)), ("escuro", (240, 8, 0)))

    def test_ler_agora_e_nova_conexao_deixam_o_sinal(self):
        self.ctx.executor = lambda cmd, cwd=None: None
        r = conexao.ROTAS["/conexao/ler-agora"](self.req("POST", "/conexao/ler-agora", {"volta": "/demandas"}))
        self.assertEqual(r.status, 303)
        self.assertTrue(ag.consumir_pedido(self.dados))
        self.assertTrue(conexao.disparar_leitura(self.ctx))                      # D-73: a leitura depois de parear
        self.assertTrue(ag.consumir_pedido(self.dados))

    def test_pausado_nao_deixa_sinal(self):
        (self.dados / "PAUSADO").write_text("")
        self.ctx.executor = lambda cmd, cwd=None: None
        conexao.ROTAS["/conexao/ler-agora"](self.req("POST", "/conexao/ler-agora"))
        self.assertFalse(conexao.disparar_leitura(self.ctx))
        self.assertFalse((self.dados / ag.SINAL).exists())

    def test_conexao_e_rodape_dizem_a_escolha(self):
        t = texto(conexao.ROTAS["/conexao"](self.req("GET", "/conexao")))
        self.assertIn("A cada 30 minutos com o Mac ligado", t)                   # padrão: a frase de sempre
        (self.dados / "preferencias.json").write_text(json.dumps({**pf.PADRAO, "leitura_min": 240, "leitura_de": 8,
                                                                  "leitura_ate": 18}))
        t = texto(conexao.ROTAS["/conexao"](self.req("GET", "/conexao")))
        self.assertIn("A cada 4 horas, das 8h às 18h com o Mac ligado", t)
        self.banco.registrar_lote(nome="l1", conversas=1, novas=1, itens=0, atencao=0)
        (self.dados / "ultima-leitura.json").write_text(json.dumps({"em": int(time.time())}))   # acabou de ler
        for frase in (conexao.proxima_leitura(self.ctx), demandas.proxima_leitura_segura(self.banco)):
            self.assertRegex(frase, r"^Próxima leitura (amanhã )?às (8|12|16):00$")

    def test_conector_informa_o_intervalo_e_o_horario(self):
        _, dados, _ = chamar(self.ctx, "greg_status")
        self.assertEqual((dados["leitura"]["a_cada"], dados["leitura"]["horario"]), ("30 minutos", "o dia todo"))
        (self.dados / "preferencias.json").write_text(json.dumps({**pf.PADRAO, "leitura_min": 60, "leitura_de": 22,
                                                                  "leitura_ate": 6}))
        _, dados, _ = chamar(self.ctx, "greg_status")
        self.assertEqual((dados["leitura"]["a_cada"], dados["leitura"]["horario"]), ("1 hora", "das 22h às 6h"))
        self.assertTrue(dados["leitura"]["proxima"].startswith("Próxima leitura"))


class TestVigiaSh(unittest.TestCase):
    def test_a_segunda_leitura_se_anuncia_e_o_agendador_nao_mudou(self):
        raiz = Path(__file__).resolve().parents[1]
        sh = (raiz / "bin" / "vigia.sh").read_text(encoding="utf-8")
        self.assertEqual(sh.count('VIGIA_SEGUNDA_LEITURA=1 "$PYTHON" "$COLETOR"'), 1)
        plist = next((raiz / "infra" / "launchagents").glob("*vigia-whatsapp.plist")).read_text(encoding="utf-8")
        self.assertEqual(plist.count("<key>Minute</key>"), 2)                   # o disparo continua em :00 e :30
        ag_py = (raiz / "bin" / "agendador.py").read_text(encoding="utf-8")
        self.assertIn('(None, 0, ["ciclo"])', ag_py)
        self.assertIn('(None, 30, ["ciclo"])', ag_py)


if __name__ == "__main__":
    unittest.main()
