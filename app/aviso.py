"""Aviso ao dono por um número de WhatsApp que NÃO é o pessoal (o bot), por um de dois caminhos:
- direto numa Evolution (AVISO_EVOLUTION_URL + AVISO_EVOLUTION_APIKEY, chave da instância do bot), que é o caminho de
  quem instala o Greg sem o cofre (Docker, outra máquina);
- pelo cofre local (padrão quando AVISO_EVOLUTION_URL não está definido).
Sem nenhum dos dois, o aviso não sai e o log diz por quê; nada mais depende dele.
Quem chama: bin/fechar-semana.py e o canário da conexão (app/conexao.py).
O que não faz: não envia texto de conversa (D-4: só contador e link; recusa com ValueError), não guarda nem loga
o token do cofre (lido só na hora do envio, em memória, do arquivo que o .env do dono aponta em
AVISO_COFRE_ARQUIVO e AVISO_COFRE_CHAVE; o código não traz nome, pasta nem variável de cofre nenhum), não usa curl
(nada em argv) e não segue redirect (SemRedirect de app/rede, o mesmo da ponte do Frentes e do cliente da Evolution).
"""
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from app.rede import SemRedirect
from app.erros import causa

log = logging.getLogger("vigia.aviso")

# A instância do bot que manda o aviso mora no .env (AVISO_INSTANCIA), como o número (DONO_WHATSAPP): nada de nome real
# no código versionado (pedido 37).
LIMITE_CHARS = 300
_ASPAS = ('"', "“", "”", "«", "»", "„")


RAIZ = Path(__file__).resolve().parents[1]
COFRE_ARQUIVO = "AVISO_COFRE_ARQUIVO"   # arquivo onde está o token do cofre local do dono (ex.: ~/.meu-cofre/.env)
COFRE_CHAVE = "AVISO_COFRE_CHAVE"       # nome da variável dentro dele
_ARQUIVO_OK = re.compile(r"(~|/)[A-Za-z0-9._/-]+")      # vira regra de permissão do Claude Code: nada de espaço, vírgula, parêntese
_CHAVE_OK = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _do_env_do_projeto(chaves) -> dict:
    """As `chaves` no .env do projeto (VIGIA_ENV ou a raiz). Lido aqui para valer em todo ponto de entrada (ciclo,
    painel, fechamento da semana) sem depender de alguém exportar a variável antes."""
    out = {}
    try:
        linhas = Path(os.environ.get("VIGIA_ENV") or RAIZ / ".env").read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for linha in linhas:
        s = linha.strip()
        if s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        if k.strip() in chaves:
            out[k.strip()] = v.split(" #", 1)[0].strip().strip('"').strip("'")
    return out


def cofre_do_dono() -> tuple[str, str] | None:
    """(arquivo, nome da variável) do token do cofre local, de AVISO_COFRE_ARQUIVO e AVISO_COFRE_CHAVE: vale o
    ambiente e, sem ele, o .env do projeto. Sem as duas, ou com valor fora do formato, devolve None: o caminho pelo
    cofre fica desligado e nenhuma pasta a mais é negada à IA. O arquivo começa por ~/ ou por /."""
    chaves = (COFRE_ARQUIVO, COFRE_CHAVE)
    v = {k: (os.environ.get(k) or "").strip() for k in chaves}
    if not all(v.values()):
        do_arquivo = _do_env_do_projeto(chaves)
        v = {k: v[k] or do_arquivo.get(k, "") for k in chaves}
    arquivo, chave = v[COFRE_ARQUIVO], v[COFRE_CHAVE]
    if not arquivo or not chave:
        return None
    if not _ARQUIVO_OK.fullmatch(arquivo) or ".." in arquivo or arquivo.endswith("/") or not _CHAVE_OK.fullmatch(chave):
        log.error("%s ou %s fora do formato: caminho pelo cofre desligado", COFRE_ARQUIVO, COFRE_CHAVE)
        return None
    return arquivo, chave


def leitura_negada_do_cofre() -> list[str]:
    """Regra a mais para o --disallowedTools do Claude Code (app/lotes.py e app/semana.py): a pasta do cofre do dono,
    se o .env dele apontar uma. `~/pasta/arquivo` nega a pasta inteira; arquivo solto na casa ou na raiz nega só ele.
    Caminho absoluto leva a segunda barra que a regra do Claude Code pede. Sem cofre configurado, lista vazia."""
    onde = cofre_do_dono()
    if onde is None:
        return []
    arquivo = onde[0]
    pasta = arquivo.rsplit("/", 1)[0]
    alvo = arquivo if pasta in ("", "~") else pasta + "/**"
    return [f"Read({'/' if alvo.startswith('/') else ''}{alvo})"]


