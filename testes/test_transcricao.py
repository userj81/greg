"""Pedido 30 (D-47): transcrição automática de áudio com motor local (app/transcricao.py).
Quem chama: unittest. O motor é um fake que escreve <stem>.txt na --output-dir; a Evolution é um fake de método;
o banco é o SQLite real num diretório temporário. Nada de rede, nada de mlx_whisper de verdade.
"""
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import transcricao as tr  # noqa: E402
from app.banco import TRANSCRICAO_MAX, Banco  # noqa: E402
from app.erros import EvolutionIndisponivel, MidiaInvalida, NaoAutorizado  # noqa: E402

PAREADO = 1_700_000_000
ANA = "5592980000001@s.whatsapp.net"
G1 = "120363000000000001@g.us"


def _audio(i, ts, seg, *, jid=ANA, de_mim=False, alt=None, tipo="audioMessage", msg=None):
    key = {"id": f"3EB{i:04d}", "remoteJid": jid, "fromMe": de_mim}
    if alt:
        key["remoteJidAlt"] = alt
    return {"seq": i, "id": f"a{i}", "key": key, "pushName": "Ana", "messageType": tipo,
            "message": msg if msg is not None else {"audioMessage": {"mimetype": "audio/ogg; codecs=opus", "seconds": seg, "ptt": True}},
            "messageTimestamp": ts}


def _texto(i, ts):
    return {"seq": i, "id": f"t{i}", "key": {"id": f"T{i}", "remoteJid": ANA, "fromMe": False}, "pushName": "Ana",
            "messageType": "conversation", "message": {"conversation": "oi"}, "messageTimestamp": ts}


class MotorFalso:
    """Imita o mlx_whisper: para cada áudio em argv escreve <stem>.txt em --output-dir (texto "fala de <stem>" ou o de
    `textos`). `codigo` != 0, `excecao` e `sem_txt` simulam as falhas."""

    def __init__(self, textos=None, codigo=0, excecao=None, sem_txt=()):
        self.textos = dict(textos or {})
        self.codigo = codigo
        self.excecao = excecao
        self.sem_txt = set(sem_txt)
        self.chamadas = []

    def __call__(self, cmd, **kw):
        self.chamadas.append((list(cmd), kw))
        if self.excecao:
            raise self.excecao
        saida = Path(cmd[cmd.index("--output-dir") + 1])
        for a in cmd[5:cmd.index("--model")]:      # nice -n 15 <python> <runner> arquivos…
            stem = Path(a).stem
            if stem not in self.sem_txt:
                (saida / f"{stem}.txt").write_text(self.textos.get(stem, f"fala de {stem}\n"), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, self.codigo, "", "")


class EvoAudio:
    def __init__(self, falhar=None):
        self.chamadas = []
        self.falhar = falhar

    def midia_audio(self, key):
        self.chamadas.append(key)
        if self.falhar:
            raise self.falhar
        return b"OggS" + b"\x00" * 40, "ogg"


