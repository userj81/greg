"""B9 (ciberseg r01, 15/11/2026): o código não traz nome, pasta nem variável do cofre local de ninguém. Onde mora o
token vem do .env do dono (AVISO_COFRE_ARQUIVO e AVISO_COFRE_CHAVE); sem as duas linhas o caminho pelo cofre fica
desligado e nenhuma pasta a mais é negada à IA.
Quem chama: unittest. O que não faz: não lê o .env real (VIGIA_ENV aponta para um arquivo do teste), não abre rede.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
from app import aviso, lotes, semana  # noqa: E402

ARQ, CHAVE = "AVISO_COFRE_ARQUIVO", "AVISO_COFRE_CHAVE"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pasta = Path(self.tmp.name)
        self.env = self.pasta / "projeto.env"
        limpo = {k: v for k, v in os.environ.items() if k not in (ARQ, CHAVE, "TOKEN_DO_COFRE_DE_TESTE")}
        limpo["VIGIA_ENV"] = str(self.env)                   # arquivo ainda não existe: nenhum cofre configurado
        p = mock.patch.dict(os.environ, limpo, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def configurar(self, arquivo: str, chave: str = "TOKEN_DO_COFRE_DE_TESTE") -> None:
        self.env.write_text(f"DONO_WHATSAPP=5500000000000\n{ARQ}={arquivo}   # comentário\n{CHAVE}=\"{chave}\"\n", encoding="utf-8")

    def negadas(self, cmd: list) -> list:
        i = cmd.index("--disallowedTools")
        fim = next(n for n in range(i + 1, len(cmd)) if cmd[n].startswith("--"))
        return cmd[i + 1:fim]


class TestSemCofre(Base):
    def test_sem_as_duas_linhas_o_cofre_fica_desligado(self):
        self.assertIsNone(aviso.cofre_do_dono())
        self.assertEqual(aviso.leitura_negada_do_cofre(), [])
        with self.assertRaises(KeyError):
            aviso.ler_token_cofre()

    def test_so_uma_das_duas_linhas_nao_liga(self):
        self.env.write_text(f"{ARQ}=~/.cofre-de-teste/.env\n", encoding="utf-8")
        self.assertIsNone(aviso.cofre_do_dono())
        self.env.write_text(f"{CHAVE}=TOKEN_DO_COFRE_DE_TESTE\n", encoding="utf-8")
        self.assertIsNone(aviso.cofre_do_dono())

    def test_aviso_sem_cofre_e_sem_evolution_direta_nao_sai_e_nao_abre_rede(self):
        http = mock.Mock()
        with mock.patch.dict(os.environ, {"DONO_WHATSAPP": "5500000000000", "AVISO_INSTANCIA": "bot-de-teste"}), \
                self.assertLogs("vigia.aviso", level="ERROR") as logs:
            self.assertFalse(aviso.enviar("3 demandas novas", http=http))
        self.assertEqual(http.mock_calls, [])
        self.assertIn("aviso não enviado", "\n".join(logs.output))

    def test_lista_negada_ao_claude_code_sem_pasta_de_cofre(self):
        lote = self.pasta / "lote"
        lote.mkdir()
        esperado_lote = ["Write,Edit,WebFetch,WebSearch,Agent", "Read(**/.env*)", "Read(~/.ssh/**)",
                         "Read(~/.frentes.env)", "Read(~/.vigia-envio.env)"]
        self.assertEqual(self.negadas(lotes.montar_comando(lote, modelo="sonnet", prompt_sistema=RAIZ / "prompts/vigia.md")), esperado_lote)
        entrada = self.pasta / "semana" / "entrada.md"
        entrada.parent.mkdir()
        entrada.write_text("x", encoding="utf-8")
        esperado_semana = ["Write,Edit,Bash,WebFetch,WebSearch,Agent", "Read(//**/.env*)", "Read(**/.env*)",
                           "Read(~/.ssh/**)", "Read(~/.frentes.env)", "Read(~/.vigia-envio.env)"]
        self.assertEqual(self.negadas(semana.montar_comando_relatorio(entrada, modelo="sonnet", prompt=RAIZ / "prompts/semanal.md", claude="claude")), esperado_semana)


class TestComCofre(Base):
    def test_le_o_token_do_arquivo_que_o_env_aponta(self):
        cofre = self.pasta / "cofre" / ".env"
        cofre.parent.mkdir()
        cofre.write_text("OUTRA=1\nTOKEN_DO_COFRE_DE_TESTE='abc123'\n", encoding="utf-8")
        self.configurar(str(cofre))
        self.assertEqual(aviso.cofre_do_dono(), (str(cofre), "TOKEN_DO_COFRE_DE_TESTE"))
        self.assertEqual(aviso.ler_token_cofre(), "abc123")

    def test_token_no_ambiente_vence_o_arquivo(self):
        self.configurar("~/.cofre-de-teste/.env")
        with mock.patch.dict(os.environ, {"TOKEN_DO_COFRE_DE_TESTE": " do-ambiente "}):
            self.assertEqual(aviso.ler_token_cofre(), "do-ambiente")

    def test_arquivo_apontado_sem_a_linha_ou_ausente_levanta(self):
        cofre = self.pasta / "cofre.env"
        self.configurar(str(cofre))
        with self.assertRaises(OSError):
            aviso.ler_token_cofre()
        cofre.write_text("OUTRA=1\n", encoding="utf-8")
        with self.assertRaises(KeyError):
            aviso.ler_token_cofre()

    def test_ambiente_vence_o_env_do_projeto(self):
        self.configurar("~/.cofre-do-arquivo/.env")
        with mock.patch.dict(os.environ, {ARQ: "~/.cofre-do-ambiente/.env", CHAVE: "OUTRA_CHAVE"}):
            self.assertEqual(aviso.cofre_do_dono(), ("~/.cofre-do-ambiente/.env", "OUTRA_CHAVE"))

    def test_pasta_negada_segue_o_que_o_dono_apontou(self):
        for arquivo, regra in (("~/.cofre-de-teste/.env", "Read(~/.cofre-de-teste/**)"),
                               ("/srv/cofre/tokens.env", "Read(//srv/cofre/**)"),
                               ("~/.cofre.env", "Read(~/.cofre.env)"),          # solto na casa: nega só o arquivo
                               ("/cofre.env", "Read(//cofre.env)")):
            self.configurar(arquivo)
            self.assertEqual(aviso.leitura_negada_do_cofre(), [regra], arquivo)

    def test_a_regra_entra_nos_dois_comandos_no_lugar_de_sempre(self):
        self.configurar("~/.cofre-de-teste/.env")
        lote = self.pasta / "lote"
        lote.mkdir()
        self.assertEqual(self.negadas(lotes.montar_comando(lote, modelo="sonnet", prompt_sistema=RAIZ / "prompts/vigia.md"))[2:4],
                         ["Read(~/.ssh/**)", "Read(~/.cofre-de-teste/**)"])
        entrada = self.pasta / "entrada.md"
        entrada.write_text("x", encoding="utf-8")
        self.assertEqual(self.negadas(semana.montar_comando_relatorio(entrada, modelo="sonnet", prompt=RAIZ / "prompts/semanal.md", claude="claude"))[3:5],
                         ["Read(~/.ssh/**)", "Read(~/.cofre-de-teste/**)"])

    def test_valor_fora_do_formato_desliga_e_avisa_no_log(self):
        for arquivo, chave in (("cofre/.env", "T"), ("~/cofre de teste/.env", "T"), ("~/.cofre/../.ssh/x", "T"),
                               ("~/.cofre/**)", "T"), ("~/.cofre/", "T"), ("~/.cofre/.env", "TOKEN COM ESPACO"),
                               ("~/.cofre/.env", "1TOKEN")):
            self.configurar(arquivo, chave)
            with self.assertLogs("vigia.aviso", level="ERROR"):
                self.assertIsNone(aviso.cofre_do_dono(), (arquivo, chave))
            with self.assertLogs("vigia.aviso", level="ERROR"):
                self.assertEqual(aviso.leitura_negada_do_cofre(), [], (arquivo, chave))


class TestNadaDoCofreNoCodigo(unittest.TestCase):
    def test_codigo_que_roda_nao_cita_pasta_nem_variavel_de_cofre(self):
        """A pasta e a variável do cofre do dono moram só no .env dele. O termo é montado aqui para este arquivo não
        virar ocorrência dele mesmo."""
        nome = "tool" + "vault"
        proibidos = ("." + nome, nome.upper() + "_")
        achados = []
        for pasta, pad in (("app", "**/*.py"), ("bin", "*.py"), ("bin", "*.sh"), ("prompts", "*.md"), (".", "config.toml"),
                           (".", ".env.exemplo"), (".", "Dockerfile"), (".", "docker-compose.yml"), ("infra", "*.yml")):
            for f in (RAIZ / pasta).glob(pad):
                if f.name == "exportar-publico.py":     # a lista de trocas da edição pública cita o termo de propósito
                    continue
                texto = f.read_text(encoding="utf-8", errors="replace")
                achados += [f"{f.relative_to(RAIZ)}: {p}" for p in proibidos if p in texto]
        self.assertEqual(achados, [])


if __name__ == "__main__":
    unittest.main()
