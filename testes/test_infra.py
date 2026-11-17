"""Teste próprio da estação E1 (infra): compose, exemplo de env, SQLs e bin/evolution.sh.
Quem chama: unittest. Usa um `docker` falso no PATH; nunca toca Docker, rede ou Postgres reais.
O que não faz: não prova o SQL contra um Postgres (isso é o smoke da F5).
"""
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from testes.util_contrato import RAIZ  # noqa: E402

SH = RAIZ / "bin" / "evolution.sh"
COMPOSE = RAIZ / "infra" / "docker-compose.yml"
EXEMPLO = RAIZ / "infra" / ".env.compose.exemplo"
SENHAS = {
    "EVOLUTION_GLOBAL_KEY": "chaveglobalsecreta1",
    "POSTGRES_PASSWORD": "senhapgsecreta2",
    "VIGIA_RO_PASSWORD": "senharosecreta3",
    "VIGIA_LIMPEZA_PASSWORD": "senhalimpsecreta4",
}

DOCKER_FALSO = """#!/bin/bash
# registra argumentos e entrada padrão; responde 't' à checagem da tabela
echo "ARGS: $*" >> "$LOG_DOCKER"
if [ ! -t 0 ]; then cat >> "$LOG_STDIN"; fi
case "$*" in "compose version") exit ${DOCKER_SEM_COMPOSE:-0};; esac
case "$*" in "info") exit ${DOCKER_INFO_SAIDA:-0};; esac
case "$*" in *pg_isready*) exit ${DOCKER_PG_SAIDA:-0};; esac
case "$*" in *"--services"*) for s in ${DOCKER_SERVICOS-api db redis}; do echo "$s"; done; exit 0;; esac
case "$*" in *to_regclass*) echo t;; esac
exit ${DOCKER_SAIDA:-0}
"""

# compose v1 (binário docker-compose): mesmo comportamento, sem o prefixo "compose"
DOCKER_COMPOSE_V1_FALSO = DOCKER_FALSO.replace('echo "ARGS: $*"', 'echo "ARGS(v1): $*"')

