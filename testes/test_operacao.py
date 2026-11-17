"""Testes próprios da estação E11: vigia.sh (lock, pausa, mapeamento de saída), instalador em dry-run, plists.
Quem chama: unittest. O que não faz: não usa rede, não chama launchctl, não toca ~/Library/LaunchAgents.
"""
import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from testes.util_contrato import RAIZ  # noqa: E402


class TestSintaxe(unittest.TestCase):
    def test_bash_n(self):
        scripts = list((RAIZ / "bin").glob("*.sh"))
        self.assertTrue(scripts)
        for s in scripts:
            r = subprocess.run(["bash", "-n", str(s)], capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(r.returncode, 0, f"{s.name}: {r.stderr}")

    def test_plists_sem_segredo_e_com_placeholder(self):
        for p in (RAIZ / "infra/launchagents").glob("*.plist"):
            txt = p.read_text(encoding="utf-8")
            self.assertIn("/REPO_RAIZ", txt)
            d = plistlib.loads(p.read_bytes())
            for k in d.get("EnvironmentVariables", {}):
                for proibido in ("KEY", "TOKEN", "PASSWORD", "SECRET"):
                    self.assertNotIn(proibido, k.upper(), p.name)

    def test_plists_python_homebrew(self):
        for p in (RAIZ / "infra/launchagents").glob("*.plist"):
            d = plistlib.loads(p.read_bytes())
            a0 = d["ProgramArguments"][0]
            self.assertIn(a0, ("/bin/bash", "/opt/homebrew/bin/python3"), p.name)
            self.assertIn("/opt/homebrew/bin", d["EnvironmentVariables"]["PATH"])


class TestPlistsE11(unittest.TestCase):
    def _d(self, nome):
        return plistlib.loads((RAIZ / f"infra/launchagents/io.vigia.{nome}.plist").read_bytes())

    def test_painel_standard_e_p_antes_do_script(self):
        d = self._d("vigia-painel")
        self.assertEqual(d["ProcessType"], "Standard")
        a = d["ProgramArguments"]
        self.assertLess(a.index("-P"), a.index("/REPO_RAIZ/app/server.py"))

    def test_semanais_chamam_vigia_sh_semanal(self):
        self.assertEqual(self._d("vigia-semanal")["ProgramArguments"][1:],
                         ["/REPO_RAIZ/bin/vigia.sh", "semanal"])
        self.assertEqual(self._d("vigia-semanal-limpeza")["ProgramArguments"][1:],
                         ["/REPO_RAIZ/bin/vigia.sh", "semanal", "--executar-limpeza"])


class TestEnvDoVigiaSh(unittest.TestCase):
    """O prelúdio do vigia.sh exporta DONO_WHATSAPP e AVISO_INSTANCIA do .env para o monitor e o aviso (I6; pedido 41:
    sem AVISO_INSTANCIA exportada todo aviso do monitor falhava em silêncio). Valor inseguro é descartado."""

    def _prelúdio(self):
        linhas = (RAIZ / "bin/vigia.sh").read_text(encoding="utf-8").splitlines()
        fim = next(i for i, l in enumerate(linhas) if l.startswith("uso() {"))
        return "\n".join(linhas[:fim]) + "\n"

    CHAVES = ("DONO_WHATSAPP", "AVISO_INSTANCIA", "VIGIA_PORT")

    def _exportadas(self, env_texto: str, extra: dict | None = None) -> dict:
        """Roda o prelúdio num bash limpo (sem as 3 chaves do ambiente de teste) e devolve só essas chaves exportadas."""
        with tempfile.TemporaryDirectory() as d:
            topo = Path(d) / "topo.sh"
            topo.write_text(self._prelúdio(), encoding="utf-8")
            env_arq = Path(d) / "env"
            env_arq.write_text(env_texto, encoding="utf-8")
            amb = {k: v for k, v in os.environ.items() if k not in self.CHAVES}
            amb["VIGIA_ENV"] = str(env_arq)
            amb.update(extra or {})
            r = subprocess.run(["/bin/bash", "-c", f"source {topo} >/dev/null 2>&1; env"], env=amb, capture_output=True, text=True)
            tudo = dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)
            return {k: tudo[k] for k in self.CHAVES if k in tudo}

    def test_exporta_numero_e_instancia_do_env(self):
        e = self._exportadas("DONO_WHATSAPP=5592900000000\nAVISO_INSTANCIA=bot-teste   # comentário\n")
        self.assertEqual((e.get("DONO_WHATSAPP"), e.get("AVISO_INSTANCIA")), ("5592900000000", "bot-teste"))

    def test_valor_inseguro_e_descartado(self):
        e = self._exportadas("DONO_WHATSAPP=55abc\nAVISO_INSTANCIA=bot teste; rm -rf\n")
        self.assertEqual((e.get("DONO_WHATSAPP"), e.get("AVISO_INSTANCIA")), (None, None))

    def test_ambiente_tem_precedencia_sobre_o_env(self):
        e = self._exportadas("AVISO_INSTANCIA=do-arquivo\n", extra={"AVISO_INSTANCIA": "do-ambiente"})
        self.assertEqual(e.get("AVISO_INSTANCIA"), "do-ambiente")


