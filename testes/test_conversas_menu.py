"""Pedido 31 (D-48): deixar a conversa fora do Greg direto na área de conversas — menu ⋯ no cabeçalho da conversa aberta
(três passos: itens · nome no Greg · confirmar), ⋯ e botão direito na linha da lista, modo Selecionar com a barra, e a volta
do POST /bloqueios para a lista com `fora=N`. O servidor continua decidindo tudo (D-41/D-46); o JS só abre, posiciona e marca."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.paginas import _comum as c  # noqa: E402
from app.paginas import bloqueios, conversas  # noqa: E402
from testes.test_conversas_zap import AGORA, ANA, G1, LID, XSS, BaseZap, BloqueioFalso, texto  # noqa: E402


def local(r) -> str:
    return next(v for k, v in r.cabecalhos if k.lower() == "location")


class TestMenuDoCabecalho(BaseZap):
    def test_contato_tem_os_tres_passos_e_os_botoes_antigos_sairam(self):
        t = texto(self.get("/conversas", c=ANA))
        self.assertIn('id="zap-menu-cab"', t)
        self.assertIn("Deixar fora do Greg", t)
        self.assertIn("Nome no Greg…", t)
        self.assertIn('data-passo="nome" hidden', t)
        self.assertIn('data-passo="fora" hidden', t)
        self.assertIn("Deixar “Ana Paula” fora do Greg?", t)
        self.assertIn('name="acao" value="bloquear"', t)
        self.assertIn(f'name="c" value="{ANA}"', t)
        self.assertIn('name="volta" value="/conversas"', t)
        self.assertIn('action="/conversas/apelido"', t)
        self.assertNotIn('class="zap-renomear"', t)
        self.assertNotIn(">Bloquear<", t)
        self.assertNotIn('class="botao neutro pequeno">Demandas', t)

    def test_grupo_nao_renomeia_e_demandas_so_depois_de_classificar(self):
        conversas.servicos(self.ctx).grupos.nomes([G1], sincrono=True)
        t = texto(self.get("/conversas", c=G1))
        self.assertIn("Deixar “CBA · Marketing” fora do Greg?", t)
        self.assertNotIn("Nome no Greg", t)
        self.assertNotIn("Ver demandas desta conversa", t)
        self.banco.gravar_classificacao(msg_id="g1m3", conversa=G1, contato="Marcos", ts=AGORA, classe="demanda",
                                        resumo="link do convênio", confianca="alta")
        t = texto(self.get("/conversas", c=G1))
        self.assertIn("Ver demandas desta conversa", t)
        self.assertIn('href="/demandas?conversa=', t)

    def test_conversa_sem_numero_tem_menu_sem_envio(self):
        t = texto(self.get("/conversas", c=LID))
        self.assertIn('id="zap-menu-cab"', t)
        self.assertIn("Deixar “~Carla” fora do Greg?", t)

    def test_nome_hostil_sai_escapado_no_titulo_da_confirmacao(self):
        self.banco.definir_apelido(ANA, XSS)
        t = texto(self.get("/conversas", c=ANA))
        self.assertNotIn(XSS, t)
        self.assertIn(f"Deixar “{c.esc(XSS)}” fora do Greg?", t)

    def test_conversa_bloqueada_nao_tem_menu(self):
        self.psql.bloqueados = [G1]
        t = texto(self.get("/conversas", c=G1))
        self.assertIn("Conversa bloqueada", t)
        self.assertNotIn('id="zap-menu-cab"', t)


class TestLinhaDaLista(BaseZap):
    def test_linha_leva_dados_caixa_e_botao_mais(self):
        t = texto(self.get("/conversas"))
        self.assertIn(f'data-jid="{ANA}" data-nome="Ana Paula"', t)
        self.assertIn(f'name="j:{ANA}" value="1" form="form-fora-varios"', t)
        self.assertIn('class="zap-mais" role="button" tabindex="0"', t)
        # menu compartilhado da linha, barra do modo Selecionar e menu do topo (uma vez cada, fora de #zap-itens)
        for marca in ('id="zap-menu-linha"', 'id="zap-ml-abrir"', 'id="zap-ml-titulo"', 'id="form-fora-varios"',
                      'name="acao" value="bloquear_varios"', 'id="zap-selecionar"', 'id="zap-sel-cancelar"',
                      'id="zap-menu-topo"', 'href="/bloqueios"'):
            self.assertEqual(t.count(marca), 1, marca)
        self.assertLess(t.index('id="zap-itens"'), t.index('id="zap-menu-linha"'))
        self.assertIn("<style>", t)   # CSS do menu vive na página (a pele compartilhada é da Fábrica de Design)

    def test_nome_hostil_na_linha_sai_escapado(self):
        self.banco.definir_apelido(ANA, XSS)
        t = texto(self.get("/conversas"))
        self.assertNotIn(XSS, t)
        self.assertIn(f'data-nome="{c.esc(XSS)}"', t)

    def test_aviso_da_volta(self):
        self.assertIn("1 conversa fora do Greg.", texto(self.get("/conversas", fora="1")))
        self.assertIn("3 conversas fora do Greg.", texto(self.get("/conversas", fora="3")))
        t = texto(self.get("/conversas", fora="2", falhou="1"))
        self.assertIn("2 saíram", t)
        self.assertIn('class="zap-aviso-lista ruim"', t)
        self.assertIn("Nenhuma conversa saiu.", texto(self.get("/conversas", fora="0")))
        # o nome da classe também aparece no CSS da página: o que não pode existir é o elemento
        self.assertNotIn('class="zap-aviso-lista', texto(self.get("/conversas", fora="abc")))
        self.assertNotIn('class="zap-aviso-lista', texto(self.get("/conversas")))

    def test_nova_mensagem_tambem_lista_com_menu(self):
        t = texto(self.get("/conversas/nova"))
        self.assertIn('id="zap-menu-topo"', t)
        self.assertIn('id="form-fora-varios"', t)


class TestVoltaParaALista(BaseZap):
    def setUp(self):
        super().setUp()
        self.ctx.bloqueio = BloqueioFalso(self.psql)

    def bl(self, corpo):
        return bloqueios.ROTAS["/bloqueios"](self.req("POST", "/bloqueios", corpo))

    def test_uma_pela_linha_ou_pelo_cabecalho_volta_com_fora_1(self):
        r = self.bl({"acao": "bloquear", "c": ANA, "volta": "/conversas"})
        self.assertEqual((r.status, local(r)), (303, "/conversas?fora=1"))
        self.assertEqual(self.ctx.bloqueio.chamadas, [("bloquear", ANA)])
        self.assertIn(ANA, self.psql.bloqueados)

    def test_varias_pelo_modo_selecionar(self):
        r = self.bl({"acao": "bloquear_varios", "volta": "/conversas", f"j:{ANA}": "1", f"j:{G1}": "1", f"j:{LID}": "0"})
        self.assertEqual((r.status, local(r)), (303, "/conversas?fora=2"))
        self.assertEqual(sorted(j for _, j in self.ctx.bloqueio.chamadas), sorted([ANA, G1]))

    def test_falha_no_meio_volta_dizendo_quantas_sairam(self):
        falso = self.ctx.bloqueio

        class ParaNaSegunda(BloqueioFalso):
            def __call__(self, acao, jid):
                if len(self.chamadas) == 1:
                    self.falhar = True
                return super().__call__(acao, jid)

        self.ctx.bloqueio = ParaNaSegunda(self.psql)
        r = self.bl({"acao": "bloquear_varios", "volta": "/conversas", f"j:{ANA}": "1", f"j:{G1}": "1"})
        self.assertEqual((r.status, local(r)), (303, "/conversas?fora=1&falhou=1"))
        self.assertEqual(len(self.ctx.bloqueio.chamadas), 1)
        self.ctx.bloqueio = falso

    def test_nada_marcado_e_jid_invalido(self):
        self.assertEqual(self.bl({"acao": "bloquear_varios", "volta": "/conversas"}).status, 400)
        self.assertEqual(self.bl({"acao": "bloquear_varios", "volta": "/conversas", "j:x'; drop@g.us": "1"}).status, 400)
        self.assertEqual(self.ctx.bloqueio.chamadas, [])

    def test_desbloquear_segue_abrindo_a_conversa(self):
        self.psql.bloqueados = [G1]
        r = self.bl({"acao": "desbloquear", "c": G1, "volta": "/conversas"})
        self.assertEqual(local(r), f"/conversas?c={G1}")

    def test_volta_para_ajustes_e_bloqueios_nao_mudou(self):
        r = self.bl({"acao": "bloquear_varios", "tipo": "contatos", f"j:{ANA}": "1"})
        self.assertEqual(local(r), "/bloqueios?tipo=contatos&fora=1")
        r = self.bl({"acao": "bloquear", "c": G1})
        self.assertTrue(local(r).startswith("/ajustes?bloqueado=1"))


if __name__ == "__main__":
    unittest.main()
