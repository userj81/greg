"""Contrato executável da estação fechar_semana (E10): app/aviso.py e bin/fechar-semana.py.
Quem chama: unittest. O cofre é um servidor HTTP local de teste; subprocess é substituído.
O que não faz: não manda WhatsApp, não roda pg_dump nem DELETE de verdade.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from testes.util_contrato import RAIZ, _arquivo, _importar, _importar_arquivo  # noqa: E402


class _Cofre:
    """Captura POST /proxy; responde no envelope do cofre."""

    def __init__(self):
        self.recebidos = []
        dono = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                corpo = json.loads(self.rfile.read(n) or b"{}")
                dono.recebidos.append((self.path, dict(self.headers), corpo))
                dados = json.dumps({"success": True, "status": 201, "data": {"key": {"id": "3EB0"}}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(dados)))
                self.end_headers()
                self.wfile.write(dados)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def parar(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class TestContratoAviso(unittest.TestCase):
    def setUp(self):
        self.aviso = _importar("app.aviso", "fechar_semana")

    def test_recusa_texto_de_conversa(self):
        with self.assertRaises(ValueError):
            self.aviso.enviar("palavra " * 60)          # > 300 chars
        with self.assertRaises(ValueError):
            self.aviso.enviar("linha um\n\nlinha dois")   # quebra dupla
        with self.assertRaises(ValueError):
            self.aviso.enviar("Maria disse “paga hoje”")  # aspas de citação

    def test_envia_contador_e_link_pelo_cofre(self):
        tv = _Cofre()
        self.addCleanup(tv.parar)
        texto = "Semana 2026-W45 fechada · 12 msgs · 3 itens · limpeza em 24 h · http://127.0.0.1:8120/demandas"
        ok = self.aviso.enviar(texto, cofre_url=tv.url, token_fn=lambda: "token-de-teste")
        self.assertTrue(ok)
        caminho, cab, corpo = tv.recebidos[0]
        self.assertEqual(caminho, "/proxy")
        self.assertEqual(corpo["service"], "evolution")
        self.assertEqual(corpo["method"], "POST")
        self.assertEqual(corpo["endpoint"], "/message/sendText/bot-avisos")   # AVISO_INSTANCIA de testes/__init__.py
        self.assertEqual(corpo["body"]["text"], texto)
        self.assertTrue(corpo["body"]["number"])
        self.assertIn("token-de-teste", cab.values())


class TestContratoFecharSemana(unittest.TestCase):
    def setUp(self):
        self.fs = _importar_arquivo("bin/fechar-semana.py", "fechar_semana")
        self.banco_mod = _importar("app.banco", "banco")
        self.erros = _importar("app.erros", "F0")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.banco = self.banco_mod.Banco(str(Path(self.tmp.name) / "vigia.db"))

    def test_janela(self):
        ini, fim = self.fs.janela("2026-W45")
        self.assertLess(ini, fim)
        self.assertAlmostEqual(fim - ini, 7 * 86400, delta=3600)

    def test_limpar_com_count_residual_levanta_e_nao_grava_lapide(self):
        caixa = Path(self.tmp.name) / "caixa"
        backup = Path(self.tmp.name) / "backup"
        caixa.mkdir()
        backup.mkdir()
        psql = lambda q: [["1"]]  # noqa: E731  qualquer consulta devolve count 1
        ok = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch("subprocess.run", return_value=ok):
            with self.assertRaises(self.erros.LimpezaIncompleta):
                self.fs.limpar(psql, self.banco, ini_ts=1000, fim_ts=2000, pasta_caixa=caixa, backup_dir=backup)
        self.assertEqual(self.banco.lapides(), [])

    def test_relatorio_chama_claude_so_com_read(self):
        vistos = []

        def executor(cmd, **kw):
            vistos.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": "# Relatório da semana\n- ok"}), "")

        r = self.fs.relatorio(lambda q: [], self.banco, modelo="opus", executor=executor)
        self.assertIn("Relatório", r)
        cmd = vistos[0]
        self.assertIn("-p", cmd)
        self.assertIn("opus", cmd)
        i = cmd.index("--allowedTools")
        permitidas = []
        for a in cmd[i + 1:]:
            if a.startswith("--"):
                break
            permitidas.append(a)
        # Emenda F4 (8): Read confinado à pasta do JSON temporário, nunca 'Read' puro (igual à D-17 do coletor)
        self.assertEqual(len(permitidas), 1)
        self.assertRegex(permitidas[0], r"^Read\(//.+/\*\*\)$")

    def test_segurar_nao_apaga_nem_grava_lapide(self):
        antigo = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, antigo)
        os.makedirs("dados", exist_ok=True)
        self.fs.segurar(self.banco, "2026-W45", dias=7)
        self.assertEqual(self.banco.lapides(), [])

    def test_dry_run_nao_chama_delete(self):
        script = _arquivo("bin/fechar-semana.py", "fechar_semana")
        shim = Path(self.tmp.name) / "shim"
        shim.mkdir()
        log = Path(self.tmp.name) / "chamadas.log"
        for nome in ("docker", "colima", "claude", "pg_dump", "psql", "curl"):
            f = shim / nome
            f.write_text(f'#!/bin/bash\necho "{nome} $*" >> "{log}"\ncat >> "{log}" 2>/dev/null\nexit 0\n')
            f.chmod(0o755)
        env = dict(os.environ, PATH=f"{shim}:/usr/bin:/bin", HOME=self.tmp.name)
        subprocess.run([sys.executable, str(script), "--dry-run", "--semana", "2026-W45"], cwd=RAIZ, env=env,
                       capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        registro = log.read_text() if log.exists() else ""
        self.assertNotIn("DELETE", registro.upper())


if __name__ == "__main__":
    unittest.main()
