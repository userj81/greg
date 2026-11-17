"""Fase A (D-45): app/segredos.py — segredos no Keychain pelo `security`, com .env como volta.
Quem chama: unittest. Tudo com um `security` FALSO em memória: a suíte nunca toca no Keychain real
(testes/__init__.py põe VIGIA_KEYCHAIN=off; aqui ligamos só dentro do teste, com o executor falso).
"""
import io
import contextlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import segredos  # noqa: E402


class SecurityFalso:
    """Imita o /usr/bin/security: find-generic-password -w devolve pelo stdout; `-i` lê add/delete pelo stdin."""

    def __init__(self):
        self.itens = {}
        self.chamadas = []

    def __call__(self, cmd, **kw):
        self.chamadas.append((list(cmd), kw.get("input")))
        self.assert_sem_valor_em_argv(cmd)
        if cmd[1] == "find-generic-password":
            conta = cmd[cmd.index("-a") + 1]
            if conta in self.itens:
                return subprocess.CompletedProcess(cmd, 0, self.itens[conta] + "\n", "")
            return subprocess.CompletedProcess(cmd, 44, "", "security: SecKeychainSearchCopyNext: The specified item could not be found.\n")
        if cmd[1] == "delete-generic-password":
            conta = cmd[cmd.index("-a") + 1]
            return subprocess.CompletedProcess(cmd, 0 if self.itens.pop(conta, None) is not None else 44, "", "")
        if cmd[1] == "-i":
            partes = (kw.get("input") or "").split()
            if partes and partes[0] == "add-generic-password":
                self.itens[partes[partes.index("-a") + 1]] = partes[partes.index("-w") + 1]
                return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 2, "", "comando desconhecido")

    def assert_sem_valor_em_argv(self, cmd):
        for v in self.itens.values():
            assert v not in cmd, "valor de segredo em argv"


class Base(unittest.TestCase):
    """Três cercas: (1) o `subprocess.run` do módulo vira o falso; (2) o serviço vira `.teste`; (3) o caminho do
    binário vira um que NÃO EXISTE — se as duas primeiras falharem, o `security` real nem é encontrado (duas rodadas
    de 07/11 alcançaram o Keychain do Mac: uma por `executor=subprocess.run` como padrão, outra por `discover`
    sem `-t .`, que pula testes/__init__.py). Fora destes testes o Keychain é opt-in (VIGIA_KEYCHAIN=on)."""

    def setUp(self):
        self.sec = SecurityFalso()
        amb = mock.patch.dict(os.environ, {"VIGIA_KEYCHAIN": "on", "VIGIA_KEYCHAIN_SERVICO": "io.vigia.vigia.teste"})
        amb.start()
        self.addCleanup(amb.stop)
        p = mock.patch.object(segredos.subprocess, "run", self.sec)
        p.start()
        self.addCleanup(p.stop)
        b = mock.patch.object(segredos, "SECURITY", "/nonexistent/security-so-no-teste")
        b.start()
        self.addCleanup(b.stop)

    def test_keychain_e_opt_in_desligado_por_padrao(self):
        """Decisão do dono (07/11): sem VIGIA_KEYCHAIN=on explícito, nada toca no Keychain — nem suíte sem -t ."""
        amb = dict(os.environ)
        amb.pop("VIGIA_KEYCHAIN", None)
        with mock.patch.dict(os.environ, amb, clear=True):
            self.assertFalse(segredos.ligado())
            self.assertEqual(segredos.ler("VIGIA_TOKEN"), "")
            self.assertFalse(segredos.gravar("VIGIA_TOKEN", "abcdef1234"))
            self.assertEqual(segredos.completar({"VIGIA_TOKEN": "do-env"}), {"VIGIA_TOKEN": "do-env"})
        for valor in ("off", "0", "false", "", "sim-nao", "ON "):
            with mock.patch.dict(os.environ, {"VIGIA_KEYCHAIN": valor}):
                self.assertEqual(segredos.ligado(), valor.strip().lower() in ("on", "1", "sim", "true"), repr(valor))
        self.assertEqual(self.sec.chamadas, [])

    def test_executor_e_resolvido_na_chamada(self):
        """Regressão: `executor=subprocess.run` como padrão congelava a função real e furava o patch."""
        import inspect
        for f in (segredos.ler, segredos.gravar, segredos.apagar, segredos.completar):
            self.assertIsNone(inspect.signature(f).parameters["executor"].default, f.__name__)


