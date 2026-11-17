"""D-57 (pedido 45): fechamento do dia — app/consolidacao.py e bin/fechar-dia.py.
Quem chama: unittest. Banco SQLite temporário, FakeFrentes, psql e executor do Claude falsos; nenhuma rede real.
O que não faz: não chama o Claude, o Postgres nem o Frentes real.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import date, datetime, time as hora_t, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import consolidacao, lancamento  # noqa: E402
from app.ponte import consolidar, rebaixar  # noqa: E402
from app.banco import Banco  # noqa: E402
from app.erros import FrentesIndisponivel  # noqa: E402
from app.frentes_ponte import FrentesPonte  # noqa: E402
from testes.fakes import FakeFrentes  # noqa: E402
from testes.test_coletor import PAREADO, ExecutorFalso, PsqlFalso, _msg  # noqa: E402

RAIZ = Path(__file__).resolve().parents[1]
PROMPT = RAIZ / "prompts" / "consolidar.md"
CONFIG = {"horarios": {"diario": "21:00"}}
# O fechamento de referência é AMANHÃ às 21:00: todo cartão gravado agora (criado_em = relógio real) é anterior a ele,
# em qualquer hora em que a suíte rode.
FECHO = datetime.combine(date.today() + timedelta(days=1), hora_t(21, 0))
AGORA = FECHO.timestamp() + 60
ANA = "5592980000001@s.whatsapp.net"
RUI = "5592980000002@s.whatsapp.net"
JSON_FECHO = '{"conversas":1,"absorvidos":1,"rebaixados":0}'


def Exec(**kw):
    """Claude do fechamento que confere e não mexe em nada (o JSON final dele tem os três contadores do D-58)."""
    return ExecutorFalso(result=JSON_FECHO, **kw)


class ExecutorQueConsolida(ExecutorFalso):
    """Faz o que o Claude do fechamento faria: lê a pasta e funde `absorve` em `fica` pela ponte (aqui, direto no banco)."""

    def __init__(self, banco, fica=None, absorve=(), result=JSON_FECHO):
        super().__init__(result=result)
        self.banco, self.fica, self.absorve = banco, fica, list(absorve)
        self.arquivos: list[str] = []
        self.cartoes_md = self.conversa_md = ""
        self.por_conversa: dict[str, tuple[str, str]] = {}   # jid -> (cartoes.md, conversa.md) da subpasta dela

    def __call__(self, cmd, **kw):
        pasta = Path(kw["cwd"])
        self.arquivos = sorted(str(p.relative_to(pasta)) for p in pasta.rglob("*") if p.is_file())
        for p in sorted(pasta.rglob("cartoes.md")):
            cartoes, conversa = p.read_text(encoding="utf-8"), (p.parent / "conversa.md").read_text(encoding="utf-8")
            self.cartoes_md += cartoes
            self.conversa_md += conversa
            jid = next(ln.split("conversa: ", 1)[1].strip() for ln in cartoes.splitlines() if ln.startswith("conversa: "))
            self.por_conversa[jid] = (cartoes, conversa)
        for i in self.absorve:
            self.banco.substituir(i, self.fica, "consolidada")
        return super().__call__(cmd, **kw)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dados = Path(self.tmp.name) / "dados"
        self.fake = FakeFrentes().iniciar()
        self.addCleanup(self.fake.parar)
        self.banco = Banco(str(self.dados / "vigia.db"))
        self.ponte = FrentesPonte(self.fake.url, self.fake.token)
        self.psql = PsqlFalso([_msg(1, "podemos marcar amanhã 14h?", ts=PAREADO + 100),
                               _msg(2, "melhor 15h", ts=PAREADO + 200),
                               _msg(3, "fechado", ts=PAREADO + 300, jid=RUI, nome="Rui")])

    def cartao(self, msg_id, classe="demanda", conversa=ANA, **kw):
        base = dict(msg_id=msg_id, conversa=conversa, contato="Ana" if conversa == ANA else "Rui", ts=PAREADO + 100,
                    classe=classe, resumo=f"resumo {msg_id}", confianca="alta", frente_id=1)
        base.update(kw)
        return self.banco.gravar_classificacao(**base)

    def rodar(self, executor=None, agora=AGORA, **kw):
        return consolidacao.rodar(banco=self.banco, ponte=self.ponte, psql=self.psql, config=CONFIG, dados=self.dados,
                                  prompt_sistema=PROMPT, desde=PAREADO, executor=executor, agora=agora, **kw)


class TestRodar(Base):
    def test_antes_da_hora_nao_faz_nada(self):
        self.cartao("a")
        ex = Exec()
        r = self.rodar(ex, agora=FECHO.timestamp() - 60)
        self.assertEqual(r, {"status": "antes_da_hora", "fechamento": FECHO.strftime("%Y-%m-%d %H:%M")})
        self.assertEqual((ex.chamadas, self.fake.capturas), ([], []))
        self.assertEqual(len(self.banco.provisorias()), 1)

    def test_nada_pendente(self):
        self.cartao("a", "aviso")
        self.cartao("b", "atencao", item_id=5)
        ex = Exec()
        r = self.rodar(ex)
        self.assertEqual((r["status"], ex.chamadas), ("nada_pendente", []))
        self.assertEqual(self.fake.capturas, [])

    def test_dry_run_so_conta(self):
        self.cartao("a")
        self.cartao("b")
        self.cartao("c", conversa=RUI)
        ex = Exec()
        r = self.rodar(ex, dry_run=True)
        self.assertEqual((r["status"], r["conversas"], r["cartoes"], r["com_repetidos"], r["absorvidos"], r["rebaixados"],
                          r["lancados"]), ("dry_run", 2, 3, 1, 0, 0, 0))
        self.assertEqual((ex.chamadas, self.fake.capturas), ([], []))
        self.assertEqual(len(self.banco.provisorias()), 3)
        self.assertFalse((self.dados / "caixa").exists())

    def test_fecha_o_dia_consolida_e_sobe_o_que_sobrou(self):
        a = self.cartao("a", resumo="marcar reuniao 14h")
        b = self.cartao("b", resumo="reuniao 15h", ts=PAREADO + 200)
        c = self.cartao("c", conversa=RUI, resumo="fechado o orcamento")
        ex = ExecutorQueConsolida(self.banco, fica=b, absorve=[a])
        r = self.rodar(ex)
        self.assertEqual((r["status"], r["claude"], r["conversas"], r["cartoes"], r["com_repetidos"], r["absorvidos"],
                          r["rebaixados"], r["lancados"]), ("ok", "ok", 2, 3, 1, 1, 0, 2))
        # o passe do Claude (D-58): TODA conversa com provisório, uma subpasta por conversa, com cartoes.md (ids) e
        # conversa.md (mensagens do dia, [contexto])
        self.assertEqual(len(ex.chamadas), 1)
        cmd, kw = ex.chamadas[0]
        self.assertEqual(cmd[cmd.index("--system-prompt-file") + 1], str(PROMPT))
        self.assertEqual(cmd[cmd.index("--model") + 1], "sonnet")
        self.assertIn("2 conversa(s) a conferir", cmd[-1])
        self.assertIn("/cartoes.md e ", cmd[-1])
        self.assertEqual(sorted(Path(a).name for a in ex.arquivos), ["cartoes.md", "cartoes.md", "conversa.md", "conversa.md"])
        self.assertEqual(len({Path(a).parent for a in ex.arquivos}), 2)
        self.assertEqual(set(ex.por_conversa), {ANA, RUI})
        cartoes_ana, conversa_ana = ex.por_conversa[ANA]
        self.assertIn("## Cartões provisórios desta conversa", cartoes_ana)
        self.assertIn(f"- #{a} · ", cartoes_ana)
        self.assertIn(f"- #{b} · ", cartoes_ana)
        self.assertIn("demanda · alta · marcar reuniao 14h", cartoes_ana)
        self.assertIn("## Cartões desta conversa que já estão no Frentes (item aberto)\n\n- (nenhum)", cartoes_ana)
        self.assertIn(f"fechamento: {FECHO:%Y-%m-%d %H:%M}", cartoes_ana)
        self.assertNotIn("Rui", cartoes_ana)                              # cada subpasta só tem a conversa dela
        self.assertNotIn(f"- #{c} · ", cartoes_ana)
        self.assertIn("[contexto] [contato] podemos marcar amanhã 14h?", conversa_ana)
        self.assertIn("[contexto] [contato] melhor 15h", conversa_ana)
        self.assertNotIn("fechado", conversa_ana)
        cartoes_rui, conversa_rui = ex.por_conversa[RUI]
        self.assertIn(f"- #{c} · ", cartoes_rui)
        self.assertIn("[contexto] [contato] fechado", conversa_rui)
        self.assertNotIn("15h", conversa_rui)
        self.assertFalse(list((self.dados / "caixa").glob("*-fecho*")))   # a pasta é apagada depois do passe
        # o que sobrou subiu: b (consolidado) e c; a foi absorvido e não sobe
        self.assertEqual(sorted(x["texto"] for x in self.fake.capturas), ["WA Ana: reuniao 15h", "WA Rui: fechado o orcamento"])
        self.assertEqual(self.banco.provisorias(), [])
        for i in (b, c):
            reg = self.banco.classificacao(i)
            self.assertIsNotNone(reg["item_id"])
            self.assertIsNotNone(reg["lancado_em"])
            self.assertEqual(self.fake.itens[reg["item_id"]]["confirmado"], 1)
        velho = self.banco.classificacao(a)
        self.assertEqual((velho["substituida_por"], velho["item_id"], velho["lancado_em"]), (b, None, None))
        lote = self.banco.lotes(1)[0]
        self.assertEqual((lote["nome"], lote["conversas"], lote["itens"], lote["tokens_in"]), (f"fecho-{FECHO:%Y-%m-%d}", 2, 0, 15))
        self.assertFalse((self.dados / "fechamento-falhas.json").exists())
        # rodar de novo: nada pendente, nenhuma chamada a mais
        r = self.rodar(ex)
        self.assertEqual((r["status"], len(ex.chamadas), len(self.fake.capturas)), ("nada_pendente", 1, 2))

    def test_absorvidos_vem_do_banco_nao_do_que_o_claude_disse(self):
        self.cartao("a")
        self.cartao("b")
        ex = ExecutorFalso(result='{"conversas":1,"absorvidos":7,"rebaixados":4}')   # mentiu: não mexeu em nada
        r = self.rodar(ex)
        self.assertEqual((r["status"], r["absorvidos"], r["rebaixados"], r["lancados"]), ("ok", 0, 0, 2))
        self.assertEqual(len(self.fake.capturas), 2)

    def test_json_final_sem_rebaixados_e_falha_do_claude(self):   # D-58: o contrato do JSON final tem três contadores
        self.cartao("a")
        r = self.rodar(ExecutorFalso(result='{"conversas":1,"absorvidos":0}'))
        self.assertEqual((r["status"], r["claude"], r["tentativa"]), ("adiada", "falhou", 1))
        self.assertEqual(self.fake.capturas, [])

    def test_uma_por_conversa_tambem_passa_pela_regua(self):   # D-58: o último filtro vale para todo provisório
        self.cartao("a")
        self.cartao("c", conversa=RUI)
        ex = Exec()
        r = self.rodar(ex)
        self.assertEqual((r["status"], r["claude"], r["com_repetidos"], r["rebaixados"], r["lancados"]), ("ok", "ok", 0, 0, 2))
        self.assertEqual(len(ex.chamadas), 1)
        self.assertIn("2 conversa(s) a conferir", ex.chamadas[0][0][-1])
        self.assertEqual(self.banco.lotes(1)[0]["conversas"], 2)

    def test_cartao_segurado_no_fechamento_nao_sobe_e_fica_no_historico(self):   # D-58: ruído não vai ao Frentes
        ruido = self.cartao("a", resumo="perguntou se o dono viu o video")
        fusao = self.cartao("a2", resumo="mesma pergunta de novo", ts=PAREADO + 200)
        pedido = self.cartao("c", conversa=RUI, resumo="mandar o contrato assinado")
        banco, ponte = self.banco, self.ponte

        class Segura(ExecutorQueConsolida):
            def __call__(self, cmd, **kw):
                saida = super().__call__(cmd, **kw)
                consolidar.executar(argparse.Namespace(fica=ruido, absorve=[fusao], resumo=None, prazo=None), banco=banco, ponte=ponte)
                rebaixar.executar(argparse.Namespace(id=ruido, classe="aviso", motivo="o dono ja respondeu na conversa"),
                                  banco=banco, ponte=ponte)
                return saida

        r = self.rodar(Segura(self.banco, result='{"conversas":2,"absorvidos":1,"rebaixados":1}'))
        self.assertEqual((r["status"], r["cartoes"], r["absorvidos"], r["rebaixados"], r["lancados"]), ("ok", 3, 1, 1, 1))
        self.assertEqual([x["texto"] for x in self.fake.capturas], ["WA Rui: mandar o contrato assinado"])
        reg = self.banco.classificacao(ruido)
        self.assertEqual((reg["classe"], reg["classe_original"], reg["item_id"], reg["lancado_em"]), ("aviso", "demanda", None, None))
        self.assertIsNotNone(self.banco.classificacao(pedido)["item_id"])
        self.assertEqual(self.banco.provisorias(), [])
        self.assertEqual(sorted(x["id"] for x in self.banco.classificacoes()), sorted([ruido, pedido]))   # o segurado continua na Demandas
        self.assertEqual(len(self.banco.classificacoes(substituidas=None)), 3)                            # nada some
        self.assertEqual(self.rodar(Exec())["status"], "nada_pendente")   # e não volta a subir no ciclo seguinte

    def test_confianca_baixa_sobe_confirmada_e_frente_arquivada_vai_ao_inbox(self):   # D-58: nada nasce "a confirmar"
        baixa = self.cartao("a", confianca="baixa")
        arquivada = self.cartao("b", conversa=RUI, frente_id=999)
        r = self.rodar(Exec())
        self.assertEqual((r["status"], r["lancados"]), ("ok", 2))
        rb = self.banco.classificacao(baixa)
        self.assertEqual((self.fake.itens[rb["item_id"]]["confirmado"], rb["confianca"]), (1, "baixa"))
        ra = self.banco.classificacao(arquivada)
        self.assertEqual((ra["frente_id"], ra["confianca"], self.fake.itens[ra["item_id"]]["frente_id"]), (None, "baixa", None))
        self.assertEqual(self.fake.itens[ra["item_id"]]["confirmado"], 1)
        self.assertEqual(self.fake.patches, [])

    # ---- D-58: o fechamento confere o provisório contra o que JÁ subiu daquela conversa

    def _lancado(self, msg_id="ja", **kw):
        """Um cartão que já está no Frentes com item aberto (como o que a leitura lançou na hora ou num dia anterior)."""
        cid = self.cartao(msg_id, classe="lembrar", **kw)
        reg = self.banco.classificacao(cid)
        lancamento.lancar(self.banco, self.ponte, reg)
        return self.banco.classificacao(cid)

    def test_um_provisorio_e_um_ja_lancado_chama_o_claude_e_funde_no_que_ja_subiu(self):
        ja = self._lancado(resumo="reuniao quinta 10h", prazo="2030-01-10")
        prov = self.cartao("b", resumo="reuniao remarcada para 15h", ts=PAREADO + 200)
        banco, ponte = self.banco, self.ponte

        class Funde(ExecutorQueConsolida):
            def __call__(self, cmd, **kw):
                saida = super().__call__(cmd, **kw)
                consolidar.executar(argparse.Namespace(fica=ja["id"], absorve=[prov], resumo="reuniao quinta 15h", prazo="2030-01-10"),
                                    banco=banco, ponte=ponte)
                return saida

        ex = Funde(self.banco)
        r = self.rodar(ex)
        self.assertEqual((r["status"], r["claude"], r["cartoes"], r["com_repetidos"], r["com_lancados"], r["absorvidos"], r["lancados"]),
                         ("ok", "ok", 1, 1, 1, 1, 0))
        self.assertEqual(len(ex.chamadas), 1)
        self.assertIn(f"- #{prov} · ", ex.cartoes_md)
        self.assertIn(f"lembrar · alta · reuniao quinta 10h · prazo 2030-01-10 · no Frentes #{ja['item_id']}", ex.cartoes_md)
        self.assertEqual(len(self.fake.capturas), 1)                                # nenhum segundo item
        self.assertEqual(self.fake.itens[ja["item_id"]]["texto"], "WA Ana: reuniao quinta 15h (prazo 2030-01-10)")
        self.assertEqual(self.banco.classificacao(ja["id"])["resumo"], "reuniao quinta 15h")
        self.assertEqual(self.banco.classificacao(prov)["substituida_por"], ja["id"])

    def test_ja_lancado_com_item_fechado_recusado_ou_de_outra_conversa_nao_entra_na_lista_do_que_ja_subiu(self):
        fechado = self._lancado("f")
        self.fake.itens[fechado["item_id"]]["status"] = "feito"
        recusado = self._lancado("r")
        self.banco.decidir(recusado["id"], "recusado")
        outra = self._lancado("o", conversa=RUI)
        self.cartao("b")
        ex = ExecutorQueConsolida(self.banco)
        r = self.rodar(ex)
        self.assertEqual((r["status"], r["com_repetidos"], r["com_lancados"], r["lancados"]), ("ok", 0, 0, 1))
        self.assertEqual(set(ex.por_conversa), {ANA})                      # o Rui não tem provisório: não vai ao passe
        self.assertIn("## Cartões desta conversa que já estão no Frentes (item aberto)\n\n- (nenhum)", ex.cartoes_md)
        for reg in (fechado, recusado, outra):
            self.assertNotIn(f"- #{reg['id']} · ", ex.cartoes_md)

    def test_dry_run_conta_os_ja_lancados_sem_chamar_o_frentes(self):
        self._lancado()
        self.cartao("b")
        self.fake.parar()
        r = self.rodar(dry_run=True)
        self.assertEqual((r["status"], r["cartoes"], r["com_repetidos"], r["com_lancados"]), ("dry_run", 1, 1, 1))

    def test_claude_falhando_adia_e_na_terceira_sobe_sem_consolidar(self):
        self.cartao("a")
        self.cartao("b")
        ex = ExecutorFalso(stdout="nao json")
        for n in (1, 2, 3):
            r = self.rodar(ex)
            self.assertEqual((r["status"], r["claude"], r["tentativa"], r["lancados"]), ("adiada", "falhou", n, 0))
            self.assertEqual(json.loads((self.dados / "fechamento-falhas.json").read_text())["n"], n)
            self.assertEqual(self.fake.capturas, [])          # nada sobe enquanto a consolidação não rodou
            self.assertFalse(list((self.dados / "caixa").glob("*-fecho*")))
        r = self.rodar(ex)
        self.assertEqual((r["status"], r["claude"], r["lancados"], len(ex.chamadas)), ("ok", "pulado_apos_falhas", 2, 3))
        self.assertEqual(len(self.fake.capturas), 2)
        self.assertFalse((self.dados / "fechamento-falhas.json").exists())
        self.assertEqual(self.banco.lotes(1), [])            # sem passe do Claude, sem lote registrado

    def test_falhas_de_outro_fechamento_nao_contam(self):
        self.cartao("a")
        self.cartao("b")
        (self.dados / "fechamento-falhas.json").write_text(json.dumps({"fechamento": "2000-01-01 21:00:00", "n": 3}))
        r = self.rodar(ExecutorFalso(stdout="nao json"))
        self.assertEqual((r["status"], r["tentativa"]), ("adiada", 1))

    def test_cartao_criado_depois_do_fechamento_e_de_amanha(self):
        a = self.cartao("a")
        depois = self.cartao("b", conversa=RUI)
        limite = FECHO.strftime("%Y-%m-%d %H:%M:%S")
        self.banco._escrever("UPDATE classificacoes SET criado_em=? WHERE id=?", ((FECHO + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"), depois))
        r = self.rodar(Exec())
        self.assertEqual((r["status"], r["cartoes"], r["lancados"]), ("ok", 1, 1))
        self.assertIsNotNone(self.banco.classificacao(a)["item_id"])
        self.assertIsNone(self.banco.classificacao(depois)["item_id"])
        self.assertEqual([x["id"] for x in self.banco.provisorias()], [depois])
        self.assertEqual(self.banco.provisorias(ate=limite), [])

    def test_decididos_com_item_e_substituidos_ficam_de_fora(self):
        rec = self.cartao("a")
        self.banco.decidir(rec, "recusado")
        self.cartao("b", item_id=5)
        sub = self.cartao("c")
        fica = self.cartao("d")
        self.banco.substituir(sub, fica, "consolidada")
        r = self.rodar(Exec())
        self.assertEqual((r["status"], r["cartoes"], r["lancados"]), ("ok", 1, 1))
        self.assertEqual(len(self.fake.capturas), 1)
        self.assertIsNone(self.banco.classificacao(sub)["item_id"])

    def test_frentes_fora_sobe_a_excecao_e_o_cartao_fica_provisorio(self):
        self.cartao("a")
        self.fake.parar()
        with self.assertRaises(FrentesIndisponivel):
            self.rodar(Exec())
        self.assertEqual(len(self.banco.provisorias()), 1)

    def test_pendentes_e_mensagem(self):
        a = self.cartao("a")
        b = self.cartao("b", conversa=RUI)
        self.assertEqual({k: [x["id"] for x in v] for k, v in consolidacao.pendentes(self.banco, "2100-01-01 00:00:00").items()},
                         {ANA: [a], RUI: [b]})
        pasta = Path(self.tmp.name) / "fecho"
        (pasta / "x").mkdir(parents=True)
        (pasta / "x" / "cartoes.md").write_text("")
        m = consolidacao.mensagem(pasta)
        self.assertIn(f"Feche o dia dos cartões em {pasta.resolve()}: 1 conversa(s)", m)
        self.assertIn(f"- {pasta.resolve() / 'x'}/cartoes.md e {pasta.resolve() / 'x'}/conversa.md", m)
        self.assertTrue(m.endswith("termine com a linha JSON final."))


class TestLancamento(unittest.TestCase):
    def test_lanca_na_hora(self):
        hoje = date(2026, 11, 9)
        self.assertTrue(lancamento.lanca_na_hora("atencao", None, hoje))
        self.assertTrue(lancamento.lanca_na_hora("lembrar", "2026-11-10", hoje))
        self.assertTrue(lancamento.lanca_na_hora("demanda", "2026-11-09", hoje))
        self.assertTrue(lancamento.lanca_na_hora("demanda", "2026-11-01", hoje))   # atrasado também sobe
        self.assertFalse(lancamento.lanca_na_hora("demanda", "2026-11-11", hoje))
        self.assertFalse(lancamento.lanca_na_hora("demanda", None, hoje))
        self.assertFalse(lancamento.lanca_na_hora("lembrar", "amanha", hoje))

    def test_hora_do_fechamento_e_rotulo(self):
        self.assertEqual(lancamento.rotulo_fechamento({"horarios": {"diario": "20:30"}}), "20:30")
        self.assertEqual(lancamento.rotulo_fechamento({"horarios": {"diario": "meia-noite"}}), "21:00")
        self.assertEqual(lancamento.rotulo_fechamento({}), "21:00")
        self.assertEqual(lancamento.rotulo_fechamento(None), "21:00")
        self.assertEqual(lancamento.fechamento_do_dia(datetime(2026, 11, 9, 8, 0), CONFIG), datetime(2026, 11, 9, 21, 0))

    def test_texto_do_cartao_e_classe_efetiva(self):
        self.assertEqual(lancamento.texto_do_cartao("Ana", "pagar boleto", "2026-11-20"), "WA Ana: pagar boleto (prazo 2026-11-20)")
        self.assertEqual(lancamento.texto_do_cartao("Ana", "pagar ate 2026-11-20", "2026-11-20"), "WA Ana: pagar ate 2026-11-20")
        self.assertEqual(lancamento.texto_do_cartao(None, None, None, "demanda"), "WA ?: demanda")
        self.assertEqual(lancamento.classe_efetiva({"classe": "aviso", "classe_curada": "demanda"}), "demanda")
        self.assertTrue(lancamento.eh_provisoria({"classe": "aviso", "classe_curada": "demanda"}))
        self.assertFalse(lancamento.eh_provisoria({"classe": "demanda", "item_id": 1}))
        self.assertFalse(lancamento.eh_provisoria({"classe": "demanda", "lancado_em": "x"}))
        self.assertFalse(lancamento.eh_provisoria({"classe": "demanda", "decisao": "recusado"}))
        self.assertFalse(lancamento.eh_provisoria({"classe": "demanda", "substituida_por": 3}))
        self.assertFalse(lancamento.eh_provisoria({"classe": "lembrando"}))


class TestFecharDiaCLI(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        base = Path(self._t.name)
        self.dados = base / "dados"
        self.dados.mkdir()
        self.script = base / "evolution.sh"
        self.script.write_text("#!/bin/bash\nexit 0\n")
        self.script.chmod(0o755)
        self.env = dict(os.environ, VIGIA_EVOLUTION_SH=str(self.script), VIGIA_DADOS=str(self.dados),
                        VIGIA_ENV=str(base / ".env"))

    def _cli(self, *args):
        return subprocess.run([sys.executable, str(RAIZ / "bin/fechar-dia.py"), *args], capture_output=True, text=True,
                              timeout=60, stdin=subprocess.DEVNULL, env=self.env)

    def test_help_sai_0(self):
        r = self._cli("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("--dry-run", r.stdout)

    def test_sem_pareamento_sai_2_sem_criar_nada(self):
        (self.dados / "cursor.json").write_text(json.dumps({"ultimo_seq": 0}))
        r = self._cli()
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("não rodou", r.stderr)
        self.assertFalse((self.dados / "caixa").exists())

    def test_pareado_sai_0_com_json_em_uma_linha(self):
        (self.dados / "cursor.json").write_text(json.dumps({"ultimo_seq": 0, "pareado_em": PAREADO}))
        t0 = time.monotonic()
        r = self._cli("--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(time.monotonic() - t0, 10.0)
        linhas = r.stdout.strip().splitlines()
        self.assertEqual(len(linhas), 1)
        j = json.loads(linhas[0])
        self.assertIn(j["status"], ("antes_da_hora", "nada_pendente"))
        self.assertRegex(j["fechamento"], r"^\d{4}-\d{2}-\d{2} 21:00$")


if __name__ == "__main__":
    unittest.main()
