"""Segredos do Greg no Keychain do macOS (fase A, D-45).
Quem chama: app/server.py, app/conexao.py, bin/coletor.py, bin/vigia-ponte, bin/vigia-enviar, bin/evolution.sh e o
Greg.app (Swift). Todos leem pelo MESMO caminho, `/usr/bin/security`, que é o único programa na ACL dos itens:
por isso nenhum deles dispara a janela "quer acessar o Keychain" (um LaunchAgent travado nela pararia o ciclo).
O que faz: item genérico, serviço `io.vigia.vigia`, conta = nome da variável. O valor NUNCA passa por argv:
leitura sai no stdout do subprocesso; escrita entra pelo stdin de `security -i`.
Padrão: DESLIGADO (decisão do dono em 07/11/2026, depois de duas rodadas de testes alcançarem o `security` real).
Os segredos ficam no `.env` (0600) e em ~/.vigia-envio.env; este módulo devolve '' e ninguém chama o `security`.
Ligar (opt-in, só com ele): VIGIA_KEYCHAIN=on no ambiente. VIGIA_KEYCHAIN_SERVICO troca o nome do serviço (testes).
Com o Keychain ligado e o item presente, ele vence o .env; sem o item, vale o .env — a migração é reversível (`exportar`).
CLI: python3 -m app.segredos conferir | migrar [--manter-env] | exportar | gravar NOME (valor pelo stdin) | apagar NOME
"""
import os
import re
import subprocess
import sys
from pathlib import Path

SERVICO = "io.vigia.vigia"
SECURITY = "/usr/bin/security"
# o que é segredo (vai para o Keychain); o resto do .env (URLs, porta, instância, número) continua no arquivo
SECRETAS = ("VIGIA_TOKEN", "EVOLUTION_INSTANCE_APIKEY", "FRENTES_TOKEN", "VIGIA_TOKEN_ENVIO")
ARQUIVO_POR_CHAVE = {"VIGIA_TOKEN_ENVIO": Path.home() / ".vigia-envio.env"}  # as outras moram no .env do projeto
VALOR_OK = re.compile(r"^[A-Za-z0-9._:+/=-]{8,512}$")  # hex do openssl, uuid, base64: sem aspas nem espaço, sem risco no parser do security
NOME_OK = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
RAIZ = Path(__file__).resolve().parents[1]


def ligado() -> bool:
    """DESLIGADO por padrão (decisão do dono, 07/11/2026): só toca no Keychain com VIGIA_KEYCHAIN=on explícito.
    Assim nenhuma suíte, LaunchAgent ou sessão paralela chama o `security` sem alguém ter pedido."""
    return os.environ.get("VIGIA_KEYCHAIN", "").strip().lower() in ("on", "1", "sim", "true")


def servico() -> str:
    return os.environ.get("VIGIA_KEYCHAIN_SERVICO") or SERVICO


def _nome(nome: str) -> str:
    if not NOME_OK.match(nome or ""):
        raise ValueError(f"nome de segredo inválido: {nome!r}")
    return nome


def _executor(executor):
    """Resolvido na CHAMADA, nunca como valor padrão de parâmetro: um padrão `=subprocess.run` congela a função
    original e passa reto por qualquer patch de teste (foi assim que 4 itens de teste entraram no Keychain real)."""
    return executor or subprocess.run


