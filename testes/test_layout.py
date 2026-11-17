"""Janela dividida (pedido 48, D-59): app/layout.py (medidas guardadas), POST /layout e a casca (_base.html) que arrasta.
Quem chama: unittest. Arquivo temporário; nenhuma rede.
O que não faz: não arrasta de verdade (isso é prova de navegador, RUNBOOK §25) nem toca o dados/layout.json real.
"""
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import layout  # noqa: E402
from app.http import Requisicao  # noqa: E402
from app.paginas import _comum as c  # noqa: E402
from app.paginas import layout as pagina_layout  # noqa: E402

RAIZ = Path(__file__).resolve().parents[1]
PADRAO = {"lateral": 0, "recolhida": False, "listas": {}}   # 0 = lateral automática
XSS = '"><script>alert(1)</script>'


def texto(r) -> str:
    return r.corpo.decode("utf-8") if isinstance(r.corpo, bytes) else r.corpo


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.antigo = layout.ARQUIVO
        layout.ARQUIVO = Path(self.tmp.name) / "dados" / "layout.json"
        self.addCleanup(setattr, layout, "ARQUIVO", self.antigo)


class TestLayout(Base):
    def test_padrao_nao_injeta_nada(self):
        p = layout.ler()
        self.assertEqual(p, PADRAO)
        self.assertEqual(layout.atributos_html(p), "")
        self.assertEqual(layout.atributos_paineis(p, "/demandas"), ' data-pagina="/demandas"')
        self.assertEqual(layout.atributos_paineis(p, ""), "")
        self.assertFalse(layout.ARQUIVO.exists())                       # ler não cria arquivo

    def test_mudar_guarda_por_cima_do_que_ja_havia(self):
        layout.mudar({"lateral": "260"})
        layout.mudar({"pagina": "/demandas", "lista": "460"})
        layout.mudar({"pagina": "/conversas", "lista": 320.4})
        p = layout.mudar({"recolhida": "1"})
        self.assertEqual(p, {"lateral": 260, "recolhida": True, "listas": {"/demandas": 460, "/conversas": 320}})
        self.assertEqual(json.loads(layout.ARQUIVO.read_text()), p)
        self.assertEqual(stat.S_IMODE(os.stat(layout.ARQUIVO).st_mode), 0o600)
        self.assertEqual(layout.atributos_html(p), ' data-lateral="recolhida" style="--lateral:260px"')
        self.assertEqual(layout.atributos_paineis(p, "/demandas"),
                         ' data-pagina="/demandas" style="--lista:460px;--lista-estreita:460px"')
        self.assertEqual(layout.atributos_paineis(p, "/frentes"), ' data-pagina="/frentes"')   # cada página tem a sua
        self.assertEqual(layout.mudar({"recolhida": "0"})["recolhida"], False)

    def test_zero_volta_ao_padrao(self):
        layout.mudar({"lateral": 300, "pagina": "/demandas", "lista": 500})
        p = layout.mudar({"lateral": "0"})
        self.assertEqual((p["lateral"], p["listas"]), (0, {"/demandas": 500}))
        self.assertEqual(layout.atributos_html(p), "")                   # automática: a base decide, nada é injetado
        self.assertEqual(layout.atributos_html(layout.mudar({"lateral": 212})), ' style="--lateral:212px"')   # 212 escolhido obedece
        layout.mudar({"lateral": 0})
        self.assertEqual(layout.mudar({"pagina": "/demandas", "lista": "0"}), PADRAO)

    def test_fora_da_faixa_ou_lixo_nao_grava(self):
        layout.mudar({"lateral": 260, "pagina": "/demandas", "lista": 460})
        for mudanca in ({"lateral": "9999"}, {"lateral": "50"}, {"lateral": "abc"}, {"lateral": XSS},
                        {"pagina": "/demandas", "lista": "100"}, {"pagina": "/demandas", "lista": "5000"},
                        {"pagina": "/demandas", "lista": XSS}, {"pagina": XSS, "lista": "400"},
                        {"pagina": "demandas", "lista": "400"}, {"pagina": "/a/b", "lista": "400"},
                        {"pagina": "/Demandas", "lista": "400"}, {"lista": "400"}, {"qualquer": "coisa"}, {}):
            p = layout.mudar(mudanca)
            self.assertEqual(p, {"lateral": 260, "recolhida": False, "listas": {"/demandas": 460}}, mudanca)
        self.assertNotIn("script", layout.ARQUIVO.read_text())

    def test_teto_de_paginas(self):
        for i in range(layout.PAGINAS_MAX + 5):
            layout.mudar({"pagina": "/" + "a" * (i % 20 + 1) + ("b" if i >= 20 else ""), "lista": 400})
        p = layout.ler()
        self.assertEqual(len(p["listas"]), layout.PAGINAS_MAX)
        self.assertEqual(layout.mudar({"pagina": "/a", "lista": 444})["listas"]["/a"], 444)   # página já guardada ainda muda

    def test_arquivo_corrompido_ou_adulterado_cai_no_padrao(self):
        layout.ARQUIVO.parent.mkdir(parents=True)
        layout.ARQUIVO.write_text("{nao é json")
        self.assertEqual(layout.ler(), PADRAO)
        layout.ARQUIVO.write_text(json.dumps({"lateral": "1e9", "recolhida": "talvez", "listas": {XSS: 400, "/ok": XSS, "/demandas": 380},
                                              "extra": XSS}))
        os.utime(layout.ARQUIVO, ns=(1, 1))                              # força a releitura do cache
        p = layout.ler()
        self.assertEqual(p, {"lateral": 0, "recolhida": False, "listas": {"/demandas": 380}})
        self.assertNotIn("script", layout.atributos_html(p) + layout.atributos_paineis(p, "/demandas"))
        layout.ARQUIVO.write_text("[1, 2]")
        os.utime(layout.ARQUIVO, ns=(2, 2))
        self.assertEqual(layout.ler(), PADRAO)

    def test_sem_arquivo_definido_nao_escreve(self):
        layout.ARQUIVO = None
        self.assertEqual(layout.mudar({"lateral": 300})["lateral"], 300)  # devolve o resultado, mas não guarda
        self.assertEqual(layout.ler(), PADRAO)                            # testes e capturas: sempre o padrão
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_gravacoes_simultaneas_nao_se_atropelam(self):
        erros = []

        def grava(i):
            try:
                layout.mudar({"pagina": f"/p{'abcdefghij'[i]}", "lista": 300 + i})
            except Exception as e:  # noqa: BLE001
                erros.append(type(e).__name__)
        fios = [threading.Thread(target=grava, args=(i,)) for i in range(10)]
        [f.start() for f in fios]
        [f.join() for f in fios]
        self.assertEqual(erros, [])
        self.assertEqual(len(layout.ler()["listas"]), 10)                 # nenhuma se perdeu


