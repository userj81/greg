"""Pedido 85: a baixa de UMA mídia que estoura o tempo não para a leitura inteira, e o aviso de leitura parada diz o
motivo real.
Quem chama: unittest. Reproduz o defeito de 16/11 com uma Evolution falsa por HTTP que demora mais que o tempo do
cliente numa única imagem, enquanto responde na hora ao pedido de estado. Dados fictícios.
"""
import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import alertas, lotes, saude  # noqa: E402
from app.erros import EvolutionIndisponivel, NaoAutorizado  # noqa: E402
from app.evolution_client import EvolutionClient  # noqa: E402
from app.transcricao import Transcritor  # noqa: E402
from testes.fakes import FakeEvolution  # noqa: E402
from testes.test_coletor import (PAREADO, BancoMudo, EvoFalsa, PonteSemFrentes, _Base, _imagem, _jid,  # noqa: E402
                                 _msg)

LENTA = "3EB0001"     # key.id da imagem que demora (a linha 1 de _imagem)


class EvolutionLenta(FakeEvolution):
    """Demora `atraso` segundos para devolver a mídia de `lentas`; o resto responde na hora."""

    def __init__(self, *a, lentas=(LENTA,), atraso=1.5, **k):
        super().__init__(*a, **k)
        self.lentas = set(lentas)
        self.atraso = atraso

    def _tratar(self, metodo, caminho, query, corpo, cabecalhos):
        if "getBase64FromMediaMessage" in caminho:
            chave = str(((corpo or {}).get("message") or {}).get("key", {}).get("id"))
            if chave in self.lentas:
                time.sleep(self.atraso)
        return super()._tratar(metodo, caminho, query, corpo, cabecalhos)


class EvoQueResponde(EvoFalsa):
    """Falsa sem HTTP: a mídia falha com `falhar`, o pedido de estado responde (ou não, com `sonda`)."""

    def __init__(self, falhar=None, sonda=None):
        super().__init__(falhar=falhar)
        self.sonda = sonda
        self.estados = 0

    def estado(self):
        self.estados += 1
        if self.sonda:
            raise self.sonda
        return {"state": "open"}


