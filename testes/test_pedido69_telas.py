"""Pedido 69 (primeira instalação guiada): as telas de app/paginas/inicio.py, a seção de Ajustes e o desvio do servidor.
Quem chama: unittest. A IA é sempre falsa; o código do WhatsApp vem da Evolution falsa; o arquivo real do token de
envio nunca é tocado."""
import json
import os
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import inicio, segredos  # noqa: E402
from app.paginas import ajustes  # noqa: E402
from app.paginas import inicio as pg  # noqa: E402
from testes import test_server  # noqa: E402
from testes.test_conversas_zap import ANA, G1, MARCOS  # noqa: E402
from testes.test_paginas import Base, texto  # noqa: E402
from testes.test_pedido69 import IaFalsa, _arquivo_token  # noqa: E402

PRONTA = {"estado": "pronta", "em": 1, "leu": True, "banco": True, "lidas": 2, "proposta": {
    "frentes": [{"nome": "Padaria Central", "tipo": "cliente", "por_que": "você citou a padaria", "apelidos": ["padaria"],
                 "vinculos": [{"jid": ANA, "nome": "Ana Paula", "forca": "forte", "por_que": "fala da arte"},
                              {"jid": MARCOS, "nome": "Marcos <b>", "forca": "talvez", "por_que": "palpite"}]},
                {"nome": "Reforma", "tipo": "pessoal", "por_que": "a reforma da casa", "apelidos": [], "vinculos": []}],
    "grupos": [{"jid": G1, "nome": "CBA · Marketing", "por_que": "grupo do cliente"}]}}


class BaseInicio(Base):
    def setUp(self):
        super().setUp()
        self.dados.mkdir(parents=True, exist_ok=True)
        self.arq_token = _arquivo_token(self)
        self.ff.frentes = []                        # instalação nova: nenhuma frente

    def get(self, caminho, query=None):
        return pg.ROTAS[caminho](self.req("GET", caminho, query=query))

    def post(self, caminho, corpo=None):
        return pg.ROTAS[caminho](self.req("POST", caminho, corpo))

    def destino(self, r):
        self.assertEqual(r.status, 303, texto(r)[:200])
        return dict(r.cabecalhos)["Location"]

    def avancar(self, passo, acao="continuar", **extra):
        return self.post("/inicio/avancar", {"passo": passo, "acao": acao, **extra})

    def por(self, sugestao):
        inicio.mudar(self.ctx, lambda e: e.__setitem__("sugestao", json.loads(json.dumps(sugestao))))


