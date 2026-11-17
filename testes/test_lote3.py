"""Pedido 63, lote 3 — ações que enganam (auditoria C1, C3, C5, D2): a tela para de enviar e diz por quê; o registro
que falha depois do envio não vira "Falhou"; mídia que não carrega diz o motivo; classe gravada sem subir avisa."""
import json
import unittest
from types import SimpleNamespace

from app import agendados
from app.erros import FrentesIndisponivel
from app.paginas import conversas, demandas
from testes.test_conversas_zap import ANA, BaseZap
from testes.test_paginas import Base, texto


class BancoQueNaoRegistra:
    """Embrulha o banco: tudo funciona, menos o registro do envio (SQLite travado, disco cheio)."""

    def __init__(self, banco):
        self._b = banco

    def __getattr__(self, nome):
        return getattr(self._b, nome)

    def registrar_envio(self, **k):
        raise RuntimeError("database is locked")

    def definir_apelido(self, *a, **k):
        raise RuntimeError("database is locked")


class TestC1PodeTerSaido(unittest.TestCase):
    def test_faixa_fixa_com_ja_conferi_e_nenhuma_barra_calada(self):
        js = conversas._JS
        self.assertIn("function marcarIncerto(texto) { incerto = true; dizer(''); mostrarIncerto(texto, false); }", js)
        self.assertIn("b.textContent = 'Já conferi';", js)
        self.assertIn("incerto = false; faixaInc.hidden = true;", js)
        # todo ponto que barrava em silêncio agora mostra a faixa
        # 1 definição + 9 guardas (pedido 84 somou o áudio, que agora mostra a bolha antes de preparar o arquivo, e o
        # "Tentar de novo" da bolha não enviada)
        self.assertEqual(js.count("barradoPorIncerto()"), 10)
        self.assertNotRegex(js, r"if \((enviando|apagando|reagindo|encaminhando) \|\| incerto")
        self.assertNotIn("if (incerto) { return false; }", js)
        # o estado não é mais ligado sem a faixa
        self.assertEqual(js.count("incerto = true"), 1)
        self.assertIn(".zap-incerto {", conversas._CSS)


class TestC3RegistroDepoisDoEnvio(BaseZap):
    def setUp(self):
        super().setUp()
        self.liberar()
        self.ctx.banco = BancoQueNaoRegistra(self.banco)

    def test_texto_enviado_responde_sucesso_mesmo_sem_registrar(self):
        with self.assertLogs("vigia", level="ERROR") as logs:
            r = self.post("/conversas/enviar", {"para": ANA, "texto": "Oi, Ana!", "nome": "Ana"})
        self.assertEqual(r.status, 200, texto(r))
        d = json.loads(texto(r))
        self.assertEqual((d["enviadas"], d["contato_salvo"]), (1, False))
        self.assertEqual(len(self.fe.enviadas), 1)
        saida = "\n".join(logs.output)
        self.assertIn("envio saiu mas o registro falhou", saida)
        self.assertNotIn("Oi, Ana", saida)

    def test_nenhum_registro_direto_sobrou_nas_rotas_de_envio(self):
        fonte = open(conversas.__file__, encoding="utf-8").read()
        self.assertEqual(fonte.count("req.ctx.banco.registrar_envio("), 1)       # só dentro de _registrar
        self.assertGreaterEqual(fonte.count("_registrar(req, "), 8)

    def test_agendado_que_saiu_nao_e_mandado_de_novo(self):
        agora = [1_800_000_000]
        ag = agendados.Agenda(self.dados / "agendados", relogio=lambda: agora[0])
        ag.criar(ANA, agora[0] + 120, texto="uma vez só")
        agora[0] += 121
        v = agendados.Agendador(self.ctx, ag, relogio=lambda: agora[0])
        with self.assertLogs("vigia.agendados", level="ERROR"):
            v.passo()
        v.passo()
        self.assertEqual(len(self.fe.enviadas), 1)
        self.assertEqual(ag.listar()[0]["estado"], "enviado")

    def test_fila_marca_o_destino_antes_de_registrar(self):
        fonte = open(__import__("app.fila_envio", fromlist=["x"]).__file__, encoding="utf-8").read()
        # desde o M2 (robustez 16/11) enviado e incerto gravam pelo mesmo _fechar; a ordem C3 continua a mesma
        i = fonte.index('self.ctx.banco.atualizar_destino_envio(d["id"], estado, motivo, msg_id=msg_id)')
        self.assertLess(i, fonte.index('self._registrar(d["para"], h, por)'))


class TestC5MidiaIndisponivel(unittest.TestCase):
    def test_a_bolha_diz_o_motivo_e_oferece_tentar_de_novo(self):
        js = conversas._JS
        self.assertIn("document.addEventListener('error', function (ev) {", js)
        self.assertIn("src.indexOf('/conversas/midia?') !== 0", js)
        self.assertIn("404: 'Esta mídia não está mais disponível.", js)
        self.assertIn("502: 'A ponte com o WhatsApp não respondeu ao baixar esta mídia.'", js)
        self.assertIn("b.textContent = 'Tentar de novo';", js)
        self.assertIn("r.status !== 404 && r.status !== 401", js)      # expirada não oferece tentar: não volta
        self.assertIn(".zap-midia-erro {", conversas._CSS)


class TestD2ClasseSemSubir(Base):
    def setUp(self):
        super().setUp()
        self.cid = self.classificar(classe="demanda", confianca="alta")

        def fora(*a, **k):
            raise FrentesIndisponivel("fora")
        self.ctx.ponte = SimpleNamespace(**{n: fora for n in ("capturar", "criar_item", "lancar", "frentes", "itens")})

    def curar(self, **cab):
        return demandas.ROTAS["/demandas/{id}/curar"](
            self.req("POST", f"/demandas/{self.cid}/curar", corpo={"classe": "atencao"}, cab=cab))

    def test_json_diz_que_gravou_e_que_nao_subiu(self):
        import unittest.mock as mock
        from app import lancamento

        def falha(*a, **k):
            raise FrentesIndisponivel("fora")
        with mock.patch.object(lancamento, "lancar", falha):
            r = self.curar(**{"x-vigia-auto": "1"})
            d = json.loads(texto(r))
            self.assertEqual((d["ok"], d["confirmado"], d["nao_subiu"]), (True, False, True))
            self.assertEqual(d["toast"], "Classe gravada: atenção. Não subiu: o Frentes não respondeu. Sobe no fechamento do dia.")
            self.assertEqual(self.banco.classificacao(self.cid)["classe_curada"], "atencao")
            r = self.curar()                                          # sem JavaScript: o aviso vai pela query
            self.assertEqual(r.status, 303)
            self.assertIn("aviso=nao_subiu", dict(r.cabecalhos)["Location"])
        t = texto(demandas.ROTAS["/demandas"](self.req("GET", "/demandas", query={"aviso": "nao_subiu"})))
        self.assertIn("Não subiu: o Frentes não respondeu", t)

    def test_quando_sobe_o_aviso_nao_aparece(self):
        self.ctx.ponte = self.ponte
        d = json.loads(texto(self.curar(**{"x-vigia-auto": "1"})))
        self.assertEqual((d["confirmado"], d["nao_subiu"]), (True, False))
        self.assertEqual(d["toast"], "Classe: atenção. Subiu para o Frentes.")


if __name__ == "__main__":
    unittest.main()