class TestLerGravar(Base):
    def test_desligado_nao_chama_security(self):
        with mock.patch.dict(os.environ, {"VIGIA_KEYCHAIN": "off"}):
            self.assertEqual(segredos.ler("VIGIA_TOKEN"), "")
            self.assertFalse(segredos.gravar("VIGIA_TOKEN", "abcdef1234"))
        self.assertEqual(self.sec.chamadas, [])

    def test_gravar_pelo_stdin_e_ler_pelo_stdout(self):
        self.assertTrue(segredos.gravar("VIGIA_TOKEN", "a1b2c3d4e5f6"))  # gitleaks:allow (valor FALSO de teste)
        cmd, entrada = self.sec.chamadas[0]
        self.assertEqual(cmd, [segredos.SECURITY, "-i"])
        self.assertIn("-w a1b2c3d4e5f6", entrada)            # valor só no stdin
        self.assertIn("-s io.vigia.vigia.teste -a VIGIA_TOKEN", entrada)
        self.assertEqual(segredos.ler("VIGIA_TOKEN"), "a1b2c3d4e5f6")
        self.assertEqual(segredos.ler("FRENTES_TOKEN"), "")  # ausente = vazio, sem exceção

    def test_gravar_confere_relendo(self):
        """Se o security disser 0 mas a releitura não bater, gravar devolve False."""
        class Mudo(SecurityFalso):
            def __call__(self, cmd, **kw):
                if cmd[1] == "-i":
                    return subprocess.CompletedProcess(cmd, 0, "", "")
                return super().__call__(cmd, **kw)
        with mock.patch.object(segredos.subprocess, "run", Mudo()):
            self.assertFalse(segredos.gravar("VIGIA_TOKEN", "a1b2c3d4e5f6"))  # gitleaks:allow (valor FALSO de teste)

    def test_recusa_valor_e_nome_fora_do_padrao(self):
        for ruim in ("tem espaco aqui", 'com"aspas"1234', "curto", "a" * 600):
            with self.subTest(ruim=ruim[:12]), self.assertRaises(ValueError):
                segredos.gravar("VIGIA_TOKEN", ruim)
        for nome in ("minusculo", "1COMECA_NUM", "COM-HIFEN", ""):
            with self.subTest(nome=nome), self.assertRaises(ValueError):
                segredos.ler(nome)
        self.assertEqual(self.sec.chamadas, [])

    def test_security_travado_ou_ausente_vira_vazio(self):
        def trava(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 10)
        with mock.patch.object(segredos.subprocess, "run", trava):
            self.assertEqual(segredos.ler("VIGIA_TOKEN"), "")
            self.assertFalse(segredos.gravar("VIGIA_TOKEN", "a1b2c3d4e5f6"))  # gitleaks:allow (valor FALSO de teste)
        with mock.patch.object(segredos.subprocess, "run", mock.Mock(side_effect=FileNotFoundError)):
            self.assertEqual(segredos.ler("VIGIA_TOKEN"), "")

    def test_apagar(self):
        segredos.gravar("VIGIA_TOKEN", "a1b2c3d4e5f6")  # gitleaks:allow (valor FALSO de teste)
        self.assertTrue(segredos.apagar("VIGIA_TOKEN"))
        self.assertFalse(segredos.apagar("VIGIA_TOKEN"))
        self.assertEqual(segredos.ler("VIGIA_TOKEN"), "")

    def test_servico_por_ambiente_e_padrao(self):
        with mock.patch.dict(os.environ, {"VIGIA_KEYCHAIN_SERVICO": "com.exemplo.outro"}):
            segredos.ler("VIGIA_TOKEN")
        self.assertIn("com.exemplo.outro", self.sec.chamadas[-1][0])
        with mock.patch.dict(os.environ, {"VIGIA_KEYCHAIN_SERVICO": ""}):
            self.assertEqual(segredos.servico(), "io.vigia.vigia")


class TestCompletar(Base):
    def test_keychain_vence_o_env_e_ausente_mantem(self):
        self.sec.itens["VIGIA_TOKEN"] = "do-keychain-1234"
        env = {"VIGIA_TOKEN": "do-env", "FRENTES_TOKEN": "frentes-env", "VIGIA_PORT": "8120"}
        saida = segredos.completar(env, ("VIGIA_TOKEN", "FRENTES_TOKEN"))
        self.assertIs(saida, env)
        self.assertEqual(env["VIGIA_TOKEN"], "do-keychain-1234")
        self.assertEqual(env["FRENTES_TOKEN"], "frentes-env")
        self.assertEqual(env["VIGIA_PORT"], "8120")

    def test_so_pede_as_chaves_listadas(self):
        """R-9: quem chama com uma lista curta (coletor) nunca dispara leitura de VIGIA_TOKEN_ENVIO."""
        segredos.completar({}, ("EVOLUTION_INSTANCE_APIKEY", "FRENTES_TOKEN"))
        contas = [c[0][c[0].index("-a") + 1] for c in self.sec.chamadas]
        self.assertEqual(contas, ["EVOLUTION_INSTANCE_APIKEY", "FRENTES_TOKEN"])