def ler(nome: str, *, executor=None) -> str:
    """Valor do item, ou '' se não existe, Keychain desligado ou `security` falhou/travou (10 s)."""
    if not ligado():
        return ""
    try:
        r = _executor(executor)([SECURITY, "find-generic-password", "-s", servico(), "-a", _nome(nome), "-w"],
                                capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (r.stdout or "").strip() if getattr(r, "returncode", 1) == 0 else ""


def gravar(nome: str, valor: str, *, executor=None) -> bool:
    """Cria ou atualiza (-U) o item; o comando inteiro entra pelo stdin de `security -i`. Confirma relendo."""
    _nome(nome)
    if not VALOR_OK.match(valor or ""):
        raise ValueError(f"valor de {nome} fora do padrão aceito (8–512 caracteres, sem espaço nem aspas)")
    if not ligado():
        return False
    comando = f"add-generic-password -U -s {servico()} -a {nome} -l Vigia:{nome} -w {valor}\n"
    try:
        r = _executor(executor)([SECURITY, "-i"], input=comando, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return getattr(r, "returncode", 1) == 0 and ler(nome, executor=executor) == valor


def apagar(nome: str, *, executor=None) -> bool:
    if not ligado():
        return False
    try:
        r = _executor(executor)([SECURITY, "delete-generic-password", "-s", servico(), "-a", _nome(nome)],
                                capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return getattr(r, "returncode", 1) == 0


def completar(env: dict, chaves=SECRETAS, *, executor=None) -> dict:
    """Para cada chave pedida, o Keychain VENCE o que veio do .env (se tiver o item). Devolve o mesmo dict."""
    for k in chaves:
        v = ler(k, executor=executor)
        if v:
            env[k] = v
    return env


# ------------------------------------------------------------------ .env (leitura/escrita só para migrar/exportar)

def _ler_env(caminho: Path) -> dict:
    out = {}
    try:
        linhas = caminho.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for linha in linhas:
        s = linha.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        out[k.strip()] = v.split(" #", 1)[0].strip().strip('"').strip("'")
    return out


def _escrever_0600(caminho: Path, texto: str) -> None:
    tmp = caminho.with_name(caminho.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(texto)
    os.replace(tmp, caminho)
    os.chmod(caminho, 0o600)


def _sem_segredos(caminho: Path) -> str:
    """Texto do .env sem as linhas das chaves secretas (comentários e o resto ficam)."""
    try:
        linhas = caminho.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    fora = {k for k in SECRETAS if ARQUIVO_POR_CHAVE.get(k, caminho) == caminho}
    saida = [l for l in linhas if not (("=" in l) and l.split("=", 1)[0].strip() in fora)]
    aviso = "# segredos (VIGIA_TOKEN, EVOLUTION_INSTANCE_APIKEY, FRENTES_TOKEN) moram no Keychain: python3 -m app.segredos conferir"
    if aviso not in saida:
        saida.insert(0, aviso)
    return "\n".join(saida).rstrip("\n") + "\n"


def conferir(raiz: Path = RAIZ) -> list[dict]:
    """Por chave: está no Keychain? está no arquivo? Nunca imprime valor."""
    envs = {raiz / ".env": _ler_env(raiz / ".env")}
    out = []
    for k in SECRETAS:
        arq = ARQUIVO_POR_CHAVE.get(k, raiz / ".env")
        if arq not in envs:
            envs[arq] = _ler_env(arq)
        out.append({"chave": k, "keychain": bool(ler(k)), "arquivo": bool(envs[arq].get(k)), "onde": str(arq)})
    return out


def migrar(raiz: Path = RAIZ, *, manter_env: bool = False) -> dict:
    """Copia os segredos dos arquivos para o Keychain (confirmando a releitura) e, salvo --manter-env, tira as linhas
    secretas do .env e apaga ~/.vigia-envio.env (que só tinha o token de envio). Devolve nomes, nunca valores."""
    if not ligado():
        raise RuntimeError("VIGIA_KEYCHAIN=off: nada a migrar")
    gravadas, faltando, falhas = [], [], []
    env = _ler_env(raiz / ".env")
    for k in SECRETAS:
        arq = ARQUIVO_POR_CHAVE.get(k, raiz / ".env")
        valor = (_ler_env(arq) if arq != raiz / ".env" else env).get(k, "")
        if not valor:
            if not ler(k):
                faltando.append(k)
            continue
        try:
            ok = gravar(k, valor)
        except ValueError:
            ok = False
        (gravadas if ok else falhas).append(k)
    limpos = []
    if not manter_env and not falhas:
        if any(k in env for k in SECRETAS if ARQUIVO_POR_CHAVE.get(k, raiz / ".env") == raiz / ".env"):
            _escrever_0600(raiz / ".env", _sem_segredos(raiz / ".env"))
            limpos.append(str(raiz / ".env"))
        arq_envio = ARQUIVO_POR_CHAVE["VIGIA_TOKEN_ENVIO"]
        if "VIGIA_TOKEN_ENVIO" in gravadas and arq_envio.exists():
            arq_envio.unlink()
            limpos.append(str(arq_envio))
    return {"gravadas": gravadas, "faltando": faltando, "falhas": falhas, "arquivos_limpos": limpos}


def exportar(raiz: Path = RAIZ) -> dict:
    """Caminho de volta: reescreve os segredos do Keychain nos arquivos 0600 (o .env mantém o resto)."""
    env_txt = _sem_segredos(raiz / ".env") if (raiz / ".env").exists() else ""
    escritas = []
    for k in SECRETAS:
        v = ler(k)
        if not v:
            continue
        arq = ARQUIVO_POR_CHAVE.get(k, raiz / ".env")
        if arq == raiz / ".env":
            env_txt += f"{k}={v}\n"
        else:
            _escrever_0600(arq, f"{k}={v}\n")
        escritas.append(k)
    if env_txt:
        _escrever_0600(raiz / ".env", env_txt)
    return {"escritas": escritas}


def main(argv=None) -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(prog="python3 -m app.segredos", description="Segredos do Greg no Keychain (nunca imprime valor).")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("conferir")
    m = sub.add_parser("migrar")
    m.add_argument("--manter-env", action="store_true", help="grava no Keychain mas deixa os arquivos como estão")
    sub.add_parser("exportar")
    g = sub.add_parser("gravar")
    g.add_argument("nome")
    a = sub.add_parser("apagar")
    a.add_argument("nome")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "conferir":
            for r in conferir():
                print(f"{r['chave']:<26} keychain={'sim' if r['keychain'] else 'NÃO':<4} arquivo={'sim' if r['arquivo'] else 'não':<4} {r['onde']}")
            return 0
        if args.cmd == "migrar":
            print(json.dumps(migrar(manter_env=args.manter_env), ensure_ascii=False))
            return 0
        if args.cmd == "exportar":
            print(json.dumps(exportar(), ensure_ascii=False))
            return 0
        if args.cmd == "gravar":
            valor = sys.stdin.readline().strip()
            print("gravado" if gravar(args.nome, valor) else "FALHOU")
            return 0
        if args.cmd == "apagar":
            print("apagado" if apagar(args.nome) else "FALHOU")
            return 0
    except (ValueError, RuntimeError) as e:
        print(f"segredos: {e}", file=sys.stderr)
        return 2
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
