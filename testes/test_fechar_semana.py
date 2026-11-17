"""Testes próprios da estação E10: app/semana.py, app/aviso.py e bin/fechar-semana.py.
Quem chama: unittest.
O que não faz: não chama Claude, Docker, Postgres nem WhatsApp (psql e executor são fakes; cofre é HTTP local;
subprocess.run do backup é substituído).
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import aviso, semana as sem  # noqa: E402
from app.banco import Banco  # noqa: E402
from app.erros import LimpezaIncompleta, VigiaErro  # noqa: E402
from app.lotes import PostgresIndisponivel  # noqa: E402
from testes.test_contrato_fechar_semana import _Cofre  # noqa: E402
from testes.util_contrato import RAIZ as RAIZ_FS, _importar_arquivo  # noqa: E402

SEMANA = "2026-W45"
INI, FIM = sem.janela(SEMANA)
OK = subprocess.CompletedProcess([], 0, b"", b"")


class PgFalso:
    """Imita o Postgres do Greg: `msgs` (ids na janela), `fila` (ids). Reconhece só as consultas do fechar-semana.
    O vigia_limpeza NÃO tem DELETE (D-25): qualquer DELETE/WITH que chegue aqui é erro do teste."""

    def __init__(self, msgs=3, fila_orfa=0, nao_apaga=0, max_ts=1_700_000_000, apos_backup_falha=False, funcao_devolve=None):
        self.msgs = msgs
        self.inicial = msgs
        self.fila = msgs
        self.orfas = fila_orfa
        self.nao_apaga = nao_apaga   # simula função que deixa resto
        self.max_ts = max_ts
        self.apos_backup_falha = apos_backup_falha
        self.funcao_devolve = funcao_devolve
        self.consultas = []

    def __call__(self, q):
        self.consultas.append(q)
        if q.startswith("SELECT vigia.limpar_janela("):
            if self.apos_backup_falha:
                raise PostgresIndisponivel("psql-limpeza saiu com código 1")
            apagadas = self.msgs - self.nao_apaga
            self.fila -= apagadas
            self.msgs = self.nao_apaga
            return [[str(apagadas if self.funcao_devolve is None else self.funcao_devolve)]]
        if q.startswith('SELECT max("messageTimestamp")'):
            return [[str(self.max_ts)]]
        if "NOT EXISTS" in q and q.startswith("SELECT count"):
            return [[str(self.orfas)]]
        if q.startswith('SELECT count(*) FROM public."Message"'):
            return [[str(self.msgs)]]
        if "JOIN" in q:
            return [["0"]]
        raise AssertionError("consulta inesperada: " + q[:60])


def backup_falso(n, ids=None):
    """subprocess.run falso do backup: escreve `n` linhas JSON no stdout (o arquivo .jsonl) e devolve rc 0."""
    def run(cmd, **kw):
        f = kw.get("stdout")
        if f is not None and hasattr(f, "write"):
            for i in range(n):
                mid = ids[i] if ids else f"m{i}"
                f.write((json.dumps({"id": mid, "messageTimestamp": 1}) + "\n").encode())
        return subprocess.CompletedProcess(cmd, 0, b"", b"")
    return run


class PgComAnteriores(PgFalso):
    """PgFalso + `antigas` linhas antes da semana (histórico do pareamento fora da janela, D-53): a janela [0, ini) é
    atendida à parte; o resto cai no PgFalso."""

    def __init__(self, antigas, **kw):
        super().__init__(**kw)
        self.antigas = antigas

    def __call__(self, q):
        if q.startswith("SELECT vigia.limpar_janela(0, "):
            self.consultas.append(q)
            n, self.antigas = self.antigas, 0
            return [[str(n)]]
        if '"messageTimestamp" >= 0 AND' in q:
            self.consultas.append(q)
            return [[str(self.antigas)]]
        return super().__call__(q)


def backup_por_janela(cmd, **kw):
    """Backup falso que devolve 2 linhas para a janela [0, ini) e 4 para a semana (casa com PgComAnteriores(2, msgs=4))."""
    sql = kw["input"].decode("utf-8")
    return backup_falso(2 if '"messageTimestamp" >= 0 AND' in sql else 4)(cmd, **kw)


class TestJanela(unittest.TestCase):
    def test_sete_dias_e_domingo_20h(self):
        self.assertEqual(FIM - INI, 7 * 86400)
        self.assertEqual(datetime.fromtimestamp(FIM).strftime("%a %H:%M"), "Sun 20:00")
        self.assertEqual(sem.semana_de(FIM), SEMANA)

    def test_semana_invalida(self):
        for ruim in ("2026-45", "2026-W60", "x"):
            with self.assertRaises(ValueError):
                sem.janela(ruim)

    def test_semana_fechada(self):
        self.assertEqual(sem.semana_fechada(FIM), SEMANA)               # no instante do fim já está fechada
        self.assertEqual(sem.semana_fechada(FIM - 60), "2026-W44")      # um minuto antes ainda não
        self.assertEqual(sem.semana_fechada(FIM + 86400), SEMANA)


class TestAviso(unittest.TestCase):
    def test_validar(self):
        self.assertEqual(aviso.validar("Semana 2026-W45 fechada · 12 msgs"), "Semana 2026-W45 fechada · 12 msgs")
        for ruim in ("", "x" * 301, "a\n\nb", 'ele disse "oi"', "ele disse «oi»"):
            with self.assertRaises(ValueError, msg=ruim[:12]):
                aviso.validar(ruim)

    def test_falha_de_token_ou_rede_devolve_false(self):
        def sem_token():
            raise OSError("sem arquivo")
        self.assertFalse(aviso.enviar("ok", token_fn=sem_token))
        self.assertFalse(aviso.enviar("ok", cofre_url="http://127.0.0.1:1", token_fn=lambda: "t"))

    def test_cofre_recusa(self):
        tv = _Cofre()
        self.addCleanup(tv.parar)
        tv.httpd.RequestHandlerClass.do_POST = lambda self: (self.send_response(200), self.send_header("Content-Length", "2"),
                                                             self.end_headers(), self.wfile.write(b"{}"))
        self.assertFalse(aviso.enviar("ok", cofre_url=tv.url, token_fn=lambda: "t"))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dados = Path(self.tmp.name) / "dados"
        self.banco = Banco(str(self.dados / "vigia.db"))
        self.caixa = self.dados / "caixa"
        self.backup = self.dados / "backup"

    def lote(self, nome, arquivos=("conversa.md", "contexto.md", "mensagens.json")):
        d = self.caixa / nome / "maria"
        d.mkdir(parents=True)
        for a in arquivos:
            (d / a).write_text("x")
        return self.caixa / nome

    def lote_na_janela(self):
        return time_nome(INI + 3 * 86400)


def time_nome(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d_%H%M")


class TestLimpar(Base):
    def chamar(self, pg, **kw):
        with mock.patch("subprocess.run", side_effect=backup_falso(kw.pop("linhas", getattr(pg, "inicial", 2)), kw.pop("ids", None))) as run:
            r = sem.limpar(pg, self.banco, ini_ts=INI, fim_ts=FIM, pasta_caixa=self.caixa, backup_dir=self.backup,
                           semana=SEMANA, hash_relatorio="abc", **kw)
        return r, run

    def test_caminho_feliz_apaga_caixa_e_grava_lapide(self):
        dentro = self.lote(time_nome(INI + 86400))
        fora = self.lote(time_nome(FIM + 3600))
        pg = PgFalso(msgs=3)
        r, run = self.chamar(pg)
        self.assertEqual((r["n_msgs"], r["lotes_apagados"]), (3, 1))
        self.assertFalse(dentro.exists())
        self.assertTrue(fora.exists())
        ls = self.banco.lapides()
        self.assertEqual([(l["semana"], l["n_msgs"], l["hash_relatorio"]) for l in ls], [(SEMANA, 3, "abc")])
        self.assertEqual(set(ls[0]), {"id", "semana", "n_msgs", "hash_relatorio", "apagado_em"})
        cmd = run.call_args[0][0]
        self.assertIn("psql-limpeza-stdin", cmd)       # a query da janela vai pelo stdin, nunca em argv (ps)
        self.assertFalse([a for a in cmd if "SELECT" in a])
        self.assertIn("SELECT row_to_json", run.call_args[1]["input"].decode("utf-8"))
        self.assertTrue(oct(os.stat(self.backup).st_mode)[-3:] == "700")

    def test_apaga_as_transcricoes_da_janela_e_mantem_as_de_fora(self):
        """D-47: a transcrição (único texto de mensagem no SQLite) sai com o cru da mesma semana."""
        self.banco.gravar_transcricao(msg_id="dentro", conversa="c", ts=INI + 100, estado="ok", texto="x")
        self.banco.gravar_transcricao(msg_id="fora", conversa="c", ts=FIM + 100, estado="ok", texto="y")
        r, _ = self.chamar(PgFalso(msgs=1))
        self.assertEqual(r["transcricoes_apagadas"], 1)
        self.assertEqual(set(self.banco.transcricoes(["dentro", "fora"])), {"fora"})

    def test_limpar_usa_a_funcao_security_definer_e_nunca_delete(self):
        pg = PgFalso(msgs=2)
        self.chamar(pg)
        self.assertIn(f"SELECT vigia.limpar_janela({INI}, {FIM})", pg.consultas)
        self.assertFalse([q for q in pg.consultas if "DELETE" in q.upper()])
        self.assertTrue(pg.consultas.index(f"SELECT vigia.limpar_janela({INI}, {FIM})")
                        > pg.consultas.index(next(q for q in pg.consultas if q.startswith('SELECT max("messageTimestamp")'))))

    def test_backup_com_linhas_diferentes_do_count_nao_apaga(self):
        pg = PgFalso(msgs=3)
        dentro = self.lote(time_nome(INI + 86400))
        with self.assertRaises(sem.BackupDivergente):
            self.chamar(pg, linhas=2)
        self.assertFalse(any(q.startswith("SELECT vigia.limpar_janela") for q in pg.consultas))
        self.assertTrue(dentro.exists())
        self.assertEqual(list(self.backup.glob("*")), [])
        self.assertEqual(self.banco.lapides(), [])

    def test_trava_de_unidade_milissegundo_nao_apaga(self):
        pg = PgFalso(msgs=1, max_ts=1_700_000_000_000)
        with self.assertRaises(VigiaErro) as c:
            self.chamar(pg)
        self.assertNotIsInstance(c.exception, LimpezaIncompleta)
        self.assertFalse(any(q.startswith("SELECT vigia.limpar_janela") for q in pg.consultas))
        self.assertEqual(list(self.backup.glob("*")), [])

    def test_erro_do_postgres_depois_do_backup_e_limpeza_incompleta_nao_nao_rodou(self):
        pg = PgFalso(msgs=2, apos_backup_falha=True)
        with self.assertRaises(LimpezaIncompleta):
            self.chamar(pg)
        self.assertEqual(self.banco.lapides(), [])
        self.assertEqual(len(list(self.backup.glob("*.jsonl"))), 1)   # backup preservado

    def test_erro_do_postgres_antes_do_backup_continua_sendo_nao_rodou(self):
        def pg(q):
            raise PostgresIndisponivel("fora")
        with self.assertRaises(PostgresIndisponivel):
            self.chamar(pg, linhas=0)

    def test_funcao_apagou_mais_que_o_backup(self):
        with self.assertRaises(LimpezaIncompleta):
            self.chamar(PgFalso(msgs=2, funcao_devolve=5))
        self.assertEqual(self.banco.lapides(), [])

    def test_apaga_midia_cache_e_falhas_dos_ids_da_semana_e_ignora_ponto_midia_como_lote(self):
        midia = self.caixa / ".midia"
        midia.mkdir(parents=True)
        (midia / "m0.jpg").write_text("x"); (midia / "m1.png").write_text("x"); (midia / "outra.jpg").write_text("x")
        (self.dados / "falhas.json").write_text("{}")
        self.lote(time_nome(INI + 86400) + "-2")        # sufixo -N também é lote
        r, _ = self.chamar(PgFalso(msgs=2))
        self.assertFalse((midia / "m0.jpg").exists() or (midia / "m1.png").exists())
        self.assertTrue((midia / "outra.jpg").exists())
        self.assertFalse((self.dados / "falhas.json").exists())
        self.assertEqual(r["lotes_apagados"], 1)
        self.assertTrue(midia.exists())

    def test_lotes_da_semana_ignora_ponto_e_aceita_sufixo(self):
        (self.caixa / ".midia").mkdir(parents=True)
        a = self.lote(time_nome(INI + 3600)); b = self.lote(time_nome(INI + 3600) + "-3")
        self.assertEqual({p.name for p in sem.lotes_da_semana(self.caixa, INI, FIM)}, {a.name, b.name})

    def test_resto_na_message_levanta_e_preserva_caixa_e_sem_lapide(self):
        dentro = self.lote(time_nome(INI + 86400))
        with self.assertRaises(LimpezaIncompleta):
            self.chamar(PgFalso(msgs=3, nao_apaga=1))
        self.assertTrue(dentro.exists())
        self.assertEqual(self.banco.lapides(), [])

    def test_fila_orfa_residual_levanta(self):
        pg = PgFalso(msgs=2)
        pg.orfas = 1
        # o DELETE de órfãs do PgFalso zera; força resto simulando que ele não consegue
        orig = pg.__call__
        pg2 = lambda q: [["1"]] if "NOT EXISTS" in q and q.startswith("SELECT count") else orig(q)  # noqa: E731
        with self.assertRaises(LimpezaIncompleta):
            self.chamar(pg2)
        self.assertEqual(self.banco.lapides(), [])

    def test_backup_falho_nao_apaga(self):
        pg = PgFalso(msgs=2)
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, b"", b"")):
            with self.assertRaises(Exception) as c:
                sem.limpar(pg, self.banco, ini_ts=INI, fim_ts=FIM, pasta_caixa=self.caixa, backup_dir=self.backup)
        self.assertNotIsInstance(c.exception, LimpezaIncompleta)
        self.assertFalse(any(q.startswith("WITH f AS (DELETE") for q in pg.consultas))
        self.assertEqual(list(self.backup.glob("*")), [])
        self.assertEqual(self.banco.lapides(), [])

    def test_depois_da_lapide_apaga_todos_os_backups_menos_o_da_semana(self):
        self.backup.mkdir(parents=True)
        velho, recente = self.backup / "2026-W40-1.jsonl", self.backup / "2026-W44-1.jsonl"
        velho.write_text("x"); recente.write_text("x")
        r, _ = self.chamar(PgFalso(msgs=1))
        self.assertEqual([p.name for p in self.backup.glob("*.jsonl")], [Path(r["backup"]).name])

    def test_lapide_nao_gravada_preserva_backup_anterior(self):
        self.backup.mkdir(parents=True)
        anterior = self.backup / "2026-W44-1.jsonl"
        anterior.write_text("x")
        with self.assertRaises(LimpezaIncompleta):
            self.chamar(PgFalso(msgs=2, nao_apaga=1))
        self.assertTrue(anterior.exists())

    def test_repetir_semana_nao_duplica_lapide(self):
        self.chamar(PgFalso(msgs=1))
        self.chamar(PgFalso(msgs=1))
        self.assertEqual(len(self.banco.lapides()), 1)


class TestRelatorio(Base):
    def test_entrada_so_tem_classificacoes_e_temporario_some(self):
        self.banco.gravar_classificacao(msg_id="m1", conversa="c@s", contato="Maria", ts=INI + 100, classe="demanda",
                                        resumo="pede orçamento", confianca="alta", item_id=7)
        self.banco.gravar_classificacao(msg_id="m2", conversa="c@s", contato="Maria", ts=FIM + 100, classe="aviso",
                                        resumo="fora da janela", confianca="alta")
        visto = {}

        def executor(cmd, **kw):
            arq = Path(cmd[-1].split("arquivo ")[1].split(". ")[0])
            visto["json"] = json.loads(arq.read_text())
            visto["cwd"] = kw["cwd"]
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": "# Semana\n"}), "")

        txt = sem.relatorio(lambda q: [["42"]], self.banco, executor=executor, semana=SEMANA)
        self.assertEqual(txt, "# Semana\n")
        self.assertEqual(visto["json"]["mensagens_recebidas"], 42)
        self.assertEqual([c["resumo"] for c in visto["json"]["classificacoes"]], ["pede orçamento"])
        self.assertEqual(visto["json"]["itens_no_frentes"], 1)
        self.assertFalse(Path(visto["cwd"]).exists())

    def test_erro_do_claude(self):
        for saida in ('{"is_error": true, "result": "x"}', "nao-json", '{"result": "  "}'):
            ex = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, saida, "")  # noqa: E731
            with self.assertRaises(sem.RelatorioFalhou):
                sem.relatorio(lambda q: [["0"]], self.banco, executor=ex, semana=SEMANA)

    def test_comando_nao_tem_bash_nem_escrita(self):
        cmd = sem.montar_comando_relatorio(Path("/x/a.json"), modelo="opus", prompt=Path("/p.md"), claude="claude")
        self.assertEqual(cmd[cmd.index("--tools") + 1], "Read")
        self.assertIn("Bash", cmd[cmd.index("--disallowedTools") + 1])


class TestFluxo(Base):
    """fechar() e apagar() do script, com tudo injetado."""

    def setUp(self):
        super().setUp()
        self.fs = _importar_arquivo("bin/fechar-semana.py", "fechar_semana")
        self.agora = FIM + 600
        self.enviados = []

    def executor(self, cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": "# Semana 2026-W45\n"}), "")

    def fechar(self, ok=True, **kw):
        def env(t):
            self.enviados.append(t)
            return ok
        return self.fs.fechar(SEMANA, dados=self.dados, banco=self.banco, psql_ro=lambda q: [["5"]],
                              executor=self.executor, enviar_fn=env, agora=lambda: self.agora, **kw)

    # ---- pedido 67: prazo de exclusão escolhido em Ajustes ----
    def _prefs(self, texto):
        self.dados.mkdir(parents=True, exist_ok=True)
        (self.dados / "preferencias.json").write_text(texto, encoding="utf-8")

    def test_prazo_padrao_e_7_dias_e_falha_para_o_lado_que_nao_apaga(self):
        self.assertEqual(sem.prazo_dias(self.dados), 7)                      # sem arquivo: o de sempre
        self.assertEqual(sem.libera_em(self.dados, FIM), FIM)
        self._prefs('{"fonte": 13}')
        self.assertEqual(sem.prazo_dias(self.dados), 7)                      # arquivo de antes do pedido 67
        self._prefs('{"retencao_dias": 30}')
        self.assertEqual((sem.prazo_dias(self.dados), sem.libera_em(self.dados, FIM)), (30, FIM + 23 * 86400))
        for estragado in ("{isso não é json", "[]", '{"retencao_dias": "muito"}', '{"retencao_dias": 3}'):
            self._prefs(estragado)
            self.assertEqual(sem.prazo_dias(self.dados), 0, estragado)       # ilegível ou fora da lista: não apaga
            self.assertIsNone(sem.libera_em(self.dados, FIM))

    def test_nao_apagar_segura_toda_semana_ate_com_forcar_e_muito_depois(self):
        """Pedido 74: com "Não apagar" em Ajustes nenhuma semana sai, nem anos depois, nem com --forcar."""
        self._prefs('{"retencao_dias": 0}')
        self.assertEqual(sem.prazo_dias(self.dados), 0)
        self.assertIsNone(sem.libera_em(self.dados, FIM))
        pg = PgFalso(msgs=4)
        for forcar in (False, True):
            r = self.apagar(pg=pg, agora=FIM + 800 * 86400, forcar=forcar)
            self.assertEqual((r["status"], r["prazo_dias"], r["libera_em"]), ("no_prazo", 0, None))
        self.assertEqual(pg.msgs, 4)
        self.assertEqual(self.banco.lapides(), [])
        self.assertIn("mensagens guardadas, sem limpeza", self.fs.texto_do_aviso(SEMANA, 4, 1, "8120", None, guardar=True))
        self.assertNotIn("limpeza em 24 h", self.fs.texto_do_aviso(SEMANA, 4, 1, "8120", None, guardar=True))

    def test_semana_dentro_do_prazo_nao_e_apagada_nem_com_forcar(self):
        self._prefs('{"retencao_dias": 14}')
        pg = PgFalso(msgs=4)
        for forcar in (False, True):
            r = self.apagar(pg=pg, agora=FIM + 6 * 86400, forcar=forcar)
            self.assertEqual((r["status"], r["prazo_dias"], r["libera_em"]), ("no_prazo", 14, FIM + 7 * 86400))
        self.assertEqual(pg.msgs, 4)                                          # nada saiu do banco
        self.assertEqual(self.banco.lapides(), [])
        guardas = {n: b for n, b, _ in self.fs.guardas_da_limpeza(SEMANA, dados=self.dados, banco=self.banco, psql_ro=pg,
                                                                 agora=lambda: FIM + 7 * 86400 + 60)}
        self.assertFalse(guardas["no_prazo"])                                 # passou o prazo: a guarda libera

    def test_aviso_diz_quando_a_limpeza_vem(self):
        self.assertIn("limpeza em 24 h", self.fs.texto_do_aviso(SEMANA, 5, 2, "8120"))
        self.assertIn("limpeza em 24 h", self.fs.texto_do_aviso(SEMANA, 5, 2, "8120", FIM))
        import time as _t
        dia = _t.strftime("%d/%m", _t.localtime(FIM + 24 * 86400))
        self.assertIn(f"limpeza a partir de {dia}", self.fs.texto_do_aviso(SEMANA, 5, 2, "8120", FIM + 23 * 86400))

    def apagar(self, pg=None, agora=None, **kw):
        pg = pg or PgFalso(msgs=4)
        t = self.agora if agora is None else agora
        with mock.patch("subprocess.run", side_effect=backup_falso(getattr(pg, "inicial", 2))):
            return self.fs.apagar(SEMANA, dados=self.dados, banco=self.banco, psql_ro=pg, psql_limpeza=pg,
                                  agora=lambda: t, enviar_fn=lambda t: self.enviados.append(t) or True, **kw)

    def test_fechar_grava_relatorio_0600_marca_e_avisa_so_contadores(self):
        self.banco.gravar_classificacao(msg_id="m1", conversa="c", contato="Maria", ts=INI + 5, classe="demanda",
                                        resumo="x", confianca="alta", item_id=3)
        r = self.fechar()
        self.assertEqual(r["status"], "ok")
        rel = self.dados / "relatorios" / f"{SEMANA}.md"
        self.assertEqual(oct(rel.stat().st_mode)[-3:], "600")
        self.assertEqual(oct((self.dados / "relatorios" / f"{SEMANA}.enviado").stat().st_mode)[-3:], "600")
        self.assertEqual(self.enviados, [f"Semana {SEMANA} fechada · 5 msgs · 1 itens · limpeza em 24 h · http://127.0.0.1:8120/demandas"])
        aviso.validar(self.enviados[0])

    def test_aviso_falho_sem_marca_e_proxima_rodada_reaproveita_relatorio(self):
        self.assertEqual(self.fechar(ok=False)["status"], "aviso_falhou")
        self.assertFalse((self.dados / "relatorios" / f"{SEMANA}.enviado").exists())
        calls = []
        self.executor = lambda cmd, **kw: calls.append(1)
        self.assertEqual(self.fechar()["status"], "ok")
        self.assertEqual(calls, [])   # não chamou o Claude de novo
        self.assertTrue((self.dados / "relatorios" / f"{SEMANA}.enviado").exists())

    def test_semana_aberta_nao_fecha(self):
        self.agora = FIM - 10
        self.assertEqual(self.fechar()["status"], "semana_aberta")
        self.assertEqual(self.enviados, [])

    def test_apagar_guardas_em_ordem(self):
        self.assertEqual(self.apagar()["status"], "sem_relatorio")
        self.fechar()
        self.assertEqual(self.apagar()["status"], "aguardando_24h")
        depois = self.agora + 24 * 3600
        self.assertEqual(self.apagar(agora=depois)["status"], "apagada")
        self.assertEqual(len(self.banco.lapides()), 1)
        self.assertEqual(self.apagar(agora=depois)["status"], "ja_apagada")

    def test_apagar_sem_aviso_recusa_mas_forcar_vai(self):
        self.fechar(ok=False)
        self.assertEqual(self.apagar(agora=self.agora + 90000)["status"], "sem_aviso")
        self.assertEqual(self.apagar(forcar=True)["status"], "apagada")

    def test_apagar_varre_o_historico_anterior_a_janela_com_lapide_propria(self):   # D-53 (pedido 43)
        """O que a Evolution gravou no pareamento e ficou ANTES da semana (fora da janela de 1 dia) sai junto da semana
        mais antiga pendente, com backup e lápide "anteriores-<semana>" próprios."""
        self.fechar()
        pg = PgComAnteriores(2, msgs=4)
        with mock.patch("subprocess.run", side_effect=backup_por_janela):
            r = self.fs.apagar(SEMANA, dados=self.dados, banco=self.banco, psql_ro=pg, psql_limpeza=pg,
                               agora=lambda: self.agora + 90000, enviar_fn=lambda t: self.enviados.append(t) or True)
        self.assertEqual((r["status"], r["n_msgs"], r["anteriores_apagadas"]), ("apagada", 4, 2))
        self.assertIn(f"SELECT vigia.limpar_janela(0, {INI})", pg.consultas)
        self.assertGreater(pg.consultas.index(f"SELECT vigia.limpar_janela(0, {INI})"),
                           pg.consultas.index(f"SELECT vigia.limpar_janela({INI}, {FIM})"))   # a semana vem primeiro
        self.assertEqual([(l["semana"], l["n_msgs"]) for l in self.banco.lapides()],
                         [(SEMANA, 4), (f"anteriores-{SEMANA}", 2)])
        self.assertIn("2 anteriores à janela", self.enviados[-1])
        aviso.validar(self.enviados[-1])

    def test_varredura_de_anteriores_espera_semana_restaurada_pendente(self):   # D-53 × I2
        self.fechar()
        (self.dados / "restaurada-2026-W44").write_text("1")
        pg = PgComAnteriores(2, msgs=4)
        with mock.patch("subprocess.run", side_effect=backup_por_janela):
            r = self.fs.apagar(SEMANA, dados=self.dados, banco=self.banco, psql_ro=pg, psql_limpeza=pg,
                               agora=lambda: self.agora + 90000, enviar_fn=lambda t: self.enviados.append(t) or True)
        self.assertEqual((r["status"], r["anteriores_apagadas"]), ("apagada", 0))
        self.assertFalse([q for q in pg.consultas if q.startswith("SELECT vigia.limpar_janela(0,")])
        self.assertEqual([l["semana"] for l in self.banco.lapides()], [SEMANA])
        self.assertNotIn("anteriores", self.enviados[-1])

    def test_varredura_que_falha_nao_derruba_o_fechamento_da_semana(self):   # D-53: a semana já está provada
        self.fechar()
        pg = PgComAnteriores(2, msgs=4)

        def run(cmd, **kw):   # backup dos anteriores devolve 1 linha com count 2 -> BackupDivergente dentro do limpar
            sql = kw["input"].decode("utf-8")
            return backup_falso(1 if '"messageTimestamp" >= 0 AND' in sql else 4)(cmd, **kw)
        with mock.patch("subprocess.run", side_effect=run):
            r = self.fs.apagar(SEMANA, dados=self.dados, banco=self.banco, psql_ro=pg, psql_limpeza=pg,
                               agora=lambda: self.agora + 90000, enviar_fn=lambda t: self.enviados.append(t) or True)
        self.assertEqual((r["status"], r["n_msgs"], r["anteriores_apagadas"]), ("apagada", 4, 0))
        self.assertEqual([l["semana"] for l in self.banco.lapides()], [SEMANA])
        self.assertIn("Semana 2026-W45: cru apagado · 4 msgs · lápide gravada", self.enviados[-1])

    def test_segura_adia_mesmo_com_forcar(self):
        self.fechar()
        sem.segurar(self.banco, SEMANA, dias=7)
        self.assertTrue((self.dados / f"segurar-{SEMANA}").exists())
        pg = PgFalso()
        self.assertEqual(self.apagar(pg, agora=self.agora + 90000)["status"], "adiada_segura")
        self.assertEqual(self.apagar(pg, forcar=True)["status"], "adiada_segura")
        self.assertEqual(pg.consultas, [])
        self.assertEqual(self.banco.lapides(), [])

    def test_arquivo_SEGURA_global_adia(self):
        self.fechar()
        (self.dados / "SEGURA").write_text("")
        self.assertEqual(self.apagar(agora=self.agora + 90000)["status"], "adiada_segura")

    def test_nao_lidas_adiam(self):
        self.fechar()

        def pg(q):
            return [["2"]] if "JOIN" in q else PgFalso()(q)
        self.assertEqual(self.apagar(pg, agora=self.agora + 90000)["status"], "ha_nao_lidas")
        self.assertEqual(self.banco.lapides(), [])

    def test_simular_so_le(self):
        self.fechar()
        consultas = []
        linhas = self.fs.simular(SEMANA, dados=self.dados, banco=self.banco, apagando=True,
                                 psql_ro=lambda q: consultas.append(q) or [["9"]], agora=lambda: self.agora)
        self.assertTrue(consultas and all(q.upper().startswith("SELECT") for q in consultas))
        self.assertIn("mensagens do cru na janela: 9", linhas)
        self.assertEqual(self.banco.lapides(), [])


class TestSegura(Base):
    def test_arquivo_vazio_ou_ilegivel_conta_como_segurado_e_epoch_vencido_libera(self):
        self.dados.mkdir(parents=True, exist_ok=True)
        marca = self.dados / f"segurar-{SEMANA}"
        marca.touch()                                    # vazio
        self.assertTrue(sem.segura(self.dados, SEMANA, 1000))
        marca.write_text("lixo")
        self.assertTrue(sem.segura(self.dados, SEMANA, 1000))
        marca.write_text("2000")
        self.assertTrue(sem.segura(self.dados, SEMANA, 1999))
        self.assertFalse(sem.segura(self.dados, SEMANA, 2000))   # vencida: volta para a fila
        marca.unlink()
        self.assertFalse(sem.segura(self.dados, SEMANA, 1000))


class TestFluxo2(TestFluxo):
    """Itens 3, 6, 10 da emenda sobre o mesmo cenário."""

    def test_forcar_nao_pula_nao_lidas(self):
        self.fechar()

        def pg(q):
            return [["2"]] if "JOIN" in q else PgFalso()(q)
        self.assertEqual(self.apagar(pg, forcar=True)["status"], "ha_nao_lidas")
        self.assertEqual(self.banco.lapides(), [])

    def test_hash_da_lapide_e_o_sha256_dos_bytes_do_arquivo(self):
        import hashlib
        self.fechar()
        self.apagar(agora=self.agora + 90000)
        arq = self.dados / "relatorios" / f"{SEMANA}.md"
        self.assertEqual(self.banco.lapides()[0]["hash_relatorio"], hashlib.sha256(arq.read_bytes()).hexdigest())

    def test_relatorio_que_nao_comeca_por_semana_e_descartado(self):
        self.executor = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, json.dumps({"result": "Claro! # Semana"}), "")
        with self.assertRaises(sem.RelatorioFalhou):
            self.fechar()
        self.assertFalse((self.dados / "relatorios" / f"{SEMANA}.md").exists())
        self.assertEqual(self.enviados, [])

    def test_comando_do_relatorio_d27(self):
        visto = {}

        def ex(cmd, **kw):
            visto["cmd"], visto["kw"] = cmd, kw
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": "# Semana x"}), "")
        sem.relatorio(lambda q: [["1"]], self.banco, executor=ex, semana=SEMANA)
        cmd = visto["cmd"]
        for flag in ("--no-session-persistence", "--strict-mcp-config", "--restricted"):
            self.assertIn(flag, cmd)
        self.assertEqual(cmd[cmd.index("--permission-prompts") + 1], "none")
        permitida = cmd[cmd.index("--allowedTools") + 1]
        self.assertRegex(permitida, r"^Read\(//.+/\*\*\)$")
        self.assertIn("Read(//**/.env*)", cmd)
        self.assertTrue(visto["kw"]["start_new_session"])
        self.assertNotIn("ANTHROPIC_API_KEY", visto["kw"]["env"])
        self.assertIn("VIGIA_LOTE", visto["kw"]["env"])


class TestSemanasPerdidas(Base):
    def setUp(self):
        super().setUp()
        self.fs = _importar_arquivo("bin/fechar-semana.py", "fechar_semana")
        self.w1, self.w2, self.w3 = "2026-W43", "2026-W44", "2026-W45"
        (self.dados).mkdir(parents=True, exist_ok=True)
        from app.cursor import Cursor
        c = Cursor(self.dados / "cursor.json", 10, sem.janela(self.w1)[0] + 3600)   # pareado dentro de W43
        c.salvar()
        self.agora = sem.janela(self.w3)[1] + 600
        self.enviados, self.saida = [], []

    def lista(self, lapides=()):
        return self.fs.semanas_pendentes(self.dados, set(lapides), self.agora)

    def test_lista_desde_o_pareamento_mais_antiga_primeiro_sem_lapides(self):
        self.assertEqual(self.lista(), [self.w1, self.w2, self.w3])
        self.assertEqual(self.lista([self.w2]), [self.w1, self.w3])

    def test_pareado_na_semana_corrente_nao_lista_nada(self):
        from app.cursor import Cursor
        Cursor(self.dados / "cursor.json", 10, self.agora - 100).salvar()
        self.assertEqual(self.lista(), [])

    def test_sem_pareamento_so_a_ultima(self):
        (self.dados / "cursor.json").unlink()
        self.assertEqual(self.lista(), [self.w3])

    def percorrer(self, apagar_modo, pg=None, executor=None, **kw):
        pg = pg or PgFalso(msgs=2)
        executor = executor or (lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, json.dumps({"result": "# Semana x\n"}), ""))
        with mock.patch("subprocess.run", side_effect=backup_falso(getattr(pg, "inicial", 2))):
            return self.fs.percorrer(self.lista(), apagar_modo=apagar_modo, explicito=False, forcar=False, refazer=False,
                                     dados=self.dados, banco=self.banco, psql_ro=pg, psql_limpeza=pg, executor=executor,
                                     enviar_fn=lambda t: self.enviados.append(t) or True, agora=lambda: self.agora,
                                     saida=self.saida.append, **kw)

    def test_relatorio_para_todas_as_semanas_sem_enviado(self):
        rel = self.dados / "relatorios"
        rel.mkdir()
        (rel / f"{self.w2}.enviado").write_text("1")           # essa já foi avisada
        self.percorrer(False)
        self.assertEqual([r["semana"] for r in self.saida], [self.w1, self.w2, self.w3])
        self.assertEqual([r["status"] for r in self.saida], ["ok", "ja_avisada", "ok"])
        self.assertEqual(len(self.enviados), 2)

    def test_limpeza_segue_depois_de_aguardando_e_segura(self):
        rel = self.dados / "relatorios"
        rel.mkdir()
        for w in (self.w1, self.w2, self.w3):
            (rel / f"{w}.md").write_text("# Semana x\n")
        (rel / f"{self.w1}.enviado").write_text(str(int(self.agora - 100)))        # aguardando_24h
        (rel / f"{self.w2}.enviado").write_text(str(int(self.agora - 90000)))      # segura
        (rel / f"{self.w3}.enviado").write_text(str(int(self.agora - 90000)))      # elegível
        (self.dados / f"segurar-{self.w2}").write_text("")
        codigo = self.percorrer(True)
        self.assertEqual([r["status"] for r in self.saida], ["aguardando_24h", "adiada_segura", "apagada"])
        self.assertEqual(codigo, 0)
        self.assertEqual([l["semana"] for l in self.banco.lapides()], [self.w3])

    def test_para_no_primeiro_erro_de_postgres(self):
        rel = self.dados / "relatorios"
        rel.mkdir()
        for w in (self.w1, self.w2, self.w3):
            (rel / f"{w}.md").write_text("# Semana x\n")
            (rel / f"{w}.enviado").write_text(str(int(self.agora - 90000)))

        def pg(q):
            raise PostgresIndisponivel("fora")
        with self.assertRaises(PostgresIndisponivel):
            self.percorrer(True, pg=pg)
        self.assertEqual(self.saida, [])
        self.assertEqual(self.banco.lapides(), [])


class TestLock(Base):
    def setUp(self):
        super().setUp()
        self.fs = _importar_arquivo("bin/fechar-semana.py", "fechar_semana")
        self.dados.mkdir(parents=True, exist_ok=True)
        self.lock = self.dados / "vigia.lock"

    def test_sem_lock_livre_com_lock_recente_ocupado_velho_livre(self):
        self.assertFalse(self.fs.lock_ocupado(self.dados))
        self.lock.mkdir()
        self.assertTrue(self.fs.lock_ocupado(self.dados))
        os.utime(self.lock, (time.time() - 7200,) * 2)
        self.assertFalse(self.fs.lock_ocupado(self.dados))

    def test_lock_do_proprio_chamador_ou_de_pid_morto_nao_conta(self):
        self.lock.mkdir()
        (self.lock / "pid").write_text(str(os.getpid()))
        self.assertFalse(self.fs.lock_ocupado(self.dados))
        (self.lock / "pid").write_text("999999")        # pid inexistente
        self.assertFalse(self.fs.lock_ocupado(self.dados))
        (self.lock / "pid").write_text("1")             # init: vivo
        self.assertTrue(self.fs.lock_ocupado(self.dados))


class TestRestaurar(Base):
    def setUp(self):
        super().setUp()
        self.fs = _importar_arquivo("bin/fechar-semana.py", "fechar_semana")
        self.dados.mkdir(parents=True, exist_ok=True)
        self.arq = self.dados / "2026-W45-1.jsonl"
        self.arq.write_text("".join(json.dumps({"id": f"m{i}", "message": {"conversation": "tem $vigia_r$ aqui"}}) + "\n"
                                    for i in range(5)))

    def test_dry_run_so_conta_e_nao_chama_o_postgres(self):
        chamadas = []
        r = self.fs.restaurar(self.arq, psql_sql=chamadas.append, dry_run=True)
        self.assertEqual((r["status"], r["linhas"]), ("dry_run", 5))
        self.assertEqual(chamadas, [])

    def test_insert_idempotente_com_dollar_quote_seguro(self):
        sqls = []
        r = self.fs.restaurar(self.arq, psql_sql=lambda q: sqls.append(q) or [["INSERT 0 3"]])
        self.assertEqual((r["linhas"], r["inseridas"], r["ja_existiam"]), (5, 3, 2))
        q = sqls[0]
        self.assertTrue(q.startswith('INSERT INTO public."Message" SELECT * FROM json_populate_record'))
        self.assertTrue(q.endswith("::json) ON CONFLICT (id) DO NOTHING"))
        self.assertIn("$vigia_r1$", q)      # a tag padrão aparece no texto: usa outra

    def test_lotes_por_tamanho(self):
        self.arq.write_text("".join(json.dumps({"id": f"m{i}"}) + "\n" for i in range(450)))
        sqls = []
        r = self.fs.restaurar(self.arq, psql_sql=lambda q: sqls.append(q) or [["INSERT 0 1"]])
        self.assertEqual(len(sqls), 3)
        self.assertEqual(r["linhas"], 450)

    def test_linha_invalida_nao_restaura_nada(self):
        self.arq.write_text(json.dumps({"id": "a"}) + "\nnao-json\n")
        sqls = []
        with self.assertRaises(VigiaErro):
            self.fs.restaurar(self.arq, psql_sql=sqls.append)
        self.assertEqual(sqls, [])


class TestRestauradaPedeNovaLimpeza(Base):
    """I2: depois de --restaurar a lápide não pode deixar o cru restaurado virar histórico (R-2/R-12)."""

    def setUp(self):
        super().setUp()
        self.fs = _importar_arquivo("bin/fechar-semana.py", "fechar_semana")
        self.dados.mkdir(parents=True, exist_ok=True)
        rel = self.dados / "relatorios"
        rel.mkdir()
        (rel / f"{SEMANA}.md").write_text("# Semana x\n")
        (rel / f"{SEMANA}.enviado").write_text(str(int(FIM)))
        self.agora = FIM + 3 * 86400
        self.banco.gravar_lapide(semana=SEMANA, n_msgs=4, hash_relatorio="h0", apagado_em=int(FIM + 90000))

    def apagar(self, **kw):
        pg = PgFalso(msgs=2)
        with mock.patch("subprocess.run", side_effect=backup_falso(2)):
            return self.fs.apagar(SEMANA, dados=self.dados, banco=self.banco, psql_ro=pg, psql_limpeza=pg,
                                  agora=lambda: self.agora, enviar_fn=lambda t: True, **kw)

    def test_sem_marca_continua_ja_apagada(self):
        self.assertEqual(self.apagar(forcar=True)["status"], "ja_apagada")
        self.assertEqual(self.fs.semanas_pendentes(self.dados, {SEMANA}, self.agora).count(SEMANA), 0)

    def test_com_marca_a_limpeza_roda_acrescenta_lapide_e_tira_a_marca(self):
        marca = self.fs.marca_restaurada(self.dados, SEMANA)
        marca.write_text("1")
        self.assertIn(SEMANA, self.fs.semanas_pendentes(self.dados, {SEMANA}, self.agora))
        r = self.apagar(forcar=True)
        self.assertEqual(r["status"], "apagada")
        self.assertFalse(marca.exists())
        ls = self.banco.lapides()
        self.assertEqual(len(ls), 2, "a lápide antiga nunca é apagada; acrescenta-se uma nova")
        self.assertEqual(ls[0]["hash_relatorio"], "h0")
        self.assertTrue(ls[1]["semana"].startswith(f"{SEMANA}~relimpeza-"))
        self.assertEqual(self.apagar(forcar=True)["status"], "ja_apagada")      # fechou de novo

    def test_limpeza_incompleta_mantem_a_marca(self):
        marca = self.fs.marca_restaurada(self.dados, SEMANA)
        marca.write_text("1")
        pg = PgFalso(msgs=2, nao_apaga=1)
        with mock.patch("subprocess.run", side_effect=backup_falso(2)):
            with self.assertRaises(LimpezaIncompleta):
                self.fs.apagar(SEMANA, dados=self.dados, banco=self.banco, psql_ro=pg, psql_limpeza=pg,
                               agora=lambda: self.agora, enviar_fn=lambda t: True, forcar=True)
        self.assertTrue(marca.exists())

    def test_restaurar_com_dados_grava_a_marca_da_semana_do_nome(self):
        arq = self.dados / f"{SEMANA}-123.jsonl"
        arq.write_text(json.dumps({"id": "a"}) + "\n")
        self.fs.restaurar(arq, psql_sql=lambda q: [["INSERT 0 1"]], dados=self.dados)
        self.assertTrue(self.fs.marca_restaurada(self.dados, SEMANA).exists())
        self.assertEqual(oct(self.fs.marca_restaurada(self.dados, SEMANA).stat().st_mode)[-3:], "600")

    def test_restaurar_deduz_semana_do_conteudo_e_dry_run_nao_marca(self):
        arq = self.dados / "qualquer.jsonl"
        arq.write_text(json.dumps({"id": "a", "messageTimestamp": INI + 10}) + "\n")
        self.fs.restaurar(arq, psql_sql=lambda q: [["INSERT 0 0"]], dry_run=True, dados=self.dados)
        self.assertFalse(self.fs.marca_restaurada(self.dados, SEMANA).exists())
        self.fs.restaurar(arq, psql_sql=lambda q: [["INSERT 0 1"]], dados=self.dados)
        self.assertTrue(self.fs.marca_restaurada(self.dados, SEMANA).exists())


class TestAvisoSemNumeroDefault(unittest.TestCase):
    """I6: o número não mora no código; sem DONO_WHATSAPP o envio não sai."""

    def test_sem_dono_pessoal_devolve_false_e_nao_chama_o_cofre(self):
        chamadas = []
        with mock.patch.dict(os.environ):
            os.environ.pop("DONO_WHATSAPP", None)
            ok = aviso.enviar("ok", token_fn=lambda: chamadas.append("token") or "t", cofre_url="http://127.0.0.1:1")
        self.assertFalse(ok)
        self.assertEqual(chamadas, [])
        self.assertFalse(hasattr(aviso, "DONO_WHATSAPP"))

    def test_sem_aviso_instancia_devolve_false_e_nao_chama_o_cofre(self):
        """Pedido 37: a instância do bot também não mora no código; sem AVISO_INSTANCIA o envio não sai."""
        chamadas = []
        with mock.patch.dict(os.environ):
            os.environ.pop("AVISO_INSTANCIA", None)
            ok = aviso.enviar("ok", token_fn=lambda: chamadas.append("token") or "t", cofre_url="http://127.0.0.1:1")
        self.assertFalse(ok)
        self.assertEqual(chamadas, [])
        self.assertFalse(hasattr(aviso, "INSTANCIA_AGENCIA"))

    def test_nenhum_numero_real_versionado_no_codigo_nem_no_exemplo(self):
        import re
        achados = []
        for pasta, pad in (("app", "*.py"), ("bin", "*.py"), ("bin", "*.sh"), (".", ".env.exemplo"), (".", "config.toml"),
                           ("docs", "*.md")):
            for f in (RAIZ_FS / pasta).glob(pad):
                if re.search(r"55\d{2}9\d{8}", f.read_text(encoding="utf-8", errors="replace")):
                    achados.append(str(f.relative_to(RAIZ_FS)))
        self.assertEqual(achados, [])


class TestRestaurarPorStdin(Base):
    """I3: SQL pelo stdin (nunca em argv), lote medido em bytes."""

    def setUp(self):
        super().setUp()
        self.fs = _importar_arquivo("bin/fechar-semana.py", "fechar_semana")
        self.dados.mkdir(parents=True, exist_ok=True)

    def test_psql_via_script_sql_stdin_manda_a_query_no_stdin_e_nao_no_argv(self):
        script = self.dados / "evolution-falso.sh"
        entrada = self.dados / "entrada.txt"
        args = self.dados / "args.txt"
        script.write_text(f'#!/bin/bash\nprintf "%s " "$@" > "{args}"\ncat > "{entrada}"\necho "INSERT 0 7"\n')
        script.chmod(0o700)
        sql = 'INSERT ... \'{"id":"m1","t":"\U0001F600 segredo-na-conversa"}\''
        r = self.fs.psql_via_script("sql-stdin", script)(sql)
        self.assertEqual(r, [["INSERT 0 7"]])
        self.assertEqual(entrada.read_text(encoding="utf-8"), sql)
        self.assertEqual(args.read_text().strip(), "sql-stdin")
        self.assertNotIn("segredo-na-conversa", args.read_text())

    def test_main_restaurar_usa_sql_stdin(self):
        fonte = (RAIZ_FS / "bin" / "fechar-semana.py").read_text(encoding="utf-8")
        self.assertIn('psql_via_script("sql-stdin", script)', fonte)
        self.assertNotIn('psql_via_script("sql", script)', fonte)

    def test_lote_conta_bytes_e_linha_grande_vai_sozinha(self):
        emoji = "\U0001F600" * 10000      # 10 mil caracteres = 40 kB em UTF-8
        linhas = [json.dumps({"id": "a", "t": emoji}, ensure_ascii=False), json.dumps({"id": "b", "t": emoji}, ensure_ascii=False),
                  json.dumps({"id": "c", "t": emoji}, ensure_ascii=False), json.dumps({"id": "d"})]
        self.assertLess(sum(len(l) for l in linhas), self.fs.LOTE_RESTAURAR_BYTES)      # por caracteres caberia tudo
        self.assertGreater(sum(len(l.encode()) for l in linhas), self.fs.LOTE_RESTAURAR_BYTES)
        arq = self.dados / "x.jsonl"
        arq.write_text("\n".join(linhas) + "\n", encoding="utf-8")
        sqls = []
        r = self.fs.restaurar(arq, psql_sql=lambda q: sqls.append(q) or [["INSERT 0 1"]])
        self.assertGreater(len(sqls), 1, "bytes, não caracteres, decidem o lote")
        self.assertEqual(r["linhas"], 4)
        grande = json.dumps({"id": "g", "t": "x" * 200_000})
        arq.write_text(grande + "\n" + json.dumps({"id": "h"}) + "\n")
        sqls.clear()
        self.fs.restaurar(arq, psql_sql=lambda q: sqls.append(q) or [["INSERT 0 1"]])
        self.assertEqual(len(sqls), 2)
        self.assertIn('"id": "g"', sqls[0])
        self.assertNotIn('"id": "h"', sqls[0])


class TestDryRunSemEscrita(Base):
    def test_somente_leitura_nao_migra_nem_escreve_e_main_nao_altera_nada(self):
        fs = _importar_arquivo("bin/fechar-semana.py", "fechar_semana")
        antes = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in self.dados.rglob("*") if p.is_file()}
        ro = Banco(str(self.dados / "vigia.db"), somente_leitura=True)
        self.assertEqual(ro.lapides(), [])
        with self.assertRaises(Exception):
            ro.gravar_lapide(semana=SEMANA, n_msgs=0, hash_relatorio="x", apagado_em=1)
        falso = Path(self.tmp.name) / "evolution.sh"
        falso.write_text("#!/bin/bash\nexit 2\n")
        env = {"VIGIA_DADOS": str(self.dados), "VIGIA_EVOLUTION_SH": str(falso), "VIGIA_ENV": str(Path(self.tmp.name) / "x.env")}
        with mock.patch.dict(os.environ, env):
            for extra in ([], ["--apagar"]):
                self.assertEqual(fs.main(["--dry-run", "--semana", SEMANA] + extra), 0)
        depois = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in self.dados.rglob("*") if p.is_file()}
        self.assertEqual(antes, depois)
        self.assertFalse((self.dados / "relatorios").exists())

    def test_banco_somente_leitura_de_arquivo_inexistente_nao_cria(self):
        with self.assertRaises(FileNotFoundError):
            Banco(str(Path(self.tmp.name) / "nao" / "vigia.db"), somente_leitura=True)
        self.assertFalse((Path(self.tmp.name) / "nao").exists())

    def test_simular_lista_guardas_na_ordem_do_apagar(self):
        fs = _importar_arquivo("bin/fechar-semana.py", "fechar_semana")
        L = fs.simular(SEMANA, dados=self.dados, banco=self.banco, psql_ro=lambda q: [["0"]], apagando=True,
                       agora=lambda: FIM + 600)
        g = [l.split(":")[0].replace("guarda ", "") for l in L if l.startswith("guarda ")]
        self.assertEqual(g, ["ja_apagada", "semana_aberta", "no_prazo", "adiada_segura", "sem_relatorio", "sem_aviso",
                             "aguardando_24h", "ha_nao_lidas"])
        self.assertIn("resultado de um run real agora: sem_relatorio", L)


class TestAvisoSemRedirect(unittest.TestCase):
    def test_redirect_nao_e_seguido(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        visto = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                visto.append(self.path)
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path == "/proxy":
                    self.send_response(307)
                    self.send_header("Location", "/outro")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"{}")

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        ok = aviso.enviar("ok", cofre_url=f"http://127.0.0.1:{srv.server_address[1]}", token_fn=lambda: "t")
        self.assertFalse(ok)
        self.assertEqual(visto, ["/proxy"])


if __name__ == "__main__":
    unittest.main()
