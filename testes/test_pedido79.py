"""Pedido 79 (D-80): seções dentro da frente. O Greg guarda a divisão; a IA escolhe a seção na leitura, com um resumo
montado na hora dos itens em aberto; a pessoa cria, renomeia, apaga e move pela tela; o conector cria e move.
Nada sai do Mac nos testes."""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import lancamento, lotes, secoes  # noqa: E402
from app import mcp_ferramentas as mf  # noqa: E402
from app.leitor_api import FUNCOES, argv_da_ponte  # noqa: E402
from app.ponte import frentes as frentes_cmd  # noqa: E402
from app.server import _despachar, descobrir_rotas  # noqa: E402
from testes.test_mcp import BaseMcp, chamar  # noqa: E402
from testes.test_paginas import XSS, Base as BasePaginas, texto  # noqa: E402
from testes.test_ponte import AMANHA, LONGE, Base as BasePonte  # noqa: E402

RAIZ = Path(__file__).resolve().parents[1]


def sem_linha_do_tempo(t: str) -> str:
    """Pedido 86: a linha do tempo, recolhida no fim da frente, repete os itens de propósito; as contagens daqui são das
    listas que agem sobre o item."""
    return re.sub(r'<details class="fr-grupo fr-linha">.*?</details>', "", t, flags=re.S)


class TestBancoSecoes(BasePonte):
    def test_cria_lista_renomeia_e_nome_repetido_e_recusado(self):
        a = self.banco.criar_secao(1, "Site", "o site público")
        b = self.banco.criar_secao(1, "Vídeos")
        self.banco.criar_secao(2, "Site")                                # mesmo nome em outra frente pode
        self.assertEqual([(s["id"], s["nome"], s["descricao"]) for s in self.banco.secoes(1)],
                         [(a, "Site", "o site público"), (b, "Vídeos", "")])
        with self.assertRaises(ValueError):
            self.banco.criar_secao(1, "site")                            # maiúscula não diferencia
        with self.assertRaises(ValueError):
            self.banco.alterar_secao(b, nome="SITE")
        self.banco.alterar_secao(b, nome="Filmes", descricao="teasers e série")
        self.assertEqual(self.banco.secao(b)["descricao"], "teasers e série")
        self.assertEqual(len(self.banco.secoes()), 3)

    def test_ia_nao_troca_o_que_a_pessoa_pos_e_apagar_solta_os_itens(self):
        a, b = self.banco.criar_secao(1, "Site"), self.banco.criar_secao(1, "Vídeos")
        self.banco.por_na_secao(10, a)                                   # a pessoa pôs
        self.banco.por_na_secao(10, b, origem="ia")                      # a IA não tira
        self.banco.por_na_secao(11, a, origem="ia")
        self.banco.por_na_secao(11, b, origem="ia")                      # a IA troca o que ela mesma pôs
        self.assertEqual(self.banco.secao_dos_itens(), {10: a, 11: b})
        self.banco.por_na_secao(11, a)                                   # a pessoa corrige
        self.assertEqual(self.banco.secao_dos_itens()[11], a)
        self.assertEqual(self.banco.apagar_secao(a), 2)
        self.assertEqual(self.banco.secao_dos_itens(), {})
        self.assertIsNone(self.banco.secao(a))
        self.banco.por_na_secao(10, None)                                # tirar de quem não está em seção não falha

    def test_nome_e_descricao_sao_limpos(self):
        self.assertEqual(secoes.limpar_nome("  Site \n novo "), "Site novo")
        for ruim in ("", "   ", "x" * (secoes.NOME_MAX + 1)):
            with self.assertRaises(ValueError):
                secoes.limpar_nome(ruim)
        with self.assertRaises(ValueError):
            secoes.limpar_descricao("x" * (secoes.DESCRICAO_MAX + 1))