def ler_token_cofre() -> str:
    """Token do cofre local do dono: da variável que o .env aponta, no ambiente ou no arquivo apontado; levanta
    OSError/KeyError se não houver cofre configurado, arquivo ou linha."""
    onde = cofre_do_dono()
    if onde is None:
        raise KeyError(f"{COFRE_ARQUIVO} e {COFRE_CHAVE}")
    arquivo, chave = onde
    do_ambiente = os.environ.get(chave)
    if do_ambiente:
        return do_ambiente.strip()
    for linha in Path(arquivo).expanduser().read_text(encoding="utf-8").splitlines():
        if linha.startswith(chave + "="):
            return linha.partition("=")[2].strip().strip("'\"")
    raise KeyError(chave)


def validar(texto: str) -> str:
    """Aviso é contador + link. Recusa texto longo, parágrafos e aspas de citação (D-4)."""
    if not isinstance(texto, str) or not texto.strip():
        raise ValueError("aviso vazio")
    if len(texto) > LIMITE_CHARS:
        raise ValueError(f"aviso com {len(texto)} caracteres (máximo {LIMITE_CHARS}): só contador e link")
    if "\n\n" in texto or "\r\n\r\n" in texto:
        raise ValueError("aviso com quebra dupla: só contador e link")
    if any(a in texto for a in _ASPAS):
        raise ValueError("aviso com aspas de citação: só contador e link")
    return texto.strip()


def enviar(texto: str, *, cofre_url: str = "http://127.0.0.1:8000", token_fn=ler_token_cofre,
           instancia_id: int = 24, para: str | None = None, http=urllib, instancia: str | None = None,
           timeout: float = 20.0) -> bool:
    """POST {cofre}/proxy -> evolution /message/sendText/<instancia>. True se o cofre confirmou 2xx.
    `instancia_id` é só documental (id no evolution-mcp); o endpoint usa o nome da instância (AVISO_INSTANCIA)."""
    texto = validar(texto)
    numero = para or os.environ.get("DONO_WHATSAPP")      # R-1: vem do ambiente/.env, nunca versionado
    if not numero:
        log.error("aviso não enviado: DONO_WHATSAPP ausente")
        return False
    instancia = instancia or os.environ.get("AVISO_INSTANCIA")
    if not instancia:
        log.error("aviso não enviado: AVISO_INSTANCIA ausente")
        return False
    if os.environ.get("AVISO_EVOLUTION_URL"):
        return _enviar_direto(os.environ["AVISO_EVOLUTION_URL"], os.environ.get("AVISO_EVOLUTION_APIKEY", ""),
                              instancia, numero, texto, http=http, timeout=timeout)
    try:
        token = token_fn()
    except (OSError, KeyError, ValueError) as e:
        log.error("aviso não enviado: token do cofre indisponível (%s)", causa(e))
        return False
    corpo = json.dumps({"service": "evolution", "method": "POST", "endpoint": f"/message/sendText/{instancia}",
                        "body": {"number": numero, "text": texto}}).encode()
    pedido = http.request.Request(cofre_url.rstrip("/") + "/proxy", data=corpo, method="POST",
                                  headers={"Content-Type": "application/json", "X-Proxy-Auth": token})
    # localhost: ignora proxy de ambiente e NÃO segue redirect (3xx vira HTTPError -> False; o X-Proxy-Auth não viaja)
    abridor = http.request.build_opener(http.request.ProxyHandler({}), SemRedirect)
    try:
        with abridor.open(pedido, timeout=timeout) as r:
            envelope = json.loads(r.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        if isinstance(e, urllib.error.HTTPError):
            e.close()
        log.error("aviso não enviado: cofre falhou (%s)", causa(e))
        return False
    ok = isinstance(envelope, dict) and bool(envelope.get("success")) and 200 <= int(envelope.get("status") or 0) < 300
    if not ok:
        log.error("aviso não enviado: cofre respondeu success=%s status=%s",
                  envelope.get("success") if isinstance(envelope, dict) else "?",
                  envelope.get("status") if isinstance(envelope, dict) else "?")
    return ok


def _enviar_direto(url: str, apikey: str, instancia: str, numero: str, texto: str, *, http=urllib,
                   timeout: float = 20.0) -> bool:
    """POST {url}/message/sendText/<instancia> com o header apikey da instância do bot. True se 2xx.
    A chave vai só no header (nunca em argv, log ou exceção); não segue redirect."""
    if not apikey:
        log.error("aviso não enviado: AVISO_EVOLUTION_APIKEY ausente")
        return False
    corpo = json.dumps({"number": numero, "text": texto}).encode()
    alvo = url.rstrip("/") + "/message/sendText/" + urllib.parse.quote(instancia, safe="")
    pedido = http.request.Request(alvo, data=corpo, method="POST",
                                  headers={"Content-Type": "application/json", "apikey": apikey})
    abridor = http.request.build_opener(http.request.ProxyHandler({}), SemRedirect)
    try:
        with abridor.open(pedido, timeout=timeout) as r:
            ok = 200 <= int(getattr(r, "status", 200)) < 300
    except (urllib.error.URLError, OSError, ValueError) as e:
        if isinstance(e, urllib.error.HTTPError):
            e.close()
        log.error("aviso não enviado: Evolution do bot falhou (%s)", causa(e))
        return False
    if not ok:
        log.error("aviso não enviado: Evolution do bot não confirmou")
    return ok
