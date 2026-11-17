"""Pedido 86 (ordem do dono de 16/11/2026): no conector, greg_anotar aceita `prazo` (desliga a
data tirada do texto e faz um item só) e `item_unico`; greg_item_editar corrige texto e prazo de um item. Tudo
aditivo: sem os campos novos, o pedido ao Frentes é o de sempre.
Quem chama: unittest, com o Frentes falso por HTTP (testes/fakes.py) e o Frentes embutido. O parser em si (o que o
Frentes faz com `prazo` e `item_unico`) é testado no serviço das frentes, em testes/unit/test_captura_prazo.py de lá.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import mcp  # noqa: E402
from app import mcp_ferramentas  # noqa: E402,F401  (registra as ferramentas)
from app.frentes_local import FrentesLocal  # noqa: E402
from app.paginas import frentes  # noqa: E402
from app.server import _despachar, descobrir_rotas  # noqa: E402
from testes.test_mcp import BaseMcp, chamar  # noqa: E402
from testes.test_paginas import XSS, Base as BasePaginas, texto  # noqa: E402


class TestAnotarComPrazo(BaseMcp):
    def setUp(self):
        super().setUp()
        self.ligar("demandas")

    def ultima(self):
        return self.ff.capturas[-1]

    def test_sem_campos_novos_o_pedido_e_o_de_sempre(self):
        erro, dados, _ = chamar(self.ctx, "greg_anotar", texto="pagar boleto amanhã; ligar pro banco")
        self.assertFalse(erro, dados)
        self.assertNotIn("prazo", self.ultima())
        self.assertNotIn("item_unico", self.ultima())

    def test_prazo_vai_ao_frentes_e_null_tambem(self):
        erro, dados, _ = chamar(self.ctx, "greg_anotar", texto="revisar hoje a pauta", prazo="2026-11-20")
        self.assertFalse(erro, dados)
        self.assertEqual(self.ultima()["prazo"], "2026-11-20")
        erro, dados, _ = chamar(self.ctx, "greg_anotar", texto="ver hoje", prazo=None)
        self.assertFalse(erro, dados)
        self.assertIn("prazo", self.ultima())
        self.assertIsNone(self.ultima()["prazo"])

    def test_item_unico(self):
        erro, dados, _ = chamar(self.ctx, "greg_anotar", texto="café + açúcar", item_unico=True)
        self.assertFalse(erro, dados)
        self.assertIs(self.ultima()["item_unico"], True)
        chamar(self.ctx, "greg_anotar", texto="café + açúcar", item_unico=False)
        self.assertNotIn("item_unico", self.ultima())

    def test_prazo_invalido_nao_anota_nada(self):
        antes = len(self.ff.capturas)
        for ruim in ("20/11/2026", "2026-02-30", "amanhã", 20261120):
            erro, dados, _ = chamar(self.ctx, "greg_anotar", texto="qualquer coisa", prazo=ruim)
            self.assertTrue(erro, ruim)
            self.assertIn("AAAA-MM-DD", str(dados))
        self.assertEqual(len(self.ff.capturas), antes)

    def test_esquema_anuncia_os_campos(self):
        props = mcp.FERRAMENTAS["greg_anotar"].esquema["properties"]
        self.assertEqual(props["prazo"]["type"], ["string", "null"])
        self.assertEqual(props["item_unico"]["type"], "boolean")
        self.assertEqual(mcp.FERRAMENTAS["greg_anotar"].esquema["required"], ["texto"])   # os novos são opcionais
        self.assertEqual(mcp.FERRAMENTAS["greg_item_editar"].esquema["required"], ["item_id"])


class TestItemEditar(BaseMcp):
    def setUp(self):
        super().setUp()
        self.ligar("demandas")
        self.iid = self.ponte.capturar("trocar o banner", frente_id=self.ponte.frentes()[0]["id"])["itens"][0]["id"]

    def patch(self):
        return self.ff.patches[-1]

    def test_texto_e_prazo(self):
        erro, dados, _ = chamar(self.ctx, "greg_item_editar", item_id=self.iid, texto="trocar o banner da home",
                                prazo="2026-11-21")
        self.assertFalse(erro, dados)
        self.assertEqual(self.patch()["id"], self.iid)
        self.assertEqual(self.patch()["corpo"], {"texto": "trocar o banner da home", "prazo": "2026-11-21",
                                                 "lembrete_em": "2026-11-21T08:00:00-04:00", "lembrete_enviado": 0})
        self.assertEqual(dados["item"]["prazo"], "2026-11-21")
        self.assertEqual(self.ff.itens[self.iid]["texto"], "trocar o banner da home")

    def test_so_o_texto_nao_mexe_no_prazo_nem_no_lembrete(self):
        erro, dados, _ = chamar(self.ctx, "greg_item_editar", item_id=self.iid, texto="outro texto")
        self.assertFalse(erro, dados)
        self.assertEqual(self.patch()["corpo"], {"texto": "outro texto"})

    def test_prazo_null_tira_o_prazo_e_o_lembrete(self):
        erro, dados, _ = chamar(self.ctx, "greg_item_editar", item_id=self.iid, prazo=None)
        self.assertFalse(erro, dados)
        self.assertEqual(self.patch()["corpo"], {"prazo": None, "lembrete_em": None, "lembrete_enviado": 0})

    def test_recusas(self):
        n = len(self.ff.patches)
        erro, dados, _ = chamar(self.ctx, "greg_item_editar", item_id=self.iid)
        self.assertTrue(erro)
        erro, dados, _ = chamar(self.ctx, "greg_item_editar", item_id=self.iid, prazo="amanhã")
        self.assertTrue(erro)
        erro, dados, _ = chamar(self.ctx, "greg_item_editar", item_id=self.iid, texto="   ")
        self.assertTrue(erro)
        self.assertEqual(len(self.ff.patches), n)
        erro, dados, _ = chamar(self.ctx, "greg_item_editar", item_id=987654, texto="x")
        self.assertTrue(erro)
        self.assertIn("Nada mudou", str(dados))

    def test_chave_desligada_bloqueia(self):
        from app import integracao
        integracao.definir(self.ctx, "demandas", False)
        erro, dados, _ = chamar(self.ctx, "greg_item_editar", item_id=self.iid, texto="x")
        self.assertTrue(erro)


class TestFrentesEmbutido(unittest.TestCase):
    """Edição pública: o Frentes embutido não interpreta texto, mas guarda o prazo dado e aceita a edição."""

    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.p = FrentesLocal(Path(t.name) / "frentes.db")

    def test_prazo_na_captura_e_edicao(self):
        r = self.p.captura_livre("revisar hoje", prazo="2026-11-20", item_unico=True)
        item = r["itens"][0]
        self.assertEqual(item["prazo"], "2026-11-20")
        r = self.p.alterar(item["id"], texto="revisar amanhã", prazo=None, lembrete_em=None, lembrete_enviado=0)
        self.assertEqual((r["item"]["texto"], r["item"]["prazo"]), ("revisar amanhã", None))


class TestLinhaDoTempo(unittest.TestCase):
    """(4): itens por seção, com o dia em que abriram e fecharam."""

    def test_agrupa_por_secao_e_ordena_pelo_fato_mais_recente(self):
        secs = [{"id": 7, "nome": "Site"}, {"id": 8, "nome": "Vazia"}]
        abertos = [{"id": 1, "texto": "trocar  banner", "criado_em": "2026-11-10T09:00:00-04:00"},
                   {"id": 2, "texto": "solto", "criado_em": "2026-11-12T09:00:00-04:00"}]
        feitos = [{"id": 3, "texto": "logo", "criado_em": "2026-11-01T09:00:00-04:00", "concluido_em": "2026-11-14T10:00:00-04:00"},
                  {"id": 4, "texto": "embutido", "criado_em": "2026-11-02", "feito_em": "2026-11-03T08:00:00"}]
        g = frentes.linha_do_tempo(abertos, feitos, secs, {1: 7, 3: 7, 4: 99})
        self.assertEqual([(x["secao_id"], x["secao"]) for x in g], [(7, "Site"), (None, None)])   # vazia não entra; seção sumida = sem seção
        self.assertEqual(g[0]["itens"], [{"id": 3, "texto": "logo", "abriu": "2026-11-01", "fechou": "2026-11-14"},
                                         {"id": 1, "texto": "trocar banner", "abriu": "2026-11-10", "fechou": None}])
        self.assertEqual([(i["id"], i["fechou"]) for i in g[1]["itens"]], [(2, None), (4, "2026-11-03")])

    def test_sem_itens_nao_ha_bloco(self):
        self.assertEqual(frentes.html_linha_do_tempo([], "2026-11-16"), "")


class TestLinhaDoTempoNaTela(BasePaginas):
    def rota(self, metodo, caminho, corpo=None):
        exatas, padroes = descobrir_rotas()
        return _despachar(exatas, padroes, caminho)(self.req(metodo, caminho, corpo=corpo))

    def test_frente_mostra_a_linha_do_tempo_recolhida_e_so_de_leitura(self):
        sid = self.banco.criar_secao(1, "Site")
        a = self.ff.semear_item(texto="trocar o banner " + XSS, frente_id=1, criado_em="2026-11-10T09:00:00-04:00")
        self.ff.semear_item(texto="logo nova", frente_id=1, status="feito", criado_em="2026-11-01T09:00:00-04:00",
                            concluido_em="2026-11-14T10:00:00-04:00")
        self.banco.por_na_secao(a["id"], sid)
        t = texto(self.rota("GET", "/frentes/1"))
        bloco = t[t.index('<details class="fr-grupo fr-linha">'):]
        bloco = bloco[:bloco.index("</details>")]
        self.assertIn("Linha do tempo", bloco)
        self.assertIn('<h4 class="fr-lt-sec">Site</h4>', bloco)
        self.assertIn('<h4 class="fr-lt-sec">Sem seção</h4>', bloco)
        self.assertIn("abriu 01/11 · fechou 14/11", bloco.replace("/26", ""))
        self.assertIn("abriu 10/11", bloco.replace("/26", ""))
        self.assertNotIn("<script>alert", bloco)
        self.assertNotIn("<form", bloco)                          # só leitura: nenhum botão nem formulário
        self.assertNotIn(" open", bloco.split(">", 1)[0])         # começa recolhida

    def test_caixa_de_entrada_nao_tem_linha_do_tempo(self):
        self.ff.semear_item(texto="sem frente")
        self.assertNotIn('class="fr-grupo fr-linha"', texto(self.rota("GET", "/frentes/inbox")))


class TestLinhaDoTempoNoConector(BaseMcp):
    def setUp(self):
        super().setUp()
        self.ligar("demandas")
        self.fid = self.ponte.frentes()[0]["id"]

    def test_devolve_por_secao_com_as_datas(self):
        self.ff.semear_item(texto="a fazer", frente_id=self.fid, criado_em="2026-11-10T09:00:00-04:00")
        self.ff.semear_item(texto="feito", frente_id=self.fid, status="feito", criado_em="2026-11-01T09:00:00-04:00",
                            concluido_em="2026-11-14T10:00:00-04:00")
        erro, dados, _ = chamar(self.ctx, "greg_frente_linha_do_tempo", frente_id=self.fid)
        self.assertFalse(erro, dados)
        self.assertEqual(dados["total"], 2)
        itens = dados["secoes"][0]["itens"]
        self.assertEqual([(i["texto"], i["abriu"], i["fechou"]) for i in itens],
                         [("feito", "2026-11-01", "2026-11-14"), ("a fazer", "2026-11-10", None)])
        self.assertIn("aviso", dados)
        self.assertTrue(mcp.FERRAMENTAS["greg_frente_linha_do_tempo"].so_leitura)

    def test_frente_inexistente(self):
        erro, dados, _ = chamar(self.ctx, "greg_frente_linha_do_tempo", frente_id=987654)
        self.assertTrue(erro)


if __name__ == "__main__":
    unittest.main()
