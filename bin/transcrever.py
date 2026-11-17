#!/opt/homebrew/bin/python3
"""Transcreve áudios ATRASADOS (pedido 30, D-47): os que chegaram antes de a transcrição existir e os que os tetos do
ciclo adiaram. Lê "Message" (só pós-pareamento, R-2) como vigia_ro, ignora conversas bloqueadas (D-41) e passa pelo
MESMO Transcritor do ciclo (tetos do config, um processo com `nice`, texto no SQLite). Rodar de novo continua de onde
parou (o que já tem transcrição é pulado). Segura a trava do ciclo (dados/vigia.lock, app/trava.py) enquanto roda: nunca
dois motores ao mesmo tempo; com o ciclo em andamento sai com 2 sem fazer nada (--dry-run não precisa da trava).
Saída: 0 ok · 1 falhou · 2 não rodou (Postgres/Evolution fora, sem pareamento, trava ocupada).
Quem chama: o dono (ou o Claude a pedido dele), à mão. O LaunchAgent NÃO chama isto: o ciclo cuida do que chega.
O que não faz: não classifica, não mexe no cursor, não loga texto (só contagens); com --dry-run não baixa nem grava.
"""
import argparse
import importlib.util
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from app import lotes, segredos, transcricao  # noqa: E402
from app.erros import EvolutionIndisponivel, NaoAutorizado, VigiaErro, causa  # noqa: E402
from app.trava import Trava, TravaOcupada  # noqa: E402

log = logging.getLogger("vigia.transcrever")
LIMITE_MENSAGENS = 2000


