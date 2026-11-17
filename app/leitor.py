"""Ponto único de escolha do motor de IA do Greg (pedido 68): `claude-code` (o `claude -p` de sempre, padrão) ou `api`
(app/leitor_api.py: protocolo de conversa da OpenAI, com a chave do próprio usuário ou um modelo local).
Quem chama: bin/coletor.py (leitura do lote), app/consolidacao.py (fechamento do dia), app/semana.py (relatório
semanal), app/paginas/ia.py (seção "IA" de Ajustes) e bin/bancada-ia.py.
O que faz: lê a configuração (`[ia]` do config.toml, sobreposta por dados/ia.json, que é o que Ajustes grava), guarda
a chave fora do config (Keychain quando ligado, D-45; senão dados/.env.ia, 0600) e despacha a chamada.
Sem configuração o motor é `claude-code` e a chamada é repassada a `lotes.chamar_claude` sem tocar em nenhum argumento.
O que não faz: não fala com a rede (quem fala é app/leitor_api.py), não imprime nem devolve a chave para tela ou log.
"""
from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

from app import lotes, segredos

RAIZ = Path(__file__).resolve().parents[1]
MOTORES = ("claude-code", "api")
NOME_CHAVE = "VIGIA_IA_CHAVE"
ARQ_CONFIG = "ia.json"          # dentro de dados/: o que Ajustes grava (vence o config.toml)
ARQ_CHAVE = ".env.ia"           # dentro de dados/: chave quando o Keychain está desligado (0600)
CHAVE_OK = re.compile(r"^[A-Za-z0-9._:+/=_-]{8,512}$")
HOSTS_LOCAIS = ("localhost", "127.0.0.1", "::1", "host.docker.internal")

# Provedores da lista de Ajustes (padrões do estudo, aprovados em 13/11/2026). O endereço de "outro" é livre.
PROVEDORES = {
    "claude-code": {"rotulo": "Claude Code", "motor": "claude-code", "endereco": ""},
    "openrouter": {"rotulo": "OpenRouter", "motor": "api", "endereco": "https://openrouter.ai/api/v1"},
    "outro": {"rotulo": "Outro compatível", "motor": "api", "endereco": ""},
    "local": {"rotulo": "Local (experimental)", "motor": "api", "endereco": "http://localhost:11434/v1"},
}

PADRAO = {
    "motor": "claude-code",
    "provedor": "claude-code",
    "endereco": "",
    "modelo": "",
    "teto_mes_usd": 5.0,                 # teto de gasto por mês, definido em Ajustes (endereço local não gasta e não tem teto)
    "preco_entrada_usd_milhao": 3.0,     # usado quando a resposta não informa o custo
    "preco_saida_usd_milhao": 15.0,
    "visao": True,                       # False: o modelo não lê imagem; ela vai marcada "não lida", nunca inventada
    "voltas_max": 40,                    # idas e vindas com a IA por lote
    "tempo_pedido_s": 180,               # espera por resposta de cada pedido
}
_LIMITES = {"teto_mes_usd": (0.1, 10000.0), "preco_entrada_usd_milhao": (0.0, 1000.0),
            "preco_saida_usd_milhao": (0.0, 1000.0), "voltas_max": (2, 200), "tempo_pedido_s": (5, 900)}


class ConfiguracaoInvalida(ValueError):
    """Endereço, modelo ou chave fora do aceito. A mensagem é para a tela: nunca carrega a chave."""


def eh_local(endereco: str) -> bool:
    """O endereço aponta para este Mac (Ollama, LM Studio)? Aí não há chave obrigatória, custo nem teto."""
    try:
        return (urlsplit(endereco).hostname or "").lower() in HOSTS_LOCAIS
    except ValueError:
        return False


