"""Contrato executável da estação banco (E3): app/banco.py e app/cursor.py.
Quem chama: unittest.
O que não faz: não depende de Evolution nem Frentes.
"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from testes.util_contrato import RAIZ, _importar  # noqa: E402


class TestContratoBanco(unittest.TestCase):
    def setUp(self):
        self.mod = _importar("app.banco", "banco")
        self.cur = _importar("app.cursor", "banco")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.caminho = str(Path(self.tmp.name) / "vigia.db")
        self.b = self.mod.Banco(self.caminho)

    def _tabelas(self):
        con = sqlite3.connect(self.caminho)
        try:
            return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        finally:
            con.close()

    def test_migracao_001_existe_e_cria_tabelas(self):
        self.assertTrue((RAIZ / "app/migracoes/001_inicial.sql").exists())
        self.assertTrue({"classificacoes", "lotes", "qr_eventos", "envios", "lapides", "conversas"} <= self._tabelas())

    def test_migrar_duas_vezes_e_idempotente(self):
        antes = self._tabelas()
        r1 = self.b.migrar()
        r2 = self.b.migrar()
        self.assertIsInstance(r1, list)
        self.assertIsInstance(r2, list)
        self.assertEqual(self._tabelas(), antes)
        self.mod.Banco(self.caminho)  # reabrir também não quebra

    def test_classificacao_roundtrip(self):
        i = self.b.gravar_classificacao(msg_id="m1", conversa="55@s.whatsapp.net", contato="Maria", ts=1000,
                                        classe="atencao", resumo="cobrar", confianca="alta", frente_id=1)
        self.assertIsInstance(i, int)
        self.b.gravar_classificacao(msg_id="m2", conversa="56@s.whatsapp.net", contato="João", ts=1001,
                                    classe="aviso", resumo="aviso", confianca="baixa")
        todas = self.b.classificacoes()
        self.assertEqual(len(todas), 2)
        self.assertEqual(len(self.b.classificacoes(classe="aviso")), 1)
        self.assertEqual(len(self.b.classificacoes(conversa="55@s.whatsapp.net")), 1)
        self.assertEqual(len(self.b.ultimas_da_conversa("55@s.whatsapp.net", 10)), 1)

    def test_lotes_envios_lapides(self):
        self.assertIsInstance(self.b.registrar_lote(nome="l1", conversas=1, novas=2, itens=1, atencao=0), int)
        self.assertIsInstance(self.b.registrar_envio(para="5592000000000", hash_texto="h", por="dono"), int)
        self.b.gravar_lapide(semana="2026-W45", n_msgs=10, hash_relatorio="h", apagado_em=1000)
        self.assertEqual(len(self.b.lapides()), 1)

    def test_qrs_hoje(self):
        self.assertEqual(self.b.qrs_hoje(), 0)
        self.assertEqual(self.b.registrar_qr("clique"), 1)
        self.assertEqual(self.b.registrar_qr("renovacao"), 2)
        self.assertEqual(self.b.qrs_hoje(), 2)

    def test_qrs_hoje_ignora_dias_anteriores(self):
        self.b.registrar_qr("clique")
        self.b.registrar_qr("clique")
        con = sqlite3.connect(self.caminho)
        try:
            colunas = [(r[1], r[2].upper()) for r in con.execute("PRAGMA table_info(qr_eventos)")]
            alvo = next(((n, t) for n, t in colunas if n in ("criado_em", "em", "ts", "quando", "gerado_em")), None)
            if alvo is None:
                self.fail("qr_eventos precisa de uma coluna de instante (criado_em/em/ts/quando/gerado_em)")
            valor = 946684800 if "INT" in alvo[1] or "REAL" in alvo[1] else "2000-01-01 12:00:00"
            con.execute(f"UPDATE qr_eventos SET {alvo[0]}=? WHERE rowid=(SELECT MIN(rowid) FROM qr_eventos)", (valor,))
            con.commit()
        finally:
            con.close()
        self.assertEqual(self.b.qrs_hoje(), 1)

    def test_conversas_recentes(self):
        import time
        self.b.gravar_classificacao(msg_id="m1", conversa="55@s.whatsapp.net", contato="Maria",
                                    ts=int(time.time()), classe="demanda", resumo="x", confianca="alta")
        r = self.b.conversas_recentes(7)
        self.assertEqual(len(r), 1)
        for k in ("conversa", "contato", "n_novas", "ultima_classe", "ultima_em"):
            self.assertIn(k, r[0])


class TestContratoCursor(unittest.TestCase):
    def setUp(self):
        self.cur = _importar("app.cursor", "banco")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.caminho = Path(self.tmp.name) / "cursor.json"

    def test_avancar_so_cresce(self):
        c = self.cur.Cursor.carregar(self.caminho)
        self.assertEqual(c.ultimo_seq, 0)
        self.assertIsNone(c.pareado_em)
        c.avancar(5)
        c.avancar(3)
        self.assertEqual(c.ultimo_seq, 5)

    def test_salvar_e_carregar(self):
        c = self.cur.Cursor.carregar(self.caminho)
        c.avancar(7)
        c.pareado_em = 1_700_000_000
        c.salvar()
        d = self.cur.Cursor.carregar(self.caminho)
        self.assertEqual((d.ultimo_seq, d.pareado_em), (7, 1_700_000_000))


if __name__ == "__main__":
    unittest.main()
