"""Pedido 82 (D-85): na raiz da frente com seções, um bloco Hoje reúne o que está atrasado ou vence hoje em todas as
seções e fora delas. Cada item diz de que seção vem e pode ser concluído ali; o item não muda de seção; as pastas
continuam embaixo; sem item do dia o bloco não aparece; frente sem seção fica como era.
Nada sai do Mac nos testes."""
import re
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.server import _despachar, descobrir_rotas  # noqa: E402
from testes.test_paginas import XSS, Base as BasePaginas, texto  # noqa: E402

HOJE = date.today().isoformat()
ONTEM = (date.today() - timedelta(days=1)).isoformat()
AMANHA = (date.today() + timedelta(days=1)).isoformat()
BLOCO = 'class="fr-grupo grupo-dodia"'      # o nome da classe também está no CSS da página


def sem_linha_do_tempo(t: str) -> str:
    """Pedido 86: a linha do tempo, recolhida no fim da frente, repete os itens de propósito; as contagens daqui são das
    listas que agem sobre o item."""
    return re.sub(r'<details class="fr-grupo fr-linha">.*?</details>', "", t, flags=re.S)


class TestBlocoDoDia(BasePaginas):
    def rota(self, metodo, caminho, corpo=None):
        exatas, padroes = descobrir_rotas()
        h = _despachar(exatas, padroes, caminho)
        self.assertIsNotNone(h, caminho)
        return h(self.req(metodo, caminho, corpo=corpo))

    def item(self, texto_, prazo=None, secao=None, **campos):
        it = self.ff.semear_item(texto=texto_, frente_id=1, prazo=prazo, **campos)
        if secao is not None:
            self.banco.por_na_secao(it["id"], secao)
        return it

    def bloco(self, t: str) -> str:
        """Só o HTML do bloco Hoje (do começo dele até as pastas)."""
        self.assertIn(BLOCO, t)
        return t[t.index(BLOCO):t.index('class="fr-pastas"')]

    def test_item_de_hoje_em_duas_secoes_aparece_junto_dizendo_de_onde_vem(self):
        site, videos = self.banco.criar_secao(1, "Site"), self.banco.criar_secao(1, "Vídeos")
        a = self.item("trocar o banner", HOJE, site)
        b = self.item("gravar o roteiro", HOJE, videos)
        t = sem_linha_do_tempo(texto(self.rota("GET", "/frentes/1")))
        bloco = self.bloco(t)
        self.assertIn("trocar o banner", bloco)
        self.assertIn("gravar o roteiro", bloco)
        self.assertIn(f'<a class="de fr-onde" href="/frentes/secao/{site}">seção Site</a>', bloco)
        self.assertIn(f'<a class="de fr-onde" href="/frentes/secao/{videos}">seção Vídeos</a>', bloco)
        self.assertIn('<span class="n">2</span>', bloco)
        # as pastas continuam embaixo, contando os mesmos itens, e ninguém mudou de seção
        self.assertIn(f'class="fr-pasta" href="/frentes/secao/{site}"', t)
        self.assertIn(f'class="fr-pasta" href="/frentes/secao/{videos}"', t)
        self.assertEqual(self.banco.secao_dos_itens(), {a["id"]: site, b["id"]: videos})
        self.assertEqual((t.count("trocar o banner"), t.count("gravar o roteiro")), (1, 1))

    def test_atrasado_em_secao_entra_no_bloco_antes_do_de_hoje(self):
        site = self.banco.criar_secao(1, "Site")
        self.item("entrega de hoje", HOJE, site)
        self.item("ficou para tras", ONTEM, site)
        bloco = self.bloco(texto(self.rota("GET", "/frentes/1")))
        self.assertLess(bloco.index("ficou para tras"), bloco.index("entrega de hoje"))
        self.assertIn("atrasado, ", bloco)                                 # a pílula de prazo de sempre
        self.assertEqual(bloco.count("seção Site</a>"), 2)

    def test_frente_sem_secao_fica_como_era(self):
        self.item("entrega de hoje", HOJE)
        self.item("ficou para tras", ONTEM)
        t = sem_linha_do_tempo(texto(self.rota("GET", "/frentes/1")))
        self.assertNotIn(BLOCO, t)
        self.assertIn('class="fr-grupo grupo-atrasados"', t)
        self.assertIn('class="fr-grupo grupo-hoje"', t)
        self.assertNotIn('class="de fr-onde"', t)

    def test_sem_item_do_dia_o_bloco_nao_aparece(self):
        site = self.banco.criar_secao(1, "Site")
        self.item("para amanha", AMANHA, site)
        self.item("sem data na secao", None, site)
        self.item("sem data e sem secao")
        t = sem_linha_do_tempo(texto(self.rota("GET", "/frentes/1")))
        self.assertNotIn(BLOCO, t)
        self.assertNotIn("para amanha", t)                                 # o que não é do dia segue só dentro da pasta
        self.assertIn("sem data e sem secao", t)

    def test_item_do_dia_sem_secao_sobe_para_o_bloco_e_nao_se_repete_embaixo(self):
        self.banco.criar_secao(1, "Site")
        self.item("solto de hoje", HOJE)
        self.item("solto atrasado", ONTEM)
        self.item("solto sem data")
        t = sem_linha_do_tempo(texto(self.rota("GET", "/frentes/1")))
        bloco = self.bloco(t)
        self.assertIn("solto de hoje", bloco)
        self.assertIn("solto atrasado", bloco)
        self.assertNotIn('class="de fr-onde"', bloco)                                 # sem seção, sem rótulo de seção
        self.assertEqual((t.count("solto de hoje"), t.count("solto atrasado")), (1, 1))
        depois = t[t.index('class="fr-sem-secao"'):]
        self.assertIn("solto sem data", depois)
        self.assertNotIn('grupo-atrasados"', depois)
        self.assertNotIn('grupo-hoje"', depois)

    def test_so_item_do_dia_sem_secao_nao_deixa_titulo_sem_secao_vazio(self):
        self.banco.criar_secao(1, "Site")
        self.item("solto de hoje", HOJE)
        t = sem_linha_do_tempo(texto(self.rota("GET", "/frentes/1")))
        self.assertIn("solto de hoje", self.bloco(t))
        self.assertNotIn('class="fr-sem-secao"', t)

    def test_conclui_no_bloco_e_volta_para_a_raiz_da_frente(self):
        site = self.banco.criar_secao(1, "Site")
        it = self.item("trocar o banner", HOJE, site)
        bloco = self.bloco(texto(self.rota("GET", "/frentes/1")))
        self.assertIn(f'action="/frentes/item/{it["id"]}/feito"', bloco)
        self.assertIn('name="volta" value="/frentes/1"', bloco)
        r = self.rota("POST", f"/frentes/item/{it['id']}/feito", {"volta": "/frentes/1"})
        self.assertEqual((r.status, dict(r.cabecalhos)["Location"]), (303, "/frentes/1"))
        t = sem_linha_do_tempo(texto(self.rota("GET", "/frentes/1")))
        self.assertNotIn(BLOCO, t)                                 # era o único do dia: o bloco some
        self.assertEqual(self.banco.secao_dos_itens().get(it["id"]), site)  # concluir não tira da seção

    def test_mover_no_bloco_oferece_as_outras_secoes_e_tirar_da_secao(self):
        site, videos = self.banco.criar_secao(1, "Site"), self.banco.criar_secao(1, "Vídeos")
        it = self.item("trocar o banner", HOJE, site)
        bloco = self.bloco(texto(self.rota("GET", "/frentes/1")))
        self.assertIn(f'form="fr-sc-{it["id"]}" name="secao_id" value="{videos}"', bloco)
        self.assertNotIn(f'form="fr-sc-{it["id"]}" name="secao_id" value="{site}"', bloco)   # não oferece a própria
        self.assertIn(f'form="fr-sc-{it["id"]}" name="secao_id" value="0"', bloco)           # Sem seção

    def test_nome_de_secao_com_html_sai_escapado(self):
        sid = self.banco.criar_secao(1, "Site " + XSS)
        self.item("trocar o banner", HOJE, sid)
        t = sem_linha_do_tempo(texto(self.rota("GET", "/frentes/1")))
        self.assertIn("seção Site", self.bloco(t))
        self.assertNotIn("<script>alert", t)

    def test_item_preso_a_secao_de_outra_frente_entra_sem_rotulo(self):
        self.banco.criar_secao(1, "Site")
        de_outra = self.banco.criar_secao(2, "Financeiro")
        self.item("andarilho de hoje", HOJE, de_outra)
        bloco = self.bloco(texto(self.rota("GET", "/frentes/1")))
        self.assertIn("andarilho de hoje", bloco)
        self.assertNotIn('class="de fr-onde"', bloco)

    def test_janela_da_secao_e_caixa_de_entrada_nao_ganham_o_bloco(self):
        site = self.banco.criar_secao(1, "Site")
        self.item("trocar o banner", HOJE, site)
        self.ff.semear_item(texto="na caixa", frente_id=None, prazo=HOJE)
        self.assertNotIn(BLOCO, texto(self.rota("GET", f"/frentes/secao/{site}")))
        self.assertNotIn(BLOCO, texto(self.rota("GET", "/frentes/inbox")))


if __name__ == "__main__":
    unittest.main()
