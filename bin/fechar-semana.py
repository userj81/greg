#!/opt/homebrew/bin/python3
"""Fechamento semanal do Greg (R-12): relatório do Claude (Opus) sobre as classificações, aviso ao dono e, 24 h depois,
limpeza do cru com prova de count=0 e lápide.
Uso:
  fechar-semana.py [--semana AAAA-Www] [--dry-run]            relatório + aviso (contador e link)
  fechar-semana.py --executar-limpeza|--apagar [--forcar]     limpeza do cru da semana, se passaram 24 h
  fechar-semana.py --segurar [DIAS]                           adia a limpeza da semana (padrão 7 dias)
  fechar-semana.py --restaurar ARQUIVO.jsonl [--dry-run]      devolve ao Postgres um backup de dados/backup/ (idempotente; marca a semana p/ nova limpeza)
Sem --semana, percorre TODAS as semanas fechadas sem lápide desde o pareamento (cursor.pareado_em), da mais antiga para
a mais nova (semana perdida por Mac dormindo é alcançada no dia seguinte, R-12/R-2). Para no primeiro erro (Postgres
fora = 2; falha = 1), mas não em aguardando_24h/segura/ha_nao_lidas: essas seguem para a semana seguinte.
Saída: 0 ok (inclui "adiada" e lock ocupado) · 1 falhou · 2 não rodou (Postgres fora, semana ainda aberta, sem relatório).
Quem chama: LaunchAgent io.vigia.vigia-semanal (via bin/vigia.sh semanal) e o dono, à mão.
O que não faz: não manda texto de conversa (o aviso leva só contadores e link), não apaga classificações nem itens do
Frentes, não apaga nada com --dry-run, não loga texto de mensagem e não ecoa token.
D-30 (aceito): linhas [contexto] da semana W sobrevivem nos lotes de W+1 até o fechamento seguinte (D-5); não se apaga
linha dentro de lote.
"""
import argparse
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from app import aviso, lotes, semana as sem  # noqa: E402
from app.erros import LimpezaIncompleta, VigiaErro, causa  # noqa: E402
from app.lotes import PostgresIndisponivel  # noqa: E402
from app.semana import (ESPERA_APAGAR_S, janela, limpar, relatorio, segura, segurar, semana_de, semana_fechada)  # noqa: E402,F401

log = logging.getLogger("vigia.fechar_semana")


# ---------------------------------------------------------------------------
# Peças injetáveis
# ---------------------------------------------------------------------------

_STDIN = {"psql-ro": "psql-ro-stdin", "psql-limpeza": "psql-limpeza-stdin"}      # nomes antigos -> variante stdin


def psql_via_script(subcomando: str, script: Path, timeout: float = 120.0):
    """psql(query) -> list[list[str]] chamando `bin/evolution.sh <sql-stdin|psql-ro-stdin|psql-limpeza-stdin>`.
    A query vai SEMPRE pelo stdin (I3: nada de SQL em argv/ps, sem E2BIG). Implementação única em lotes.psql_via_script."""
    return lotes.psql_via_script(script, _STDIN.get(subcomando, subcomando), timeout)


def ler_env(caminho: Path) -> dict:
    saida = {}
    try:
        texto = caminho.read_text(encoding="utf-8")
    except OSError:
        return saida
    for bruto in texto.splitlines():
        linha = bruto.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        valor = valor.split(" #")[0]
        saida[chave.strip()] = valor.strip().strip("'\"")
    return saida


# ---------------------------------------------------------------------------
# Passos
# ---------------------------------------------------------------------------

LOCK_EXPIRA_S = 3600        # igual ao de bin/vigia.sh (E11): lock mais velho que isto é de job morto
MAX_SEMANAS = 104           # teto de semanas percorridas numa rodada (2 anos)
LOTE_RESTAURAR_BYTES = 96 * 1024      # bytes UTF-8 por lote (vai pelo stdin do psql, não por argv)
LOTE_RESTAURAR_LINHAS = 200
_CODIGO_1 = ("aviso_falhou", "sem_aviso", "ha_nao_lidas")
_CODIGO_2 = ("semana_aberta", "sem_relatorio")


def marca_restaurada(dados: Path, semana: str) -> Path:
    """dados/restaurada-<semana>: o cru da semana voltou ao Postgres por --restaurar e precisa de nova limpeza (I2)."""
    return Path(dados) / f"restaurada-{semana}"