class TestPassos(BaseInicio):
    def test_cada_passo_renderiza_um_por_tela_com_passo_n_de_8_e_pular(self):
        for n, pid in enumerate(inicio.IDS, 1):
            r = self.get(f"/inicio/{pid}")
            t = texto(r)
            self.assertEqual(r.status, 200, pid)
            self.assertIn(f"Passo {n} de 8", t, pid)
            self.assertEqual(t.count('class="grupo ini-cartao"'), 1, pid)          # T-5: um passo por tela
            self.assertIn(f'name="passo" value="{pid}"', t, pid)
            self.assertIn('value="pular"', t, pid)                                 # Pular sempre à mão
            self.assertIn("<b>Por quê</b>", t, pid)
            self.assertIn(inicio.passo(pid)["titulo"], t, pid)
            self.assertIn("Sair do início", t, pid)
            self.assertNotRegex(t.split('id="inicio"', 1)[1].split("<script", 1)[0], r"~/\.|/Users/|\.env\b")   # A-1
        self.assertEqual(self.fe.qrs_gerados, 0)                                  # abrir as telas não gasta código
        self.assertEqual(inicio.ler(self.ctx), {})                                # nem grava nada

    def test_caminho_de_quem_comeca_nao_mostra_senha_trancar_nem_menu(self):
        t = texto(self.get("/inicio/boas-vindas"))
        self.assertIn(".lateral", t.split("<style>")[-1] if "<style>" in t else t)   # a lateral é escondida pelo CSS do passo
        cartao = t.split('id="ini-boas-vindas"', 1)[1].split("</form>", 1)[0]
        for fora in ("Trocar a senha", "Trancar", 'href="/ajustes', "Sair da conta"):
            self.assertNotIn(fora, cartao)

    def test_entrada_leva_ao_primeiro_passo_e_depois_aonde_parou(self):
        self.assertEqual(self.destino(self.get("/inicio")), "/inicio/boas-vindas")
        self.assertEqual(self.destino(self.avancar("boas-vindas")), "/inicio/whatsapp")
        self.assertEqual(self.destino(self.get("/inicio")), "/inicio/whatsapp")

    def test_continuar_em_cada_passo_avanca(self):
        self.ctx.tokens.envio = ""
        self.assertEqual(self.destino(self.avancar("boas-vindas")), "/inicio/whatsapp")
        self.assertEqual(self.destino(self.avancar("whatsapp")), "/inicio/ia")
        self.assertEqual(self.destino(self.avancar("ia")), "/inicio/envio")
        self.assertEqual(self.destino(self.avancar("envio")), "/inicio/descricao")
        self.assertEqual(self.destino(self.avancar("descricao", texto="Tenho uma agência.")), "/inicio/sugestao")
        self.assertEqual(self.destino(self.avancar("sugestao")), "/inicio/revisao")
        self.assertEqual(self.destino(self.avancar("revisao")), "/inicio/revisao")
        self.assertEqual(self.destino(self.avancar("roteiro")), "/demandas?roteiro=1")
        e = inicio.ler(self.ctx)
        self.assertEqual(e["estado"], "concluido")
        self.assertEqual(inicio.descricao(self.ctx), "Tenho uma agência.")
        self.assertEqual(stat.S_IMODE(os.stat(self.arq_token).st_mode), 0o600)     # T-3: o envio ficou pronto
        self.assertFalse(inicio.deve_aparecer(self.ctx))

    def test_pular_em_cada_passo_avanca_sem_fazer_nada(self):
        self.ctx.tokens.envio = ""
        esperado = {"boas-vindas": "/inicio/whatsapp", "whatsapp": "/inicio/ia", "ia": "/inicio/envio",
                    "envio": "/inicio/descricao", "descricao": "/inicio/sugestao", "sugestao": "/inicio/roteiro",
                    "revisao": "/inicio/roteiro", "roteiro": "/demandas"}
        for pid in inicio.IDS:
            self.assertEqual(self.destino(self.avancar(pid, "pular")), esperado[pid], pid)
        e = inicio.ler(self.ctx)
        self.assertEqual(e["estado"], "concluido")
        self.assertEqual(set(e["feitos"].values()), {"pulado"})
        self.assertFalse(self.arq_token.exists())            # pulou o envio: nenhuma chave criada
        self.assertEqual(self.ctx.tokens.envio, "")
        self.assertEqual((self.fe.qrs_gerados, inicio.descricao(self.ctx)), (0, ""))
        self.assertNotIn(("POST", "/api/frentes"), self.ff.chamadas)

    def test_pedido_torto_e_recusado(self):
        self.assertEqual(self.avancar("inexistente").status, 400)
        self.assertEqual(self.avancar("ia", "apagar-tudo").status, 400)
        self.assertEqual(pg.ROTAS["/inicio/avancar"](self.req("GET", "/inicio/avancar")).status, 405)
        r = pg.ROTAS["/inicio/avancar"](self.req("POST", "/inicio/avancar", {"passo": "ia"},
                                                 cab={"Host": "127.0.0.1:8765", "Origin": "http://mal.example"}))
        self.assertEqual(r.status, 403)

    def test_sair_do_inicio_nao_volta_sozinho(self):
        self.avancar("boas-vindas")
        self.assertEqual(self.destino(self.post("/inicio/sair")), "/demandas")
        self.assertFalse(inicio.deve_aparecer(self.ctx))


class TestScripts(BaseInicio):
    def test_todo_script_do_assistente_e_js_valido(self):
        node = next((x for x in ("/opt/homebrew/bin/node", "/usr/local/bin/node", "/usr/bin/node") if os.path.exists(x)), None)
        if node is None:
            self.skipTest("node não instalado")
        for nome in ("_JS_COMUM", "_JS_QR", "_JS_AUDIO", "_JS_ESPERA", "_JS_REVISAO"):
            js = getattr(pg, nome)
            self.assertTrue(js.startswith("<script>") and js.rstrip().endswith("</script>"), nome)
            arq = Path(self.tmp.name) / f"{nome}.js"
            arq.write_text(js.strip()[len("<script>"):-len("</script>")], encoding="utf-8")
            r = subprocess.run([node, "--check", str(arq)], capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, f"{nome}: {r.stderr[:400]}")


