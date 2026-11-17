"""Testes da costura F3: app/server.py servindo as páginas reais com FakeEvolution/FakeFrentes.
Quem chama: unittest. Sobe o servidor numa porta efêmera de 127.0.0.1 e fala HTTP de verdade.
O que não faz: não testa regra de página (isso é test_paginas); só cookie, Host, despacho e serialização.
"""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from testes.fakes import FakeEvolution, FakeFrentes  # noqa: E402
from testes.util_contrato import RelogioFalso  # noqa: E402

from app import server as srv_mod  # noqa: E402
from app.banco import Banco  # noqa: E402
from app.conexao import Conexao  # noqa: E402
from app.cursor import Cursor  # noqa: E402
from app.evolution_client import EvolutionClient  # noqa: E402
from app.frentes_ponte import FrentesPonte  # noqa: E402

TOKEN = "tok-painel-teste"
TOKEN_ENVIO = "tok-envio-teste"


class TestServidor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        dados = Path(self.tmp.name) / "dados"
        dados.mkdir()
        self.fe = FakeEvolution().iniciar()
        self.addCleanup(self.fe.parar)
        self.ff = FakeFrentes().iniciar()
        self.addCleanup(self.ff.parar)
        evo = EvolutionClient(self.fe.url, self.fe.instancia, self.fe.apikey)
        banco = Banco(str(dados / "vigia.db"))
        banco.migrar()
        ponte = FrentesPonte(self.ff.url, self.ff.token)
        conexao = Conexao(evo, banco, relogio=RelogioFalso(), cursor=Cursor(dados / "cursor.json"),
                          estado_arq=dados / "conexao.json")
        self.ctx = SimpleNamespace(banco=banco, ponte=ponte, conexao=conexao, evo=evo,
                                   config={"dados_dir": str(dados)},
                                   tokens=SimpleNamespace(envio=TOKEN_ENVIO, painel=TOKEN), executor=None)
        self.srv = srv_mod.criar_servidor(self.ctx, TOKEN, porta=0)
        self.porta = self.srv.server_address[1]
        t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        t.start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def _http(self, metodo, caminho, corpo=None, cab=None, cookie=True, host="127.0.0.1"):
        con = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=5)
        cabs = {"Host": f"{host}:{self.porta}"}
        if cookie:
            cabs["Cookie"] = f"{srv_mod.COOKIE}={srv_mod.valor_cookie(TOKEN)}"
        dados = None
        if corpo is not None:
            dados = json.dumps(corpo).encode()
            cabs["Content-Type"] = "application/json"
        cabs.update(cab or {})
        con.request(metodo, caminho, body=dados, headers=cabs)
        r = con.getresponse()
        texto = r.read()
        con.close()
        return r.status, dict(r.getheaders()), texto

    # -- autenticação
    def test_sem_cookie_redireciona_para_login(self):
        st, h, _ = self._http("GET", "/conexao", cookie=False)
        self.assertEqual(st, 303)
        self.assertEqual(h.get("Location"), "/login")
        self.assertEqual(self.fe.qrs_gerados, 0)

    def test_saude_sem_cookie(self):
        st, h, corpo = self._http("GET", "/saude", cookie=False)
        self.assertEqual(st, 200)
        self.assertIn("json", h.get("Content-Type", ""))
        # segurança r01 (B6): sem login a rota só diz que o painel está de pé, sem detalhe da instalação
        self.assertEqual(json.loads(corpo), {"ok": True})

    def test_login_form_e_cookie(self):
        st, _, corpo = self._http("GET", "/login", cookie=False)
        self.assertEqual(st, 200)
        self.assertIn(b"VIGIA_TOKEN", corpo)
        con = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=5)
        con.request("POST", "/login", body="token=errado", headers={"Content-Type": "application/x-www-form-urlencoded"})
        r = con.getresponse(); r.read(); con.close()
        self.assertEqual(r.status, 401)
        con = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=5)
        con.request("POST", "/login", body=f"token={TOKEN}", headers={"Content-Type": "application/x-www-form-urlencoded"})
        r = con.getresponse(); r.read(); con.close()
        self.assertEqual(r.status, 303)
        sc = r.getheader("Set-Cookie", "")
        valor = sc.split(";")[0].split("=", 1)[1]
        self.assertEqual(valor, srv_mod.valor_cookie(TOKEN, 0, int(valor.split(".")[0])))
        self.assertEqual(srv_mod.estado_cookie(valor, TOKEN, 0), "ok")
        self.assertNotIn(TOKEN, sc)  # token cru nunca vai no cookie
        self.assertIn("HttpOnly", sc)
        self.assertIn("SameSite=Strict", sc)

    def test_cookie_errado_nao_entra(self):
        st, _, _ = self._http("GET", "/demandas", cab={"Cookie": f"{srv_mod.COOKIE}=outro"}, cookie=False)
        self.assertEqual(st, 303)
        st, _, _ = self._http("GET", "/demandas", cab={"Cookie": f"{srv_mod.COOKIE}={TOKEN}"}, cookie=False)
        self.assertEqual(st, 303)  # o token cru também não serve de cookie

    def test_tick_sem_cookie_401_json(self):
        st, h, _ = self._http("GET", "/conexao/tick", cookie=False)
        self.assertEqual(st, 401)
        self.assertIn("json", h.get("Content-Type", ""))

    # -- host / rotas
    def test_host_fora_de_loopback_421(self):
        st, _, _ = self._http("GET", "/saude", cookie=False, host="vigia.example.com")
        self.assertEqual(st, 421)

    def test_raiz_redireciona_e_404(self):
        st, h, _ = self._http("GET", "/")
        self.assertEqual((st, h.get("Location")), (303, "/demandas"))   # Papel: a casa é Demandas
        st, _, _ = self._http("GET", "/nao-existe")
        self.assertEqual(st, 404)

    def test_paginas_get_200_sem_gerar_qr(self):
        for caminho in ("/conexao", "/demandas", "/conversas", "/frentes", "/enviar"):
            st, h, corpo = self._http("GET", caminho)
            self.assertEqual(st, 200, caminho)
            self.assertIn("text/html", h.get("Content-Type", ""))
            self.assertIn(b"<nav>", corpo)
            self.assertNotIn(b"$conteudo", corpo)
        self.assertEqual(self.fe.qrs_gerados, 0)
        self.assertEqual(self._http("HEAD", "/conexao")[2], b"")

    def test_post_qr_gera_um_e_redireciona_ou_renderiza(self):
        st, _, _ = self._http("POST", "/conexao/qr", cab={"Origin": f"http://127.0.0.1:{self.porta}"})
        self.assertIn(st, (200, 303))
        self.assertEqual(self.fe.qrs_gerados, 1)

    def test_rota_padrao_com_id(self):
        item = self.ctx.ponte.capturar("teste", frente_id=1)["itens"][0]
        self.ctx.ponte.marcar_nao_confirmado(item["id"])
        st, h, _ = self._http("POST", f"/frentes/{item['id']}/confirmar",
                              cab={"Origin": f"http://127.0.0.1:{self.porta}", "Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual((st, h.get("Location")), (303, "/frentes"))
        self.assertEqual(self.ff.patches[-1]["corpo"], {"confirmado": 1})
        st, _, _ = self._http("POST", "/frentes/abc/confirmar", cab={"Origin": f"http://127.0.0.1:{self.porta}"})
        self.assertEqual(st, 404)

    def test_metodo_errado_405(self):
        st, _, _ = self._http("GET", "/conexao/qr")
        self.assertEqual(st, 405)

    # -- envio
    def test_enviar_sem_header_401_com_header_200(self):
        corpo = {"para": "5592900000000", "texto": "oi"}
        st, _, _ = self._http("POST", "/enviar", corpo)
        self.assertEqual(st, 401)
        self.assertEqual(len(self.fe.enviadas), 0)
        st, _, saida = self._http("POST", "/enviar", corpo, cab={"X-Vigia-Envio": TOKEN_ENVIO,
                                                               "Origin": f"http://127.0.0.1:{self.porta}"})
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(saida)["enviadas"], 1)

    def test_corpo_grande_400_e_json_invalido_tolerado(self):
        con = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=5)
        con.request("POST", "/enviar", body=b"x" * (srv_mod.LIMITE_CORPO + 1),
                    headers={"Content-Type": "application/json", "Cookie": f"{srv_mod.COOKIE}={srv_mod.valor_cookie(TOKEN)}"})
        r = con.getresponse(); r.read(); con.close()
        self.assertEqual(r.status, 400)

    def test_corpo_nao_lido_nao_vira_proxima_requisicao(self):
        """F4i I4: corpo > limite, GET com corpo e POST chunked fechavam sem consumir o corpo, que era lido como
        a requisição seguinte no mesmo socket (dessincronização de protocolo)."""
        import socket
        contrabando = b"GET /saude HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"

        def falar(cabecalho: bytes, corpo: bytes) -> list[bytes]:
            s = socket.create_connection(("127.0.0.1", self.porta), timeout=5)
            s.sendall(cabecalho + corpo)
            s.shutdown(socket.SHUT_WR)
            dados = b""
            while True:
                parte = s.recv(65536)
                if not parte:
                    break
                dados += parte
            s.close()
            return [l for l in dados.split(b"\r\n") if l.startswith(b"HTTP/1.")]

        grande = contrabando + b"x" * (srv_mod.LIMITE_CORPO + 1)
        cab = (f"POST /login HTTP/1.1\r\nHost: 127.0.0.1:{self.porta}\r\nContent-Type: application/x-www-form-urlencoded\r\n"
               f"Content-Length: {len(grande)}\r\n\r\n").encode()
        linhas = falar(cab, grande)
        self.assertEqual(len(linhas), 1, linhas)
        self.assertIn(b"400", linhas[0])

        cab = (f"GET /saude HTTP/1.1\r\nHost: 127.0.0.1:{self.porta}\r\nContent-Length: {len(contrabando)}\r\n\r\n").encode()
        linhas = falar(cab, contrabando)
        self.assertEqual(len(linhas), 1, linhas)

        cab = (f"POST /login HTTP/1.1\r\nHost: 127.0.0.1:{self.porta}\r\nTransfer-Encoding: chunked\r\n\r\n").encode()
        corpo = b"%x\r\n%s\r\n0\r\n\r\n" % (len(contrabando), contrabando)
        linhas = falar(cab, corpo)
        self.assertEqual(len(linhas), 1, linhas)
        self.assertIn(b"400", linhas[0])

    def test_cabecalhos_de_seguranca(self):
        _, h, _ = self._http("GET", "/saude", cookie=False)
        self.assertEqual(h.get("Cache-Control"), "no-store")
        self.assertEqual(h.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(h.get("X-Frame-Options"), "DENY")
        self.assertEqual(h.get("Server"), "Greg")
        self.assertEqual(h.get("Referrer-Policy"), "same-origin")  # no-referrer anula o Origin dos forms
        self.assertIn("form-action 'self'", h.get("Content-Security-Policy", ""))


    # ---- B1: /sair revoga ----
    def test_B1_sair_revoga_o_cookie_copiado_antes(self):
        copiado = {"Cookie": f"{srv_mod.COOKIE}={srv_mod.valor_cookie(TOKEN)}"}
        st, _, _ = self._http("GET", "/demandas", cab=copiado, cookie=False)
        self.assertEqual(st, 200)
        st, _, _ = self._http("POST", "/sair", cab={"Origin": f"http://127.0.0.1:{self.porta}"})
        self.assertEqual(st, 303)
        st, _, _ = self._http("GET", "/demandas", cab=copiado, cookie=False)
        self.assertEqual(st, 303)                                   # o cookie antigo não entra mais
        arq = Path(self.ctx.config["dados_dir"]) / "sessao.json"
        self.assertEqual(json.loads(arq.read_text())["geracao"], 1)
        self.assertEqual(arq.stat().st_mode & 0o777, 0o600)
        novo = {"Cookie": f"{srv_mod.COOKIE}={srv_mod.valor_cookie(TOKEN, 1)}"}   # novo login vale
        st, _, _ = self._http("GET", "/demandas", cab=novo, cookie=False)
        self.assertEqual(st, 200)

    # ---- B8 (segurança r01): o login vale 30 dias ----
    def test_B8_cookie_vence_em_30_dias(self):
        import time as _t
        agora = int(_t.time())
        velho = srv_mod.valor_cookie(TOKEN, 0, agora - srv_mod.VALIDADE_SESSAO - 60)
        quase = srv_mod.valor_cookie(TOKEN, 0, agora - srv_mod.VALIDADE_SESSAO + 600)
        self.assertEqual(srv_mod.VALIDADE_SESSAO, 30 * 24 * 3600)
        self.assertEqual(srv_mod.estado_cookie(velho, TOKEN, 0), "vencido")
        self.assertEqual(srv_mod.estado_cookie(quase, TOKEN, 0), "ok")
        self.assertEqual(srv_mod.estado_cookie(srv_mod.valor_cookie(TOKEN, 0, agora + 3600), TOKEN, 0), "invalido")
        self.assertEqual(srv_mod.estado_cookie(f"{agora}.{'0' * 64}", TOKEN, 0), "invalido")
        self.assertEqual(srv_mod.estado_cookie(velho.split(".")[1], TOKEN, 0), "invalido")   # formato antigo, sem data
        st, h, _ = self._http("GET", "/demandas", cab={"Cookie": f"{srv_mod.COOKIE}={velho}"}, cookie=False)
        self.assertEqual((st, h.get("Location")), (303, "/login?venceu=1"))
        st, h, _ = self._http("GET", "/demandas", cab={"Cookie": f"{srv_mod.COOKIE}=lixo"}, cookie=False)
        self.assertEqual((st, h.get("Location")), (303, "/login"))
        st, _, _ = self._http("GET", "/demandas", cab={"Cookie": f"{srv_mod.COOKIE}={quase}"}, cookie=False)
        self.assertEqual(st, 200)

    # ---- pedido 63 (T1): pedido da página sem sessão recebe 401 marcado, nunca o 303 que o fetch leria como sucesso ----
    def test_T1_pedido_da_pagina_sem_sessao_recebe_401_marcado(self):
        origem = {"Origin": f"http://127.0.0.1:{self.porta}"}
        for cab in ({"X-Vigia-Auto": "1"}, {"Sec-Fetch-Mode": "cors"}, {"Sec-Fetch-Mode": "no-cors"},
                    {"Accept": "application/json"}):
            st, h, corpo = self._http("POST", "/demandas/1/recusar", cookie=False, cab={**origem, **cab})
            self.assertEqual((st, h.get("X-Vigia-Sessao"), h.get("Location")), (401, "caiu", None), cab)
            d = json.loads(corpo)
            self.assertEqual(d["login"], "/login")
            self.assertIn("sessão terminou", d["mensagem"])
        import time as _t
        velho = srv_mod.valor_cookie(TOKEN, 0, int(_t.time()) - srv_mod.VALIDADE_SESSAO - 60)
        st, h, corpo = self._http("GET", "/conexao/lateral", cookie=False,
                                  cab={"Cookie": f"{srv_mod.COOKIE}={velho}", "X-Vigia-Auto": "1"})
        self.assertEqual((st, json.loads(corpo)["login"]), (401, "/login?venceu=1"))
        # a navegação da janela continua indo para o login
        st, h, _ = self._http("GET", "/demandas", cookie=False, cab={"Sec-Fetch-Mode": "navigate"})
        self.assertEqual((st, h.get("Location")), (303, "/login"))

    def test_T1_base_marca_a_sessao_caida(self):
        base = (Path(srv_mod.__file__).parent / "templates" / "_base.html").read_text(encoding="utf-8")
        self.assertIn("X-Vigia-Sessao", base)
        self.assertIn("data-sessao-caiu", base)
        self.assertNotIn('class="banner-sessao"', base)   # pedido 83: sem faixa, vai direto ao login

    def test_B1_sair_sem_sessao_nao_gira_a_geracao(self):
        st, _, _ = self._http("POST", "/sair", cookie=False, cab={"Origin": f"http://127.0.0.1:{self.porta}"})
        self.assertEqual(st, 303)
        self.assertFalse((Path(self.ctx.config["dados_dir"]) / "sessao.json").exists())

    # ---- B2: --porta 0 ----
    def test_B2_porta_zero_e_respeitada(self):
        class Srv:
            def serve_forever(self): raise KeyboardInterrupt
            def server_close(self): pass
        vistas = []
        with mock.patch.object(srv_mod, "montar_ctx", return_value=(SimpleNamespace(tokens=SimpleNamespace(envio="")), "t")), \
                mock.patch.object(srv_mod, "criar_servidor", side_effect=lambda c, t, porta: vistas.append(porta) or Srv()), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VIGIA_PORT", None)
            self.assertEqual(srv_mod.main(["--raiz", self.tmp.name, "--porta", "0"]), 0)
        self.assertEqual(vistas, [0])

    # ---- B4: VIGIA_DADOS ----
    def test_B4_montar_ctx_respeita_vigia_dados(self):
        raiz = Path(self.tmp.name) / "raiz"
        raiz.mkdir()
        (raiz / ".env").write_text("EVOLUTION_URL=http://127.0.0.1:1\nEVOLUTION_INSTANCE=x\nEVOLUTION_INSTANCE_APIKEY=x\n"
                                   "FRENTES_URL=http://127.0.0.1:1\nFRENTES_TOKEN=x\nVIGIA_TOKEN=x\n")
        outro = Path(self.tmp.name) / "outro-dados"
        with mock.patch.dict(os.environ, {"VIGIA_DADOS": str(outro)}):
            ctx, _ = srv_mod.montar_ctx(raiz)
        self.assertEqual(ctx.config["dados_dir"], str(outro))
        self.assertTrue((outro / "vigia.db").exists())
        self.assertFalse((raiz / "dados" / "vigia.db").exists())

    # ---- B8: /saude não repara ----
    def test_B8_saude_sem_login_nao_renomeia_cursor_corrompido(self):
        arq = Path(self.ctx.config["dados_dir"]) / "cursor.json"
        arq.write_text("{corrompido", encoding="utf-8")
        st, _, corpo = self._http("GET", "/saude", cookie=False)
        self.assertEqual(st, 200)
        self.assertEqual(arq.read_text(encoding="utf-8"), "{corrompido")
        self.assertEqual(list(arq.parent.glob("cursor.json.corrompido-*")), [])
        # segurança r01 (B6): sem login a rota nem lê o cursor; só responde que está de pé
        self.assertEqual(json.loads(corpo), {"ok": True})


class TestUnidades(unittest.TestCase):
    def test_descobrir_rotas_padroes(self):
        exatas, padroes = srv_mod.descobrir_rotas()
        for c in ("/conexao", "/conexao/qr", "/demandas", "/saude", "/enviar"):
            self.assertIn(c, exatas)
        self.assertTrue(any(rx.match("/demandas/12/confirmar") for rx, _ in padroes))
        self.assertFalse(any(rx.match("/demandas/x/confirmar") for rx, _ in padroes))

    def test_recusa_bind_fora_de_loopback_e_sem_token(self):
        ctx = SimpleNamespace()
        with self.assertRaises(ValueError):
            srv_mod.criar_servidor(ctx, "t", porta=0, host="0.0.0.0")
        with self.assertRaises(ValueError):
            srv_mod.criar_servidor(ctx, "", porta=0)

    def test_token_envio_exige_0600(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "env"
            p.write_text("VIGIA_TOKEN_ENVIO=abc\n")
            p.chmod(0o644)
            self.assertEqual(srv_mod._ler_token_envio(p), "")
            p.chmod(0o600)
            self.assertEqual(srv_mod._ler_token_envio(p), "abc")
            self.assertEqual(srv_mod._ler_token_envio(Path(d) / "nao-existe"), "")

    def test_roda_como_script_sem_sombrear_http_da_stdlib(self):
        """F4 E11 (alto): `python3 app/server.py` punha app/ em sys.path[0] e app/http.py escondia http.server."""
        import subprocess
        raiz = Path(__file__).resolve().parents[1]
        r = subprocess.run([sys.executable, "-B", str(raiz / "app" / "server.py"), "--help"],
                           capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        self.assertIn("--porta", r.stdout)

    def test_montar_ctx_sem_env_falha_legivel(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError) as cm:
                srv_mod.montar_ctx(Path(d))
            self.assertIn("VIGIA_TOKEN", str(cm.exception))


class TestHandleError(unittest.TestCase):
    """O WKWebView derruba conexões keep-alive ociosas com RST; o socketserver imprimia 16 linhas de
    traceback por conexão (07/11). O servidor engole SÓ esse caso; qualquer outra exceção sai inteira."""

    def _handle_error_com(self, exc):
        import contextlib
        import io
        srv = srv_mod.criar_servidor(SimpleNamespace(), "tok", porta=0)
        self.addCleanup(srv.server_close)
        err = io.StringIO()
        try:
            raise exc
        except Exception:
            with contextlib.redirect_stderr(err), self.assertLogs("vigia.server", level="DEBUG") as logs:
                srv_mod.log.debug("ancora")  # garante que assertLogs não falha quando nada é logado
                srv.handle_error(None, ("127.0.0.1", 64824))
        return err.getvalue(), [m for m in logs.output if "ancora" not in m]

    def test_criar_servidor_devolve_servidor_vigia_com_threads_daemon(self):
        srv = srv_mod.criar_servidor(SimpleNamespace(), "tok", porta=0)
        self.addCleanup(srv.server_close)
        self.assertIsInstance(srv, srv_mod.ServidorVigia)
        self.assertTrue(srv.daemon_threads)

    def test_reset_pelo_cliente_nao_imprime_traceback(self):
        for exc in (ConnectionResetError(54, "Connection reset by peer"), BrokenPipeError(32, "Broken pipe")):
            with self.subTest(exc=exc.__class__.__name__):
                stderr, logs = self._handle_error_com(exc)
                self.assertEqual(stderr, "")
                self.assertEqual(len(logs), 1)
                self.assertIn("DEBUG", logs[0])
                self.assertIn(exc.__class__.__name__, logs[0])

    def test_outra_excecao_continua_saindo_inteira(self):
        stderr, logs = self._handle_error_com(RuntimeError("quebrou de verdade"))
        self.assertIn("Exception occurred during processing of request", stderr)
        self.assertIn("RuntimeError: quebrou de verdade", stderr)
        self.assertEqual(logs, [])


if __name__ == "__main__":
    unittest.main()