def _restauradas(dados: Path) -> set[str]:
    try:
        return {p.name[len("restaurada-"):] for p in Path(dados).glob("restaurada-*")
                if p.is_file() and re.fullmatch(r"\d{4}-W\d{2}", p.name[len("restaurada-"):])}
    except OSError:
        return set()


def _marca_enviado(dados: Path, semana: str) -> Path:
    return Path(dados) / "relatorios" / f"{semana}.enviado"


def _enviado_em(dados: Path, semana: str) -> float | None:
    m = _marca_enviado(dados, semana)
    try:
        return float(m.read_text(encoding="utf-8").strip())
    except ValueError:
        return m.stat().st_mtime
    except OSError:
        return None


def texto_do_aviso(semana: str, n_msgs: int, n_itens: int, porta: str, libera: int | None = None,
                   guardar: bool = False) -> str:
    """`libera`: epoch em que o prazo escolhido em Ajustes deixa apagar (pedido 67); None ou já passado = 24 h.
    `guardar` (pedido 74): Ajustes está em "Não apagar"; o aviso não promete limpeza."""
    quando = "limpeza em 24 h"
    if guardar:
        quando = "mensagens guardadas, sem limpeza"
    elif libera is not None and libera > janela(semana)[1]:
        quando = "limpeza a partir de " + time.strftime("%d/%m", time.localtime(libera + 86400))
    return (f"Semana {semana} fechada · {n_msgs} msgs · {n_itens} itens · {quando} · "
            f"http://127.0.0.1:{porta}/demandas")