class TestWhatsapp(BaseInicio):
    def test_codigo_so_no_clique_e_aparece_na_tela(self):
        t = texto(self.get("/inicio/whatsapp"))
        self.assertIn("Mostrar o código", t)
        self.assertIn("último dia", t)                       # D-53: diz o que entra de histórico
        self.assertNotIn('id="qr-img"', t)
        self.assertEqual(self.destino(self.post("/inicio/whatsapp/qr")), "/inicio/whatsapp")
        self.post("/inicio/whatsapp/qr")                     # clique duplo não gasta outro
        self.assertEqual(self.fe.qrs_gerados, 1)
        t = texto(self.get("/inicio/whatsapp"))
        self.assertIn('id="qr-img"', t)
        self.assertIn('src="data:image/png;base64,', t)
        self.assertIn("/conexao/tick", t)
        self.assertIn('value="pular"', t)

    def test_ligado_mostra_pronto_e_deixa_continuar(self):
        self.fe.estado = "open"
        t = texto(self.get("/inicio/whatsapp"))
        self.assertIn("WhatsApp ligado.", t)
        self.assertIn('value="continuar"', t)
        self.assertNotIn("Mostrar o código", t)

    def test_teto_do_dia_desliga_o_botao_e_explica(self):
        for _ in range(self.conexao.teto_dia):
            self.banco.registrar_qr("teste")
        t = texto(self.get("/inicio/whatsapp"))
        self.assertIn("limite de códigos de hoje", t)
        self.assertRegex(t, r'data-espera="Preparando o código…" disabled')
        r = self.post("/inicio/whatsapp/qr")
        self.assertEqual(r.status, 429)
        self.assertEqual(self.fe.qrs_gerados, 0)

    def test_codigo_vencido_e_ligacao_presa_dizem_o_que_fazer(self):
        self.assertIn("O código venceu sem ser lido", texto(self.get("/inicio/whatsapp", {"expirou": "1"})))
        self.assertIn("A ligação ficou presa", texto(self.get("/inicio/whatsapp", {"presa": "1"})))


class TestIa(BaseInicio):
    def test_testar_com_ia_boa(self):
        t = texto(self.get("/inicio/ia"))
        self.assertIn("Ainda não testada", t)
        self.assertIn('action="/inicio/ia/testar"', t)
        self.ctx.executor_ia = IaFalsa(result='{"ok": 1}')
        self.assertEqual(self.destino(self.post("/inicio/ia/testar")), "/inicio/ia")
        t = texto(self.get("/inicio/ia"))
        self.assertIn("A IA respondeu.", t)
        self.assertIn('value="continuar"', t)

    def test_ia_fora_explica_e_deixa_seguir(self):
        self.ctx.executor_ia = IaFalsa(excecao=FileNotFoundError("claude"))
        self.post("/inicio/ia/testar")
        t = texto(self.get("/inicio/ia"))
        self.assertIn('class="ini-erro" role="alert"', t)
        self.assertIn("Instale o Claude Code.", t)
        self.assertIn("Testar de novo", t)
        self.assertIn("Você pode seguir assim mesmo", t)
        self.assertEqual(self.destino(self.avancar("ia", "pular")), "/inicio/envio")

    def test_area_da_ia_e_uma_funcao_so(self):
        """Ponto de troca do pedido 68: a tela pergunta tudo a area_ia; trocar a função troca o passo."""
        antes = pg.area_ia
        self.addCleanup(lambda: setattr(pg, "area_ia", antes))
        pg.area_ia = lambda ctx, estado: ('<p id="outra-ia">Escolha a sua IA</p>', False)
        self.assertIn('id="outra-ia"', texto(self.get("/inicio/ia")))


