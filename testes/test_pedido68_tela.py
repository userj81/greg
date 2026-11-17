"""Pedido 68: seção "IA" de Ajustes, avisos da IA por API e a bancada de qualidade (lotes fictícios, modo sombra, régua).
Servidor falso em 127.0.0.1; nenhum provedor é chamado e nada é gravado fora da pasta temporária.
"""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from app import alertas, bancada, leitor, leitor_api, problemas, saude  # noqa: E402
from app.paginas import ajustes, ia  # noqa: E402
from testes.fakes_ia import ServidorIA, erro, resposta  # noqa: E402
from testes.test_paginas import Base, texto  # noqa: E402

CHAVE = "sk-or-v1-chave_de_teste_0123456789"


class BaseIA(Base):
    def post(self, corpo, cab=None):
        return ia.ROTAS["/ajustes/ia"](self.req("POST", "/ajustes/ia", corpo, cab=cab))

    def pagina(self, **query):
        return texto(ajustes.ROTAS["/ajustes"](self.req("GET", "/ajustes", query=query)))

    def servidor(self, roteiro):
        s = ServidorIA(roteiro).iniciar()
        self.addCleanup(s.parar)
        return s


class TestSecaoIA(BaseIA):
    def test_padrao_e_claude_code_e_a_secao_aparece(self):
        h = self.pagina()
        self.assertIn('<p class="rotulo" id="ia">IA</p>', h)
        self.assertLess(h.index('id="ia"'), h.index('id="claude"'))
        self.assertIn('data-provedor="claude-code"', h)
        for rotulo in ("Claude Code", "OpenRouter", "Outro compatível", "Local (experimental)"):
            self.assertIn(f">{rotulo}</label>", h)
        self.assertRegex(h, r'name="provedor" value="claude-code" checked')
        self.assertIn("Teto de gasto por mês", h)
        self.assertIn('name="teto_mes_usd" min="0.1" max="10000" step="0.1" value="5"', h)      # padrão: US$ 5 por mês
        self.assertIn("Gasto neste mês: US$ 0,00 de US$ 5,00", h)
        self.assertIn('value="testar"', h)
        self.assertFalse((self.dados / "ia.json").exists())      # abrir a tela não grava nada

    def test_salvar_grava_a_escolha_e_a_chave_nunca_volta_para_a_tela(self):
        r = self.post({"acao": "salvar", "provedor": "openrouter", "modelo": "fabricante/modelo-x", "chave": CHAVE,
                       "endereco": "https://ignorado.exemplo/v1", "teto_mes_usd": "3.5", "visao": "0"})
        self.assertEqual(r.status, 303)
        cfg = leitor.configuracao({}, self.dados)
        self.assertEqual((cfg["motor"], cfg["provedor"], cfg["endereco"], cfg["modelo"], cfg["teto_mes_usd"], cfg["visao"]),
                         ("api", "openrouter", "https://openrouter.ai/api/v1", "fabricante/modelo-x", 3.5, False))
        self.assertEqual(leitor.chave(self.dados), CHAVE)
        h = self.pagina(ia="salvo")
        self.assertNotIn(CHAVE, h)
        self.assertNotIn(CHAVE[:12], h)
        self.assertIn('name="chave" value=""', h)
        self.assertIn("Há uma chave guardada", h)
        self.assertIn("Salvo. Vale a partir da próxima leitura.", h)
        self.assertIn("Gasto neste mês: US$ 0,00 de US$ 3,50", h)
        (self.dados / "ia-gasto.json").write_text(json.dumps({"mes": leitor_api.mes_corrente(), "usd": 1.234}))
        self.assertIn("Gasto neste mês: US$ 1,23 de US$ 3,50", self.pagina())        # a tela mostra o acumulado do mês
        self.assertNotIn(CHAVE, (self.dados / "ia.json").read_text())

    def test_salvar_sem_chave_mantem_a_guardada_e_apagar_remove(self):
        self.post({"acao": "salvar", "provedor": "openrouter", "modelo": "m/x", "chave": CHAVE})
        self.post({"acao": "salvar", "provedor": "openrouter", "modelo": "m/y", "chave": ""})
        self.assertEqual(leitor.chave(self.dados), CHAVE)
        self.assertEqual(self.post({"acao": "apagar-chave"}).status, 303)
        self.assertEqual(leitor.chave(self.dados), "")
        self.assertIn("Cole a chave do provedor", self.pagina())

    def test_voltar_ao_claude_code_guarda_o_que_estava(self):
        self.post({"acao": "salvar", "provedor": "local", "endereco": "http://localhost:11434/v1", "modelo": "qwen3:8b"})
        self.post({"acao": "salvar", "provedor": "claude-code"})
        cfg = leitor.configuracao({}, self.dados)
        self.assertEqual((cfg["motor"], cfg["endereco"], cfg["modelo"]), ("claude-code", "http://localhost:11434/v1", "qwen3:8b"))

    def test_entrada_invalida_nao_grava_e_diz_por_que(self):
        casos = [({"provedor": "outro", "endereco": "http://fora.exemplo/v1", "modelo": "m"}, "precisa ser https"),
                 ({"provedor": "outro", "endereco": "https://a.exemplo/v1", "modelo": ""}, "informe o nome do modelo"),
                 ({"provedor": "local", "endereco": "https://a.exemplo/v1", "modelo": "m"}, "tem de ser deste Mac"),
                 ({"provedor": "inventado"}, "escolha quem lê"),
                 ({"provedor": "openrouter", "modelo": "m", "chave": "curta"}, "chave fora do formato")]
        for corpo, trecho in casos:
            r = self.post({"acao": "salvar", **corpo})
            self.assertEqual(r.status, 400, corpo)
            self.assertIn(trecho, texto(r))
            self.assertIn("Não salvou", texto(r))
        self.assertFalse((self.dados / "ia.json").exists())
        self.assertFalse((self.dados / ".env.ia").exists())
        self.assertEqual(self.post({"acao": "outra"}).status, 400)

    def test_testar_chama_o_endereco_e_responde_em_uma_frase(self):
        srv = self.servidor([resposta(None, [("confirmar", {"ok": True})]), erro(401, "invalid_api_key")])
        corpo = {"acao": "testar", "provedor": "local", "endereco": srv.url, "modelo": "modelo-local"}
        r = self.post(corpo, cab={"x-vigia-auto": "1"})
        j = json.loads(texto(r))
        self.assertEqual((r.status, j["funcionou"], j["salvo"]), (200, True, True))
        self.assertEqual(j["frase"], "Funcionou: o modelo respondeu e sabe chamar função.")
        self.assertEqual(srv.pedidos[0]["corpo"]["model"], "modelo-local")
        self.assertEqual(leitor.configuracao({}, self.dados)["modelo"], "modelo-local")     # testar grava o que está na tela
        j = json.loads(texto(self.post(corpo, cab={"x-vigia-auto": "1"})))
        self.assertEqual((j["funcionou"], j["frase"]), (False, "Não funcionou: o provedor recusou a chave."))
        h = texto(self.post(corpo))                    # sem JS: a frase vem na própria página
        self.assertIn('class="ia-aviso', h)
        self.assertEqual(len(srv.pedidos), 3)

    def test_testar_que_passa_fecha_o_aviso_de_ia_e_o_que_falha_nao(self):
        """QA de 14/11: o teste por chave aprovado em Ajustes não fechava o aviso de leitura parada."""
        from app import alertas
        srv = self.servidor([erro(401, "invalid_api_key"), resposta(None, [("confirmar", {"ok": True})])])
        corpo = {"acao": "testar", "provedor": "local", "endereco": srv.url, "modelo": "modelo-local"}
        alertas.abrir(self.dados, "ia-login")
        self.assertFalse(json.loads(texto(self.post(corpo, cab={"x-vigia-auto": "1"})))["funcionou"])
        self.assertEqual([a["codigo"] for a in alertas.abertos(self.dados)], ["ia-login"])
        self.assertTrue(json.loads(texto(self.post(corpo, cab={"x-vigia-auto": "1"})))["funcionou"])
        self.assertEqual(alertas.abertos(self.dados), [])

    def test_testar_com_claude_code_nao_chama_ninguem(self):
        with mock.patch.object(leitor_api, "_pedir") as pedir:
            j = json.loads(texto(self.post({"acao": "testar", "provedor": "claude-code"}, cab={"x-vigia-auto": "1"})))
        pedir.assert_not_called()
        self.assertIn("login do Claude deste Mac", j["frase"])

    def test_so_a_sessao_do_painel_mexe_na_ia(self):
        self.assertEqual(self.post({"acao": "salvar", "provedor": "claude-code"}, cab={"origin": "http://site.exemplo"}).status, 403)
        self.assertEqual(self.post({"acao": "salvar", "provedor": "claude-code"}, cab={"authorization": "Bearer x"}).status, 403)
        self.assertEqual(ia.ROTAS["/ajustes/ia"](self.req("GET", "/ajustes/ia")).status, 405)
        self.assertFalse((self.dados / "ia.json").exists())