class TestRota(Base):
    def req(self, metodo="POST", corpo=None, cab=None):
        return Requisicao(metodo, "/layout", {}, corpo or {}, {}, None, cabecalhos=cab or {})

    def test_post_grava_e_responde_json(self):
        r = pagina_layout.ROTAS["/layout"](self.req(corpo={"lateral": "240", "recolhida": "1", "pagina": "/historico", "lista": "420",
                                                           "tema": "escuro"}))
        self.assertEqual((r.status, r.tipo), (200, "application/json; charset=utf-8"))
        self.assertEqual(json.loads(texto(r)), {"ok": True, "lateral": 240, "recolhida": True})
        self.assertEqual(layout.ler(), {"lateral": 240, "recolhida": True, "listas": {"/historico": 420}})

    def test_so_post_e_so_da_propria_origem(self):
        self.assertEqual(pagina_layout.ROTAS["/layout"](self.req("GET")).status, 405)
        r = pagina_layout.ROTAS["/layout"](self.req(corpo={"lateral": "300"}, cab={"Origin": "http://evil.example"}))
        self.assertEqual(r.status, 403)
        self.assertEqual(layout.ler(), PADRAO)

    def test_o_servidor_descobre_a_rota_e_aponta_o_arquivo(self):
        from app import server
        exatas, _ = server.descobrir_rotas()
        self.assertIn("/layout", exatas)
        self.assertIn('layout.ARQUIVO = dados / "layout.json"', (RAIZ / "app/server.py").read_text(encoding="utf-8"))


