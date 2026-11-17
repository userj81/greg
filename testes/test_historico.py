"""Página /historico (pedido 46, D-58): conversas por situação à esquerda, linha do tempo à direita.
Quem chama: unittest. Mesmo contexto falso das outras páginas (testes.test_paginas.Base); nenhuma rede real.
O que não faz: não sobe app/server.py nem toca o vigia.db real.
"""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.paginas import _comum as c, historico  # noqa: E402
from testes.test_paginas import XSS, Base, texto  # noqa: E402

HORA, DIA = 3600, 86400
ANA, RUI, BIA = "5592911110001@s.whatsapp.net", "5592911110002@s.whatsapp.net", "5592911110003@s.whatsapp.net"
ROTA = historico.ROTAS["/historico"]


class TestHistorico(Base):
    def analise(self, msg_id, conversa, contato, ha, situacao, resumo=None):
        self.banco.gravar_analise(msg_id=msg_id, conversa=conversa, contato=contato, ts=int(time.time() - ha),
                                  resumo=resumo or f"resumo {msg_id}", situacao=situacao)

    def semear(self):
        self.analise("a1", ANA, "Ana Paula", 3 * DIA, "aguarda_dono", "Ana pediu o orçamento revisado")
        self.analise("a2", ANA, "Ana Paula", 2 * HORA, "encerrada", "Dono mandou o orçamento e ela agradeceu")
        self.analise("r1", RUI, "Rui", 26 * HORA, "aguarda_dono", "Rui perguntou se a reunião de quinta está de pé")
        self.analise("b1", BIA, "Bia", HORA, "aguarda_contato", "Dono pediu o contrato e a Bia ficou de mandar")
        self.analise("v1", RUI, "Rui", 20 * DIA, "aguarda_dono", "assunto antigo")   # fora dos 7 dias

    def get(self, **query):
        return ROTA(self.req("GET", "/historico", query=query))

    # ---- estados antes do caminho feliz

    def test_primeira_vez_sem_nenhuma_leitura(self):
        r = self.get()
        t = texto(r)
        self.assertEqual(r.status, 200)
        self.assertIn("<b>Nenhuma leitura registrada</b>", t)
        self.assertIn("Cada leitura grava aqui o que a conversa trouxe e em que pé ficou", t)
        self.assertIn("<b>O histórico começa na próxima leitura</b>", t)
        self.assertIn(">Nenhuma conversa no período<", t)
        self.assertEqual(t.count('class="hi-linha"'), 0)

    def test_vazio_no_periodo_orienta_a_ampliar(self):
        self.analise("v1", RUI, "Rui", 20 * DIA, "aguarda_dono", "assunto antigo")
        t = texto(self.get())
        self.assertIn("<b>Nada neste período</b><span>Escolha um período maior</span>", t)
        self.assertNotIn("assunto antigo", t)
        self.assertIn("assunto antigo", texto(self.get(dias="30")))

    def test_banco_ilegivel_diz_o_que_houve_e_oferece_tentar_de_novo(self):
        def quebra(**kw):
            raise RuntimeError("database is locked: texto interno que não pode vazar")
        self.banco.analises = quebra
        r = self.get(dias="30")
        t = texto(r)
        self.assertEqual(r.status, 500)
        self.assertIn("Não deu para ler o histórico agora.", t)
        self.assertIn('<a class="btn" href="/historico?dias=30">Tentar de novo</a>', t)
        self.assertNotIn("database is locked", t)

    def test_denso_corta_e_avisa_no_rodape(self):
        for i in range(5):
            self.analise(f"m{i}", ANA, "Ana", i * 60 + 60, "encerrada")
        antigo = historico.LIMITE
        historico.LIMITE = 3
        self.addCleanup(setattr, historico, "LIMITE", antigo)
        t = texto(self.get())
        self.assertEqual(t.count('class="hi-linha"'), 3)
        self.assertIn("Mostrando as 3 leituras mais recentes", t)

    # ---- caminho feliz

    def test_tudo_conversas_por_situacao_e_linha_do_tempo(self):
        self.semear()
        self.classificar(msg_id="c1", conversa=ANA, item_id=7)                      # no Frentes
        seg = self.classificar(msg_id="c2", conversa=RUI)
        self.banco.rebaixar(seg, "aviso", "só conversa")                            # segurado pela IA
        self.classificar(msg_id="c3", conversa=BIA)                                 # provisório
        t = texto(self.get())
        lista, det = t.split('aria-label="Linha do tempo"')
        # lista: "Todas" marcada, depois as três seções na ordem de quem precisa dele primeiro
        self.assertIn('<a class="item-lista hi-tudo ativo" href="/historico" aria-current="true">', lista)
        self.assertIn("4 leituras no período", lista)
        ordem = [lista.index(x) for x in ('Aguardando você <span class="hi-n">1</span>', ">Rui<",
                                          'Aguardando o contato <span class="hi-n">1</span>', ">Bia<",
                                          'Encerradas <span class="hi-n">1</span>', ">Ana Paula<")]
        self.assertEqual(ordem, sorted(ordem))
        self.assertIn(f'href="/historico?c={ANA.replace("@", "%40")}"', lista)
        self.assertIn(">3 conversas, 1 aguarda você<", lista)
        # linha do tempo: nome em negrito, o que houve e a situação; só a pendência ATUAL ganha a pílula
        self.assertEqual(det.count('class="hi-linha"'), 4)
        self.assertIn('<b class="hi-nome">Rui</b><span class="pilula">aguarda você</span>'
                      '<p class="hi-txt">Rui perguntou se a reunião de quinta está de pé</p>', det)
        self.assertIn('<b class="hi-nome">Ana Paula</b><span class="meta-linha">aguardava você</span>'
                      '<p class="hi-txt">Ana pediu o orçamento revisado</p>', det)
        self.assertIn('<span class="meta-linha">aguarda o contato</span>', det)
        self.assertEqual(det.count('class="pilula"'), 1)
        # as horas da semeadura são relativas ao relógio: logo depois da meia-noite o mais recente cai em "Ontem"
        self.assertRegex(det, r'<p class="rotulo">(Hoje|Ontem), ')
        self.assertNotIn("assunto antigo", t)
        self.assertIn(">Cartões no período: 1 no Frentes, 1 segurado pela IA, 1 ainda vai subir<", det)
        self.assertIn('id="hi-busca"', det)
        self.assertIn('<div class="vazio" id="hi-sem-busca" hidden>', det)

    def test_conversa_escolhida_mostra_so_ela_com_o_pe_em_que_esta(self):
        self.semear()
        self.classificar(msg_id="c1", conversa=RUI, item_id=7)
        self.classificar(msg_id="c2", conversa=ANA, item_id=8)
        t = texto(self.get(c=RUI))
        self.assertIn(">Cartões desta conversa: 1 no Frentes<", t)                              # o rodapé acompanha a conversa
        self.assertIn(">Cartões no período: 2 no Frentes<", texto(self.get()))
        lista, det = t.split('aria-label="Linha do tempo"')
        self.assertIn('<a class="item-lista hi-tudo" href="/historico">', lista)                 # "Todas" volta a ser link
        self.assertRegex(lista, r'<a class="item-lista hi-it ativo" href="/historico\?c=5592911110002%40s\.whatsapp\.net"[^>]* aria-current="true">')
        self.assertIn('<div class="hi-quem"><span>Rui</span></div>', det)
        self.assertIn('<a class="btn" href="/conversas?c=5592911110002%40s.whatsapp.net">Abrir conversa</a>', det)
        self.assertRegex(det, r"Aguarda você desde [^<]{3,40} às ")   # "ontem" de dia; outro rótulo logo depois da meia-noite
        self.assertEqual(det.count('class="hi-linha"'), 1)
        self.assertEqual(det.count('class="hi-nome"'), 0)                                        # o nome já está no cabeçalho
        self.assertIn('<p class="hi-txt">Rui perguntou se a reunião de quinta está de pé</p><span class="pilula">aguarda você</span>', det)
        self.assertNotIn("Bia ficou de mandar", det)
        # o período troca sem perder a conversa
        self.assertIn('href="/historico?dias=30&amp;c=5592911110002%40s.whatsapp.net"', det)
        t30 = texto(self.get(c=RUI, dias="30"))
        self.assertEqual(t30.split('aria-label="Linha do tempo"')[1].count('class="hi-linha"'), 2)
        self.assertIn('class="on"><span>30 dias</span>', t30)

    def test_conversa_desconhecida_ou_dias_invalido_caem_no_padrao(self):
        self.semear()
        for query in ({"c": "naoexiste@s.whatsapp.net"}, {"dias": "999"}, {"dias": "abc"}, {"c": '"><script>'}):
            t = texto(self.get(**query))
            self.assertIn('<a class="item-lista hi-tudo ativo" href="/historico" aria-current="true">', t, query)
            self.assertEqual(t.count('class="hi-linha"'), 4, query)
            self.assertEqual(t.count('class="hi-quem"'), 0, query)
            self.assertIn('class="on"><span>7 dias</span>', t, query)

    def test_escapa_html_de_nome_e_resumo(self):
        self.analise("x1", ANA, XSS, HORA, "aguarda_dono", XSS)
        for t in (texto(self.get()), texto(self.get(c=ANA))):
            self.assertNotIn("<script>alert", t)
            self.assertIn("&lt;script&gt;", t)

    def test_so_get_e_esta_na_lateral(self):
        self.assertEqual(ROTA(self.req("POST", "/historico")).status, 405)
        self.assertIn(("/historico", "Histórico"), c.NAV)
        self.assertEqual(c.NAV[-1], ("/historico", "Histórico"))                   # no fim: ⌘1–⌘5 não mudam
        t = texto(self.get())
        self.assertRegex(t.split("<main>")[0], r'<a[^>]*href="/historico"[^>]*class="ativo"|<a[^>]*class="ativo"[^>]*href="/historico"')

    def test_modo_de_gravacao_borra_nome_e_resumo_do_historico(self):
        base = (Path(c.TEMPLATES) / "_base.html").read_text(encoding="utf-8")
        regra = next(ln for ln in base.splitlines() if ln.startswith(":root[data-gravacao] :is("))
        for seletor in (".hi-it .nome", ".hi-it .texto", ".hi-nome", ".hi-txt", ".hi-quem"):
            self.assertIn(seletor, regra)


if __name__ == "__main__":
    unittest.main()
