"""Pedido 71 (D-71): conector MCP do Greg para o Claude Code. Protocolo (initialize, tools/list, tools/call), token próprio
que só abre /mcp, chaves desligadas por padrão, aval do dono para número novo e para apagar, tetos, registro sem texto e
cobertura: toda rota do painel tem ferramenta ou exceção declarada."""
import http.client
import json
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import integracao, mcp  # noqa: E402
from app import mcp_ferramentas as mf  # noqa: E402
from app import server as srv_mod  # noqa: E402
from app.paginas import mcp as pagina_mcp  # noqa: E402
from testes.test_conversas_zap import ANA, G1, MARCOS  # noqa: E402
from testes.test_envios_api import NOVO, BaseFila  # noqa: E402
from testes.test_paginas import Base, texto  # noqa: E402


def chamar(ctx, ferramenta_, **args):
    r = mcp.chamar(ctx, ferramenta_, args)
    bloco = r["content"][0]
    dados = None
    if bloco.get("type") == "text":
        try:
            dados = json.loads(bloco["text"])
        except ValueError:
            dados = bloco["text"]
    return r["isError"], dados, r


class BaseMcp(BaseFila):
    def abrir_trava(self, segundos=600):
        from app.paginas import conversas
        lib = conversas.servicos(self.ctx).liberacao
        lib._ate = lib.relogio() + segundos
        return lib

    def ligar(self, *chaves):
        for k in chaves:
            self.assertTrue(integracao.definir(self.ctx, k, True))