class TestResumoParaAIA(BasePonte):
    def test_resumo_traz_secoes_descricao_exemplos_e_sem_secao(self):
        site = self.banco.criar_secao(1, "Site", "tudo do site público")
        videos = self.banco.criar_secao(1, "Vídeos")
        a = self.fake.semear_item(texto="trocar o link do rodapé", frente_id=1)
        b = self.fake.semear_item(texto="publicar a página de preços", frente_id=1)
        solto = self.fake.semear_item(texto="pagar o contador", frente_id=1)
        self.fake.semear_item(texto="renovar o domínio", frente_id=2)
        self.banco.por_na_secao(a["id"], site)
        self.banco.por_na_secao(b["id"], site)
        md = secoes.resumo_md(self.banco, self.ponte)
        self.assertTrue(md.startswith(secoes.TITULO))
        self.assertIn(f"- seção {site} · Site (tudo do site público) · 2 em aberto · ex.: publicar a página de preços | trocar o link do rodapé", md)
        self.assertIn(f"- seção {videos} · Vídeos · vazia", md)
        self.assertIn("- sem seção · 1 em aberto · ex.: pagar o contador", md)
        self.assertIn("- 1 em aberto · ex.: renovar o domínio", md)       # frente sem seção: só os exemplos
        self.assertLess(md.index("### frente 1 "), md.index("### frente 2 "))   # quem tem seção vem primeiro

    def test_e_dinamico_mover_o_item_muda_o_exemplo(self):
        site, videos = self.banco.criar_secao(1, "Site"), self.banco.criar_secao(1, "Vídeos")
        it = self.fake.semear_item(texto="gravar o teaser", frente_id=1)
        self.banco.por_na_secao(it["id"], site, origem="ia")
        self.assertIn(f"seção {site} · Site · 1 em aberto · ex.: gravar o teaser", secoes.resumo_md(self.banco, self.ponte))
        self.banco.por_na_secao(it["id"], videos)                        # a pessoa corrigiu
        md = secoes.resumo_md(self.banco, self.ponte)
        self.assertIn(f"seção {site} · Site · vazia", md)
        self.assertIn(f"seção {videos} · Vídeos · 1 em aberto · ex.: gravar o teaser", md)

    def test_exemplo_longo_e_cortado_e_o_bloco_tem_teto(self):
        self.fake.semear_item(texto="x" * 500, frente_id=1)
        md = secoes.resumo_md(self.banco, self.ponte)
        self.assertNotIn("x" * (secoes.EXEMPLO_MAX + 1), md)
        for i in range(400):
            self.banco.criar_secao(2, f"seção de número {i:03d} com um nome comprido", "d" * 150)
        md = secoes.resumo_md(self.banco, self.ponte)
        self.assertLessEqual(len(md), secoes.RESUMO_MAX + 200)
        self.assertIn("sem resumo por falta de espaço", md)

    def test_entra_no_contexto_do_ciclo_e_frentes_fora_nao_derruba(self):
        self.banco.criar_secao(1, "Site")
        self.fake.semear_item(texto="trocar o banner", frente_id=1)
        ctx = lotes.contexto_do_ciclo(self.banco, self.ponte)
        self.assertIn(secoes.TITULO, ctx)
        self.assertLess(ctx.index("## Frentes"), ctx.index(secoes.TITULO))
        self.assertLess(ctx.index(secoes.TITULO), ctx.index("## Itens do WhatsApp no registro local"))

        class Fora:
            def estado(self):
                from app.erros import FrentesIndisponivel
                raise FrentesIndisponivel("fora")
        self.assertEqual(secoes.resumo_md(self.banco, Fora()), "")
        self.assertEqual(secoes.resumo_md(object(), self.ponte).count("- seção "), 0)   # banco sem seções não quebra

    def test_sem_secao_e_sem_item_nao_gera_bloco(self):
        self.assertEqual(secoes.resumo_md(self.banco, self.ponte), "")


