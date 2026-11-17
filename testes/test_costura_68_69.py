"""Costura dos pedidos 68 e 69: o assistente de primeira instalação usa a IA escolhida em Ajustes › IA.
Quem chama: unittest. A IA por API é o servidor falso em 127.0.0.1 (testes/fakes_ia.py); nada sai do Mac."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import alertas, inicio, leitor  # noqa: E402
from app.paginas import inicio as pagina_inicio  # noqa: E402
from testes.fakes_ia import ServidorIA, erro, resposta  # noqa: E402
from testes.test_pedido69 import PROPOSTA, IaFalsa, TestSugestao  # noqa: E402


class TestInicioComIaPorApi(TestSugestao):
    """Herda a montagem do TestSugestao (agenda, conversas, descrição) e troca o motor para `api`."""

    def _api(self, roteiro):
        srv = ServidorIA(roteiro).iniciar()
        self.addCleanup(srv.parar)
        leitor.gravar_configuracao(self.dados, {"motor": "api", "provedor": "outro", "endereco": srv.url,
                                                "modelo": "modelo-de-teste"})
        return srv

    def test_sugestao_vai_para_a_ia_escolhida_sem_funcao_e_sem_numero(self):
        srv = self._api([resposta(json.dumps(PROPOSTA, ensure_ascii=False))])
        s = self.rodar()
        self.assertEqual(s["estado"], "pronta", s)
        self.assertEqual(self.ia.chamadas, [])                       # o Claude Code não foi chamado
        self.assertEqual(len(srv.pedidos), 1)
        corpo = srv.pedidos[0]["corpo"]
        self.assertNotIn("tools", corpo)                             # a IA só escreve: nenhuma função declarada
        usuario = corpo["messages"][-1]["content"]
        self.assertIn("<<<arquivo descricao.md>>>", usuario)
        self.assertIn("Atendo a Padaria Central.", usuario)
        self.assertIn("<<<arquivo agenda.md>>>", usuario)
        tudo = json.dumps(corpo, ensure_ascii=False)
        for numero in ("5592900001111", "5592900003333", "5592900004444", "120363000000000001"):
            self.assertNotIn(numero, tudo)
        self.assertNotIn(str(self.dados), tudo)                      # nenhum caminho do Mac no pedido

    def test_sugestao_aceita_resposta_em_bloco_de_codigo(self):
        self._api([resposta("```json\n" + json.dumps(PROPOSTA, ensure_ascii=False) + "\n```")])
        self.assertEqual(self.rodar()["estado"], "pronta")

    def test_ia_por_api_fora_nao_trava_o_caminho(self):
        self._api([erro(401, "invalid_api_key")])
        s = self.rodar()
        self.assertEqual(s["estado"], "falhou", s)
        self.assertEqual(self.ia.chamadas, [])

    def test_testar_usa_a_ia_escolhida(self):
        srv = self._api([resposta(None, [("confirmar", {"ok": True})])])
        self.assertEqual(inicio.testar_ia(self.ctx), {"ok": True})
        self.assertEqual(len(srv.pedidos), 1)
        self.assertEqual(self.ia.chamadas, [])

    def test_testar_que_falha_manda_para_ajustes(self):
        self._api([erro(401, "invalid_api_key")])
        r = inicio.testar_ia(self.ctx)
        self.assertFalse(r["ok"])
        self.assertIn("recusou a chave", r["por_que"])
        self.assertIn("Ajustes", r["passos"][0])

    def test_testar_que_passa_fecha_o_aviso_de_ia_aberto(self):
        """QA de 14/11: quem passava no teste por chave seguia vendo "O Greg parou de ler" até a próxima leitura."""
        alertas.abrir(self.dados, "ia-login")
        self.assertEqual([a["codigo"] for a in alertas.abertos(self.dados)], ["ia-login"])
        self._api([resposta(None, [("confirmar", {"ok": True})])])
        self.assertEqual(inicio.testar_ia(self.ctx), {"ok": True})
        self.assertEqual(alertas.abertos(self.dados), [])

    def test_testar_que_falha_deixa_o_aviso_de_ia_aberto(self):
        alertas.abrir(self.dados, "ia-login")
        self._api([erro(401, "invalid_api_key")])
        self.assertFalse(inicio.testar_ia(self.ctx)["ok"])
        self.assertEqual([a["codigo"] for a in alertas.abertos(self.dados)], ["ia-login"])

    def test_tela_diz_qual_ia_e_onde_trocar(self):
        html, ok = pagina_inicio.area_ia(self.ctx, {})
        self.assertFalse(ok)
        self.assertIn("Claude Code", html)
        self.assertIn('href="/ajustes#ia"', html)
        self._api([resposta("ok")])
        html, _ = pagina_inicio.area_ia(self.ctx, {"ia_teste": {"ok": True}})
        self.assertIn("127.0.0.1", html)
        self.assertNotIn("Claude Code", html)

    def test_sem_configuracao_continua_no_claude_code(self):
        self.ctx.executor_ia = IaFalsa(result='{"ok": 1}')
        self.assertEqual(inicio.testar_ia(self.ctx), {"ok": True})
        self.assertEqual(len(self.ctx.executor_ia.chamadas), 1)


del TestSugestao      # só a classe nova roda neste arquivo