class TestEnvio(BaseInicio):
    def test_sem_token_cria_ao_continuar_e_explica_a_liberacao(self):
        self.ctx.tokens.envio = ""
        t = texto(self.get("/inicio/envio"))
        self.assertIn("cria a chave de envio", t)
        self.assertFalse(self.arq_token.exists())            # abrir a tela não cria
        self.avancar("envio")
        self.assertEqual(stat.S_IMODE(os.stat(self.arq_token).st_mode), 0o600)
        self.assertTrue(self.ctx.tokens.envio)
        t = texto(self.get("/inicio/envio"))
        self.assertIn("O envio já está pronto", t)
        self.assertNotIn(self.ctx.tokens.envio, t)           # a chave nunca vai para a tela

    def test_com_token_nao_cria_outro(self):
        antes = self.ctx.tokens.envio
        self.avancar("envio")
        self.assertEqual(self.ctx.tokens.envio, antes)
        self.assertFalse(self.arq_token.exists())

    def test_disco_recusando_nao_quebra_a_tela(self):
        self.ctx.tokens.envio = ""
        segredos.ARQUIVO_POR_CHAVE["VIGIA_TOKEN_ENVIO"] = Path(self.tmp.name) / "nao-existe" / "x" / "envio.env"
        original = segredos._escrever_0600
        self.addCleanup(lambda: setattr(segredos, "_escrever_0600", original))

        def falha(*a, **k):
            raise OSError("disco cheio")
        segredos._escrever_0600 = falha
        r = self.avancar("envio")
        self.assertEqual(r.status, 500)
        self.assertIn("não conseguiu guardar a chave de envio", texto(r))
        self.assertIn('value="pular"', texto(r))


class TestDescricao(BaseInicio):
    def test_vazia_pede_texto_ou_pular(self):
        r = self.avancar("descricao", texto="   ")
        self.assertEqual(r.status, 400)
        self.assertIn("Escreva ao menos uma frase", texto(r))

    def test_texto_volta_escapado(self):
        self.avancar("descricao", texto='Atendo a <script>alert(1)</script> "Padaria"')
        t = texto(self.get("/inicio/descricao"))
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", t)
        self.assertNotIn("<script>alert(1)", t)

    def test_gravar_audio_so_aparece_com_transcricao(self):
        antes = inicio.pode_transcrever
        self.addCleanup(lambda: setattr(inicio, "pode_transcrever", antes))
        inicio.pode_transcrever = lambda: False
        self.assertNotIn('id="ini-gravar"', texto(self.get("/inicio/descricao")))
        inicio.pode_transcrever = lambda: True
        t = texto(self.get("/inicio/descricao"))
        self.assertIn('id="ini-gravar"', t)
        self.assertIn("não fica guardado", t)

    def test_audio_vira_texto_e_falha_vira_mensagem(self):
        antes = inicio.transcrever_audio
        self.addCleanup(lambda: setattr(inicio, "transcrever_audio", antes))
        inicio.transcrever_audio = lambda ctx, wav: "tenho uma agência"
        r = self.post("/inicio/audio", {"_bruto": b"RIFF....WAVE"})
        self.assertEqual((r.status, json.loads(texto(r))), (200, {"texto": "tenho uma agência"}))

        def recusa(ctx, wav):
            raise inicio.AudioRecusado("Não deu para entender o áudio. Escreva o texto.")
        inicio.transcrever_audio = recusa
        r = self.post("/inicio/audio", {"_bruto": b"RIFF....WAVE"})
        self.assertEqual((r.status, json.loads(texto(r))["mensagem"]), (422, "Não deu para entender o áudio. Escreva o texto."))
        self.assertEqual(self.post("/inicio/audio", {}).status, 400)

    def test_audio_que_nao_e_wav_e_recusado_sem_chamar_motor(self):
        chamado = []
        self.ctx.executor_motor = lambda *a, **k: chamado.append(1)
        with self.assertRaises(inicio.AudioRecusado):
            inicio.transcrever_audio(self.ctx, b"isto nao e audio")
        self.assertEqual(chamado, [])


