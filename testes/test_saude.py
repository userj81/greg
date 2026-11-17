"""Pedido 63, lote 2: os detectores ligados à central de avisos (app/saude.py), o vigia de leitura parada, a
notificação e a tela Saúde do Greg."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app import alertas, lotes, saude
from app.erros import EvolutionIndisponivel, VigiaErro
from app.paginas import avisos, _comum as c

RAIZ = Path(__file__).resolve().parent.parent


class Base(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        self.notas = []
        antigo = saude.NOTIFICAR
        saude.NOTIFICAR = lambda titulo, texto: self.notas.append((titulo, texto))
        self.addCleanup(setattr, saude, "NOTIFICAR", antigo)

    def codigos(self):
        return [a["codigo"] for a in alertas.abertos(self.d)]


class TestColetor(Base):
    def test_cada_erro_abre_o_aviso_certo(self):
        casos = [(lotes.ClaudeSemLogin("login"), "ia-login"),
                 (lotes.ErroClaude("claude não executou (FileNotFoundError)"), "ia-ausente"),
                 (lotes.ErroClaude("claude respondeu com erro (subtype=success): Credit balance is too low"), "ia-limite"),
                 (lotes.ErroClaude("claude excedeu 600 s"), "ia-erro"),
                 (lotes.PostgresIndisponivel("fora"), "banco-fora"),
                 (lotes.NaoPareado("sem pareado_em"), "nao-pareado"),
                 (EvolutionIndisponivel("fora"), "evolution-fora")]
        for erro, cod in casos:
            self.assertEqual(saude.codigo_do_erro(erro), cod, erro)
        self.assertIsNone(saude.codigo_do_erro(VigiaErro("outra coisa")))
        self.assertIsNone(saude.codigo_do_erro(ValueError("x")))

    def test_login_avisa_na_primeira_e_fecha_quando_o_claude_responde(self):
        saude.do_coletor(self.d, lotes.ClaudeSemLogin("login"))
        self.assertEqual(self.codigos(), ["ia-login"])
        saude.do_coletor(self.d, None, {"status": "sem_novidade"})      # ciclo sem lote não prova que o login voltou
        self.assertEqual(self.codigos(), ["ia-login"])
        saude.do_coletor(self.d, None, {"status": "ok"})
        self.assertEqual(self.codigos(), [])

    def test_falha_passageira_so_aparece_na_segunda_e_um_motivo_de_ia_por_vez(self):
        saude.do_coletor(self.d, lotes.PostgresIndisponivel("fora"))
        self.assertEqual(self.codigos(), [])
        saude.do_coletor(self.d, lotes.PostgresIndisponivel("fora"))
        self.assertEqual(self.codigos(), ["banco-fora"])
        saude.do_coletor(self.d, None, {"status": "sem_novidade"})
        self.assertEqual(self.codigos(), [])
        saude.do_coletor(self.d, lotes.ClaudeSemLogin("login"))
        saude.do_coletor(self.d, lotes.ErroClaude("claude respondeu com erro: usage limit reached"))
        self.assertEqual(self.codigos(), ["ia-limite"])

    def test_detalhe_nao_leva_mensagem_de_terceiros(self):
        saude.do_coletor(self.d, lotes.ClaudeSemLogin("login do Claude expirado"))
        bruto = (self.d / "alertas.json").read_text()
        self.assertIn("ClaudeSemLogin", bruto)

    def test_transcricao_so_abre_quando_nenhum_audio_do_ciclo_saiu(self):
        t = {"audios": 3, "transcritos": 0, "falhas": 3}
        saude.do_coletor(self.d, None, {"status": "ok", "transcricao": t})
        saude.do_coletor(self.d, None, {"status": "ok", "transcricao": t})
        self.assertEqual(self.codigos(), ["transcricao-erro"])
        saude.do_coletor(self.d, None, {"status": "ok", "transcricao": {"audios": 2, "transcritos": 2, "falhas": 0}})
        self.assertEqual(self.codigos(), [])

    def test_coletor_chama_a_central_e_respeita_dry_run(self):
        fonte = (RAIZ / "bin/coletor.py").read_text()
        self.assertIn("if args.dry_run or args.sem_claude:\n        return", fonte)
        self.assertEqual(fonte.count("_saude(dados, args, e)"), 3)   # pedido 85: o erro sem código também registra
        self.assertIn("_saude(dados, args, None, r)", fonte)


class TestMonitorEFechamento(Base):
    def test_eventos_do_monitor(self):
        saude.do_monitor(self.d, {"evento": "queda", "motivo": "caiu"})
        self.assertEqual(self.codigos(), ["whatsapp-caiu"])              # só a pessoa resolve: avisa na primeira
        saude.do_monitor(self.d, {"evento": "socket_zumbi", "motivo": "sonda_falhou_reiniciada"})
        self.assertEqual(self.codigos(), [])                               # trocou de motivo; passageiro espera a 2ª
        saude.do_monitor(self.d, {"evento": "socket_zumbi", "motivo": "sonda_falhou_persistente"})
        self.assertEqual(self.codigos(), ["whatsapp-sem-resposta"])
        saude.do_monitor(self.d, {"evento": "ok"})
        self.assertEqual(self.codigos(), [])
        saude.do_monitor(self.d, {"evento": "nao_rodou"})
        saude.do_monitor(self.d, None)
        self.assertEqual(self.codigos(), [])

    def test_fechamento_abre_com_1_fecha_com_0_e_ignora_2(self):
        for _ in range(2):
            saude.do_fechamento(self.d, "dia", 1)
        self.assertEqual(self.codigos(), ["fechamento-dia-erro"])
        saude.do_fechamento(self.d, "dia", 2)
        self.assertEqual(self.codigos(), ["fechamento-dia-erro"])
        saude.do_fechamento(self.d, "dia", 0)
        self.assertEqual(self.codigos(), [])

    def test_cli_do_vigia_sh(self):
        import os
        import unittest.mock as mock
        with mock.patch.dict(os.environ, {"VIGIA_DADOS": str(self.d)}):
            self.assertEqual(saude.main(["abrir", "docker-parado"]), 0)
            self.assertEqual(self.codigos(), ["docker-parado"])
            self.assertEqual(saude.main(["notificar"]), 0)
            self.assertEqual(len(self.notas), 1)
            self.assertEqual(saude.main(["fechar", "docker-parado", "codigo-que-nao-existe"]), 0)
            self.assertEqual(self.codigos(), [])
            self.assertEqual(saude.main(["abrir", "codigo-que-nao-existe"]), 0)
            self.assertEqual(saude.main(["fechamento", "semana", "1"]), 0)
        sh = (RAIZ / "bin/vigia.sh").read_text()
        for trecho in ("saude abrir docker-parado", "saude abrir evolution-fora", 'saude fechamento dia "$rc_fec"',
                       'saude fechamento semana "$rc"', "saude notificar"):
            self.assertIn(trecho, sh)


class TestNotificacao(Base):
    def test_notifica_uma_vez_e_repete_depois_de_um_dia(self):
        alertas.abrir(self.d, "ia-login", agora=1000)
        self.assertEqual(saude.notificar(self.d, agora=1000), 1)
        self.assertEqual(saude.notificar(self.d, agora=2000), 0)
        self.assertEqual(saude.notificar(self.d, agora=1000 + alertas.REPETE_S), 1)
        titulo, texto = self.notas[0]
        self.assertEqual(titulo, "Greg: O Greg parou de ler as conversas")
        self.assertIn("O login do Claude neste Mac expirou.", texto)

    def test_sem_aviso_nao_notifica_e_no_docker_nao_chama_osascript(self):
        import os
        import unittest.mock as mock
        self.assertEqual(saude.notificar(self.d), 0)
        with mock.patch.dict(os.environ, {"VIGIA_CONTAINER": "1"}), mock.patch.object(saude.subprocess, "run") as run:
            saude._notificar_mac("a", "b")
        run.assert_not_called()

    def test_texto_da_notificacao_e_seguro(self):
        import unittest.mock as mock
        with mock.patch.object(saude.shutil, "which", lambda n: "/usr/bin/osascript"), \
                mock.patch.dict("os.environ", {"VIGIA_SEM_NOTIFICACAO": "0"}), \
                mock.patch.object(saude.subprocess, "run") as run:
            saude._notificar_mac('Greg "x"', 'rode: claude auth login" & do shell script "id')
        script = run.call_args[0][0][2]
        self.assertEqual(script.count('"'), 6)
        self.assertNotIn("&", script)


class TestLeituraParada(Base):
    def setUp(self):
        super().setUp()
        (self.d / "cursor.json").write_text(json.dumps({"ultimo_seq": 0, "pareado_em": 1}))
        self.t = 1_800_000_000

    def conferir(self, **k):
        k.setdefault("despertar", lambda: None)
        k.setdefault("agente", lambda: True)
        return saude.conferir_leitura(self.d, agora=self.t, **k)

    def lida_ha(self, minutos):
        (self.d / "ultima-leitura.json").write_text(json.dumps({"em": self.t - minutos * 60, "status": "ok"}))

    def test_em_dia_ate_75_minutos_e_parada_depois(self):
        self.lida_ha(74)
        self.assertEqual(self.conferir(), "em_dia")
        self.assertEqual(self.codigos(), [])
        self.lida_ha(76)
        self.assertEqual(self.conferir(), "leitura-parada")
        self.assertEqual(self.codigos(), ["leitura-parada"])

    def test_mac_que_acabou_de_acordar_ou_painel_que_acabou_de_subir_nao_avisa(self):
        self.lida_ha(600)
        self.assertEqual(self.conferir(despertar=lambda: self.t - 600), "em_dia")
        self.assertEqual(self.conferir(inicio=self.t - 60), "em_dia")
        self.assertEqual(self.conferir(despertar=lambda: self.t - 76 * 60), "leitura-parada")

    def test_pausado_e_sem_pareamento_nao_contam_e_fecham_o_aviso(self):
        self.lida_ha(300)
        self.conferir()
        (self.d / "PAUSADO").write_text("")
        self.assertEqual(self.conferir(), "pausado")
        self.assertEqual(self.codigos(), [])
        (self.d / "PAUSADO").unlink()
        (self.d / "cursor.json").write_text(json.dumps({"ultimo_seq": 0, "pareado_em": None}))
        self.assertEqual(self.conferir(), "sem_pareamento")

    def test_com_outro_aviso_que_explica_nao_abre_leitura_parada(self):
        self.lida_ha(300)
        alertas.abrir(self.d, "ia-login")
        self.assertEqual(self.conferir(), "outro_aviso")
        self.assertEqual(self.codigos(), ["ia-login"])

    def test_agendador_descarregado_tem_aviso_proprio(self):
        self.lida_ha(300)
        self.assertEqual(self.conferir(agente=lambda: False), "agendador-parado")
        self.assertEqual(self.codigos(), ["agendador-parado"])
        self.assertEqual(self.conferir(agente=lambda: None), "leitura-parada")   # sem saber (Docker): aviso genérico
        self.assertEqual(self.codigos(), ["leitura-parada"])

    def test_leitura_boa_fecha_e_o_vigia_notifica(self):
        self.lida_ha(300)
        v = saude.Vigia(self.d, relogio=lambda: self.t)
        v.inicio = 0
        import unittest.mock as mock
        with mock.patch.object(saude, "_despertar", lambda: None), mock.patch.object(saude, "_agente_carregado", lambda: True):
            self.assertEqual(saude.conferir_leitura(self.d, agora=self.t, despertar=lambda: None, agente=lambda: True), "leitura-parada")
            saude.notificar(self.d, agora=self.t)
        self.assertEqual(len(self.notas), 1)
        saude.do_coletor(self.d, None, {"status": "sem_novidade"})
        self.assertEqual(self.codigos(), [])


class TestTelas(Base):
    def setUp(self):
        super().setUp()
        self.ctx = SimpleNamespace(config={"dados_dir": str(self.d)}, banco=None)

    def test_faixa_vazia_sem_aviso_e_com_o_mais_antigo_e_a_contagem(self):
        self.assertEqual(c.faixa_avisos(self.ctx), "")
        self.assertEqual(c.faixa_avisos(None), "")
        alertas.abrir(self.d, "docker-parado", agora=10)
        alertas.abrir(self.d, "ia-login", agora=20)
        f = c.faixa_avisos(self.ctx)
        self.assertIn("O Docker está parado", f)
        self.assertIn("e mais 1 aviso", f)
        self.assertIn('href="/avisos"', f)

    def test_tela_lista_as_pecas_e_o_passo_a_passo_com_comando_copiavel(self):
        h = avisos.html_pagina(self.ctx)
        self.assertIn("Tudo funcionando", h)
        for rotulo in saude.ROTULO_PECA.values():
            self.assertIn(c.esc(rotulo), h)
        alertas.abrir(self.d, "ia-login")
        h = avisos.html_pagina(self.ctx)
        self.assertIn("1 peça precisa de você", h)
        self.assertIn("<code>claude auth login</code>", h)
        self.assertIn('href="/conexao"', h)
        self.assertNotIn("ClaudeSemLogin", h)            # detalhe técnico não vai para a tela

    def test_todo_problema_do_catalogo_tem_peca_com_rotulo(self):
        from app.problemas import CATALOGO, PECAS
        self.assertEqual(set(saude.ROTULO_PECA), set(PECAS))
        for p in CATALOGO.values():
            self.assertIn(p.peca, PECAS)

    def test_servidor_liga_o_vigia(self):
        fonte = (RAIZ / "app/server.py").read_text()
        self.assertIn("ctx.vigia_saude = saude.Vigia(", fonte)
        self.assertIn("ctx.vigia_saude.iniciar()", fonte)
        self.assertIn("saude.do_monitor(dados, r)", (RAIZ / "app/conexao.py").read_text())


if __name__ == "__main__":
    unittest.main()
