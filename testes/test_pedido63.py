"""Pedido 63 (falhas caladas), lote 1: causa no log (T2). A sessão caída (T1) é testada em test_server.py.
Pedido 64: anexo na mesma fila do texto, campo esvaziado na hora do envio.
"""
import tempfile
import unittest
from pathlib import Path

from app import alertas, erros, problemas
from app.erros import EvolutionIndisponivel, VigiaErro, causa
from app.paginas import conversas


class TestCausa(unittest.TestCase):
    def test_erro_do_greg_leva_a_mensagem_em_uma_linha(self):
        e = EvolutionIndisponivel("Evolution respondeu HTTP 503\n em POST /message/sendText")
        self.assertEqual(causa(e), "EvolutionIndisponivel: Evolution respondeu HTTP 503 em POST /message/sendText")
        self.assertEqual(causa(VigiaErro()), "VigiaErro")
        self.assertLessEqual(len(causa(VigiaErro("x" * 900))), len("VigiaErro: ") + 200)

    def test_erro_de_terceiros_leva_arquivo_e_linha_e_nunca_a_mensagem(self):
        def falha():
            return {}["segredo da conversa"]
        try:
            falha()
        except KeyError as e:
            txt = causa(e)
        self.assertTrue(txt.startswith("KeyError em testes/test_pedido63.py:"), txt)
        self.assertNotIn("segredo", txt)

    def test_sem_traceback_fica_so_o_nome(self):
        self.assertEqual(causa(ValueError("valor do usuário")), "ValueError")
        self.assertTrue(erros._RAIZ.endswith("/"))


class TestEnvioDeAnexo(unittest.TestCase):
    def test_anexo_entra_na_fila_do_texto_e_o_campo_esvazia_na_hora(self):
        js = conversas._JS
        self.assertIn("function enviarAnexo(arq, tipo, legenda)", js)
        # o campo e a faixa do anexo esvaziam assim que entra na fila, não quando o servidor responde
        self.assertIn("if (!enviarAnexo(lote[0], tipoDe(lote[0]), t)) { return; }", js)
        self.assertIn("pendurar(null); txt.value = ''; crescer(); txt.focus();", js)
        self.assertNotIn("enviarMidia(arq, tipoDe(arq), t,", js)
        # a mesma bomba manda texto e anexo, um de cada vez, na ordem
        self.assertIn("(it.arq ? pedirJson('/conversas/enviar-midia?' + it.q", js)
        # falhou: o anexo volta para a faixa e a legenda para o campo
        self.assertIn("repender = function (a) { var antes = pendentes.length; anexar([a]); return pendentes.length > antes; };", js)
        self.assertIn("zap-previa-foto", js)
        # a foto não pisca: imagem carregada é reaproveitada e a miniatura fica até a foto do servidor carregar
        self.assertIn("manterImagens(o, n); msgs.replaceChild(n, o);", js)
        self.assertIn("function herdarMiniatura(prov, real)", js)
        self.assertIn("data-src-real", js)

    def test_varios_anexos_com_miniaturas_e_conversa_inteira_recebe_o_arquivo(self):
        # pedido 71: até 10 anexos por vez, miniaturas que navegam, soltar em qualquer ponto da conversa
        js, css = conversas._JS, conversas._CSS
        self.assertIn("LIMITE_ANEXOS = 10", js)
        self.assertIn("entrada.multiple = true;", js)
        self.assertIn("function anexar(lista)", js)
        self.assertIn("ficou') + ' de fora.'", js)
        # a legenda e a citação vão só com o primeiro; os demais seguem sem legenda, na ordem
        self.assertIn("for (var k = 1; k < lote.length; k++) { enviarAnexo(lote[k], tipoDe(lote[k]), ''); }", js)
        # a área de soltar é a conversa inteira, não só o rodapé
        self.assertIn("solte.className = 'zap-solte'", js)
        self.assertIn(".zap-solte {", css)
        self.assertNotIn(".zap-rodape-envio.soltar", css)
        self.assertIn(".zap-mini", css)
        # miniatura em data:, nunca blob: (a CSP do painel só aceita img-src 'self' data:)
        self.assertNotIn("createObjectURL", js)
        # a voz continua no caminho próprio (o áudio só é montado depois de gravado)
        self.assertIn("enviarMidia(r.wav, 'voz'", js)


