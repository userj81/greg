"""Pedido 83 (D-86): sessão vencida não deixa a tela aberta. Recarga, navegação ou chamada sem sessão vão ao login, e o
login devolve a pessoa à página em que estava.
Quem chama: unittest. Sobe o servidor de verdade numa porta efêmera de 127.0.0.1 (como testes/test_server.py).
O que não faz: não roda JavaScript; o cliente (_base.html) é conferido pelo texto.
"""
import http.client
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from testes.fakes import FakeEvolution, FakeFrentes  # noqa: E402
from testes.util_contrato import RelogioFalso  # noqa: E402

from app import server as srv_mod  # noqa: E402
from app.banco import Banco  # noqa: E402
from app.conexao import Conexao  # noqa: E402
from app.cursor import Cursor  # noqa: E402
from app.evolution_client import EvolutionClient  # noqa: E402
from app.frentes_ponte import FrentesPonte  # noqa: E402

RAIZ = Path(__file__).resolve().parents[1]
TOKEN = "tok-painel-teste"
NAVEGA = {"Sec-Fetch-Mode": "navigate", "Accept": "text/html"}


class TestSessaoVaiAoLogin(unittest.TestCase):
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
        conexao = Conexao(evo, banco, relogio=RelogioFalso(), cursor=Cursor(dados / "cursor.json"),
                          estado_arq=dados / "conexao.json")
        self.ctx = SimpleNamespace(banco=banco, ponte=FrentesPonte(self.ff.url, self.ff.token), conexao=conexao, evo=evo,
                                   config={"dados_dir": str(dados)},
                                   tokens=SimpleNamespace(envio="tok-envio-teste", painel=TOKEN), executor=None)
        self.srv = srv_mod.criar_servidor(self.ctx, TOKEN, porta=0)
        self.porta = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.host = f"127.0.0.1:{self.porta}"
        self.vencido = srv_mod.valor_cookie(TOKEN, 0, int(time.time()) - srv_mod.VALIDADE_SESSAO - 60)

    def _http(self, metodo, caminho, cab=None, corpo=None):
        """Devolve status, Location e a lista de Set-Cookie (pode vir mais de um)."""
        con = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=5)
        cabs = {"Host": self.host, **(cab or {})}
        dados = corpo.encode() if corpo is not None else None
        if dados is not None:
            cabs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        con.request(metodo, caminho, body=dados, headers=cabs)
        r = con.getresponse()
        r.read()
        cookies = [v for k, v in r.getheaders() if k.lower() == "set-cookie"]
        local = r.getheader("Location")
        con.close()
        return r.status, local, cookies

    def _volta(self, cookies):
        for c in cookies:
            nome, _, resto = c.partition("=")
            if nome == srv_mod.COOKIE_VOLTA:
                return resto.split(";")[0]
        return None

    # ---- página: o servidor redireciona e guarda a página
    def test_recarga_com_sessao_vencida_vai_ao_login_e_guarda_a_pagina(self):
        st, local, cookies = self._http("GET", "/ajustes?aba=leitura",
                                        cab={**NAVEGA, "Cookie": f"{srv_mod.COOKIE}={self.vencido}"})
        # a URL fica exatamente /login?venceu=1: o Greg.app (main.swift) reconhece essa e entra de novo sozinho
        self.assertEqual((st, local), (303, "/login?venceu=1"))
        self.assertEqual(self._volta(cookies), quote("/ajustes?aba=leitura", safe=""))
        c = next(c for c in cookies if c.startswith(srv_mod.COOKIE_VOLTA + "="))
        for marca in ("HttpOnly", "SameSite=Strict", "Path=/", f"Max-Age={srv_mod.VALIDADE_VOLTA}"):
            self.assertIn(marca, c)

    def test_sem_cookie_nenhum_tambem_guarda_a_pagina(self):
        st, local, cookies = self._http("GET", "/frentes", cab=NAVEGA)
        self.assertEqual((st, local), (303, "/login"))
        self.assertEqual(self._volta(cookies), quote("/frentes", safe=""))

    # ---- login: devolve à página e apaga o cookie
    def test_login_devolve_a_pagina_guardada_e_apaga_a_volta(self):
        guardada = quote("/conversas?c=5", safe="")
        st, local, cookies = self._http("POST", "/login", corpo=f"token={TOKEN}",
                                        cab={"Cookie": f"{srv_mod.COOKIE_VOLTA}={guardada}"})
        self.assertEqual((st, local), (303, "/conversas?c=5"))
        self.assertTrue(any(c.startswith(srv_mod.COOKIE + "=") and "Max-Age" not in c for c in cookies), cookies)
        self.assertTrue(any(c.startswith(srv_mod.COOKIE_VOLTA + "=;") and "Max-Age=0" in c for c in cookies), cookies)

    def test_login_sem_volta_vai_para_demandas(self):
        st, local, _ = self._http("POST", "/login", corpo=f"token={TOKEN}")
        self.assertEqual((st, local), (303, "/demandas"))

    def test_login_recusa_volta_para_fora_do_painel(self):
        for ruim in ("//exemplo.test/x", "https://exemplo.test/", "/\\exemplo.test", "/%2F%2Fexemplo.test",
                     "/login?venceu=1", "/sair", "demandas", "/" + "a" * 1200):
            with self.subTest(ruim=ruim):
                st, local, _ = self._http("POST", "/login", corpo=f"token={TOKEN}",
                                          cab={"Cookie": f"{srv_mod.COOKIE_VOLTA}={quote(ruim, safe='')}"})
                self.assertEqual((st, local), (303, "/demandas"))

    def test_token_errado_nao_segue_a_volta(self):
        st, local, _ = self._http("POST", "/login", corpo="token=errado",
                                  cab={"Cookie": f"{srv_mod.COOKIE_VOLTA}={quote('/ajustes', safe='')}"})
        self.assertEqual((st, local), (401, None))

    # ---- chamada da página: 401 marcado, e a página de origem fica guardada pelo Referer
    def test_chamada_da_pagina_guarda_a_pagina_de_onde_veio(self):
        st, local, cookies = self._http("POST", "/demandas/1/recusar", cab={
            "Origin": f"http://{self.host}", "X-Vigia-Auto": "1", "Referer": f"http://{self.host}/frentes/secao/3",
            "Cookie": f"{srv_mod.COOKIE}={self.vencido}"})
        self.assertEqual((st, local), (401, None))
        self.assertEqual(self._volta(cookies), quote("/frentes/secao/3", safe=""))

    def test_referer_de_outro_endereco_nao_vira_volta(self):
        st, _, cookies = self._http("GET", "/conexao/lateral", cab={
            "X-Vigia-Auto": "1", "Referer": "http://exemplo.test/demandas"})
        self.assertEqual(st, 401)
        self.assertIsNone(self._volta(cookies))

    def test_formulario_sem_sessao_volta_para_a_pagina_do_formulario(self):
        st, local, cookies = self._http("POST", "/demandas/1/recusar", corpo="", cab={
            **NAVEGA, "Origin": f"http://{self.host}", "Referer": f"http://{self.host}/demandas?aba=hoje"})
        self.assertEqual((st, local), (303, "/login"))
        self.assertEqual(self._volta(cookies), quote("/demandas?aba=hoje", safe=""))

    def test_ida_e_volta_completa(self):
        _, _, cookies = self._http("GET", "/contatos?q=ana%20b", cab=NAVEGA)
        guardada = self._volta(cookies)
        st, local, _ = self._http("POST", "/login", corpo=f"token={TOKEN}",
                                  cab={"Cookie": f"{srv_mod.COOKIE_VOLTA}={guardada}"})
        self.assertEqual((st, local), (303, "/contatos?q=ana%20b"))

    # ---- REGRA PADRÃO (D-86): nenhuma rota protegida deixa agir sem sessão; percorre TODAS as rotas de app/paginas/
    def _todas_as_rotas(self):
        exatas, padroes = srv_mod.descobrir_rotas()
        caminhos = sorted(exatas) + sorted(rx.pattern.strip("^$").replace(r"(\d+)", "1").replace("\\", "")
                                           for rx, _ in padroes)
        return [c for c in caminhos if c not in srv_mod.SEM_COOKIE]

    def test_rota_sem_sessao_nunca_responde_a_tela(self):
        rotas = self._todas_as_rotas()
        self.assertGreater(len(rotas), 80)          # a lista vem do servidor, não de uma cópia aqui
        origem = {"Origin": f"http://{self.host}", "Referer": f"http://{self.host}/demandas"}
        sessoes = {"sem cookie": {}, "vencido": {"Cookie": f"{srv_mod.COOKIE}={self.vencido}"},
                   "lixo": {"Cookie": f"{srv_mod.COOKIE}=lixo"}}
        for rota in rotas:
            for nome, cookie in sessoes.items():
                login = "/login?venceu=1" if nome == "vencido" else "/login"
                for metodo in ("GET", "POST"):
                    corpo = "" if metodo == "POST" else None
                    with self.subTest(rota=rota, sessao=nome, metodo=metodo, modo="página"):
                        st, local, _ = self._http(metodo, rota, corpo=corpo, cab={**NAVEGA, **origem, **cookie})
                        # /tick é só dado (o servidor sempre responde 401 marcado); toda outra rota vai ao login
                        self.assertEqual((st, local), (401, None) if rota.endswith("/tick") else (303, login))
                    with self.subTest(rota=rota, sessao=nome, metodo=metodo, modo="chamada"):
                        con = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=5)
                        con.request(metodo, rota, body=corpo, headers={
                            "Host": self.host, "X-Vigia-Auto": "1", **origem, **cookie,
                            **({"Content-Type": "application/x-www-form-urlencoded"} if corpo is not None else {})})
                        r = con.getresponse()
                        r.read()
                        marca = r.getheader("X-Vigia-Sessao")
                        con.close()
                        self.assertEqual((r.status, marca), (401, "caiu"))

    def test_so_login_e_saude_ficam_sem_sessao(self):
        # abrir uma rota sem sessão exige mudar este teste de propósito (D-86)
        self.assertEqual(srv_mod.SEM_COOKIE, {"/saude", "/login"})
        st, _, _ = self._http("GET", "/saude")
        self.assertEqual(st, 200)