def lock_ocupado(dados: Path, agora: float | None = None, expira_s: int = LOCK_EXPIRA_S) -> bool:
    """True se dados/vigia.lock (mkdir do bin/vigia.sh) existe, é recente e é de OUTRO processo vivo (F4(8) item 3).
    O lock do chamador (`vigia.sh semanal` grava o seu $$ em vigia.lock/pid e faz exec deste script) não conta."""
    agora = time.time() if agora is None else agora
    lock = Path(dados) / "vigia.lock"
    try:
        idade = agora - lock.stat().st_mtime
    except OSError:
        return False
    if idade > expira_s:
        return False
    try:
        pid = int((lock / "pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return True         # lock sem pid legível (formato antigo): recente = ocupado
    if pid in (os.getpid(), os.getppid()):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False        # dono morreu: lock órfão
    except OSError:
        pass                # PermissionError: existe
    return True


def _proxima(semana: str) -> str:
    return semana_de(janela(semana)[1] + 7 * 86400)


def semanas_pendentes(dados: Path, lapides: set[str], agora: float | None = None) -> list[str]:
    """Semanas fechadas sem lápide, da que contém o pareamento até a última fechada, mais antiga primeiro."""
    agora = time.time() if agora is None else agora
    ultima = semana_fechada(agora)
    restauradas = _restauradas(dados)
    lapides = set(lapides) - restauradas       # restaurada = pendente de nova limpeza (I2)
    from app.cursor import Cursor
    pareado = Cursor.carregar(Path(dados) / "cursor.json").pareado_em
    if not pareado:
        base = [] if ultima in lapides else [ultima]
        return sorted(restauradas - set(base)) + base
    atual = _proxima(semana_fechada(pareado))      # a semana cuja janela contém o pareamento
    lista = []
    while janela(atual)[1] <= janela(ultima)[1] and len(lista) < MAX_SEMANAS * 2:
        lista.append(atual)
        atual = _proxima(atual)
    pend = [w for w in lista[-MAX_SEMANAS:] if w not in lapides]
    return sorted(restauradas - set(pend)) + pend      # restauradas fora da janela também entram (mais antigas primeiro)


def _lapides(banco) -> set[str]:
    if banco is None:
        return set()
    try:
        return {l["semana"].split("~", 1)[0] for l in banco.lapides()}      # "W~relimpeza-N" = nova lápide de W
    except sqlite3.Error:
        return set()    # banco somente-leitura de antes das migrações: sem lápides


def fechar(semana: str, *, dados: Path, banco, psql_ro, executor=subprocess.run, enviar_fn=aviso.enviar,
           agora=time.time, refazer: bool = False, porta: str = "8120") -> dict:
    """Relatório (reaproveita o do disco, salvo `refazer`) + aviso. A marca .enviado só nasce se o aviso foi entregue.
    O relatório novo só é aceito se começar por `# Semana` (prompt/semanal.md); senão RelatorioFalhou, nada é gravado."""
    ini_ts, fim_ts = janela(semana)
    if fim_ts > agora():
        return {"status": "semana_aberta", "semana": semana}
    arq = sem.pasta_relatorios(dados) / f"{semana}.md"
    if arq.exists() and not refazer:
        texto = arq.read_text(encoding="utf-8")
        gerado = False
    else:
        texto = sem.relatorio(psql_ro, banco, modelo="opus", executor=executor, semana=semana)
        if not texto.lstrip().startswith("# Semana"):
            raise sem.RelatorioFalhou("relatório não começa por '# Semana'; descartado")
        sem._escrever_0600(arq, texto)
        gerado = True
    n_msgs = sem.contar_janela(psql_ro, ini_ts, fim_ts)
    n_itens = len({c["item_id"] for c in banco.classificacoes(desde=ini_ts, ate=fim_ts - 1, limite=100000)
                   if c.get("item_id")})
    ok = False
    if _enviado_em(dados, semana) is None or gerado:
        libera = sem.libera_em(dados, fim_ts)
        ok = enviar_fn(texto_do_aviso(semana, n_msgs, n_itens, porta, libera, guardar=libera is None))
        if ok:
            sem._escrever_0600(_marca_enviado(dados, semana), str(int(agora())))
    else:
        ok = True  # aviso já tinha ido
    return {"status": "ok" if ok else "aviso_falhou", "semana": semana, "relatorio": str(arq), "gerado": gerado,
            "n_msgs": n_msgs, "n_itens": n_itens, "hash": sem.hash_arquivo(arq)[:12]}


def guardas_da_limpeza(semana: str, *, dados: Path, banco, psql_ro, agora=time.time, forcar: bool = False):
    """Gera, na ORDEM em que bloqueiam, (nome, bloqueia, resultado). `apagar` para na primeira que bloqueia; `simular`
    lista todas. `forcar` pula só a espera de 24 h e a exigência do .enviado; NÃO vence o prazo de Ajustes, SEGURA
    nem as não lidas."""
    dados = Path(dados)
    ini_ts, fim_ts = janela(semana)
    lapide = (banco is not None and semana in _lapides(banco)
              and not marca_restaurada(dados, semana).exists())      # restaurada: a lápide não vale mais (I2)
    yield "ja_apagada", lapide, {"status": "ja_apagada", "semana": semana}
    yield "semana_aberta", fim_ts > agora(), {"status": "semana_aberta", "semana": semana}
    # pedido 67: o prazo escolhido em Ajustes. Nem `forcar` passa por cima (como SEGURA): guardar mais tempo tem volta,
    # apagar não tem.
    libera = sem.libera_em(dados, fim_ts)
    yield "no_prazo", libera is None or libera > agora(), {"status": "no_prazo", "semana": semana, "prazo_dias": sem.prazo_dias(dados),
                                         "libera_em": libera}
    yield "adiada_segura", segura(dados, semana, agora()), {"status": "adiada_segura", "semana": semana}
    arq = dados / "relatorios" / f"{semana}.md"
    yield "sem_relatorio", not arq.exists(), {"status": "sem_relatorio", "semana": semana}
    enviado = _enviado_em(dados, semana)
    if not forcar:
        yield "sem_aviso", enviado is None, {"status": "sem_aviso", "semana": semana}
        idade = agora() - enviado if enviado is not None else None
        yield ("aguardando_24h", idade is not None and idade < ESPERA_APAGAR_S,
               {"status": "aguardando_24h", "semana": semana,
                "faltam_s": int(ESPERA_APAGAR_S - idade) if idade is not None else None})
    from app.cursor import Cursor
    ultimo = Cursor.carregar(dados / "cursor.json").ultimo_seq
    linhas = psql_ro('SELECT count(*) FROM vigia.fila f JOIN public."Message" m ON m.id = f.message_id '
                     f'WHERE f.seq > {int(ultimo)} AND {sem._onde(ini_ts, fim_ts, "m")}')
    nao_lidas = int(linhas[0][0]) if linhas and linhas[0] else 0
    yield "ha_nao_lidas", nao_lidas > 0, {"status": "ha_nao_lidas", "semana": semana, "nao_lidas": nao_lidas}


def apagar(semana: str, *, dados: Path, banco, psql_ro, psql_limpeza, agora=time.time, forcar: bool = False,
           enviar_fn=aviso.enviar) -> dict:
    """Limpeza da semana, com todas as guardas (ver guardas_da_limpeza)."""
    dados = Path(dados)
    ini_ts, fim_ts = janela(semana)
    for _nome, bloqueia, resultado in guardas_da_limpeza(semana, dados=dados, banco=banco, psql_ro=psql_ro,
                                                         agora=agora, forcar=forcar):
        if bloqueia:
            return resultado
    arq = dados / "relatorios" / f"{semana}.md"
    marca = marca_restaurada(dados, semana)
    relimpeza = marca.exists()
    r = limpar(psql_limpeza, banco, ini_ts=ini_ts, fim_ts=fim_ts, pasta_caixa=dados / "caixa",
               backup_dir=dados / "backup", semana=semana, hash_relatorio=sem.hash_arquivo(arq), agora=agora,
               relimpeza=relimpeza)
    if relimpeza:
        try:
            marca.unlink()      # limpeza nova provada e lápide nova gravada: a semana volta a ser histórico vazio
        except OSError:
            pass
    # D-53 (pedido 43): o histórico que a Evolution gravou no pareamento e ficou antes da janela de leitura nunca é lido;
    # sai aqui, junto da semana mais antiga pendente, com backup e lápide próprios ("anteriores-<semana>"). Não roda com
    # semana restaurada pendente (I2): a releitura dela vem primeiro, na ordem de semanas_perdidas.
    r["anteriores_apagadas"] = 0
    try:
        antigas = sem.contar_janela(psql_ro, 0, ini_ts)
    except VigiaErro as e:
        log.warning("contagem de anteriores falhou (%s); fica para a próxima semana", causa(e))
        antigas = 0
    if antigas and not any(Path(dados).glob("restaurada-*")):
        try:   # a semana já está provada e com lápide; a varredura falhando fica para a próxima, sem derrubar o fechamento
            r2 = limpar(psql_limpeza, banco, ini_ts=0, fim_ts=ini_ts, pasta_caixa=dados / "caixa",
                        backup_dir=dados / "backup", semana=f"anteriores-{semana}", hash_relatorio=sem.hash_arquivo(arq),
                        agora=agora)
            r["anteriores_apagadas"] = r2["n_msgs"]
        except VigiaErro as e:
            log.warning("varredura dos anteriores falhou (%s); fica para a próxima semana", causa(e))
    extra = f" · {r['anteriores_apagadas']} anteriores à janela" if r["anteriores_apagadas"] else ""
    try:
        enviar_fn(f"Semana {semana}: cru apagado · {r['n_msgs']} msgs{extra} · lápide gravada")
    except Exception as e:  # o aviso final é cortesia; a limpeza já está provada
        log.warning("aviso final não enviado (%s)", causa(e))
    return {"status": "apagada", **r}


def simular(semana: str, *, dados: Path, banco, psql_ro, apagando: bool, agora=time.time, forcar: bool = False) -> list[str]:
    """--dry-run: descreve o que faria. Só SELECT e leitura de arquivo; nenhum DELETE, nenhum claude, nenhum envio,
    nenhuma escrita (o SQLite vem em mode=ro). As guardas saem na mesma ordem do `apagar`."""
    dados = Path(dados)
    ini_ts, fim_ts = janela(semana)
    L = [f"semana {semana}: janela {time.strftime('%Y-%m-%d %H:%M', time.localtime(ini_ts))} a "
         f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(fim_ts))}"
         + ("" if fim_ts <= agora() else " (ainda aberta: um run real recusaria)")]
    try:
        L.append(f"mensagens do cru na janela: {sem.contar_janela(psql_ro, ini_ts, fim_ts)}")
    except VigiaErro as e:
        L.append(f"mensagens do cru na janela: indisponível ({e})")
    try:
        n_cl = len(banco.classificacoes(desde=ini_ts, ate=fim_ts - 1, limite=100000)) if banco else 0
    except sqlite3.Error:
        n_cl = 0
    L.append(f"classificações da semana (ficam): {n_cl}")
    if not apagando:
        existe = (dados / "relatorios" / f"{semana}.md").exists()
        L.append("faria: " + ("reaproveitar o relatório do disco" if existe else "gerar relatório com claude -p (opus, só Read)")
                 + " e enviar o aviso (contador e link)")
        return L
    pastas = sem.lotes_da_semana(dados / "caixa", ini_ts, fim_ts)
    L.append("apagaria do cru: tudo da janela via SELECT vigia.limpar_janela, depois a prova de count=0; backup em dados/backup")
    L.append(f"apagaria da caixa: {len(pastas)} pasta(s) de lote" + "".join(f"\n  {p.name}" for p in pastas)
             + " (e o cache dados/caixa/.midia dos ids da semana e dados/falhas.json)")
    bloqueou = None
    try:
        for nome, bloqueia, resultado in guardas_da_limpeza(semana, dados=dados, banco=banco, psql_ro=psql_ro,
                                                            agora=agora, forcar=forcar):
            L.append(f"guarda {nome}: " + ("BLOQUEIA" if bloqueia else "livre"))
            if bloqueia and bloqueou is None:
                bloqueou = resultado["status"]
    except VigiaErro as e:
        L.append(f"guarda ha_nao_lidas: indisponível ({e})")
    L.append("resultado de um run real agora: " + (bloqueou or "apagaria"))
    return L