class TestAvisos(unittest.TestCase):
    NOVOS = ("ia-chave", "ia-saldo", "ia-api-limite", "ia-teto", "ia-api-fora", "ia-api-erro")

    def test_catalogo_tem_os_avisos_da_ia_por_api_sem_dizer_claude(self):
        for cod in self.NOVOS:
            p = problemas.CATALOGO[cod]
            self.assertEqual(p.peca, "ia")
            self.assertNotIn("Claude", p.titulo + p.por_que + " ".join(p.passos), cod)
            self.assertEqual(p.link, "/ajustes")
        self.assertIn("chave", problemas.CATALOGO["ia-chave"].titulo)
        self.assertIn("sem saldo", problemas.CATALOGO["ia-saldo"].titulo)
        self.assertIn("limit", problemas.CATALOGO["ia-api-limite"].titulo)
        self.assertNotIn("Claude", saude.ROTULO_PECA["ia"])

    def test_saude_trata_todo_aviso_de_ia_como_um_so_motivo_por_vez(self):
        self.assertEqual(set(saude._IA), {c for c, p in problemas.CATALOGO.items() if p.peca == "ia"})
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            alertas.abrir(d, "ia-login")
            saude.do_coletor(d, leitor_api.ChaveRecusada("chave recusada: openrouter.ai respondeu 401"))
            abertos = {a["codigo"]: a for a in alertas.abertos(d)}
            self.assertEqual(set(abertos), {"ia-chave"})
            saude.do_coletor(d, leitor_api.TetoDeGasto("teto de gasto do mês atingido (US$ 5.00 de US$ 5.00)"))
            self.assertEqual({a["codigo"] for a in alertas.abertos(d)}, {"ia-teto"})
            p = problemas.CATALOGO["ia-teto"]
            self.assertIn("teto de gasto do mês", p.titulo)
            self.assertIn("aumente o teto em Ajustes", p.passos[0])
            self.assertIn("virada do mês", p.passos[1])
            self.assertTrue(p.imediato)
            saude.do_coletor(d, None, {"status": "ok"})
            self.assertEqual(alertas.abertos(d), [])

    def test_erros_do_claude_code_continuam_com_os_avisos_de_antes(self):
        from app import lotes
        self.assertEqual(saude.codigo_do_erro(lotes.ClaudeSemLogin("x")), "ia-login")
        self.assertEqual(saude.codigo_do_erro(lotes.ErroClaude("claude respondeu com erro: usage limit reached")), "ia-limite")
        self.assertEqual(saude.codigo_do_erro(lotes.ErroClaude("claude não executou (FileNotFoundError)")), "ia-ausente")
        self.assertEqual(saude.codigo_do_erro(lotes.ErroClaude("claude excedeu 900 s")), "ia-erro")
        for cod in ("ia-login", "ia-limite", "ia-ausente", "ia-erro"):
            self.assertIn("Claude", problemas.CATALOGO[cod].por_que + problemas.CATALOGO[cod].titulo)

    def test_coletor_nao_reduz_o_lote_por_falha_que_nao_e_do_lote(self):
        fonte = (RAIZ / "bin" / "coletor.py").read_text(encoding="utf-8")
        self.assertIn('getattr(e, "nao_e_do_lote", False)', fonte)
        self.assertIn("leitor.ler(pasta,", fonte)
        self.assertNotIn("lotes.chamar_claude(", fonte)
        self.assertNotIn("lotes.chamar_claude(", (RAIZ / "app" / "consolidacao.py").read_text(encoding="utf-8"))


