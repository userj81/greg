"""Pedido 51: áudio que não sai cortado, selo de encaminhada, status de entrega da mensagem própria e reações com "+"."""
import struct
import unittest

from app import whatsapp as wa
from app.paginas import conversas


def _caixa(nome: bytes, corpo: bytes = b"") -> bytes:
    return struct.pack(">I", 8 + len(corpo)) + nome + corpo


class Audio(unittest.TestCase):
    def test_mp4_fragmentado_e_reconhecido(self):
        frag = _caixa(b"ftyp", b"isom" * 2) + _caixa(b"moov", b"x" * 40) + _caixa(b"moof", b"y" * 16) + _caixa(b"mdat", b"z" * 64)
        comum = _caixa(b"ftyp", b"M4A " * 2) + _caixa(b"moov", b"x" * 40) + _caixa(b"mdat", b"z" * 64)
        self.assertTrue(conversas._mp4_fragmentado(frag))
        self.assertFalse(conversas._mp4_fragmentado(comum))
        self.assertFalse(conversas._mp4_fragmentado(b"RIFF\x00\x00\x00\x00WAVEfmt "))
        self.assertFalse(conversas._mp4_fragmentado(b"\x00\x00\x00\x00ftyp"))      # caixa de tamanho inválido não trava

    def test_a_pagina_converte_para_wav_e_confere_a_duracao(self):
        js = conversas._JS
        self.assertIn("function paraWav(blob)", js)
        self.assertIn("O áudio saiu incompleto", js)
        self.assertIn("enviarMidia(r.wav, 'voz'", js)


class Encaminhada(unittest.TestCase):
    def test_le_o_selo_do_conteudo(self):
        self.assertTrue(wa.encaminhada({"extendedTextMessage": {"text": "oi", "contextInfo": {"isForwarded": True}}}))
        self.assertTrue(wa.encaminhada({"audioMessage": {"seconds": 3, "contextInfo": {"isForwarded": True, "forwardingScore": 1}}}))
        self.assertFalse(wa.encaminhada({"conversation": "oi"}))
        self.assertFalse(wa.encaminhada({"extendedTextMessage": {"text": "oi", "contextInfo": {"stanzaId": "x"}}}))
        self.assertFalse(wa.encaminhada({}))

    def test_a_bolha_mostra_o_selo(self):
        base = {"id": "m1", "key": {"id": "m1", "remoteJid": "5511999990000@s.whatsapp.net"}, "push": "Ana", "tipo_msg": "conversation",
                "message": {"conversation": "oi"}, "ts": 1794440000, "status": "", "citada": None, "apagada": False}
        com = conversas.html_mensagens([dict(base, encaminhada=True)], "5511999990000@s.whatsapp.net", {}, {})
        sem = conversas.html_mensagens([dict(base, encaminhada=False)], "5511999990000@s.whatsapp.net", {}, {})
        self.assertIn("zap-encaminhada", com)
        self.assertIn("Encaminhada", com)
        self.assertNotIn("zap-encaminhada", sem)


class Entrega(unittest.TestCase):
    def test_as_consultas_leem_o_nivel_anotado(self):
        for sql in (wa.sql_lista(0), wa.sql_mensagens("5511999990000@s.whatsapp.net", 0)):
            self.assertIn("LEFT JOIN vigia.entrega en ON en.message_id = m.id", sql)
            self.assertIn("en.nivel >", sql)

    def test_tiques(self):
        self.assertIn("saindo", conversas._ticks("PENDING"))
        self.assertIn("saindo", conversas._ticks("PENDING", 1000, agora=1030))
        velha = conversas._ticks("PENDING", 1000, agora=1000 + conversas.ESPERA_CONFIRMACAO_S + 1)
        self.assertIn("enviada", velha)        # sem confirmação depois de 1 minuto: enviada, não "saindo" para sempre
        self.assertNotIn("saindo", velha)
        self.assertIn("enviada", conversas._ticks("SERVER_ACK"))
        self.assertIn("entregue", conversas._ticks("DELIVERY_ACK"))
        self.assertIn("lida", conversas._ticks("READ"))
        self.assertIn("lida", conversas._ticks("PLAYED"))


class Reacoes(unittest.TestCase):
    def test_grade_do_mais(self):
        lista = conversas.EMOJIS_MAIS.split()
        self.assertGreater(len(set(lista)), 100)
        for e in lista:      # o servidor só aceita emoji curto e sem ASCII
            self.assertLessEqual(len(e), 16)
            self.assertFalse(any(ch.isascii() for ch in e))
        self.assertIn("greg.reacoes", conversas._JS)
        self.assertIn("button.mais", conversas._CSS)