class TestIAEscolheASecao(BasePonte):
    def test_sobe_na_hora_ja_na_secao(self):
        sid = self.banco.criar_secao(1, "Site")
        r = self.cls("m1", "demanda", resumo="trocar o banner", frente=1, secao=sid, prazo=AMANHA)
        self.assertEqual((r["lancado"], r["secao"]), (True, sid))
        self.assertEqual(self.banco.secao_dos_itens(), {r["item_id"]: sid})

    def test_provisorio_guarda_a_secao_e_entra_nela_quando_sobe(self):
        sid = self.banco.criar_secao(1, "Site")
        r = self.cls("m1", "demanda", resumo="rever o texto", frente=1, secao=sid, prazo=LONGE)
        self.assertTrue(r.get("provisorio"))
        self.assertEqual(self.banco.secao_dos_itens(), {})
        self.assertEqual(self.banco.classificacao(r["id"])["secao_id"], sid)
        fim = lancamento.lancar(self.banco, self.ponte, self.banco.classificacao(r["id"]))
        self.assertEqual(self.banco.secao_dos_itens(), {fim["item_id"]: sid})

    def test_secao_de_outra_frente_e_ignorada_e_avisada(self):
        de_outra = self.banco.criar_secao(2, "Financeiro")
        r = self.cls("m1", "demanda", resumo="x", frente=1, secao=de_outra, prazo=AMANHA)
        self.assertTrue(r["secao_invalida"])
        self.assertNotIn("secao", r)
        self.assertEqual(self.banco.secao_dos_itens(), {})
        r = self.cls("m2", "demanda", resumo="y", frente=1, secao=987654, prazo=AMANHA)
        self.assertTrue(r["secao_invalida"])

    def test_cartao_que_mudou_de_frente_na_revisao_sobe_sem_secao(self):
        sid = self.banco.criar_secao(1, "Site")
        r = self.cls("m1", "demanda", resumo="x", frente=1, secao=sid, prazo=LONGE)
        fim = lancamento.lancar(self.banco, self.ponte, self.banco.classificacao(r["id"]), frente_id=2)
        self.assertIsNone(fim["secao"])
        self.assertEqual(self.banco.secao_dos_itens(), {})

    def test_sem_secao_e_classe_que_nao_vira_item_ficam_como_antes(self):
        sid = self.banco.criar_secao(1, "Site")
        r = self.cls("m1", "demanda", resumo="x", frente=1, prazo=AMANHA)
        self.assertNotIn("secao", r)
        self.assertNotIn("secao_invalida", r)
        r = self.cls("m2", "aviso", resumo="só um aviso", frente=1, secao=sid)
        self.assertNotIn("secao", r)
        self.assertIsNone(self.banco.classificacao(r["id"])["secao_id"])

    def test_a_ia_de_api_e_o_comando_frentes_conhecem_a_secao(self):
        self.assertIn("secao", FUNCOES["classificar"][2])
        argv = argv_da_ponte("classificar", {"msg_id": "m1", "classe": "demanda", "resumo": "r", "confianca": "alta",
                                          "frente": 1, "secao": 7})
        self.assertIn("--secao=7", argv)
        sid = self.banco.criar_secao(1, "Site", "o site")
        import argparse
        r = frentes_cmd.executar(argparse.Namespace(), banco=self.banco, ponte=self.ponte)
        self.assertEqual(r["frentes"][0]["secoes"], [{"id": sid, "nome": "Site", "descricao": "o site"}])
        self.assertEqual(r["frentes"][1]["secoes"], [])

    def test_o_prompt_ensina_a_escolher(self):
        p = (RAIZ / "prompts" / "vigia.md").read_text(encoding="utf-8")
        self.assertIn("## Qual seção", p)
        self.assertIn("[--secao ID]", p)
        self.assertIn("O que tem em cada frente", p)


