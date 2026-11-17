"""Contrato executável da estação infra (E1): compose, SQLs e bin/evolution.sh.
Quem chama: unittest. Olha arquivos por caminho relativo à raiz do repositório.
O que não faz: não sobe docker; `docker compose config -q` só roda se o docker existir.
"""
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from testes.util_contrato import RAIZ, _arquivo  # noqa: E402

ENV_ESPERADAS = [
    "SERVER_URL=http://127.0.0.1:8080", "AUTHENTICATION_API_KEY=${EVOLUTION_GLOBAL_KEY}",
    "DATABASE_ENABLED=true", "DATABASE_PROVIDER=postgresql", "DATABASE_SAVE_DATA_INSTANCE=true",
    "DATABASE_SAVE_DATA_NEW_MESSAGE=true", "DATABASE_SAVE_MESSAGE_UPDATE=true",
    "DATABASE_SAVE_DATA_CONTACTS=true", "DATABASE_SAVE_DATA_CHATS=false",
    "DATABASE_SAVE_DATA_LABELS=false", "DATABASE_SAVE_DATA_HISTORIC=true",
    "CACHE_REDIS_ENABLED=true", "CACHE_REDIS_URI=redis://redis:6379/0", "LOG_LEVEL=ERROR",
    "LOG_BAILEYS=error", "CONFIG_SESSION_PHONE_CLIENT=Greg", "CONFIG_SESSION_PHONE_NAME=Chrome",
    "WEBHOOK_GLOBAL_ENABLED=false", "WEBSOCKET_ENABLED=false", "RABBITMQ_ENABLED=false",
    "SQS_ENABLED=false", "CHATWOOT_ENABLED=false", "TYPEBOT_ENABLED=false", "OPENAI_ENABLED=false",
    "S3_ENABLED=false", "DEL_INSTANCE=false", "QRCODE_LIMIT=3",
    # Emenda F4 (5): nada sai do Mac, chave da instância não exposta, CORS só do painel
    "TELEMETRY_ENABLED=false", "AUTHENTICATION_EXPOSE_IN_FETCH_INSTANCES=false", "CORS_ORIGIN=*",
]


def _portas_por_servico(texto: str) -> dict:
    """Parser mínimo: {servico: [entradas de ports:]} lendo só a indentação."""
    servicos, atual, em_ports, ind_ports, em_servicos = {}, None, False, 0, False
    for linha in texto.splitlines():
        if not linha.strip() or linha.strip().startswith("#"):
            continue
        ind = len(linha) - len(linha.lstrip())
        if ind == 0:
            em_servicos = linha.strip() == "services:"
            atual, em_ports = None, False
            continue
        if not em_servicos:
            continue
        m = re.fullmatch(r"  ([\w-]+):\s*", linha)
        if m:
            atual = m.group(1)
            servicos[atual] = []
            em_ports = False
            continue
        if atual is None:
            continue
        if re.fullmatch(r"\s+ports:\s*", linha):
            em_ports, ind_ports = True, ind
        elif em_ports and ind > ind_ports and linha.strip().startswith("-"):
            servicos[atual].append(linha.strip()[1:].strip().strip("\"'"))
        elif em_ports and ind <= ind_ports:
            em_ports = False
    return servicos