def validar_endereco(endereco) -> str:
    """Base do provedor (ex.: https://openrouter.ai/api/v1). https sempre; http só para este Mac, porque em http a
    chave viajaria aberta. Sem usuário, consulta ou fragmento. Devolve sem a barra final."""
    e = str(endereco or "").strip()
    try:
        u = urlsplit(e)
        porta = u.port          # ValueError se a porta não for número
    except ValueError:
        raise ConfiguracaoInvalida("endereço inválido") from None
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise ConfiguracaoInvalida("endereço inválido: use algo como https://provedor/v1")
    if u.scheme == "http" and u.hostname.lower() not in HOSTS_LOCAIS:
        raise ConfiguracaoInvalida("endereço fora deste Mac precisa ser https")
    if not re.fullmatch(r"[A-Za-z0-9.:\[\]-]+", u.netloc) or not re.fullmatch(r"[A-Za-z0-9._~/-]*", u.path):
        raise ConfiguracaoInvalida("endereço com caractere não aceito")
    del porta
    return f"{u.scheme}://{u.netloc}{u.path}".rstrip("/")


def validar_modelo(modelo) -> str:
    m = str(modelo or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}", m):
        raise ConfiguracaoInvalida("nome de modelo inválido")
    return m


def _numero(valor, padrao, minimo, maximo):
    try:
        n = float(str(valor).replace(",", "."))
    except (TypeError, ValueError):
        return padrao
    if n != n or n < minimo or n > maximo:      # NaN ou fora da faixa
        return padrao
    return n


def normalizar(bruto) -> dict:
    """Configuração válida a partir de qualquer coisa. O que estiver errado cai no padrão; motor `api` com endereço ou
    modelo inválido continua `api` com o campo vazio (a chamada falha com aviso claro, em vez de voltar calada ao
    Claude Code e mandar a conversa para outro lugar sem o dono saber)."""
    bruto = bruto if isinstance(bruto, dict) else {}
    cfg = dict(PADRAO)
    motor = str(bruto.get("motor") or "").strip().lower()
    cfg["motor"] = motor if motor in MOTORES else "claude-code"
    prov = str(bruto.get("provedor") or "").strip().lower()
    if prov not in PROVEDORES or PROVEDORES[prov]["motor"] != cfg["motor"]:
        prov = "claude-code" if cfg["motor"] == "claude-code" else "outro"
    cfg["provedor"] = prov
    for campo, validar in (("endereco", validar_endereco), ("modelo", validar_modelo)):
        try:
            cfg[campo] = validar(bruto.get(campo)) if bruto.get(campo) else ""
        except ConfiguracaoInvalida:
            cfg[campo] = ""
    for campo, (minimo, maximo) in _LIMITES.items():
        n = _numero(bruto.get(campo), PADRAO[campo], minimo, maximo)
        cfg[campo] = int(n) if isinstance(PADRAO[campo], int) else round(n, 4)
    v = bruto.get("visao", True)
    cfg["visao"] = v if isinstance(v, bool) else str(v).strip().lower() not in ("0", "false", "nao", "não", "off", "")
    return cfg


def _do_toml() -> dict:
    try:
        return tomllib.loads((RAIZ / "config.toml").read_text(encoding="utf-8")).get("ia") or {}
    except (OSError, ValueError):
        return {}


def configuracao(config: dict | None = None, dados: Path | None = None) -> dict:
    """`[ia]` do `config` recebido (ou do config.toml, quando quem chama não tem o config em mãos), sobreposto por
    dados/ia.json. Sem nenhum dos dois: o padrão, motor `claude-code`."""
    base = dict((config.get("ia") or {}) if isinstance(config, dict) else _do_toml())
    if dados is not None:
        try:
            gravado = json.loads((Path(dados) / ARQ_CONFIG).read_text(encoding="utf-8"))
            if isinstance(gravado, dict):
                base.update(gravado)
        except (OSError, ValueError):
            pass
    return normalizar(base)


def gravar_configuracao(dados: Path, bruto: dict) -> dict:
    """Valida e grava dados/ia.json (troca atômica, 0600). Motor `api` exige endereço e modelo válidos."""
    cfg = normalizar(bruto)
    if cfg["motor"] == "api":
        cfg["endereco"] = validar_endereco(bruto.get("endereco"))
        cfg["modelo"] = validar_modelo(bruto.get("modelo"))
    dados = Path(dados)
    dados.mkdir(parents=True, exist_ok=True, mode=0o700)
    _escrever_0600(dados / ARQ_CONFIG, json.dumps(cfg, ensure_ascii=False, indent=1) + "\n")
    return cfg


