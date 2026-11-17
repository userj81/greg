"""Trava do PyAV na imagem Docker (achado #420 do mural da gerência, 16/11/2026): o faster-whisper 1.1.1 chama
av.open(..., metadata_errors=...), argumento que o PyAV 19 tirou. Sem a fixação o pip puxa o PyAV 19.x, todo áudio falha
com TypeError dentro do motor (bin/motor-transcricao.py pula o arquivo) e o Greg em Docker fica sem transcrição.
Quem chama: unittest. Lê só o Dockerfile da raiz; a prova com áudio de verdade é na construção da imagem (a conferência
com metadata_errors que o próprio Dockerfile roda) e no contêiner.
"""
import re
import unittest
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"


def _pip_da_transcricao(texto: str) -> str:
    """A linha do `pip install` do bloco de transcrição (a que instala o faster-whisper)."""
    linhas = [ln for ln in texto.splitlines() if "pip install" in ln and "faster-whisper" in ln]
    if len(linhas) != 1:
        raise AssertionError(f"esperava um pip install com faster-whisper no Dockerfile, achei {len(linhas)}")
    return linhas[0]


class TestPyAvAbaixoDo19(unittest.TestCase):
    def setUp(self):
        self.texto = DOCKERFILE.read_text(encoding="utf-8")
        self.pip = _pip_da_transcricao(self.texto)

    def test_faster_whisper_continua_no_1_1_1(self):
        # Se o faster-whisper mudar de versão, a trava do PyAV precisa ser revista junto (este teste avisa).
        self.assertIn('"faster-whisper==1.1.1"', self.pip)

    def test_av_fixado_com_teto_abaixo_do_19(self):
        specs = re.findall(r'"av([<>=!~][^"]*)"', self.pip)
        self.assertEqual(len(specs), 1, f"o pip install da transcrição precisa fixar o av: {self.pip.strip()}")
        tetos = [int(m) for m in re.findall(r"<\s*(\d+)", specs[0])]
        self.assertTrue(tetos, f"o av precisa de teto (<19): {specs[0]}")
        self.assertLessEqual(min(tetos), 19, f"PyAV 19 tirou metadata_errors; teto atual: {specs[0]}")
        self.assertNotRegex(specs[0], r"==\s*(19|[2-9]\d)")

    def test_construcao_confere_metadata_errors(self):
        i = self.texto.index(self.pip)
        depois = self.texto[i:i + 800]
        self.assertIn('metadata_errors=\\"ignore\\"', depois)
        self.assertIn("except TypeError", depois)
        self.assertIn("sys.exit", depois)


if __name__ == "__main__":
    unittest.main()