class TestSugestaoTela(BaseInicio):
    def test_antes_de_pedir_avisa_o_que_vai_para_a_ia(self):
        t = texto(self.get("/inicio/sugestao"))
        for trecho in ("O que vai para a IA:", "sem os números", "Quem lê é o Claude", "Seguir sem ler as conversas",
                       "Pedir a sugestão", "Você ainda não contou o que faz", "O WhatsApp ainda não está ligado"):
            self.assertIn(trecho, t)

    def test_pedir_chama_a_ia_uma_vez_e_fica_pronta_sem_criar_nada(self):
        inicio.gravar_descricao(self.ctx, "Tenho uma agência.")
        self.ctx.executor_ia = IaFalsa()
        self.assertEqual(self.destino(self.post("/inicio/sugestao/pedir", {"ler": "0"})), "/inicio/sugestao")
        self.ctx.inicio_thread.join(20)
        self.assertEqual(len(self.ctx.executor_ia.chamadas), 1)
        self.assertEqual(json.loads(texto(self.get("/inicio/sugestao/estado"))), {"estado": "pronta"})
        t = texto(self.get("/inicio/sugestao"))
        self.assertIn("A proposta está pronta.", t)
        self.assertIn("As conversas não foram lidas, como você pediu", t)
        self.assertNotIn(("POST", "/api/frentes"), self.ff.chamadas)
        self.assertEqual(self.banco.vinculos(), {})

    def test_rodando_mostra_espera(self):
        self.por({"estado": "rodando", "em": int(time.time())})
        t = texto(self.get("/inicio/sugestao"))
        self.assertIn("A IA está montando a proposta.", t)
        self.assertIn("/inicio/sugestao/estado", t)
        self.assertIn("O Greg parou de responder", t)                      # pedido 77: a espera não para calada
        self.assertIn('value="pular"', t)

    def test_ia_fora_explica_e_deixa_seguir(self):
        inicio.gravar_descricao(self.ctx, "Tenho uma agência.")
        self.ctx.executor_ia = IaFalsa(excecao=FileNotFoundError("claude"))
        self.post("/inicio/sugestao/pedir", {"ler": "1"})
        self.ctx.inicio_thread.join(20)
        t = texto(self.get("/inicio/sugestao"))
        self.assertIn('class="ini-erro" role="alert"', t)
        self.assertIn("Nada foi criado.", t)
        self.assertIn("Tentar de novo", t)
        self.assertEqual(self.destino(self.avancar("sugestao", "pular")), "/inicio/roteiro")
        self.assertEqual(self.get("/inicio/revisao").status, 200)

    def test_proposta_invalida_nao_quebra_nenhuma_tela(self):
        inicio.gravar_descricao(self.ctx, "Tenho uma agência.")
        for result in ("isto não é JSON", json.dumps({"frentes": [{"nome": "A"}, {"nome": "A"}]}),
                       json.dumps({"frentes": [{"nome": f"F{i}"} for i in range(9)]})):
            self.ctx.executor_ia = IaFalsa(result=result)
            self.post("/inicio/sugestao/pedir", {"ler": "0"})
            self.ctx.inicio_thread.join(20)
            for caminho in ("/inicio/sugestao", "/inicio/revisao"):
                self.assertEqual(self.get(caminho).status, 200, result[:20])
            self.assertIn("Não há proposta para revisar.", texto(self.get("/inicio/revisao")))
        self.assertNotIn(("POST", "/api/frentes"), self.ff.chamadas)

    def test_estado_torto_no_arquivo_nao_quebra_a_revisao(self):
        for torto in ({"estado": "pronta"}, {"estado": "pronta", "proposta": {"frentes": [], "grupos": []}},
                      {"estado": "pronta", "proposta": "texto"}, "texto", {"estado": "pronta", "proposta": {"frentes": [{}]}}):
            self.por(torto)
            self.assertEqual(self.get("/inicio/revisao").status, 200, str(torto))
            self.assertEqual(self.get("/inicio/sugestao").status, 200, str(torto))