class TestMigrarExportar(Base):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.raiz = Path(self.tmp.name)
        self.arq_envio = self.raiz / "casa" / ".vigia-envio.env"
        self.arq_envio.parent.mkdir()
        p = mock.patch.dict(segredos.ARQUIVO_POR_CHAVE, {"VIGIA_TOKEN_ENVIO": self.arq_envio})
        p.start()
        self.addCleanup(p.stop)
        (self.raiz / ".env").write_text(
            "# config do vigia\nEVOLUTION_URL=http://127.0.0.1:8080\nEVOLUTION_INSTANCE=vigia\n"
            "EVOLUTION_INSTANCE_APIKEY=apikey-0123456789\nVIGIA_PORT=8120\nVIGIA_TOKEN=token-painel-0123456789\n"  # gitleaks:allow (valor FALSO de teste)
            "FRENTES_URL=http://127.0.0.1:8110\nFRENTES_TOKEN=frentes-0123456789 # comentário\nDONO_WHATSAPP=5592900000000\n",  # gitleaks:allow (valor FALSO de teste)
            encoding="utf-8")
        self.arq_envio.write_text("VIGIA_TOKEN_ENVIO=envio-0123456789\n", encoding="utf-8")  # gitleaks:allow (valor FALSO de teste)

    def test_migrar_grava_confere_e_limpa_os_arquivos(self):
        r = segredos.migrar(self.raiz)
        self.assertEqual(r["gravadas"], list(segredos.SECRETAS))
        self.assertEqual((r["faltando"], r["falhas"]), ([], []))
        self.assertEqual(self.sec.itens["VIGIA_TOKEN"], "token-painel-0123456789")
        self.assertEqual(self.sec.itens["VIGIA_TOKEN_ENVIO"], "envio-0123456789")
        env = (self.raiz / ".env").read_text(encoding="utf-8")
        for chave in ("VIGIA_TOKEN=", "EVOLUTION_INSTANCE_APIKEY=", "FRENTES_TOKEN="):
            self.assertNotIn(chave, env)
        for mantido in ("EVOLUTION_URL=http://127.0.0.1:8080", "VIGIA_PORT=8120", "DONO_WHATSAPP=5592900000000", "# config do vigia"):
            self.assertIn(mantido, env)
        self.assertIn("moram no Keychain", env)
        self.assertEqual(oct((self.raiz / ".env").stat().st_mode & 0o777), "0o600")
        self.assertFalse(self.arq_envio.exists())
        self.assertEqual(sorted(r["arquivos_limpos"]), sorted([str(self.raiz / ".env"), str(self.arq_envio)]))

    def test_manter_env_nao_toca_nos_arquivos(self):
        antes = (self.raiz / ".env").read_text(encoding="utf-8")
        r = segredos.migrar(self.raiz, manter_env=True)
        self.assertEqual(len(r["gravadas"]), 4)
        self.assertEqual((self.raiz / ".env").read_text(encoding="utf-8"), antes)
        self.assertTrue(self.arq_envio.exists())

    def test_falha_em_uma_chave_preserva_os_arquivos(self):
        (self.raiz / ".env").write_text((self.raiz / ".env").read_text().replace("FRENTES_TOKEN=frentes-0123456789", "FRENTES_TOKEN=tem espaco"))  # gitleaks:allow (valor FALSO de teste)
        r = segredos.migrar(self.raiz)
        self.assertEqual(r["falhas"], ["FRENTES_TOKEN"])
        self.assertIn("VIGIA_TOKEN=", (self.raiz / ".env").read_text())  # nada foi tirado do arquivo
        self.assertTrue(self.arq_envio.exists())

    def test_migrar_idempotente_e_conferir_sem_valores(self):
        segredos.migrar(self.raiz)
        r = segredos.migrar(self.raiz)
        self.assertEqual((r["gravadas"], r["faltando"]), ([], []))  # arquivos já limpos, Keychain já tem tudo
        conf = segredos.conferir(self.raiz)
        self.assertEqual([(c["chave"], c["keychain"], c["arquivo"]) for c in conf],
                         [(k, True, False) for k in segredos.SECRETAS])
        saida = io.StringIO()
        with contextlib.redirect_stdout(saida), mock.patch.object(segredos, "RAIZ", self.raiz):
            self.assertEqual(segredos.main(["conferir"]), 0)
        for valor in self.sec.itens.values():
            self.assertNotIn(valor, saida.getvalue())

    def test_exportar_e_o_caminho_de_volta(self):
        segredos.migrar(self.raiz)
        r = segredos.exportar(self.raiz)
        self.assertEqual(r["escritas"], list(segredos.SECRETAS))
        env = segredos._ler_env(self.raiz / ".env")
        self.assertEqual(env["VIGIA_TOKEN"], "token-painel-0123456789")
        self.assertEqual(env["VIGIA_PORT"], "8120")
        self.assertEqual(segredos._ler_env(self.arq_envio)["VIGIA_TOKEN_ENVIO"], "envio-0123456789")
        self.assertEqual(oct(self.arq_envio.stat().st_mode & 0o777), "0o600")

    def test_desligado_nao_migra(self):
        with mock.patch.dict(os.environ, {"VIGIA_KEYCHAIN": "off"}), self.assertRaises(RuntimeError):
            segredos.migrar(self.raiz)

    def test_cli_gravar_le_o_valor_pelo_stdin(self):
        with mock.patch.object(sys, "stdin", io.StringIO("valor-0123456789\n")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(segredos.main(["gravar", "VIGIA_TOKEN"]), 0)
        self.assertEqual(self.sec.itens["VIGIA_TOKEN"], "valor-0123456789")


if __name__ == "__main__":
    unittest.main()
