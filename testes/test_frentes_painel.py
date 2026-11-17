"""Frentes dentro do Greg (D-37): lista com árvore, detalhe com grupos e as ações (jogar, feito, reabrir, mover,
confirmar) pela ponte. Usa o FakeFrentes e a mesma base dos testes de páginas."""
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.paginas import frentes  # noqa: E402
from app.server import descobrir_rotas, _despachar  # noqa: E402
from testes.test_paginas import XSS, Base, texto  # noqa: E402

ONTEM = (date.today() - timedelta(days=1)).isoformat()
HOJE = date.today().isoformat()
SEMANA_QUE_VEM = (date.today() + timedelta(days=9)).isoformat()


class TestFrentesPainel(Base):
    def rota(self, metodo, caminho, corpo=None, cab=None):
        exatas, padroes = descobrir_rotas()
        h = _despachar(exatas, padroes, caminho)
        self.assertIsNotNone(h, caminho)
        return h(self.req(metodo, caminho, corpo=corpo, cab=cab))

    def test_lista_tem_arvore_cartoes_placar_e_captura(self):
        self.ff.semear_item(texto="ligar pro contador", frente_id=1)
        t = texto(self.rota("GET", "/frentes"))
        self.assertIn('class="arvore"', t)
        self.assertIn('href="/frentes/1"', t)
        self.assertIn('class="tabela fr-frentes"', t)
        self.assertIn('class="kv placar', t)
        self.assertIn('action="/frentes/captura"', t)
        self.assertIn(">AG<", t)  # ficha de "Agência"

    def test_detalhe_agrupa_e_escapa(self):
        self.ff.semear_item(texto="atrasado " + XSS, frente_id=1, prazo=ONTEM)
        self.ff.semear_item(texto="de hoje", frente_id=1, prazo=HOJE)
        self.ff.semear_item(texto="duvida do zap", frente_id=1, confirmado=0, origem="whatsapp")
        self.ff.semear_item(texto="mais pra frente", frente_id=1, prazo=SEMANA_QUE_VEM)
        self.ff.semear_item(texto="sem data", frente_id=1)
        self.ff.semear_item(texto="ja foi", frente_id=1, status="feito")
        self.ff.semear_item(texto="de outra frente", frente_id=2)
        r = self.rota("GET", "/frentes/1")
        t = texto(r)
        self.assertEqual(r.status, 200)
        self.assertNotIn("<script>alert", t)
        for grupo in ("atrasados", "hoje", "confirmar", "proximos", "sem_prazo", "feitos"):
            self.assertIn(f"grupo-{grupo}", t)
        self.assertNotIn("de outra frente", t)
        self.assertIn('aria-current="page"', t)  # frente ativa na árvore
        self.assertIn("do WhatsApp", t)

    def test_frente_inexistente_404_e_inbox(self):
        self.assertEqual(self.rota("GET", "/frentes/999").status, 404)
        self.ff.semear_item(texto="solto no inbox")
        t = texto(self.rota("GET", "/frentes/inbox"))
        self.assertIn("solto no inbox", t)

    def test_feito_reabrir_e_volta_validada(self):
        it = self.ff.semear_item(texto="x", frente_id=1)
        r = self.rota("POST", f"/frentes/item/{it['id']}/feito", {"volta": "/frentes/1"})
        self.assertEqual((r.status, dict(r.cabecalhos)["Location"]), (303, "/frentes/1"))
        self.assertEqual(self.ff.itens[it["id"]]["status"], "feito")
        r = self.rota("POST", f"/frentes/item/{it['id']}/reabrir", {"volta": "https://fora.example/x"})
        self.assertEqual(dict(r.cabecalhos)["Location"], "/frentes")  # volta de fora é ignorada
        self.assertEqual(self.ff.itens[it["id"]]["status"], "aberto")

    def test_mover_para_frente_e_inbox(self):
        it = self.ff.semear_item(texto="x", frente_id=1)
        self.rota("POST", f"/frentes/item/{it['id']}/mover", {"frente_id": "2"})
        self.assertEqual(self.ff.itens[it["id"]]["frente_id"], 2)
        self.rota("POST", f"/frentes/item/{it['id']}/mover", {"frente_id": "inbox"})
        self.assertIsNone(self.ff.itens[it["id"]]["frente_id"])
        n = len(self.ff.patches)
        self.rota("POST", f"/frentes/item/{it['id']}/mover", {"frente_id": "lixo"})
        self.assertEqual(len(self.ff.patches), n)  # destino inválido não chama o Frentes

    def test_confirmar_qualquer_item_espelha_decisao(self):
        it = self.ff.semear_item(texto="do ditado", frente_id=1, confirmado=0, origem="ditado")
        cid = self.classificar(item_id=it["id"], confianca="baixa")
        self.rota("POST", f"/frentes/item/{it['id']}/confirmar", {"volta": "/frentes/1"})
        self.assertEqual(self.ff.itens[it["id"]]["confirmado"], 1)
        self.assertEqual(self.banco.classificacao(cid)["decisao"], "confirmado")

    def test_captura_usa_parser_do_frentes_com_frente(self):
        r = self.rota("POST", "/frentes/captura", {"texto": "  mandar   relatório sexta ", "frente_id": "1",
                                                   "volta": "/frentes/1"})
        self.assertEqual(r.status, 303)
        self.assertEqual(self.ff.capturas[-1], {"texto": "mandar relatório sexta", "origem": "vigia", "frente_id": 1})
        n = len(self.ff.capturas)
        self.rota("POST", "/frentes/captura", {"texto": "   "})
        self.assertEqual(len(self.ff.capturas), n)  # vazio não chama

    def test_criar_frente_pelo_painel(self):
        t = texto(self.rota("GET", "/frentes"))
        self.assertIn('action="/frentes/nova"', t)
        self.assertNotIn("app Frentes", t)
        r = self.rota("POST", "/frentes/nova", {"nome": "  Padaria   do Bairro ", "tipo": "cliente"})
        self.assertEqual(r.status, 303)
        nova = next(f for f in self.ff.frentes if f["nome"] == "Padaria do Bairro")
        self.assertEqual(nova["tipo"], "cliente")
        self.assertIn(("Location", f"/frentes/{nova['id']}"), r.cabecalhos)
        n = len(self.ff.frentes)
        self.assertEqual(self.rota("POST", "/frentes/nova", {"nome": "   "}).status, 303)   # vazio não chama
        self.assertEqual(self.rota("POST", "/frentes/nova", {"nome": "X", "tipo": "<b>"}).status, 400)
        self.assertEqual(self.rota("POST", "/frentes/nova", {"nome": "Padaria do Bairro"}).status, 502)  # repetido
        self.assertEqual(self.rota("GET", "/frentes/nova").status, 405)
        self.assertEqual(self.rota("POST", "/frentes/nova", {"nome": "Y"}, cab={"sec-fetch-site": "cross-site"}).status, 403)
        self.assertEqual(len(self.ff.frentes), n)

    def test_acoes_recusam_get_e_origem_de_fora(self):
        it = self.ff.semear_item(texto="x", frente_id=1)
        self.assertEqual(self.rota("GET", f"/frentes/item/{it['id']}/feito").status, 405)
        r = self.rota("POST", f"/frentes/item/{it['id']}/feito", cab={"host": "127.0.0.1:8120",
                                                                     "origin": "http://evil.example"})
        self.assertEqual(r.status, 403)
        r = self.rota("POST", "/frentes/captura", {"texto": "x"}, cab={"sec-fetch-site": "cross-site"})
        self.assertEqual(r.status, 403)
        self.assertEqual(self.ff.itens[it["id"]]["status"], "aberto")

    def test_frentes_fora_do_ar_no_detalhe_e_na_acao(self):
        it = self.ff.semear_item(texto="x", frente_id=1)
        self.ff.parar()
        r = self.rota("GET", "/frentes/1")
        self.assertIn("Frentes indisponível", texto(r))
        self.assertEqual(self.rota("POST", f"/frentes/item/{it['id']}/feito").status, 502)