class TestCandidatosEPlano(unittest.TestCase):
    def test_candidatos_filtram_e_ordenam_por_chegada(self):
        linhas = [
            _texto(1, PAREADO + 10),
            _audio(2, PAREADO + 50, 30),                                   # entra
            _audio(3, PAREADO - 5, 30),                                    # antes do pareamento (R-2)
            _audio(4, PAREADO + 20, 30, jid="status@broadcast"),           # status
            _audio(5, PAREADO + 30, 30, jid="111@s.whatsapp.net"),         # bloqueado pelo jid
            _audio(6, PAREADO + 40, 30, jid="222@lid", alt="333@s.whatsapp.net"),   # bloqueado pelo par em número
            _audio(7, PAREADO + 15, 30, jid=G1),                           # grupo: entra só com grupos=True
            _audio(8, PAREADO + 60, 30, de_mim=True),                      # do próprio dono: entra
            _audio(9, PAREADO + 70, 30),                                   # já ok
            _audio(10, PAREADO + 80, 30),                                  # falhou 1x: tenta de novo
            _audio(11, PAREADO + 90, 30),                                  # falhou 3x: desiste
            dict(_audio(2, PAREADO + 50, 30), seq=99),                     # id repetido
        ]
        existentes = {"a9": {"estado": "ok", "tentativas": 1}, "a10": {"estado": "falhou", "tentativas": 1},
                      "a11": {"estado": "falhou", "tentativas": 3}}
        bloq = frozenset({"111@s.whatsapp.net", "333@s.whatsapp.net"})
        ids = [l["id"] for l in tr.candidatos(linhas, desde=PAREADO, bloqueados=bloq, grupos=True,
                                              existentes=existentes, tentativas=3)]
        self.assertEqual(ids, ["a7", "a2", "a8", "a10"])
        ids = [l["id"] for l in tr.candidatos(linhas, desde=PAREADO, bloqueados=bloq, grupos=False,
                                              existentes=existentes, tentativas=3)]
        self.assertEqual(ids, ["a2", "a8", "a10"])

    def test_audio_embrulhado_e_ptt_contam_como_audio(self):
        emb = _audio(1, PAREADO + 1, 9, msg={"ephemeralMessage": {"message": {"audioMessage": {"seconds": 9}}}})
        self.assertTrue(tr.eh_audio(emb))
        self.assertEqual(tr.segundos_de(emb), 9)
        self.assertFalse(tr.eh_audio(_texto(2, PAREADO + 2)))

    def test_planejar_tetos_corte_e_curtos(self):
        cands = [_audio(1, PAREADO + 1, 1), _audio(2, PAREADO + 2, 30), _audio(3, PAREADO + 3, 700),
                 _audio(4, PAREADO + 4, 400), _audio(5, PAREADO + 5, 500), _audio(6, PAREADO + 6, 20)]
        p = tr.planejar(cands, teto_ciclo=4, teto_segundos_ciclo=900, min_segundos=2, max_segundos_audio=600)
        self.assertEqual([l["id"] for l in p["pulados"]], ["a1"])
        self.assertEqual([(f["linha"]["id"], f["segundos"], f["clip"]) for f in p["fazer"]],
                         [("a2", 30, None), ("a3", 700, 600), ("a6", 20, None)])
        self.assertEqual([l["id"] for l in p["adiados"]], ["a4", "a5"])
        self.assertEqual(p["segundos"], 650)
        p = tr.planejar(cands, teto_ciclo=1, teto_segundos_ciclo=900, min_segundos=2, max_segundos_audio=600)
        self.assertEqual(([f["linha"]["id"] for f in p["fazer"]], len(p["adiados"])), (["a2"], 4))
        # o primeiro sempre cabe, mesmo que passe do teto de segundos (senão nunca sairia da fila)
        p = tr.planejar([_audio(3, PAREADO + 3, 700)], teto_ciclo=5, teto_segundos_ciclo=100, min_segundos=2,
                        max_segundos_audio=600)
        self.assertEqual([f["clip"] for f in p["fazer"]], [600])

    def test_comando_so_caminhos_e_opcoes_e_ambiente_offline(self):
        cmd = tr.comando("/x/python3", ["/t/a1.ogg", "/t/a2.ogg"], modelo="m/large", idioma="pt", saida=Path("/t"),
                         clip_s=600, nice=15)
        self.assertEqual(cmd[:7], ["/usr/bin/nice", "-n", "15", "/x/python3", str(tr.RUNNER), "/t/a1.ogg", "/t/a2.ogg"])
        self.assertTrue(tr.RUNNER.is_file() and os.access(tr.RUNNER, os.X_OK))
        for par in (("--model", "m/large"), ("--language", "pt"), ("--output-dir", "/t"), ("--clip-s", "600")):
            self.assertEqual(cmd[cmd.index(par[0]) + 1], par[1])
        self.assertNotIn("--clip-s", tr.comando("/x/m", ["/t/a.ogg"], modelo="m", idioma="pt", saida=Path("/t")))
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "s", "CLAUDE_CODE_X": "s", "VIGIA_TOKEN": "s", "HOME": "/h"}):
            env = tr.ambiente()
        self.assertEqual(env["HF_HUB_OFFLINE"], "1")
        self.assertEqual(env["HOME"], "/h")
        self.assertIn("/opt/homebrew/bin", env["PATH"])
        self.assertFalse({k for k in env if k.startswith(("ANTHROPIC", "CLAUDE", "VIGIA"))})

    def test_achar_motor_respeita_env_path_e_shebang_do_cli(self):
        with mock.patch.dict(os.environ, {"VIGIA_WHISPER": "/meu/python"}):
            self.assertEqual(tr.achar_motor(), "/meu/python")
        with mock.patch.dict(os.environ, {"VIGIA_WHISPER": "", "PATH": "/nada"}), \
                mock.patch("shutil.which", return_value=None) as w:
            self.assertIsNone(tr.achar_motor())
        self.assertIn("/opt/homebrew/bin", w.call_args[1]["path"])
        with tempfile.TemporaryDirectory() as d:
            cli = Path(d) / "mlx_whisper"
            cli.write_text(f"#!{sys.executable}\nimport x\n")
            with mock.patch.dict(os.environ, {"VIGIA_WHISPER": ""}), mock.patch("shutil.which", return_value=str(cli)):
                self.assertEqual(tr.achar_motor(), sys.executable)        # o interpretador do CLI, não o CLI
            cli.write_text("#!/nao/existe/python\n")
            with mock.patch.dict(os.environ, {"VIGIA_WHISPER": ""}), mock.patch("shutil.which", return_value=str(cli)):
                self.assertEqual(tr.achar_motor(), tr.PYTHON_PADRAO)