class TestVoltaSegura(unittest.TestCase):
    def test_aceita_caminho_do_painel_como_veio(self):
        for bom in ("/demandas", "/ajustes?aba=leitura", "/contatos?q=a%20b&r=%2F", "/frentes/secao/3"):
            self.assertEqual(srv_mod.volta_segura(bom), bom)

    def test_recusa(self):
        for ruim in (None, "", "demandas", "//x", "/\\x", "/%5Cx", "/%2F%2Fx", "http://x/", "/login", "/login/",
                     "/login?venceu=1", "/sair", "/x\r\nSet-Cookie: a=b", "/ção", "/" + "a" * 1001):
            self.assertIsNone(srv_mod.volta_segura(ruim), ruim)

    def test_cookie_ida_e_volta(self):
        c = srv_mod.cookie_volta("/contatos?q=a%20b")
        valor = c.split(";")[0].split("=", 1)[1]
        self.assertNotIn(";", valor)
        self.assertNotIn(" ", valor)
        self.assertEqual(srv_mod.ler_volta(valor), "/contatos?q=a%20b")


class TestCliente(unittest.TestCase):
    def setUp(self):
        self.base = (RAIZ / "app" / "templates" / "_base.html").read_text(encoding="utf-8")

    def test_faixa_saiu(self):
        self.assertNotIn("banner-sessao", self.base)
        self.assertNotIn("Nada do que você fizer agora é gravado", self.base)
        self.assertNotIn("sessao-entrar", self.base)

    def test_sessao_caida_vai_direto_ao_login(self):
        ini = self.base.index("window.vigiaSessaoCaiu = function")
        corpo = self.base[ini:self.base.index("};", ini)]
        self.assertIn("location.replace(", corpo)
        self.assertIn("'/login?venceu=1'", corpo)
        self.assertIn("X-Vigia-Sessao", self.base)
        # enquanto a ida ao login carrega, a tela não recebe clique
        self.assertIn(":root[data-sessao-caiu] body { pointer-events: none; }", self.base)

    def test_greg_app_reconhece_a_url_do_login_vencido(self):
        swift = (RAIZ / "macapp" / "Vigia" / "main.swift").read_text(encoding="utf-8")
        self.assertIn('w.url?.query == "venceu=1"', swift)


if __name__ == "__main__":
    unittest.main()