class TestBancada(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.saida = Path(self.tmp.name) / "bancada"

    def test_casos_cobrem_o_que_o_estudo_pediu(self):
        self.assertGreaterEqual(len(bancada.CASOS), 30)
        ids = [c["id"] for c in bancada.CASOS]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual({c["classe"] for c in bancada.CASOS}, {"atencao", "demanda", "lembrar", "lembrando", "aviso", "sem_acao"})
        tudo = json.dumps([c["msgs"] for c in bancada.CASOS], ensure_ascii=False)
        self.assertIn("transcrição automática", tudo)
        self.assertIn("[imagem: @", tudo)
        self.assertTrue(any("cancela" in c["espera"] for c in bancada.CASOS))
        self.assertTrue(any("substitui" in c["espera"] for c in bancada.CASOS))
        self.assertGreaterEqual(sum(c["classe"] == "atencao" for c in bancada.CASOS), 5)
        for c in bancada.CASOS:
            self.assertIn(c["alvo"], [m[1] for m in c["msgs"] if m[2] == "n"], c["id"])
            self.assertNotIn(c["classe"], c["aceita"], c["id"])

    def test_lotes_ficticios_no_formato_do_coletor_e_so_numero_de_ficcao(self):
        pastas = bancada.gerar(self.saida)
        feitos = [p for p in pastas.values() if p is not None]
        self.assertGreaterEqual(len(feitos), 30)
        gab = json.loads((self.saida / "gabarito.json").read_text(encoding="utf-8"))
        self.assertEqual(set(gab), set(pastas))
        for p in feitos:
            conversa = (p / "conversa" / "conversa.md").read_text(encoding="utf-8")
            jid = re.search(r"^conversa: (\S+)$", conversa, re.M).group(1)
            self.assertRegex(jid, r"^(141555501\d\d@s\.whatsapp\.net|120363000000000\d{3}@g\.us)$")
            self.assertRegex(conversa, r"\n\[2026-11-1\d \d\d:\d\d\] \[id:\w+\] \[novo\] \[(eu|contato)\] ")
            self.assertIn("hoje: 2026-11-16", conversa)
            self.assertTrue((p / "conversa" / "contexto.md").read_text(encoding="utf-8").startswith("> Dados do Frentes"))
            man = json.loads((p / "conversa" / "mensagens.json").read_text(encoding="utf-8"))
            self.assertTrue(man)
            for m in re.findall(r"\[imagem: (img-[^\]]+)\]", conversa):
                self.assertEqual((p / "conversa" / m).read_bytes()[:2], b"\xff\xd8")
            mensagens, info = leitor_api.montar_mensagens(p, RAIZ / "prompts" / "vigia.md", "lote")
            self.assertEqual(info["conversas"], 1)

    def test_modo_sombra_nao_grava_nada_e_confere_como_a_ponte(self):
        caso = next(c for c in bancada.CASOS if c["id"] == "lembrando-proposta")
        pasta = bancada.gerar_lote(caso, self.saida / "lotes", 1)
        antes = {p: p.read_bytes() for p in self.saida.rglob("*") if p.is_file()}
        s = bancada.Sombra(pasta)
        ok = s("classificar", {"msg_id": "l1", "classe": "demanda", "resumo": "Ana cobra a proposta", "confianca": "baixa"})
        self.assertEqual((ok["ok"], ok["cartao_item"], ok["sombra"]), (True, True, True))
        self.assertTrue(s("atualizar", {"item_id": 41, "nota": "cobrou", "msg": "l1"})["ok"])
        self.assertTrue(s("historico", {"msg_id": "l1", "resumo": "cobrança", "situacao": "aguarda_dono"})["ok"])
        self.assertEqual(len(s("frentes", {})["frentes"]), 3)
        self.assertEqual(s("classificar", {"msg_id": "fora", "classe": "aviso", "resumo": "x", "confianca": "alta"}),
                         {"ok": False, "erro": "msg_id fora do lote"})
        self.assertFalse(s("classificar", {"msg_id": "l1", "classe": "aviso", "resumo": "tem ; proibido", "confianca": "alta"})["ok"])
        self.assertFalse(s("atualizar", {"item_id": 999, "nota": "x", "msg": "l1"})["ok"])
        self.assertEqual(len(s.chamadas), 7)
        self.assertEqual(antes, {p: p.read_bytes() for p in self.saida.rglob("*") if p.is_file()})
        fonte = (RAIZ / "app" / "bancada.py").read_text(encoding="utf-8")
        for proibido in ("Banco(", "criar_ponte", "gravar_classificacao", "PontePorProcesso", "vigia.db\")"):
            self.assertNotIn(proibido, fonte)

    def _resultados(self, erros=(), sem=()):
        res = {}
        for c in bancada.CASOS:
            if c["id"] in sem:
                res[c["id"]] = {"chamadas": []}
                continue
            classe = "sem_acao" if c["id"] in erros else c["classe"]
            if classe == "lembrando":
                ch = {"funcao": "atualizar", "ok": True, "args": {"item_id": c["espera"]["item"], "msg": c["alvo"],
                                                                 "prioridade": c["espera"].get("prioridade")}}
            else:
                ch = {"funcao": "classificar", "ok": True,
                      "args": {"msg_id": c["alvo"], "classe": classe, **{k: v for k, v in c["espera"].items()}}}
            res[c["id"]] = {"chamadas": [ch]}
        return res

    def test_regua_90_por_cento_e_nenhuma_atencao_perdida(self):
        n = len(bancada.CASOS)
        tudo = bancada.comparar(self._resultados())
        self.assertEqual((tudo["casos"], tudo["certos"], tudo["aprovado"], tudo["atencao_perdidos"]), (n, n, True, []))
        self.assertEqual(tudo["secundarios_ok"], tudo["secundarios_total"])
        # três erros fora de atenção em 36 = 91,7%: passa. Quatro = 88,9%: não passa.
        fora = [c["id"] for c in bancada.CASOS if c["classe"] in ("demanda", "lembrar") and "sem_acao" not in c["aceita"]]
        self.assertTrue(bancada.comparar(self._resultados(erros=fora[:3]))["aprovado"])
        quatro = bancada.comparar(self._resultados(erros=fora[:4]))
        self.assertEqual((quatro["aprovado"], quatro["atencao_perdidos"]), (False, []))
        self.assertLess(quatro["acerto"], 0.9)
        # uma atenção perdida reprova mesmo com 97% de acerto
        uma = bancada.comparar(self._resultados(erros=["atencao-site"]))
        self.assertEqual((uma["aprovado"], uma["atencao_perdidos"]), (False, ["atencao-site"]))
        self.assertGreater(uma["acerto"], 0.9)
        # mensagem que a IA nem classificou conta como erro; atenção não classificada conta como perdida
        self.assertEqual(bancada.comparar(self._resultados(sem=["audio-atencao"]))["atencao_perdidos"], ["audio-atencao"])

    def test_classe_aceita_vale_erro_e_pulado_ficam_claros(self):
        res = self._resultados()
        res["aviso-materia"]["chamadas"][0]["args"]["classe"] = "sem_acao"          # o prompt admite as duas
        res["semacao-ok"] = {"erro": "a IA passou do teto de 40 voltas no lote"}
        res["imagem-comprovante"] = {"pulado": "modelo sem visão"}
        res["cancela-reuniao"]["chamadas"][0]["args"]["cancela"] = 99                # classe certa, id errado
        p = bancada.comparar(res)
        linhas = {l["id"]: l for l in p["linhas"]}
        self.assertTrue(linhas["aviso-materia"]["acertou"])
        self.assertFalse(linhas["semacao-ok"]["acertou"])
        self.assertIsNone(linhas["imagem-comprovante"]["acertou"])
        self.assertEqual(p["casos"], len(bancada.CASOS) - 1)
        self.assertIn("cancela ERRADO", linhas["cancela-reuniao"]["nota"])
        self.assertTrue(linhas["cancela-reuniao"]["acertou"])                        # secundário não entra na régua
        md = bancada.markdown(p, modelo="m", endereco="http://localhost:1/v1", gasto_usd=0.0)
        self.assertIn("| semacao-ok | sem_acao | - | ERRADO |", md)
        self.assertIn("| imagem-comprovante | aviso | - | pulado | modelo sem visão |", md)

    def test_rodada_inteira_em_modo_sombra_contra_servidor_falso(self):
        """Um "modelo" falso que responde o gabarito: prova o caminho inteiro (lote → leitor por API → sombra → régua)."""
        por_lote = {f"{n:02d}-{c['id']}": c for n, c in enumerate(bancada.CASOS, 1)}

        def modelo(pedido):
            msgs = pedido["corpo"]["messages"]
            if msgs[-1]["role"] == "tool":
                return resposta('{"lote":"x","conversas":1,"novas":1,"itens":0,"atencao":0}')
            cabeca = msgs[1]["content"][0]["text"]
            c = por_lote[re.search(r"nome do lote: ([\w-]+)\)", cabeca).group(1)]
            if c["classe"] == "lembrando":
                return resposta(None, [("atualizar", {"item_id": c["espera"]["item"], "nota": "cobrou de novo", "msg": c["alvo"]})])
            args = {"msg_id": c["alvo"], "classe": c["classe"], "resumo": "resumo de teste", "confianca": "alta"}
            args.update({k: v for k, v in c["espera"].items() if k in ("prazo", "substitui", "cancela", "confianca")})
            return resposta(None, [("classificar", args), ("historico", {"msg_id": c["alvo"], "resumo": "lida", "situacao": "encerrada"})])

        srv = ServidorIA([modelo]).iniciar()
        self.addCleanup(srv.parar)
        cfg = leitor.normalizar({"motor": "api", "endereco": srv.url, "modelo": "gabarito"})
        avisos = []
        placar, resultados = bancada.rodar(cfg, "", self.saida, avisar=avisos.append)
        self.assertTrue(placar["aprovado"], [l for l in placar["linhas"] if not l["acertou"]])
        self.assertEqual(placar["certos"], placar["casos"])
        self.assertGreaterEqual(placar["casos"], 30)
        self.assertEqual(len(srv.pedidos), 2 * placar["casos"])
        self.assertEqual(len(avisos), len(bancada.CASOS))
        self.assertFalse(list(self.saida.rglob("*.db")))                    # a sombra não criou banco nenhum
        com_imagem = [p for p in srv.pedidos if "image_url" in json.dumps(p["corpo"]["messages"][1])]
        self.assertEqual(len(com_imagem), 2 * sum(1 for c in bancada.CASOS if c["imagens"] and resultados[c["id"]].get("chamadas")))

    def test_rodada_para_quando_a_falha_nao_e_do_caso(self):
        srv = ServidorIA([erro(401)]).iniciar()
        self.addCleanup(srv.parar)
        cfg = leitor.normalizar({"motor": "api", "endereco": srv.url, "modelo": "m"})
        placar, resultados = bancada.rodar(cfg, "", self.saida, casos=bancada.CASOS[:5])
        self.assertEqual(len(srv.pedidos), 1)
        self.assertEqual((placar["casos"], placar["aprovado"]), (0, False))
        self.assertIn("rodada interrompida", resultados["atencao-site"]["pulado"])

    def test_comando_recusa_rodar_em_endereco_pago_sem_teto(self):
        import importlib.machinery
        import importlib.util
        import io
        from contextlib import redirect_stderr, redirect_stdout
        carregador = importlib.machinery.SourceFileLoader("bancada_ia_p68", str(RAIZ / "bin" / "bancada-ia.py"))
        mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("bancada_ia_p68", carregador))
        carregador.exec_module(mod)
        err, out = io.StringIO(), io.StringIO()
        with mock.patch.object(leitor_api, "_pedir") as pedir, mock.patch.dict("os.environ", {"VIGIA_DADOS": self.tmp.name}), \
                redirect_stderr(err), redirect_stdout(out):
            self.assertEqual(mod.main(["rodar", "--endereco", "https://provedor.exemplo/v1", "--modelo", "m"]), 2)
            self.assertIn("--teto-usd", err.getvalue())
            self.assertEqual(mod.main(["rodar", "--endereco", "https://provedor.exemplo/v1", "--modelo", "m", "--teto-usd", "1"]), 2)
            self.assertIn("não há chave guardada", err.getvalue())
            self.assertEqual(mod.main(["rodar"]), 2)                         # sem outra IA configurada: nada a medir
            self.assertEqual(mod.main(["gerar", "--saida", str(self.saida)]), 0)
        pedir.assert_not_called()
        self.assertTrue((self.saida / "gabarito.json").is_file())


if __name__ == "__main__":
    unittest.main()