class TestCatalogo(unittest.TestCase):
    def test_todo_problema_tem_texto_completo_e_peca_conhecida(self):
        self.assertGreaterEqual(len(problemas.CATALOGO), 15)
        for codigo, p in problemas.CATALOGO.items():
            self.assertEqual(codigo, p.codigo)
            self.assertRegex(codigo, r"^[a-z]+(-[a-z]+)*$")
            self.assertIn(p.peca, problemas.PECAS, codigo)
            self.assertTrue(p.titulo and p.por_que and p.passos, codigo)
            self.assertTrue(p.por_que.endswith("."), codigo)
            self.assertTrue(p.link == "" or p.link.startswith("/"), codigo)
            self.assertLessEqual(len(p.titulo), 60, codigo)       # cabe no título da notificação e na faixa

    def test_comandos_citados_existem_no_repositorio(self):
        raiz = Path(problemas.__file__).parent.parent
        for p in problemas.CATALOGO.values():
            for passo in p.passos:
                for palavra in passo.split():
                    if palavra.startswith("bin/"):
                        self.assertTrue((raiz / palavra).is_file(), f"{p.codigo}: {palavra}")


class TestCentral(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dados = Path(self.tmp.name) / "dados"

    def test_imediato_aparece_na_primeira_falha_e_fecha_quando_volta(self):
        self.assertEqual(alertas.abertos(self.dados), [])
        with self.assertLogs("vigia.alertas", "WARNING") as registro:
            self.assertTrue(alertas.abrir(self.dados, "ia-login", detalhe="ClaudeSemLogin: sessão\n expirada", agora=1000))
        self.assertIn("aviso aberto ia-login (ClaudeSemLogin: sessão", registro.output[0])   # o mesmo código vai ao log
        self.assertFalse(alertas.abrir(self.dados, "ia-login", agora=2800))
        (a,) = alertas.abertos(self.dados)
        self.assertEqual((a["codigo"], a["desde"], a["vezes"], a["peca"], a["link"]), ("ia-login", 1000, 2, "ia", "/conexao"))
        self.assertIn("claude auth login", " ".join(a["passos"]))
        self.assertEqual(alertas.fechar(self.dados, "ia-login", "ia-erro"), ["ia-login"])
        self.assertEqual(alertas.abertos(self.dados), [])
        self.assertEqual(alertas.fechar(self.dados, "ia-login"), [])

    def test_passageiro_so_aparece_na_segunda_falha_seguida(self):
        alertas.abrir(self.dados, "evolution-fora", agora=1000)
        self.assertEqual(alertas.abertos(self.dados), [])
        self.assertEqual(alertas.a_notificar(self.dados, agora=1000), [])
        alertas.abrir(self.dados, "evolution-fora", agora=2800)
        self.assertEqual([a["codigo"] for a in alertas.abertos(self.dados)], ["evolution-fora"])
        # voltou e caiu de novo: a contagem recomeça
        alertas.fechar(self.dados, "evolution-fora")
        alertas.abrir(self.dados, "evolution-fora", agora=5000)
        self.assertEqual(alertas.abertos(self.dados), [])

    def test_notifica_uma_vez_e_repete_depois_de_um_dia(self):
        alertas.abrir(self.dados, "docker-parado", agora=1000)
        self.assertEqual([p.codigo for p in alertas.a_notificar(self.dados, agora=1000)], ["docker-parado"])
        self.assertEqual(alertas.a_notificar(self.dados, agora=1000 + 3600), [])
        alertas.abrir(self.dados, "docker-parado", agora=1000 + 7200)                      # repetir a falha não renotifica
        self.assertEqual(alertas.a_notificar(self.dados, agora=1000 + 7300), [])
        self.assertEqual([p.codigo for p in alertas.a_notificar(self.dados, agora=1000 + alertas.REPETE_S)], ["docker-parado"])

    def test_codigo_fora_do_catalogo_e_arquivo_estragado_nao_derrubam(self):
        with self.assertLogs("vigia.alertas", "ERROR"):
            self.assertFalse(alertas.abrir(self.dados, "inventado"))
        self.dados.mkdir(parents=True, exist_ok=True)
        (self.dados / alertas.ARQUIVO).write_text("{isso não é json", encoding="utf-8")
        self.assertEqual(alertas.abertos(self.dados), [])
        self.assertTrue(alertas.abrir(self.dados, "nao-pareado", agora=10))
        self.assertEqual(len(alertas.abertos(self.dados)), 1)
        self.assertEqual(oct((self.dados / alertas.ARQUIVO).stat().st_mode & 0o777), "0o600")

    def test_disco_que_nao_grava_nao_levanta(self):
        arquivo = Path(self.tmp.name) / "nao-e-pasta"
        arquivo.write_text("x", encoding="utf-8")
        with self.assertLogs("vigia.alertas", "WARNING"):
            self.assertFalse(alertas.abrir(arquivo / "dados", "ia-login"))
        self.assertEqual(alertas.abertos(arquivo / "dados"), [])


if __name__ == "__main__":
    unittest.main()
