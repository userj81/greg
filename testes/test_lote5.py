"""Pedido 63, lote 5 — conforto (auditoria C2, C4, D1, D3, F1, K1, E1 e falhas de rede caladas 30, 32, 53)."""
import unittest
from pathlib import Path

from app.paginas import contatos, conversas, demandas, envios, frentes

RAIZ = Path(__file__).resolve().parents[1]


class TestConversas(unittest.TestCase):
    js = conversas._JS

    def test_c2_envio_nao_liberado_diz_o_motivo(self):
        for chave in ("cancelado:", "sem_touch:", "sem_token:", "recusado:", "rede:"):
            self.assertIn(chave, self.js)
        self.assertIn("window.vigiaEnvioLiberado = function (ok, motivo)", self.js)
        swift = (RAIZ / "macapp" / "Vigia" / "main.swift").read_text(encoding="utf-8")
        for motivo in ('"sem_touch"', '"cancelado"', '"sem_token"', '"recusado"'):
            self.assertIn(motivo, swift)

    def test_falha_de_rede_nao_fica_calada(self):
        self.assertIn("}).catch(function () { dizer(MOTIVO_TRAVA.rede, true); });", self.js)                 # 32
        self.assertIn("As mensagens anteriores não foram carregadas.", self.js)                              # 30
        self.assertIn("O painel não respondeu. Tente conferir de novo.", self.js)                            # 53

    def test_c4_encaminhar_busca_a_lista_inteira_quando_ha_filtro(self):
        self.assertIn("fetch('/conversas?f=todas'", self.js)
        self.assertIn("(encFonte || document).querySelectorAll('#zap-itens .zap-item')", self.js)
        self.assertIn("Só as conversas do filtro atual aparecem aqui.", self.js)

    def test_d3_gravacao_no_teto_espera_o_enviar(self):
        self.assertIn("if (seg >= 600) { pararGravacao('segurar'); }", self.js)
        self.assertNotIn("if (seg >= 600) { pararGravacao(true); }", self.js)
        self.assertIn("if (retido) { var rt = retido; largarRetido(); mandarAudio(rt.partes, rt.tipo, rt.gravado); return; }", self.js)
        self.assertIn("Enviar manda o áudio; Cancelar descarta.", self.js)


class TestOutrasTelas(unittest.TestCase):
    def test_d1_subir_agora_sem_desfazer_no_toast(self):
        js = demandas._JS
        self.assertIn("if (decisao === 'recusado') historico.push(id);", js)
        self.assertIn("if (decisao !== 'recusado') { window.vigiaToast(j.toast || 'Subiu para o Frentes.'); return; }", js)

    def test_k1_marcas_ficam_quando_falta_a_frente(self):
        self.assertIn("ev.submitter.value !== 'marcar'", contatos._JS_K1)
        self.assertIn("ev.preventDefault();", contatos._JS_K1)

    def test_e1_envios_oferece_liberar(self):
        import inspect
        fonte = inspect.getsource(envios)
        self.assertIn("data-liberar-envio>Liberar o envio para retomar</button>", fonte)
        self.assertNotIn("Retomar pede Touch ID ou a ferramenta", fonte)

    def test_f1_captura_guarda_o_texto_e_diz_onde_caiu(self):
        import inspect
        fonte = inspect.getsource(frentes)
        self.assertIn("O que você escreveu (nada foi gravado):", fonte)
        self.assertIn('"anotado=" +', fonte)
        self.assertIn('f"Anotado em {nome}."', fonte)