# ---------------------------------------------------------------------------
# Restauração (RUNBOOK): devolve um backup .jsonl ao Postgres
# ---------------------------------------------------------------------------

def _linhas_do_backup(arquivo: Path) -> list[str]:
    """Linhas não vazias do .jsonl, todas validadas como objeto JSON com `id`. Erro = VigiaErro sem ecoar conteúdo."""
    arquivo = Path(arquivo)
    if not arquivo.is_file():
        raise VigiaErro("arquivo de backup inexistente ou não é arquivo comum")
    saida = []
    try:
        with open(arquivo, "r", encoding="utf-8") as f:
            for n, bruto in enumerate(f, 1):
                linha = bruto.strip()
                if not linha:
                    continue
                try:
                    obj = json.loads(linha)
                except ValueError:
                    raise VigiaErro(f"linha {n} do backup não é JSON; nada foi restaurado") from None
                if not isinstance(obj, dict) or not isinstance(obj.get("id"), str):
                    raise VigiaErro(f"linha {n} do backup não é uma mensagem (sem id); nada foi restaurado")
                saida.append(linha)
    except (OSError, UnicodeDecodeError) as e:
        raise VigiaErro(f"backup ilegível ({type(e).__name__})") from None
    return saida


def _sql_restaurar(linhas: list[str]) -> str:
    corpo = "[" + ",".join(linhas) + "]"
    n = 0
    tag = "$vigia_r$"
    while tag in corpo:
        n += 1
        tag = f"$vigia_r{n}$"
    return ('INSERT INTO public."Message" SELECT * FROM json_populate_recordset(NULL::public."Message", '
            f'{tag}{corpo}{tag}::json) ON CONFLICT (id) DO NOTHING')