class TestCasca(Base):
    def pagina(self, ativo="/demandas"):
        lista = c.painel("<p>lista</p>", toolbar_html=c.toolbar(c.titulo("X")), classe="lista")
        return texto(c.pagina("X", lista + c.painel("<p>detalhe</p>"), ativo))

    def test_toda_pagina_aplica_o_layout_guardado(self):
        t = self.pagina()
        self.assertIn('<html lang="pt-BR" data-tema="claro">', t)
        self.assertIn('<div class="paineis" data-pagina="/demandas">', t)
        layout.mudar({"lateral": 260, "recolhida": 1, "pagina": "/demandas", "lista": 460})
        t = self.pagina()
        self.assertIn('<html lang="pt-BR" data-tema="claro" data-lateral="recolhida" style="--lateral:260px">', t)
        self.assertIn('<div class="paineis" data-pagina="/demandas" style="--lista:460px;--lista-estreita:460px">', t)
        self.assertIn('<div class="paineis" data-pagina="/historico">', self.pagina("/historico"))
        self.assertIn('<div class="paineis">', self.pagina(""))            # página sem destino: sem chave, sem largura

    def test_botoes_de_recolher_e_mostrar_a_lateral(self):
        t = self.pagina()
        topo, resto = t.split("<main>")
        self.assertIn('<aside class="lateral" id="lateral">', topo)
        self.assertRegex(topo, r'<button type="button" class="[^"]*\brecolher vigia-lateral" title="Ocultar a lateral \(⌃⌘S\)" '
                               r'aria-label="Ocultar a lateral" aria-expanded="true" aria-controls="lateral">')
        self.assertRegex(resto, r'<button type="button" class="[^"]*\bmostrar-lateral vigia-lateral" title="Mostrar a lateral \(⌃⌘S\)" '
                                r'aria-label="Mostrar a lateral" aria-expanded="false" aria-controls="lateral">')
        self.assertLess(resto.index("mostrar-lateral"), resto.index('<div class="paineis"'))

    def test_css_e_script_da_janela_dividida(self):
        base = (RAIZ / "app/templates/_base.html").read_text(encoding="utf-8")
        for trecho in (
                ".divisoria { flex: 0 0 1px; margin-left: -1px;",                      # em cima do fio: não desloca nada
                ".divisoria::before {", "cursor: col-resize;",
                ".divisoria:focus-visible { background: var(--acento); outline: 1px solid var(--acento);",   # foco por teclado visível
                # sem largura escolhida a lateral é automática (nunca corta o rodapé dela); escolhida, obedece e cede espaço
                ".lateral { flex: 0 0 var(--lateral);",
                ':root[style*="--lateral:"] .lateral { flex: 0 1 var(--lateral); min-width: 168px; }',
                ':root[style*="--lateral:"] main { min-width: 540px; }',
                "soltar: function () { guardar({ lateral: escolhida(), recolhida: recolhida() ? 1 : 0 });",
                "window.vigiaLateral(false, true); repor(antes);",              # recolher arrastando não perde a largura
                ".painel.lista ~ .painel.detalhe { min-width: 300px; }",
                # lateral recolhida vale no navegador e no app novo; no app antigo (alça fixa de 212 px) é ignorada
                ':root[data-lateral="recolhida"]:is(:not([data-app]), [data-alca]) .lateral {',
                ':root[data-app="mac"]:not([data-alca]) .vigia-lateral { display: none; }',
                "left: var(--semaforo, 78px);",
                "window.vigiaLateral = function (mostrar, semGuardar) {",
                "window.vigiaPost('/layout', dados)",
                "d.setAttribute('role', 'separator'); d.setAttribute('aria-orientation', 'vertical');",
                "d.addEventListener('pointerdown'", "d.addEventListener('dblclick'", "e.key === 'ArrowLeft'",
                "postMessage({ acao: 'lateral', largura: recolhida() ? 0 :",
                "var LAT_MIN = appAntigo ? 212 : 168, LAT_MAX = 360,",
                "if (nome === 'lateral') {",
                "e.metaKey && e.ctrlKey && !e.shiftKey && !e.altKey && (e.key === 's' || e.key === 'S')",
                "var secoes = ['/demandas', '/conversas', '/frentes', '/envios', '/contatos', '/historico'];",
                "e.key >= '1' && e.key <= '6'"):
            self.assertIn(trecho, base, trecho)
        # os limites do script são os mesmos do servidor (que é quem recusa o que vier fora)
        self.assertEqual((layout.LATERAL_MIN, layout.LATERAL_AUTO, layout.LATERAL_MAX, layout.LISTA_MIN, layout.LISTA_MAX),
                         (168, 0, 360, 240, 900))
        self.assertIn("LISTA_MAX = 900", base)
        self.assertIn(".painel.lista { flex: 0 1 var(--lista); min-width: 240px;", base)
        self.assertIn("--lateral: 212px;", base)

    def test_app_nativo_acompanha_a_lateral(self):
        swift = (RAIZ / "macapp/Vigia/main.swift").read_text(encoding="utf-8")
        for trecho in ("setAttribute('data-alca', '1')", "style.setProperty('--semaforo',", "func ajustarAlca(_ largura: CGFloat)",
                       'if acao == "lateral" {', 'guard acao == "liberarEnvio" else { return }',
                       'withTitle: "Histórico", action: #selector(irHistorico), keyEquivalent: "6"',
                       'withTitle: "Mostrar ou ocultar a lateral"', "lateral.keyEquivalentModifierMask = [.command, .control]",
                       '@objc func irHistorico() { ir("/historico") }', '@objc func lateral() { atalho("lateral") }'):
            self.assertIn(trecho, swift, trecho)
        # a mensagem da página só vale vinda do próprio painel: a checagem de origem fica antes de qualquer ação
        self.assertLess(swift.index('(url.port ?? 80) == amb.porta else { return }'), swift.index('if acao == "lateral" {'))


if __name__ == "__main__":
    unittest.main()
