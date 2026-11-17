"""Pedido 75 — pareamento sem tropeço: o histórico antigo não atrasa a leitura, o erro traz o botão que resolve,
o QR reinicia a Evolution sozinho, "Ler agora" em Conversas e leitura automática depois de parear."""
import unittest
from types import SimpleNamespace

from app import lotes
from app.erros import VigiaErro
from app.paginas import conexao as pg, conversas


class QRIndisponivel(VigiaErro):
    pass


class CxFalsa:
    def __init__(self, falhas_qr=1, reinicio_ok=True):
        self.falhas_qr, self.reinicio_ok, self.reinicios, self.qrs = falhas_qr, reinicio_ok, 0, 0
        self.estado = "ocioso"

    def iniciar_qr(self):
        self.qrs += 1
        if self.falhas_qr > 0:
            self.falhas_qr -= 1
            raise QRIndisponivel("sem qr")
        return {}

    def reiniciar(self, executor):
        self.reinicios += 1
        if not self.reinicio_ok:
            raise VigiaErro("não reiniciou")
        return 0


class TestFila(unittest.TestCase):
    def test_historico_antigo_fica_fora_ja_na_consulta(self):
        qs = []
        lotes.ler_fila(lambda q: qs.append(q) or ([["99999"]] if "last_value" in q else []), 2954, 500, desde=1794536840)
        self.assertIn('>= 1794536840', qs[0])
        self.assertIn('m."messageTimestamp" / 1000', qs[0])       # timestamp em milissegundos também é filtrado
        self.assertIn("f.seq > 2954", qs[0])

    def test_sem_desde_a_consulta_e_a_de_antes(self):
        qs = []
        lotes.ler_fila(lambda q: qs.append(q) or [], 0)
        self.assertNotIn("CASE WHEN", qs[0])

    def test_coletor_passa_o_limite_da_leitura(self):
        from pathlib import Path
        fonte = (Path(__file__).resolve().parents[1] / "bin" / "coletor.py").read_text(encoding="utf-8")
        self.assertIn('lotes.ler_fila(psql, cursor.ultimo_seq, int(cfg_x["limite_fila"]), desde=desde)', fonte)