_NOME_BACKUP = re.compile(r"^(\d{4}-W\d{2})-\d+\.jsonl$")


def _semana_do_backup(arquivo: Path, linhas: list[str]) -> str | None:
    """Semana pelo nome `<semana>-<epoch>.jsonl`; senão pelo primeiro messageTimestamp (segundos) das linhas."""
    m = _NOME_BACKUP.match(Path(arquivo).name)
    if m:
        return m.group(1)
    for linha in linhas:
        ts = json.loads(linha).get("messageTimestamp")
        if isinstance(ts, str) and ts.isdigit():
            ts = int(ts)
        if isinstance(ts, (int, float)) and not isinstance(ts, bool) and 0 < ts < 10**11:
            w = semana_de(int(ts))
            return _proxima(w) if janela(w)[1] <= ts else w      # a janela que CONTÉM ts
    return None


def _gravar_marca_restaurada(dados: Path, semana: str) -> None:
    dados = Path(dados)
    dados.mkdir(parents=True, exist_ok=True, mode=0o700)
    sem._escrever_0600(marca_restaurada(dados, semana), str(int(time.time())))


def restaurar(arquivo: Path, *, psql_sql, dry_run: bool = False, dados: Path | None = None) -> dict:
    """INSERT ... json_populate_record ... ON CONFLICT (id) DO NOTHING como superusuário `evolution` (bin/evolution.sh sql).
    Idempotente: o trigger reenfileira em vigia.fila e o coletor relê. Lotes de até 200 linhas / 96 KiB (bytes) por chamada,
    SQL pelo stdin (`evolution.sh sql-stdin`). Com `dados`, grava dados/restaurada-<semana> ANTES de inserir: a semana volta a
    ser pendente de limpeza mesmo com lápide (I2); a marca sai quando a nova limpeza é provada
    (json_populate_recordset = a mesma função, em vetor). --dry-run só conta as linhas (valida o JSON; não chama o Postgres)."""
    linhas = _linhas_do_backup(arquivo)
    if dry_run:
        return {"status": "dry_run", "arquivo": Path(arquivo).name, "linhas": len(linhas)}
    if dados is not None:
        semana_bk = _semana_do_backup(arquivo, linhas)
        if semana_bk:
            _gravar_marca_restaurada(dados, semana_bk)
    inseridas, conhecidas, lote, bytes_lote = 0, True, [], 0

    def descarrega():
        nonlocal inseridas, conhecidas, lote, bytes_lote
        if not lote:
            return
        resp = psql_sql(_sql_restaurar(lote))
        m = next((re.match(r"INSERT 0 (\d+)$", c.strip()) for l in resp for c in l if re.match(r"INSERT 0 (\d+)$", c.strip())), None)
        if m:
            inseridas += int(m.group(1))
        else:
            conhecidas = False
        lote, bytes_lote = [], 0

    for linha in linhas:
        if lote and (len(lote) >= LOTE_RESTAURAR_LINHAS or bytes_lote + len(linha.encode("utf-8")) > LOTE_RESTAURAR_BYTES):
            descarrega()
        lote.append(linha)
        bytes_lote += len(linha.encode("utf-8")) + 1
    descarrega()
    return {"status": "restaurado", "arquivo": Path(arquivo).name, "linhas": len(linhas),
            "inseridas": inseridas if conhecidas else "desconhecido", "ja_existiam": (len(linhas) - inseridas) if conhecidas else "desconhecido"}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _argumentos(argv):
    p = argparse.ArgumentParser(prog="fechar-semana.py", description="Fechamento semanal do Greg (relatório, aviso, limpeza).")
    p.add_argument("--semana", help="AAAA-Www (padrão: todas as fechadas sem lápide desde o pareamento)")
    p.add_argument("--dry-run", action="store_true", help="só lista o que faria; não chama claude, não envia, não apaga")
    p.add_argument("--executar-limpeza", "--apagar", dest="apagar", action="store_true",
                   help="apaga o cru da semana (só se o aviso tem >= 24 h, sem SEGURA e sem não lidas)")
    p.add_argument("--forcar", action="store_true",
                   help="com --apagar: pula a espera de 24 h e o .enviado (não vence SEGURA nem não lidas)")
    p.add_argument("--refazer", action="store_true", help="gera o relatório de novo mesmo que já exista")
    p.add_argument("--segurar", nargs="?", const=7, type=int, metavar="DIAS", help="adia a limpeza da semana (padrão 7 dias)")
    p.add_argument("--restaurar", metavar="ARQUIVO.jsonl",
                   help="devolve ao Postgres as mensagens de um backup (ON CONFLICT DO NOTHING); com --dry-run só conta")
    return p.parse_args(argv)