class TestTelaSecoes(BasePaginas):
    def rota(self, metodo, caminho, corpo=None):
        exatas, padroes = descobrir_rotas()
        h = _despachar(exatas, padroes, caminho)
        self.assertIsNotNone(h, caminho)
        return h(self.req(metodo, caminho, corpo=corpo))

    def test_frente_sem_secao_fica_como_antes_e_oferece_criar(self):
        self.ff.semear_item(texto="sem data", frente_id=1)
        t = sem_linha_do_tempo(texto(self.rota("GET", "/frentes/1")))
        self.assertIn("grupo-sem_prazo", t)
        self.assertIn('action="/frentes/1/secoes"', t)
        self.assertNotIn('class="fr-pasta"', t)
        self.assertNotIn('action="/frentes/1/secoes"', texto(self.rota("GET", "/frentes/inbox")))

    def test_cria_secao_e_a_frente_mostra_pastas(self):
        r = self.rota("POST", "/frentes/1/secoes", {"nome": " Site ", "descricao": "o site " + XSS})
        self.assertEqual((r.status, dict(r.cabecalhos)["Location"]), (303, "/frentes/1"))
        sid = self.banco.secoes(1)[0]["id"]
        dentro = self.ff.semear_item(texto="trocar o banner", frente_id=1, prazo="2020-01-01")
        self.ff.semear_item(texto="pagar o contador", frente_id=1)
        self.banco.por_na_secao(dentro["id"], sid)
        t = sem_linha_do_tempo(texto(self.rota("GET", "/frentes/1")))
        self.assertNotIn("<script>alert", t)
        self.assertIn(f'class="fr-pasta" href="/frentes/secao/{sid}"', t)
        self.assertIn("1 atrasado", t)                                   # a pasta avisa o que há dentro
        # pedido 82 (D-85): item atrasado de uma seção aparece também no bloco Hoje da raiz, dizendo de onde vem
        self.assertEqual(t.count("trocar o banner"), 1)
        self.assertLess(t.index('class="fr-grupo grupo-dodia"'), t.index("trocar o banner"))
        self.assertLess(t.index("trocar o banner"), t.index('class="fr-pastas"'))
        self.assertIn('class="fr-sem-secao"', t)
        self.assertLess(t.index('class="fr-sem-secao"'), t.index("pagar o contador"))
        self.assertIn(f'name="secao_id" value="{sid}"', t)               # o Mover do item solto oferece a seção
        self.assertIn("1 seção", t)

    def test_a_secao_abre_na_propria_janela(self):
        sid, outra = self.banco.criar_secao(1, "Site", "páginas " + XSS), self.banco.criar_secao(1, "Vídeos")
        dentro = self.ff.semear_item(texto="trocar o banner", frente_id=1)
        feito = self.ff.semear_item(texto="ja publicado", frente_id=1, status="feito")
        self.ff.semear_item(texto="pagar o contador", frente_id=1)
        for i in (dentro, feito):
            self.banco.por_na_secao(i["id"], sid)
        r = self.rota("GET", f"/frentes/secao/{sid}")
        t = texto(r)
        self.assertEqual(r.status, 200)
        self.assertNotIn("<script>alert", t)
        self.assertIn('class="fr-trilha"', t)
        self.assertIn('href="/frentes/1"', t)                            # trilha e botão de voltar para a frente
        self.assertIn("trocar o banner", t)
        self.assertIn("ja publicado", t)
        self.assertNotIn("pagar o contador", t)
        self.assertIn(f'name="volta" value="/frentes/secao/{sid}"', t)   # as ações voltam para a janela da seção
        self.assertIn(f'name="secao_id" value="{outra}"', t)             # Mover para a outra seção
        self.assertIn(">Sem seção<", t)                                  # e tirar da seção
        self.assertIn(f'action="/frentes/secao/{sid}/alterar"', t)
        self.assertIn(f'action="/frentes/secao/{sid}/apagar"', t)
        self.assertIn("Nada nesta seção ainda", texto(self.rota("GET", f"/frentes/secao/{outra}")))
        self.assertEqual(self.rota("GET", "/frentes/secao/987654").status, 404)
        # mover e marcar feito de dentro da seção voltam para ela
        r = self.rota("POST", f"/frentes/item/{dentro['id']}/feito", {"volta": f"/frentes/secao/{sid}"})
        self.assertEqual(dict(r.cabecalhos)["Location"], f"/frentes/secao/{sid}")
        r = self.rota("POST", f"/frentes/secao/{sid}/alterar", {"nome": "Site novo", "volta": f"/frentes/secao/{sid}"})
        self.assertEqual(dict(r.cabecalhos)["Location"], f"/frentes/secao/{sid}")

    def test_nome_vazio_repetido_e_frente_inexistente(self):
        self.assertEqual(self.rota("POST", "/frentes/1/secoes", {"nome": "  "}).status, 400)
        self.rota("POST", "/frentes/1/secoes", {"nome": "Site"})
        r = self.rota("POST", "/frentes/1/secoes", {"nome": "site"})
        self.assertEqual(r.status, 400)
        self.assertIn("já existe uma seção", texto(r))
        self.assertEqual(self.rota("POST", "/frentes/999/secoes", {"nome": "X"}).status, 404)
        self.assertEqual(len(self.banco.secoes()), 1)

    def test_move_item_para_secao_tira_e_recusa_secao_de_outra_frente(self):
        sid, de_outra = self.banco.criar_secao(1, "Site"), self.banco.criar_secao(2, "Financeiro")
        it = self.ff.semear_item(texto="x", frente_id=1)
        r = self.rota("POST", f"/frentes/item/{it['id']}/secao", {"secao_id": str(sid), "volta": "/frentes/1"})
        self.assertEqual((r.status, dict(r.cabecalhos)["Location"]), (303, "/frentes/1"))
        self.assertEqual(self.banco.secao_dos_itens(), {it["id"]: sid})
        self.assertEqual(self.rota("POST", f"/frentes/item/{it['id']}/secao", {"secao_id": str(de_outra)}).status, 400)
        self.assertEqual(self.rota("POST", f"/frentes/item/{it['id']}/secao", {"secao_id": "987654"}).status, 404)
        self.assertEqual(self.rota("POST", f"/frentes/item/{it['id']}/secao", {"secao_id": "abc"}).status, 400)
        self.assertEqual(self.banco.secao_dos_itens(), {it["id"]: sid})
        self.rota("POST", f"/frentes/item/{it['id']}/secao", {"secao_id": "0"})
        self.assertEqual(self.banco.secao_dos_itens(), {})

    def test_item_que_mudou_de_frente_aparece_sem_secao_la(self):
        sid = self.banco.criar_secao(1, "Site")
        self.banco.criar_secao(2, "Financeiro")
        it = self.ff.semear_item(texto="andarilho", frente_id=1)
        self.banco.por_na_secao(it["id"], sid)
        self.rota("POST", f"/frentes/item/{it['id']}/mover", {"frente_id": "2"})
        t = texto(self.rota("GET", "/frentes/2"))
        self.assertLess(t.index('class="fr-sem-secao"'), t.index("andarilho"))

    def test_anotar_dentro_da_secao(self):
        sid = self.banco.criar_secao(1, "Site")
        r = self.rota("POST", "/frentes/captura", {"texto": "trocar o favicon", "frente_id": "1", "secao_id": str(sid),
                                                   "volta": "/frentes/1"})
        self.assertEqual(r.status, 303)
        novo = max(self.ff.itens)
        self.assertEqual(self.banco.secao_dos_itens(), {novo: sid})
        self.rota("POST", "/frentes/captura", {"texto": "sem seção", "frente_id": "1", "volta": "/frentes/1"})
        self.assertEqual(len(self.banco.secao_dos_itens()), 1)

    def test_renomeia_e_apaga_sem_apagar_item(self):
        sid = self.banco.criar_secao(1, "Site")
        it = self.ff.semear_item(texto="fica", frente_id=1)
        self.banco.por_na_secao(it["id"], sid)
        r = self.rota("POST", f"/frentes/secao/{sid}/alterar", {"nome": "Site novo", "descricao": "páginas públicas"})
        self.assertEqual((r.status, dict(r.cabecalhos)["Location"]), (303, "/frentes/1"))
        self.assertEqual((self.banco.secao(sid)["nome"], self.banco.secao(sid)["descricao"]), ("Site novo", "páginas públicas"))
        self.assertEqual(self.rota("POST", f"/frentes/secao/{sid}/alterar", {"nome": ""}).status, 400)
        self.assertEqual(self.rota("POST", "/frentes/secao/987654/alterar", {"nome": "x"}).status, 404)
        r = self.rota("POST", f"/frentes/secao/{sid}/apagar")
        self.assertEqual(r.status, 303)
        self.assertEqual((self.banco.secoes(), self.banco.secao_dos_itens()), ([], {}))
        self.assertEqual(self.ff.itens[it["id"]]["status"], "aberto")
        self.assertIn("fica", texto(self.rota("GET", "/frentes/1")))
        self.assertEqual(self.rota("POST", f"/frentes/secao/{sid}/apagar").status, 404)


