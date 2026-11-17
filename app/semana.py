"""Fechamento semanal do Greg: janela, relatório (Claude/Opus sobre CLASSIFICAÇÕES), limpeza do cru com prova e lápide.
Quem chama: bin/fechar-semana.py e testes.
O que não faz: não abre conexão Postgres (recebe `psql` injetado: psql_ro p/ leitura, psql_limpeza p/ DELETE), não manda
aviso (app/aviso.py), não lê texto cru de mensagem (o relatório nasce dos resumos já classificados no SQLite),
não loga texto de conversa (só semana, contagens e códigos) e não apaga classificações nem itens do Frentes (P-2).
"""
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import date, datetime, time as hora, timedelta
from pathlib import Path

from app import aviso, lotes
from app.erros import LimpezaIncompleta, VigiaErro

log = logging.getLogger("vigia.semana")

RAIZ = Path(__file__).resolve().parents[1]
HORA_FECHAMENTO = 20            # a semana fecha no domingo 20:00 local (mesmo horário do LaunchAgent semanal)
RETENCAO_BACKUP_S = 7 * 86400   # D-8: backup da janela vive até a lápide seguinte
ESPERA_APAGAR_S = 24 * 3600 - 1800   # "24 h" depois do aviso, com 30 min de tolerância: o aviso sai alguns minutos
                                     # depois de dom 20:00 (Opus gera o relatório) e o LaunchAgent roda seg 20:00 em ponto
_SEMANA = re.compile(r"^(\d{4})-W(\d{2})$")
_LOTE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})(\d{2})(?:-\d+)?$")   # sufixo -N: lote repetido no mesmo minuto
LIMITE_UNIDADE_TS = 10 ** 11    # messageTimestamp em segundos < 1e11; milissegundos (>= 1e11) = unidade errada (D-trava)


class RelatorioFalhou(VigiaErro):
    """O claude -p não devolveu um relatório utilizável."""


class BackupDivergente(LimpezaIncompleta):
    """O backup da janela não tem tantas linhas quanto o count do Postgres: NADA foi apagado e a lápide não foi gravada."""


# ---------------------------------------------------------------------------
# Janela
# ---------------------------------------------------------------------------

def _fim_da_semana(semana: str) -> datetime:
    m = _SEMANA.match(str(semana))
    if not m:
        raise ValueError("semana deve ser AAAA-Www (ISO)")
    domingo = date.fromisocalendar(int(m.group(1)), int(m.group(2)), 7)
    return datetime.combine(domingo, hora(HORA_FECHAMENTO))


def janela(semana: str) -> tuple[int, int]:
    """(ini_ts, fim_ts) em epoch: da semana ISO AAAA-Www, de domingo 20:00 (anterior) a domingo 20:00, hora local.
    [ini, fim): mensagem em fim exato pertence à semana seguinte."""
    fim = _fim_da_semana(semana)
    return int((fim - timedelta(days=7)).timestamp()), int(fim.timestamp())