class TestMidiaQueDemora(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.raiz = Path(t.name)
        self.cache = self.raiz / ".midia"

    def montar(self, novas, evo, pasta="c01"):
        return lotes.montar_lote(self.raiz / pasta, novas[0]["key"]["remoteJid"], novas, [], banco=BancoMudo(),
                                 ponte=PonteSemFrentes(), evo=evo, teto_imagens=20, pausa_s=0, dormir=lambda s: None,
                                 agora=PAREADO + 100, cache_midia=self.cache)

    def test_tempo_esgotado_com_a_evolution_de_pe_e_falha_so_da_imagem(self):
        evo = EvoQueResponde(falhar=EvolutionIndisponivel(
            "Evolution inacessível em POST /chat/getBase64FromMediaMessage/i: TimeoutError"))
        r = self.montar([_imagem(1, PAREADO + 1), _msg(2, "segue a foto", ts=PAREADO + 2)], evo)
        self.assertEqual(r["imagens_falhas"], 1)
        self.assertEqual(evo.estados, 1)                          # um pedido simples decidiu
        md = (self.raiz / "c01" / "conversa.md").read_text(encoding="utf-8")
        self.assertIn("[imagem: não baixada]", md)
        self.assertIn("segue a foto", md)

    def test_imagem_que_falhou_nao_e_pedida_de_novo_no_mesmo_lote(self):
        evo = EvoQueResponde(falhar=EvolutionIndisponivel("TimeoutError"))
        self.montar([_imagem(1, PAREADO + 1)], evo)
        self.assertEqual(len(evo.chamadas), 1)
        evo2 = EvoQueResponde()
        r = self.montar([_imagem(1, PAREADO + 1)], evo2, pasta="c01-de-novo")
        self.assertEqual(evo2.chamadas, [])                       # o lote montado de novo não volta a pedir
        self.assertEqual(r["imagens_falhas"], 1)
        self.assertIsNone(lotes._do_cache(self.cache, lotes._nome_seguro("cm0001", "x")))   # a marca não é imagem
        lotes.apagar_cache(self.cache, ["cm0001"])                # fechamento do lote leva a marca junto
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_evolution_sem_resposta_ainda_para_a_leitura(self):
        evo = EvoQueResponde(falhar=EvolutionIndisponivel("fora"), sonda=EvolutionIndisponivel("fora"))
        with self.assertRaises(EvolutionIndisponivel):
            self.montar([_imagem(1, PAREADO + 1)], evo)
        with self.assertRaises(EvolutionIndisponivel):            # falsa sem pedido de estado: como antes, para
            self.montar([_imagem(1, PAREADO + 1)], EvoFalsa(falhar=EvolutionIndisponivel("fora")), pasta="c02")

    def test_chave_recusada_sempre_para(self):
        with self.assertRaises(NaoAutorizado):
            self.montar([_imagem(1, PAREADO + 1)], EvoQueResponde(falhar=NaoAutorizado("401")))


class TestAudioQueDemora(unittest.TestCase):
    def test_audio_com_tempo_esgotado_vira_falhou_e_a_leitura_segue(self):
        fonte = (Path(__file__).resolve().parents[1] / "app/transcricao.py").read_text(encoding="utf-8")
        trecho = fonte[fonte.index('dados, ext = evo.midia_audio(ln["key"])'):]
        trecho = trecho[:trecho.index("caminho = self.pasta")]
        self.assertIn("except NaoAutorizado:\n                    raise", trecho)
        self.assertIn("if not lotes.evolution_responde(evo):", trecho)
        self.assertIn('estado="falhou"', trecho[trecho.index("evolution_responde"):])   # tentado até `tentativas`
        self.assertTrue(callable(Transcritor))


class TestCicloComImagemLenta(_Base):
    """O defeito de 16/11 por HTTP de verdade: a baixa de uma imagem estoura o tempo do cliente e as outras conversas
    são lidas no mesmo ciclo."""

    def setUp(self):
        super().setUp()
        self.evo_fake.parar()
        self.evo_fake = EvolutionLenta().iniciar()
        self.addCleanup(self.evo_fake.parar)

    def test_uma_imagem_lenta_nao_derruba_a_leitura(self):
        linhas = [_imagem(1, PAREADO + 1, jid=_jid(1)), _msg(2, "oi, tudo bem?", ts=PAREADO + 2, jid=_jid(2)),
                  _imagem(3, PAREADO + 3, jid=_jid(3))]
        evo = lambda: EvolutionClient(self.evo_fake.url, self.evo_fake.instancia, self.evo_fake.apikey, timeout=0.5)
        r = self._rodar(linhas, obter_evo=evo, sem_claude=True)
        conversas = {p.read_text(encoding="utf-8").split("conversa: ", 1)[-1].split("\n", 1)[0]: p.read_text(encoding="utf-8")
                     for p in (self.dados / "caixa").rglob("conversa.md")}
        self.assertEqual(len(conversas), 3, r)                   # as três conversas entraram no lote
        textos = "\n".join(conversas.values())
        self.assertIn("[imagem: não baixada]", textos)           # a lenta
        self.assertIn("oi, tudo bem?", textos)
        self.assertRegex(textos, r"\[imagem: img-01-")           # a outra imagem baixou
        self.assertIn(("GET", "/instance/connectionState/vigia"), self.evo_fake.chamadas)

    def test_ciclo_inteiro_termina_bem_e_avanca_o_cursor(self):
        evo = lambda: EvolutionClient(self.evo_fake.url, self.evo_fake.instancia, self.evo_fake.apikey, timeout=0.5)
        r = self._rodar([_imagem(1, PAREADO + 1, jid=_jid(1)), _msg(2, "oi", ts=PAREADO + 2, jid=_jid(2))], obter_evo=evo)
        self.assertEqual(r["status"], "ok", r)
        self.assertEqual(self.cursor.ultimo_seq, 2)


class TestLeituraParadaDizOMotivo(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        (self.d / "cursor.json").write_text(json.dumps({"ultimo_seq": 0, "pareado_em": 1}))
        self.t = 1_800_000_000
        (self.d / "ultima-leitura.json").write_text(json.dumps({"em": self.t - 90 * 60, "status": "ok"}))
        antigo = saude.NOTIFICAR
        self.notas = []
        saude.NOTIFICAR = lambda titulo, texto: self.notas.append((titulo, texto))
        self.addCleanup(setattr, saude, "NOTIFICAR", antigo)

    def conferir(self):
        return saude.conferir_leitura(self.d, agora=self.t, despertar=lambda: None, agente=lambda: True)

    def aviso(self):
        return next(a for a in alertas.abertos(self.d) if a["codigo"] == "leitura-parada")

    def test_evolution_que_o_monitor_fechou_vira_o_motivo(self):
        erro = EvolutionIndisponivel("Evolution inacessível em POST /chat/getBase64FromMediaMessage/vigia: TimeoutError")
        saude.do_coletor(self.d, erro)
        saude.do_monitor(self.d, {"evento": "ok"})               # a sonda do monitor fecha o evolution-fora
        self.assertEqual(self.conferir(), "leitura-parada")
        a = self.aviso()
        self.assertIn("Na última tentativa, a Evolution, que liga o Greg ao WhatsApp", a["por_que"])
        self.assertNotIn("não identificou", a["por_que"])
        self.assertNotIn("/chat/", a["por_que"])                   # sem nome de rota
        self.assertNotIn("getBase64", json.dumps(a))
        self.assertIn("Use Reiniciar Evolution.", a["passos"])
        saude.notificar(self.d, agora=self.t)
        self.assertTrue(any("Na última tentativa" in texto for _, texto in self.notas))

    def test_erro_sem_codigo_diz_que_houve_erro(self):
        saude.do_coletor(self.d, RuntimeError("trecho que nunca vai para a tela"))
        self.conferir()
        a = self.aviso()
        self.assertIn("erro que o Greg não reconhece", a["por_que"])
        self.assertNotIn("trecho", json.dumps(a))
        self.assertIn("Se repetir, rode no Terminal: bin/vigia.sh logs", a["passos"])

    def test_sem_tentativa_registrada_fica_o_texto_do_catalogo(self):
        self.conferir()
        self.assertIn("não identificou", self.aviso()["por_que"])

    def test_leitura_boa_apaga_o_registro(self):
        saude.do_coletor(self.d, EvolutionIndisponivel("fora"))
        self.assertTrue((self.d / saude.ERRO_LEITURA).exists())
        self.assertEqual(json.loads((self.d / saude.ERRO_LEITURA).read_text()), {"motivo": "evolution-fora",
                                                                                  "em": json.loads((self.d / saude.ERRO_LEITURA).read_text())["em"]})
        saude.do_coletor(self.d, None, {"status": "sem_novidade"})
        self.assertFalse((self.d / saude.ERRO_LEITURA).exists())


if __name__ == "__main__":
    unittest.main()
