"""D-58 (pedido 46): leitura do histórico de análise — app/analises.py e bin/historico.py.
Quem chama: unittest. Banco SQLite temporário; nenhuma rede, nenhum Claude.
O que não faz: não toca o vigia.db real (VIGIA_DADOS aponta para a pasta temporária).
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import analises  # noqa: E402
from app.banco import Banco  # noqa: E402

RAIZ = Path(__file__).resolve().parents[1]
AGORA = datetime(2026, 11, 10, 20, 0).timestamp()
HORA, DIA = 3600, 86400
ANA, RUI, BIA = "a@s.whatsapp.net", "r@s.whatsapp.net", "b@s.whatsapp.net"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dados = Path(self.tmp.name) / "dados"
        self.dados.mkdir()
        self.banco = Banco(str(self.dados / "vigia.db"))

    def analise(self, msg_id, conversa, contato, ha, situacao, resumo=None, agora=AGORA):
        self.banco.gravar_analise(msg_id=msg_id, conversa=conversa, contato=contato, ts=int(agora - ha),
                                  resumo=resumo or f"resumo {msg_id}", situacao=situacao)

    def cartao(self, msg_id, classe="demanda", conversa=ANA, ha=HORA, agora=AGORA, **kw):
        return self.banco.gravar_classificacao(msg_id=msg_id, conversa=conversa, contato="x", ts=int(agora - ha), classe=classe,
                                               resumo=f"cartao {msg_id}", confianca="alta", **kw)

    def semear(self, agora=AGORA):
        self.analise("a1", ANA, "Ana Paula", 3 * DIA, "aguarda_dono", "Ana pediu o orcamento revisado", agora)
        self.analise("a2", ANA, "Ana Paula", 2 * HORA, "encerrada", "Dono mandou o orcamento e ela agradeceu", agora)
        self.analise("r1", RUI, "Rui", 26 * HORA, "aguarda_dono", "Rui perguntou se a reuniao de quinta esta de pe", agora)
        self.analise("b1", BIA, "Bia", HORA, "aguarda_contato", "Dono pediu o contrato e a Bia ficou de mandar", agora)
        self.analise("v1", RUI, "Rui", 20 * DIA, "aguarda_dono", "assunto antigo", agora)   # fora da janela de 7 dias


class TestMontar(Base):
    def test_semana_diz_o_que_houve_e_quem_ficou_sem_resposta(self):
        self.semear()
        h = analises.montar(self.banco, agora=AGORA)
        self.assertEqual(h["periodo"], {"dias": 7, "de": "2026-11-03", "ate": "2026-11-10"})
        self.assertEqual((h["conversas"], [a["contato"] for a in h["analises"]]), (3, ["Bia", "Ana Paula", "Rui", "Ana Paula"]))
        # a ÚLTIMA análise de cada conversa é que vale: a Ana foi respondida depois, o Rui continua esperando
        self.assertEqual([a["contato"] for a in h["aguardando_dono"]], ["Rui"])
        self.assertEqual([a["contato"] for a in h["aguardando_contato"]], ["Bia"])
        self.assertEqual(h["analises"][0], {"quando": "2026-11-10 19:00", "ts": int(AGORA - HORA), "contato": "Bia", "conversa": BIA,
                                            "situacao": "aguarda_contato", "atual": True,
                                            "resumo": "Dono pediu o contrato e a Bia ficou de mandar"})
        # `atual` = a última análise da conversa; a linha antiga da Ana (aguardava resposta) deixou de ser a atual
        self.assertEqual([(a["contato"], a["atual"]) for a in h["analises"]],
                         [("Bia", True), ("Ana Paula", True), ("Rui", True), ("Ana Paula", False)])
        self.assertEqual(([a["contato"] for a in h["encerradas"]], h["cortado"]), (["Ana Paula"], False))

    def test_limite_corta_e_avisa(self):
        self.semear()
        h = analises.montar(self.banco, agora=AGORA, limite=2)
        self.assertEqual((len(h["analises"]), h["cortado"]), (2, True))
        self.assertEqual([a["contato"] for a in h["aguardando_dono"]], ["Rui"])   # quem aguarda não depende do corte
        self.assertFalse(analises.montar(self.banco, agora=AGORA, limite=4)["cortado"])

    def test_janela_filtro_por_contato_e_limites(self):
        self.semear()
        self.assertEqual(len(analises.montar(self.banco, dias=1, agora=AGORA)["analises"]), 2)
        self.assertEqual(len(analises.montar(self.banco, dias=30, agora=AGORA)["analises"]), 5)
        self.assertEqual(analises.montar(self.banco, dias=0, agora=AGORA)["periodo"]["dias"], 1)
        self.assertEqual(analises.montar(self.banco, dias=9999, agora=AGORA)["periodo"]["dias"], 90)
        h = analises.montar(self.banco, agora=AGORA, conversa="ana")
        self.assertEqual((h["conversas"], {a["contato"] for a in h["analises"]}, h["aguardando_dono"]), (1, {"Ana Paula"}, []))
        self.assertEqual(analises.montar(self.banco, agora=AGORA, conversa=RUI.upper())["conversas"], 1)   # o jid inteiro também serve
        self.assertEqual(analises.montar(self.banco, agora=AGORA, conversa="zzz")["analises"], [])

    def test_contagem_dos_cartoes_do_periodo(self):
        self.semear()
        self.cartao("p1")                                             # provisório
        self.cartao("f1", item_id=7)                                  # no Frentes
        seg = self.cartao("s1")                                       # a IA segurou no fechamento
        self.banco.rebaixar(seg, "aviso", "so conversa")
        self.cartao("av", classe="aviso")
        rec = self.cartao("rc", conversa=RUI, item_id=8)
        self.banco.decidir(rec, "recusado")
        velho, novo = self.cartao("su1", conversa=RUI), self.cartao("su2", conversa=RUI, item_id=9)
        self.banco.substituir(velho, novo, "atualizada")              # substituído não conta
        self.cartao("ant", ha=20 * DIA)                               # fora da janela
        c = analises.montar(self.banco, agora=AGORA)["cartoes"]
        self.assertEqual(c, {"total": 6, "no_frentes": 2, "segurados": 1, "provisorios": 1, "recusados": 1, "avisos": 1})
        c = analises.montar(self.banco, agora=AGORA, conversa="Rui")["cartoes"]
        self.assertEqual((c["total"], c["no_frentes"], c["recusados"]), (2, 1, 1))
        c = analises.cartoes_da_conversa(self.banco, RUI, agora=AGORA)                # jid exato, para a página
        self.assertEqual((c["total"], c["no_frentes"], c["recusados"]), (2, 1, 1))
        self.assertEqual(analises.cartoes_da_conversa(self.banco, "Rui", agora=AGORA)["total"], 0)

    def test_vazio(self):
        h = analises.montar(self.banco, agora=AGORA)
        self.assertEqual((h["analises"], h["aguardando_dono"], h["conversas"], h["cartoes"]["total"]), ([], [], 0, 0))
        self.assertIn("Nenhuma análise gravada neste período", analises.texto(h))


class TestTexto(Base):
    def test_texto_tem_pendentes_dia_a_dia_e_cartoes(self):
        self.semear()
        self.cartao("f1", item_id=7)
        t = analises.texto(analises.montar(self.banco, agora=AGORA))
        self.assertIn("Histórico de análise · últimos 7 dia(s) (03/11 (terça) a 10/11 (terça)) · 4 análise(s) em 3 conversa(s)", t)
        self.assertIn("Aguardando você (1)\n- Rui · desde 09/11 (segunda) 18:00 · Rui perguntou se a reuniao de quinta esta de pe", t)
        self.assertIn("Aguardando o contato (1)\n- Bia · desde 10/11 (terça) 19:00 · ", t)
        self.assertIn("Dia a dia\n10/11 (terça)\n  19:00 · Bia · Dono pediu o contrato e a Bia ficou de mandar [aguarda o contato]\n"
                      "  18:00 · Ana Paula · Dono mandou o orcamento e ela agradeceu [encerrada]\n09/11 (segunda)\n", t)
        self.assertIn("  20:00 · Ana Paula · Ana pediu o orcamento revisado [aguarda você]", t)
        self.assertIn("Cartões no período: 1 · 1 no Frentes · 0 segurados pela IA · 0 ainda vão subir · 0 avisos · 0 recusados por você", t)
        self.assertNotIn("assunto antigo", t)

    def test_so_pendentes_tira_o_dia_a_dia_e_ninguem_quando_vazio(self):
        self.analise("a1", ANA, "Ana", HORA, "encerrada")
        t = analises.texto(analises.montar(self.banco, agora=AGORA), so_pendentes=True)
        self.assertIn("Aguardando você (0)\n- ninguém", t)
        self.assertNotIn("Dia a dia", t)
        self.assertNotIn("resumo a1", t)


class TestCLI(Base):
    def _cli(self, *args, dados=None):
        env = dict(os.environ, VIGIA_DADOS=str(dados or self.dados))
        return subprocess.run([sys.executable, str(RAIZ / "bin/historico.py"), *args], capture_output=True, text=True,
                              timeout=60, stdin=subprocess.DEVNULL, env=env)

    def test_texto_json_e_contagem(self):
        import time
        agora = time.time()
        self.semear(agora)
        r = self._cli()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Aguardando você (1)\n- Rui · ", r.stdout)
        self.assertIn("Dia a dia", r.stdout)
        r = self._cli("--pendentes", "--dias", "30")
        self.assertIn("Aguardando você (1)", r.stdout)                 # o assunto antigo do Rui não duplica: vale a última
        self.assertNotIn("Dia a dia", r.stdout)
        r = self._cli("--json", "--conversa", "ana")
        j = json.loads(r.stdout)
        self.assertEqual((len(r.stdout.strip().splitlines()), j["conversas"], len(j["analises"])), (1, 1, 2))
        r = self._cli("--contagem")
        j = json.loads(r.stdout)
        self.assertEqual((j["analises"], j["conversas"], j["aguardando_dono"], j["aguardando_contato"]), (4, 3, 1, 1))
        for nome in ("Ana", "Rui", "Bia", "orcamento"):                # só números: nenhum nome, nenhum resumo
            self.assertNotIn(nome, r.stdout)

    def test_sem_banco_sai_2_e_argumento_invalido_sai_2(self):
        vazio = Path(self.tmp.name) / "vazio"
        vazio.mkdir()
        r = self._cli(dados=vazio)
        self.assertEqual((r.returncode, r.stdout), (2, ""))
        self.assertIn("não rodou", r.stderr)
        self.assertFalse((vazio / "vigia.db").exists())                # leitura não cria banco
        self.assertEqual(self._cli("--json", "--contagem").returncode, 2)

    def test_vigia_sh_historico_chama_o_script(self):
        self.analise("a1", ANA, "Ana", HORA, "aguarda_dono", agora=__import__("time").time())
        r = subprocess.run(["/bin/bash", str(RAIZ / "bin/vigia.sh"), "historico", "--contagem"], capture_output=True, text=True,
                           timeout=60, stdin=subprocess.DEVNULL, env=dict(os.environ, VIGIA_DADOS=str(self.dados)))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["aguardando_dono"], 1)


if __name__ == "__main__":
    unittest.main()