class FakeRepo:
    """Repo mínimo em diretório temporário com coletor/evolution/colima falsos."""

    def __init__(self, tmp: str, rc_coletor=0, evolution_ok=True, monitor_json="", envio_ok=True):
        self.raiz = Path(tmp) / "repo"
        (self.raiz / "bin").mkdir(parents=True)
        (self.raiz / "dados").mkdir()
        shutil.copy(RAIZ / "bin/vigia.sh", self.raiz / "bin/vigia.sh")
        self.chamadas = Path(tmp) / "chamadas.txt"
        self.home = Path(tmp) / "home"
        self.home.mkdir()
        fake = Path(tmp) / "fakebin"
        fake.mkdir()
        self.escrever(fake / "colima", f'echo "colima $*" >> "{self.chamadas}"\nexit 0')
        self.escrever(self.raiz / "bin/evolution.sh",
                      f'echo "evolution $*" >> "{self.chamadas}"\nexit {0 if evolution_ok else 1}')
        self.escrever(Path(tmp) / "coletor.sh", f'echo "coletor" >> "{self.chamadas}"\nexit {rc_coletor}')
        # python falso: registra o módulo chamado (-m app.conexao --monitorar)
        self.escrever(fake / "python3", f'echo "python $*" >> "{self.chamadas}"\nexit 0')
        self.escrever(fake / "caffeinate", f'echo "caffeinate $*" >> "{self.chamadas}"\nexit 0')
        self.monitor_json = monitor_json
        # app.aviso falso: enviar() registra a chamada e devolve o que o arquivo dados/ENVIO_OK mandar
        (self.raiz / "app").mkdir()
        (self.raiz / "app/__init__.py").write_text("")
        (self.raiz / "app/aviso.py").write_text(
            "import os\n"
            "def enviar(texto):\n"
            f"    open({str(self.chamadas)!r}, 'a').write('aviso ' + texto + '\\n')\n"
            "    return os.path.exists(os.path.join(os.path.dirname(__file__), '..', 'dados', 'ENVIO_OK'))\n")
        if envio_ok:
            (self.raiz / "dados/ENVIO_OK").write_text("")
        self.fake = fake
        self.coletor = Path(tmp) / "coletor.sh"
        self.tmp = tmp

    @staticmethod
    def escrever(p: Path, corpo: str):
        p.write_text("#!/bin/bash\n" + corpo + "\n")
        p.chmod(p.stat().st_mode | stat.S_IXUSR)

    def rodar(self, *args):
        env = dict(os.environ, HOME=str(self.home), PATH=f"{self.fake}:/usr/bin:/bin",
                   VIGIA_PYTHON=str(self.fake / "python3"), VIGIA_COLETOR=str(self.coletor))
        # coletor.sh é script bash: o vigia.sh o chama via $PYTHON, então o fake de python executa o arquivo
        self.escrever(self.fake / "python3",
                      f'echo "python $*" >> "{self.chamadas}"\n'
                      f'case "$1" in *.sh) exec /bin/bash "$1";; esac\n'
                      f'if [ "$1" = "-m" ]; then cat <<\'J\'\n{self.monitor_json}\nJ\nfi\n'
                      f'if [ "$1" = "-" ]; then exec "{sys.executable}" -; fi\nexit 0')
        return subprocess.run(["/bin/bash", str(self.raiz / "bin/vigia.sh"), *args], env=env, cwd=self.tmp,
                              capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)

    def log(self) -> str:
        return self.chamadas.read_text() if self.chamadas.exists() else ""


