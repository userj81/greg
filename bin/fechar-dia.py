#!/opt/homebrew/bin/python3
"""Fechamento do dia do Greg (D-57, pedido 45; R-12): depois de [horarios].diario, consolida os cartões provisórios do
dia (um passe do Claude só nas conversas com 2+ cartões) e sobe o que sobrou ao Frentes. Antes da hora, não faz nada.
Uso: fechar-dia.py [--dry-run] [--config config.toml]
Saída: 0 ok (inclui "antes da hora", "nada pendente" e "adiada") · 1 falhou · 2 não rodou (Postgres/Frentes fora, sem
pareamento, Claude ausente).
Quem chama: bin/vigia.sh ciclo (depois do coletor, a cada 30 min) e o dono, à mão.
O que não faz: não lê .env de envio, não loga texto de mensagem (só ids e contagens), com --dry-run não escreve nada.
"""
import argparse
import json
import logging
import os
import sys
import tomllib
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from app import consolidacao, lotes, segredos  # noqa: E402
from app.erros import FrentesIndisponivel, NaoAutorizado, VigiaErro, causa  # noqa: E402

log = logging.getLogger("vigia.fechar_dia")
CHAVES_ENV = ("FRENTES_URL", "FRENTES_TOKEN")


def ler_env(caminho: Path) -> dict:
    """Só as chaves da lista branca; comentário no fim da linha e aspas saem (mesma regra do coletor)."""
    saida = {}
    try:
        texto = caminho.read_text(encoding="utf-8")
    except OSError:
        return saida
    for bruto in texto.splitlines():
        linha = bruto.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        k, v = linha.split("=", 1)
        if k.strip() in CHAVES_ENV:
            saida[k.strip()] = v.split(" #", 1)[0].split("\t#", 1)[0].strip().strip('"').strip("'")
    return saida


def carregar_config(caminho: Path) -> dict:
    try:
        return tomllib.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise VigiaErro(f"config ilegível ({type(e).__name__})") from None


def _argumentos(argv):
    p = argparse.ArgumentParser(description="Fechamento do dia (D-57): consolida cartões provisórios e sobe ao Frentes.")
    p.add_argument("--dry-run", action="store_true", help="só conta; não chama o Claude nem o Frentes")
    p.add_argument("--config", default=str(RAIZ / "config.toml"))
    return p.parse_args(argv)


def _configurar_log() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stderr)


def main(argv=None) -> int:
    os.umask(0o077)
    args = _argumentos(argv)
    _configurar_log()
    dados = Path(os.environ.get("VIGIA_DADOS") or RAIZ / "dados")
    env = ler_env(Path(os.environ.get("VIGIA_ENV") or RAIZ / ".env"))
    segredos.completar(env, ("FRENTES_TOKEN",))
    for k in CHAVES_ENV:
        if os.environ.get(k):
            env[k] = os.environ[k]
    try:
        from app.banco import Banco
        from app.cursor import Cursor
        from app.frentes_ponte import criar_ponte
        config = carregar_config(Path(args.config))
        cursor = Cursor.carregar(dados / "cursor.json")
        if cursor.pareado_em is None:
            raise lotes.NaoPareado("cursor sem pareado_em: nada é lido antes do pareamento (R-2)")
        psql = lotes.psql_via_script(Path(os.environ.get("VIGIA_EVOLUTION_SH") or RAIZ / "bin/evolution.sh"),
                                     "psql-ro-stdin", 60.0)
        banco = Banco(str(dados / "vigia.db"))
        ponte = criar_ponte(env.get("FRENTES_URL"), env.get("FRENTES_TOKEN", ""), dados, padrao_url="http://127.0.0.1:8110")
        r = consolidacao.rodar(banco=banco, ponte=ponte, psql=psql, config=config, dados=dados,
                               prompt_sistema=RAIZ / "prompts/consolidar.md", desde=cursor.desde, dry_run=args.dry_run)
    except (lotes.PostgresIndisponivel, lotes.NaoPareado, NaoAutorizado, FrentesIndisponivel, FileNotFoundError) as e:
        log.error("não rodou: %s", e if isinstance(e, VigiaErro) else causa(e))
        print(f"fechar-dia: não rodou ({e if isinstance(e, VigiaErro) else type(e).__name__})", file=sys.stderr)
        return 2
    except VigiaErro as e:
        log.error("falhou: %s", e)
        print(f"fechar-dia: falhou ({e})", file=sys.stderr)
        return 1
    except Exception as e:  # nunca imprime a mensagem: pode carregar trecho de conversa
        log.error("falhou: %s", causa(e))
        print(f"fechar-dia: falhou ({type(e).__name__})", file=sys.stderr)
        return 1
    print(json.dumps(r, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
