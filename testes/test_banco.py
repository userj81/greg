"""Teste próprio da estação E3: Banco e Cursor.
Quem chama: unittest.
O que não faz: não toca rede nem arquivos fora de um diretório temporário.
"""
import json
import multiprocessing
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.banco import TRANSCRICAO_MAX, Banco  # noqa: E402

# Todas as migrações versionadas, em ordem (002 = apelidos, r02).
TODAS = sorted(p.name for p in (Path(__file__).resolve().parents[1] / "app" / "migracoes").glob("*.sql"))
from app.cursor import Cursor  # noqa: E402
from app.erros import CursorCorrompido  # noqa: E402


class TestBanco(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.caminho = str(Path(self.tmp.name) / "sub" / "v.db")
        self.b = Banco(self.caminho)

    def test_migrar_duas_vezes(self):
        self.assertEqual(self.b.migrar(), [])
        self.assertEqual(self.b.migrar(), [])
        self.assertEqual(Banco(self.caminho).migrar(), [])
        con = sqlite3.connect(self.caminho)
        self.assertEqual(con.execute("SELECT nome FROM _migracoes ORDER BY nome").fetchall(), [(n,) for n in TODAS])
        con.close()

    def test_primeira_migracao_devolve_nome(self):
        b = Banco.__new__(Banco)
        b.caminho = str(Path(self.tmp.name) / "novo.db")
        self.assertEqual(b.migrar(), TODAS)

    def test_qrs_hoje_com_evento_de_ontem(self):
        ontem = (datetime.now() - timedelta(days=1)).isoformat(sep=" ", timespec="seconds")
        con = sqlite3.connect(self.caminho)
        con.execute("INSERT INTO qr_eventos (origem, criado_em) VALUES ('clique', ?)", (ontem,))
        con.commit(); con.close()
        self.assertEqual(self.b.qrs_hoje(), 0)
        self.assertEqual(self.b.registrar_qr("clique"), 1)

    def test_reclassificar_mesmo_msg_id_nao_duplica(self):
        a = self.b.gravar_classificacao(msg_id="m", conversa="c", contato="x", ts=1, classe="aviso", resumo="r", confianca="alta")
        c = self.b.gravar_classificacao(msg_id="m", conversa="c", contato="x", ts=1, classe="demanda", resumo="r", confianca="alta")
        self.assertEqual(a, c)
        self.assertEqual([r["classe"] for r in self.b.classificacoes()], ["demanda"])

    def test_conversas_recentes(self):
        agora = int(time.time())
        for i, (conv, ts, cl) in enumerate([("a", agora - 100, "aviso"), ("a", agora - 10, "demanda"),
                                            ("b", agora - 5, "atencao"), ("velha", agora - 30 * 86400, "aviso")]):
            self.b.gravar_classificacao(msg_id=f"m{i}", conversa=conv, contato=conv.upper(), ts=ts, classe=cl, resumo="r", confianca="alta")
        r = {x["conversa"]: x for x in self.b.conversas_recentes(7)}
        self.assertEqual(set(r), {"a", "b"})
        self.assertEqual((r["a"]["n_novas"], r["a"]["ultima_classe"], r["a"]["ultima_em"]), (2, "demanda", agora - 10))
        self.assertEqual(self.b.conversas_recentes(7)[0]["conversa"], "b")

    def test_view_conversas(self):
        self.b.gravar_classificacao(msg_id="m", conversa="c", contato="X", ts=5, classe="aviso", resumo="r", confianca="alta")
        con = sqlite3.connect(self.caminho)
        self.assertEqual(con.execute("SELECT conversa, n_novas, ultima_classe FROM conversas").fetchall(), [("c", 1, "aviso")])
        con.close()


class TestCursor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p = Path(self.tmp.name) / "d" / "cursor.json"

    def test_monotonico(self):
        c = Cursor.carregar(self.p)
        for s in (5, 3, 5, 9, 1):
            c.avancar(s)
        self.assertEqual(c.ultimo_seq, 9)

    def test_persistencia_atomica_sem_sobras(self):
        c = Cursor.carregar(self.p)
        c.avancar(4); c.pareado_em = 123
        c.salvar()
        self.assertEqual(json.loads(self.p.read_text()), {"ultimo_seq": 4, "pareado_em": 123})
        self.assertEqual(sorted(x.name for x in self.p.parent.iterdir()), ["cursor.json", "cursor.lock"])

    def test_ausente_e_zero(self):
        c = Cursor.carregar(self.p)
        self.assertEqual((c.ultimo_seq, c.pareado_em), (0, None))

    def test_corrompido_falha_fechado_e_preserva(self):
        for conteudo in ("{quebrado", "", "[1,2]", '{"ultimo_seq": "x"}', '{"ultimo_seq": 5'):
            with self.subTest(conteudo=conteudo):
                self.p.parent.mkdir(parents=True, exist_ok=True)
                self.p.write_text(conteudo)
                with self.assertRaises(CursorCorrompido):
                    Cursor.carregar(self.p)
                self.assertFalse(self.p.exists())
                guardados = [x for x in self.p.parent.iterdir() if ".corrompido-" in x.name]
                self.assertTrue(guardados)
                self.assertEqual(guardados[-1].read_text(), conteudo)
                for x in guardados:
                    x.unlink()

    def test_salvar_merge_nao_regride(self):
        a = Cursor.carregar(self.p); b = Cursor.carregar(self.p)
        a.avancar(10); a.definir_pareado_em(111); a.salvar()
        b.avancar(4); b.salvar()  # b atrasado e sem pareado_em
        d = Cursor.carregar(self.p)
        self.assertEqual((d.ultimo_seq, d.pareado_em), (10, 111))
        self.assertEqual((b.ultimo_seq, b.pareado_em), (10, 111))

    def test_dois_processos_salvando(self):
        ctx = multiprocessing.get_context("fork")
        ps = [ctx.Process(target=_salvar_muitos, args=(str(self.p), base)) for base in (0, 1000)]
        for pr in ps: pr.start()
        for pr in ps: pr.join(60)
        self.assertEqual([pr.exitcode for pr in ps], [0, 0])
        self.assertEqual(Cursor.carregar(self.p).ultimo_seq, 1000 + 49)


def _salvar_muitos(caminho, base):
    for i in range(50):
        c = Cursor.carregar(caminho) if os.path.exists(caminho) else Cursor(caminho)
        c.avancar(base + i)
        c.salvar()


def _migrar(caminho):
    b = Banco.__new__(Banco)
    b.caminho = caminho
    r = b.migrar()   # com 2+ migrações, os processos podem dividir entre si: cada uma aplicada por um só
    os._exit(0 if r == sorted(r) and set(r) <= set(TODAS) else 1)


class TestEmendaF4(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.caminho = str(Path(self.tmp.name) / "dados" / "v.db")
        self.b = Banco(self.caminho)
        self.t = int(time.time())

    def _g(self, msg_id="m", **kw):
        base = dict(msg_id=msg_id, conversa="c", contato="x", ts=self.t, classe="aviso", resumo="r", confianca="alta")
        base.update(kw)
        return self.b.gravar_classificacao(**base)

    def test_upsert_coalesce_nao_apaga_vinculo(self):
        i = self._g(item_id=7, frente_id=3, imagem="a.jpg", prazo="2026-11-10")
        self.assertEqual(self._g(classe="demanda"), i)
        r = self.b.classificacao(i)
        self.assertEqual((r["item_id"], r["frente_id"], r["imagem"], r["prazo"], r["classe"]), (7, 3, "a.jpg", "2026-11-10", "demanda"))
        self._g(item_id=9)
        self.assertEqual(self.b.classificacao_por_msg_id("m")["item_id"], 9)
        self.assertIsNone(self.b.classificacao_por_msg_id("nao"))
        self.assertIsNone(self.b.classificacao(9999))

    def test_ate_e_lotes(self):
        self._g("a", ts=self.t - 100); self._g("b", ts=self.t)
        self.assertEqual([r["msg_id"] for r in self.b.classificacoes(ate=self.t - 50)], ["a"])
        for n in range(25):
            self.b.registrar_lote(nome=f"l{n}", conversas=1, novas=1, itens=0, atencao=0)
        ls = self.b.lotes()
        self.assertEqual((len(ls), ls[0]["nome"]), (20, "l24"))
        self.assertEqual(len(self.b.lotes(limite=3)), 3)

    def test_decidir(self):
        i = self._g()
        self.assertIsNone(self.b.classificacao(i)["decisao"])
        self.b.decidir(i, "confirmado")
        self.assertEqual(self.b.classificacao(i)["decisao"], "confirmado")
        with self.assertRaises(ValueError):
            self.b.decidir(i, "talvez")

    def test_lapide_unica_por_semana(self):
        a = self.b.gravar_lapide(semana="2026-W45", n_msgs=1, hash_relatorio="h", apagado_em=1)
        b = self.b.gravar_lapide(semana="2026-W45", n_msgs=2, hash_relatorio="h2", apagado_em=2)
        self.assertEqual(a, b)
        self.assertEqual(len(self.b.lapides()), 1)

    def test_ts_validado(self):
        for ruim in (self.t * 1000, "123", 1.5, None, True, -1, 4102444801):
            with self.subTest(ts=ruim), self.assertRaises(ValueError):
                self._g(ts=ruim)
        self.assertEqual(self.b.classificacoes(), [])

    def test_resumo_truncado(self):
        i = self._g(resumo="a" * 500)
        r = self.b.classificacao(i)["resumo"]
        self.assertEqual((len(r), r[-1]), (298, "…"))
        self.assertEqual(self.b.classificacao(self._g("m2", resumo="b" * 300))["resumo"], "b" * 300)

    def test_permissoes(self):
        self.assertEqual(os.stat(self.caminho).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(Path(self.caminho).parent).st_mode & 0o777, 0o700)

    def test_migracao_com_erro_faz_rollback(self):
        import app.banco as m
        mig = Path(self.tmp.name) / "mig"; mig.mkdir()
        (mig / "001_ok.sql").write_text("CREATE TABLE IF NOT EXISTS t1 (a INTEGER);")
        (mig / "002_ruim.sql").write_text("CREATE TABLE t2 (a INTEGER); INSERT INTO nao_existe VALUES (1);")
        antigo = m.MIGRACOES
        m.MIGRACOES = mig
        try:
            b = Banco.__new__(Banco); b.caminho = str(Path(self.tmp.name) / "r.db")
            with self.assertRaises(sqlite3.Error):
                b.migrar()
        finally:
            m.MIGRACOES = antigo
        con = sqlite3.connect(b.caminho)
        self.assertEqual([r[0] for r in con.execute("SELECT nome FROM _migracoes")], ["001_ok.sql"])
        self.assertEqual(con.execute("SELECT count(*) FROM sqlite_master WHERE name='t2'").fetchone()[0], 0)
        con.close()

    def test_corrida_primeira_migracao(self):
        ctx = multiprocessing.get_context("fork")
        alvo = str(Path(self.tmp.name) / "corrida.db")
        ps = [ctx.Process(target=_migrar, args=(alvo,)) for _ in range(4)]
        for pr in ps: pr.start()
        for pr in ps: pr.join(60)
        self.assertEqual([pr.exitcode for pr in ps], [0] * 4)
        con = sqlite3.connect(alvo)
        self.assertEqual(con.execute("SELECT count(*) FROM _migracoes").fetchone()[0], len(TODAS))
        con.close()

    def test_qr_epoch_e_criado_em_iso(self):
        self.b.registrar_qr("clique")
        con = sqlite3.connect(self.caminho)
        criado, epoch = con.execute("SELECT criado_em, epoch FROM qr_eventos").fetchone()
        con.close()
        self.assertEqual(len(criado), 19)
        self.assertLess(abs(epoch - time.time()), 5)


class TestFaseA(unittest.TestCase):
    """D-45: colunas de custo/duração no lote, rotulagem (classe_curada), conexao_eventos e as consultas de /metricas."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.b = Banco(str(Path(self.tmp.name) / "v.db"))
        self.b.migrar()

    def classificar(self, msg_id, classe, **kw):
        base = dict(msg_id=msg_id, conversa="c@s.whatsapp.net", contato="A", ts=int(time.time()), classe=classe,
                    resumo="r", confianca="alta")
        base.update(kw)
        return self.b.gravar_classificacao(**base)

    def test_lote_com_e_sem_custo(self):
        self.b.registrar_lote(nome="a", conversas=1, novas=1, itens=0, atencao=0, tokens_in=1, tokens_out=1, modelo="sonnet",
                              custo_usd=0.0123, duracao_ms=4200, turnos=3)
        self.b.registrar_lote(nome="b", conversas=1, novas=1, itens=0, atencao=0)
        b, a = self.b.lotes(2)
        self.assertEqual((a["custo_usd"], a["duracao_ms"], a["turnos"]), (0.0123, 4200, 3))
        self.assertEqual((b["custo_usd"], b["duracao_ms"], b["turnos"]), (None, None, None))
        dia = self.b.metricas_lotes_por_dia(14)[0]
        self.assertEqual((dia["lotes"], dia["custo_usd"], dia["com_custo"], dia["duracao_ms"]), (2, 0.0123, 1, 4200.0))

    def test_curar_grava_a_classe_certa_e_valida(self):
        cid = self.classificar("m1", "demanda")
        self.b.curar(cid, "aviso")
        reg = self.b.classificacao(cid)
        self.assertEqual((reg["classe"], reg["classe_curada"]), ("demanda", "aviso"))
        self.assertRegex(reg["curado_em"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.b.curar(cid, "demanda")   # pode corrigir o rótulo
        self.assertEqual(self.b.classificacao(cid)["classe_curada"], "demanda")
        with self.assertRaises(ValueError):
            self.b.curar(cid, "urgente")

    def test_metricas_curadoria(self):
        a = self.classificar("a", "demanda")
        b = self.classificar("b", "aviso", item_id=7)
        self.classificar("c", "sem_acao")
        self.b.curar(a, "demanda")
        self.b.curar(b, "sem_acao")
        self.b.decidir(b, "recusado")
        m = self.b.metricas_curadoria(14)
        self.assertEqual((m["total"], m["curadas"], m["acertos"], m["itens"], m["confirmados"], m["recusados"]), (3, 2, 1, 1, 0, 1))
        self.assertEqual(m["matriz"], [{"classe": "aviso", "classe_curada": "sem_acao", "n": 1},
                                       {"classe": "demanda", "classe_curada": "demanda", "n": 1}])
        self.assertEqual([(r["classe"], r["n"], r["curadas"], r["acertos"]) for r in m["por_classe"]],
                         [("aviso", 1, 1, 0), ("demanda", 1, 1, 1), ("sem_acao", 1, 0, 0)])
        vazio = Banco(str(Path(self.tmp.name) / "vazio.db"))
        vazio.migrar()
        self.assertEqual(vazio.metricas_curadoria(14)["total"], 0)

    def test_eventos_conexao_respeitam_a_janela(self):
        self.b.registrar_evento_conexao(evento="queda", estado="close", motivo="caiu", reason_code=428)
        self.b.registrar_evento_conexao(evento="voltou", estado="open")
        con = sqlite3.connect(self.b.caminho)
        con.execute("INSERT INTO conexao_eventos (evento, estado, criado_em) VALUES ('queda','close', datetime('now','localtime','-40 days'))")
        con.commit()
        con.close()
        ev = self.b.eventos_conexao(dias=14)
        self.assertEqual([e["evento"] for e in ev], ["voltou", "queda"])
        self.assertEqual(ev[1]["reason_code"], 428)
        self.assertEqual(len(self.b.eventos_conexao(dias=90)), 3)

    def test_qrs_e_envios_por_dia(self):
        self.b.registrar_qr("clique")
        self.b.registrar_qr("renovacao")
        self.b.registrar_envio(para="x@s.whatsapp.net", hash_texto="h", por="api:claude-code")
        self.b.registrar_envio(para="y@s.whatsapp.net", hash_texto="h", por="api:painel")
        self.assertEqual(self.b.qrs_por_dia(14)[0]["n"], 2)
        e = self.b.envios_por_dia(14)[0]
        self.assertEqual((e["n"], e["api"]), (2, 1))


class TestTranscricoes(unittest.TestCase):
    """Migração 005 (D-47): o único texto de mensagem no SQLite, por msg_id, apagado por janela de ts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.b = Banco(str(Path(self.tmp.name) / "v.db"))

    def gravar(self, mid, ts=1_700_000_000, estado="ok", **kw):
        self.b.gravar_transcricao(msg_id=mid, conversa="c@s.whatsapp.net", ts=ts, estado=estado, **kw)

    def test_upsert_soma_tentativas_e_valida(self):
        self.gravar("m1", estado="falhou", segundos=40)
        self.gravar("m1", estado="ok", texto="oi", segundos=40, segundos_lidos=40, motor="mlx", tempo_ms=120)
        t = self.b.transcricoes(["m1"])["m1"]
        self.assertEqual((t["estado"], t["texto"], t["tentativas"], t["segundos_lidos"], t["motor"], t["tempo_ms"]),
                         ("ok", "oi", 2, 40, "mlx", 120))
        self.assertTrue(t["em"])
        with self.assertRaises(ValueError):
            self.gravar("m2", estado="pendente")
        with self.assertRaises(ValueError):
            self.gravar("m2", ts="ontem")
        self.gravar("m3", texto="x" * (TRANSCRICAO_MAX + 10))
        self.assertEqual(len(self.b.transcricoes(["m3"])["m3"]["texto"]), TRANSCRICAO_MAX)

    def test_por_ids_em_blocos_e_ids_desconhecidos(self):
        for i in range(900):
            self.gravar(f"m{i}")
        ids = [f"m{i}" for i in range(900)] + ["nada", "", None, "m1"]
        self.assertEqual(len(self.b.transcricoes(ids)), 900)
        self.assertEqual(self.b.transcricoes([]), {})

    def test_contar_apagar_por_janela_e_metricas(self):
        self.gravar("a", ts=100, estado="ok", segundos_lidos=10, tempo_ms=5)
        self.gravar("b", ts=200, estado="cortado", segundos_lidos=600, tempo_ms=50)
        self.gravar("c", ts=300, estado="falhou")
        self.gravar("d", ts=400, estado="pulado")
        m = self.b.metricas_transcricao()
        self.assertEqual((m["n"], m["ok"], m["cortados"], m["falhas"], m["pulados"], m["segundos"], m["tempo_ms"]),
                         (4, 1, 1, 1, 1, 610, 55))
        self.assertEqual(self.b.apagar_transcricoes(150, 350), 2)
        self.assertEqual((self.b.contar_transcricoes(), self.b.contar_transcricoes(0, 150), self.b.contar_transcricoes(150, 350)),
                         (2, 1, 0))
        self.assertEqual(set(self.b.transcricoes(["a", "b", "c", "d"])), {"a", "d"})


class TestD57(unittest.TestCase):
    """Migração 009 (D-57, pedido 45): substituição de cartões, consolidação e lançamento adiado."""

    MIGRACOES = Path(__file__).resolve().parents[1] / "app" / "migracoes"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.b = Banco(str(Path(self.tmp.name) / "v.db"))

    def classificar(self, msg_id, classe="demanda", **kw):
        base = dict(msg_id=msg_id, conversa="c@s.whatsapp.net", contato="A", ts=1_700_000_000, classe=classe,
                    resumo="r", confianca="alta")
        base.update(kw)
        return self.b.gravar_classificacao(**base)

    def test_migracao_009_preenche_lancado_em_dos_cartoes_que_ja_tinham_item(self):
        caminho = str(Path(self.tmp.name) / "velho.db")
        con = sqlite3.connect(caminho)
        con.execute("CREATE TABLE _migracoes (nome TEXT PRIMARY KEY, aplicada_em TEXT NOT NULL)")
        for nome in TODAS:
            if nome.startswith("009"):
                break
            con.executescript((self.MIGRACOES / nome).read_text(encoding="utf-8"))
            con.execute("INSERT INTO _migracoes VALUES (?, 'x')", (nome,))
        con.execute("INSERT INTO classificacoes (msg_id, conversa, contato, ts, classe, resumo, confianca, item_id, criado_em) VALUES "
                    "('a','c','A',1,'demanda','r','alta',7,'2026-11-01 10:00:00'), "
                    "('b','c','A',2,'demanda','r','alta',NULL,'2026-11-01 10:01:00')")
        con.commit()
        con.close()
        b = Banco(caminho)                       # aplica a 009 (e o que vier depois dela)
        self.assertEqual(b.migrar(), [])
        a, bb = b.classificacao_por_msg_id("a"), b.classificacao_por_msg_id("b")
        self.assertEqual(a["lancado_em"], "2026-11-01 10:00:00")   # já estava no Frentes: lançado quando foi criado
        self.assertIsNone(bb["lancado_em"])                        # sem item: continua sem lançamento
        for col in ("substituida_por", "substituicao_motivo", "substituida_em"):
            self.assertIsNone(a[col])

    def test_substituir_regras_e_idempotencia(self):
        a = self.classificar("a")
        b = self.classificar("b")
        c = self.classificar("c")
        outra = self.classificar("o", conversa="outra@s.whatsapp.net")
        recusado = self.classificar("r")
        self.b.decidir(recusado, "recusado")
        for velho, novo, motivo in [(a, b, "porque_sim"), (a, a, "atualizada"), (a, 999, "atualizada"), (999, a, "atualizada"),
                                    (a, outra, "atualizada"), (recusado, b, "atualizada")]:
            with self.assertRaises(ValueError, msg=(velho, novo, motivo)):
                self.b.substituir(velho, novo, motivo)
        self.b.substituir(a, b, "atualizada")
        self.b.substituir(a, b, "atualizada")        # repetir é inócuo
        reg = self.b.classificacao(a)
        self.assertEqual((reg["substituida_por"], reg["substituicao_motivo"]), (b, "atualizada"))
        self.assertRegex(reg["substituida_em"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        with self.assertRaises(ValueError):          # já substituído por outro
            self.b.substituir(a, c, "consolidada")
        with self.assertRaises(ValueError):          # o novo já foi substituído
            self.b.substituir(c, a, "consolidada")
        self.b.substituir(b, c, "cancelada")         # a cadeia segue pelo atual
        self.assertEqual(self.b.classificacao(b)["substituicao_motivo"], "cancelada")

    def test_listas_substituidas_de_anteriores_de_e_restaurar(self):
        a, b, c = self.classificar("a"), self.classificar("b"), self.classificar("c")
        self.b.substituir(a, c, "consolidada")
        self.b.substituir(b, c, "consolidada")
        self.assertEqual([r["id"] for r in self.b.classificacoes()], [c])                       # padrão: só vigentes
        self.assertEqual([r["id"] for r in self.b.classificacoes(substituidas=True)], [b, a])
        self.assertEqual(len(self.b.classificacoes(substituidas=None)), 3)
        self.assertEqual([r["id"] for r in self.b.substituidas_de(c)], [a, b])
        self.assertEqual(self.b.substituidas_de(a), [])
        ant = self.b.anteriores_de([c, a, 999])
        self.assertEqual({k: [r["id"] for r in v] for k, v in ant.items()}, {c: [a, b]})
        self.assertEqual(self.b.anteriores_de([]), {})
        self.b.restaurar(a)
        reg = self.b.classificacao(a)
        self.assertEqual((reg["substituida_por"], reg["substituicao_motivo"], reg["substituida_em"]), (None, None, None))
        self.assertEqual([r["id"] for r in self.b.classificacoes()], [c, a])
        self.assertEqual([r["id"] for r in self.b.substituidas_de(c)], [b])

    def test_anteriores_de_acima_do_limite_de_variaveis(self):
        novos = [self.classificar(f"n{i}") for i in range(401)]
        velhos = [self.classificar(f"v{i}") for i in range(3)]
        for v, n in zip(velhos, novos[:3]):
            self.b.substituir(v, n, "consolidada")
        ant = self.b.anteriores_de(novos)
        self.assertEqual({k: [r["id"] for r in v] for k, v in ant.items()}, {novos[i]: [velhos[i]] for i in range(3)})

    def test_provisorias_e_marcar_lancado(self):
        dem = self.classificar("d")                                   # demanda sem item: provisória
        lem = self.classificar("l", "lembrar", prazo="2026-12-01", conversa="b@s.whatsapp.net", ts=1_700_000_001)
        self.classificar("av", "aviso")                               # não vira item
        self.classificar("sa", "sem_acao")
        com_item = self.classificar("ci", "atencao", item_id=7)       # já no Frentes
        decidido = self.classificar("de")
        self.b.decidir(decidido, "recusado")
        subst = self.classificar("su")
        self.b.substituir(subst, dem, "consolidada")
        curado_para_item = self.classificar("cu", "aviso")            # a IA disse aviso, o dono rotulou demanda: provisório
        self.b.curar(curado_para_item, "demanda")
        curado_para_aviso = self.classificar("ca", "demanda")         # a IA disse demanda, o dono rotulou aviso: não
        self.b.curar(curado_para_aviso, "aviso")
        self.assertEqual([r["id"] for r in self.b.provisorias()], [lem, dem, curado_para_item])   # conversa, ts, id
        self.assertEqual([r["id"] for r in self.b.provisorias(conversa="b@s.whatsapp.net")], [lem])
        self.assertEqual(self.b.provisorias(ate="2000-01-01 00:00:00"), [])
        self.assertEqual(len(self.b.provisorias(ate="2100-01-01 00:00:00")), 3)
        self.b.marcar_lancado(dem, 42, frente_id=2)
        reg = self.b.classificacao(dem)
        self.assertEqual((reg["item_id"], reg["frente_id"], reg["confianca"]), (42, 2, "alta"))
        self.assertRegex(reg["lancado_em"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.b.marcar_lancado(lem, 43, frente_id=None, confianca="baixa")   # frente arquivada: Inbox + baixa
        reg = self.b.classificacao(lem)
        self.assertEqual((reg["item_id"], reg["frente_id"], reg["confianca"]), (43, None, "baixa"))
        self.assertEqual([r["id"] for r in self.b.provisorias()], [curado_para_item])
        self.assertEqual(self.b.classificacao(com_item)["lancado_em"], None)   # gravar_classificacao não marca lançamento

    def test_atualizar_resumo_prazo_so_em_provisorio(self):
        a = self.classificar("a", resumo="antes")
        self.b.atualizar_resumo_prazo(a, resumo="depois")
        self.assertEqual((self.b.classificacao(a)["resumo"], self.b.classificacao(a)["prazo"]), ("depois", None))
        self.b.atualizar_resumo_prazo(a, prazo="2026-12-01")
        self.assertEqual((self.b.classificacao(a)["resumo"], self.b.classificacao(a)["prazo"]), ("depois", "2026-12-01"))
        self.b.atualizar_resumo_prazo(a)                                      # nada a fazer
        self.b.atualizar_resumo_prazo(a, resumo="x" * 400)
        cortado = self.b.classificacao(a)["resumo"]
        self.assertTrue(len(cortado) <= 300 and cortado.endswith("…"), len(cortado))   # mesmo corte de gravar_classificacao
        com_item = self.classificar("b", item_id=7)
        decidido = self.classificar("c")
        self.b.decidir(decidido, "confirmado")
        lancado = self.classificar("d")
        self.b.marcar_lancado(lancado, 9)
        subst = self.classificar("e")
        self.b.substituir(subst, a, "consolidada")
        for i in (com_item, decidido, lancado, subst, 999):
            with self.assertRaises(ValueError, msg=i):
                self.b.atualizar_resumo_prazo(i, resumo="nao")
        self.assertEqual(self.b.classificacao(com_item)["resumo"], "r")

    def test_lancadas_e_reescrita_do_cartao_ja_lancado(self):   # D-58: a IA corrige o que já subiu
        prov = self.classificar("p")                                          # provisório: não é "lançada"
        com_item = self.classificar("ci", "atencao", item_id=7)
        outra = self.classificar("o", "lembrar", item_id=8, conversa="b@s.whatsapp.net")
        confirmado = self.classificar("co", item_id=9)
        self.b.decidir(confirmado, "confirmado")                              # "Subir agora": continua corrigível
        recusado = self.classificar("re", item_id=10)
        self.b.decidir(recusado, "recusado")
        subst = self.classificar("su", item_id=11)
        self.b.substituir(subst, com_item, "atualizada")
        self.classificar("le", "lembrando", item_id=7)                        # nota de cobrança: o item é de outro cartão
        curado_aviso = self.classificar("ca", item_id=12)
        self.b.curar(curado_aviso, "aviso")                                   # o dono disse que não era demanda
        self.assertEqual([r["id"] for r in self.b.lancadas()], [outra, com_item, confirmado])   # conversa, ts, id
        self.assertEqual([r["id"] for r in self.b.lancadas(conversa="b@s.whatsapp.net")], [outra])
        self.assertEqual(self.b.lancadas(dias=-1), [])                        # fora da janela
        self.b.atualizar_resumo_prazo(com_item, resumo="versao nova", prazo="2026-12-02", lancado=True)
        reg = self.b.classificacao(com_item)
        self.assertEqual((reg["resumo"], reg["prazo"], reg["item_id"]), ("versao nova", "2026-12-02", 7))
        self.b.atualizar_resumo_prazo(confirmado, resumo="tambem", lancado=True)
        self.b.atualizar_resumo_prazo(prov, resumo="provisorio segue valendo", lancado=True)
        for i in (recusado, subst):
            with self.assertRaises(ValueError, msg=i):
                self.b.atualizar_resumo_prazo(i, resumo="nao", lancado=True)


if __name__ == "__main__":
    unittest.main()