class TestCiclo(unittest.TestCase):
    def test_ciclo_ok_chama_coletor_e_monitor(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = FakeRepo(tmp)
            res = r.rodar()
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("coletor", r.log())
            self.assertIn("-m app.conexao --monitorar", r.log())
            self.assertFalse((r.raiz / "dados/vigia.lock").exists(), "lock deve ser solto")
            self.assertTrue((r.home / "Library/Logs/vigia-whatsapp.log").exists())

    def test_recuperacao_pelo_monitor_le_de_novo_no_mesmo_ciclo(self):   # D-54: ao ligar, recupera
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"VIGIA_ESPERA_RECUPERACAO_S": "0"}):
            r = FakeRepo(tmp, monitor_json='{"evento": "ok", "avisar": false, "recuperacao": "reiniciada"}')
            self.assertEqual(r.rodar().returncode, 0)
            self.assertEqual(r.log().count("coletor\n"), 2)
            self.assertIn("segunda leitura", (r.home / "Library/Logs/vigia-whatsapp.log").read_text())

    def test_sem_recuperacao_le_uma_vez(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = FakeRepo(tmp, monitor_json='{"evento": "ok", "avisar": false, "sonda": "ok"}')
            self.assertEqual(r.rodar().returncode, 0)
            self.assertEqual(r.log().count("coletor\n"), 1)

    def test_fechamento_do_dia_roda_depois_do_coletor_e_so_com_coletor_ok(self):   # D-57 (pedido 45)
        with tempfile.TemporaryDirectory() as tmp:
            r = FakeRepo(tmp)
            self.assertEqual(r.rodar().returncode, 0)
            log = r.log()
            self.assertEqual(log.count("bin/fechar-dia.py"), 1)
            self.assertLess(log.index("coletor\n"), log.index("fechar-dia.py"))
            self.assertLess(log.index("--monitorar"), log.index("fechar-dia.py"))   # depois do monitor (e da 2ª leitura)
        with tempfile.TemporaryDirectory() as tmp:
            r = FakeRepo(tmp, rc_coletor=2)
            self.assertEqual(r.rodar().returncode, 2)
            self.assertNotIn("fechar-dia.py", r.log())
        with tempfile.TemporaryDirectory() as tmp:   # PAUSADO: nem coletor nem fechamento
            r = FakeRepo(tmp)
            (r.raiz / "dados/PAUSADO").write_text("")
            self.assertEqual(r.rodar().returncode, 0)
            self.assertNotIn("fechar-dia.py", r.log())

    def test_pausado_pula_coletor_mas_roda_monitor_sem_colima(self):
        """D-33: Evolution de pé + PAUSADO = só monitor; nunca colima start nem evolution up."""
        with tempfile.TemporaryDirectory() as tmp:
            r = FakeRepo(tmp)
            (r.raiz / "dados/PAUSADO").write_text("")
            self.assertEqual(r.rodar().returncode, 0)
            self.assertIn("--monitorar", r.log())
            for proibido in ("coletor", "colima", "evolution up"):
                self.assertNotIn(proibido, r.log())

    def test_pausado_com_evolution_fora_nao_chama_nada_alem_do_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = FakeRepo(tmp, evolution_ok=False)
            (r.raiz / "dados/PAUSADO").write_text("")
            self.assertEqual(r.rodar().returncode, 0)
            self.assertEqual(r.log().strip(), "evolution status")

    def test_coletor_falhou_exit_1_e_monitor_ainda_roda(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = FakeRepo(tmp, rc_coletor=1)
            self.assertEqual(r.rodar().returncode, 1)
            self.assertIn("--monitorar", r.log())

    def test_coletor_nao_rodou_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = FakeRepo(tmp, rc_coletor=2)
            self.assertEqual(r.rodar().returncode, 2)

    def test_lock_ocupado_sai_2_sem_coletor(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = FakeRepo(tmp)
            (r.raiz / "dados/vigia.lock").mkdir()
            self.assertEqual(r.rodar().returncode, 2)
            self.assertNotIn("coletor", r.log())
            self.assertTrue((r.raiz / "dados/vigia.lock").exists(), "lock alheio não pode ser solto")

    def test_lock_expirado_e_recolhido(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = FakeRepo(tmp)
            lock = r.raiz / "dados/vigia.lock"
            lock.mkdir()
            velho = 1_000_000_000
            os.utime(lock, (velho, velho))
            self.assertEqual(r.rodar().returncode, 0)
            self.assertIn("coletor", r.log())

    def test_lock_de_dono_vivo_nao_e_recolhido_nem_solto(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = FakeRepo(tmp)
            lock = r.raiz / "dados/vigia.lock"
            lock.mkdir()
            dono = subprocess.Popen(["sleep", "30"])
            try:
                (lock / "pid").write_text(str(dono.pid))
                velho = 1_000_000_000
                os.utime(lock, (velho, velho))
                self.assertEqual(r.rodar().returncode, 2)
                self.assertNotIn("coletor", r.log())
                self.assertEqual((lock / "pid").read_text(), str(dono.pid))
            finally:
                dono.kill()
                dono.wait()

    def test_lock_de_dono_morto_e_recolhido_na_hora(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = FakeRepo(tmp)
            lock = r.raiz / "dados/vigia.lock"
            lock.mkdir()
            morto = subprocess.Popen(["true"])
            morto.wait()
            (lock / "pid").write_text(str(morto.pid))
            self.assertEqual(r.rodar().returncode, 0)
            self.assertIn("coletor", r.log())
            self.assertFalse(lock.exists())

    def test_ciclo_segura_o_mac_acordado(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = FakeRepo(tmp)
            r.rodar()
            self.assertRegex(r.log(), r"caffeinate -i -w \d+")

    def test_evolution_fora_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = FakeRepo(tmp, evolution_ok=False)
            src = (r.raiz / "bin/vigia.sh").read_text().replace("ESPERA_EVOLUTION_S=90", "ESPERA_EVOLUTION_S=0")
            (r.raiz / "bin/vigia.sh").write_text(src)
            self.assertEqual(r.rodar().returncode, 2)
            self.assertNotIn("coletor", r.log())

    def test_subcomando_desconhecido_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(FakeRepo(tmp).rodar("xyz").returncode, 2)


class TestSemanal(unittest.TestCase):
    def test_semanal_passa_por_colima_lock_evolution_e_repassa_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = FakeRepo(tmp)
            res = r.rodar("semanal", "--executar-limpeza")
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("colima status", r.log())
            self.assertIn("evolution status", r.log())
            self.assertRegex(r.log(), r"python \S*fechar-semana\.py --executar-limpeza")
            self.assertRegex(r.log(), r"caffeinate -i -w \d+")
            self.assertFalse((r.raiz / "dados/vigia.lock").exists(), "lock solto no fim")

    def test_segurar_e_dry_run_nao_exigem_colima_evolution_nem_lock(self):
        """I1: --segurar só grava dados/segurar-W e --dry-run só lê: chamam o fechar-semana direto."""
        for args, esperado in ((("--segurar", "3"), "fechar-semana.py --segurar 3"), (("--dry-run",), "fechar-semana.py --dry-run")):
            with tempfile.TemporaryDirectory() as tmp:
                r = FakeRepo(tmp, evolution_ok=False)
                (r.raiz / "dados/vigia.lock").mkdir()          # lock alheio ocupado não atrapalha
                res = r.rodar("semanal", *args)
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertIn(esperado, r.log())
                for proibido in ("colima", "evolution", "caffeinate"):
                    self.assertNotIn(proibido, r.log())

    def _repo_com_colima_parado(self, tmp, **kw):
        r = FakeRepo(tmp, **kw)
        # colima status falha: qualquer garantir_colima chamaria `colima start`, que ficaria registrado
        r.escrever(r.fake / "colima", f'echo "colima $*" >> "{r.chamadas}"\n[ "$1" = status ] && exit 1\nexit 0')
        return r

    def test_pausado_com_evolution_fora_semanal_adia_sem_colima_start_nem_up(self):
        """I1: o kill switch (pausar + evolution.sh down) não pode ser desfeito pelo job das 20:05/20:35."""
        for args in ((), ("--executar-limpeza",)):
            with tempfile.TemporaryDirectory() as tmp:
                r = self._repo_com_colima_parado(tmp, evolution_ok=False)
                (r.raiz / "dados/PAUSADO").write_text("")
                res = r.rodar("semanal", *args)
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertNotIn("colima", r.log())
                self.assertNotIn("evolution up", r.log())
                self.assertNotIn("fechar-semana", r.log())
                self.assertIn("PAUSADO e Evolution fora; semanal adiado",
                              (r.home / "Library/Logs/vigia-whatsapp.log").read_text())

    def test_pausado_com_evolution_ok_semanal_roda_sem_colima_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._repo_com_colima_parado(tmp)
            (r.raiz / "dados/PAUSADO").write_text("")
            res = r.rodar("semanal", "--executar-limpeza")
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("fechar-semana.py --executar-limpeza", r.log())
            self.assertNotIn("colima", r.log())
            self.assertNotIn("evolution up", r.log())
            self.assertFalse((r.raiz / "dados/vigia.lock").exists(), "lock solto no fim")

    def test_semanal_roda_mesmo_pausado(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = FakeRepo(tmp)
            (r.raiz / "dados/PAUSADO").write_text("")
            self.assertEqual(r.rodar("semanal", "--executar-limpeza").returncode, 0)
            self.assertIn("fechar-semana.py --executar-limpeza", r.log())

    def test_semanal_evolution_fora_exit_2_sem_fechar(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = FakeRepo(tmp, evolution_ok=False)
            src = (r.raiz / "bin/vigia.sh").read_text().replace("ESPERA_EVOLUTION_S=90", "ESPERA_EVOLUTION_S=0")
            (r.raiz / "bin/vigia.sh").write_text(src)
            self.assertEqual(r.rodar("semanal").returncode, 2)
            self.assertNotIn("fechar-semana", r.log())

    def test_semanal_lock_ocupado_espera_e_sai_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = FakeRepo(tmp)
            (r.raiz / "dados/vigia.lock").mkdir()
            src = (r.raiz / "bin/vigia.sh").read_text().replace("ESPERA_LOCK_S=120", "ESPERA_LOCK_S=0")
            (r.raiz / "bin/vigia.sh").write_text(src)
            self.assertEqual(r.rodar("semanal").returncode, 2)
            self.assertNotIn("fechar-semana", r.log())


class TestLockRobusto(unittest.TestCase):
    def test_dono_de_outro_usuario_eperm_nao_e_lido_como_morto(self):
        """I-EPERM: pid 1 (launchd) existe e `kill -0` dá EPERM para usuário comum: o lock não pode ser recolhido."""
        if os.geteuid() == 0:
            self.skipTest("root: kill -0 não dá EPERM")
        with tempfile.TemporaryDirectory() as tmp:
            r = FakeRepo(tmp)
            lock = r.raiz / "dados/vigia.lock"
            lock.mkdir()
            (lock / "pid").write_text("1")
            res = r.rodar()
            self.assertEqual(res.returncode, 2, res.stderr)      # 2 = não rodou (lock ocupado), como no contrato do script
            self.assertNotIn("coletor", r.log())
            self.assertEqual((lock / "pid").read_text(), "1")
            log = (r.home / "Library/Logs/vigia-whatsapp.log").read_text()
            self.assertIn("outro ciclo em andamento", log)
            self.assertNotIn("recolhendo", log)

    def test_dados_nasce_0700_e_um_existente_0755_e_corrigido(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = FakeRepo(tmp)
            shutil.rmtree(r.raiz / "dados")
            self.assertEqual(r.rodar().returncode, 0)
            self.assertEqual(stat.S_IMODE(os.stat(r.raiz / "dados").st_mode), 0o700)   # os.stat: "stat -f" é BSD
        with tempfile.TemporaryDirectory() as tmp:
            r = FakeRepo(tmp)
            os.chmod(r.raiz / "dados", 0o755)
            self.assertEqual(r.rodar().returncode, 0)
            self.assertEqual(stat.S_IMODE(os.stat(r.raiz / "dados").st_mode), 0o700)


class TestPortaDoEnv(unittest.TestCase):
    def _rodar(self, r, *args):
        with mock.patch.dict(os.environ):
            os.environ.pop("VIGIA_PORT", None)
            return r.rodar(*args)

    def test_status_usa_a_porta_do_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = FakeRepo(tmp)
            (r.raiz / ".env").write_text("EVOLUTION_URL=x\nVIGIA_PORT=8999   # porta\n")
            r.escrever(r.fake / "lsof", f'echo "lsof $*" >> "{r.chamadas}"\nexit 0')
            res = self._rodar(r, "status")
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("-iTCP:8999", r.log())
            self.assertIn("escutando em 8999", res.stdout)

    def test_sem_env_a_porta_e_8120(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = FakeRepo(tmp)
            r.escrever(r.fake / "lsof", f'echo "lsof $*" >> "{r.chamadas}"\nexit 0')
            res = self._rodar(r, "status")
            self.assertIn("-iTCP:8120", r.log())
            self.assertIn("escutando em 8120", res.stdout)

    def test_aviso_do_monitor_leva_a_porta_do_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = FakeRepo(tmp, monitor_json='{"evento":"queda","avisar":true,"motivo":"caiu"}')
            (r.raiz / ".env").write_text("VIGIA_PORT=8999\n")
            self.assertEqual(self._rodar(r).returncode, 0)
            self.assertIn("http://127.0.0.1:8999/conexao", r.log())


class TestAvisoPendente(unittest.TestCase):
    MONITOR = '{"evento":"queda","avisar":true,"motivo":"caiu"}'

    def test_aviso_enviado_nao_deixa_pendente(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = FakeRepo(tmp, monitor_json=self.MONITOR, envio_ok=True)
            self.assertEqual(r.rodar().returncode, 0)
            self.assertIn("aviso Greg: conexão caiu", r.log())
            self.assertFalse((r.raiz / "dados/aviso-pendente.json").exists())

    def test_aviso_falho_grava_pendente_e_proximo_ciclo_reenvia(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = FakeRepo(tmp, monitor_json=self.MONITOR, envio_ok=False)
            self.assertEqual(r.rodar().returncode, 0, "falha no aviso não derruba o ciclo")
            pend = r.raiz / "dados/aviso-pendente.json"
            d = json.loads(pend.read_text())
            self.assertEqual((d["evento"], d["motivo"]), ("queda", "caiu"))
            self.assertIsInstance(d["criado_em"], int)
            self.assertEqual(stat.S_IMODE(pend.stat().st_mode), 0o600)
            # ciclo seguinte: monitor sem aviso novo, envio volta a funcionar -> reenvia e apaga
            (r.raiz / "dados/ENVIO_OK").write_text("")
            r.monitor_json = '{"evento":"ok","avisar":false}'
            r.chamadas.write_text("")
            self.assertEqual(r.rodar().returncode, 0)
            self.assertIn("aviso Greg: conexão caiu", r.log())
            self.assertFalse(pend.exists())

    def test_pendente_de_problema_que_ja_passou_avisa_que_voltou(self):   # pedido 43: "caiu" das 14:40 chegou às 16:14
        with tempfile.TemporaryDirectory() as tmp:
            r = FakeRepo(tmp, monitor_json='{"evento":"ok","avisar":false}', envio_ok=True)
            criado = 1794248438                    # 09/11/2026 14:40:38 local
            (r.raiz / "dados/aviso-pendente.json").write_text(
                json.dumps({"evento": "queda", "motivo": "caiu", "criado_em": criado}))
            self.assertEqual(r.rodar().returncode, 0)
            hora = time.strftime("%H:%M", time.localtime(criado))
            self.assertIn(f"aviso Greg: conexão caiu às {hora}, já voltou · http://127.0.0.1:", r.log())
            self.assertFalse((r.raiz / "dados/aviso-pendente.json").exists())

    def test_pendente_de_sonda_resolvido_vira_sem_resposta_e_monitor_fora_do_ok_reenvia_como_antes(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = FakeRepo(tmp, monitor_json='{"evento":"ok","avisar":false}', envio_ok=True)
            (r.raiz / "dados/aviso-pendente.json").write_text(
                json.dumps({"evento": "socket_zumbi", "motivo": "sonda_sem_foto_persistente", "criado_em": 1794250538}))
            self.assertEqual(r.rodar().returncode, 0)
            self.assertIn("aviso Greg: conexão sem resposta às ", r.log())
            self.assertIn(", já voltou · ", r.log())
        with tempfile.TemporaryDirectory() as tmp:   # monitor ainda em queda: o pendente sai com a frase de sempre
            r = FakeRepo(tmp, monitor_json='{"evento":"queda","avisar":false,"motivo":"continua_fora"}', envio_ok=True)
            (r.raiz / "dados/aviso-pendente.json").write_text(
                json.dumps({"evento": "queda", "motivo": "caiu", "criado_em": 1794248438}))
            self.assertEqual(r.rodar().returncode, 0)
            self.assertIn("aviso Greg: conexão caiu · http://127.0.0.1:", r.log())
            self.assertNotIn("já voltou", r.log())

    def test_pendente_persiste_se_reenvio_falha(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = FakeRepo(tmp, monitor_json='{"evento":"ok","avisar":false}', envio_ok=False)
            (r.raiz / "dados/aviso-pendente.json").write_text(
                json.dumps({"evento": "queda", "motivo": "caiu", "criado_em": 1}))
            self.assertEqual(r.rodar().returncode, 0)
            self.assertTrue((r.raiz / "dados/aviso-pendente.json").exists())


class TestInstalador(unittest.TestCase):
    def _rodar(self, *args):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, VIGIA_LAUNCHAGENTS_DIR=tmp)
            r = subprocess.run(["bash", str(RAIZ / "bin/instalar-launchagent.sh"), *args], env=env,
                               capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
            return r, list(Path(tmp).iterdir())

    def test_sem_sim_nao_muda_nada(self):
        for args in ((), ("--dry-run",), ("--remover",)):
            r, arquivos = self._rodar(*args)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(arquivos, [])
            self.assertIn("[dry-run]", r.stdout)

    def test_dry_run_menciona_bootstrap_e_caminho(self):
        r, _ = self._rodar("--dry-run")
        self.assertIn("launchctl bootstrap gui/", r.stdout)
        self.assertIn(str(RAIZ), r.stdout)

    def _copia_repo(self, base: Path, nome: str, com_server=True) -> Path:
        raiz = base / nome
        (raiz / "bin").mkdir(parents=True)
        shutil.copy(RAIZ / "bin/instalar-launchagent.sh", raiz / "bin")
        shutil.copytree(RAIZ / "infra/launchagents", raiz / "infra/launchagents")
        if com_server:
            (raiz / "app").mkdir()
            (raiz / "app/server.py").write_text("")
        return raiz

    def test_recusa_caminho_com_caractere_do_sed(self):
        for nome in ("a|b", "a&b", "a<b", "a>b"):
            with tempfile.TemporaryDirectory() as tmp:
                raiz = self._copia_repo(Path(tmp), nome)
                r = subprocess.run(["bash", str(raiz / "bin/instalar-launchagent.sh"), "--dry-run"],
                                   capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
                                   env=dict(os.environ, VIGIA_LAUNCHAGENTS_DIR=str(Path(tmp) / "la")))
                self.assertEqual(r.returncode, 2, nome)
                self.assertNotIn("[dry-run]", r.stdout)

    def test_recusa_painel_sem_server_py_e_segue_com_os_outros(self):
        with tempfile.TemporaryDirectory() as tmp:
            raiz = self._copia_repo(Path(tmp), "repo", com_server=False)
            r = subprocess.run(["bash", str(raiz / "bin/instalar-launchagent.sh"), "--dry-run"],
                               capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
                               env=dict(os.environ, VIGIA_LAUNCHAGENTS_DIR=str(Path(tmp) / "la")))
            self.assertEqual(r.returncode, 1)
            self.assertIn("RECUSADO io.vigia.vigia-painel", r.stderr)
            for outro in ("vigia-whatsapp", "vigia-semanal-limpeza"):
                self.assertIn(f"== instalando io.vigia.{outro}", r.stdout)
            self.assertFalse((Path(tmp) / "la").exists(), "dry-run não escreve nada")

    def test_opcao_invalida_exit_2(self):
        r, _ = self._rodar("--bobagem")
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()


class TestAvisoEntregaTravada(unittest.TestCase):
    """D-56 (pedido 44): os motivos do canário viram frase no aviso e "entrega travada" no pendente resolvido."""

    def test_motivos_do_canario_viram_frase(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = FakeRepo(tmp, monitor_json='{"evento":"entrega_travada","avisar":true,"motivo":"entrega_travada_reiniciada"}',
                         envio_ok=True)
            self.assertEqual(r.rodar().returncode, 0)
            self.assertIn("aviso Greg: conexão aberta, mas a entrega travou: o teste do bot não chegou nem depois do "
                          "reinício automático; use Parear de novo · http://127.0.0.1:", r.log())
        with tempfile.TemporaryDirectory() as tmp:
            r = FakeRepo(tmp, monitor_json='{"evento":"entrega_travada","avisar":true,"motivo":"entrega_travada_reinicio_recente"}',
                         envio_ok=True)
            self.assertEqual(r.rodar().returncode, 0)
            self.assertIn("houve reinício há pouco; use Parear de novo", r.log())

    def test_pendente_de_entrega_travada_resolvido_diz_entrega_travada_ja_voltou(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = FakeRepo(tmp, monitor_json='{"evento":"ok","avisar":false,"canario":"chegou"}', envio_ok=True)
            (r.raiz / "dados/aviso-pendente.json").write_text(
                json.dumps({"evento": "entrega_travada", "motivo": "entrega_travada_reiniciada", "criado_em": 1794250538}))
            self.assertEqual(r.rodar().returncode, 0)
            self.assertIn("aviso Greg: conexão entrega travada às ", r.log())
            self.assertIn(", já voltou · ", r.log())
