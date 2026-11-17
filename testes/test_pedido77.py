"""Pedido 77 (segunda passada da auditoria de ações): agendado que não sai abre aviso e notifica, pedido do Claude
Code aparece na faixa e notifica, e arquivo agendado ganha ferramenta no conector. Nada sai do Mac nos testes."""
import base64
import tempfile
import time
import unittest
import unittest.mock as mock
from pathlib import Path

from app import agendados, alertas, integracao, saude
from app import mcp_ferramentas as mf
from app.erros import EnvioIncerto
from app.paginas import _comum as c
from testes.test_agendados import BaseAg
from testes.test_conversas_zap import ANA
from testes.test_mcp import BaseMcp, chamar

RAIZ = Path(__file__).resolve().parents[1]


class _Notas:
    """Troca a notificação do Mac por uma lista."""

    def espiar(self):
        self.notas = []
        antigo = saude.NOTIFICAR
        saude.NOTIFICAR = lambda titulo, texto: self.notas.append((titulo, texto))
        self.addCleanup(setattr, saude, "NOTIFICAR", antigo)


class TestAgendadoAvisa(BaseAg, _Notas):
    def setUp(self):
        super().setUp()
        self.espiar()

    def codigos(self):
        return [a["codigo"] for a in alertas.abertos(self.dados)]

    def test_vencido_abre_aviso_notifica_e_fecha_ao_ver_envios(self):
        self.agenda.criar(ANA, self.em(600), texto="segredo do agendado")
        self.agora[0] += 600 + agendados.TOLERANCIA_S + 1
        self.ag.passo()
        self.assertEqual(self.codigos(), ["agendado-nao-saiu"])
        self.assertEqual([t for t, _ in self.notas], ["Greg: Um envio agendado não saiu"])
        self.assertNotIn("segredo", (self.dados / "alertas.json").read_text(encoding="utf-8"))
        self.assertIn("Um envio agendado não saiu", c.faixa_avisos(self.ctx))
        from app.paginas import envios
        self.assertIn("Não saiu", envios.html_pagina(self.ctx, envios.fila_de(self.ctx)))      # a tela Envios, vista
        self.assertEqual(self.codigos(), [])

    def test_incerto_abre_o_aviso_proprio(self):
        self.agenda.criar(ANA, self.em(600), texto="oi")
        self.agora[0] += 601

        def incerto(*a, **k):
            raise EnvioIncerto("sem confirmação")
        self.ctx.evo.enviar_texto = incerto
        self.ag.passo()
        self.assertEqual(self.codigos(), ["agendado-incerto"])
        self.assertEqual(len(self.notas), 1)

    def test_enviado_nao_avisa(self):
        self.agenda.criar(ANA, self.em(600), texto="oi")
        self.agora[0] += 601
        self.ag.passo()
        self.assertEqual(self.agenda.listar()[0]["estado"], "enviado")
        self.assertEqual((self.codigos(), self.notas), ([], []))


class TestPedidoDoClaudeAvisa(BaseMcp, _Notas):
    def test_pedido_notifica_aparece_na_faixa_e_some_ao_decidir(self):
        self.espiar()
        self.ligar("enviar")
        self.abrir_trava()
        self.assertEqual(c.faixa_avisos(self.ctx), "")
        erro, dados, _ = chamar(self.ctx, "greg_apagar_para_todos", conversa=ANA, id="a1")
        self.assertFalse(erro, dados)
        self.assertEqual([t for t, _ in self.notas], ["Greg: o Claude Code pediu o seu aval"])
        faixa = c.faixa_avisos(self.ctx)
        self.assertIn("O Claude Code pediu o seu aval", faixa)
        self.assertIn('href="/ajustes#claude"', faixa)
        integracao.pedidos(self.ctx).decidir(dados["aguardando_aval"], False)
        self.assertEqual(c.faixa_avisos(self.ctx), "")

    def test_pedido_junto_de_um_aviso_aberto(self):
        self.espiar()
        self.ligar("enviar")
        self.abrir_trava()
        alertas.abrir(self.dados, "agendado-nao-saiu")
        chamar(self.ctx, "greg_apagar_para_todos", conversa=ANA, id="a1")
        faixa = c.faixa_avisos(self.ctx)
        self.assertIn("Um envio agendado não saiu", faixa)
        self.assertIn("1 pedido do Claude Code esperando", faixa)