class TestRevisao(BaseInicio):
    def setUp(self):
        super().setUp()
        self.por(PRONTA)

    def test_tela_marca_o_forte_desmarca_o_talvez_e_nunca_marca_grupo(self):
        t = texto(self.get("/inicio/revisao"))
        self.assertRegex(t, r'id="f0" name="f0" value="1" checked')
        self.assertRegex(t, r'id="f1" name="f1" value="1" checked')
        self.assertIn('name="n0" value="Padaria Central"', t)             # nome editável
        self.assertIn("você citou a padaria", t)                           # o motivo em uma frase
        self.assertRegex(t, r'id="v0_0" name="v0_0" value="1" checked>')    # forte: marcado
        self.assertRegex(t, r'id="v0_1" name="v0_1" value="1">')            # talvez: desmarcado
        self.assertRegex(t, r'id="g0" name="g0" value="1">')                # grupo: nunca marcado
        self.assertNotRegex(t, r'name="g\d+" value="1" checked')
        self.assertIn("Marcos &lt;b&gt;", t)
        for botao in ("Descartar tudo", "Criar o que marquei", "Pular este passo"):
            self.assertIn(botao, t)
        for segredo in (ANA, MARCOS, G1, "5592900001111"):
            self.assertNotIn(segredo, t)                                    # nenhum número vai para a página

    def test_nada_e_criado_sem_o_post_da_revisao(self):
        for caminho in ("/inicio/revisao", "/inicio/sugestao", "/inicio/roteiro"):
            self.get(caminho)
        self.avancar("revisao")                                             # Continuar por fora do formulário não cria
        self.assertNotIn(("POST", "/api/frentes"), self.ff.chamadas)
        self.assertEqual(self.banco.vinculos(), {})
        self.assertEqual(self.post("/inicio/revisao", {"acao": "outra"}).status, 400)
        self.assertEqual(self.ff.frentes, [])

    def test_criar_o_que_marquei(self):
        r = self.post("/inicio/revisao", {"acao": "criar", "f0": "1", "n0": "Padaria do Centro", "t0": "empresa",
                                          "v0_0": "1", "g0": "1"})
        self.assertEqual(self.destino(r), "/inicio/roteiro?criadas=1&vinculadas=1&falhas=0")
        self.assertEqual([(f["nome"], f["tipo"]) for f in self.ff.frentes], [("Padaria do Centro", "empresa")])
        fid = self.ff.frentes[0]["id"]
        v = self.banco.vinculos()
        self.assertEqual(set(v), {ANA})                                     # o "talvez" desmarcado não entrou
        self.assertEqual((v[ANA]["frente_id"], v[ANA]["origem"]), (fid, "dono"))
        e = inicio.ler(self.ctx)
        self.assertEqual([g["jid"] for g in e["grupos_marcados"]], [G1])    # grupo só fica anotado (D-10)
        self.assertNotIn("grupos", self.ctx.config)
        self.assertNotIn("sugestao", e)                                     # proposta usada some
        self.assertEqual(e["feitos"]["revisao"], "feito")
        t = texto(self.get("/inicio/roteiro", {"criadas": "1", "vinculadas": "1", "falhas": "0"}))
        self.assertIn("1 frente criada, 1 conversa ligada.", t)
        # voltar no navegador e reenviar não cria de novo
        self.assertEqual(self.destino(self.post("/inicio/revisao", {"acao": "criar", "f0": "1"})), "/inicio/revisao")
        self.assertEqual(len(self.ff.frentes), 1)

    def test_conversa_que_nao_liga_nao_derruba_a_revisao(self):
        """Pedido 77: a frente já foi criada; falha ao ligar a conversa vira contagem no resumo, não erro 500."""
        def quebra(*a, **k):
            raise RuntimeError("disco")
        self.banco.vincular = quebra
        r = self.post("/inicio/revisao", {"acao": "criar", "f0": "1", "n0": "Padaria do Centro", "v0_0": "1"})
        self.assertEqual(self.destino(r), "/inicio/roteiro?criadas=1&vinculadas=0&falhas=0&semligar=1")
        self.assertEqual(len(self.ff.frentes), 1)
        t = texto(self.get("/inicio/roteiro", {"criadas": "1", "vinculadas": "0", "falhas": "0", "semligar": "1"}))
        self.assertIn("1 conversa não foi ligada: ligue na tela Contatos.", t)

    def test_post_forjado_nao_cria_o_que_nao_esta_na_proposta(self):
        self.post("/inicio/revisao", {"acao": "criar", "f7": "1", "n7": "Invasora", "v0_0": "1", "v9_9": "1", "g5": "1"})
        self.assertEqual(self.ff.frentes, [])
        self.assertEqual(self.banco.vinculos(), {})                         # vínculo de frente desmarcada não entra

    def test_sem_marcar_nada_segue_sem_criar(self):
        self.assertEqual(self.destino(self.post("/inicio/revisao", {"acao": "criar"})),
                         "/inicio/roteiro?criadas=0&vinculadas=0&falhas=0")
        self.assertEqual(self.ff.frentes, [])

    def test_nome_vazio_ou_repetido_volta_com_aviso_e_nao_cria(self):
        r = self.post("/inicio/revisao", {"acao": "criar", "f0": "1", "n0": "  ", "f1": "1"})
        self.assertEqual(r.status, 400)
        self.assertIn("Uma frente marcada ficou sem nome", texto(r))
        r = self.post("/inicio/revisao", {"acao": "criar", "f0": "1", "n0": "Reforma", "f1": "1", "n1": "reforma"})
        self.assertEqual(r.status, 400)
        self.assertIn("Duas frentes marcadas com o nome", texto(r))
        self.assertEqual(self.ff.frentes, [])
        self.assertEqual(inicio.sugestao(self.ctx)["estado"], "pronta")     # a proposta continua lá

    def test_descartar_tudo(self):
        self.assertEqual(self.destino(self.post("/inicio/revisao", {"acao": "descartar"})), "/inicio/sugestao")
        self.assertEqual(inicio.sugestao(self.ctx), {})
        self.assertEqual(self.ff.frentes, [])

    def test_frente_que_ja_existe_recebe_os_contatos(self):
        self.ff.frentes = [{"id": 7, "nome": "Padaria Central", "tipo": "cliente", "status": "ativa", "apelidos": []}]
        r = self.post("/inicio/revisao", {"acao": "criar", "f0": "1", "v0_0": "1"})
        self.assertEqual(self.destino(r), "/inicio/roteiro?criadas=0&vinculadas=1&falhas=0")
        self.assertEqual(self.banco.vinculos()[ANA]["frente_id"], 7)

    def test_frentes_fora_do_ar_conta_falha_e_nao_quebra(self):
        self.ff.parar()
        r = self.post("/inicio/revisao", {"acao": "criar", "f0": "1", "v0_0": "1", "f1": "1"})
        self.assertEqual(self.destino(r), "/inicio/roteiro?criadas=0&vinculadas=0&falhas=2")
        t = texto(self.get("/inicio/roteiro", {"criadas": "0", "vinculadas": "0", "falhas": "2"}))
        self.assertIn("2 não foram criadas: crie na tela Frentes.", t)

    def test_proposta_sem_frente_diz_isso(self):
        self.por({**PRONTA, "proposta": {"frentes": [], "grupos": []}})
        t = texto(self.get("/inicio/revisao"))
        self.assertIn("A IA não encontrou frentes para sugerir.", t)
        self.assertNotIn('id="f-rev"', t)