def _codigo(r: dict) -> int:
    if r["status"] in _CODIGO_2:
        return 2
    if r["status"] in _CODIGO_1:
        return 1
    return 0


def _imprimir(r: dict) -> None:
    print("fechar-semana: " + " ".join(f"{k}={v}" for k, v in r.items()))


def percorrer(semanas, *, apagar_modo: bool, explicito: bool, forcar: bool, refazer: bool, dados: Path, banco, psql_ro,
              psql_limpeza, porta: str = "8120", executor=subprocess.run, enviar_fn=aviso.enviar, agora=time.time,
              saida=_imprimir) -> int:
    """Roda cada semana, da mais antiga para a mais nova. Status (aguardando_24h, adiada_segura, ha_nao_lidas, ...) NÃO
    interrompem: seguem para a próxima. Exceção (Postgres fora, limpeza incompleta, relatório inválido) interrompe a
    rodada e sobe para o main (a mais antiga precisa fechar antes das outras). Devolve o pior código dos status."""
    codigo = 0
    for w in semanas:
        if apagar_modo:
            r = apagar(w, dados=dados, banco=banco, psql_ro=psql_ro, psql_limpeza=psql_limpeza, forcar=forcar,
                       agora=agora, enviar_fn=enviar_fn)
        elif not explicito and not refazer and _enviado_em(dados, w) is not None:
            r = {"status": "ja_avisada", "semana": w}      # semana perdida já coberta: só falta a limpeza
        else:
            r = fechar(w, dados=dados, banco=banco, psql_ro=psql_ro, refazer=refazer, porta=porta, executor=executor,
                       enviar_fn=enviar_fn, agora=agora)
        saida(r)
        codigo = max(codigo, _codigo(r))
    return codigo