class NaHora(unittest.TestCase):
    """Pedido 52: a mensagem aparece na hora e o fio é trocado por diferença."""

    def test_a_pagina_tem_bolha_provisoria_fila_e_troca_por_diferenca(self):
        js = conversas._JS
        for trecho in ("function bolhaNaHora(", "function enviarTexto(", "function devolverFila(", "function trocarFio(",
                       "trocarFio(mn);", "if (enviarTexto(corpo))"):
            self.assertIn(trecho, js)
        # a leitura periódica não redesenha mais o fio inteiro
        self.assertNotIn("msgs.innerHTML = mn.innerHTML; msgs.setAttribute('data-assinatura', mn.getAttribute('data-assinatura'));\n        if (!fica", js)


class SessaoAberta(unittest.TestCase):
    """Pedido 52: uma sessão de psql atende todas as consultas do painel; se morre, reabre."""

    def _script(self, pasta):
        import os
        arq = os.path.join(pasta, "falso.sh")
        with open(arq, "w", encoding="utf-8") as f:
            f.write("#!/bin/bash\necho \"$$\" >> \"$(dirname \"$0\")/pids\"\n"
                    "while IFS= read -r l; do case \"$l\" in\n"
                    "  '\\echo '*) echo \"${l#\\\\echo }\" ;;\n"
                    "  'morrer;') exit 3 ;;\n"
                    "  'vazio;') ;;\n"
                    "  *) printf 'a\\tb\\nc\\td\\n' ;;\n"
                    "esac; done\n")
        return arq

    def test_uma_sessao_para_varias_consultas_e_reabre_se_morrer(self):
        import os
        import tempfile
        from app import lotes
        with tempfile.TemporaryDirectory() as pasta:
            psql = lotes.psql_sessao(self._script(pasta), "psql-ro-stdin", 5.0)
            try:
                self.assertEqual(psql("select 1"), [["a", "b"], ["c", "d"]])
                self.assertEqual(psql("select 2;"), [["a", "b"], ["c", "d"]])
                self.assertEqual(psql("vazio"), [])
                with open(os.path.join(pasta, "pids"), encoding="utf-8") as f:
                    self.assertEqual(len(f.read().split()), 1)          # três consultas, um processo
                with self.assertRaises(lotes.PostgresIndisponivel):
                    psql("morrer")
                self.assertEqual(psql("select 3"), [["a", "b"], ["c", "d"]])   # reabriu sozinha
            finally:
                psql.fechar()

    def test_so_aceita_subcomando_que_le_o_stdin(self):
        from app import lotes
        with self.assertRaises(ValueError):
            lotes.psql_sessao("x.sh", "psql-ro", 5.0)


class Pulso(unittest.TestCase):
    def test_consulta_sem_conteudo_e_valor_estavel(self):
        sql = wa.sql_pulso(123)
        self.assertNotIn("message", sql.replace('"messageTimestamp"', "").replace('"Message"', ""))
        self.assertIn(">= 123", sql)
        self.assertEqual(wa.pulso(lambda q: [["10", "5", "1", "0", "0", "2", "99"]], desde=0), "10.5.1.0.0.2.99")
        self.assertEqual(wa.pulso(lambda q: [], desde=0), "")

    def test_a_pagina_pergunta_o_pulso(self):
        self.assertIn("/conversas/pulso", conversas._JS)
        self.assertIn("/conversas/pulso", conversas.ROTAS)
        self.assertNotIn("setInterval(atualizar, 15000)", conversas._JS)


class SegurancaR01(unittest.TestCase):
    def test_midia_em_cache_consulta_o_banco_antes_de_servir(self):
        import inspect
        fonte = inspect.getsource(conversas.midia_get)
        self.assertLess(fonte.index("chave_da_mensagem"), fonte.index("_obter_midia"))
        self.assertIn('achou[1] == "bloqueado"', fonte)
        self.assertIn("_esquecer_midia(req.ctx, mid)", fonte)

    def test_dmg_sai_com_hash_e_exportacao_deixa_o_dns_de_fora(self):
        from pathlib import Path
        raiz = Path(__file__).resolve().parent.parent
        self.assertIn("Greg.dmg.sha256", (raiz / "bin" / "construir-dmg.sh").read_text())
        exportador = raiz / "bin" / "exportar-publico.py"
        if exportador.is_file():       # o exportador só existe no repositório privado; a edição pública não o leva
            self.assertIn("infra/dns-oigreg-app.sh", exportador.read_text())

    def test_instalador_confere_a_assinatura_do_docker(self):
        from pathlib import Path
        fonte = (Path(__file__).resolve().parent.parent / "macapp" / "Vigia" / "instalador.swift").read_text()
        self.assertIn("Developer ID Application: Docker Inc", fonte)
        self.assertIn("spctl", fonte)


if __name__ == "__main__":
    unittest.main()
