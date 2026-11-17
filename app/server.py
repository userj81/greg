"""Servidor HTTP do painel (127.0.0.1 só): descobre app/paginas/*.py, autentica por cookie e despacha.
Quem chama: `bin/vigia.sh painel` (`python3 -m app.server`) e o LaunchAgent (`python3 app/server.py`).
O que não faz: não fala com Evolution/Frentes diretamente (é das páginas), não loga querystring nem corpo
(podem levar nome de conversa), não aceita Host fora de loopback, não serve arquivo estático.
Costura do coordenador (F3) — CONTRATOS.md "Costura pendente registrada pela E9/E11".
"""
from __future__ import annotations

# `python3 app/server.py` (plist) põe app/ em sys.path[0] e app/http.py esconderia o pacote http da stdlib
# (F4 E11, alto). Corrige ANTES de qualquer import da stdlib; o plist também passa `-P`.
import os as _os  # noqa: E402
import sys as _sys  # noqa: E402
if __package__ in (None, ""):
    _aqui = _os.path.dirname(_os.path.abspath(__file__))
    _sys.path[:] = [p for p in _sys.path if _os.path.abspath(p or _os.getcwd()) != _aqui]
    _sys.path.insert(0, _os.path.dirname(_aqui))
    _sys.modules.pop("http", None)

import hmac
import importlib
import json
import logging
import os
import pkgutil
import re
import sys
import threading
import time
import tomllib
from http import cookies as _cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, unquote, urlsplit

from app.banco import Banco  # noqa: E402
from app import agendados, fila_envio, inicio, integracao, layout, preferencias, saude, segredos
from app.conexao import Conexao, _ler_env, dados_dir  # noqa: E402
from app.cursor import Cursor  # noqa: E402
from app.evolution_client import EvolutionClient  # noqa: E402
from app.frentes_ponte import FrentesPonte, criar_ponte, embutido  # noqa: E402
from app.http import Requisicao, Resposta  # noqa: E402
from app.whatsapp import comando_bloqueio, psql_ro  # noqa: E402
from app.erros import causa

RAIZ = Path(__file__).resolve().parents[1]
COOKIE = "vigia_sessao"
# pedido 83 (D-86): página em que a pessoa estava quando a sessão caiu. O servidor grava, o login lê e apaga. Fica fora da
# URL do login para que /login?venceu=1 continue igual (o Greg.app reconhece essa URL e entra de novo sozinho).
COOKIE_VOLTA = "vigia_volta"
VALIDADE_VOLTA = 600


def volta_segura(valor: str | None) -> str | None:
    """Caminho local do painel (como veio na linha do pedido, ainda codificado) para onde o login devolve a pessoa, ou
    None. Recusa endereço de fora (//x, /\\x, esquema), caractere fora do ASCII visível, o próprio login e o /sair, e
    caminho comprido demais. A conferência vale para o texto cru e para o decodificado."""
    if not valor or len(valor) > 1000 or any(not (32 < ord(c) < 127) for c in valor):
        return None
    for v in (valor, unquote(valor)):
        if not v.startswith("/") or v.startswith("//") or "\\" in v or any(ord(c) < 32 or ord(c) == 127 for c in v):
            return None
        partes = urlsplit(v)
        if partes.scheme or partes.netloc:
            return None
        caminho = partes.path.rstrip("/") or "/"
        if caminho in ("/login", "/sair") or caminho.startswith("/login/"):
            return None
    return valor


def cookie_volta(caminho: str) -> str:
    return f"{COOKIE_VOLTA}={quote(caminho, safe='')}; Path=/; HttpOnly; SameSite=Strict; Max-Age={VALIDADE_VOLTA}"


def ler_volta(cookie: str | None) -> str | None:
    return volta_segura(unquote(cookie)) if cookie else None


COOKIE_VOLTA_APAGA = f"{COOKIE_VOLTA}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
SEM_COOKIE = {"/saude", "/login"}
LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}
LIMITE_CORPO = 64 * 1024
# Pedido 50: só estes caminhos aceitam corpo cru (o arquivo que vai para o WhatsApp), e só de quem já tem sessão;
# sem sessão vale o LIMITE_CORPO de sempre, então o corpo grande nem chega a ser lido.
LIMITE_BRUTO = 16 * 1024 * 1024
# Pedido 71: o conector do Claude Code manda arquivo em base64 dentro do JSON; só com o token da integração.
LIMITE_MCP = 24 * 1024 * 1024
CAMINHOS_BRUTOS = ("/conversas/enviar-midia", "/conversas/agendar-midia", "/inicio/audio")   # pedido 69: áudio do início
# Pedido 80: arquivo anexado à seção. Vai do socket direto para o disco, em pedaços (nunca inteiro na memória), e só
# de quem já tem sessão. O limite é o de app/anexos.py.
CAMINHOS_ARQUIVO = ("/frentes/secao/anexar",)
CSP = ("default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
       "connect-src 'self'; media-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")


class CorpoGrande(ValueError):
    """Corpo acima de LIMITE_CORPO (único ValueError que vira 400 com texto)."""
log = logging.getLogger("vigia.server")

