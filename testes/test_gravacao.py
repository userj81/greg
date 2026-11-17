"""Modo de gravação (D-24 de design, pedido 36; Papel, pedido 39): a base única leva o script do <head> que marca o <html>
antes de pintar, a regra que borra o que vem do WhatsApp e o atalho ⌘⇧B; o interruptor vive em /ajustes (role=switch).
Só texto dos templates: a prova de verdade é docs/imagens/gerar.py, que liga o modo, recarrega e fotografa."""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.http import Requisicao  # noqa: E402
from app.paginas import ajustes  # noqa: E402

TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "templates"
BASE = "_base.html"
CHAVE = "localStorage.getItem('vigia-gravacao')"
# O que o borrão tem que cobrir (classes das páginas no Papel).
SELETORES = (".zap-nome", ".zap-previa", ".zap-av", ".zap-cab .t", ".zap-quem", ".zap-txt", ".zap-transcricao",
             ".zap-anexo", ".zap-ia-txt", ".zap-confirma strong", ".item-lista .nome", ".item-lista .texto", ".dm-nome",
             ".dm-titulo", ".citacao", ".item .texto", ".bl-nome", ".fora-lista td:nth-child(2)", ".env-lotes td:nth-child(3)", "pre")


def _base() -> str:
    return (TEMPLATES / BASE).read_text(encoding="utf-8")


class TestModoGravacao(unittest.TestCase):
    def test_so_existe_uma_base(self):
        self.assertEqual(sorted(p.name for p in TEMPLATES.glob("_base*.html")), [BASE])
        self.assertEqual(list(TEMPLATES.glob("tokens-*.css")), [])

    def test_base_leva_script_regra_e_funcao(self):
        s = _base()
        self.assertIn(CHAVE, s)
        self.assertIn(":root[data-gravacao]", s)
        self.assertIn("window.vigiaGravar", s)
        self.assertNotIn("backdrop-filter", s)

    def test_interruptor_fica_em_ajustes(self):
        t = ajustes.ROTAS["/ajustes"](Requisicao("GET", "/ajustes", {}, {}, {}, None, cabecalhos={})).corpo
        self.assertIn('role="switch" id="vigia-gravar" aria-checked="false" aria-pressed="false"', t)

    def test_script_do_cabecalho_vem_antes_do_estilo(self):
        """Marca o <html> antes da primeira pintura: sem isso o conteúdo real aparece nítido por um instante."""
        s = _base()
        self.assertLess(s.index(CHAVE), s.index("<style>"))

    def test_regra_cobre_o_que_vem_do_whatsapp(self):
        s = _base()
        m = re.search(r":root\[data-gravacao\] :is\((.*?)\) \{([^}]*)\}", s, flags=re.S)
        self.assertIsNotNone(m, "regra :root[data-gravacao] :is(...) ausente")
        lista, corpo = m.group(1), m.group(2)
        for sel in SELETORES:
            self.assertIn(sel, lista, sel)
        self.assertIn("blur(", corpo)
        self.assertIn("user-select: none", corpo)

    def test_atalho_e_aviso_na_toolbar(self):
        s = _base()
        self.assertIn("e.key === 'B' || e.key === 'b'", s)
        self.assertIn("⌘⇧B", s)
        self.assertIn('content: "Modo de gravação ligado"', s)


if __name__ == "__main__":
    unittest.main()
