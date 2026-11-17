"""Pedido 62: login do Claude vencido deixa de falhar calado. O coletor reconhece o motivo, não reduz o lote por causa
dele e o painel diz o que fazer."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app import lotes
from app.paginas import _comum as c

RAIZ = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("coletor_p62", RAIZ / "bin" / "coletor.py")
coletor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(coletor)


class MotivoDaFalha(unittest.TestCase):
    def test_sessao_vencida_vira_erro_proprio_com_a_saida(self):
        saida = {"type": "result", "is_error": True, "subtype": "success",
                 "result": "Failed to authenticate: OAuth session expired and could not be refreshed"}
        with self.assertRaises(lotes.ClaudeSemLogin) as e:
            lotes._resultado_final(saida)
        self.assertIn("claude auth login", str(e.exception))
        self.assertIsInstance(e.exception, lotes.ErroClaude)

    def test_outro_erro_leva_o_motivo_curto_e_nao_e_login(self):
        saida = {"type": "result", "is_error": True, "subtype": "success", "result": "Credit balance is too low " + "x" * 400}
        with self.assertRaises(lotes.ErroClaude) as e:
            lotes._resultado_final(saida)
        self.assertNotIsInstance(e.exception, lotes.ClaudeSemLogin)
        self.assertIn("Credit balance is too low", str(e.exception))
        self.assertLess(len(str(e.exception)), 200)


class FalhaAnotada(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.d, True)
        self.notas = []
        antigo = coletor.NOTIFICAR
        coletor.NOTIFICAR = lambda titulo, texto: self.notas.append((titulo, texto))
        self.addCleanup(setattr, coletor, "NOTIFICAR", antigo)

    def test_anotar_falha_nao_notifica_mais_quem_avisa_e_a_central(self):
        # lote 2 do pedido 63: a notificação saiu do coletor; app/saude.do_coletor abre o aviso e notificar() avisa
        coletor._anotar_falha(self.d, 5, login=True)
        coletor._anotar_falha(self.d, 9)
        self.assertEqual(self.notas, [])

    def test_login_nao_conta_para_reduzir_o_lote_e_guarda_desde_quando(self):
        coletor._anotar_falha(self.d, 2640, login=True)
        primeiro = json.loads((self.d / "falhas.json").read_text())
        coletor._anotar_falha(self.d, 2640, login=True)
        v = json.loads((self.d / "falhas.json").read_text())
        self.assertEqual((v["n"], v["motivo"], v["desde"]), (0, "login", primeiro["desde"]))
        self.assertEqual(coletor._falhas_da_faixa(self.d, 2640), 0)

    def test_falha_comum_continua_contando(self):
        coletor._anotar_falha(self.d, 7)
        coletor._anotar_falha(self.d, 7)
        self.assertEqual(coletor._falhas_da_faixa(self.d, 7), 2)
        self.assertEqual(json.loads((self.d / "falhas.json").read_text())["motivo"], "")


class AvisoNaTela(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.d, True)
        self.ctx = SimpleNamespace(config={"dados_dir": str(self.d)}, raiz=self.d)

    def _aviso(self):
        import unittest.mock as mock
        with mock.patch.object(c, "dados_dir", lambda ctx: self.d):
            return c.aviso_leitura(self.ctx)

    def test_sem_falha_nao_ha_aviso(self):
        self.assertEqual(self._aviso(), "")

    def test_login_vencido_avisa_o_que_fazer(self):
        from app import alertas
        alertas.abrir(self.d, "ia-login")
        t = self._aviso()
        self.assertIn("O login do Claude neste Mac expirou", t)
        self.assertIn("claude auth login", t)

    def test_falha_passageira_da_ia_ou_central_torta_nao_avisa(self):
        from app import alertas
        alertas.abrir(self.d, "ia-erro")
        alertas.abrir(self.d, "ia-erro")
        self.assertEqual(self._aviso(), "")
        (self.d / "alertas.json").write_text("{torto")
        self.assertEqual(self._aviso(), "")

    def test_ler_agora_diz_o_erro_na_hora(self):
        import unittest.mock as mock
        from app.paginas import conexao
        with mock.patch.object(c, "dados_dir", lambda ctx: self.d):
            self.assertEqual(conexao.toast_ler(self.ctx), conexao.TOAST_LENDO)
            from app import alertas
            alertas.abrir(self.d, "ia-login")
            self.assertEqual(conexao.toast_ler(self.ctx), conexao.TOAST_SEM_LOGIN)
            alertas.fechar(self.d, "ia-login")
            alertas.abrir(self.d, "ia-limite")
            self.assertIn("limite de uso", conexao.toast_ler(self.ctx))

    def test_notificacao_do_mac_so_leva_texto_seguro(self):
        import unittest.mock as mock
        with mock.patch.object(coletor.shutil, "which", lambda n: "/usr/bin/osascript"), \
                mock.patch.dict("os.environ", {"VIGIA_SEM_NOTIFICACAO": "0"}), \
                mock.patch.object(coletor.subprocess, "run") as run:
            coletor._notificar_mac('Greg "x"', 'rode: claude auth login" & do shell script "id')
        script = run.call_args[0][0][2]
        self.assertEqual(script.count('"'), 6)          # só as aspas do próprio comando
        self.assertNotIn("&", script)

    def test_o_aviso_aparece_na_faixa_de_todas_as_telas(self):
        # o inline de Demandas e Conexão deu lugar à faixa da base (pedido 63, lote 2)
        import unittest.mock as mock
        from app import alertas
        alertas.abrir(self.d, "ia-login")
        with mock.patch.object(c, "dados_dir", lambda ctx: self.d):
            f = c.faixa_avisos(self.ctx)
        self.assertIn("O Greg parou de ler as conversas", f)
        self.assertIn('href="/avisos"', f)
        self.assertIn('id="faixa-avisos"', (RAIZ / "app/templates/_base.html").read_text())
        self.assertNotIn("c.aviso_leitura(ctx)", (RAIZ / "app/paginas/demandas.py").read_text())


if __name__ == "__main__":
    unittest.main()