class TestConectorSecoes(BaseMcp):
    def setUp(self):
        super().setUp()
        self.ligar("demandas")
        self.fid = self.ponte.frentes()[0]["id"]

    def test_cria_lista_e_move_pelo_conector(self):
        erro, dados, _ = chamar(self.ctx, "greg_secao_criar", frente_id=self.fid, nome="Site", descricao="o site")
        self.assertFalse(erro, dados)
        sid = dados["secao"]["id"]
        erro, dados, _ = chamar(self.ctx, "greg_secao_criar", frente_id=self.fid, nome="site")
        self.assertTrue(erro)
        erro, dados, _ = chamar(self.ctx, "greg_frentes")
        self.assertEqual(next(f for f in dados["frentes"] if f["id"] == self.fid)["secoes"],
                         [{"id": sid, "nome": "Site", "descricao": "o site"}])
        item = self.ponte.capturar("trocar o banner", frente_id=self.fid)["itens"][0]["id"]
        erro, dados, _ = chamar(self.ctx, "greg_item_secao", item_id=item, secao_id=sid)
        self.assertFalse(erro, dados)
        erro, dados, _ = chamar(self.ctx, "greg_frente_itens", frente_id=self.fid)
        self.assertEqual([(i["id"], i.get("secao")) for i in dados["itens"]], [(item, "Site")])
        self.assertEqual(dados["secoes"], [{"id": sid, "nome": "Site"}])
        erro, dados, _ = chamar(self.ctx, "greg_item_secao", item_id=item, secao_id=987654)
        self.assertTrue(erro)
        erro, dados, _ = chamar(self.ctx, "greg_item_secao", item_id=item)
        self.assertFalse(erro, dados)
        self.assertEqual(self.banco.secao_dos_itens(), {})

    def test_rotas_novas_estao_na_cobertura(self):
        self.assertEqual(mf.COBERTURA["/frentes/{id}/secoes"], "greg_secao_criar")
        self.assertEqual(mf.COBERTURA["/frentes/item/{id}/secao"], "greg_item_secao")
        self.assertEqual(mf.COBERTURA["/frentes/secao/{id}"], "greg_frente_itens")
        for rota in ("/frentes/secao/{id}/alterar", "/frentes/secao/{id}/apagar"):
            self.assertTrue(mf.COBERTURA[rota].startswith("tela:"))


del BasePonte, BasePaginas, BaseMcp

if __name__ == "__main__":
    unittest.main()
