"""Contrato executável da estação ponte (E7): bin/vigia-ponte e app/ponte/{classificar,atualizar,frentes}.py.
Quem chama: unittest. Usa Banco (SQLite temporário) e FrentesPonte contra FakeFrentes.
O que não faz: não toca Evolution.
"""
import argparse
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from testes.fakes import FakeFrentes  # noqa: E402
from testes.util_contrato import RAIZ, _arquivo, _importar  # noqa: E402


def args_classificar(msg_id, classe, resumo="resumo", **kw):
    base = dict(msg_id=msg_id, classe=classe, resumo=resumo, conversa="55@s.whatsapp.net", contato="Maria",
                ts=1_700_000_100, frente=1, prazo=None, confianca="alta", imagem=None)
    base.update(kw)
    return argparse.Namespace(**base)


class TestContratoPonte(unittest.TestCase):
    def setUp(self):
        self.classificar = _importar("app.ponte.classificar", "ponte")
        self.atualizar = _importar("app.ponte.atualizar", "ponte")
        self.frentes_cmd = _importar("app.ponte.frentes", "ponte")
        banco = _importar("app.banco", "banco")
        fp = _importar("app.frentes_ponte", "frentes_ponte")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = FakeFrentes().iniciar()
        self.addCleanup(self.fake.parar)
        self.banco = banco.Banco(str(Path(self.tmp.name) / "vigia.db"))
        self.ponte = fp.FrentesPonte(self.fake.url, self.fake.token)

    def _cls(self, *a, **k):
        return self.classificar.executar(args_classificar(*a, **k), banco=self.banco, ponte=self.ponte)

    def test_atencao_baixa_confianca_captura_e_sobe_confirmado(self):   # D-58: nada nasce "a confirmar"
        r = self._cls("m1", "atencao", confianca="baixa")
        self.assertIsInstance(r, dict)
        self.assertEqual(len(self.fake.capturas), 1)
        self.assertEqual(self.fake.capturas[0]["origem"], "whatsapp")
        self.assertEqual([p for p in self.fake.patches if "confirmado" in p["corpo"]], [])
        self.assertEqual(self.fake.itens[r["item_id"]]["confirmado"], 1)
        self.assertEqual(self.banco.classificacao(r["id"])["confianca"], "baixa")   # fica como registro

    def test_alta_confianca_nao_gera_patch(self):
        self._cls("m1", "atencao", confianca="alta")
        self.assertEqual(len(self.fake.capturas), 1)
        self.assertEqual(self.fake.patches, [])

    def test_demanda_fica_provisoria_ate_o_fechamento_do_dia(self):
        """D-57 (pedido 45): demanda/lembrar sem prazo em 24 h não sobe na hora; o cartão fica provisório no SQLite."""
        r = self._cls("m1", "demanda", confianca="alta")
        self.assertEqual(self.fake.capturas, [])
        self.assertEqual((r["cartao_item"], r["provisorio"], r["lancado"], r["item_id"]), (True, True, False, None))
        reg = self.banco.classificacoes()[0]
        self.assertEqual((reg["item_id"], reg["lancado_em"]), (None, None))
        self.assertEqual(len(self.banco.provisorias()), 1)

    def test_aviso_nao_captura(self):
        self._cls("m2", "aviso")
        self.assertEqual(self.fake.capturas, [])
        self.assertEqual(len(self.banco.classificacoes()), 1)

    def test_mesmo_msg_id_duas_vezes_uma_captura(self):
        a = self._cls("m3", "atencao")
        b = self._cls("m3", "atencao")
        self.assertEqual(len(self.fake.capturas), 1)
        self.assertEqual(len(self.banco.classificacoes()), 1)
        self.assertIsInstance(b, dict)
        self.assertIsInstance(a, dict)

    def test_classificar_aceita_substitui_e_cancela(self):   # D-57
        sub = argparse.ArgumentParser().add_subparsers()
        self.classificar.configurar(sub)
        ns = sub.choices["classificar"].parse_args(["m", "lembrar", "r", "--substitui", "3", "--confianca", "alta"])
        self.assertEqual((ns.substitui, ns.cancela), (3, None))
        ns = sub.choices["classificar"].parse_args(["m", "aviso", "r", "--cancela", "4", "--confianca", "alta"])
        self.assertEqual((ns.substitui, ns.cancela), (None, 4))
        velho = self._cls("m1", "atencao")
        r = self._cls("m2", "lembrar", substitui=velho["id"])
        self.assertEqual((r["substituiu"], r["item_id"]), (velho["id"], velho["item_id"]))
        self.assertEqual(self.banco.classificacao(velho["id"])["substituida_por"], r["id"])

    def test_consolidar_existe_e_funde_provisorios(self):   # D-57
        consolidar = _importar("app.ponte.consolidar", "ponte")
        sub = argparse.ArgumentParser().add_subparsers()
        consolidar.configurar(sub)
        a = self._cls("m1", "demanda")["id"]
        b = self._cls("m2", "demanda")["id"]
        ns = sub.choices["consolidar"].parse_args([str(b), "--absorve", str(a)])
        r = consolidar.executar(ns, banco=self.banco, ponte=self.ponte)
        self.assertIsInstance(r, dict)
        self.assertEqual(r["absorvidos"], [a])
        self.assertEqual(self.banco.classificacao(a)["substituida_por"], b)
        self.assertEqual(self.fake.capturas, [])

    def test_atualizar_anota(self):
        item = self.ponte.capturar("x", frente_id=1)["itens"][0]
        ns = argparse.Namespace(item_id=item["id"], nota="atualizado", prioridade=1)
        r = self.atualizar.executar(ns, banco=self.banco, ponte=self.ponte)
        self.assertIsInstance(r, dict)
        self.assertNotIn("nota", self.fake.patches[-1]["corpo"])  # D-13
        self.assertEqual(self.fake.patches[-1]["corpo"]["prioridade"], 1)

    def test_frentes_lista(self):
        r = self.frentes_cmd.executar(argparse.Namespace(), banco=self.banco, ponte=self.ponte)
        self.assertIsInstance(r, dict)

    def test_cli_help_e_sem_token_de_envio(self):
        cli = _arquivo("bin/vigia-ponte", "ponte")
        r = subprocess.run([sys.executable, str(cli), "--help"], capture_output=True, text=True, timeout=30,
                           stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0)
        for arq in [cli, *sorted((RAIZ / "app/ponte").glob("*.py"))]:
            self.assertNotIn("VIGIA_TOKEN_ENVIO", arq.read_text(encoding="utf-8"), arq.name)


if __name__ == "__main__":
    unittest.main()