class TestAgendarArquivo(BaseMcp):
    def setUp(self):
        super().setUp()
        self.quando = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + 86400))
        self.ligar("enviar")
        self.abrir_trava()

    def test_agenda_arquivo_e_nada_sai_na_hora(self):
        erro, dados, _ = chamar(self.ctx, "greg_agendar_arquivo", conversa=ANA, nome="proposta.pdf", legenda="segue",
                                conteudo_base64=base64.b64encode(b"%PDF-1.4 proposta").decode(), quando=self.quando)
        self.assertFalse(erro, dados)
        self.assertTrue(dados["ok"])
        itens = agendados.da(self.ctx).listar()
        self.assertEqual([(i["id"], i["tipo"], i["nome"], i["estado"]) for i in itens],
                         [(dados["id"], "arquivo", "proposta.pdf", "agendado")])
        self.assertEqual(self.envios_db(), [])

    def test_voz_nao_se_agenda_e_falta_de_arquivo_e_recusada(self):
        erro, dados, _ = chamar(self.ctx, "greg_agendar_arquivo", conversa=ANA, conteudo_base64="aGk=", como="voz",
                                quando=self.quando)
        self.assertTrue(erro)
        erro, dados, _ = chamar(self.ctx, "greg_agendar_arquivo", conversa=ANA, quando=self.quando)
        self.assertTrue(erro)
        self.assertIn("Informe caminho ou conteudo_base64", dados)
        self.assertEqual(agendados.da(self.ctx).listar(), [])

    def test_conta_no_teto_e_esta_na_cobertura(self):
        self.assertIn("greg_agendar_arquivo", mf.DIRETAS)
        self.assertEqual(mf.COBERTURA["/conversas/agendar-midia"], "greg_agendar_arquivo")


class TestNotificacaoPeloApp(unittest.TestCase):
    """Pedido 78: a notificação sai pelo Greg.app (nome e ícone do Greg); o osascript fica de reserva."""

    def _app(self, pasta: Path) -> Path:
        exe = pasta / "Greg.app" / "Contents" / "MacOS" / "Vigia"
        exe.parent.mkdir(parents=True)
        exe.write_text("")
        return exe

    def rodar(self, codigo, app=True):
        with tempfile.TemporaryDirectory() as d:
            exe = self._app(Path(d)) if app else None
            feito = mock.Mock(returncode=codigo)
            with mock.patch.object(saude, "APPS_DO_GREG", (str(Path(d) / "Greg.app"),)), \
                    mock.patch.dict("os.environ", {"VIGIA_SEM_NOTIFICACAO": "0"}), \
                    mock.patch.object(saude.shutil, "which", lambda n: "/usr/bin/osascript"), \
                    mock.patch.object(saude.subprocess, "run", return_value=feito) as run:
                saude._notificar_mac("Greg: aviso", "texto fixo")
            return exe, [c.args[0] for c in run.call_args_list]

    def test_app_emite_e_o_osascript_nao_roda(self):
        exe, chamadas = self.rodar(0)
        self.assertEqual(chamadas, [[str(exe), "--notificar", "Greg: aviso", "texto fixo"]])

    def test_app_recusa_e_a_reserva_assume(self):
        for codigo in (2, 3, 4):
            _, chamadas = self.rodar(codigo)
            self.assertEqual([c[0].rsplit("/", 1)[-1] for c in chamadas], ["Vigia", "osascript"], codigo)

    def test_sem_app_vai_direto_para_a_reserva(self):
        _, chamadas = self.rodar(0, app=False)
        self.assertEqual([c[0] for c in chamadas], ["osascript"])

    def test_o_app_tem_o_modo_de_notificar_e_o_framework(self):
        swift = (RAIZ / "macapp" / "Vigia" / "main.swift").read_text(encoding="utf-8")
        self.assertIn('firstIndex(of: "--notificar")', swift)
        self.assertLess(swift.index('firstIndex(of: "--notificar")'), swift.index("let app = NSApplication.shared"))
        self.assertIn("Avisos.pedirPermissao()", swift)
        self.assertIn("-framework UserNotifications", (RAIZ / "bin" / "construir-app.sh").read_text(encoding="utf-8"))


del BaseAg, BaseMcp