class TestArquivarEPessoal(Base):
    """Pedido 41: seção Pessoal na árvore; arquivar/desarquivar frente com registro local e Desfazer."""

    def rota(self, metodo, caminho, corpo=None, query=None):
        exatas, padroes = descobrir_rotas()
        h = _despachar(exatas, padroes, caminho)
        self.assertIsNotNone(h, caminho)
        return h(self.req(metodo, caminho, corpo=corpo, query=query))

    def test_secao_pessoal_na_arvore(self):
        t = texto(self.rota("GET", "/frentes"))
        self.assertIn('<div class="secao-lista">Pessoal</div>', t)
        self.assertLess(t.index('<div class="secao-lista">Interno</div>') if "Interno</div>" in t else 0,
                        t.index('<div class="secao-lista">Pessoal</div>'))
        self.assertIn('href="/frentes/2"', t)                    # "Casa" (tipo pessoal) mora na seção
        self.assertNotIn('<details class="fr-arquivadas"', t)     # sem arquivadas, o grupo não aparece (o CSS fica)

    def test_arquivar_some_da_arvore_e_da_ia_e_desarquivar_volta(self):
        self.banco.vincular("5592999@s.whatsapp.net", 2, "dono")
        r = self.rota("POST", "/frentes/2/arquivar")
        self.assertEqual((r.status, dict(r.cabecalhos)["Location"]), (303, "/frentes?arquivada=2"))
        self.assertEqual(self.ff.frentes_patches, [{"id": 2, "corpo": {"status": "arquivada"}}])
        t = texto(self.rota("GET", "/frentes", query={"arquivada": "2"}))
        self.assertNotIn('href="/frentes/2"', t.split('<details class="fr-arquivadas"')[0])
        self.assertIn('<details class="fr-arquivadas"', t)
        self.assertIn('<span class="n">1</span>', t)
        self.assertIn('action="/frentes/2/desarquivar"', t)
        self.assertIn("Frente arquivada.", t)                      # toast com Desfazer
        self.assertIn('class="toast-acao"', t)
        self.assertEqual([a["frente_id"] for a in self.banco.frentes_arquivadas()], [2])
        from app import lotes
        self.assertIn("foi arquivada", lotes.linha_da_frente(self.banco, self.ponte, "5592999@s.whatsapp.net"))
        # detalhe da arquivada: aviso + Desarquivar, sem itens nem ações
        t = texto(self.rota("GET", "/frentes/2"))
        self.assertIn("fr-arquivada-aviso", t)
        self.assertIn("Desarquivar", t)
        r = self.rota("POST", "/frentes/2/desarquivar", {"volta": "/frentes"})
        self.assertEqual((r.status, dict(r.cabecalhos)["Location"]), (303, "/frentes?desarquivada=2"))
        t = texto(self.rota("GET", "/frentes", query={"desarquivada": "2"}))
        self.assertIn('href="/frentes/2"', t)
        self.assertNotIn('<details class="fr-arquivadas"', t)
        self.assertIn("Frente de volta às ativas.", t)
        self.assertEqual(self.banco.frentes_arquivadas(), [])
        self.assertEqual(lotes.linha_da_frente(self.banco, self.ponte, "5592999@s.whatsapp.net"),
                         "frente: 2 · Casa (marcada pelo dono)")

    def test_detalhe_tem_menu_com_arquivar(self):
        t = texto(self.rota("GET", "/frentes/2"))
        self.assertIn('action="/frentes/2/arquivar"', t)
        self.assertIn("Arquivar frente", t)

    def test_arquivar_inexistente_404_e_so_post(self):
        self.assertEqual(self.rota("POST", "/frentes/999/arquivar").status, 404)
        self.assertEqual(self.rota("GET", "/frentes/2/arquivar").status, 405)
        self.assertEqual(self.ff.frentes_patches, [])

    def test_frentes_fora_do_ar_nao_arquiva_nem_grava_local(self):
        self.ff.parar()
        r = self.rota("POST", "/frentes/2/arquivar")
        self.assertIn(r.status, (502, 503))
        self.assertEqual(self.banco.frentes_arquivadas(), [])


