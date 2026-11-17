"""Contrato executável da estação coletor (E6): app/lotes.py e bin/coletor.py.
Quem chama: unittest.
O que não faz: não chama Claude, Evolution nem Postgres reais (psql, executor e clientes são stubs).
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from testes.fakes import fila_fixture, imagem_jpeg_minima  # noqa: E402
from testes.util_contrato import RAIZ, _arquivo, _importar  # noqa: E402

PAREADO = 1_700_000_000


class _Ponte:
    def contexto_markdown(self):
        return "# Frentes\n- 1 Agência"


class _Banco:
    def ultimas_da_conversa(self, conversa, n=10):
        return []


class _Evo:
    def __init__(self):
        self.baixadas = []

    def midia_base64(self, key):
        self.baixadas.append(key)
        return imagem_jpeg_minima()


class TestContratoColetor(unittest.TestCase):
    def setUp(self):
        self.lotes = _importar("app.lotes", "coletor")

    def _tres(self):
        linhas = fila_fixture(3, PAREADO + 100)
        linhas[0]["messageTimestamp"] = PAREADO - 100            # anterior ao pareamento
        linhas[1]["key"]["remoteJid"] = "120363000000000001@g.us"  # grupo não permitido
        return linhas

    def test_filtrar_e_agrupar_da_um_conversa(self):
        ok = self.lotes.filtrar(self._tres(), desde=PAREADO, grupos_permitidos=set())
        self.assertEqual(len(ok), 1)
        self.assertEqual(len(self.lotes.agrupar_por_conversa(ok)), 1)

    def test_filtrar_descarta_status_broadcast_e_aceita_grupo_permitido(self):
        linhas = fila_fixture(2, PAREADO + 10)
        linhas[0]["key"]["remoteJid"] = "status@broadcast"
        linhas[1]["key"]["remoteJid"] = "120363000000000002@g.us"
        ok = self.lotes.filtrar(linhas, desde=PAREADO, grupos_permitidos={"120363000000000002@g.us"})
        self.assertEqual([x["key"]["remoteJid"] for x in ok], ["120363000000000002@g.us"])

    def test_ler_fila_consulta_vigia_fila(self):
        base = fila_fixture(2, PAREADO + 10)
        consultas = []

        def psql(q):
            consultas.append(q)
            return [[str(r["seq"]), r["id"], json.dumps(r["key"]), r["pushName"], r["messageType"],
                     json.dumps(r["message"]), str(r["messageTimestamp"])] for r in base]

        r = self.lotes.ler_fila(psql, 10, limite=500)
        q = consultas[0]
        for trecho in ("vigia.fila", '"Message"', "ORDER BY", "LIMIT"):
            self.assertIn(trecho, q)
        self.assertIn("10", q)
        self.assertEqual([x["seq"] for x in r], [1, 2])
        self.assertEqual(r[0]["key"]["remoteJid"], base[0]["key"]["remoteJid"])

    def test_montar_lote_escreve_conversa_contexto_e_baixa_com_teto(self):
        linhas = fila_fixture(3, PAREADO + 10)  # conversation, imageMessage, audioMessage (3 conversas)
        jid = linhas[0]["key"]["remoteJid"]
        for ln in linhas:
            ln["key"]["remoteJid"] = jid
        linhas.append(dict(fila_fixture(2, PAREADO + 500)[1]))
        linhas[-1]["key"] = dict(linhas[-1]["key"], remoteJid=jid)
        evo = _Evo()
        with tempfile.TemporaryDirectory() as t:
            pasta = Path(t)
            self.lotes.montar_lote(pasta, jid, linhas, [], banco=_Banco(), ponte=_Ponte(),
                                   evo=evo, teto_imagens=1, pausa_s=0.0)
            conv = list(pasta.rglob("conversa.md"))
            ctx = list(pasta.rglob("contexto.md"))
            self.assertTrue(conv and ctx)
            texto = conv[0].read_text(encoding="utf-8")
            self.assertIn("[novo]", texto)
            self.assertIn("[imagem:", texto)
            self.assertIn("[áudio", texto)
            self.assertEqual(len(evo.baixadas), 1)  # 2 imagens novas, teto 1
            self.assertEqual(len(list(pasta.rglob("*.jpg"))), 1)
            self.assertIn("Agência", ctx[0].read_text(encoding="utf-8"))

    def test_chamar_claude_comando_da_constitution(self):
        vistos = []

        def executor(cmd, **kw):
            vistos.append(cmd)
            saida = {"type": "result", "result": 'ok\n{"lote":"l1","conversas":1,"novas":2,"itens":1,"atencao":0}'}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(saida), "")

        with tempfile.TemporaryDirectory() as t:
            r = self.lotes.chamar_claude(Path(t), modelo="sonnet", prompt_sistema=RAIZ / "prompts/vigia.md",
                                         executor=executor)
        cmd = list(vistos[0])
        self.assertIn("-p", cmd)
        i = cmd.index("--allowedTools")
        permitidas = []
        for a in cmd[i + 1:]:
            if a.startswith("--"):
                break
            permitidas.append(a)
        # D-17: Read confinado ao lote — 'Read(//<lote>/**)', nunca 'Read' puro
        self.assertTrue(any(a.startswith("Read(") for a in permitidas), permitidas)
        self.assertNotIn("Read", permitidas)
        self.assertIn("Bash(vigia-ponte:*)", permitidas)
        self.assertFalse([a for a in permitidas if "Write" in a])
        self.assertIsInstance(r, dict)
        self.assertEqual(r["novas"], 2)

    def test_coletor_cli_help_e_sem_claude_nao_cria_pasta(self):
        script = _arquivo("bin/coletor.py", "coletor")
        r = subprocess.run([sys.executable, str(script), "--help"], capture_output=True, text=True,
                           timeout=30, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0)
        for flag in ("--dry-run", "--sem-claude", "--config"):
            self.assertIn(flag, r.stdout)
        caixa = RAIZ / "dados" / "caixa"
        antes = sorted(p.name for p in caixa.iterdir()) if caixa.exists() else []
        r = subprocess.run([sys.executable, str(script), "--sem-claude", "--dry-run"], capture_output=True,
                           text=True, timeout=120, stdin=subprocess.DEVNULL)
        self.assertIn(r.returncode, (0, 2))  # 2 = Evolution/Postgres fora; nunca 1 em fila vazia
        depois = sorted(p.name for p in caixa.iterdir()) if caixa.exists() else []
        self.assertEqual(antes, depois)


if __name__ == "__main__":
    unittest.main()
