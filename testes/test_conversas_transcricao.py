"""Pedido 30 (D-47): a transcrição do áudio aparece abaixo do player na conversa aberta (só leitura do SQLite)."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from testes.test_conversas_zap import AGORA, ANA, BaseZap, k, texto  # noqa: E402


class TestTranscricaoNaConversa(BaseZap):
    def audio(self, mid="a1x", seg=42):
        self.psql.msgs[ANA] = [[mid, k(ANA, mid="A1"), "Ana Paula", "audioMessage",
                                json.dumps({"audioMessage": {"seconds": seg}}), str(AGORA - 60), ""]]

    def gravar(self, mid="a1x", estado="ok", texto_="oi Dono", **kw):
        self.banco.gravar_transcricao(msg_id=mid, conversa=ANA, ts=AGORA - 60, estado=estado, texto=texto_, segundos=42,
                                      segundos_lidos=kw.pop("lidos", 42), **kw)

    def test_sem_transcricao_falhou_ou_pulado_so_o_player(self):
        self.audio()
        t = texto(self.get("/conversas", c=ANA))
        self.assertIn("<audio controls", t)
        self.assertNotIn('class="zap-transcricao"', t)
        for estado in ("falhou", "pulado"):
            self.gravar(estado=estado, texto_="")
            self.assertNotIn('class="zap-transcricao"', texto(self.get("/conversas", c=ANA)))
        self.assertIn(":t1", texto(self.get("/conversas", c=ANA)).split('data-assinatura="', 1)[1].split('"', 1)[0])

    def test_texto_curto_inteiro_escapado_e_assinatura_muda(self):
        self.audio()
        antes = texto(self.get("/conversas", c=ANA)).split('data-assinatura="', 1)[1].split('"', 1)[0]
        self.gravar(texto_="oi <b>Dono</b> & cia")
        t = texto(self.get("/conversas", c=ANA))
        self.assertIn('<p class="zap-transcricao">oi &lt;b&gt;Dono&lt;/b&gt; &amp; cia</p>', t)
        self.assertNotIn("<b>Dono</b>", t)
        depois = t.split('data-assinatura="', 1)[1].split('"', 1)[0]
        self.assertNotEqual(antes, depois)

    def test_texto_longo_abre_em_ver_tudo_e_cortado_tem_nota(self):
        self.audio()
        longo = " ".join(f"palavra{i}" for i in range(80))
        self.gravar(estado="cortado", texto_=longo, lidos=600)
        t = texto(self.get("/conversas", c=ANA))
        self.assertIn('<details class="zap-transcricao"><summary>palavra0 ', t)
        self.assertIn("… <b>ver tudo</b></summary><p>" + longo + "</p><small>só os primeiros 10:00</small></details>", t)
        resumo = t.split('<summary>', 1)[1].split("…", 1)[0]
        self.assertLess(len(resumo), 281)
        self.assertFalse(resumo.endswith(" "))


if __name__ == "__main__":
    unittest.main()