class TestContratoInfra(unittest.TestCase):
    def _compose(self) -> str:
        return _arquivo("infra/docker-compose.yml", "infra").read_text(encoding="utf-8")

    def test_servicos_e_imagens_com_tag_fixa(self):
        t = self._compose()
        for img in ("evoapicloud/evolution-api:v2.3.7", "postgres:16-alpine", "redis:7-alpine"):
            self.assertIn(img, t)
        self.assertNotIn("latest", t)
        self.assertEqual(set(_portas_por_servico(t)), {"api", "db", "redis"})

    def test_portas_so_em_loopback(self):
        portas = _portas_por_servico(self._compose())
        self.assertEqual(portas["redis"], [])
        # pedido 49: a porta do lado do Mac é configurável (teste do DMG ao lado da produção), o padrão e o loopback não
        self.assertEqual(portas["api"], ["127.0.0.1:${EVOLUTION_PORTA_HOST:-8080}:8080"])
        # B4 da ciberseg r01 (15/11/2026): o Postgres não publica porta; o acesso é por docker exec e pela rede do compose
        self.assertEqual(portas["db"], [])
        for lista in portas.values():
            for p in lista:
                self.assertTrue(p.startswith("127.0.0.1:"), p)

    def test_env_da_evolution_e_volumes(self):
        t = self._compose()
        for item in ENV_ESPERADAS:
            chave, valor = item.split("=", 1)
            padrao = re.escape(chave) + r"\s*[:=]\s*[\"']?" + re.escape(valor)
            self.assertRegex(t, padrao, item)
        self.assertIn("DATABASE_CONNECTION_URI", t)
        self.assertIn("@db:5432/evolution", t)
        for vol in ("evolution_instances", "evolution_pg"):
            self.assertIn(vol, t)

    def test_exemplo_de_env_compose(self):
        _arquivo("infra/.env.compose.exemplo", "infra")

    def test_docker_compose_config(self):
        comp = _arquivo("infra/docker-compose.yml", "infra")
        env = _arquivo("infra/.env.compose.exemplo", "infra")
        if shutil.which("docker"):
            # plugin compose pode estar ausente (06/11: colima sem docker-compose): só valida se ele existir
            v = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True,
                               timeout=30, stdin=subprocess.DEVNULL)
            if v.returncode == 0:
                r = subprocess.run(["docker", "compose", "-f", str(comp), "--env-file", str(env), "config", "-q"],
                                   capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
                self.assertEqual(r.returncode, 0, f"compose config -q falhou: {r.stderr[-300:]}")
                return
        t = comp.read_text(encoding="utf-8")
        self.assertIn("ports:", t)
        self.assertIn("127.0.0.1:", t)

    def test_sql_idempotente_e_aditivo(self):
        s1 = _arquivo("infra/sql/001_vigia_fila.sql", "infra").read_text(encoding="utf-8")
        s2 = _arquivo("infra/sql/002_papeis.sql", "infra").read_text(encoding="utf-8")
        self.assertIn("IF NOT EXISTS", s1)
        for trecho in ("CREATE SCHEMA IF NOT EXISTS vigia", "vigia.fila", "message_id", "TRIGGER", 'public."Message"'):
            self.assertIn(trecho, s1)
        self.assertNotRegex(s1, r'(?i)ALTER\s+TABLE\s+public\."Message"')
        self.assertIn("IF NOT EXISTS", s2)
        for trecho in ("vigia_ro", "vigia_limpeza", "LOGIN", "SELECT", "DELETE"):
            self.assertIn(trecho, s2)

    def test_remendo_do_baileys_montado_e_integro(self):
        """D-55 (pedido 44): o Baileys rc.9 da v2.3.7 não confirma a stanza <status> e o WhatsApp fecha o stream a
        cada ~50 min, segurando as mensagens novas. A cópia corrigida tem que estar montada por cima do arquivo da
        imagem, com os três trechos, sintaxe válida e amarrada à tag v2.3.7 (imagem nova = rever o remendo)."""
        t = self._compose()
        patch = _arquivo("infra/patches/baileys-messages-recv.js", "infra")
        _arquivo("infra/patches/README.md", "infra")
        montagem = ("./patches/baileys-messages-recv.js:"
                    "/evolution/node_modules/baileys/lib/Socket/messages-recv.js:ro")
        self.assertIn(montagem, t)
        self.assertIn("evoapicloud/evolution-api:v2.3.7", t,
                      "tag da imagem mudou: reveja infra/patches/README.md antes de manter a montagem")
        js = patch.read_text(encoding="utf-8")
        self.assertIn("const handleStatus = async (node) => {", js)
        self.assertIn("await sendMessageAck(node);", js)
        self.assertIn("['status', handleStatus]", js)
        self.assertIn("ws.on('CB:status', async (node) => {", js)
        self.assertIn("await processNode('status', node, 'acking status stanza', handleStatus);", js)
        self.assertEqual(js.count("Greg (D-55"), 3)
        if shutil.which("node"):
            r = subprocess.run(["node", "--check", str(patch)], capture_output=True, text=True, timeout=30,
                               stdin=subprocess.DEVNULL)
            self.assertEqual(r.returncode, 0, f"node --check falhou: {r.stderr[-300:]}")

    def test_evolution_sh_help(self):
        sh = _arquivo("bin/evolution.sh", "infra")
        r = subprocess.run(["bash", str(sh), "--help"], capture_output=True, text=True, timeout=30,
                           stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0)
        texto = sh.read_text(encoding="utf-8")
        for sub in ("up", "down", "status", "schema", "sql", "psql-ro", "restart"):
            self.assertIn(sub, texto)


if __name__ == "__main__":
    unittest.main()