class TestRoteiroTela(BaseInicio):
    def test_paradas_em_lista_e_fim(self):
        t = texto(self.get("/inicio/roteiro"))
        for p in inicio.ROTEIRO:
            self.assertIn(f"<b>{p['titulo']}</b>", t)
        self.assertIn("Começar o passeio", t)
        self.assertEqual(self.destino(self.avancar("roteiro", "pular")), "/demandas")
        self.assertEqual(json.loads(texto(self.post("/inicio/roteiro/visto"))), {"ok": True})
        self.assertEqual(inicio.ler(self.ctx)["roteiro"], "visto")


class TestAjustes(BaseInicio):
    def test_secao_inicio_com_refazer_roteiro_e_descricao(self):
        inicio.gravar_descricao(self.ctx, "Tenho uma <agência>.")
        t = texto(ajustes.ROTAS["/ajustes"](self.req("GET", "/ajustes")))
        sec = t.split('id="inicio"', 1)[1]
        for trecho in ("Refazer o início", 'action="/inicio/refazer"', "Ver o roteiro de novo", 'href="/demandas?roteiro=1"',
                       "Tenho uma &lt;agência&gt;.", "Apagar o texto", "Salvar o texto"):
            self.assertIn(trecho, sec)

    def test_refazer_por_ajustes_reabre_para_quem_ja_usa(self):
        self.ff.frentes = [{"id": 1, "nome": "Agência", "tipo": "empresa", "status": "ativa", "apelidos": []}]
        self.assertFalse(inicio.deve_aparecer(self.ctx))
        inicio.gravar_descricao(self.ctx, "meu texto")
        self.assertEqual(self.destino(self.post("/inicio/refazer")), "/inicio/boas-vindas")
        self.assertTrue(inicio.deve_aparecer(self.ctx))
        self.assertEqual(inicio.descricao(self.ctx), "meu texto")           # refazer não apaga o que a pessoa escreveu
        self.assertEqual(self.destino(self.get("/inicio")), "/inicio/boas-vindas")
        self.assertEqual(len(self.ff.frentes), 1)                           # nem mexe no que já existe

    def test_editar_e_apagar_a_descricao(self):
        r = self.post("/inicio/descricao/salvar", {"acao": "salvar", "texto": "novo texto"})
        self.assertEqual(self.destino(r), "/ajustes?inicio=salvo#inicio")
        self.assertEqual(inicio.descricao(self.ctx), "novo texto")
        t = texto(ajustes.ROTAS["/ajustes"](self.req("GET", "/ajustes", query={"inicio": "salvo"})))
        self.assertIn("Texto salvo neste computador.", t)
        r = self.post("/inicio/descricao/salvar", {"acao": "apagar"})
        self.assertEqual(self.destino(r), "/ajustes?inicio=apagado#inicio")
        self.assertEqual(inicio.descricao(self.ctx), "")
        self.assertNotIn("Apagar o texto", texto(ajustes.ROTAS["/ajustes"](self.req("GET", "/ajustes"))))