def main(argv=None) -> int:
    args = _argumentos(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", stream=sys.stderr)
    dados = Path(os.environ.get("VIGIA_DADOS") or RAIZ / "dados")
    env = ler_env(Path(os.environ.get("VIGIA_ENV") or RAIZ / ".env"))
    for chave in ("DONO_WHATSAPP", "AVISO_INSTANCIA"):     # destino e instância do aviso: só do .env, nunca do código
        if env.get(chave):
            os.environ.setdefault(chave, env[chave])
    porta = os.environ.get("VIGIA_PORT") or env.get("VIGIA_PORT") or "8120"
    script = Path(os.environ.get("VIGIA_EVOLUTION_SH") or RAIZ / "bin" / "evolution.sh")
    if args.restaurar:
        try:
            _imprimir(restaurar(Path(args.restaurar), psql_sql=psql_via_script("sql-stdin", script), dry_run=args.dry_run,
                              dados=dados))
            return 0
        except PostgresIndisponivel as e:
            print(f"fechar-semana: restauração não terminou ({e}); é idempotente, rode de novo", file=sys.stderr)
            return 2
        except VigiaErro as e:
            print(f"fechar-semana: falhou ({e})", file=sys.stderr)
            return 1
    if args.semana:
        try:
            janela(args.semana)
        except ValueError:
            print("fechar-semana: --semana deve ser AAAA-Www", file=sys.stderr)
            return 2
    psql_ro = psql_via_script("psql-ro-stdin", script)
    psql_limpeza = psql_via_script("psql-limpeza-stdin", script)
    try:
        if args.dry_run:
            banco = None
            if (dados / "vigia.db").exists():
                from app.banco import Banco
                banco = Banco(str(dados / "vigia.db"), somente_leitura=True)
            semanas = [args.semana] if args.semana else semanas_pendentes(dados, _lapides(banco))
            saida = ["dry-run (nada será alterado)"]
            for w in semanas:
                saida += simular(w, dados=dados, banco=banco, psql_ro=psql_ro, apagando=args.apagar, forcar=args.forcar)
            if not semanas:
                saida.append("nenhuma semana fechada sem lápide")
            print("\n".join(saida))
            return 0
        from app.banco import Banco
        banco = Banco(str(dados / "vigia.db"))
        semana_alvo = args.semana or semana_fechada()
        if args.segurar is not None:
            segurar(banco, semana_alvo, dias=args.segurar)
            print(f"fechar-semana: limpeza de {semana_alvo} adiada por {args.segurar} dia(s)")
            return 0
        if lock_ocupado(dados):
            print("fechar-semana: adiado (dados/vigia.lock recente de outro processo); roda de novo no próximo agendamento")
            return 0
        semanas = [args.semana] if args.semana else semanas_pendentes(dados, _lapides(banco))
        if not semanas:
            print("fechar-semana: nada pendente (todas as semanas fechadas têm lápide)")
            return 0
        return percorrer(semanas, apagar_modo=args.apagar, explicito=bool(args.semana), forcar=args.forcar,
                         refazer=args.refazer, dados=dados, banco=banco, psql_ro=psql_ro, psql_limpeza=psql_limpeza,
                         porta=porta)
    except PostgresIndisponivel as e:
        print(f"fechar-semana: não rodou ({e})", file=sys.stderr)
        return 2
    except LimpezaIncompleta as e:
        print(f"fechar-semana: LIMPEZA INCOMPLETA ({e})", file=sys.stderr)
        return 1
    except VigiaErro as e:
        print(f"fechar-semana: falhou ({e})", file=sys.stderr)
        return 1
    except Exception as e:  # nunca imprime a mensagem: pode conter trecho de conversa
        print(f"fechar-semana: falhou ({type(e).__name__})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
