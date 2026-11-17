"""Teste próprio da estação ponte (E7): comandos classificar/atualizar/frentes e o CLI bin/vigia-ponte.
Quem chama: unittest. Usa Banco SQLite temporário e FakeFrentes; nenhuma rede real.
O que não faz: não toca Evolution nem o Frentes real.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.banco import Banco  # noqa: E402
from app.erros import FrentesIndisponivel  # noqa: E402
from app.frentes_ponte import FrentesPonte  # noqa: E402
from app.ponte import atualizar, classificar, consolidar, frentes, historico, rebaixar  # noqa: E402
from testes.fakes import FakeFrentes  # noqa: E402
from testes.test_contrato_ponte import args_classificar  # noqa: E402

RAIZ = Path(__file__).resolve().parents[1]
CLI = RAIZ / "bin" / "vigia-ponte"
AMANHA = (date.today() + timedelta(days=1)).isoformat()     # prazo em 24 h: sobe na hora (D-57)
LONGE = (date.today() + timedelta(days=5)).isoformat()      # prazo distante: espera o fechamento do dia


class Base(unittest.TestCase):
    def setUp(self):
        antes = os.environ.pop("VIGIA_LOTE", None)
        self.addCleanup(lambda: os.environ.pop("VIGIA_LOTE", None) if antes is None else os.environ.__setitem__("VIGIA_LOTE", antes))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = FakeFrentes().iniciar()
        self.addCleanup(self.fake.parar)
        self.db = str(Path(self.tmp.name) / "vigia.db")
        self.banco = Banco(self.db)
        self.ponte = FrentesPonte(self.fake.url, self.fake.token)

    def cls(self, *a, **k):
        return classificar.executar(args_classificar(*a, **k), banco=self.banco, ponte=self.ponte)

    def lote(self, manifesto, subpasta="2026-11-06_1000/conv-a"):
        """Cria <tmp>/lote/<subpasta>/mensagens.json e aponta VIGIA_LOTE para <tmp>/lote/<AAAA..>."""
        raiz = Path(self.tmp.name) / "lote" / subpasta.split("/")[0]
        pasta = Path(self.tmp.name) / "lote" / subpasta
        pasta.mkdir(parents=True)
        (pasta / "mensagens.json").write_text(json.dumps(manifesto), encoding="utf-8")
        os.environ["VIGIA_LOTE"] = str(raiz)
        return raiz


class TestClassificar(Base):
    def test_classe_invalida(self):
        with self.assertRaises(ValueError):
            self.cls("m1", "urgentissimo")
        self.assertEqual(self.fake.capturas, [])

    def test_confianca_invalida(self):
        with self.assertRaises(ValueError):
            self.cls("m1", "demanda", confianca="media")

    def test_grava_item_id_e_texto(self):
        r = self.cls("m1", "atencao", resumo="Maria pede proposta", frente=2)
        cap = self.fake.capturas[0]
        self.assertEqual(cap["origem"], "whatsapp")
        self.assertEqual(cap["frente_id"], 2)
        self.assertIn("Maria pede proposta", cap["texto"])
        linha = self.banco.classificacoes()[0]
        self.assertEqual(linha["item_id"], r["item_id"])
        self.assertEqual(linha["classe"], "atencao")

    def test_repetido_devolve_existente_com_item(self):
        a = self.cls("m1", "atencao")
        b = self.cls("m1", "atencao")
        self.assertTrue(b["repetido"])
        self.assertEqual(a["item_id"], b["item_id"])
        self.assertEqual(len(self.fake.capturas), 1)

    def test_repetido_provisorio_nao_grava_duas_vezes(self):   # D-57
        a = self.cls("m1", "demanda")
        b = self.cls("m1", "demanda")
        self.assertTrue(b["repetido"])
        self.assertEqual((a["id"], a["item_id"], b["item_id"]), (b["id"], None, None))
        self.assertEqual(len(self.banco.provisorias()), 1)
        self.assertEqual(self.fake.capturas, [])

    def test_resumo_vazio_nao_manda_texto_vazio(self):
        self.cls("m1", "atencao", resumo="   ", contato="")
        self.assertTrue(self.fake.capturas[0]["texto"].strip())

    def test_sem_acao_e_aviso_nao_capturam(self):
        self.cls("m1", "sem_acao")
        self.cls("m2", "aviso")
        self.assertEqual(self.fake.capturas, [])
        self.assertEqual(len(self.banco.classificacoes()), 2)

    def test_lembrando_sem_item_recusa(self):
        with self.assertRaises(ValueError):
            self.cls("m1", "lembrando", item=None)
        self.assertEqual(self.fake.capturas, [])
        self.assertEqual(self.banco.classificacoes(), [])

    def test_lembrando_item_inexistente_ou_de_outra_origem_recusa(self):
        manual = self.fake.semear_item(texto="manual", origem="manual")
        with self.assertRaises(ValueError):
            self.cls("m1", "lembrando", item=manual["id"])
        with self.assertRaises(ValueError):
            self.cls("m2", "lembrando", item=999)
        fechado = self.fake.semear_item(texto="x", origem="whatsapp", status="feito")
        with self.assertRaises(ValueError):
            self.cls("m3", "lembrando", item=fechado["id"])
        self.assertEqual(self.banco.classificacoes(), [])
        self.assertEqual(self.fake.patches, [])

    def test_lembrando_grava_nota_local_e_so_prioridade_vai_ao_frentes(self):
        item = self.ponte.capturar("x", frente_id=1)["itens"][0]
        antes = len(self.fake.capturas)
        r = self.cls("m1", "lembrando", resumo="cobrou de novo", item=item["id"], prioridade=1)
        self.assertEqual(len(self.fake.capturas), antes)
        self.assertEqual(self.fake.patches[-1]["corpo"], {"prioridade": 1})
        self.assertEqual(r["item_id"], item["id"])
        linha = self.banco.classificacao_por_msg_id("m1")
        self.assertEqual((linha["classe"], linha["item_id"], linha["resumo"]), ("lembrando", item["id"], "cobrou de novo"))

    def test_lembrando_sem_prioridade_nao_chama_frentes(self):
        item = self.ponte.capturar("x", frente_id=1)["itens"][0]
        self.cls("m1", "lembrando", item=item["id"])
        self.assertEqual(self.fake.patches, [])
        self.assertEqual(self.banco.classificacao_por_msg_id("m1")["classe"], "lembrando")

    def test_lembrando_nao_rebaixa_prioridade(self):
        item = self.fake.semear_item(texto="x", origem="whatsapp", prioridade=1)
        r = self.cls("m1", "lembrando", item=item["id"], prioridade=2)
        self.assertEqual(self.fake.patches, [])
        self.assertIsNone(r["prioridade"])

    def test_lembrando_idempotente_nao_toca_frentes(self):
        item = self.ponte.capturar("x", frente_id=1)["itens"][0]
        self.cls("m1", "lembrando", item=item["id"], prioridade=1)
        n = len(self.fake.patches)
        chamadas = len(self.fake.chamadas)
        r = self.cls("m1", "lembrando", item=item["id"], prioridade=1)
        self.assertTrue(r["repetido"])
        self.assertEqual(len(self.fake.patches), n)
        self.assertEqual(len(self.fake.chamadas), chamadas)

    def test_resumo_proibido_recusa_sem_gravar(self):
        for ruim in ('com "aspas"', "com `crase`", "custa $5", "a; b", "a | b", "a & b", "a > b", "a < b",
                     "quebra\nlinha", "quebra\rlinha", "x" * 301):
            with self.assertRaises(ValueError, msg=ruim):
                self.cls("m1", "demanda", resumo=ruim)
        self.assertEqual(self.fake.capturas, [])
        self.assertEqual(self.banco.classificacoes(), [])

    def test_resumo_aspas_simples_e_300_ok(self):
        self.cls("m1", "demanda", resumo="Maria pede o 'relatorio'")
        self.cls("m2", "aviso", resumo="x" * 300)
        self.assertEqual(len(self.banco.classificacoes()), 2)

    def test_frente_invalida_vai_ao_inbox_com_baixa_confianca(self):
        r = self.cls("m1", "atencao", frente=999)
        self.assertTrue(r["frente_invalida"])
        self.assertIsNone(self.fake.capturas[0]["frente_id"])
        self.assertEqual(self.fake.patches, [])            # D-58: nada nasce "a confirmar"; baixa é só registro
        linha = self.banco.classificacao_por_msg_id("m1")
        self.assertEqual(linha["confianca"], "baixa")
        self.assertIsNone(linha["frente_id"])

    def test_frente_invalida_no_provisorio_vira_inbox_baixa_sem_capturar(self):   # D-57
        r = self.cls("m1", "demanda", frente=999)
        self.assertTrue(r["frente_invalida"])
        self.assertTrue(r["provisorio"])
        self.assertEqual((self.fake.capturas, self.fake.patches), ([], []))
        linha = self.banco.classificacao_por_msg_id("m1")
        self.assertEqual((linha["confianca"], linha["frente_id"], linha["item_id"]), ("baixa", None, None))

    def test_capturar_recebe_tipo_prioridade_prazo_do_mapa(self):
        self.cls("m1", "lembrar", prazo=AMANHA)   # prazo em 24 h sobe na hora (D-57)
        cap = self.fake.capturas[0]
        self.assertEqual((cap["tipo"], cap["prioridade"], cap["prazo"]), ("prazo", 2, AMANHA))
        self.cls("m2", "atencao")
        cap = self.fake.capturas[1]
        self.assertEqual((cap["tipo"], cap["prioridade"]), ("tarefa", 1))

    # ------------------------------------------------------------------ D-57: lançamento adiado (pedido 45)

    def test_demanda_fica_provisoria_sem_subir_ao_frentes(self):
        r = self.cls("m1", "demanda", resumo="mandar proposta")
        self.assertEqual(self.fake.capturas, [])
        self.assertEqual((r["cartao_item"], r["lancado"], r["provisorio"], r["item_id"], r["criou_item"]),
                         (True, False, True, None, False))
        linha = self.banco.classificacao(r["id"])
        self.assertEqual((linha["item_id"], linha["lancado_em"], linha["frente_id"]), (None, None, 1))  # a frente fica guardada
        self.assertEqual([x["id"] for x in self.banco.provisorias()], [r["id"]])

    def test_prazo_ate_amanha_sobe_na_hora_e_prazo_longe_espera(self):
        r1 = self.cls("m1", "lembrar", prazo=AMANHA)
        self.assertTrue(r1["lancado"])
        self.assertNotIn("provisorio", r1)
        self.assertIn(f"(prazo {AMANHA})", self.fake.capturas[0]["texto"])
        r2 = self.cls("m2", "lembrar", prazo=LONGE)
        self.assertTrue(r2["provisorio"])
        self.assertEqual(len(self.fake.capturas), 1)
        r3 = self.cls("m3", "atencao", prazo=LONGE)   # atenção sobe sempre
        self.assertTrue(r3["lancado"])
        self.assertEqual(len(self.fake.capturas), 2)

    def test_frentes_fora_depois_de_gravar_deixa_o_cartao_provisorio(self):
        self.fake.parar()
        with self.assertRaises(FrentesIndisponivel):
            self.cls("m1", "atencao", frente=None)   # sem --frente não há validação prévia: grava, depois tenta subir
        linha = self.banco.classificacao_por_msg_id("m1")
        self.assertIsNotNone(linha)
        self.assertEqual((linha["item_id"], linha["lancado_em"]), (None, None))
        self.assertEqual(len(self.banco.provisorias()), 1)   # sobe no fechamento do dia

    def test_prazo_invalido_recusa(self):
        with self.assertRaises(ValueError):
            self.cls("m1", "lembrar", prazo="amanha")

    def test_sem_lote_e_sem_conversa_recusa(self):
        with self.assertRaises(ValueError):
            self.cls("m1", "demanda", conversa="", ts=None)
        self.assertEqual(self.fake.capturas, [])

    def test_manifesto_fornece_conversa_contato_ts(self):
        self.lote({"cuid1": {"conversa": "5592@s.whatsapp.net", "contato": "Ana", "ts": 1_700_000_500,
                             "from_me": False, "lote": "2026-11-06_1000/conv-a"}})
        r = self.cls("cuid1", "atencao", conversa="outra", contato="Fulano", ts=1_600_000_000)
        linha = self.banco.classificacao_por_msg_id("cuid1")
        self.assertEqual((linha["conversa"], linha["contato"], linha["ts"]), ("5592@s.whatsapp.net", "Ana", 1_700_000_500))
        self.assertIn("WA Ana:", self.fake.capturas[0]["texto"])
        self.assertTrue(r["ok"])

    def test_msg_id_fora_do_manifesto_recusa_sem_gravar(self):
        self.lote({"cuid1": {"conversa": "c", "contato": "Ana", "ts": 1_700_000_500, "from_me": False, "lote": "x"}})
        with self.assertRaises(ValueError) as cm:
            self.cls("outro", "demanda")
        self.assertEqual(str(cm.exception), "msg_id fora do lote")
        self.assertEqual(self.fake.capturas, [])
        self.assertEqual(self.banco.classificacoes(), [])

    def test_manifesto_varre_varias_subpastas(self):
        raiz = self.lote({"a1": {"conversa": "c1", "contato": "A", "ts": 1_700_000_001, "from_me": False, "lote": "x"}})
        outra = raiz / "conv-b"
        outra.mkdir()
        (outra / "mensagens.json").write_text(json.dumps(
            {"b1": {"conversa": "c2", "contato": "B", "ts": 1_700_000_002, "from_me": True, "lote": "y"}}), encoding="utf-8")
        self.cls("b1", "aviso")
        self.assertEqual(self.banco.classificacao_por_msg_id("b1")["conversa"], "c2")

    def test_imagem_so_basename(self):
        self.cls("m1", "aviso", imagem="/etc/../x/foto.jpg")
        self.assertEqual(self.banco.classificacoes()[0]["imagem"], "foto.jpg")

    def test_frentes_fora_do_ar_nao_grava(self):
        self.fake.parar()
        with self.assertRaises(FrentesIndisponivel):
            self.cls("m1", "demanda")
        self.assertEqual(self.banco.classificacoes(), [])


class TestSubstituicao(Base):
    """D-57: `--substitui ID` (o evento mudou) e `--cancela ID` (o evento caiu) na mesma conversa."""

    def _velho(self, **kw):
        base = dict(resumo="reuniao 14h", prazo=AMANHA)
        base.update(kw)
        return self.cls("m1", "atencao", **base)   # atenção sobe na hora: o cartão antigo tem item

    def test_substitui_atualiza_texto_e_prazo_no_frentes_e_herda_o_item(self):
        velho = self._velho()
        item = velho["item_id"]
        r = self.cls("m2", "lembrar", resumo="reuniao remarcada para 15h", prazo=LONGE, substitui=velho["id"])
        self.assertEqual((r["substituiu"], r["substituicao"], r["frentes"], r["item_id"], r["lancado"]),
                         (velho["id"], "atualizada", "atualizado", item, True))
        self.assertNotIn("provisorio", r)
        self.assertEqual(len(self.fake.capturas), 1)               # nenhum item novo
        corpo = self.fake.patches[-1]["corpo"]
        self.assertEqual(corpo["prazo"], LONGE)
        self.assertEqual(corpo["texto"], f"WA Maria: reuniao remarcada para 15h (prazo {LONGE})")
        antigo = self.banco.classificacao(velho["id"])
        self.assertEqual((antigo["substituida_por"], antigo["substituicao_motivo"]), (r["id"], "atualizada"))
        self.assertRegex(antigo["substituida_em"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        novo = self.banco.classificacao(r["id"])
        self.assertEqual((novo["item_id"], novo["frente_id"]), (item, 1))
        self.assertIsNotNone(novo["lancado_em"])
        self.assertEqual([x["id"] for x in self.banco.classificacoes()], [r["id"]])                     # o velho saiu
        self.assertEqual([x["id"] for x in self.banco.classificacoes(substituidas=True)], [velho["id"]])

    def test_item_editado_pelo_dono_ganha_a_correcao_sem_perder_as_palavras_dele(self):   # D-58
        velho = self._velho()
        item = velho["item_id"]
        self.fake.itens[item]["texto"] = "Reunião com a Maria (eu editei)"
        r = self.cls("m2", "lembrar", resumo="remarcada 15h", prazo=LONGE, substitui=velho["id"])
        self.assertEqual((r["frentes"], r["item_id"]), ("corrigido_editado", item))   # continua no mesmo item
        self.assertEqual(self.fake.patches[-1]["corpo"],
                         {"texto": f"Reunião com a Maria (eu editei) · atualizado: remarcada 15h (prazo {LONGE})", "prazo": LONGE})
        self.assertEqual(self.banco.classificacao(velho["id"])["substituida_por"], r["id"])
        # a segunda correção troca a primeira, não empilha
        r2 = self.cls("m3", "lembrar", resumo="agora 16h", prazo=LONGE, substitui=r["id"])
        self.assertEqual(r2["frentes"], "corrigido_editado")
        self.assertEqual(self.fake.itens[item]["texto"], f"Reunião com a Maria (eu editei) · atualizado: agora 16h (prazo {LONGE})")
        self.assertEqual(len(self.fake.capturas), 1)               # nenhum item novo

    def test_correcao_em_item_editado_respeita_o_limite_de_texto(self):
        velho = self._velho()
        self.fake.itens[velho["item_id"]]["texto"] = "e" * 390
        self.cls("m2", "lembrar", resumo="remarcada para as 15h", substitui=velho["id"])
        texto = self.fake.itens[velho["item_id"]]["texto"]
        self.assertEqual(len(texto), 400)
        self.assertTrue(texto.startswith("e" * 390 + " · atu") and texto.endswith("…"))

    def test_prazo_editado_pelo_dono_nao_impede_a_correcao(self):   # D-58
        velho = self._velho()
        self.fake.itens[velho["item_id"]]["prazo"] = "2030-01-01"
        r = self.cls("m2", "lembrar", resumo="x", substitui=velho["id"])
        self.assertEqual(r["frentes"], "atualizado")                                   # o texto era o da ponte: troca inteira
        self.assertEqual(self.fake.patches[-1]["corpo"], {"texto": "WA Maria: x"})     # sem prazo novo, o prazo dele fica
        self.assertEqual(self.fake.itens[velho["item_id"]]["prazo"], "2030-01-01")

    def test_substitui_provisorio_nao_toca_o_frentes(self):
        velho = self.cls("m1", "demanda", resumo="orcamento ate sexta")
        r = self.cls("m2", "demanda", resumo="orcamento ate segunda", substitui=velho["id"])
        self.assertEqual((r["frentes"], r["provisorio"]), ("nenhum", True))
        self.assertEqual(self.fake.capturas, [])
        self.assertEqual([x["id"] for x in self.banco.provisorias()], [r["id"]])   # só o novo sobe no fechamento

    def test_item_ja_fechado_no_frentes_segue_sozinho(self):
        velho = self._velho()
        self.fake.itens[velho["item_id"]]["status"] = "feito"
        r = self.cls("m2", "demanda", resumo="de novo", substitui=velho["id"])
        self.assertEqual((r["frentes"], r["provisorio"], r["item_id"]), ("fechado", True, None))

    def test_cancela_cancela_o_item_e_o_novo_cartao_e_aviso(self):
        velho = self._velho()
        r = self.cls("m2", "aviso", resumo="reuniao cancelada", cancela=velho["id"])
        self.assertEqual((r["substituiu"], r["substituicao"], r["frentes"], r["item_id"], r["cartao_item"]),
                         (velho["id"], "cancelada", "cancelado", None, False))
        self.assertEqual(self.fake.patches[-1], {"id": velho["item_id"], "corpo": {"status": "cancelado"}})
        self.assertEqual(self.banco.classificacao(velho["id"])["substituicao_motivo"], "cancelada")
        self.assertEqual(self.banco.classificacoes(), [self.banco.classificacao(r["id"])])

    def test_cancela_fecha_tambem_o_item_editado(self):   # D-58: a IA corrige; Restaurar reabre
        velho = self._velho()
        self.fake.itens[velho["item_id"]]["texto"] = "editado"
        r = self.cls("m2", "aviso", resumo="cancelou", cancela=velho["id"])
        self.assertEqual(r["frentes"], "cancelado")
        self.assertEqual(self.fake.itens[velho["item_id"]]["status"], "cancelado")
        self.assertEqual(self.fake.itens[velho["item_id"]]["texto"], "editado")   # as palavras dele não mudam

    def test_recusado_pelo_dono_nao_e_substituido_e_o_comando_segue(self):
        velho = self._velho()
        self.banco.decidir(velho["id"], "recusado")
        n = len(self.fake.patches)
        r = self.cls("m2", "demanda", resumo="x", substitui=velho["id"])
        self.assertEqual(r["nao_substituiu"], "cartao_recusado_pelo_dono")
        self.assertNotIn("substituiu", r)
        self.assertTrue(r["provisorio"])
        self.assertIsNone(self.banco.classificacao(velho["id"])["substituida_por"])
        self.assertEqual(len(self.fake.patches), n)

    def test_recusas_nao_gravam_nada(self):
        velho = self._velho()
        outro = self.cls("m9", "demanda", conversa="outra@s.whatsapp.net")
        casos = [("x1", "demanda", dict(substitui=velho["id"], cancela=velho["id"])),   # os dois
                 ("x2", "demanda", dict(substitui=999)),                                # inexistente
                 ("x3", "demanda", dict(substitui=outro["id"])),                        # outra conversa
                 ("x4", "aviso", dict(substitui=velho["id"])),                          # --substitui exige classe que vira item
                 ("x5", "lembrando", dict(item=velho["item_id"], substitui=velho["id"]))]
        for msg, classe, kw in casos:
            with self.assertRaises(ValueError, msg=msg):
                self.cls(msg, classe, **kw)
            self.assertIsNone(self.banco.classificacao_por_msg_id(msg), msg)
        self.assertIsNone(self.banco.classificacao(velho["id"])["substituida_por"])
        self.assertEqual(len(self.fake.capturas), 1)

    def test_cartao_ja_substituido_recusa_e_a_cadeia_segue_pelo_atual(self):
        velho = self._velho()
        novo = self.cls("m2", "lembrar", resumo="15h", substitui=velho["id"])
        with self.assertRaises(ValueError):
            self.cls("m3", "lembrar", resumo="16h", substitui=velho["id"])
        r = self.cls("m3", "lembrar", resumo="16h", substitui=novo["id"])
        self.assertEqual(self.banco.classificacao(novo["id"])["substituida_por"], r["id"])
        self.assertEqual(r["item_id"], velho["item_id"])   # o mesmo item do Frentes atravessa a cadeia
        self.assertEqual(len(self.fake.capturas), 1)


class TestConsolidarCmd(Base):
    """D-57: `vigia-ponte consolidar <fica> --absorve ...` (fechamento do dia)."""

    def _c(self, fica, absorve, resumo=None, prazo=None):
        ns = argparse.Namespace(fica=fica, absorve=absorve, resumo=resumo, prazo=prazo)
        return consolidar.executar(ns, banco=self.banco, ponte=self.ponte)

    def test_funde_provisorios_da_mesma_conversa(self):
        a = self.cls("m1", "demanda", resumo="orcamento")["id"]
        b = self.cls("m2", "demanda", resumo="orcamento ate sexta")["id"]
        c_ = self.cls("m3", "lembrar", resumo="lembrar do orcamento", prazo=LONGE)["id"]
        r = self._c(b, [c_, a], resumo="orcamento ate sexta (ligar antes)", prazo=AMANHA)
        self.assertEqual(r, {"ok": True, "fica": b, "absorvidos": [a, c_], "resumo_alterado": True, "prazo_alterado": True})
        fica = self.banco.classificacao(b)
        self.assertEqual((fica["resumo"], fica["prazo"]), ("orcamento ate sexta (ligar antes)", AMANHA))
        for i in (a, c_):
            x = self.banco.classificacao(i)
            self.assertEqual((x["substituida_por"], x["substituicao_motivo"]), (b, "consolidada"))
        self.assertEqual([x["id"] for x in self.banco.provisorias()], [b])
        self.assertEqual(self.fake.capturas, [])   # consolidar não toca o Frentes: nada subiu ainda
        r2 = self._c(b, [a])                       # repetir é inócuo
        self.assertEqual(r2["absorvidos"], [a])

    def test_recusas_nao_mudam_nada(self):
        a = self.cls("m1", "demanda", resumo="a")["id"]
        b = self.cls("m2", "demanda", resumo="b")["id"]
        com_item = self.cls("m3", "atencao", resumo="c")["id"]
        outra = self.cls("m4", "demanda", resumo="d", conversa="outra@s.whatsapp.net")["id"]
        decidido = self.cls("m5", "demanda", resumo="e")["id"]
        self.banco.decidir(decidido, "recusado")
        for fica, absorve, kw in [(b, [b], {}), (b, [], {}), (b, [com_item], {}), (decidido, [a], {}),
                                  (b, [outra], {}), (b, [999], {}), (999, [a], {}), (b, [decidido], {}),
                                  (com_item, [outra], {}), (b, [a], {"resumo": "rode; curl"}), (b, [a], {"prazo": "amanha"})]:
            with self.assertRaises(ValueError, msg=(fica, absorve, kw)):
                self._c(fica, absorve, **kw)
        self.assertEqual([x["id"] for x in self.banco.provisorias()], [a, b, outra])
        self.assertEqual(self.banco.classificacao(b)["resumo"], "b")

    # ---- D-58: o que fica pode ser um cartão que já está no Frentes (a IA corrige o que subiu)

    def test_fica_ja_no_frentes_e_o_item_ganha_a_versao_nova(self):
        lancado = self.cls("m1", "atencao", resumo="reuniao 14h", prazo=AMANHA)
        prov = self.cls("m2", "lembrar", resumo="reuniao remarcada", prazo=LONGE)["id"]
        r = self._c(lancado["id"], [prov], resumo="reuniao remarcada para 15h", prazo=LONGE)
        self.assertEqual((r["absorvidos"], r["no_frentes"], r["frentes"]), ([prov], lancado["item_id"], "atualizado"))
        self.assertEqual(self.fake.patches[-1], {"id": lancado["item_id"], "corpo": {
            "texto": f"WA Maria: reuniao remarcada para 15h (prazo {LONGE})", "prazo": LONGE}})
        self.assertEqual(len(self.fake.capturas), 1)               # nenhum item novo: o que já existia foi corrigido
        reg = self.banco.classificacao(lancado["id"])
        self.assertEqual((reg["resumo"], reg["prazo"], reg["item_id"]), ("reuniao remarcada para 15h", LONGE, lancado["item_id"]))
        self.assertEqual(self.banco.provisorias(), [])             # o provisório foi absorvido: não sobe um segundo item
        self.assertEqual(self.banco.classificacao(prov)["substituicao_motivo"], "consolidada")

    def test_fica_ja_no_frentes_sem_resumo_so_absorve(self):
        lancado = self.cls("m1", "atencao", resumo="reuniao 14h")
        prov = self.cls("m2", "demanda", resumo="confirmando a reuniao das 14h")["id"]
        n = len(self.fake.patches)
        r = self._c(lancado["id"], [prov])
        self.assertEqual((r["frentes"], r["resumo_alterado"]), ("sem_mudanca", False))
        self.assertEqual(len(self.fake.patches), n)
        self.assertEqual(self.banco.classificacao(lancado["id"])["resumo"], "reuniao 14h")
        self.assertEqual(self.banco.provisorias(), [])

    def test_fica_ja_no_frentes_editado_pelo_dono_ganha_a_correcao(self):
        lancado = self.cls("m1", "atencao", resumo="reuniao 14h")
        self.fake.itens[lancado["item_id"]]["texto"] = "Reunião (levar contrato)"
        prov = self.cls("m2", "demanda", resumo="mudou para 15h")["id"]
        r = self._c(lancado["id"], [prov], resumo="reuniao 15h")
        self.assertEqual(r["frentes"], "corrigido_editado")
        self.assertEqual(self.fake.itens[lancado["item_id"]]["texto"], "Reunião (levar contrato) · atualizado: reuniao 15h")

    def test_fica_ja_no_frentes_com_item_fechado_ou_frentes_fora_nao_grava_nada(self):
        lancado = self.cls("m1", "atencao", resumo="reuniao 14h")
        prov = self.cls("m2", "demanda", resumo="mudou para 15h")["id"]
        recusado = self.cls("m3", "atencao", resumo="outra coisa")
        self.banco.decidir(recusado["id"], "recusado")
        with self.assertRaises(ValueError):                         # recusado pelo dono não recebe nada
            self._c(recusado["id"], [prov], resumo="x")
        self.fake.itens[lancado["item_id"]]["status"] = "feito"
        with self.assertRaises(ValueError):                         # item já fechado: o provisório sobe sozinho
            self._c(lancado["id"], [prov], resumo="reuniao 15h")
        self.fake.itens[lancado["item_id"]]["status"] = "aberto"
        self.fake.parar()
        with self.assertRaises(FrentesIndisponivel):
            self._c(lancado["id"], [prov], resumo="reuniao 15h")
        self.assertEqual([x["id"] for x in self.banco.provisorias()], [prov])
        self.assertEqual(self.banco.classificacao(lancado["id"])["resumo"], "reuniao 14h")


class TestRebaixar(Base):
    """D-58: no fechamento do dia a IA segura o cartão provisório que era só conversa (ele não sobe ao Frentes)."""

    def _r(self, cid, classe="aviso", motivo="o dono ja respondeu na conversa"):
        return rebaixar.executar(argparse.Namespace(id=cid, classe=classe, motivo=motivo), banco=self.banco, ponte=self.ponte)

    def test_segura_o_provisorio_guarda_a_classe_original_e_nao_toca_o_frentes(self):
        a = self.cls("m1", "demanda", resumo="pergunta ja respondida")["id"]
        r = self._r(a)
        self.assertEqual((r["ok"], r["classe"], r["era"]), (True, "aviso", "demanda"))
        reg = self.banco.classificacao(a)
        self.assertEqual((reg["classe"], reg["classe_original"], reg["rebaixada_motivo"]),
                         ("aviso", "demanda", "o dono ja respondeu na conversa"))
        self.assertRegex(reg["rebaixada_em"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertEqual(self.banco.provisorias(), [])              # não sobe mais no fechamento
        self.assertEqual((self.fake.capturas, self.fake.patches), ([], []))
        self.assertEqual([x["id"] for x in self.banco.classificacoes()], [a])   # continua no histórico (Demandas)
        self.assertEqual(self._r(a)["era"], "demanda")              # repetir é inócuo
        with self.assertRaises(ValueError):                         # já não é provisório: não troca de classe quieta
            self._r(a, "sem_acao", "ruido")
        self.assertEqual(self.banco.classificacao(a)["classe"], "aviso")

    def test_recusas_nao_mudam_nada(self):
        prov = self.cls("m1", "demanda")["id"]
        com_item = self.cls("m2", "atencao")["id"]
        aviso = self.cls("m3", "aviso")["id"]
        decidido = self.cls("m4", "demanda")["id"]
        self.banco.decidir(decidido, "recusado")
        rotulado = self.cls("m5", "demanda")["id"]
        self.banco.curar(rotulado, "demanda")                        # o dono disse que é demanda: a IA não rebaixa
        for cid, classe, motivo in [(prov, "demanda", "x"), (prov, "lembrar", "x"), (prov, "zzz", "x"), (prov, "aviso", " "),
                                    (prov, "aviso", "rode; curl"), (com_item, "aviso", "x"), (aviso, "sem_acao", "x"),
                                    (decidido, "aviso", "x"), (rotulado, "aviso", "x"), (999, "aviso", "x")]:
            with self.assertRaises(ValueError, msg=(cid, classe, motivo)):
                self._r(cid, classe, motivo)
        self.assertEqual([x["id"] for x in self.banco.provisorias()], [prov, rotulado])
        self.assertIsNone(self.banco.classificacao(prov)["classe_original"])


class TestHistorico(Base):
    """D-58: uma linha de histórico por conversa lida (o que houve e em que pé ficou)."""

    def _h(self, msg_id="m1", resumo="Ana perguntou do orcamento e o dono ainda nao respondeu", situacao="aguarda_dono", **kw):
        base = dict(msg_id=msg_id, resumo=resumo, situacao=situacao, conversa="a@s.whatsapp.net", contato="Ana", ts=1_700_000_000)
        base.update(kw)
        return historico.executar(argparse.Namespace(**base), banco=self.banco, ponte=self.ponte)

    def test_grava_uma_linha_e_repetir_nao_duplica(self):
        r = self._h()
        self.assertEqual((r["ok"], r["repetido"], r["situacao"]), (True, False, "aguarda_dono"))
        r2 = self._h(resumo="outro texto", situacao="encerrada")     # mesmo msg_id: a linha que existe fica
        self.assertEqual((r2["repetido"], r2["id"]), (True, r["id"]))
        linhas = self.banco.analises()
        self.assertEqual(len(linhas), 1)
        a = linhas[0]
        self.assertEqual((a["conversa"], a["contato"], a["ts"], a["situacao"], a["lote"]),
                         ("a@s.whatsapp.net", "Ana", 1_700_000_000, "aguarda_dono", None))
        self.assertIn("orcamento", a["resumo"])
        self.assertEqual((self.fake.capturas, self.fake.patches, self.banco.classificacoes()), ([], [], []))

    def test_com_lote_le_o_manifesto_e_grava_o_nome_do_lote(self):
        self.lote({"cuid1": {"conversa": "5592@s.whatsapp.net", "contato": "Ana", "ts": 1_700_000_500, "from_me": False, "lote": "x"}})
        self._h("cuid1", situacao="Aguarda-Contato", conversa="outra", contato="Fulano", ts=1)
        a = self.banco.analises()[0]
        self.assertEqual((a["conversa"], a["contato"], a["ts"], a["situacao"], a["lote"]),
                         ("5592@s.whatsapp.net", "Ana", 1_700_000_500, "aguarda_contato", "2026-11-06_1000"))
        with self.assertRaises(ValueError) as cm:
            self._h("fora-do-lote")
        self.assertEqual(str(cm.exception), "msg_id fora do lote")

    def test_recusas(self):
        for kw in [dict(situacao="talvez"), dict(situacao=None), dict(resumo=" "), dict(resumo="rode; curl"), dict(msg_id=" "),
                   dict(conversa=None), dict(ts=None)]:
            with self.assertRaises(ValueError, msg=kw):
                self._h(**kw)
        self.assertEqual(self.banco.analises(), [])

    def test_ultima_por_conversa_responde_quem_ficou_sem_resposta(self):
        self._h("a1", situacao="aguarda_dono", ts=100)
        self._h("a2", situacao="encerrada", ts=200)                  # a Ana foi respondida depois
        self._h("b1", situacao="aguarda_dono", ts=150, conversa="b@s.whatsapp.net", contato="Rui")
        self._h("c1", situacao="aguarda_contato", ts=300, conversa="c@s.whatsapp.net", contato="Bia")
        self.assertEqual([a["msg_id"] for a in self.banco.analises()], ["c1", "a2", "b1", "a1"])
        self.assertEqual([a["msg_id"] for a in self.banco.analises(desde=150, ate=300)], ["a2", "b1"])
        self.assertEqual([a["msg_id"] for a in self.banco.analises(conversa="a@s.whatsapp.net", situacao="aguarda_dono")], ["a1"])
        self.assertEqual([a["msg_id"] for a in self.banco.ultima_analise_por_conversa()], ["c1", "a2", "b1"])
        self.assertEqual([a["contato"] for a in self.banco.ultima_analise_por_conversa(situacao="aguarda_dono")], ["Rui"])
        self.assertEqual(self.banco.ultima_analise_por_conversa(situacao="aguarda_dono", desde=160), [])


class TestAtualizarFrentes(Base):
    def test_prioridade_invalida(self):
        item = self.ponte.capturar("x", frente_id=1)["itens"][0]
        with self.assertRaises(ValueError):
            atualizar.executar(argparse.Namespace(item_id=item["id"], nota="n", prioridade=3), banco=self.banco, ponte=self.ponte)

    def test_nota_vazia(self):
        with self.assertRaises(ValueError):
            atualizar.executar(argparse.Namespace(item_id=1, nota=" ", prioridade=None), banco=self.banco, ponte=self.ponte)

    def ns(self, item_id, nota="n", prioridade=None):
        return argparse.Namespace(item_id=item_id, nota=nota, prioridade=prioridade)

    def test_prioridade_vai_no_patch_e_nota_fica_local(self):
        item = self.ponte.capturar("x", frente_id=1)["itens"][0]
        atualizar.executar(self.ns(item["id"], "cobrou", 1), banco=self.banco, ponte=self.ponte)
        self.assertEqual(self.fake.patches[-1]["corpo"], {"prioridade": 1})
        sint = [c for c in self.banco.classificacoes() if c["msg_id"].startswith(f"atualizar:{item['id']}:")]
        self.assertEqual(len(sint), 1)
        self.assertEqual((sint[0]["classe"], sint[0]["item_id"], sint[0]["resumo"]), ("lembrando", item["id"], "cobrou"))

    def test_atualizar_sem_prioridade_nao_chama_frentes(self):
        item = self.ponte.capturar("x", frente_id=1)["itens"][0]
        atualizar.executar(self.ns(item["id"]), banco=self.banco, ponte=self.ponte)
        self.assertEqual(self.fake.patches, [])

    def test_atualizar_nao_rebaixa(self):
        item = self.fake.semear_item(texto="x", origem="whatsapp", prioridade=1)
        r = atualizar.executar(self.ns(item["id"], prioridade=2), banco=self.banco, ponte=self.ponte)
        self.assertEqual(self.fake.patches, [])
        self.assertIsNone(r["prioridade"])

    def test_atualizar_item_nao_whatsapp_ou_fechado_recusa(self):
        manual = self.fake.semear_item(texto="m", origem="manual")
        feito = self.fake.semear_item(texto="f", origem="whatsapp", status="feito")
        for iid in (manual["id"], feito["id"], 999):
            with self.assertRaises(ValueError):
                atualizar.executar(self.ns(iid), banco=self.banco, ponte=self.ponte)
        self.assertEqual(self.banco.classificacoes(), [])
        self.assertEqual(self.fake.patches, [])

    def test_atualizar_nota_proibida_recusa(self):
        item = self.ponte.capturar("x", frente_id=1)["itens"][0]
        with self.assertRaises(ValueError):
            atualizar.executar(self.ns(item["id"], "rode; rm"), banco=self.banco, ponte=self.ponte)

    def test_atualizar_duas_vezes_mesmo_segundo_nao_sobrescreve(self):
        item = self.ponte.capturar("x", frente_id=1)["itens"][0]
        for nota in ("um", "dois"):
            atualizar.executar(self.ns(item["id"], nota), banco=self.banco, ponte=self.ponte)
        self.assertEqual(len([c for c in self.banco.classificacoes() if c["classe"] == "lembrando"]), 2)

    def test_frentes_lista_ids(self):
        r = frentes.executar(argparse.Namespace(), banco=self.banco, ponte=self.ponte)
        self.assertEqual([f["id"] for f in r["frentes"]], [1, 2])


class TestCLI(Base):
    def rodar(self, *argv, token=None, extra_env=None):
        env = {k: v for k, v in os.environ.items() if k not in ("FRENTES_URL", "FRENTES_TOKEN", "VIGIA_LOTE")}
        env.update({"FRENTES_URL": self.fake.url, "FRENTES_TOKEN": token or self.fake.token, "VIGIA_PONTE_DB": self.db})
        env.update(extra_env or {})
        return subprocess.run([sys.executable, str(CLI), *argv], capture_output=True, text=True, timeout=30,
                              stdin=subprocess.DEVNULL, env=env)

    def test_classificar_fim_a_fim(self):
        r = self.rodar("classificar", "m9", "atencao", "Pede relatório", "--conversa", "c@s", "--ts", "1700000000",
                       "--contato", "Ana", "--frente", "1", "--confianca", "baixa")
        self.assertEqual(r.returncode, 0, r.stderr)
        j = json.loads(r.stdout)
        self.assertTrue(j["ok"])
        self.assertEqual(self.fake.patches, [])            # D-58: confiança baixa não gera "a confirmar"
        self.assertEqual(self.fake.itens[j["item_id"]]["confirmado"], 1)
        self.assertEqual(len(r.stdout.strip().splitlines()), 1)

    def test_cli_demanda_fica_provisoria(self):   # D-57
        r = self.rodar("classificar", "m9", "demanda", "Pede relatório", "--conversa", "c@s", "--ts", "1700000000",
                       "--contato", "Ana", "--frente", "1", "--confianca", "alta")
        self.assertEqual(r.returncode, 0, r.stderr)
        j = json.loads(r.stdout)
        self.assertEqual((j["ok"], j["cartao_item"], j["provisorio"], j["item_id"]), (True, True, True, None))
        self.assertEqual(self.fake.capturas, [])

    def test_cli_substitui_e_cancela_fim_a_fim(self):   # D-57
        velho = self.cls("m1", "atencao", resumo="reuniao 14h", conversa="c@s", ts=1_700_000_000)
        r = self.rodar("classificar", "m2", "lembrar", "reuniao 15h", "--conversa", "c@s", "--ts", "1700000100",
                       "--contato", "Maria", "--frente", "1", "--confianca", "alta", "--prazo", LONGE,
                       "--substitui", str(velho["id"]))
        self.assertEqual(r.returncode, 0, r.stderr)
        j = json.loads(r.stdout)
        self.assertEqual((j["substituiu"], j["substituicao"], j["frentes"], j["item_id"]),
                         (velho["id"], "atualizada", "atualizado", velho["item_id"]))
        r = self.rodar("classificar", "m3", "aviso", "cancelou", "--conversa", "c@s", "--ts", "1700000200",
                       "--confianca", "alta", "--cancela", str(j["id"]))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["frentes"], "cancelado")
        self.assertEqual(self.fake.itens[velho["item_id"]]["status"], "cancelado")
        r = self.rodar("classificar", "m4", "aviso", "x", "--conversa", "c@s", "--ts", "1", "--confianca", "alta",
                       "--substitui", "1", "--cancela", "1")
        self.assertEqual(r.returncode, 1)
        self.assertFalse(json.loads(r.stdout)["ok"])

    def test_cli_consolidar_fim_a_fim(self):   # D-57
        a = self.cls("m1", "demanda", resumo="a")["id"]
        b = self.cls("m2", "demanda", resumo="b")["id"]
        r = self.rodar("consolidar", str(b), "--absorve", str(a), "--resumo", "a e b")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["absorvidos"], [a])
        self.assertEqual(self.banco.classificacao(a)["substituida_por"], b)
        self.assertEqual(self.banco.classificacao(b)["resumo"], "a e b")
        r = self.rodar("consolidar", str(b), "--absorve", str(b))
        self.assertEqual(r.returncode, 1)
        self.assertFalse(json.loads(r.stdout)["ok"])

    def test_cli_rebaixar_e_historico_fim_a_fim(self):   # D-58
        a = self.cls("m1", "demanda", resumo="a")["id"]
        r = self.rodar("rebaixar", str(a), "sem_acao", "so conversa")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["era"], "demanda")
        self.assertEqual(self.banco.classificacao(a)["classe"], "sem_acao")
        self.assertEqual(self.rodar("rebaixar", str(a), "demanda", "x").returncode, 1)
        raiz = self.lote({"cuid9": {"conversa": "c@s", "contato": "Ana", "ts": 1_700_000_000, "from_me": False, "lote": "x"}})
        r = self.rodar("historico", "cuid9", "Ana avisou que remarcou e o dono confirmou", "--situacao", "encerrada",
                       extra_env={"VIGIA_LOTE": str(raiz)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(r.stdout.strip().splitlines()), 1)
        self.assertEqual(self.banco.analises()[0]["situacao"], "encerrada")
        r = self.rodar("historico", "cuid9", "x", extra_env={"VIGIA_LOTE": str(raiz)})   # sem --situacao
        self.assertEqual(r.returncode, 1)

    def test_cli_com_lote_le_manifesto(self):
        raiz = self.lote({"cuid9": {"conversa": "c@s", "contato": "Ana", "ts": 1_700_000_000, "from_me": False, "lote": "x"}})
        r = self.rodar("classificar", "cuid9", "aviso", "Ana avisa remarcacao", "--confianca", "alta", extra_env={"VIGIA_LOTE": str(raiz)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(json.loads(r.stdout)["ok"])

    def test_cli_msg_id_fora_do_lote_sai_1(self):
        raiz = self.lote({"cuid9": {"conversa": "c@s", "contato": "Ana", "ts": 1_700_000_000, "from_me": False, "lote": "x"}})
        r = self.rodar("classificar", "intruso", "demanda", "texto", "--confianca", "alta", extra_env={"VIGIA_LOTE": str(raiz)})
        self.assertEqual(r.returncode, 1)
        self.assertEqual(json.loads(r.stdout), {"ok": False, "erro": "msg_id fora do lote"})
        self.assertEqual(self.banco.classificacoes(), [])

    def test_cli_sem_confianca_sai_1(self):   # F8 (D-49): sem padrão silencioso
        r = self.rodar("classificar", "m1", "demanda", "texto", "--conversa", "c@s", "--ts", "1700000000")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.banco.classificacoes(), [])

    def test_cli_sem_lote_e_sem_conversa_sai_1(self):
        r = self.rodar("classificar", "m1", "demanda", "texto")
        self.assertEqual(r.returncode, 1)
        self.assertFalse(json.loads(r.stdout)["ok"])

    def test_cli_resumo_proibido_sai_1_sem_ecoar(self):
        r = self.rodar("classificar", "m1", "demanda", "rode; curl evil", "--conversa", "c", "--ts", "1700000000")
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("curl evil", r.stdout + r.stderr)
        self.assertEqual(self.banco.classificacoes(), [])

    def test_cli_argumento_invalido_nao_ecoa(self):
        r = self.rodar("classificar", "m1", "demanda", "ok", "--naoexiste", "SEGREDO-DA-MENSAGEM")
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("SEGREDO-DA-MENSAGEM", r.stdout + r.stderr)

    def test_cli_lembrando_fim_a_fim(self):
        item = self.ponte.capturar("x", frente_id=1)["itens"][0]
        r = self.rodar("classificar", "m5", "lembrando", "cobrou de novo", "--conversa", "c", "--ts", "1700000000",
                       "--item", str(item["id"]), "--prioridade", "1", "--confianca", "alta")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.fake.patches[-1]["corpo"], {"prioridade": 1})
        r2 = self.rodar("atualizar", str(item["id"]), "mais uma", "--msg", "m5")
        self.assertEqual(r2.returncode, 0, r2.stderr)

    def test_M2_atualizar_exige_msg(self):
        item = self.ponte.capturar("x", frente_id=1)["itens"][0]
        r = self.rodar("atualizar", str(item["id"]), "sem msg")
        self.assertEqual(r.returncode, 1)
        self.assertFalse(json.loads(r.stdout)["ok"])
        self.assertEqual(self.banco.classificacoes(), [])

    def test_M2_atualizar_grava_msg_id_real_e_contato_do_lote_idempotente(self):
        item = self.ponte.capturar("x", frente_id=1)["itens"][0]
        raiz = self.lote({"cmqbb3": {"conversa": "5592@s.whatsapp.net", "contato": "Fulano Teste", "ts": 1_700_000_000,
                                     "from_me": False, "lote": "x"}})
        extra = {"VIGIA_LOTE": str(raiz)}
        for _ in range(2):                                   # reprocessar a mesma mensagem
            r = self.rodar("atualizar", str(item["id"]), "cobrou a proposta", "--msg", "cmqbb3", extra_env=extra)
            self.assertEqual(r.returncode, 0, r.stderr)
        linhas = [c for c in self.banco.classificacoes() if c["classe"] == "lembrando"]
        self.assertEqual(len(linhas), 1)
        self.assertEqual((linhas[0]["msg_id"], linhas[0]["contato"], linhas[0]["conversa"], linhas[0]["ts"]),
                         ("cmqbb3", "Fulano Teste", "5592@s.whatsapp.net", 1_700_000_000))
        self.assertEqual(json.loads(r.stdout)["repetido"], True)

    def test_M2_atualizar_msg_fora_do_lote_sai_1(self):
        item = self.ponte.capturar("x", frente_id=1)["itens"][0]
        raiz = self.lote({"cmqbb3": {"conversa": "c@s", "contato": "Ana", "ts": 1_700_000_000, "from_me": False, "lote": "x"}})
        r = self.rodar("atualizar", str(item["id"]), "nota", "--msg", "inventado", extra_env={"VIGIA_LOTE": str(raiz)})
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.banco.classificacoes(), [])

    def test_B3_frentes_url_default_e_8110(self):
        self.assertIn('"http://127.0.0.1:8110"', CLI.read_text(encoding="utf-8"))

    def test_B4_vigia_dados_define_o_banco(self):
        item = self.ponte.capturar("x", frente_id=1)["itens"][0]
        pasta = Path(self.tmp.name) / "outros-dados"
        env = {k: v for k, v in os.environ.items() if k not in ("VIGIA_PONTE_DB", "VIGIA_LOTE")}
        env.update({"FRENTES_URL": self.fake.url, "FRENTES_TOKEN": self.fake.token, "VIGIA_DADOS": str(pasta)})
        r = subprocess.run([sys.executable, str(CLI), "atualizar", str(item["id"]), "n", "--msg", "mx"],
                           capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((pasta / "vigia.db").exists())

    def test_B5_env_corta_comentario_no_fim_da_linha_e_aspas(self):
        import importlib.machinery, importlib.util
        loader = importlib.machinery.SourceFileLoader("vigia_ponte_cli", str(CLI))
        mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("vigia_ponte_cli", loader))
        loader.exec_module(mod)
        raiz = Path(self.tmp.name) / "raiz"
        raiz.mkdir()
        (raiz / ".env").write_text('FRENTES_URL=http://127.0.0.1:8110   # painel do Frentes\n'
                                   'FRENTES_TOKEN="abc123"  # token\n', encoding="utf-8")
        antes = {k: os.environ.pop(k, None) for k in ("FRENTES_URL", "FRENTES_TOKEN")}
        try:
            mod.RAIZ = raiz
            env = mod.carregar_env()
        finally:
            for k, v in antes.items():
                if v is not None:
                    os.environ[k] = v
        self.assertEqual(env, {"FRENTES_URL": "http://127.0.0.1:8110", "FRENTES_TOKEN": "abc123"})

    def test_erro_de_frentes_sai_1_sem_token(self):
        r = self.rodar("frentes", token="errado-segredo-123")
        self.assertEqual(r.returncode, 1)
        j = json.loads(r.stdout)
        self.assertIn("erro", j)
        self.assertFalse(j["ok"])
        self.assertNotIn("errado-segredo-123", r.stdout + r.stderr)

    def test_classe_invalida_sai_1(self):
        r = self.rodar("classificar", "m1", "xis", "texto secreto")
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("texto secreto", r.stdout)

    def test_variavel_de_envio_ignorada(self):
        nome = "VIGIA_TOKEN_" + "ENVIO"
        r = self.rodar("frentes", extra_env={nome: "segredo-envio-xyz"})
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("segredo-envio-xyz", r.stdout + r.stderr)

    def test_help(self):
        r = self.rodar("--help")
        self.assertEqual(r.returncode, 0)
        for c in ("classificar", "atualizar", "frentes"):
            self.assertIn(c, r.stdout)


if __name__ == "__main__":
    unittest.main()