def _coletor():
    """Reaproveita config, .env, psql e log do coletor (é um script, não um módulo: carregado pelo caminho)."""
    spec = importlib.util.spec_from_file_location("coletor", RAIZ / "bin" / "coletor.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def linhas_de_audio(psql, desde: int, limite: int = LIMITE_MENSAGENS) -> list[dict]:
    """Mensagens de áudio dentro da janela de leitura (Cursor.desde, D-53), mais recentes primeiro (o Transcritor reordena por chegada)."""
    consulta = (f"SELECT {lotes._colunas('0')} FROM public.\"Message\" m "
                f"WHERE m.\"messageType\" IN ('audioMessage','pttMessage') AND m.\"messageTimestamp\" >= {int(desde)} "
                f'ORDER BY m."messageTimestamp" DESC LIMIT {int(limite)}')
    return [ln for ln in (lotes._linha(c) for c in psql(consulta)) if ln]


class _SemTranscricoes:
    """Dry-run num banco ainda sem a migração 005 (ou inexistente): nada foi transcrito ainda."""

    def transcricoes(self, ids):
        return {}


def _argumentos(argv):
    p = argparse.ArgumentParser(prog="transcrever.py",
                                description="Transcreve os áudios que ainda não têm texto (D-47), dentro dos tetos do config.")
    p.add_argument("--dry-run", action="store_true", help="só conta o que faria; não baixa, não roda o motor, não grava")
    p.add_argument("--teto", type=int, help="áudios nesta rodada (padrão: [transcricao].teto_ciclo)")
    p.add_argument("--segundos", type=int, help="soma de áudio nesta rodada (padrão: [transcricao].teto_segundos_ciclo)")
    p.add_argument("--config", default=str(RAIZ / "config.toml"))
    return p.parse_args(argv)


def main(argv=None) -> int:
    os.umask(0o077)
    args = _argumentos(argv)
    col = _coletor()
    col._configurar_log()
    dados = Path(os.environ.get("VIGIA_DADOS") or RAIZ / "dados")
    env = col.ler_env(Path(os.environ.get("VIGIA_ENV") or RAIZ / ".env"))
    segredos.completar(env, ("EVOLUTION_INSTANCE_APIKEY",))
    for k in ("EVOLUTION_URL", "EVOLUTION_INSTANCE", "EVOLUTION_INSTANCE_APIKEY"):
        env.setdefault(k, os.environ.get(k, ""))

    def fabrica_banco():
        from app.banco import Banco
        return Banco(str(dados / "vigia.db"), somente_leitura=args.dry_run)

    def fabrica_evo():
        from app.evolution_client import EvolutionClient
        if not env.get("EVOLUTION_INSTANCE_APIKEY"):
            raise NaoAutorizado("EVOLUTION_INSTANCE_APIKEY ausente no .env")
        return EvolutionClient(env.get("EVOLUTION_URL") or "http://127.0.0.1:8080",
                               env.get("EVOLUTION_INSTANCE") or "vigia", env["EVOLUTION_INSTANCE_APIKEY"])

    try:
        from app.cursor import Cursor
        config = col.carregar_config(Path(args.config))
        cfg = dict(config.get("transcricao") or {})
        if args.teto is not None:
            cfg["teto_ciclo"] = max(0, args.teto)
        if args.segundos is not None:
            cfg["teto_segundos_ciclo"] = max(0, args.segundos)
        cursor = Cursor.carregar(dados / "cursor.json")
        if cursor.pareado_em is None:
            raise lotes.NaoPareado("cursor sem pareado_em: nada é lido antes do pareamento (R-2)")
        psql = col.psql_ro_via_script(Path(os.environ.get("VIGIA_EVOLUTION_SH") or RAIZ / "bin/evolution.sh"))
        bloqueados = frozenset(ln[0] for ln in psql("SELECT jid FROM vigia.bloqueados") if ln and ln[0])
        linhas = linhas_de_audio(psql, cursor.desde)
        t = transcricao.Transcritor(cfg, dados / "caixa" / ".audio")
        if not t.ativo():
            print(json.dumps({"status": "desligado", "motivo": "transcricao.ativo=false ou motor ausente"}))
            return 0
        if args.dry_run:
            try:
                banco = fabrica_banco() if (dados / "vigia.db").exists() else _SemTranscricoes()
                plano = t.planejar(linhas, banco=banco, desde=cursor.desde, bloqueados=bloqueados)
            except sqlite3.OperationalError:   # banco de antes da migração 005, aberto só para leitura
                plano = t.planejar(linhas, banco=_SemTranscricoes(), desde=cursor.desde, bloqueados=bloqueados)
            r = {"status": "dry_run", "audios_no_banco": len(linhas), "candidatos": 0, "fazer": 0, "pulados": 0,
                 "adiados": 0, "segundos": 0}
            if plano:
                r.update(candidatos=len(plano["candidatos"]), fazer=len(plano["fazer"]), pulados=len(plano["pulados"]),
                         adiados=len(plano["adiados"]), segundos=plano["segundos"])
            print(json.dumps(r, ensure_ascii=False))
            return 0
        with Trava(dados):   # o ciclo do LaunchAgent vê a trava e sai com 2; nunca dois motores ao mesmo tempo
            r = t.rodar(linhas, obter_banco=fabrica_banco, obter_evo=fabrica_evo, desde=cursor.desde,
                        bloqueados=bloqueados)
    except (lotes.PostgresIndisponivel, lotes.NaoPareado, EvolutionIndisponivel, NaoAutorizado, TravaOcupada,
            FileNotFoundError) as e:
        log.error("não rodou: %s", e if isinstance(e, VigiaErro) else causa(e))
        print(f"transcrever: não rodou ({e if isinstance(e, VigiaErro) else type(e).__name__})", file=sys.stderr)
        return 2
    except VigiaErro as e:
        log.error("falhou: %s", e)
        print(f"transcrever: falhou ({e})", file=sys.stderr)
        return 1
    except Exception as e:  # nunca a mensagem da exceção: pode conter texto
        log.error("falhou: %s", causa(e))
        print(f"transcrever: falhou ({type(e).__name__})", file=sys.stderr)
        return 1
    print(json.dumps({"status": "ok", "audios_no_banco": len(linhas), **r}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
