"""Ajustes de aparência (pedido 12; estilo Papel, pedido 39): validação, gravação, CSS gerado e a página /ajustes.
Só tema e tamanho da letra sobrevivem ao Papel: estilo, acento próprio, tom do vidro e plano de fundo saíram."""
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import preferencias as pf  # noqa: E402
from app.http import Requisicao  # noqa: E402
from app.paginas import ajustes, conexao  # noqa: E402
from app.paginas import _comum as c  # noqa: E402


def texto(r) -> str:
    return r.corpo.decode("utf-8") if isinstance(r.corpo, bytes) else r.corpo


class TestPreferencias(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.antigo = pf.ARQUIVO
        pf.ARQUIVO = Path(self.tmp.name) / "dados" / "preferencias.json"
        self.addCleanup(setattr, pf, "ARQUIVO", self.antigo)

    def req(self, metodo, corpo=None, query=None, cab=None):
        return Requisicao(metodo, "/ajustes", query or {}, corpo or {}, {}, None, cabecalhos=cab or {})

    def test_validar_recusa_fora_da_lista_e_injecao(self):
        p = pf.validar({"fonte": "99", "tema": "neon", "acento": "#fff;}</style><script>"})
        self.assertEqual(p, pf.PADRAO)
        self.assertEqual(pf.validar({"fonte": "14", "tema": "escuro", "acento": "#0A66D6"}), {**pf.PADRAO, "fonte": 14, "tema": "escuro"})

    def test_cor_de_destaque_da_lista_entra_no_css_e_tem_contraste(self):
        """Pedido 66: poucas cores fixas. Só cor da tabela vira CSS; branco sobre a bolha e texto de acento >= 4,5:1."""
        p = pf.validar({"acento": "roxo"})
        self.assertEqual(p["acento"], "roxo")
        css = pf.css(p)
        self.assertIn(":root:root:root { --acento: light-dark(#7A4FD6, #8E63E6);", css)
        self.assertIn("--bolha-saida:", css)
        self.assertEqual(pf.css_acento("mac"), "")
        self.assertEqual(pf.css_acento("#fff;}</style>"), "")
        self.assertLessEqual(len(pf.ACENTOS), 7)

        def lum(h):
            canais = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            r, g, b = [c / 12.92 if c <= .03928 else ((c + .055) / 1.055) ** 2.4 for c in canais]
            return .2126 * r + .7152 * g + .0722 * b

        def contraste(a, b):
            la, lb = sorted((lum(a), lum(b)), reverse=True)
            return (la + .05) / (lb + .05)

        def mistura(h, outro, parte):
            x, y = ([int(c[i:i + 2], 16) for i in (1, 3, 5)] for c in (h, outro))
            return "#%02x%02x%02x" % tuple(round(a * parte + b * (1 - parte)) for a, b in zip(x, y))

        for nome, (_, par) in pf.ACENTOS.items():
            if not par:
                continue
            self.assertGreaterEqual(contraste("#ffffff", mistura(par[0], "#000000", .92)), 4.5, nome)   # bolha, tema claro
            self.assertGreaterEqual(contraste("#ffffff", mistura(par[1], "#000000", .86)), 4.5, nome)   # bolha, tema escuro
            self.assertGreaterEqual(contraste(mistura(par[0], "#000000", .92), "#ffffff"), 4.5, nome)   # texto, tema claro
            self.assertGreaterEqual(contraste(mistura(par[1], "#ffffff", .78), "#1e2024"), 4.5, nome)   # texto, tema escuro

    def test_padrao_nao_injeta_css_e_forca_o_tema_claro(self):
        self.assertEqual(pf.css(pf.PADRAO), '<style id="prefs"></style>')
        self.assertEqual(pf.atributos_html(pf.PADRAO), ' data-tema="claro"')       # pedido 76: o Greg abre no claro
        self.assertEqual(pf.atributos_html({**pf.PADRAO, "tema": "sistema"}), "")

    def test_chaves_antigas_sao_ignoradas_e_a_letra_antiga_e_convertida(self):
        """preferencias.json anterior ao Papel (corpo 14 por padrão, escala 13..18) cai na escala nova (12..15, padrão 13)."""
        p = pf.validar({"fonte": 14, "tema": "sistema", "acento": "#2b2f36", "estilo": "glass", "fundo_claro": "auto",
                        "fundo_escuro": "auto", "vidro": "auto"})
        self.assertEqual(p, {**pf.PADRAO, "fonte": 13, "tema": "sistema"})
        self.assertEqual(pf.validar({"fonte": 18, "estilo": "solido"})["fonte"], 15)
        self.assertEqual(pf.validar({"fonte": 16, "estilo": "solido"})["fonte"], 14)
        self.assertEqual(pf.validar({"fonte": 13, "vidro": "azul"})["fonte"], 12)
        self.assertEqual(pf.validar({"fonte": 14})["fonte"], 14)   # arquivo já no Papel: 14 é "Grande" mesmo
        self.assertEqual(pf.validar({"fonte": 14, "acento": "#123"})["fonte"], 14)   # acento solto (POST velho) não converte

    def test_gravar_ler_e_permissao(self):
        pf.gravar({"fonte": "15", "tema": "claro", "acento": "#0d9488"})
        self.assertEqual(stat.S_IMODE(os.stat(pf.ARQUIVO).st_mode), 0o600)
        p = pf.ler()
        self.assertEqual(p["fonte"], 15)
        self.assertIn("font-size:15px", pf.css(p))
        self.assertNotIn("acento", pf.css(p))
        self.assertEqual(pf.atributos_html(p), ' data-tema="claro"')
        self.assertEqual(json.loads(pf.ARQUIVO.read_text()), {**pf.PADRAO, "fonte": 15, "tema": "claro"})

    def test_arquivo_corrompido_volta_ao_padrao(self):
        pf.ARQUIVO.parent.mkdir(parents=True)
        pf.ARQUIVO.write_text("{nao é json")
        self.assertEqual(pf.ler(), pf.PADRAO)

    def test_toda_pagina_aplica_a_preferencia(self):
        pf.gravar({"fonte": "14", "tema": "escuro"})
        t = texto(c.pagina("X", "<p>oi</p>", "/conexao"))
        self.assertIn('<html lang="pt-BR" data-tema="escuro">', t)
        self.assertIn("html { font-size:14px; }", t)
        # Papel + pedidos 40 e 46: a lateral tem os seis destinos, o indicador da conexão e a engrenagem de Ajustes no rodapé
        self.assertIn('class="lateral-ajustes" href="/ajustes" title="Ajustes (⌘,)"', t.split("<main>")[0])
        for destino in ("/demandas", "/conversas", "/frentes", "/envios", "/contatos", "/historico", "/conexao"):
            self.assertIn(f'href="{destino}"', t, destino)
        self.assertNotIn("backdrop-filter", t)
        self.assertIn("light-dark(", t)

    def test_pagina_get_post_e_restaurar(self):
        r = ajustes.ROTAS["/ajustes"](self.req("GET"))
        self.assertEqual(r.status, 200)
        t = texto(r)
        self.assertIn('name="fonte"', t)
        self.assertIn('name="tema"', t)
        self.assertIn('role="switch" id="vigia-gravar" aria-checked="false"', t)
        self.assertIn('name="acento" value="mac" checked', t)      # pedido 66: a cor de destaque voltou, com lista fechada
        self.assertIn('name="acento" value="roxo"', t)
        self.assertIn("As alterações valem na hora", t)
        for sumiu in ("Tom do vidro", "Plano de fundo", "aj-hero", 'name="vidro"', 'name="estilo"', 'acento_livre'):
            self.assertNotIn(sumiu, t, sumiu)
        r = ajustes.ROTAS["/ajustes"](self.req("POST", {"fonte": "15", "tema": "escuro", "acento": "livre", "acento_livre": "#123456"}))
        self.assertEqual((r.status, dict(r.cabecalhos)["Location"]), (303, "/ajustes?salvo=1"))
        self.assertEqual(json.loads(pf.ARQUIVO.read_text()), {**pf.PADRAO, "fonte": 15, "tema": "escuro"})
        self.assertIn("Salvo neste Mac.", texto(ajustes.ROTAS["/ajustes"](self.req("GET", query={"salvo": "1"}))))
        ajustes.ROTAS["/ajustes"](self.req("POST", {"restaurar": "1"}))
        self.assertEqual(pf.ler(), pf.PADRAO)

    def test_post_de_fora_recusado(self):
        r = ajustes.ROTAS["/ajustes"](self.req("POST", {"fonte": "15"}, cab={"sec-fetch-site": "cross-site"}))
        self.assertEqual(r.status, 403)
        self.assertFalse(pf.ARQUIVO.exists())

    def test_post_automatico_devolve_css_da_previa(self):
        r = ajustes.ROTAS["/ajustes"](self.req("POST", {"fonte": "14", "tema": "claro"}, cab={"x-vigia-auto": "1"}))
        self.assertEqual((r.status, r.tipo), (200, "application/json; charset=utf-8"))
        j = json.loads(r.corpo)
        self.assertEqual(j["attrs"]["data-tema"], "claro")
        self.assertEqual(j["css"], "html { font-size:14px; }")
        self.assertEqual(pf.ler(), {**pf.PADRAO, "fonte": 14, "tema": "claro"})
        j = json.loads(ajustes.ROTAS["/ajustes"](self.req("POST", {"tema": "sistema"}, cab={"x-vigia-auto": "1"})).corpo)
        self.assertIsNone(j["attrs"]["data-tema"])
        self.assertEqual(j["css"], "")

    def test_rotas_de_fundo_sumiram(self):
        self.assertEqual(set(ajustes.ROTAS), {"/ajustes"})
        self.assertFalse(hasattr(pf, "FUNDOS"))
        self.assertFalse(hasattr(pf, "ESTILOS"))

    def test_rota_conexao_continua_intacta(self):
        self.assertIn("/conexao", conexao.ROTAS)


if __name__ == "__main__":
    unittest.main()