class TestQRComReinicio(unittest.TestCase):
    def test_reinicia_sozinho_e_entrega_o_qr(self):
        cx = CxFalsa(falhas_qr=2)
        ctx = SimpleNamespace(conexao=cx, executor=lambda *a, **k: 0)
        t = [0.0]
        ok = pg._reiniciar_e_gerar_qr(ctx, dormir=lambda s: t.__setitem__(0, t[0] + s), relogio=lambda: t[0])
        self.assertTrue(ok)
        self.assertEqual((cx.reinicios, cx.qrs), (1, 3))

    def test_desiste_no_prazo_e_nao_levanta(self):
        cx = CxFalsa(falhas_qr=10**6)
        ctx = SimpleNamespace(conexao=cx, executor=lambda *a, **k: 0)
        t = [0.0]
        self.assertFalse(pg._reiniciar_e_gerar_qr(ctx, dormir=lambda s: t.__setitem__(0, t[0] + s), relogio=lambda: t[0]))
        self.assertEqual(cx.reinicios, 1)
        self.assertLessEqual(cx.qrs, pg.ESPERA_REINICIO_S // 5 + 1)

    def test_reinicio_que_falha_nao_tenta_qr(self):
        cx = CxFalsa(reinicio_ok=False)
        self.assertFalse(pg._reiniciar_e_gerar_qr(SimpleNamespace(conexao=cx, executor=lambda *a, **k: 0)))
        self.assertEqual(cx.qrs, 0)


class TestAvisosComBotao(unittest.TestCase):
    def _aviso(self, **monitor):
        cx = SimpleNamespace(janela_428_s=600, estado="ocioso")
        return pg._avisos_monitor(cx, {"atualizado_em": 1000, **monitor}, 1000)

    def test_401_manda_gerar_qr_e_nao_reiniciar(self):
        h = self._aviso(evento="queda", reason_code=401)
        self.assertIn('action="/conexao/qr"', h)
        self.assertIn("Reiniciar não resolve", h)
        self.assertNotIn('form="f-reiniciar"', h)

    def test_outras_quedas_e_428_trazem_o_botao_de_reiniciar(self):
        for h in (self._aviso(evento="queda", reason_code=408), self._aviso(evento="socket_morto")):
            self.assertIn('form="f-reiniciar">Reiniciar Evolution</button>', h)
            self.assertNotIn("menu ···", h)


class TestLeituraDoPareamento(unittest.TestCase):
    def test_so_o_painel_de_verdade_agenda_e_uma_vez_por_pareamento(self):
        ctx = SimpleNamespace(conexao=SimpleNamespace(pareou_em=123))
        pg._agendar_leitura_do_pareamento(ctx)                     # contexto de teste: não agenda
        self.assertFalse(hasattr(ctx, "leitura_do_pareamento"))
        criados = []
        real = pg.threading.Timer
        pg.threading.Timer = lambda s, f: criados.append(s) or SimpleNamespace(start=lambda: None, daemon=True)
        try:
            ctx.leitura_automatica = True
            pg._agendar_leitura_do_pareamento(ctx)
            pg._agendar_leitura_do_pareamento(ctx)
        finally:
            pg.threading.Timer = real
        self.assertEqual(criados, [pg.ESPERA_LEITURA_S])
        self.assertEqual(ctx.leitura_do_pareamento, 123)

    def test_ler_agora_no_menu_de_conversas(self):
        self.assertIn("vigiaPost('/conexao/ler-agora', {volta: '/conversas'})", conversas._JS)
        self.assertIn("/conversas", pg._VOLTA_LER)


class TestJanelaDepoisDeReparear(unittest.TestCase):
    """D-74: parear de novo não encolhe o que as telas mostram; a leitura da IA segue pareado_em − 1 dia."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.arq = Path(self.tmp.name) / "cursor.json"

    def test_repareamento_guarda_a_janela_antiga_so_para_as_telas(self):
        from app.cursor import Cursor, HISTORICO_S
        Cursor(self.arq, 10, 1_000_000).salvar()
        primeiro = Cursor.carregar(self.arq)
        self.assertIsNone(primeiro.janela_desde)
        self.assertEqual(primeiro.desde_tela, primeiro.desde)
        primeiro.pareado_em = 1_600_000                     # pareou de novo
        primeiro.salvar()
        depois = Cursor.carregar(self.arq)
        self.assertEqual(depois.desde, 1_600_000 - HISTORICO_S)          # a IA lê só o dia do pareamento novo
        self.assertEqual(depois.desde_tela, 1_000_000 - HISTORICO_S)     # as telas continuam de onde estavam
        depois.pareado_em = 2_000_000                       # terceiro pareamento: o limite mais antigo fica
        depois.salvar()
        self.assertEqual(Cursor.carregar(self.arq).desde_tela, 1_000_000 - HISTORICO_S)

    def test_avancar_o_cursor_nao_perde_a_janela_e_arquivo_antigo_continua_valendo(self):
        import json
        from app.cursor import Cursor, HISTORICO_S
        self.arq.write_text(json.dumps({"ultimo_seq": 5, "pareado_em": 900_000, "janela_desde": 100}), encoding="utf-8")
        c = Cursor(self.arq, 50, 900_000)                   # como o coletor: objeto novo, sem saber da janela
        c.salvar()
        lido = Cursor.carregar(self.arq)
        self.assertEqual((lido.ultimo_seq, lido.janela_desde, lido.desde_tela), (50, 100, 100))
        self.arq.write_text(json.dumps({"ultimo_seq": 5, "pareado_em": 900_000, "janela_desde": "x"}), encoding="utf-8")
        self.assertEqual(Cursor.carregar(self.arq).desde_tela, 900_000 - HISTORICO_S)