class TestServidor(unittest.TestCase):
    """O desvio de /demandas para o assistente, pelo servidor de verdade (em porta livre do teste)."""
    setUp = test_server.TestServidor.setUp
    _http = test_server.TestServidor._http
    NAV = {"Sec-Fetch-Mode": "navigate", "Accept": "text/html"}

    def test_instalacao_nova_abre_no_assistente(self):
        self.ff.frentes = []
        st, h, _ = self._http("GET", "/demandas", cab=self.NAV)
        self.assertEqual((st, h.get("Location")), (303, "/inicio"))
        st, h, _ = self._http("GET", "/inicio", cab=self.NAV)
        self.assertEqual((st, h.get("Location")), (303, "/inicio/boas-vindas"))
        st, _, corpo = self._http("GET", "/inicio/boas-vindas", cab=self.NAV)
        self.assertEqual(st, 200)
        self.assertIn("Passo 1 de 8", corpo.decode())

    def test_busca_de_dados_da_pagina_nao_e_desviada(self):
        self.ff.frentes = []
        st, _, _ = self._http("GET", "/demandas", cab={"X-Vigia-Auto": "1"})
        self.assertEqual(st, 200)

    def test_quem_ja_tem_frente_vai_direto_para_demandas(self):
        st, _, _ = self._http("GET", "/demandas", cab=self.NAV)
        self.assertEqual(st, 200)
        st, _, _ = self._http("GET", "/inicio/boas-vindas", cab=self.NAV)
        self.assertEqual(st, 200)                                           # mas abre se a pessoa pedir

    def test_quem_ja_pareou_vai_direto_para_demandas(self):
        self.ff.frentes = []
        dados = Path(self.ctx.config["dados_dir"])
        (dados / "cursor.json").write_text(json.dumps({"ultimo_seq": 0, "pareado_em": int(time.time())}))
        st, _, _ = self._http("GET", "/demandas", cab=self.NAV)
        self.assertEqual(st, 200)

    def test_depois_de_sair_nao_desvia_mais_e_sem_sessao_pede_login(self):
        self.ff.frentes = []
        st, h, _ = self._http("POST", "/inicio/sair", corpo={})
        self.assertEqual((st, h.get("Location")), (303, "/demandas"))
        st, _, _ = self._http("GET", "/demandas", cab=self.NAV)
        self.assertEqual(st, 200)
        st, h, _ = self._http("GET", "/inicio/boas-vindas", cookie=False)
        self.assertEqual((st, h.get("Location")), (303, "/login"))

    def test_audio_bruto_chega_ao_passo(self):
        antes = inicio.transcrever_audio
        self.addCleanup(lambda: setattr(inicio, "transcrever_audio", antes))
        vistos = []
        inicio.transcrever_audio = lambda ctx, wav: vistos.append(wav) or "ditado"
        import http.client
        con = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=5)
        from app import server as srv_mod
        con.request("POST", "/inicio/audio", body=b"RIFFxxxxWAVEdados",
                    headers={"Host": f"127.0.0.1:{self.porta}", "Content-Type": "audio/wav",
                             "Cookie": f"{srv_mod.COOKIE}={srv_mod.valor_cookie(test_server.TOKEN)}"})
        r = con.getresponse()
        corpo = r.read()
        con.close()
        self.assertEqual((r.status, json.loads(corpo)), (200, {"texto": "ditado"}))
        self.assertEqual(vistos, [b"RIFFxxxxWAVEdados"])