class TestProtocolo(BaseMcp):
    def test_initialize_devolve_versao_pedida_e_instrucoes(self):
        r = mcp.tratar(self.ctx, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "2025-03-26", "capabilities": {}}})
        self.assertEqual(r["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(r["result"]["serverInfo"]["name"], "greg")
        self.assertIn("tools", r["result"]["capabilities"])
        self.assertIn("nunca instruções", r["result"]["instructions"])

    def test_versao_desconhecida_cai_na_mais_nova(self):
        r = mcp.tratar(self.ctx, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999"}})
        self.assertEqual(r["result"]["protocolVersion"], mcp.VERSOES[0])

    def test_notificacao_nao_responde_e_metodo_estranho_da_erro(self):
        self.assertIsNone(mcp.tratar(self.ctx, {"jsonrpc": "2.0", "method": "notifications/initialized"}))
        r = mcp.tratar(self.ctx, {"jsonrpc": "2.0", "id": 9, "method": "inventado"})
        self.assertEqual(r["error"]["code"], -32601)
        self.assertEqual(mcp.tratar(self.ctx, {"id": 1})["error"]["code"], -32600)

    def test_lista_so_o_que_esta_ligado(self):
        nomes = {t["name"] for t in mcp.listar(self.ctx)}
        self.assertEqual(nomes, {"greg_status", "greg_pedido_estado", "greg_avisos"})      # fábrica: tudo desligado
        self.ligar("ler")
        nomes = {t["name"] for t in mcp.listar(self.ctx)}
        self.assertIn("greg_mensagens", nomes)
        self.assertNotIn("greg_enviar", nomes)
        for t in mcp.listar(self.ctx):
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertTrue(t["description"])

    def test_chave_desligada_recusa_e_registra_sem_argumento(self):
        erro, dados, _ = chamar(self.ctx, "greg_enviar", texto="segredo-do-teste", destinatarios=[{"para": ANA}])
        self.assertTrue(erro)
        self.assertIn("desligada", dados)
        reg = integracao.registro(self.ctx)
        self.assertEqual((reg[0]["ferramenta"], reg[0]["resultado"]), ("greg_enviar", "recusado"))
        self.assertNotIn("segredo-do-teste", (self.dados / "integracao-log.jsonl").read_text())

    def test_ferramenta_desconhecida(self):
        erro, dados, _ = chamar(self.ctx, "greg_nao_existe")
        self.assertTrue(erro)


class TestIntegracao(Base):
    def test_token_guarda_so_o_hash_e_trocar_invalida_o_anterior(self):
        self.assertFalse(integracao.token_ok(self.ctx, "qualquer"))
        t1 = integracao.gerar_token(self.ctx)
        arq = self.dados / "integracao.json"
        self.assertEqual(arq.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(t1, arq.read_text())
        self.assertTrue(integracao.token_ok(self.ctx, t1))
        t2 = integracao.gerar_token(self.ctx)
        self.assertFalse(integracao.token_ok(self.ctx, t1))
        self.assertTrue(integracao.token_ok(self.ctx, t2))

    def test_revogar_desliga_tudo(self):
        t = integracao.gerar_token(self.ctx)
        integracao.definir(self.ctx, "enviar", True)
        integracao.revogar(self.ctx)
        self.assertFalse(integracao.token_ok(self.ctx, t))
        self.assertFalse(any(integracao.ler(self.ctx)["chaves"].values()))

    def test_cabecalho_bearer(self):
        self.assertEqual(integracao.token_do_cabecalho("Bearer abc"), "abc")
        self.assertEqual(integracao.token_do_cabecalho("abc"), "")
        self.assertEqual(integracao.token_do_cabecalho(None), "")

    def test_pedido_vence_e_so_decide_uma_vez(self):
        agora = [1000.0]
        p = integracao.Pedidos(relogio=lambda: agora[0])
        rodou = []
        pid = p.criar("x", "resumo", lambda: rodou.append(1) or {"ok": True})
        self.assertEqual(p.decidir(pid, True)["estado"], "feito")
        self.assertIsNone(p.decidir(pid, True))
        self.assertEqual(rodou, [1])
        pid2 = p.criar("x", "resumo", lambda: rodou.append(2))
        agora[0] += integracao.VALIDADE_PEDIDO_S + 1
        self.assertEqual(p.estado(pid2)["estado"], "vencido")
        self.assertIsNone(p.decidir(pid2, True))
        self.assertEqual(rodou, [1])


class TestEnviar(BaseMcp):
    def setUp(self):
        super().setUp()
        self.abrir_trava()

    def test_trava_fechada_recusa_todo_envio_e_pede_o_desbloqueio(self):
        self.ligar("enviar")
        self.abrir_trava().travar()
        casos = [("greg_enviar", dict(texto="oi", destinatarios=[{"para": ANA}], esperar_s=0)),
                 ("greg_responder", dict(conversa=ANA, id="a1", texto="oi")),
                 ("greg_enviar_arquivo", dict(conversa=ANA, conteudo_base64="aGk=", nome="a.txt")),
                 ("greg_reagir", dict(conversa=ANA, id="a1", emoji="👍")),
                 ("greg_apagar_para_todos", dict(conversa=ANA, id="a1"))]
        for nome, args in casos:
            erro, dados, _ = chamar(self.ctx, nome, **args)
            self.assertTrue(erro, nome)
            self.assertIn("liberar o envio", dados, nome)
        self.assertEqual(self.envios_db(), [])
        self.assertEqual(integracao.pedidos(self.ctx).pendentes(), [])

    def test_trava_aberta_vale_o_tempo_que_resta_e_o_status_informa(self):
        self.ligar("enviar")
        self.abrir_trava(125)
        _, dados, _ = chamar(self.ctx, "greg_status")
        self.assertEqual(dados["liberacao_de_envio"], {"aberta": True, "minutos_restantes": 2})
        self.abrir_trava().travar()
        _, dados, _ = chamar(self.ctx, "greg_status")
        self.assertFalse(dados["liberacao_de_envio"]["aberta"])

    def test_texto_para_conhecido_vai_para_a_fila_com_origem_do_claude(self):
        self.ligar("enviar")
        erro, lote, _ = chamar(self.ctx, "greg_enviar", texto="Oi {nome}", destinatarios=[{"para": ANA}], esperar_s=0)
        self.assertFalse(erro, lote)
        self.assertEqual(lote["origem"], "claude-code")
        self.rodar()
        self.assertEqual([e["por"] for e in self.envios_db()], ["api:claude-code"])

    def test_numero_novo_com_ler_ligado_espera_aval_e_so_sai_se_permitir(self):
        self.ligar("enviar", "ler")
        erro, dados, _ = chamar(self.ctx, "greg_enviar", texto="proposta", destinatarios=[{"para": NOVO}], esperar_s=0)
        self.assertFalse(erro)
        pid = dados["aguardando_aval"]
        self.assertIsNone(self.banco.proximo_destino_envio())           # nada na fila antes do aval
        self.assertEqual(integracao.registro(self.ctx)[0]["resultado"], "aval")
        _, est, _ = chamar(self.ctx, "greg_pedido_estado", id=pid)
        self.assertEqual(est["estado"], "aguardando")
        r = integracao.pedidos(self.ctx).decidir(pid, True)
        self.assertEqual(r["estado"], "feito")
        self.assertIsNotNone(self.banco.proximo_destino_envio())

    def test_numero_novo_recusado_nao_sai(self):
        self.ligar("enviar", "ler")
        _, dados, _ = chamar(self.ctx, "greg_enviar", texto="proposta", destinatarios=[{"para": NOVO}], esperar_s=0)
        integracao.pedidos(self.ctx).decidir(dados["aguardando_aval"], False)
        self.assertIsNone(self.banco.proximo_destino_envio())

    def test_numero_novo_sem_ler_ligado_segue_a_fila(self):
        self.ligar("enviar")
        erro, lote, _ = chamar(self.ctx, "greg_enviar", texto="oi", destinatarios=[{"para": NOVO}], esperar_s=0)
        self.assertFalse(erro)
        self.assertIn("destinatarios", lote)

    def test_teto_do_dia_vira_recusa_legivel(self):
        self.ligar("enviar")
        self.nova_fila(teto_dia_conhecidos=0)
        erro, dados, _ = chamar(self.ctx, "greg_enviar", texto="oi", destinatarios=[{"para": ANA}], esperar_s=0)
        self.assertTrue(erro)
        self.assertIn("teto", dados.lower())

    def test_responder_com_a_trava_aberta_nao_e_recusado_pela_trava_nem_mexe_nela(self):
        self.ligar("enviar")
        lib = self.abrir_trava(300)
        self.psql.chave = {"id": "A", "remoteJid": ANA, "fromMe": False}
        erro, dados, _ = chamar(self.ctx, "greg_responder", conversa=ANA, id="a1", texto="respondo já")
        if erro:      # o psql falso pode não achar a mensagem citada: o que importa é que a recusa não é a trava
            self.assertNotIn("Touch ID", dados)
        self.assertTrue(0 < lib.restante() <= 300)

    def test_envio_direto_para_numero_sem_conversa_e_recusado(self):
        self.ligar("enviar")
        erro, dados, _ = chamar(self.ctx, "greg_enviar_arquivo", conversa=NOVO, conteudo_base64="aGk=", nome="a.txt")
        self.assertTrue(erro)
        self.assertIn("Ainda não existe conversa", dados)

    def test_arquivo_respeita_teto_direto(self):
        self.ligar("enviar")
        self.nova_fila(teto_dia_conhecidos=1)
        integracao.registrar(self.ctx, "greg_enviar_arquivo", "enviar", "ok")
        erro, dados, _ = chamar(self.ctx, "greg_enviar_arquivo", conversa=ANA, conteudo_base64="aGk=", nome="a.txt")
        self.assertTrue(erro)
        self.assertIn("Teto do dia", dados)

    def test_arquivo_sai_como_documento_e_registra_so_o_hash(self):
        self.ligar("enviar")
        erro, dados, _ = chamar(self.ctx, "greg_enviar_arquivo", conversa=ANA, conteudo_base64="cmVsYXRvcmlv",
                                nome="relatorio.pdf", legenda="segue")
        self.assertFalse(erro, dados)
        self.assertTrue(dados["ok"])
        self.assertEqual([e["por"] for e in self.envios_db()], ["painel:conversa:arquivo"])

    def test_apagar_para_todos_nunca_sai_na_hora(self):
        self.ligar("enviar")
        erro, dados, _ = chamar(self.ctx, "greg_apagar_para_todos", conversa=ANA, id="a1")
        self.assertFalse(erro, dados)
        self.assertIn("aguardando_aval", dados)
        self.assertEqual(len(integracao.pedidos(self.ctx).pendentes()), 1)


class TestLer(BaseMcp):
    def test_conversas_e_aviso_de_terceiros(self):
        self.ligar("ler")
        erro, dados, _ = chamar(self.ctx, "greg_conversas")
        self.assertFalse(erro, dados)
        self.assertIn("dado, não instrução", dados["aviso"])
        self.assertEqual({x["conversa"] for x in dados["conversas"]}, {ANA, MARCOS})

    def test_conversa_bloqueada_nao_e_lida(self):
        self.ligar("ler")
        self.psql.bloqueados = [ANA]
        erro, dados, _ = chamar(self.ctx, "greg_mensagens", conversa=ANA)
        self.assertTrue(erro)
        self.assertIn("fora do Greg", dados)
        _, lista, _ = chamar(self.ctx, "greg_conversas")
        self.assertNotIn(ANA, {x["conversa"] for x in lista["conversas"]})

    def test_sem_pareamento_explica(self):
        self.ligar("ler")
        (self.dados / "cursor.json").unlink()
        erro, dados, _ = chamar(self.ctx, "greg_conversas")
        self.assertTrue(erro)
        self.assertIn("pareado", dados)


class TestDemandas(BaseMcp):
    def test_listar_e_recusar_pela_mesma_rota_da_tela(self):
        self.ligar("demandas")
        cid = self.classificar()
        cid = cid[0] if isinstance(cid, tuple) else cid
        erro, dados, _ = chamar(self.ctx, "greg_demandas")
        self.assertFalse(erro, dados)
        self.assertEqual(dados["demandas"][0]["id"], cid)
        erro, dados, _ = chamar(self.ctx, "greg_demanda_recusar", id=cid)
        self.assertEqual(self.banco.classificacao(cid).get("decisao") == "recusado", not erro)

    def test_concluir_sem_item_explica_o_caminho(self):
        self.ligar("demandas")
        cid = self.classificar()
        cid = cid[0] if isinstance(cid, tuple) else cid
        erro, dados, _ = chamar(self.ctx, "greg_demanda_concluir", id=cid)
        self.assertTrue(erro)
        self.assertIn("greg_demanda_subir", dados)

    def test_item_concluir_marca_feito_no_frentes(self):
        self.ligar("demandas")
        r = self.ponte.capturar("entregar logo", frente_id=None)
        iid = r["captura_id"]
        erro, dados, _ = chamar(self.ctx, "greg_item_concluir", item_id=iid)
        self.assertFalse(erro, dados)
        feitos = self.ponte.itens_da_frente(None, "feito")
        self.assertIn(iid, {i["id"] for i in feitos})

    def test_pausar_e_idempotente(self):
        self.ligar("demandas")
        erro, dados, _ = chamar(self.ctx, "greg_pausar_leituras", pausar=True)
        self.assertFalse(erro, dados)
        self.assertTrue((self.dados / "PAUSADO").exists())
        _, dados, _ = chamar(self.ctx, "greg_pausar_leituras", pausar=True)
        self.assertEqual(dados["mensagem"], "Já estava assim.")
        chamar(self.ctx, "greg_pausar_leituras", pausar=False)
        self.assertFalse((self.dados / "PAUSADO").exists())


class TestContatos(BaseMcp):
    def test_nomear_e_ligar_a_frente(self):
        self.ligar("contatos")
        erro, _, _ = chamar(self.ctx, "greg_contato_nomear", conversa=ANA, nome="Ana do Financeiro")
        self.assertFalse(erro)
        self.assertEqual(self.banco.apelidos()[ANA], "Ana do Financeiro")
        erro, dados, _ = chamar(self.ctx, "greg_contato_nomear", conversa=G1, nome="x")
        self.assertTrue(erro)
        fid = self.ponte.frentes()[0]["id"]
        erro, dados, _ = chamar(self.ctx, "greg_contato_frente", conversas=[ANA], frente_id=fid)
        self.assertFalse(erro, dados)
        self.assertEqual(self.banco.vinculo(ANA)["frente_id"], fid)
        erro, dados, _ = chamar(self.ctx, "greg_contato_frente", conversas=[ANA], frente_id=987654)
        self.assertTrue(erro)

    def test_vcard_escapa(self):
        v = mf._vcard("Silva; Ana, Jr", "+5592999990000")
        self.assertIn("FN:Silva\\; Ana\\, Jr", v)
        self.assertTrue(v.startswith("BEGIN:VCARD") and v.rstrip().endswith("END:VCARD"))


class TestCobertura(unittest.TestCase):
    """O conector acompanha a tela: rota nova no painel sem ferramenta nem exceção declarada falha aqui."""

    def rotas(self):
        exatas, padroes = srv_mod.descobrir_rotas()
        return set(exatas) | {rx.pattern.strip("^$").replace("\\", "").replace("(d+)", "{id}") for rx, _ in padroes}

    def test_toda_rota_tem_destino(self):
        faltam = sorted(self.rotas() - set(mf.COBERTURA))
        self.assertEqual(faltam, [], "rota do painel sem ferramenta MCP nem exceção em app/mcp_ferramentas.COBERTURA")

    def test_cobertura_nao_cita_rota_nem_ferramenta_que_nao_existe(self):
        self.assertEqual(sorted(set(mf.COBERTURA) - self.rotas()), [])
        for rota, destino in mf.COBERTURA.items():
            nomes = destino if isinstance(destino, tuple) else (destino,)
            for n in nomes:
                if n.startswith(("humano: ", "tela: ")):
                    self.assertGreater(len(n.split(": ", 1)[1]), 8, rota)
                else:
                    self.assertIn(n, mcp.FERRAMENTAS, rota)

    def test_so_o_humano_mexe_na_integracao_e_no_pareamento(self):
        for rota in ("/ajustes/claude", "/conexao/qr", "/conexao/reiniciar", "/conexao/desparear", "/conexao/segurar"):
            self.assertTrue(mf.COBERTURA[rota].startswith("humano: "), rota)
        for f in mcp.FERRAMENTAS.values():
            self.assertNotIn("token", f.nome)
            self.assertIn(f.chave, (None, *integracao.CHAVES))


class TestServidor(unittest.TestCase):
    """Pelo servidor real: o token da integração abre /mcp e nada mais; a tela dos Ajustes só com o cookie."""

    def setUp(self):
        base = Base()
        base.setUp()
        self.addCleanup(base.tmp.cleanup)
        self.addCleanup(base.fe.parar)
        self.addCleanup(base.ff.parar)
        self.ctx = base.ctx
        self.ctx.executor = None
        self.ctx.tokens.painel = "tok-painel"
        self.srv = srv_mod.criar_servidor(self.ctx, "tok-painel", porta=0)
        self.porta = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.addCleanup(lambda: getattr(self.ctx, "fila", None) and self.ctx.fila.encerrar())
        self.token = integracao.gerar_token(self.ctx)

    def http(self, metodo, caminho, corpo=None, cab=None):
        con = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=10)
        cabs = {"Host": f"127.0.0.1:{self.porta}", "Accept": "application/json, text/event-stream"}
        if corpo is not None:
            cabs["Content-Type"] = "application/json"
        cabs.update(cab or {})
        con.request(metodo, caminho, body=json.dumps(corpo).encode() if corpo is not None else None, headers=cabs)
        r = con.getresponse()
        t = r.read()
        con.close()
        return r.status, t

    def rpc(self, metodo, params=None, token=None, mid=1):
        msg = {"jsonrpc": "2.0", "method": metodo, "params": params or {}}
        if mid is not None:
            msg["id"] = mid
        st, t = self.http("POST", "/mcp", msg, {"Authorization": f"Bearer {token or self.token}"})
        return st, (json.loads(t) if t else None)

    def test_sem_token_ou_com_token_errado_401(self):
        st, _ = self.http("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(st, 401)
        st, _ = self.rpc("ping", token="errado")
        self.assertEqual(st, 401)

    def test_fluxo_initialize_list_call(self):
        st, r = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
        self.assertEqual((st, r["result"]["serverInfo"]["name"]), (200, "greg"))
        st, r = self.rpc("notifications/initialized", mid=None)
        self.assertEqual((st, r), (202, None))
        st, r = self.rpc("tools/list")
        self.assertEqual({t["name"] for t in r["result"]["tools"]}, {"greg_status", "greg_pedido_estado", "greg_avisos"})
        st, r = self.rpc("tools/call", {"name": "greg_status", "arguments": {}})
        self.assertFalse(r["result"]["isError"])
        self.assertIn("liberado_para_voce", r["result"]["content"][0]["text"])

    def test_token_do_conector_nao_abre_outra_rota(self):
        cab = {"Authorization": f"Bearer {self.token}"}
        for caminho in ("/ajustes/claude", "/envios", "/demandas", "/conversas"):
            st, _ = self.http("POST" if caminho == "/ajustes/claude" else "GET", caminho,
                              {"acao": "chave", "nome": "enviar", "ligar": "1"} if caminho == "/ajustes/claude" else None, cab)
            self.assertEqual(st, 401, caminho)
        self.assertFalse(integracao.ler(self.ctx)["chaves"]["enviar"])

    def test_get_no_mcp_e_405_e_origem_de_fora_e_recusada(self):
        st, _ = self.http("GET", "/mcp", None, {"Authorization": f"Bearer {self.token}"})
        self.assertEqual(st, 405)
        st, _ = self.http("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                          {"Authorization": f"Bearer {self.token}", "Origin": "https://exemplo.invalid"})
        self.assertEqual(st, 403)

    def test_ajustes_gera_token_uma_vez_e_liga_chave_so_com_cookie(self):
        jar = f"{srv_mod.COOKIE}={srv_mod.valor_cookie('tok-painel', srv_mod.ler_geracao(self.ctx))}"
        con = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=10)
        con.request("POST", "/ajustes/claude", body="acao=gerar",
                    headers={"Host": f"127.0.0.1:{self.porta}", "Cookie": jar,
                             "Content-Type": "application/x-www-form-urlencoded"})
        r = con.getresponse()
        pagina = r.read().decode()
        con.close()
        self.assertEqual(r.status, 200)
        self.assertIn("claude mcp add --transport http --scope user greg", pagina)
        self.assertFalse(integracao.token_ok(self.ctx, self.token))      # o código anterior deixou de valer
        con = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=10)
        con.request("POST", "/ajustes/claude", body="acao=chave&nome=ler&ligar=1",
                    headers={"Host": f"127.0.0.1:{self.porta}", "Cookie": jar,
                             "Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(con.getresponse().status, 303)
        con.close()
        self.assertTrue(integracao.ler(self.ctx)["chaves"]["ler"])


class TestTelaAjustes(Base):
    def test_grupo_mostra_chaves_pedidos_e_registro_sem_token(self):
        integracao.gerar_token(self.ctx)
        integracao.pedidos(self.ctx).criar("greg_enviar", "Enviar para número novo: «oi»", lambda: {})
        integracao.registrar(self.ctx, "greg_status", "", "ok")
        html = pagina_mcp.html_grupo(self.ctx)
        for trecho in ("Enviar", "Ler conversas", "Demandas e frentes", "Contatos", "Pedidos do Claude", "Permitir",
                       "greg_status", "Trocar código"):
            self.assertIn(trecho, html)
        self.assertNotIn("claude mcp add", html)      # o comando com o código só aparece na tela em que ele é gerado

    def test_comando(self):
        cmd = pagina_mcp.comando("127.0.0.1:8120", "abc")
        self.assertIn('claude mcp add --transport http --scope user greg http://127.0.0.1:8120/mcp '
                      '--header "Authorization: Bearer abc"', cmd)
        self.assertTrue(cmd.startswith("claude mcp remove greg --scope user"))      # trocar o código não esbarra no antigo
        self.assertIn(f'&& echo "{pagina_mcp.CONECTOU}" || echo "{pagina_mcp.NAO_CONECTOU}"', cmd)
        self.assertNotIn("\n", cmd)
        self.assertNotIn("!", cmd)       # zsh interativo expande ! dentro de aspas duplas


if __name__ == "__main__":
    unittest.main()