class TestTranscritor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dados = Path(self.tmp.name)
        self.banco = Banco(str(self.dados / "vigia.db"))
        self.pasta = self.dados / "caixa" / ".audio"
        self.evo = EvoAudio()
        self.motor = MotorFalso()
        self.relogio = [0.0]
        self.t = self.novo()

    def novo(self, cfg=None, **kw):
        def agora():
            self.relogio[0] += 0.5
            return self.relogio[0]
        args = dict(motor="/fake/mlx_whisper", executor=self.motor, agora=agora)
        args.update(kw)
        return tr.Transcritor(cfg, self.pasta, **args)

    def rodar(self, linhas, t=None, bloqueados=()):
        return (t or self.t).rodar(linhas, obter_banco=lambda: self.banco, obter_evo=lambda: self.evo,
                                   desde=PAREADO, bloqueados=frozenset(bloqueados))

    def test_ok_cortado_pulado_contagens_e_pasta_limpa(self):
        r = self.rodar([_texto(1, PAREADO + 1), _audio(2, PAREADO + 2, 40), _audio(3, PAREADO + 3, 1),
                        _audio(4, PAREADO + 4, 700)])
        self.assertEqual({k: r[k] for k in ("audios", "transcritos", "cortados", "falhas", "pulados", "adiados", "segundos")},
                         {"audios": 3, "transcritos": 2, "cortados": 1, "falhas": 0, "pulados": 1, "adiados": 0, "segundos": 640})
        self.assertEqual(r["motor_ms"], 1000)                      # dois processos de 500 ms no relógio falso
        self.assertEqual(len(self.motor.chamadas), 2)              # normais juntos; o longo em processo próprio
        normais, longo = self.motor.chamadas[0][0], self.motor.chamadas[1][0]
        self.assertNotIn("--clip-s", normais)
        self.assertEqual(longo[longo.index("--clip-s") + 1], "600")
        self.assertTrue(all(kw["start_new_session"] and kw["env"]["HF_HUB_OFFLINE"] == "1" for _, kw in self.motor.chamadas))
        t = self.banco.transcricoes(["a2", "a3", "a4"])
        self.assertEqual({k: (v["estado"], v["segundos"], v["segundos_lidos"]) for k, v in t.items()},
                         {"a2": ("ok", 40, 40), "a3": ("pulado", 1, 0), "a4": ("cortado", 700, 600)})
        self.assertEqual((t["a2"]["texto"], t["a2"]["conversa"], t["a2"]["ts"], t["a2"]["motor"]),
                         ("fala de a2", ANA, PAREADO + 2, "mlx_whisper/whisper-large-v3-turbo"))
        self.assertEqual(t["a4"]["tempo_ms"], 500)
        self.assertEqual(sorted(p.name for p in self.pasta.iterdir()), [])     # áudio cru e .txt não ficam
        self.assertEqual(oct(self.pasta.stat().st_mode)[-3:], "700")
        self.assertEqual(len(self.evo.chamadas), 2)                # o pulado não é baixado
        # rodar de novo: nada a fazer, nem banco nem Evolution são tocados pelo que já está feito
        r2 = self.rodar([_audio(2, PAREADO + 2, 40), _audio(3, PAREADO + 3, 1), _audio(4, PAREADO + 4, 700)])
        self.assertEqual((r2["audios"], len(self.evo.chamadas), len(self.motor.chamadas)), (0, 2, 2))

    def test_motor_falhando_vira_falhou_e_tenta_ate_o_teto(self):
        self.motor.codigo = 1
        for n in (1, 2, 3):
            r = self.rodar([_audio(2, PAREADO + 2, 40)])
            self.assertEqual((r["audios"], r["falhas"], r["transcritos"]), (1, 1, 0))
            self.assertEqual(self.banco.transcricoes(["a2"])["a2"]["tentativas"], n)
        self.motor.codigo = 0
        r = self.rodar([_audio(2, PAREADO + 2, 40)])
        self.assertEqual((r["audios"], len(self.motor.chamadas)), (0, 3))   # desistiu depois de 3
        self.motor.excecao = subprocess.TimeoutExpired(cmd="x", timeout=1)
        r = self.rodar([_audio(5, PAREADO + 5, 40)])
        self.assertEqual((r["falhas"], self.banco.transcricoes(["a5"])["a5"]["estado"]), (1, "falhou"))
        self.motor.excecao = OSError("sem motor")
        r = self.rodar([_audio(6, PAREADO + 6, 40)])
        self.assertEqual(r["falhas"], 1)
        self.assertEqual(sorted(p.name for p in self.pasta.iterdir()), [])

    def test_txt_ausente_so_derruba_aquele_audio(self):
        self.motor.sem_txt = {"a3"}
        r = self.rodar([_audio(2, PAREADO + 2, 10), _audio(3, PAREADO + 3, 10)])
        self.assertEqual((r["transcritos"], r["falhas"]), (1, 1))
        t = self.banco.transcricoes(["a2", "a3"])
        self.assertEqual((t["a2"]["estado"], t["a3"]["estado"]), ("ok", "falhou"))

    def test_midia_invalida_vira_falhou_e_evolution_fora_sobe(self):
        self.evo.falhar = MidiaInvalida("x")
        r = self.rodar([_audio(2, PAREADO + 2, 10)])
        self.assertEqual((r["falhas"], len(self.motor.chamadas)), (1, 0))
        for exc in (EvolutionIndisponivel("fora"), NaoAutorizado("401")):
            self.evo.falhar = exc
            with self.assertRaises(type(exc)):
                self.rodar([_audio(3, PAREADO + 3, 10)])
            self.assertEqual(self.banco.transcricoes(["a3"]), {})
            self.assertEqual(sorted(p.name for p in self.pasta.iterdir()), [])

    def test_texto_do_motor_e_normalizado_e_cortado(self):
        self.motor.textos = {"a2": "  olá \n\n Dono,\tvamos?  ", "a3": "x" * (tr.TEXTO_MAX + 50)}
        self.rodar([_audio(2, PAREADO + 2, 10), _audio(3, PAREADO + 3, 10)])
        t = self.banco.transcricoes(["a2", "a3"])
        self.assertEqual(t["a2"]["texto"], "olá Dono, vamos?")
        self.assertEqual(len(t["a3"]["texto"]), min(tr.TEXTO_MAX, TRANSCRICAO_MAX))

    def test_desligado_sem_motor_bloqueado_ou_sem_audio_nao_toca_nada(self):
        t = self.novo({"ativo": False})
        self.assertFalse(t.ativo())
        self.assertEqual(self.rodar([_audio(2, PAREADO + 2, 10)], t)["audios"], 0)
        with mock.patch("shutil.which", return_value=None), mock.patch.dict(os.environ, {"VIGIA_WHISPER": ""}):
            self.assertFalse(self.novo({}, motor=None).ativo())
        self.assertEqual(self.rodar([_audio(2, PAREADO + 2, 10)], bloqueados=[ANA])["audios"], 0)
        explode = lambda: (_ for _ in ()).throw(AssertionError("não devia abrir"))  # noqa: E731
        r = self.t.rodar([_texto(1, PAREADO + 1)], obter_banco=explode, obter_evo=explode, desde=PAREADO)
        self.assertEqual(r["audios"], 0)
        self.assertEqual((len(self.evo.chamadas), len(self.motor.chamadas)), (0, 0))

    def test_tetos_do_config_e_adiados(self):
        t = self.novo({"teto_ciclo": 1})
        r = self.rodar([_audio(2, PAREADO + 2, 10), _audio(3, PAREADO + 3, 10)], t)
        self.assertEqual((r["transcritos"], r["adiados"]), (1, 1))
        self.assertEqual(set(self.banco.transcricoes(["a2", "a3"])), {"a2"})   # adiado não ganha linha: volta depois

    def test_texto_nunca_vai_para_o_log(self):
        self.motor.textos = {"a2": "segredo falado em voz alta"}
        self.motor.sem_txt = {"a3"}
        with self.assertLogs("vigia.transcricao", level="INFO") as cm:
            self.rodar([_audio(2, PAREADO + 2, 10), _audio(3, PAREADO + 3, 10)])
        self.assertTrue(cm.output)
        self.assertNotIn("segredo", "\n".join(cm.output))
        logging.getLogger("vigia.transcricao").handlers.clear()


if __name__ == "__main__":
    unittest.main()
