"""Pedido 84: Conversas sem tranco. (a) foto de perfil no diálogo de encaminhar, (c) transições e atualização parcial.
Quem chama: unittest. Confere o CSS e o JavaScript que a página leva (o JavaScript não roda aqui; a prova no navegador
está no relatório do pedido) e a página montada com as conversas fictícias de testes/test_conversas_zap.py.
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.paginas import conversas  # noqa: E402
from testes.test_conversas_zap import BaseZap  # noqa: E402
from testes.test_paginas import texto  # noqa: E402

JS = conversas._JS
CSS = conversas._CSS


def funcao(nome: str) -> str:
    """Corpo da função JavaScript `nome` (até a chave que a fecha)."""
    ini = JS.index(f"function {nome}(")
    abre = JS.index("{", ini)
    nivel = 0
    for i in range(abre, len(JS)):
        nivel += {"{": 1, "}": -1}.get(JS[i], 0)
        if nivel == 0:
            return JS[ini:i + 1]
    raise AssertionError(nome)


class TestFotoNoEncaminhar(unittest.TestCase):
    def test_destino_leva_a_foto_da_linha_da_lista(self):
        corpo = funcao("pintarDestinos")
        self.assertIn("a.querySelector('.zap-av')", corpo)        # a mesma foto (ou silhueta) da linha da lista
        self.assertIn("importNode(av, true)", corpo)               # serve também para a lista inteira buscada à parte
        self.assertIn("loading = 'lazy'", corpo)
        self.assertNotIn("/conversas/avatar", corpo)               # nenhuma busca nova: o endereço vem da linha
        self.assertIn("zap-enc-nome", corpo)

    def test_nome_do_destino_nao_depende_do_texto_do_botao(self):
        self.assertIn("encNome = b.getAttribute('data-nome')", JS)

    def test_linha_da_lista_tem_foto_ou_silhueta(self):
        z = BaseZap("get")
        z.setUp()
        self.addCleanup(z.doCleanups)
        html = texto(z.get("/conversas"))
        linhas = re.findall(r'<a class="item-lista zap-item[^"]*".*?</a>', html, re.S)
        self.assertTrue(linhas)
        for l in linhas:
            self.assertRegex(l, r'class="(avatar zap-av|zap-av)|class="avatar zap-av')

    def test_css_do_destino(self):
        self.assertIn(".zap-enc-lista .zap-av { width: 28px; height: 28px; }", CSS)
        self.assertIn(".zap-enc-nome {", CSS)


class TestSemTranco(unittest.TestCase):
    def test_lista_trocada_por_linha(self):
        corpo = funcao("trocarLista")
        self.assertIn("data-jid", corpo)
        self.assertIn("manterImagens(o, n)", corpo)                # a foto da linha não é recriada
        self.assertIn("trocarLista(li, lt); filtrarLista();", funcao("atualizar"))
        self.assertNotIn("lt.innerHTML = li.innerHTML; filtrarLista();", funcao("atualizar"))

    def test_bolha_nova_entra_com_animacao_curta(self):
        self.assertIn("chegou(n); msgs.insertBefore(n, cur);", funcao("trocarFio"))
        self.assertIn("chegou(b);", funcao("bolhaNaHora"))
        self.assertIn("el.classList.remove('chegou')", funcao("chegou"))   # volta a ser igual à do servidor
        self.assertIn(".zap-msg.chegou { animation: zap-chega .16s ease-out; }", CSS)

    def test_movimento_respeita_reduzir_movimento(self):
        bloco = CSS[CSS.index("@media (prefers-reduced-motion: no-preference) {"):]
        bloco = bloco[:bloco.index("\n}\n")]
        self.assertIn("@view-transition { navigation: auto; }", bloco)
        self.assertIn("view-transition-name: zap-lista;", bloco)
        self.assertIn("@media (prefers-reduced-motion: reduce) { .zap-msg.chegou { animation: none; } }", CSS)
        self.assertEqual(CSS.count("@view-transition"), 1)     # nunca fora do bloco sem "reduzir movimento"

    def test_rolagem_da_lista_volta_ao_trocar_de_conversa(self):
        self.assertIn("sessionStorage.getItem('greg.zap.lista')", JS)
        self.assertIn("addEventListener('pagehide'", JS)
        ini = JS.index("sessionStorage.getItem('greg.zap.lista')")
        self.assertIn("try {", JS[ini - 120:ini])                  # sem armazenamento, a lista só abre no topo

    def test_esqueleto_enquanto_carrega(self):
        self.assertIn("encLista.appendChild(esq)", JS)
        self.assertIn("esq.remove();", JS)
        self.assertIn("var esqA = esqueleto(2);", JS)
        self.assertEqual(JS.count("esqA.remove();"), 2)            # falha e erro de rede tiram o esqueleto
        self.assertIn("esq-linha", funcao("esqueleto"))


class TestEnvioOtimista(unittest.TestCase):
    """(b) D-87: aparece na hora, sobe na lista, só a confirmação tira o relógio; falha certa deixa a bolha com Tentar
    de novo e Descartar; pode ter saído (202 ou rede) nunca tem botão de repetir."""

    def test_os_tres_caminhos_sobem_a_conversa_na_hora(self):
        self.assertIn("subirNaLista(f.dataset.para, corpo.texto)", funcao("enviarTexto"))
        self.assertIn("subirNaLista(f.dataset.para, rot)", funcao("enviarAnexo"))
        self.assertIn("subirNaLista(f.dataset.para, 'Áudio de voz')", funcao("mandarAudio"))
        self.assertIn("subirNaLista(corpo.para, 'Encaminhando…')", funcao("mandarEncaminhar"))

    def test_audio_mostra_a_bolha_antes_de_preparar_o_arquivo(self):
        corpo = funcao("mandarAudio")
        self.assertLess(corpo.index("bolhaNaHora('Áudio de voz')"), corpo.index("paraWav("))
        self.assertIn(", prov);", corpo)                         # a mesma bolha segue para o envio

    def test_encaminhar_para_a_conversa_aberta_mostra_a_bolha(self):
        corpo = funcao("encaminhar")
        self.assertIn("corpo.para === corpo.de", corpo)
        self.assertIn("bolhaNaHora(", corpo)
        self.assertNotIn("querySelector('.zap-msg[data-id=\"' +", corpo)   # id comparado por atributo, sem montar seletor

    def test_selo_de_saida_so_com_a_confirmacao(self):
        for nome in ("bombear", "enviarMidia", "mandarEncaminhar"):
            corpo = funcao(nome)
            self.assertEqual(corpo.count("classList.add('saiu')"), 1, nome)
            antes = corpo[:corpo.index("classList.add('saiu')")]
            self.assertIn("x.s === 200", antes[-160:], nome)

    def test_falha_certa_deixa_a_bolha_com_tentar_e_descartar(self):
        corpo = funcao("estadoBolha")
        self.assertIn("'Tentar de novo'", corpo)
        self.assertIn("'Descartar'", corpo)
        self.assertIn("if (tipo !== 'incerta')", corpo)           # pode ter saído não tem botão nenhum de repetir
        self.assertIn("if (barradoPorIncerto()) { return; }", corpo)   # nem o de outra bolha passa com a faixa aberta
        self.assertIn("pararFila('falhou', 'Não enviada. '", funcao("bombear"))
        self.assertIn("estadoBolha(prov, 'falhou', m3, deNovo)", funcao("enviarMidia"))
        self.assertIn("estadoBolha(prov, 'falhou', 'Não enviada. ' + m3, deNovo)", funcao("mandarEncaminhar"))

    def test_texto_nao_volta_ao_campo_quando_ha_bolha(self):
        corpo = funcao("pararFila")
        self.assertIn("filter(function (i) { return i.b; })", corpo)
        self.assertIn("sem.length ? devolverFila() : ''", corpo)   # só o que não tem bolha volta, como antes
        self.assertNotIn("devolverFila()", funcao("bombear"))

    def test_pode_ter_saido_nos_tres_caminhos(self):
        for nome in ("bombear", "enviarMidia", "mandarEncaminhar"):
            corpo = funcao(nome)
            self.assertIn("'incerta'", corpo, nome)
            self.assertIn("marcarIncerto(", corpo[corpo.index(".catch(function ()"):], nome)   # rede caída = incerto
            self.assertIn("x.s === 202", corpo, nome)

    def test_trava_nunca_finge_envio(self):
        for nome in ("bombear", "enviarMidia", "mandarEncaminhar"):
            corpo = funcao(nome)
            trecho = corpo[corpo.index("x.s === 423"):]
            trecho = trecho[:trecho.index("else if")]
            self.assertIn("liberar(", trecho, nome)
            self.assertNotIn("saiu", trecho, nome)
        self.assertIn("'travado', 'Envio travado. Libere o envio para mandar.'", funcao("bombear"))

    def test_ja_conferi_tira_as_bolhas_incertas(self):
        corpo = funcao("mostrarIncerto")
        self.assertIn(".zap-msg.otimista.incerta", corpo)

    def test_aviso_da_tela_nao_e_sobrescrito(self):
        # defeito achado no pedido 84: o painel de perfil declarava outro `var aviso` (uma função) no mesmo escopo e,
        # pelo içamento do var, nenhum aviso de envio nem a faixa "pode ter saído" aparecia nas conversas com perfil
        self.assertEqual(len(re.findall(r"var aviso\b", JS)), 1)
        self.assertIn("var avisoPerfil = function", JS)

    def test_nomes_novos_sem_colisao(self):
        for nome in ("seloRelogio", "voltarAEnviar", "estadoBolha", "subirNaLista", "pararFila", "mandarEncaminhar",
                     "trocarLista", "chegou", "esqueleto"):
            self.assertEqual(len(re.findall(r"(?:var|function)\s+" + nome + r"\b", JS)), 1, nome)
        self.assertNotIn("function relogio(", JS)                   # `relogio` já é o cronômetro da gravação

    def test_css_dos_estados(self):
        self.assertIn(".zap-msg.saida.otimista:is(.falhou, .travado, .incerta) { background: var(--atrasado-fundo);", CSS)
        self.assertIn(".zap-falha button {", CSS)


if __name__ == "__main__":
    unittest.main()
