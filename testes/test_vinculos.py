"""D-49 (pedido 34): vínculo conversa → frente. Sugestão pelo nome, memória (auto nunca vence dono), semeadura, aprendizado
pelo Frentes, frente padrão na ponte, cabeçalho do lote, página /contatos e seletor de frente / recusa em /demandas."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app import lotes, vinculos as vc
from app.banco import Banco
from app.paginas import contatos, demandas
from app.ponte import classificar
from testes.test_conversas_zap import ANA, G1, MARCOS, BaseZap
from testes.test_paginas import Base, texto

FRENTES = [
    {"id": 7, "nome": "Velta Suplementos", "tipo": "cliente", "apelidos": ["velta"]},
    {"id": 20, "nome": "Grupo Exemplo (agência)", "tipo": "empresa", "apelidos": ["exemplo", "agência"]},
    {"id": 2, "nome": "CBA Laboratório", "tipo": "cliente", "apelidos": ["cba", "laboratorio"]},
    {"id": 15, "nome": "Helena Rocha", "tipo": "cliente", "apelidos": ["helena"]},
    {"id": 24, "nome": "Sistemas internos", "tipo": "interno", "apelidos": ["bella"]},
]


class TestSugerir(unittest.TestCase):
    def test_forte_por_apelido_distintivo_e_por_nome_inteiro(self):
        self.assertEqual(vc.sugerir("Luciana Maciel CBA", FRENTES), {"frente_id": 2, "termo": "cba", "forca": "forte"})
        self.assertEqual(vc.sugerir("Helena Rocha Saúde e Estética", FRENTES)["forca"], "forte")

    def test_fraca_por_primeiro_nome_ou_palavra_generica(self):
        self.assertEqual(vc.sugerir("Helena Nakada", FRENTES)["forca"], "fraca")
        self.assertEqual(vc.sugerir("Bella Massas", FRENTES)["forca"], "fraca")
        self.assertEqual(vc.sugerir("Laboratório Santos e Vidal", FRENTES)["forca"], "fraca")

    def test_grupo_da_agencia_com_cliente_vai_para_o_cliente(self):
        self.assertEqual(vc.sugerir("VELTA NUTRIÇÃO - EXEMPLO MKT", FRENTES)["frente_id"], 7)

    def test_palavra_inteira_e_sem_casamento(self):
        self.assertIsNone(vc.sugerir("Veltaso Advogados", FRENTES))   # "velta" dentro de outra palavra não casa
        self.assertIsNone(vc.sugerir("", FRENTES))
        self.assertIsNone(vc.sugerir("Maria", FRENTES))

    def test_empate_de_verdade_nao_sugere(self):
        fr = [{"id": 1, "nome": "Alfa", "tipo": "cliente", "apelidos": ["beta"]},
              {"id": 2, "nome": "Gama", "tipo": "cliente", "apelidos": ["zeta"]}]
        self.assertIsNone(vc.sugerir("beta zeta", fr))


class TestMemoria(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.banco = Banco(str(Path(self.tmp.name) / "v.db"))

    def test_auto_nunca_sobrescreve_dono(self):
        self.banco.vincular(ANA, 7, "dono")
        self.assertFalse(self.banco.vincular(ANA, 2, "auto"))
        self.assertEqual(self.banco.vinculo(ANA), {"frente_id": 7, "origem": "dono"})

    def test_dono_sobrescreve_auto_e_nenhuma_frente_fica_registrada(self):
        self.banco.vincular(ANA, 2, "auto")
        self.banco.vincular(ANA, None, "dono")
        self.assertEqual(self.banco.vinculo(ANA), {"frente_id": None, "origem": "dono"})
        self.assertIsNone(vc.frente_da_conversa(self.banco, ANA))   # conferido sem frente = sem frente padrão

    def test_semear_grava_so_forte_e_tira_auto_que_deixou_de_casar(self):
        n = vc.semear(self.banco, {ANA: "Ellen CBA", MARCOS: "Helena Nakada", G1: "Grupo da família"}, FRENTES)
        self.assertEqual(n, 1)
        self.assertEqual(self.banco.vinculos(), {ANA: {"frente_id": 2, "origem": "auto"}})
        vc.semear(self.banco, {ANA: "Ellen"}, FRENTES)
        self.assertEqual(self.banco.vinculos(), {})

    def test_frente_pelo_jid_alternativo(self):
        self.banco.vincular("123@lid", 7, "dono")
        self.assertEqual(vc.frente_da_conversa(self.banco, ANA, "123@lid")["frente_id"], 7)


class TestPonteUsaVinculo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.banco = Banco(str(Path(self.tmp.name) / "v.db"))

    def test_regras(self):
        av = classificar.aplicar_vinculo
        self.assertEqual(av(self.banco, ANA, None, "alta"), (None, "alta", None))       # sem vínculo, sem frente
        self.assertEqual(av(self.banco, ANA, 7, "alta"), (7, "alta", "ia"))             # sem vínculo: escolha da IA
        self.banco.vincular(ANA, 7, "dono")
        self.assertEqual(av(self.banco, ANA, None, "alta"), (7, "alta", "dono"))        # padrão do dono, confirmado
        self.assertEqual(av(self.banco, ANA, 2, "alta"), (2, "baixa", "ia"))            # IA diverge do dono: a confirmar
        self.banco.vincular(MARCOS, 2, "auto")
        self.assertEqual(av(self.banco, MARCOS, None, "alta"), (2, "baixa", "auto"))    # sugestão: na frente, a confirmar


class TestPonteFimAFim(Base):
    def test_item_nasce_na_frente_do_dono_confirmado_e_sem_vinculo_vai_a_confirmar(self):
        self.banco.vincular(ANA, 1, "dono")
        # atenção sobe na hora (D-57); demanda ficaria provisória até o fechamento do dia
        args = SimpleNamespace(msg_id="m1", classe="atencao", resumo="pede arte", conversa=ANA, contato="Ana", ts=1,
                               frente=None, prazo=None, confianca="alta", imagem=None, item=None, prioridade=None)
        r = classificar.executar(args, banco=self.banco, ponte=self.ponte)
        item = self.ff.itens[r["item_id"]]
        self.assertEqual((item["frente_id"], item["confirmado"], r["frente_de"]), (1, 1, "dono"))

    def test_confianca_e_obrigatoria(self):
        args = SimpleNamespace(msg_id="m2", classe="demanda", resumo="x", conversa=ANA, contato="Ana", ts=1,
                               frente=None, prazo=None, confianca=None, imagem=None, item=None, prioridade=None)
        with self.assertRaises(ValueError):
            classificar.executar(args, banco=self.banco, ponte=self.ponte)


class TestAprender(Base):
    def test_item_movido_no_frentes_vira_vinculo_do_dono(self):
        it = self.ff.semear_item(texto="WA Ana: x", origem="whatsapp", frente_id=None)
        self.classificar(msg_id="m1", conversa=ANA, item_id=it["id"], frente_id=None)
        self.assertEqual(vc.aprender_do_frentes(self.banco, self.ponte), 0)    # ninguém moveu
        self.ff.itens[it["id"]]["frente_id"] = 2
        self.assertEqual(vc.aprender_do_frentes(self.banco, self.ponte), 1)
        self.assertEqual(self.banco.vinculo(ANA), {"frente_id": 2, "origem": "dono"})
        self.assertEqual(vc.aprender_do_frentes(self.banco, self.ponte), 0)    # já aprendido

    def test_frentes_fora_nao_derruba(self):
        class Fora:
            def itens_abertos(self, **kw):
                raise OSError("fora")
        self.assertEqual(vc.aprender_do_frentes(self.banco, Fora()), 0)


class TestCabecalhoDoLote(Base):
    def test_linha_da_frente(self):
        self.assertIn("sem vínculo", lotes.linha_da_frente(self.banco, self.ponte, ANA))
        self.banco.vincular(ANA, 1, "auto")
        self.assertEqual(lotes.linha_da_frente(self.banco, self.ponte, ANA),
                         "frente: 1 · Agência (sugestão pelo nome do contato)")
        self.banco.vincular(ANA, 2, "dono")
        self.assertEqual(lotes.linha_da_frente(self.banco, self.ponte, ANA), "frente: 2 · Casa (marcada pelo dono)")


class TestPaginaContatos(BaseZap):
    def setUp(self):
        super().setUp()
        from app.paginas import conversas
        self.psql.agenda["grupos"] = [[G1, "Fulano Participante", "t"]]     # pedido 65: a agenda não nomeia grupo
        conversas.servicos(self.ctx).grupos.lembrar(G1, "Grupo Agência")
        self.psql.agenda["contatos"] = [[ANA, "Ana Paula", "t"], [MARCOS, "Marcos Casa", "f"]]
        self.psql.agenda["outros"] = []

    def pagina(self, **q):
        return contatos.ROTAS["/contatos"](self.req("GET", "/contatos", query=q))

    def post(self, corpo, cab=None):
        return contatos.ROTAS["/contatos"](self.req("POST", "/contatos", corpo, cab=cab))

    def test_abas_e_sugestao_na_linha(self):
        r = self.pagina(tipo="contatos")
        t = texto(r)
        self.assertEqual(r.status, 200)
        self.assertIn('aria-current="page">Contatos', t)
        self.assertIn(">Ana Paula</label>", t)
        self.assertIn("sugestão: Casa", t)         # "casa" é apelido fora de FRACOS: forte
        self.assertIn("talvez: Agência", texto(self.pagina(tipo="grupos")))   # "agência" é genérico: fraca
        self.assertIn('name="frente"', t)

    def test_ativas_e_padrao(self):
        t = texto(self.pagina())
        self.assertIn('href="/contatos?tipo=ativas" aria-current="page"', t)

    def test_marcar_varios_grava_dono(self):
        r = self.post({"acao": "marcar", "tipo": "contatos", "frente": "1", f"j:{ANA}": "1", f"j:{MARCOS}": "2"})
        self.assertEqual(r.status, 303)
        self.assertEqual(self.banco.vinculos(), {ANA: {"frente_id": 1, "origem": "dono"},
                                                 MARCOS: {"frente_id": 1, "origem": "dono"}})

    def test_aceitar_usa_a_sugestao_da_linha_e_limpar_apaga(self):
        self.post({"acao": "aceitar", "tipo": "contatos", f"j:{MARCOS}": "2", f"j:{ANA}": "1x"})
        self.assertEqual(self.banco.vinculos(), {MARCOS: {"frente_id": 2, "origem": "dono"}})
        self.post({"acao": "limpar", "tipo": "contatos", f"j:{MARCOS}": "1"})
        self.assertEqual(self.banco.vinculos(), {})

    def test_confirmar_todas_as_sugestoes(self):
        self.banco.vincular(ANA, 2, "auto")
        self.banco.vincular(MARCOS, 1, "dono")
        self.post({"acao": "confirmar_sugestoes"})
        self.assertEqual(self.banco.vinculo(ANA), {"frente_id": 2, "origem": "dono"})

    def test_estados_ruins(self):
        self.assertEqual(self.post({"acao": "marcar", "tipo": "contatos", "frente": "1"}).status, 400)   # nada marcado
        self.assertEqual(self.post({"acao": "marcar", "frente": "", f"j:{ANA}": "1"}).status, 400)     # sem frente
        self.assertEqual(self.post({"acao": "marcar", "frente": "99", f"j:{ANA}": "1"}).status, 400)   # frente que não existe
        self.assertEqual(self.post({"acao": "marcar", "frente": "1", "j:nao-e-jid": "1"}).status, 400)
        self.assertEqual(self.post({"acao": "xyz"}).status, 400)
        self.assertEqual(self.banco.vinculos(), {})

    def test_csrf(self):
        r = self.post({"acao": "marcar", "frente": "1", f"j:{ANA}": "1"}, cab={"Origin": "http://mal.exemplo"})
        self.assertEqual(r.status, 403)
        self.assertEqual(self.banco.vinculos(), {})


class TestDemandasFrente(Base):
    def test_mover_grava_vinculo_sem_virar_decisao_e_recusar_cancela(self):
        it = self.ff.semear_item(texto="WA Ana: x", origem="whatsapp", frente_id=None, confirmado=0)
        cid = self.classificar(msg_id="m1", conversa=ANA, item_id=it["id"], confianca="baixa")
        r = demandas.ROTAS["/demandas/{id}/frente"](self.req("POST", f"/demandas/{cid}/frente", {"frente": "2"}))
        self.assertEqual(r.status, 303)
        self.assertEqual((self.ff.itens[it["id"]]["frente_id"], self.ff.itens[it["id"]]["confirmado"]), (2, 1))
        self.assertEqual(self.banco.vinculo(ANA), {"frente_id": 2, "origem": "dono"})
        self.assertIsNone(self.banco.classificacao(cid)["decisao"])   # D-58: mover um cartão já lançado é só mover

        it2 = self.ff.semear_item(texto="WA Ana: y", origem="whatsapp", confirmado=0)
        cid2 = self.classificar(msg_id="m2", conversa=ANA, item_id=it2["id"], confianca="baixa")
        demandas.ROTAS["/demandas/{id}/recusar"](self.req("POST", f"/demandas/{cid2}/recusar"))
        self.assertEqual(self.ff.itens[it2["id"]]["status"], "cancelado")

    def test_frente_invalida(self):
        it = self.ff.semear_item(texto="WA Ana: x", origem="whatsapp")
        cid = self.classificar(msg_id="m1", conversa=ANA, item_id=it["id"])
        r = demandas.ROTAS["/demandas/{id}/frente"](self.req("POST", f"/demandas/{cid}/frente", {"frente": "77"}))
        self.assertEqual(r.status, 400)
        self.assertIsNone(self.banco.vinculo(ANA))


if __name__ == "__main__":
    unittest.main()