def semana_de(fim_ts: int) -> str:
    """Rótulo AAAA-Www da semana cuja janela termina em fim_ts."""
    iso = datetime.fromtimestamp(fim_ts).date().isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def semana_fechada(agora: float | None = None) -> str:
    """A semana mais recente cujo fim (dom 20:00) já passou."""
    agora = time.time() if agora is None else agora
    hoje = datetime.fromtimestamp(agora).date()
    iso = hoje.isocalendar()
    atual = f"{iso[0]}-W{iso[1]:02d}"
    if janela(atual)[1] <= agora:
        return atual
    iso = (hoje - timedelta(days=7)).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def prazo_dias(dados: Path) -> int:
    """Dias que as mensagens ficam guardadas antes da limpeza (pedido 67), escolhidos em Ajustes
    (dados/preferencias.json, `retencao_dias`). Sem arquivo ou sem a chave: 7, o comportamento de sempre. Falha
    FECHADA para o lado que não apaga: arquivo ilegível ou valor fora da lista conta como NÃO APAGAR (0, pedido 74),
    porque apagar não tem volta e guardar tem. 0 = a pessoa escolheu não apagar."""
    from app import preferencias as pf
    maior = pf.NAO_APAGAR
    arq = Path(dados) / "preferencias.json"
    if not arq.exists():
        return pf.PADRAO["retencao_dias"]
    try:
        bruto = json.loads(arq.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return maior
    if not isinstance(bruto, dict):
        return maior
    if "retencao_dias" not in bruto:
        return pf.PADRAO["retencao_dias"]
    try:
        dias = int(str(bruto["retencao_dias"]).strip())
    except ValueError:
        return maior
    return dias if dias in pf.RETENCAO_DIAS else maior


def libera_em(dados: Path, fim_ts: int) -> int:
    """Epoch a partir do qual a semana que termina em fim_ts pode ser apagada pelo prazo: com 7 dias é o próprio fim
    (vale só a espera de 24 h do aviso); cada dia a mais de prazo empurra um dia. None = não apagar (pedido 74)."""
    dias = prazo_dias(dados)
    return None if dias == 0 else int(fim_ts) + (dias - 7) * 86400


def _onde(ini_ts: int, fim_ts: int, alias: str = "") -> str:
    p = f"{alias}." if alias else ""
    return f'{p}"messageTimestamp" >= {int(ini_ts)} AND {p}"messageTimestamp" < {int(fim_ts)}'


def contar_janela(psql, ini_ts: int, fim_ts: int) -> int:
    linhas = psql(f'SELECT count(*) FROM public."Message" WHERE {_onde(ini_ts, fim_ts)}')
    return int(linhas[0][0]) if linhas and linhas[0] else 0


# ---------------------------------------------------------------------------
# "Segura": adia a limpeza
# ---------------------------------------------------------------------------

def _dados_do(banco) -> Path:
    return Path(banco.caminho).resolve().parent


def segurar(banco, semana: str, dias: int = 7) -> None:
    """Adia a limpeza da semana por `dias`: grava dados/segurar-<semana> com o epoch até quando vale. Não apaga nada."""
    _fim_da_semana(semana)
    dados = _dados_do(banco)
    dados.mkdir(parents=True, exist_ok=True, mode=0o700)
    _escrever_0600(dados / f"segurar-{semana}", str(int(time.time()) + int(dias) * 86400))


def segura(dados: Path, semana: str, agora: float | None = None) -> bool:
    """True se a limpeza está adiada: dados/SEGURA (global) ou dados/segurar-<semana>. Fail-safe (F4(8) item 1): arquivo
    por semana que existe mas está vazio, ilegível ou sem epoch parseável CONTA COMO SEGURADO; só um epoch válido já
    vencido libera."""
    agora = time.time() if agora is None else agora
    if (Path(dados) / "SEGURA").exists():
        return True
    marca = Path(dados) / f"segurar-{semana}"
    if not marca.exists():
        return False
    try:
        return float(marca.read_text(encoding="utf-8").strip()) > agora
    except (OSError, ValueError):
        return True


# ---------------------------------------------------------------------------
# Arquivos
# ---------------------------------------------------------------------------

def _escrever_0600(caminho: Path, texto: str) -> None:
    fd = os.open(str(caminho), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(texto)
    os.chmod(caminho, 0o600)


def pasta_relatorios(dados: Path) -> Path:
    p = Path(dados) / "relatorios"
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    return p


def hash_texto(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def hash_arquivo(caminho: Path) -> str:
    """sha256 dos BYTES do arquivo (hash da lápide = o relatório exatamente como está no disco)."""
    return hashlib.sha256(Path(caminho).read_bytes()).hexdigest()


def lotes_da_semana(pasta_caixa: Path, ini_ts: int, fim_ts: int) -> list[Path]:
    """Pastas dados/caixa/AAAA-MM-DD_HHMM[-N] cujo instante cai em [ini, fim)."""
    achados = []
    try:
        filhos = sorted(Path(pasta_caixa).iterdir())
    except OSError:
        return achados
    for p in filhos:
        if p.name.startswith("."):      # .midia (cache de imagem) nunca é lote
            continue
        m = _LOTE.match(p.name)
        if not m or p.is_symlink() or not p.is_dir():
            continue
        try:
            ts = int(datetime(*(int(x) for x in m.groups())).timestamp())
        except ValueError:
            continue
        if ini_ts <= ts < fim_ts:
            achados.append(p)
    return achados


# ---------------------------------------------------------------------------
# Relatório
# ---------------------------------------------------------------------------

def _entrada_json(banco, semana: str, ini_ts: int, fim_ts: int, n_msgs: int) -> dict:
    cl = banco.classificacoes(desde=ini_ts, ate=fim_ts - 1, limite=100000)
    cl.reverse()
    return {
        "semana": semana,
        "periodo": [datetime.fromtimestamp(ini_ts).isoformat(" ", "minutes"),
                    datetime.fromtimestamp(fim_ts).isoformat(" ", "minutes")],
        "mensagens_recebidas": n_msgs,
        "classificacoes_total": len(cl),
        "itens_no_frentes": sum(1 for c in cl if c.get("item_id")),
        "classificacoes": [{
            "ts": datetime.fromtimestamp(c["ts"]).isoformat(" ", "minutes"), "contato": c.get("contato"),
            "classe": c.get("classe"), "resumo": c.get("resumo"), "confianca": c.get("confianca"),
            "prazo": c.get("prazo"), "item_id": c.get("item_id"), "decisao": c.get("decisao"),
        } for c in cl],
    }


def montar_comando_relatorio(entrada: Path, *, modelo: str, prompt: Path, claude: str) -> list[str]:
    """claude -p só com Read, confinado à pasta do JSON temporário (D-27, F4(8) item 6): `Read(//<pasta>/**)`, segredos
    conhecidos negados, sem sessão persistida, sem MCP, sem prompt de permissão."""
    entrada = Path(entrada)
    pasta = entrada.parent.resolve()
    msg = (f"Escreva o relatório semanal. A entrada é o arquivo {entrada}. "
           "Siga as instruções do sistema e responda só com o Markdown.")
    return [claude, "-p", "--model", modelo, "--tools", "Read",
            "--allowedTools", f"Read(/{pasta}/**)",
            "--disallowedTools", "Write,Edit,Bash,WebFetch,WebSearch,Agent", "Read(//**/.env*)", "Read(**/.env*)",
            "Read(~/.ssh/**)", *aviso.leitura_negada_do_cofre(), "Read(~/.frentes.env)", "Read(~/.vigia-envio.env)",
            "--permission-mode", "default",
            "--no-session-persistence", "--strict-mcp-config", "--restricted", "--permission-prompts", "none",
            "--add-dir", str(pasta),
            "--system-prompt-file", str(Path(prompt).resolve()),
            "--output-format", "json", msg]


def _texto_do_resultado(saida) -> str:
    if isinstance(saida, list):
        saida = next((x for x in reversed(saida) if isinstance(x, dict) and x.get("type") == "result"), None)
    if not isinstance(saida, dict):
        raise RelatorioFalhou("saída do claude sem objeto de resultado")
    if saida.get("is_error") or str(saida.get("subtype") or "success").startswith("error"):
        raise RelatorioFalhou(f"claude respondeu com erro (subtype={saida.get('subtype')})")
    texto = saida.get("result")
    if not isinstance(texto, str) or not texto.strip():
        raise RelatorioFalhou("campo result ausente ou vazio na saída do claude")
    return texto.strip() + "\n"


def relatorio(psql_ro, banco, *, modelo: str = "opus", executor=subprocess.run, semana: str | None = None,
              prompt: Path | None = None, timeout: float = 900.0, claude: str | None = None) -> str:
    """Markdown do relatório da semana. O Claude lê SÓ um arquivo temporário com as classificações (resumos) do SQLite
    e a contagem de mensagens do Postgres; nunca o texto cru. O temporário é apagado ao fim."""
    semana = semana or semana_fechada()
    ini_ts, fim_ts = janela(semana)
    n_msgs = contar_janela(psql_ro, ini_ts, fim_ts)
    entrada_dir = Path(tempfile.mkdtemp(prefix="vigia-semanal-", dir=_dados_do(banco)))
    try:
        os.chmod(entrada_dir, 0o700)
        entrada = entrada_dir / "classificacoes.json"
        _escrever_0600(entrada, json.dumps(_entrada_json(banco, semana, ini_ts, fim_ts, n_msgs),
                                           ensure_ascii=False, indent=1))
        from app import leitor
        cfg_ia = leitor.configuracao(None, _dados_do(banco))
        if cfg_ia["motor"] == "api":      # pedido 68: outra IA escreve o relatório, sem ferramenta nenhuma
            from app import leitor_api
            try:
                return leitor_api.relatorio(entrada, cfg=cfg_ia, chave=leitor.chave(_dados_do(banco)),
                                            dados=_dados_do(banco), prompt=prompt or RAIZ / "prompts" / "semanal.md",
                                            timeout=timeout)
            except lotes.ErroClaude as e:
                raise RelatorioFalhou(str(e)) from None
        cmd = montar_comando_relatorio(entrada, modelo=modelo, prompt=prompt or RAIZ / "prompts" / "semanal.md",
                                       claude=claude or lotes.achar_claude())
        env = lotes.ambiente_do_claude(entrada_dir.resolve(), cmd[0])   # lista branca (D-27)
        try:
            proc = lotes.executar_em_grupo(executor, cmd, capture_output=True, text=True, timeout=timeout,
                                           stdin=subprocess.DEVNULL, cwd=str(entrada_dir), env=env)
        except subprocess.TimeoutExpired:
            raise RelatorioFalhou(f"claude excedeu {int(timeout)} s") from None
        except OSError as e:
            raise RelatorioFalhou(f"claude não executou ({type(e).__name__})") from None
        bruto = getattr(proc, "stdout", "") or ""
        try:
            saida = json.loads(bruto)
        except ValueError:
            raise RelatorioFalhou(f"claude saiu com código {getattr(proc, 'returncode', '?')} sem JSON na saída "
                                  f"({len(bruto)} bytes)") from None
        return _texto_do_resultado(saida)
    finally:
        shutil.rmtree(entrada_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Limpeza
# ---------------------------------------------------------------------------

def _evolution_sh() -> Path:
    return Path(os.environ.get("VIGIA_EVOLUTION_SH") or RAIZ / "bin" / "evolution.sh")


def _backup(ini_ts: int, fim_ts: int, destino: Path, timeout: float = 600.0) -> None:
    """Despeja a janela de "Message" (uma linha JSON por mensagem) em `destino` (0600) pelo papel vigia_limpeza."""
    query = f'SELECT row_to_json(m) FROM public."Message" m WHERE {_onde(ini_ts, fim_ts, "m")} ORDER BY m."messageTimestamp"'
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(["/opt/homebrew/bin", "/usr/local/bin", env.get("PATH") or "/usr/bin:/bin"])
    fd = os.open(str(destino), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as saida:
            try:
                # A query vai pelo stdin (psql-limpeza-stdin): janela de datas fora do argv/ps.
                r = subprocess.run(["/bin/bash", str(_evolution_sh()), "psql-limpeza-stdin"], stdout=saida,
                                   stderr=subprocess.DEVNULL, input=query.encode("utf-8"), timeout=timeout, env=env)
            except (OSError, subprocess.TimeoutExpired) as e:
                raise VigiaErro(f"backup da janela não executou ({type(e).__name__})") from None
        if r.returncode != 0:
            raise VigiaErro(f"backup da janela saiu com código {r.returncode}; nada foi apagado")
    except BaseException:
        try:
            os.unlink(destino)
        except OSError:
            pass
        raise
    os.chmod(destino, 0o600)


def _podar_backups_exceto(backup_dir: Path, manter: Path) -> int:
    """D-8 literal: depois da lápide, apaga todo `*.jsonl` de dados/backup/ exceto o da semana que acabou de ser limpa."""
    n = 0
    for p in Path(backup_dir).glob("*.jsonl"):
        try:
            if p.resolve() != Path(manter).resolve():
                p.unlink()
                n += 1
        except OSError:
            pass
    return n


def _inteiro(linhas, i: int = 0) -> int:
    try:
        return int(linhas[0][i])
    except (IndexError, ValueError, TypeError):
        raise LimpezaIncompleta("resposta inesperada do Postgres ao contar; lápide não gravada") from None


def _ler_backup(backup: Path) -> tuple[int, set[str]]:
    """(linhas não vazias, ids das mensagens) do .jsonl do backup. Linha que não é JSON conta como linha, sem id."""
    n, ids = 0, set()
    with open(backup, "rb") as f:
        for bruto in f:
            if not bruto.strip():
                continue
            n += 1
            try:
                obj = json.loads(bruto)
            except ValueError:
                continue
            if isinstance(obj, dict) and isinstance(obj.get("id"), str):
                ids.add(obj["id"])
    return n, ids


def _checar_unidade(psql_limpeza) -> None:
    """Trava de unidade (F4(8) item 9): messageTimestamp em segundos. Qualquer valor >= 1e11 indica milissegundos e a
    janela [ini, fim) deixaria mensagem para trás sem avisar."""
    linhas = psql_limpeza('SELECT max("messageTimestamp") FROM public."Message"')
    bruto = (linhas[0][0] if linhas and linhas[0] else "").strip()
    if not bruto:
        return          # tabela vazia
    try:
        maximo = int(bruto)
    except ValueError:
        raise VigiaErro("resposta inesperada do Postgres ao checar a unidade de messageTimestamp; nada foi apagado") from None
    if maximo >= LIMITE_UNIDADE_TS:
        raise VigiaErro("messageTimestamp fora de segundos (>= 1e11, provável milissegundo); nada foi apagado")


def limpar(psql_limpeza, banco, *, ini_ts: int, fim_ts: int, pasta_caixa: Path, backup_dir: Path,
           semana: str | None = None, hash_relatorio: str | None = None, agora=time.time,
           relimpeza: bool = False) -> dict:
    """trava de unidade -> backup da janela (linhas == count) -> SELECT vigia.limpar_janela(ini, fim) (D-25: o papel
    vigia_limpeza não tem DELETE) -> prova count==0 -> apaga caixa e cache de mídia da semana -> lápide -> poda backups.
    Erro do Postgres DEPOIS do backup = LimpezaIncompleta (exit 1), nunca "não rodou". Qualquer resto levanta
    LimpezaIncompleta e NENHUMA lápide é gravada."""
    ini_ts, fim_ts = int(ini_ts), int(fim_ts)
    semana = semana or semana_de(fim_ts)
    backup_dir = Path(backup_dir)
    pasta_caixa = Path(pasta_caixa)
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    n_antes = _inteiro(psql_limpeza(f'SELECT count(*) FROM public."Message" WHERE {_onde(ini_ts, fim_ts)}'))
    _checar_unidade(psql_limpeza)
    backup = backup_dir / f"{semana}-{int(agora())}.jsonl"
    _backup(ini_ts, fim_ts, backup)
    linhas_backup, ids = _ler_backup(backup)
    if linhas_backup != n_antes:
        try:
            os.unlink(backup)
        except OSError:
            pass
        raise BackupDivergente(f"backup com {linhas_backup} linha(s) e o Postgres conta {n_antes}; nada foi apagado")
    onde = _onde(ini_ts, fim_ts)
    try:    # daqui em diante o cru pode estar parcialmente apagado: falha de Postgres NÃO é "não rodou"
        apagadas = _inteiro(psql_limpeza(f"SELECT vigia.limpar_janela({ini_ts}, {fim_ts})"))
        if apagadas > n_antes:
            raise LimpezaIncompleta(f"limpar_janela apagou {apagadas} mensagem(ns) e o backup tem {n_antes}; lápide não gravada")
        restam_msg = _inteiro(psql_limpeza(f'SELECT count(*) FROM public."Message" WHERE {onde}'))
        restam_fila = _inteiro(psql_limpeza(
            'SELECT count(*) FROM vigia.fila f WHERE NOT EXISTS (SELECT 1 FROM public."Message" m WHERE m.id = f.message_id)'))
    except LimpezaIncompleta:
        raise
    except VigiaErro as e:
        raise LimpezaIncompleta(f"Postgres falhou depois do backup ({type(e).__name__}); a limpeza pode estar pela metade; "
                                f"lápide não gravada") from None
    if restam_msg or restam_fila:
        raise LimpezaIncompleta(f"após limpar_janela restam {restam_msg} mensagem(ns) na janela e {restam_fila} linha(s) "
                                f"órfã(s) em vigia.fila; lápide não gravada")
    pastas = lotes_da_semana(pasta_caixa, ini_ts, fim_ts)
    for p in pastas:
        shutil.rmtree(p, ignore_errors=True)
    restam_pastas = [p for p in pastas if p.exists()]
    if restam_pastas:
        raise LimpezaIncompleta(f"{len(restam_pastas)} pasta(s) de lote não puderam ser apagadas; lápide não gravada")
    cache = pasta_caixa / ".midia"
    lotes.apagar_cache(cache, ids)
    restam_midia = [p for i in ids for p in cache.glob(f"{lotes._nome_seguro(i, 'x')}.*")] if cache.is_dir() else []
    if restam_midia:
        raise LimpezaIncompleta(f"{len(restam_midia)} arquivo(s) do cache de mídia não puderam ser apagados; lápide não gravada")
    # D-47: a transcrição é o único texto de mensagem no SQLite e sai com o cru da mesma janela (R-12).
    transcricoes_apagadas = 0
    apagar_transcricoes = getattr(banco, "apagar_transcricoes", None)
    if callable(apagar_transcricoes):
        transcricoes_apagadas = apagar_transcricoes(ini_ts, fim_ts)
        if banco.contar_transcricoes(ini_ts, fim_ts):
            raise LimpezaIncompleta("restam transcrições de áudio na janela; lápide não gravada")
    try:
        (pasta_caixa.parent / "falhas.json").unlink()      # contador de falhas do coletor: só números, mas é da janela
    except FileNotFoundError:
        pass
    except OSError:
        raise LimpezaIncompleta("dados/falhas.json não pôde ser apagado; lápide não gravada") from None
    h = hash_relatorio or hash_texto("")
    gravar = banco.gravar_lapide_relimpeza if relimpeza else banco.gravar_lapide     # relimpeza acrescenta; nunca apaga
    lapide_id = gravar(semana=semana, n_msgs=n_antes, hash_relatorio=h, apagado_em=int(agora()))
    podados = _podar_backups_exceto(backup_dir, backup)
    log.info("semana %s limpa: %d msgs, %d lote(s) de caixa, %d mídia(s), %d transcrição(ões), lápide %s, "
             "%d backup(s) antigo(s) podado(s)", semana, n_antes, len(pastas), len(ids), transcricoes_apagadas,
             lapide_id, podados)
    return {"semana": semana, "n_msgs": n_antes, "apagadas_postgres": apagadas, "lotes_apagados": len(pastas),
            "transcricoes_apagadas": transcricoes_apagadas, "lapide_id": lapide_id, "backup": str(backup)}