_LOGIN_HTML = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><title>Entrar · Greg</title>
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="color-scheme" content="light dark">
<style>/* estilo Papel (pedido 39): a mesma pele do painel, sem depender da base (esta página vem antes do login) */
:root{color-scheme:light dark;--fundo:light-dark(#F6F7F9,#1E2024);--grupo:light-dark(#FFFFFF,#26282D);--texto:light-dark(#1B1F27,#E8EAEE);
--meta:light-dark(#5B6472,#A2A8B3);--rotulo:light-dark(#6B7380,#9AA1AD);--fio:light-dark(rgba(16,24,40,.16),rgba(255,255,255,.10));
--acento:light-dark(#007AFF,#0A84FF);--acento-topo:light-dark(#1E8CFF,#2B95FF);--anel:light-dark(rgba(0,90,200,.35),rgba(0,70,160,.6));
--selecao:light-dark(rgba(0,122,255,.16),rgba(10,132,255,.24));--campo:light-dark(#FFFFFF,#1A1C20);
--campo-sombra:inset 0 0 0 .5px light-dark(rgba(16,24,40,.28),rgba(255,255,255,.18));--erro-fundo:light-dark(#FBE7E4,#3F1D1A);--erro-texto:light-dark(#9B1C12,#F29C92);
--sombra:0 0 0 .5px var(--fio),0 10px 30px light-dark(rgba(16,24,40,.10),rgba(0,0,0,.5))}
html{font-size:13px}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--fundo);color:var(--texto);font:400 1rem/1.45 -apple-system,"SF Pro Text",system-ui,sans-serif;-webkit-font-smoothing:antialiased}
form{width:320px;padding:22px 24px 24px;border-radius:8px;background:var(--grupo);box-shadow:var(--sombra)}
.marca{display:flex;align-items:center;gap:8px;font-weight:700;margin-bottom:18px}
.nome{height:30px;width:auto;display:block;fill:currentColor}.nome .ponto{fill:var(--acento)}
label{display:block;font-size:.9231rem;color:var(--meta);margin-bottom:4px}
input{width:100%;box-sizing:border-box;height:28px;padding:0 10px;border:0;border-radius:6px;background:var(--campo);color:inherit;font:inherit;box-shadow:var(--campo-sombra)}
input:focus{outline:0;box-shadow:var(--campo-sombra),0 0 0 3px var(--selecao)}
button{margin-top:14px;width:100%;height:28px;border:0;border-radius:6px;color:#fff;font:600 1rem/1 -apple-system,"SF Pro Text",system-ui,sans-serif;background:linear-gradient(180deg,var(--acento-topo),var(--acento));box-shadow:0 0 0 .5px var(--anel),0 1px 1px rgba(16,24,40,.14),inset 0 .5px 0 rgba(255,255,255,.28)}
button:hover{filter:brightness(1.04)}
p.erro{margin:0 0 12px;padding:6px 10px;border-radius:6px;background:var(--erro-fundo);color:var(--erro-texto);font-size:.9231rem}
p.meta{margin:12px 0 0;color:var(--rotulo);font-size:.9231rem}</style></head><body>
<form method="post" action="/login"><div class="marca"><svg class="nome" viewBox="40 -745 1735 975" role="img" aria-label="Greg"><path d="M236 9Q150 9 101 -58Q52 -126 52 -253Q52 -306 66 -370Q79 -434 106 -498Q134 -561 175 -614Q216 -666 272 -698Q328 -730 398 -730Q442 -730 473 -722Q504 -715 525 -703Q534 -698 531 -682L501 -530Q498 -513 485 -513Q473 -513 473 -526L472 -555Q469 -641 450 -672Q432 -704 393 -704Q347 -704 310 -679Q272 -654 242 -612Q211 -570 188 -519Q166 -468 152 -415Q137 -362 130 -315Q122 -268 122 -235Q122 -126 154 -72Q187 -17 239 -17Q271 -17 296 -36Q321 -54 332 -103L382 -322Q387 -342 384 -350Q380 -359 357 -361L322 -365Q309 -366 309 -376Q309 -388 327 -388H508Q521 -388 521 -378Q521 -367 506 -365L491 -363Q469 -360 462 -351Q456 -342 451 -321L393 -70Q391 -59 382 -49Q374 -39 356 -28Q331 -13 302 -2Q273 9 236 9ZM578 0Q564 0 567 -13L662 -412Q667 -434 664 -446Q660 -459 645 -459Q627 -459 606 -432Q584 -406 557 -332Q553 -319 543 -319Q528 -319 535 -337Q557 -406 582 -445Q606 -484 631 -500Q656 -516 677 -516Q710 -516 724 -492Q738 -467 725 -411L714 -364Q713 -357 717 -356Q721 -355 724 -361Q762 -448 793 -482Q824 -516 864 -516Q891 -516 904 -502Q918 -487 918 -462Q918 -437 906 -422Q894 -407 875 -407Q859 -407 852 -415Q845 -423 842 -434Q838 -444 834 -452Q830 -460 821 -460Q806 -460 782 -422Q758 -384 730 -320Q703 -255 678 -175Q652 -95 633 -11Q630 0 619 0ZM1011 9Q958 9 926 -32Q894 -74 894 -150Q894 -216 918 -281Q941 -346 979 -399Q1017 -452 1062 -484Q1108 -516 1151 -516Q1198 -516 1218 -494Q1239 -471 1239 -433Q1239 -388 1208 -344Q1176 -299 1120 -264Q1063 -229 987 -212Q965 -207 964 -187Q963 -180 963 -172Q963 -165 963 -158Q963 -101 982 -72Q1002 -43 1036 -43Q1074 -43 1110 -78Q1145 -113 1169 -177Q1172 -186 1181 -186Q1187 -186 1188 -181Q1190 -176 1188 -169Q1156 -81 1110 -36Q1065 9 1011 9ZM974 -250Q972 -243 976 -238Q981 -234 988 -236Q1050 -253 1092 -288Q1134 -322 1156 -365Q1177 -408 1177 -449Q1177 -491 1144 -491Q1115 -491 1081 -457Q1047 -423 1018 -368Q989 -313 974 -250ZM1323 216Q1244 216 1203 187Q1162 158 1162 118Q1162 83 1190 50Q1217 16 1291 -27Q1302 -33 1304 -40Q1305 -48 1294 -55Q1266 -73 1260 -97Q1255 -121 1273 -131L1349 -175Q1359 -180 1360 -188Q1360 -195 1349 -201Q1322 -217 1308 -246Q1295 -276 1295 -311Q1295 -365 1322 -412Q1348 -458 1392 -487Q1437 -516 1492 -516Q1515 -516 1531 -510Q1543 -506 1549 -518Q1566 -555 1592 -576Q1619 -598 1648 -598Q1669 -598 1680 -587Q1692 -576 1692 -557Q1692 -538 1681 -526Q1670 -514 1653 -514Q1640 -514 1634 -519Q1627 -524 1622 -529Q1617 -534 1606 -534Q1584 -534 1571 -509Q1567 -500 1569 -492Q1571 -485 1577 -477Q1603 -443 1603 -391Q1603 -344 1582 -302Q1562 -259 1526 -230Q1491 -200 1445 -190Q1427 -186 1412 -180Q1398 -175 1376 -161L1336 -137Q1321 -128 1328 -114Q1335 -101 1367 -89L1453 -57Q1509 -37 1530 -8Q1552 21 1552 60Q1552 108 1520 143Q1489 178 1437 197Q1385 216 1323 216ZM1362 -293Q1362 -247 1376 -226Q1389 -206 1414 -206Q1446 -206 1474 -236Q1501 -265 1518 -312Q1535 -358 1535 -408Q1535 -458 1522 -476Q1509 -495 1483 -495Q1452 -495 1424 -466Q1396 -436 1379 -390Q1362 -344 1362 -293ZM1334 191Q1373 191 1407 178Q1441 165 1462 141Q1484 117 1484 84Q1484 51 1468 30Q1451 8 1403 -10L1384 -17Q1366 -24 1348 -22Q1330 -20 1314 -12Q1260 18 1241 44Q1222 70 1222 101Q1222 138 1249 164Q1276 191 1334 191Z"/><path class="ponto" d="M1703 9Q1682 9 1669 -4Q1656 -18 1656 -38Q1656 -64 1674 -80Q1692 -96 1712 -96Q1732 -96 1746 -82Q1759 -69 1759 -49Q1759 -23 1741 -7Q1723 9 1703 9Z"/></svg></div>$erro
<label for="token">Token do painel</label>
<input id="token" name="token" type="password" autocomplete="current-password" autofocus required>
<button type="submit">Entrar</button>
<p class="meta">O VIGIA_TOKEN do .env. WhatsApp pessoal, só neste Mac.</p></form></body></html>"""


# ------------------------------------------------------------------ rotas

def descobrir_rotas(pacote: str = "app.paginas") -> tuple[dict, list]:
    """Junta ROTAS de app/paginas/*.py (ignora módulos iniciados por '_').
    Devolve (exatas: {caminho: handler}, padroes: [(regex, handler)]) — '{id}' vira (\\d+)."""
    exatas: dict = {}
    padroes: list = []
    pkg = importlib.import_module(pacote)
    for info in sorted(pkgutil.iter_modules(pkg.__path__), key=lambda i: i.name):
        if info.name.startswith("_"):
            continue
        mod = importlib.import_module(f"{pacote}.{info.name}")
        rotas = getattr(mod, "ROTAS", None)
        if not isinstance(rotas, dict):
            continue
        for caminho, handler in rotas.items():
            if "{" in caminho:
                rx = re.compile("^" + re.escape(caminho).replace(r"\{id\}", r"(\d+)") + "$")
                padroes.append((rx, handler))
            else:
                if caminho in exatas:
                    raise RuntimeError(f"rota duplicada: {caminho}")
                exatas[caminho] = handler
    return exatas, padroes


def _despachar(exatas: dict, padroes: list, caminho: str):
    h = exatas.get(caminho)
    if h is not None:
        return h
    for rx, handler in padroes:
        if rx.match(caminho):
            return handler
    return None


# ------------------------------------------------------------------ handler

VALIDADE_SESSAO = 30 * 24 * 3600   # segurança r01 (B8, "ok, vamos adotar" em 11/11/2026): o login vale 30 dias


def valor_cookie(token_painel: str, geracao: int = 0, emitido: int | None = None) -> str:
    """O cookie não leva o VIGIA_TOKEN cru: leva '<emitido>.HMAC-SHA256(token, "painel-v2:<geração>:<emitido>")'
    (F4 E9; B1; B8). `/sair` incrementa a geração em dados/sessao.json, então o cookie anterior deixa de valer;
    `emitido` (segundos) faz o cookie copiado parar de valer sozinho depois de VALIDADE_SESSAO."""
    import hashlib
    quando = int(time.time()) if emitido is None else int(emitido)
    mac = hmac.new((token_painel or "").encode("utf-8"), f"painel-v2:{int(geracao)}:{quando}".encode("utf-8"),
                   hashlib.sha256).hexdigest()
    return f"{quando}.{mac}"


def estado_cookie(recebido: str, token_painel: str, geracao: int, agora: float | None = None) -> str:
    """'ok', 'vencido' (assinatura certa, mais velho que VALIDADE_SESSAO) ou 'invalido'."""
    quando, ponto, _ = (recebido or "").partition(".")
    if not token_painel or not ponto or not quando.isdigit() or len(quando) > 12:
        return "invalido"
    if not hmac.compare_digest(recebido.encode("utf-8"), valor_cookie(token_painel, geracao, int(quando)).encode("utf-8")):
        return "invalido"
    idade = (time.time() if agora is None else agora) - int(quando)
    if idade < -60:
        return "invalido"
    return "ok" if idade <= VALIDADE_SESSAO else "vencido"


_TRAVA_SESSAO = threading.Lock()


def _arq_sessao(ctx) -> Path:
    return Path((getattr(ctx, "config", None) or {}).get("dados_dir", "dados")) / "sessao.json"


def ler_geracao(ctx) -> int:
    """Geração da sessão (dados/sessao.json); ausente ou ilegível = 0."""
    try:
        g = json.loads(_arq_sessao(ctx).read_text(encoding="utf-8")).get("geracao", 0)
    except (OSError, ValueError, AttributeError):
        return 0
    return g if isinstance(g, int) and not isinstance(g, bool) and g >= 0 else 0


def revogar_sessoes(ctx) -> int:
    """Incrementa a geração (0600, escrita atômica). Devolve a nova geração; falha de disco propaga OSError."""
    import tempfile
    arq = _arq_sessao(ctx)
    with _TRAVA_SESSAO:
        nova = ler_geracao(ctx) + 1
        arq.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=arq.parent, prefix=arq.name + ".", suffix=".tmp")  # mkstemp já cria 0600
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"geracao": nova}, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, arq)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return nova


def montar_handler(ctx, token_painel: str, exatas: dict, padroes: list):
    """Classe de handler fechada sobre o contexto (uma por servidor)."""
    token_bytes = (token_painel or "").encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        server_version = "Greg"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        # -- utilidades
        def version_string(self):
            return "Greg"

        def log_message(self, fmt, *args):  # só método, caminho sem query e status
            pass

        def _cabecalhos(self) -> dict:
            return {k.lower(): v for k, v in self.headers.items()}

        def _cookies(self) -> dict:
            jar = _cookies.SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie", ""))
            except _cookies.CookieError:
                return {}
            return {k: m.value for k, m in jar.items()}

        def _corpo(self, caminho: str = "") -> dict:
            # F4i I4: corpo que não é lido fica no socket e viraria a "próxima requisição" (keep-alive).
            # Chunked não é lido → fecha a conexão; corpo grande → fecha a conexão ao responder 400.
            if self.headers.get("Transfer-Encoding"):
                self.close_connection = True
                raise CorpoGrande("transfer-encoding não aceito")
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            if n <= 0:
                return {}
            tipo = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if (caminho in CAMINHOS_ARQUIVO and tipo not in ("application/json", "application/x-www-form-urlencoded")
                    and self._autenticado()):
                from app import anexos
                if n > anexos.LIMITE:
                    self.close_connection = True
                    raise CorpoGrande("corpo acima do limite")
                return {"_arquivo": self._para_o_disco(n), "_tipo": tipo}
            cru = (caminho in CAMINHOS_BRUTOS and tipo not in ("application/json", "application/x-www-form-urlencoded")
                   and self._autenticado())
            conector = caminho == "/mcp" and self._token_api_ok(caminho)
            if n > (LIMITE_BRUTO if cru else LIMITE_MCP if conector else LIMITE_CORPO):
                self.close_connection = True
                raise CorpoGrande("corpo acima do limite")
            bruto = self.rfile.read(n)
            if cru:
                return {"_bruto": bruto, "_tipo": tipo}
            if tipo == "application/json":
                try:
                    obj = json.loads(bruto.decode("utf-8"))
                except ValueError:
                    return {}
                if conector and isinstance(obj, list):      # lote JSON-RPC (versões antigas do protocolo)
                    return {"_lote": obj}
                return obj if isinstance(obj, dict) else {}
            if tipo == "application/x-www-form-urlencoded":
                return {k: v[0] for k, v in parse_qs(bruto.decode("utf-8"), keep_blank_values=True).items()}
            return {}

        def _para_o_disco(self, n: int) -> str:
            """Grava os `n` bytes do corpo em dados/anexos/.chegando/ e devolve o caminho. Conexão que cai no meio
            apaga o pedaço e fecha: nada fica pela metade."""
            import tempfile
            from app import anexos
            dados = Path((getattr(ctx, "config", None) or {}).get("dados_dir", "dados"))
            fd, nome = tempfile.mkstemp(prefix="sobe-", dir=str(anexos.pasta_chegando(dados)))
            falta = n
            try:
                with os.fdopen(fd, "wb") as f:
                    while falta > 0:
                        bloco = self.rfile.read(min(falta, 1024 * 1024))
                        if not bloco:
                            break
                        f.write(bloco)
                        falta -= len(bloco)
            except BaseException:
                Path(nome).unlink(missing_ok=True)
                raise
            if falta:
                Path(nome).unlink(missing_ok=True)
                self.close_connection = True
                raise CorpoGrande("corpo incompleto")
            return nome

        def _autenticado(self) -> bool:
            recebido = self._cookies().get(COOKIE, "")
            return self._sessao(recebido) == "ok"

        def _sessao(self, recebido: str | None = None) -> str:
            if recebido is None:
                recebido = self._cookies().get(COOKIE, "")
            return estado_cookie(recebido, token_painel, ler_geracao(ctx)) if token_bytes and recebido else "invalido"

        def _token_api_ok(self, caminho: str) -> bool:
            """Só em /envios*: o token de envio em X-Vigia-Envio vale como sessão (API das ferramentas do dono, R-15/D-44).
            As páginas conferem o token de novo antes de criar ou retomar um lote."""
            if caminho == "/mcp":   # pedido 71: o conector do Claude Code entra com o token da integração, e só aqui
                return integracao.token_ok(ctx, integracao.token_do_cabecalho(self.headers.get("Authorization")))
            if caminho != "/envios" and not caminho.startswith("/envios/"):
                return False
            esperado = (getattr(getattr(ctx, "tokens", None), "envio", "") or "").encode("utf-8")
            recebido = (self.headers.get("X-Vigia-Envio") or "").encode("utf-8")
            return bool(esperado) and bool(recebido) and hmac.compare_digest(recebido, esperado)

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").strip()
            nome = urlsplit("//" + host).hostname if host else None
            return nome in LOOPBACK

        def _navegacao(self) -> bool:
            """A janela está navegando (clique em link, formulário comum), e não a página buscando dados."""
            if self.headers.get("X-Vigia-Auto") == "1" or self.headers.get("X-Vigia-Envio"):
                return False
            modo = (self.headers.get("Sec-Fetch-Mode") or "").lower()
            aceita = self.headers.get("Accept") or ""
            return modo == "navigate" or (not modo and "text/html" in aceita)

        def _vestir_erro(self, r: Resposta, metodo: str) -> Resposta:
            """Auditoria T3: erro em texto puro ou JSON que iria cru para a janela vira página com a casca do painel
            e uma saída. Pedido da própria página (fetch), da API e do MCP passa como está."""
            try:
                tipo = (r.tipo or "").lower()
                if r.status < 400 or not self._navegacao() or not tipo.startswith(("text/plain", "application/json")):
                    return r
                texto = r.corpo.decode("utf-8", "replace") if isinstance(r.corpo, (bytes, bytearray)) else str(r.corpo or "")
                if tipo.startswith("application/json"):
                    try:
                        d = json.loads(texto)
                        texto = str((d.get("mensagem") or d.get("erro") or "") if isinstance(d, dict) else "")
                    except ValueError:
                        texto = ""
                from app.paginas import _comum
                nova = _comum.pagina_erro(ctx, r.status, texto, pode_repetir=metodo in ("GET", "HEAD"))
                nova.cabecalhos = list(r.cabecalhos or [])
                return nova
            except Exception as e:  # noqa: BLE001  (vestir o erro nunca pode virar outro erro)
                log.warning("página de erro não montada: %s", causa(e))
                return r

        def _responder(self, r: Resposta, cabeca_so: bool = False) -> None:
            corpo = r.corpo.encode("utf-8") if isinstance(r.corpo, str) else (r.corpo or b"")
            self.send_response(r.status)
            self.send_header("Content-Type", r.tipo)
            self.send_header("Content-Length", str(len(corpo)))
            # tudo sai sem cache, menos o que a rota marcar (hoje só a foto de contato, que já mora em disco no Greg)
            if not any(k.lower() == "cache-control" for k, _ in r.cabecalhos or []):
                self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")  # no-referrer anularia o Origin dos formulários (F4 E9)
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Frame-Options", "DENY")
            for k, v in r.cabecalhos or []:
                self.send_header(k, v)
            self.end_headers()
            if not cabeca_so:
                self.wfile.write(corpo)
            # pedido 41 (desempenho): tempo de resposta em ms no log de acesso; só caminho e status, nunca query/corpo
            t0 = getattr(self, "_t0", None)
            ms = f" {int((time.monotonic() - t0) * 1000)}ms" if t0 is not None else ""
            log.info("%s %s %s%s", self.command, urlsplit(self.path).path, r.status, ms)

        # -- login
        def _login(self, metodo: str, corpo: dict) -> Resposta:
            if metodo == "POST":
                dado = str(corpo.get("token") or "").encode("utf-8")
                if token_bytes and dado and hmac.compare_digest(dado, token_bytes):
                    jar = f"{COOKIE}={valor_cookie(token_painel, ler_geracao(ctx))}; Path=/; HttpOnly; SameSite=Strict"
                    # pedido 83: volta para a página em que estava quando a sessão caiu
                    volta = ler_volta(self._cookies().get(COOKIE_VOLTA)) or "/demandas"
                    return Resposta(303, "", cabecalhos=[("Location", volta), ("Set-Cookie", jar),
                                                         ("Set-Cookie", COOKIE_VOLTA_APAGA)])
                return Resposta(401, _LOGIN_HTML.replace("$erro", '<p class="erro">token errado</p>'))
            return Resposta(200, _LOGIN_HTML.replace("$erro", ""))

        def _pagina_de_volta(self, metodo: str) -> str | None:
            """pedido 83: a página que a pessoa tinha aberta. Navegação por GET é o próprio endereço; formulário e pedido
            da página (fetch) vêm da página que os fez, dita pelo Referer (Referrer-Policy same-origin)."""
            if metodo in ("GET", "HEAD") and self._navegacao():
                return volta_segura(self.path)
            ref = urlsplit(self.headers.get("Referer") or "")
            if ref.scheme not in ("http", "https") or ref.netloc != (self.headers.get("Host") or "").strip():
                return None
            return volta_segura(ref.path + (f"?{ref.query}" if ref.query else ""))

        # -- despacho
        def _tratar(self, metodo: str) -> None:
            self._t0 = time.monotonic()
            try:
                if not self._host_ok():
                    return self._responder(Resposta(421, "host não permitido", "text/plain; charset=utf-8"))
                partes = urlsplit(self.path)
                caminho = partes.path.rstrip("/") or "/"
                query = {k: v[0] for k, v in parse_qs(partes.query, keep_blank_values=True).items()}
                if metodo == "POST":
                    corpo = self._corpo(caminho)
                else:
                    corpo = {}
                    if self.headers.get("Transfer-Encoding") or (self.headers.get("Content-Length") or "0") not in ("", "0"):
                        self.close_connection = True   # GET/HEAD com corpo: não lemos, então não reaproveitamos o socket
                if caminho == "/":
                    return self._responder(Resposta(303, "", cabecalhos=[("Location", "/demandas")]))
                if caminho == "/login":
                    return self._responder(self._login(metodo, corpo), metodo == "HEAD")
                if caminho == "/sair" and metodo == "POST":
                    if self._autenticado():          # B1: revoga TODAS as sessões (cookie copiado deixa de valer)
                        revogar_sessoes(ctx)
                    jar = f"{COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
                    return self._responder(Resposta(303, "", cabecalhos=[("Location", "/login"), ("Set-Cookie", jar)]))
                if caminho == "/saude" and not self._autenticado():
                    # segurança r01 (B6): sem login só diz que o painel está de pé; estado, cursor e lotes pedem sessão
                    return self._responder(Resposta(200, '{"ok": true}', "application/json; charset=utf-8"), metodo == "HEAD")
                handler = _despachar(exatas, padroes, caminho)
                if handler is None:
                    r404 = Resposta(404, "Esta página não existe no Greg", "text/plain; charset=utf-8")
                    # sem sessão o 404 vai cru: a casca do painel carrega estado (lateral, avisos) que pede login
                    return self._responder(self._vestir_erro(r404, metodo) if self._autenticado() else r404, metodo == "HEAD")
                if caminho not in SEM_COOKIE and not self._autenticado() and not self._token_api_ok(caminho):
                    # B8: sessão vencida avisa o motivo; o Greg.app usa isso para entrar de novo sozinho
                    destino = "/login?venceu=1" if self._sessao() == "vencido" else "/login"
                    # pedido 83 (D-86): sem sessão nada fica aberto; o login devolve a pessoa a esta página
                    volta = self._pagina_de_volta(metodo)
                    cab_volta = [("Set-Cookie", cookie_volta(volta))] if volta else []
                    # pedido 63 (T1): pedido feito pela página (fetch, imagem, mídia) nunca recebe o 303 para o login.
                    # O fetch seguiria o redirecionamento, receberia 200 e a tela mostraria sucesso sem nada gravado.
                    # Só a navegação da janela é redirecionada; o resto recebe 401 com a marca que o _base.html lê.
                    modo = (self.headers.get("Sec-Fetch-Mode") or "").lower()
                    if ("json" in (self.headers.get("Accept") or "") or caminho.endswith("/tick")
                            or self.headers.get("X-Vigia-Auto") == "1" or (modo and modo != "navigate")):
                        return self._responder(Resposta(
                            401, json.dumps({"erro": "sem sessão", "login": destino,
                                             "mensagem": "Sua sessão terminou. Entre de novo."}, ensure_ascii=False),
                            "application/json; charset=utf-8", cabecalhos=[("X-Vigia-Sessao", "caiu"), *cab_volta]))
                    return self._responder(Resposta(303, "", cabecalhos=[("Location", destino), *cab_volta]))
                # pedido 69: Greg sem pareamento e sem frente abre no assistente de primeira instalação
                if caminho == "/demandas" and metodo == "GET" and self._navegacao() and inicio.deve_aparecer(ctx):
                    return self._responder(Resposta(303, "", cabecalhos=[("Location", "/inicio")]))
                req = Requisicao(metodo, caminho, query, corpo, self._cookies(), ctx, cabecalhos=self._cabecalhos())
                self._responder(self._vestir_erro(handler(req), metodo), metodo == "HEAD")
            except CorpoGrande:
                self._responder(Resposta(400, "corpo acima do limite", "text/plain; charset=utf-8"))
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as e:
                # o cliente fechou no meio da resposta (trocou de conversa, parou o áudio): não é erro do painel e
                # não há para quem responder 500
                log.debug("cliente fechou a conexão: %s", causa(e))
                self.close_connection = True
            except Exception as e:  # nunca stack trace com dado para o cliente nem para o log
                # pedido 63 (T2): o log leva rota, classe, arquivo e linha (sem mensagem nem variável) e um código curto
                # que também vai para a tela, para casar o que a pessoa viu com a linha do log
                cod = "E-" + os.urandom(3).hex()
                log.error("erro interno %s em %s %s: %s", cod, metodo, urlsplit(self.path).path, causa(e))
                self._responder(self._vestir_erro(Resposta(500, f"O Greg teve um erro interno ({cod}). Tente de novo; se repetir, "
                                                           "esse código acha a causa no log", "text/plain; charset=utf-8"), metodo))

        def do_GET(self):
            self._tratar("GET")

        def do_HEAD(self):
            self._tratar("HEAD")

        def do_POST(self):
            self._tratar("POST")

    return Handler


class ServidorVigia(ThreadingHTTPServer):
    """ThreadingHTTPServer sem o traceback do socketserver quando o CLIENTE derruba a conexão.

    O WKWebView mantém conexões keep-alive ociosas e as fecha com RST; o `readline()` do handler
    levanta ConnectionResetError (errno 54) e o socketserver imprimia 16 linhas de traceback por
    conexão. Não é erro do Greg: nenhuma requisição foi perdida. Qualquer outra exceção continua
    saindo inteira no log.
    """
    daemon_threads = True
    SILENCIOSAS = (ConnectionResetError, BrokenPipeError)

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, self.SILENCIOSAS):
            log.debug("conexão derrubada pelo cliente %s: %s", client_address, exc.__class__.__name__)
            return
        super().handle_error(request, client_address)


def criar_servidor(ctx, token_painel: str, *, porta: int = 8120, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    """Servidor pronto (não iniciado). Recusa bind fora de loopback (P-1). Exceção única: dentro do contêiner
    (VIGIA_CONTAINER=1) escuta em 0.0.0.0 da rede do Docker, e quem limita ao loopback é o compose, que publica a porta só
    em 127.0.0.1 do computador (docker-compose.yml da raiz)."""
    if host == "0.0.0.0" and os.environ.get("VIGIA_CONTAINER") == "1":
        pass
    elif host not in ("127.0.0.1", "::1", "localhost"):
        raise ValueError("o painel só escuta em loopback")
    if not token_painel:
        raise ValueError("VIGIA_TOKEN vazio: o painel não sobe sem token (gere com openssl rand -hex 24)")
    exatas, padroes = descobrir_rotas()
    return ServidorVigia((host, porta), montar_handler(ctx, token_painel, exatas, padroes))


# ------------------------------------------------------------------ contexto real

def _ler_token_envio(caminho: Path) -> str:
    """VIGIA_TOKEN_ENVIO: ~/.vigia-envio.env (0600); Keychain só se ligado (opt-in, D-45). Nenhum = sem envio (R-9)."""
    do_keychain = segredos.ler("VIGIA_TOKEN_ENVIO")
    if do_keychain:
        return do_keychain
    try:
        modo = caminho.stat().st_mode & 0o077
    except OSError:
        return ""
    if modo:
        log.warning("%s com permissão de grupo/outros: envio desativado", caminho.name)
        return ""
    return _ler_env(caminho).get("VIGIA_TOKEN_ENVIO", "")


def montar_ctx(raiz: Path) -> tuple[SimpleNamespace, str]:
    """Lê .env (config e segredos; Keychain só com VIGIA_KEYCHAIN=on, D-45) e config.toml, instancia Banco/Evolution/Frentes/Conexao.
    Devolve (ctx, token_painel)."""
    env = segredos.completar(_ler_env(raiz / ".env"), ("VIGIA_TOKEN", "EVOLUTION_INSTANCE_APIKEY", "FRENTES_TOKEN"))
    obrigatorias = ["EVOLUTION_URL", "EVOLUTION_INSTANCE", "EVOLUTION_INSTANCE_APIKEY", "VIGIA_TOKEN"]
    if not embutido(env.get("FRENTES_URL")):
        obrigatorias.append("FRENTES_TOKEN")   # sem FRENTES_URL o Frentes é o embutido (app/frentes_local.py)
    faltam = [k for k in obrigatorias if not env.get(k)]
    if faltam:
        raise ValueError("faltam no .env: " + ", ".join(faltam))
    try:
        config = tomllib.loads((raiz / "config.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        config = {}
    dados = dados_dir(raiz, config)
    dados.mkdir(mode=0o700, parents=True, exist_ok=True)
    config["dados_dir"] = str(dados)
    preferencias.ARQUIVO = dados / "preferencias.json"  # aparência escolhida em /ajustes
    layout.ARQUIVO = dados / "layout.json"              # larguras arrastadas nas divisórias (pedido 48)
    evo = EvolutionClient(env["EVOLUTION_URL"], env["EVOLUTION_INSTANCE"], env["EVOLUTION_INSTANCE_APIKEY"])
    banco = Banco(str(dados / "vigia.db"))
    banco.migrar()
    ponte = criar_ponte(env.get("FRENTES_URL"), env.get("FRENTES_TOKEN", ""), dados)
    conexao = Conexao.de_config(evo, banco, config, cursor=Cursor.carregar(dados / "cursor.json"),
                                estado_arq=dados / "conexao.json")
    tokens = SimpleNamespace(envio=_ler_token_envio(Path.home() / ".vigia-envio.env"), painel=env["VIGIA_TOKEN"])
    # r02: Conversas lê o banco da Evolution pelo papel só-leitura vigia_ro (o mesmo do coletor)
    psql = psql_ro(raiz / "bin" / "evolution.sh", vivo=True)   # sessão aberta: sem abrir processo a cada consulta
    ctx = SimpleNamespace(banco=banco, ponte=ponte, conexao=conexao, evo=evo, config=config, tokens=tokens,
                          executor=None, raiz=str(raiz), psql=psql,
                          bloqueio=comando_bloqueio(raiz / "bin" / "evolution.sh"))  # D-41
    # D-44: uma fila de envio por processo; lotes de um processo anterior viram "interrompido" aqui (o texto morreu com ele)
    ctx.fila = fila_envio.Fila(ctx, limites=fila_envio.limites_de_config(config))
    ctx.fila.iniciar()
    # pedido 72 (D-70): o que foi agendado sobrevive ao reinício (fica em dados/agendados) e sai pela thread abaixo
    ctx.agendador = agendados.Agendador(ctx, agendados.da(ctx))
    ctx.agendador.iniciar()
    # pedido 63, lote 2: o painel é quem percebe que o ciclo não roda (leitura parada) e notifica o que a central pedir
    ctx.leitura_automatica = True   # pedido 75: só o painel de verdade dispara leitura sozinho depois de parear
    ctx.vigia_saude = saude.Vigia(Path(config.get("dados_dir", "dados")))
    ctx.vigia_saude.iniciar()
    return ctx, env["VIGIA_TOKEN"]


def _aplicar_sql(raiz: Path) -> None:
    """Reaplica infra/sql/*.sql (idempotente) quando o painel sobe. As consultas de Conversas dependem de tabelas do
    esquema `vigia` (apagadas, entrega); sem isto, quem atualiza o Greg com a Evolution já no ar ficaria com a tela
    quebrada até rodar `evolution.sh up`. Falha aqui não derruba o painel: só fica no registro."""
    import subprocess
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(["/opt/homebrew/bin", "/usr/local/bin", env.get("PATH") or "/usr/bin:/bin"])
    try:
        r = subprocess.run(["/bin/bash", str(raiz / "bin" / "evolution.sh"), "aplicar-sql"], capture_output=True,
                           text=True, timeout=120, env=env)
        if r.returncode == 0:
            log.info("infra/sql conferido na subida")
        else:
            log.warning("infra/sql não aplicado na subida (código %s)", r.returncode)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("infra/sql não aplicado na subida: %s", causa(e))


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python3 -m app.server", description="Painel local do Greg (127.0.0.1).")
    ap.add_argument("--raiz", default=str(RAIZ))
    ap.add_argument("--porta", type=int, default=int(os.environ.get("VIGIA_PORT")) if os.environ.get("VIGIA_PORT") else None)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stderr)
    raiz = Path(args.raiz)
    try:
        ctx, token = montar_ctx(raiz)
        porta = args.porta if args.porta is not None else int(_ler_env(raiz / ".env").get("VIGIA_PORT") or 8120)
        if os.environ.get("VIGIA_CONTAINER") == "1":   # Docker: o compose publica só em 127.0.0.1 do computador
            srv = criar_servidor(ctx, token, porta=porta, host="0.0.0.0")
        else:
            srv = criar_servidor(ctx, token, porta=porta)
    except (ValueError, OSError) as e:
        print(f"painel não subiu: {e}", file=sys.stderr)
        return 2
    log.info("painel em http://127.0.0.1:%s (envio %s)", porta, "ligado" if ctx.tokens.envio else "desligado")
    threading.Thread(target=_aplicar_sql, args=(raiz,), daemon=True, name="aplicar-sql").start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