CURL_FALSO = """#!/bin/bash
echo "ARGS: $*" >> "$LOG_CURL"
cfg="$(cat)"
echo "$cfg" >> "$LOG_CURL_CFG"
case "$cfg" in
  *instance/create*) echo '{"instance":{"instanceName":"vigia"},"hash":"apikey-da-instancia-9"}'; echo "${CURL_COD_CRIAR:-201}";;
  *) echo '{"ok":true}'; echo "${CURL_COD_SET:-200}";;
esac
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        (t / "bin").mkdir()
        d = t / "bin" / "docker"
        d.write_text(DOCKER_FALSO)
        d.chmod(d.stat().st_mode | stat.S_IXUSR)
        d1 = t / "bin" / "docker-compose"
        d1.write_text(DOCKER_COMPOSE_V1_FALSO)
        d1.chmod(d1.stat().st_mode | stat.S_IXUSR)
        c = t / "bin" / "curl"
        c.write_text(CURL_FALSO)
        c.chmod(c.stat().st_mode | stat.S_IXUSR)
        self.log_curl, self.cfg_curl = t / "curl.log", t / "curl_cfg.log"
        self.env_file = t / "env.compose"
        self.env_file.write_text("".join(f"{k}={v}\n" for k, v in SENHAS.items()))
        self.env_file.chmod(0o600)
        self.app_env = t / "app.env"
        self.config = t / "config.toml"
        self.config.write_text("[grupos]\npermitidos = []\n")
        self.log, self.stdin = t / "docker.log", t / "stdin.log"
        self.base_env = {
            "PATH": f"{t / 'bin'}:/usr/bin:/bin", "LOG_DOCKER": str(self.log), "LOG_STDIN": str(self.stdin),
            "VIGIA_ENV_COMPOSE": str(self.env_file),
            "VIGIA_ENV_APP": str(self.app_env), "VIGIA_CONFIG": str(self.config),
            "LOG_CURL": str(self.log_curl), "LOG_CURL_CFG": str(self.cfg_curl), "HOME": str(t),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def rodar(self, *args, env=None):
        e = dict(self.base_env)
        e.update(env or {})
        return subprocess.run(["bash", str(SH), *args], capture_output=True, text=True, timeout=60,
                              env=e, stdin=subprocess.DEVNULL)

    def args_docker(self) -> str:
        return self.log.read_text() if self.log.exists() else ""


class TestArquivos(unittest.TestCase):
    def test_exemplo_tem_todas_as_variaveis_do_compose(self):
        usadas = set(re.findall(r"\$\{(\w+)\}", COMPOSE.read_text(encoding="utf-8")))
        chaves = set(re.findall(r"^(\w+)=", EXEMPLO.read_text(encoding="utf-8"), re.M))
        self.assertTrue(usadas, "compose deveria usar variáveis")
        self.assertLessEqual(usadas, chaves, f"faltam no exemplo: {usadas - chaves}")
        self.assertTrue({"VIGIA_RO_PASSWORD", "VIGIA_LIMPEZA_PASSWORD"} <= chaves)

    def test_exemplo_so_placeholder(self):
        for linha in EXEMPLO.read_text(encoding="utf-8").splitlines():
            if re.match(r"^\w+=", linha):
                self.assertTrue(linha.split("=", 1)[1].startswith("TROQUE"), linha)

    def test_compose_sem_segredo_literal_e_limites(self):
        t = COMPOSE.read_text(encoding="utf-8")
        self.assertNotRegex(t, r"(?i)(password|key|secret)\w*:\s*[\"']?(?!\$\{)[A-Za-z0-9]{12,}")
        for lim in ('memory: 1G', 'memory: 512M', 'memory: 256M', 'cpus: "1.0"', 'cpus: "0.5"', 'cpus: "0.3"'):
            self.assertIn(lim, t)

    def test_nenhuma_porta_fora_do_loopback_no_arquivo(self):
        for m in re.finditer(r'^\s*-\s*"?(\S+?:\d+:\d+)"?\s*$', COMPOSE.read_text(encoding="utf-8"), re.M):
            self.assertTrue(m.group(1).startswith("127.0.0.1:"), m.group(1))

    def test_sql_001_nao_toca_na_tabela_do_evolution(self):
        s = (RAIZ / "infra/sql/001_vigia_fila.sql").read_text(encoding="utf-8")
        self.assertRegex(s, r"AFTER INSERT ON public\.\"Message\"")
        self.assertIn("NEW.id", s)
        self.assertNotRegex(s, r"(?i)\b(DROP\s+TABLE|TRUNCATE|UPDATE\s+public|DELETE\s+FROM\s+public)\b")
        self.assertIn("message_id text NOT NULL UNIQUE", s)

    def test_sql_002_privilegios_exatos(self):
        s = (RAIZ / "infra/sql/002_papeis.sql").read_text(encoding="utf-8")
        self.assertNotRegex(s, r"(?i)\b(SUPERUSER|CREATEDB|CREATEROLE|GRANT\s+ALL)\b")
        ro = re.findall(r"GRANT (\w+) ON ([^;]+?) TO vigia_ro;", s)
        self.assertEqual([r[0] for r in ro], ["SELECT", "SELECT"])  # tabelas + a sequência da fila
        self.assertTrue(all("DELETE" not in r[1] for r in ro))
        self.assertNotRegex(s, r"GRANT DELETE[^;]*vigia_ro;")
        # D-25: DELETE direto revogado; só via função SECURITY DEFINER
        self.assertNotRegex(s, r"GRANT DELETE")
        self.assertRegex(s, r"REVOKE DELETE ON public\.\"Message\", vigia\.fila FROM vigia_limpeza;")
        self.assertRegex(s, r"GRANT SELECT ON SEQUENCE vigia\.fila_seq_seq TO vigia_ro;")
        self.assertIn("vigia.limpar_janela(ini int, fim int) RETURNS int", s)
        self.assertIn("SECURITY DEFINER", s)
        self.assertIn("SET search_path = pg_catalog, public, vigia", s)
        self.assertRegex(s, r"date_trunc\('week', now\(\)\)")
        self.assertRegex(s, r"RAISE EXCEPTION[^;]*semana ISO corrente")
        self.assertRegex(s, r"REVOKE EXECUTE ON FUNCTION vigia\.limpar_janela\(int, int\) FROM PUBLIC;")
        self.assertRegex(s, r"GRANT EXECUTE ON FUNCTION vigia\.limpar_janela\(int, int\) TO vigia_limpeza;")
        self.assertNotRegex(s, r"(?i)ALTER\s+TABLE|UPDATE\s+public")
        self.assertIn(":'ro_pass'", s)
        self.assertNotRegex(s, r"PASSWORD\s+'")  # nenhuma senha literal

    def test_sql_001_trigger_atomico_e_fila_que_nunca_derruba_a_evolution(self):
        s = (RAIZ / "infra/sql/001_vigia_fila.sql").read_text(encoding="utf-8")
        self.assertIn("CREATE OR REPLACE TRIGGER vigia_fila_apos_insert", s)
        self.assertNotRegex(s, r"(?i)DROP\s+TRIGGER")
        self.assertRegex(s, r"EXCEPTION WHEN others THEN\s+(--[^\n]*\n\s*)*RAISE WARNING[^;]*;\s+RETURN NEW;")

    def test_compose_telemetria_cors_e_redis_persistente(self):
        t = COMPOSE.read_text(encoding="utf-8")
        for linha in ('TELEMETRY_ENABLED: "false"', 'TELEMETRY_URL: ""',
                      'AUTHENTICATION_EXPOSE_IN_FETCH_INSTANCES: "false"',
                      'CORS_ORIGIN: "*"',
                      'command: ["redis-server", "--appendonly", "yes"]',
                      "- evolution_redis:/data"):
            self.assertIn(linha, t)
        self.assertRegex(t, r"(?m)^volumes:\n(\s+\w+:\n)*\s+evolution_redis:")


class TestEvolutionSh(Base):
    def test_help_e_sem_args(self):
        self.assertEqual(self.rodar("--help").returncode, 0)
        r = self.rodar()
        self.assertEqual(r.returncode, 2)

    def test_comando_desconhecido_exit_2(self):
        self.assertEqual(self.rodar("explodir").returncode, 2)

    def test_dry_run_nao_chama_docker(self):
        for c in ("up", "down", "status", "schema", "restart"):
            r = self.rodar("--dry-run", c)
            self.assertEqual(r.returncode, 0, c)
        self.assertEqual(self.args_docker(), "")

    def test_env_ausente_exit_2(self):
        r = self.rodar("status", env={"VIGIA_ENV_COMPOSE": str(self.env_file) + ".nao"})
        self.assertEqual(r.returncode, 2)

    def test_placeholder_recusado(self):
        self.env_file.write_text(EXEMPLO.read_text(encoding="utf-8"))
        self.assertEqual(self.rodar("up").returncode, 2)
        self.assertNotIn(" up -d", self.args_docker())

    @unittest.skipIf(any(os.access(f"{d}/docker", os.X_OK) for d in ("/usr/bin", "/bin")),
                     "docker instalado em /usr/bin (runner Linux): não dá para simular a ausência só pelo PATH")
    def test_docker_ausente_exit_2(self):
        r = self.rodar("status", env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(r.returncode, 2)

    def test_status_down_restart(self):
        for c, esperado in (("status", " ps"), ("down", " down"), ("restart", "restart api")):
            self.assertEqual(self.rodar(c).returncode, 0, c)
            self.assertIn(esperado, self.args_docker(), c)
        self.assertIn("--env-file", self.args_docker())

    def test_falha_do_docker_vira_exit_1(self):
        self.assertEqual(self.rodar("status", env={"DOCKER_SAIDA": "3"}).returncode, 1)

    def test_status_exige_db_e_api_rodando_pg_e_http(self):
        r = self.rodar("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        a = self.args_docker()
        self.assertIn("ps --status running --services", a)
        self.assertIn("pg_isready -U evolution", a)
        self.assertIn("http://127.0.0.1:8080/", self.cfg_curl.read_text())

    def test_status_servico_parado_exit_1(self):
        for servicos, falta in (("db redis", "api"), ("api redis", "db"), ("", "db"), ("api redis db2", "db")):
            r = self.rodar("status", env={"DOCKER_SERVICOS": servicos})
            self.assertEqual(r.returncode, 1, servicos)
            self.assertIn(falta, r.stderr)

    def test_status_postgres_nao_pronto_exit_1(self):
        self.assertEqual(self.rodar("status", env={"DOCKER_PG_SAIDA": "2"}).returncode, 1)

    def test_status_api_sem_200_exit_1(self):
        self.assertEqual(self.rodar("status", env={"CURL_COD_SET": "503"}).returncode, 1)

    def test_fallback_para_docker_compose_v1(self):
        r = self.rodar("down", env={"DOCKER_SEM_COMPOSE": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ARGS(v1): -f ", self.args_docker())
        self.assertIn("--env-file", self.args_docker())
        self.assertIn(" down", self.args_docker())

    def test_sem_nenhum_compose_exit_2_com_dica(self):
        (Path(self.tmp.name) / "bin" / "docker-compose").unlink()
        r = self.rodar("status", env={"DOCKER_SEM_COMPOSE": "1"})
        self.assertEqual(r.returncode, 2)
        self.assertIn("brew install docker-compose", r.stderr)
        self.assertIn("cliPluginsExtraDirs", r.stderr)
        self.assertIn("/opt/homebrew/lib/docker/cli-plugins", r.stderr)

    def test_daemon_parado_exit_2(self):
        for c in ("status", "up", "down"):
            r = self.rodar(c, env={"DOCKER_INFO_SAIDA": "1"})
            self.assertEqual(r.returncode, 2, c)
            self.assertIn("daemon parado", r.stderr)
        self.assertNotIn(" up -d", self.args_docker())

    def test_env_compose_com_permissao_de_grupo_ou_outros_exit_2(self):
        for modo in (0o640, 0o604, 0o644, 0o660, 0o777):
            self.env_file.chmod(modo)
            r = self.rodar("status")
            self.assertEqual(r.returncode, 2, oct(modo))
            self.assertIn("chmod 600", r.stderr)
        self.env_file.chmod(0o400)
        self.assertEqual(self.rodar("status").returncode, 0)
        self.env_file.chmod(0o600)
        self.assertEqual(self.rodar("status").returncode, 0)

    def test_psql_ro_usa_vigia_ro_e_tab(self):
        r = self.rodar("psql-ro", "SELECT 1")
        self.assertEqual(r.returncode, 0)
        a = self.args_docker()
        self.assertIn("-U vigia_ro", a)
        self.assertIn("-At", a)
        self.assertEqual(self.rodar("psql-ro").returncode, 2)

    def test_sql_e_schema(self):
        self.assertEqual(self.rodar("sql", "SELECT 1").returncode, 0)
        self.assertIn("-U evolution", self.args_docker())
        self.assertEqual(self.rodar("schema").returncode, 0)
        a = self.args_docker()
        self.assertIn('\\d public."Message"', a)
        self.assertIn('\\d public."Contact"', a)

    def test_sql_stdin_le_a_query_do_stdin_e_nao_do_argv(self):
        """I3: sql-stdin roda `psql -f -` como evolution; a query viaja pelo stdin, nunca em argv."""
        e = dict(self.base_env)
        q = "INSERT INTO t VALUES ('conteudo-de-conversa-\U0001F600')"
        r = subprocess.run(["bash", str(SH), "sql-stdin"], capture_output=True, text=True, timeout=60, env=e, input=q)
        self.assertEqual(r.returncode, 0, r.stderr)
        a = self.args_docker()
        self.assertIn("-U evolution", a)
        self.assertIn("-f -", a)
        self.assertIn("-At", a)
        self.assertNotIn("conteudo-de-conversa", a)
        self.assertEqual(self.stdin.read_text(encoding="utf-8"), q)

    def test_psql_ro_stdin_e_psql_limpeza_stdin_levam_a_query_pelo_stdin(self):
        """Painel, coletor e fechar-semana usam estas variantes: o jid da conversa nunca aparece em argv/ps."""
        q = "SELECT 1 FROM public.\"Message\" WHERE \"remoteJid\" = '5592900001111@s.whatsapp.net'"
        for sub, usuario in (("psql-ro-stdin", "vigia_ro"), ("psql-limpeza-stdin", "vigia_limpeza")):
            self.stdin.unlink(missing_ok=True)       # o docker falso acumula (>>) o stdin de cada chamada
            r = subprocess.run(["bash", str(SH), sub], capture_output=True, text=True, timeout=60,
                               env=dict(self.base_env), input=q)
            self.assertEqual(r.returncode, 0, r.stderr)
            a = self.args_docker()
            self.assertIn(f"-U {usuario}", a)
            self.assertIn("-f -", a)
            self.assertIn("-At", a)
            self.assertIn("ON_ERROR_STOP=1", a)
            self.assertNotIn("5592900001111", a)
            self.assertNotIn("5592900001111", r.stdout + r.stderr)
            self.assertEqual(self.stdin.read_text(encoding="utf-8"), q)

    def test_up_aplica_sql_com_senha_so_no_stdin(self):
        r = self.rodar("up")
        self.assertEqual(r.returncode, 0, r.stderr)
        entrada = self.stdin.read_text()
        self.assertIn("\\set ro_pass 'senharosecreta3'", entrada)
        self.assertIn("CREATE TABLE IF NOT EXISTS vigia.fila", entrada)
        self.assertLess(entrada.index("vigia.fila ("), entrada.index("CREATE ROLE vigia_ro"))
        a = self.args_docker()
        self.assertIn(" up -d", a)

    def test_nunca_ecoa_nem_passa_segredo_em_argumento(self):
        saidas = []
        for args in (("up",), ("status",), ("schema",), ("sql", "SELECT 1"), ("psql-ro", "SELECT 1"), ("restart",)):
            r = self.rodar(*args)
            saidas.append(r.stdout + r.stderr)
        saidas.append(self.args_docker())
        junto = "\n".join(saidas)
        for v in SENHAS.values():
            self.assertNotIn(v, junto)
        self.assertNotRegex(junto, r"PGPASSWORD=")

    def test_senha_com_caractere_perigoso_recusada(self):
        self.env_file.write_text(self.env_file.read_text().replace("senharosecreta3", "a'b;c"))
        self.assertEqual(self.rodar("status").returncode, 2)

    def test_psql_limpeza_usa_vigia_limpeza(self):
        self.assertEqual(self.rodar("psql-limpeza", "SELECT 1").returncode, 0)
        a = self.args_docker()
        self.assertIn("-U vigia_limpeza", a)
        self.assertIn("-At", a)
        self.assertNotIn("senhalimpsecreta4", a)
        self.assertEqual(self.rodar("psql-limpeza").returncode, 2)

    def test_nenhum_codigo_pede_a_evolution_para_marcar_como_lida(self):
        """A página de privacidade promete que o painel nunca marca mensagem como lida: nenhuma rota disso no código."""
        for pasta, padrao in (("app", "*.py"), ("bin", "*")):
            for arq in sorted((RAIZ / pasta).rglob(padrao)):
                if arq.is_file() and "__pycache__" not in arq.parts:
                    fonte = arq.read_text(encoding="utf-8", errors="ignore")
                    self.assertTrue("markMessageAsRead" not in fonte and "markChatUnread" not in fonte, arq.name)

    def test_instancia_cria_com_qrcode_false_e_grava_chave_no_env_sem_imprimir(self):
        r = self.rodar("instancia")
        self.assertEqual(r.returncode, 0, r.stderr)
        cfg = self.cfg_curl.read_text()
        self.assertIn("instance/create", cfg)
        self.assertIn('\\"qrcode\\":false', cfg)
        self.assertIn('\\"instanceName\\":\\"vigia\\"', cfg)
        self.assertIn('\\"syncFullHistory\\":false', cfg)
        self.assertIn('\\"readMessages\\":false', cfg)      # o celular continua sendo o lugar da leitura:
        self.assertIn('\\"readStatus\\":false', cfg)        # a instância nunca marca mensagem nem status como visto
        self.assertIn("settings/set/vigia", cfg)
        self.assertIn("apikey: chaveglobalsecreta1", cfg)
        argv = self.log_curl.read_text()
        self.assertIn("--config -", argv)
        self.assertNotIn("chaveglobalsecreta1", argv)
        self.assertNotRegex(argv, r"(?i)-H|--header|apikey")
        saida = r.stdout + r.stderr
        self.assertNotIn("chaveglobalsecreta1", saida)
        self.assertNotIn("apikey-da-instancia-9", saida)
        self.assertIn("apikey gravada em .env", r.stdout)
        self.assertEqual(self.app_env.read_text(), "EVOLUTION_INSTANCE_APIKEY=apikey-da-instancia-9\n")
        self.assertEqual(stat.S_IMODE(self.app_env.stat().st_mode), 0o600)

    def test_instancia_substitui_linha_existente_e_preserva_o_resto(self):
        self.app_env.write_text("A=1\nEVOLUTION_INSTANCE_APIKEY=velha\nB=2\n")
        self.app_env.chmod(0o644)
        r = self.rodar("instancia")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.app_env.read_text(), "A=1\nEVOLUTION_INSTANCE_APIKEY=apikey-da-instancia-9\nB=2\n")
        self.assertEqual(stat.S_IMODE(self.app_env.stat().st_mode), 0o600)

    def test_instancia_acrescenta_linha_quando_nao_existe(self):
        self.app_env.write_text("A=1\n")
        self.assertEqual(self.rodar("instancia").returncode, 0)
        self.assertEqual(self.app_env.read_text(), "A=1\nEVOLUTION_INSTANCE_APIKEY=apikey-da-instancia-9\n")
        self.assertEqual([p.name for p in self.app_env.parent.iterdir() if p.name.startswith("app.env.")], [])

    def test_instancia_groupsignore_segue_config(self):
        self.rodar("instancia")
        self.assertIn('\\"groupsIgnore\\":true', self.cfg_curl.read_text())
        self.assertNotIn('\\"groupsIgnore\\":false', self.cfg_curl.read_text())
        self.cfg_curl.unlink()
        self.config.write_text('[grupos]\npermitidos = ["123@g.us"]\n')
        self.rodar("instancia")
        cfg = self.cfg_curl.read_text()
        self.assertIn('\\"groupsIgnore\\":false', cfg)
        self.assertNotIn('\\"groupsIgnore\\":true', cfg)
        self.cfg_curl.unlink()
        self.config.write_text("[grupos]\npermitidos = [  ]  # nenhum\n")
        self.rodar("instancia")
        self.assertIn('\\"groupsIgnore\\":true', self.cfg_curl.read_text())
        # r02 (D-39): grupos na tela de Conversas também fazem a Evolution guardar mensagem de grupo
        self.cfg_curl.unlink()
        self.config.write_text("[grupos]\npermitidos = []\n\n[conversas]\ngrupos = true\n")
        self.rodar("instancia")
        self.assertIn('\\"groupsIgnore\\":false', self.cfg_curl.read_text())
        self.cfg_curl.unlink()
        self.config.write_text("[grupos]\npermitidos = []\n\n[conversas]\ngrupos = false\n")
        self.rodar("instancia")
        self.assertIn('\\"groupsIgnore\\":true', self.cfg_curl.read_text())

    def test_instancia_apikey_estranha_nao_e_gravada(self):
        c = Path(self.tmp.name) / "bin" / "curl"
        c.write_text(CURL_FALSO.replace("apikey-da-instancia-9", "chave com espaco"))
        r = self.rodar("instancia")
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("chave com espaco", r.stdout + r.stderr)
        self.assertFalse(self.app_env.exists())

    def test_instancia_ja_existente_so_reaplica_settings(self):
        r = self.rodar("instancia", env={"CURL_COD_CRIAR": "403"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("settings/set/vigia", self.cfg_curl.read_text())
        self.assertNotIn("apikey-da-instancia-9", r.stdout)
        self.assertFalse(self.app_env.exists())

    def test_instancia_erro_http_exit_1(self):
        self.assertEqual(self.rodar("instancia", env={"CURL_COD_CRIAR": "500"}).returncode, 1)
        self.assertEqual(self.rodar("instancia", env={"CURL_COD_SET": "500"}).returncode, 1)

    def test_instancia_dry_run_nao_chama_curl(self):
        self.assertEqual(self.rodar("--dry-run", "instancia").returncode, 0)
        self.assertFalse(self.log_curl.exists())

    def test_executavel_e_shebang(self):
        self.assertTrue(os.access(SH, os.X_OK))
        self.assertTrue(SH.read_text(encoding="utf-8").startswith("#!/bin/bash"))


if __name__ == "__main__":
    unittest.main()