def motor(config: dict | None = None, dados: Path | None = None) -> str:
    return configuracao(config, dados)["motor"]


def rotulo(cfg: dict) -> str:
    """Nome do provedor para a tela e para o detalhe dos avisos: "Claude Code", "OpenRouter", ou o host do endereço."""
    if cfg.get("motor") != "api":
        return "Claude Code"
    if cfg.get("provedor") == "openrouter":
        return "OpenRouter"
    try:
        return urlsplit(cfg.get("endereco") or "").hostname or "outra IA"
    except ValueError:
        return "outra IA"


# ------------------------------------------------------------------ chave (nunca no config, nunca na tela)

def _escrever_0600(caminho: Path, texto: str) -> None:
    tmp = caminho.with_name(caminho.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(texto)
    os.replace(tmp, caminho)
    os.chmod(caminho, 0o600)


def chave(dados: Path | None) -> str:
    """A chave do provedor: Keychain (se ligado e com o item), senão dados/.env.ia. '' = não há."""
    v = segredos.ler(NOME_CHAVE)
    if v:
        return v
    if dados is None:
        return ""
    try:
        for linha in (Path(dados) / ARQ_CHAVE).read_text(encoding="utf-8").splitlines():
            k, _, valor = linha.partition("=")
            if k.strip() == NOME_CHAVE and CHAVE_OK.match(valor.strip()):
                return valor.strip()
    except OSError:
        pass
    return ""


def tem_chave(dados: Path | None) -> bool:
    return bool(chave(dados))


def gravar_chave(dados: Path, valor: str) -> str:
    """Guarda a chave e diz onde ficou: "keychain" ou "arquivo". Com o Keychain ligado (VIGIA_KEYCHAIN=on) vai para
    ele, como os outros segredos (D-45); desligado, vai para dados/.env.ia (0600), fora do git e do config.toml."""
    valor = str(valor or "").strip()
    if not CHAVE_OK.match(valor):
        raise ConfiguracaoInvalida("chave fora do formato aceito (8 a 512 caracteres, sem espaço nem aspas)")
    if segredos.ligado():
        try:
            if segredos.gravar(NOME_CHAVE, valor):
                apagar_chave(dados, so_arquivo=True)
                return "keychain"
        except ValueError:
            pass                # o Keychain não aceita este formato: fica no arquivo
    dados = Path(dados)
    dados.mkdir(parents=True, exist_ok=True, mode=0o700)
    _escrever_0600(dados / ARQ_CHAVE, f"{NOME_CHAVE}={valor}\n")
    return "arquivo"


def apagar_chave(dados: Path, *, so_arquivo: bool = False) -> None:
    if not so_arquivo and segredos.ligado():
        segredos.apagar(NOME_CHAVE)
    try:
        (Path(dados) / ARQ_CHAVE).unlink()
    except OSError:
        pass


# ------------------------------------------------------------------ despacho

def ler(pasta: Path, *, modelo: str, prompt_sistema: Path, executor=None, timeout: float = 900.0,
        config: dict | None = None, dados: Path | None = None, **extras) -> dict:
    """Lê um lote (ou o fechamento do dia) com o motor configurado e devolve o JSON final de contadores.
    `claude-code`: `lotes.chamar_claude` com exatamente os mesmos argumentos de antes. `api`: app/leitor_api.py, com o
    modelo de `[ia]` (o `modelo` recebido é do Claude e é ignorado). `extras`: `campos` e `mensagem` (D-57)."""
    cfg = configuracao(config, dados)
    if cfg["motor"] != "api":
        return lotes.chamar_claude(pasta, modelo=modelo, prompt_sistema=prompt_sistema, executor=executor,
                                   timeout=timeout, **extras)
    from app import leitor_api
    return leitor_api.ler_lote(pasta, cfg=cfg, chave=chave(dados), dados=dados, prompt_sistema=prompt_sistema,
                               timeout=timeout, **extras)


def modelo_usado(final: dict, do_claude: str) -> str:
    """O modelo que de fato leu (para o registro do lote): o da API quando foi ela, senão o do Claude."""
    return str(final.get("_modelo") or do_claude)