class TestRegrasPuras(unittest.TestCase):
    def test_iniciais_e_prazo(self):
        self.assertEqual(frentes.iniciais("CBA Laboratório"), "CL")
        self.assertEqual(frentes.iniciais("Velta"), "VE")
        self.assertEqual(frentes.data_amigavel("2026-11-05", "2026-11-06"), ("atrasado, 05/11", "ruim"))
        self.assertEqual(frentes.data_amigavel("2026-11-06", "2026-11-06"), ("hoje", "alerta"))
        self.assertEqual(frentes.data_amigavel("2026-11-07", "2026-11-06"), ("amanhã", "alerta"))

    def test_agrupar_urgencia_ganha_de_procedencia(self):
        g = frentes.agrupar([{"id": 1, "prazo": "2026-11-01", "confirmado": 0},
                             {"id": 2, "prazo": None, "confirmado": 0},
                             {"id": 3, "prazo": "2026-12-01", "confirmado": 1}], "2026-11-06")
        self.assertEqual([i["id"] for i in g["atrasados"]], [1])
        self.assertEqual([i["id"] for i in g["confirmar"]], [2])
        self.assertEqual([i["id"] for i in g["proximos"]], [3])


if __name__ == "__main__":
    unittest.main()


class TestNovaFrenteEmbutido(Base):
    """Pedido 47: no Frentes embutido (SQLite, sem app externo) a primeira frente nasce pelo painel."""

    def test_painel_vazio_cria_a_primeira_frente(self):
        import tempfile
        from app.frentes_local import FrentesLocal
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ctx.ponte = FrentesLocal(Path(tmp.name) / "frentes.db")
        exatas, padroes = descobrir_rotas()
        get = lambda c: _despachar(exatas, padroes, c)(self.req("GET", c))  # noqa: E731
        t = texto(get("/frentes"))
        self.assertIn("Nenhuma frente ainda", t)
        self.assertIn('action="/frentes/nova"', t)
        r = _despachar(exatas, padroes, "/frentes/nova")(self.req("POST", "/frentes/nova",
                                                                  corpo={"nome": "Casa", "tipo": "pessoal"}))
        self.assertIn(("Location", "/frentes/1"), r.cabecalhos)
        self.assertEqual(get("/frentes/1").status, 200)
        self.assertIn('href="/frentes/1"', texto(get("/frentes")))
