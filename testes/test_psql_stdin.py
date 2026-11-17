"""A query SQL do Greg carrega jid/número de conversa e vai SEMPRE pelo stdin do evolution.sh (nunca em argv/ps).
Cobre lotes.psql_via_script e os três chamadores: painel (app/whatsapp.psql_ro), coletor e fechar-semana.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from app import lotes  # noqa: E402
from app import whatsapp as wa  # noqa: E402
from testes.util_contrato import _importar_arquivo  # noqa: E402

JID = "5592900001111@s.whatsapp.net"
QUERY = f"SELECT m.id FROM public.\"Message\" m WHERE m.\"key\"->>'remoteJid' = '{JID}'"


class TestPsqlPeloStdin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name)
        self.args, self.entrada = t / "args.txt", t / "entrada.txt"
        self.script = t / "evolution-falso.sh"
        self.script.write_text(f'#!/bin/bash\nprintf "%s " "$@" > "{self.args}"\ncat > "{self.entrada}"\n'
                               f'[ "${{FALHAR:-}}" = 1 ] && exit 3\nprintf "a\\tb\\nc\\td\\n"\n')
        self.script.chmod(0o700)

    def _prova(self, subcomando_esperado: str):
        self.assertEqual(self.args.read_text().strip(), subcomando_esperado)
        self.assertEqual(self.entrada.read_text(encoding="utf-8"), QUERY)
        self.assertNotIn(JID, self.args.read_text())

    def test_lotes_psql_via_script_manda_a_query_pelo_stdin_e_parseia_tab(self):
        psql = lotes.psql_via_script(self.script, "psql-ro-stdin", 10)
        self.assertEqual(psql(QUERY), [["a", "b"], ["c", "d"]])
        self._prova("psql-ro-stdin")

    def test_recusa_subcomando_que_le_a_query_do_argv(self):
        for ruim in ("psql-ro", "psql-limpeza", "sql"):
            with self.assertRaises(ValueError, msg=ruim):
                lotes.psql_via_script(self.script, ruim, 10)

    def test_codigo_diferente_de_zero_vira_postgres_indisponivel_sem_ecoar_a_query(self):
        os.environ["FALHAR"] = "1"
        self.addCleanup(os.environ.pop, "FALHAR", None)
        with self.assertRaises(lotes.PostgresIndisponivel) as cm:
            lotes.psql_via_script(self.script, "psql-ro-stdin", 10)(QUERY)
        self.assertIn("código 3", str(cm.exception))
        self.assertNotIn(JID, str(cm.exception))

    def test_painel_coletor_e_fechar_semana_usam_a_variante_stdin(self):
        coletor = _importar_arquivo("bin/coletor.py", "coletor")
        fs = _importar_arquivo("bin/fechar-semana.py", "fechar_semana")
        casos = [
            (wa.psql_ro(self.script), "psql-ro-stdin"),
            (coletor.psql_ro_via_script(self.script), "psql-ro-stdin"),
            (fs.psql_via_script("psql-ro", self.script), "psql-ro-stdin"),            # nome antigo é traduzido
            (fs.psql_via_script("psql-ro-stdin", self.script), "psql-ro-stdin"),
            (fs.psql_via_script("psql-limpeza", self.script), "psql-limpeza-stdin"),
            (fs.psql_via_script("sql-stdin", self.script), "sql-stdin"),
        ]
        for psql, esperado in casos:
            self.assertEqual(psql(QUERY), [["a", "b"], ["c", "d"]])
            self._prova(esperado)

    def test_nenhum_modulo_chama_psql_ro_ou_psql_limpeza_com_a_query_em_argv(self):
        """Regra da casa (CLAUDE.md): nada de dado em argv. Qualquer `"psql-ro", query` ou `"psql-limpeza", query` em
        subprocess é regressão."""
        for arq in list((RAIZ / "app").rglob("*.py")) + list((RAIZ / "bin").glob("*.py")):
            codigo = arq.read_text(encoding="utf-8")
            self.assertNotRegex(codigo, r'"psql-(ro|limpeza)",\s*\w', arq.name)


if __name__ == "__main__":
    unittest.main()
