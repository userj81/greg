#!/opt/homebrew/bin/python3
"""Coletor do Greg: um ciclo de leitura incremental (R-3, R-4) do WhatsApp pessoal.
Lê a fila nova no Postgres, filtra, monta um lote em dados/caixa/<lote>/ (uma subpasta por conversa) e chama o
`claude -p` uma vez sobre ele. Só depois de o Claude devolver o JSON final grava lotes.jsonl + SQLite e avança o cursor.
Saída: 0 ok (inclui "sem novidade") · 1 falhou · 2 não rodou (Postgres/Evolution/Frentes fora, sem pareamento).
Quem chama: bin/vigia.sh (LaunchAgent a cada 30 min) e o dono, à mão.
O que não faz: não abre conversa nem marca lida, não envia nada, não lê .env de envio (VIGIA_TOKEN_ENVIO nunca é
carregado), não loga texto de mensagem (só ids e contagens) e, com --dry-run, não escreve nada.
"""
import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from app import leitor, lotes, segredos, transcricao, vinculos, whatsapp  # noqa: E402
from app.erros import causa
from app.erros import (EvolutionIndisponivel, FrentesIndisponivel, NaoAutorizado, VigiaErro)  # noqa: E402

log = logging.getLogger("vigia.coletor")

PADROES = {
    "contexto": {"horas": 48, "minimo": 30},
    "grupos": {"permitidos": []},
    "midia": {"teto_ciclo": 20, "pausa_s": 1.0},
    # Seção opcional (não está no config.toml da F0): modelo do ciclo (D-9), tamanho da fila, tempo do Claude.
    "coletor": {"modelo": "sonnet", "limite_fila": 500, "timeout_claude_s": 900},
    # D-47: transcrição automática de áudio (motor local, tetos por ciclo).
    "transcricao": dict(transcricao.PADROES),
}
_NAO_CARREGAR = {"VIGIA_TOKEN_ENVIO"}  # R-9: o token de envio nunca entra no ambiente do coletor


# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

def carregar_config(caminho: Path) -> dict:
    """config.toml sobre os padrões; arquivo ausente = só padrões."""
    cfg = {k: dict(v) for k, v in PADROES.items()}
    try:
        with open(caminho, "rb") as f:
            lido = tomllib.load(f)
    except FileNotFoundError:
        return cfg
    for secao, valores in lido.items():
        if isinstance(valores, dict):
            cfg.setdefault(secao, {}).update(valores)
    return cfg


def com_conversas_do_sistema(config: dict, do_env: str) -> dict:
    """`[leitura] conversas_do_sistema` = lista do config.toml ∪ VIGIA_CONVERSAS_DO_SISTEMA do .env (jids separados por
    vírgula), sem repetição. O jid do bot é dado pessoal: o config.toml versionado fica vazio e o .env carrega o real
    (pedido 37). Devolve um config novo; não altera o recebido."""
    leitura = dict(config.get("leitura") or {})
    lista = [str(j).strip() for j in (leitura.get("conversas_do_sistema") or []) if str(j).strip()]
    lista += [j.strip() for j in (do_env or "").split(",") if j.strip()]
    leitura["conversas_do_sistema"] = list(dict.fromkeys(lista))
    return {**config, "leitura": leitura}


def ler_env(caminho: Path) -> dict:
    """KEY=VALOR simples (sem executar nada); comentário `# ...` depois de espaço é cortado."""
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
        chave = chave.strip()
        if chave in _NAO_CARREGAR:
            continue
        for i, c in enumerate(valor):
            if c == "#" and (i == 0 or valor[i - 1] in " \t"):
                valor = valor[:i]
                break
        saida[chave] = valor.strip().strip("'\"")
    return saida


# ---------------------------------------------------------------------------
# Peças injetáveis
# ---------------------------------------------------------------------------

def psql_ro_via_script(script: Path, timeout: float = 60.0):
    """psql(query) -> list[list[str]] como vigia_ro via `bin/evolution.sh psql-ro-stdin` (query pelo stdin, nunca em
    argv/ps). Mesma implementação do painel: lotes.psql_via_script."""
    return lotes.psql_via_script(script, "psql-ro-stdin", timeout)


class _Preguicoso:
    """Cria o objeto só no primeiro uso (cliente Evolution / banco SQLite não existem em ciclo sem novidade)."""

    def __init__(self, fabrica):
        self._fabrica = fabrica
        self._obj = None

    def __getattr__(self, nome):
        if self._obj is None:
            self._obj = self._fabrica()
        return getattr(self._obj, nome)


class _PonteEmCache:
    """Frentes consultado uma vez por ciclo: contexto_markdown() e frentes() são idênticos para todas as conversas."""

    def __init__(self, ponte):
        self._ponte = ponte
        self._md = None
        self._frentes = None

    def contexto_markdown(self) -> str:
        if self._md is None:
            self._md = self._ponte.contexto_markdown()
        return self._md

    def frentes(self) -> list[dict]:
        if self._frentes is None:
            self._frentes = self._ponte.frentes()
        return self._frentes


def _vinculos_do_ciclo(banco, ponte, psql, dados: Path) -> dict:
    """D-49: aprende o que o dono moveu no Frentes e renova as sugestões pela agenda. Nunca derruba o ciclo."""
    saida = {"aprendidos": 0, "sugestoes": 0}
    if not callable(getattr(banco, "vinculos", None)):
        return saida
    try:
        saida["aprendidos"] = vinculos.aprender_do_frentes(banco, ponte)
        frentes = ponte.frentes()
        try:
            grupos = json.loads((dados / "grupos.json").read_text(encoding="utf-8")).get("grupos", {})
        except (OSError, ValueError, AttributeError):
            grupos = {}
        nomes = {j: (g or {}).get("subject", "") for j, g in grupos.items()}
        for tipo in ("grupos", "contatos", "outros"):
            for r in whatsapp.agenda(psql, tipo):
                if r["jid"].endswith("@g.us"):   # pedido 65: em grupo a agenda traz nome de participante, não o assunto
                    nomes.setdefault(r["jid"], "")
                else:
                    nomes[r["jid"]] = r["nome"] or nomes.get(r["jid"], "")
        saida["sugestoes"] = vinculos.semear(banco, nomes, frentes)
    except Exception as e:   # agenda ou Frentes fora: o ciclo segue sem vínculo novo
        log.warning("vínculos do ciclo pulados: %s", causa(e))
    return saida


# ---------------------------------------------------------------------------
# Ciclo
# ---------------------------------------------------------------------------

LIMITE_CONVERSAS = 15
LIMITE_MENSAGENS = 150
FALHAS_PARA_REDUZIR = 2


def _criar_pasta_do_lote(caixa: Path, agora: float) -> tuple[str, Path]:
    """Cria (exclusivamente) a pasta do lote: `FileExistsError` -> próximo sufixo. Só o que ESTA execução criou é
    apagado em caso de falha."""
    caixa.mkdir(parents=True, exist_ok=True, mode=0o700)
    base = datetime.fromtimestamp(agora).strftime("%Y-%m-%d_%H%M")
    n = 1
    while True:
        nome = base if n == 1 else f"{base}-{n}"
        try:
            (caixa / nome).mkdir(mode=0o700)
            return nome, caixa / nome
        except FileExistsError:
            n += 1


def _registrar_jsonl(caminho: Path, registro: dict) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(caminho, "a", encoding="utf-8") as f:
        f.write(json.dumps(registro, ensure_ascii=False) + "\n")


def _falhas_da_faixa(dados: Path, ultimo_seq: int) -> int:
    """Falhas seguidas do Claude na faixa que começa em `ultimo_seq` (dados/falhas.json: só números)."""
    try:
        v = json.loads((dados / "falhas.json").read_text(encoding="utf-8"))
        return int(v["n"]) if v.get("de") == ultimo_seq else 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return 0


def _notificar_mac(titulo: str, texto: str) -> None:
    """Notificação do macOS (pedido 62). Textos fixos do código, nunca conteúdo de conversa. No Docker ou sem
    osascript, não faz nada; falha aqui nunca derruba o ciclo."""
    if os.environ.get("VIGIA_CONTAINER") == "1" or os.environ.get("VIGIA_SEM_NOTIFICACAO") == "1" or not shutil.which("osascript"):
        return
    seguro = lambda t: re.sub(r'[^\w .,:;()áàâãéêíóôõúçÁÀÂÃÉÊÍÓÔÕÚÇ-]', "", t)[:200]
    try:        # pedido 78: pelo Greg.app a notificação sai com o nome e o ícone do Greg; osascript é a reserva
        from app import saude
        if saude.notificar_pelo_app(seguro(titulo), seguro(texto)):
            return
    except Exception:  # noqa: BLE001
        pass
    try:
        subprocess.run(["osascript", "-e", f'display notification "{seguro(texto)}" with title "{seguro(titulo)}" sound name "Basso"'],
                       timeout=10, capture_output=True, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


NOTIFICAR = _notificar_mac      # os testes trocam por uma função que só anota


def _anotar_falha(dados: Path, ultimo_seq: int, *, login: bool = False) -> None:
    """Conta a falha do Claude nesta faixa. Login vencido (pedido 62) não conta para reduzir o lote (não é o lote que
    está grande); fica anotado com `motivo` e `desde` para o painel avisar o que fazer."""
    n = _falhas_da_faixa(dados, ultimo_seq) + (0 if login else 1)
    try:
        antes = json.loads((dados / "falhas.json").read_text(encoding="utf-8"))
        desde = int(antes["desde"]) if antes.get("de") == ultimo_seq and antes.get("motivo") == ("login" if login else "") else 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        desde = 0
    try:
        dados.mkdir(parents=True, exist_ok=True, mode=0o700)
        (dados / "falhas.json").write_text(json.dumps({"de": ultimo_seq, "n": n, "motivo": "login" if login else "",
                                                       "desde": desde or int(time.time())}), encoding="utf-8")
    except OSError:
        pass
    # a notificação saiu daqui no lote 2 do pedido 63: quem avisa é a central (app/saude.do_coletor + notificar)


def _limpar_falhas(dados: Path) -> None:
    try:
        (dados / "falhas.json").unlink()
    except OSError:
        pass


def cortar_por_prefixo(fila, *, desde: int, grupos_permitidos: set, max_conversas: int, max_mensagens: int,
                       bloqueados=frozenset()):
    """Percorre a fila em ordem de seq e fecha o lote no primeiro item que estouraria o limite (conversas ou
    mensagens ÚTEIS). Tudo com seq <= seq_ate entra no lote ou foi descartado por filtro: o cursor nunca pula
    mensagem. Devolve (uteis, descartadas, seq_ate, cortou)."""
    uteis, conversas, descartadas, seq_ate, cortou = [], set(), 0, 0, False
    for ln in sorted(fila, key=lambda x: x["seq"]):
        if lotes.filtrar([ln], desde=desde, grupos_permitidos=grupos_permitidos, bloqueados=bloqueados):
            c = lotes.conversa_de(ln)
            if len(uteis) >= max_mensagens or (c not in conversas and len(conversas) >= max_conversas):
                cortou = True
                break
            conversas.add(c)
            uteis.append(ln)
        else:
            descartadas += 1
        seq_ate = ln["seq"]
    return uteis, descartadas, seq_ate, cortou


def ciclo(**kw) -> dict:
    """Um ciclo completo (`_ciclo`) e, ao fim de TODO ciclo real, `dados/ultima-leitura.json` (pedido 43): o indicador da
    lateral mostra a hora da última LEITURA, não do último lote com novidade (ciclo "sem_novidade" não grava lote, e o
    painel dizia "lido em 08/11" com o coletor rodando de meia em meia hora). dry-run e --sem-claude não gravam nada."""
    r = _ciclo(**kw)
    if not kw.get("dry_run") and not kw.get("sem_claude"):
        # pedido 81: o lote tem teto; se sobrou fila, a leitura seguinte não espera o intervalo escolhido em Ajustes
        limite = int(((kw.get("config") or {}).get("coletor") or {}).get("limite_fila") or 0)
        pendente = bool(r.get("cortado")) or (limite > 0 and int(r.get("fila") or 0) >= limite)
        _marcar_ultima_leitura(kw["dados"], r, int(kw.get("agora", time.time)()), pendente)
    return r


def _ciclo(*, psql, cursor, config: dict, dados: Path, prompt_sistema: Path, obter_banco, obter_evo, obter_ponte,
           executor=None, dormir=time.sleep, agora=time.time, dry_run: bool = False,
           sem_claude: bool = False, jid_proprio: str | None = None, transcritor=None) -> dict:
    """Um ciclo completo. Devolve um resumo (status, contagens). Exceções de rede sobem para main() virar exit 2/1.

    Garantias: fila vazia não cria pasta nem chama o Claude; o lote é um PREFIXO da fila (até 15 conversas / 150
    mensagens) e o cursor só avança até `seq_ate` (a) depois de JSON válido do Claude ou (b) quando tudo até
    `seq_ate` foi descartado pelos filtros ou já estava classificado; dry-run e --sem-claude nunca avançam cursor
    nem gravam banco/lotes.jsonl. O resumo traz `sem_fila` e `avisar` (conferência D-26) em todo ciclo.
    `transcritor` (D-47, app.transcricao.Transcritor): transcreve os áudios do prefixo ANTES de montar o lote, inclusive
    de grupo que a IA não lê (o painel mostra o texto); None = sem transcrição (comportamento anterior)."""
    if cursor.pareado_em is None:
        raise lotes.NaoPareado("cursor sem pareado_em: nada é lido antes do pareamento (R-2)")
    desde = cursor.desde          # D-53: pareado_em − 1 dia; o histórico gravado no pareamento entra por aqui, filtrado
    cfg_c, cfg_m, cfg_x = config["contexto"], config["midia"], config["coletor"]
    t0 = agora()

    fila = lotes.ler_fila(psql, cursor.ultimo_seq, int(cfg_x["limite_fila"]), desde=desde)
    aviso = _conferir_sem_fila(psql, desde)
    if not dry_run and not sem_claude:   # dry-run e --sem-claude não gravam nada em dados/ (contrato do coletor)
        aviso["ultima_mensagem_em"] = _marcar_ultima_mensagem(psql, dados, int(agora()))
    if not fila:
        log.info("fila vazia (ultimo_seq=%d)", cursor.ultimo_seq)
        return {"status": "sem_novidade", "fila": 0, "novas": 0, "conversas": 0, **aviso}

    # Lote reduzido à metade depois de 2 falhas do Claude na mesma faixa (livelock de lote grande).
    falhas = _falhas_da_faixa(dados, cursor.ultimo_seq)
    fator = 2 ** (falhas // FALHAS_PARA_REDUZIR)
    max_conv = max(1, int(cfg_x.get("max_conversas", LIMITE_CONVERSAS)) // fator)
    max_msgs = max(1, int(cfg_x.get("max_mensagens", LIMITE_MENSAGENS)) // fator)
    evento = {}
    if fator > 1:
        evento = {"evento": "lote_reduzido", "avisar": True}
        log.warning("lote reduzido: %d falhas na faixa após seq=%d; limite %d conversas / %d mensagens",
                    falhas, cursor.ultimo_seq, max_conv, max_msgs)

    # D-41: segunda trava do bloqueio (a primeira é o gatilho no banco). Se a consulta falhar, o ciclo para aqui.
    bloqueados = {ln[0] for ln in psql("SELECT jid FROM vigia.bloqueados") if ln and ln[0]}
    # F7 do estudo (D-49): conversa do sistema (o bot que manda os avisos do próprio Greg) não vai para a IA; segue no
    # painel e na transcrição, ao contrário do bloqueio.
    do_sistema = {str(j) for j in (config.get("leitura") or {}).get("conversas_do_sistema", []) if j}
    uteis, descartadas, seq_ate, cortou = cortar_por_prefixo(
        fila, desde=desde, grupos_permitidos=set(config["grupos"]["permitidos"]),
        max_conversas=max_conv, max_mensagens=max_msgs, bloqueados=frozenset(bloqueados | do_sistema))
    resumo = {"fila": len(fila), "descartadas": descartadas, "novas": len(uteis),
              "conversas": len({lotes.conversa_de(ln) for ln in uteis}), "seq_ate": seq_ate, "cortado": cortou,
              **aviso}
    if evento:
        resumo.update(evento)
        resumo["avisar"] = True

    if dry_run:
        return {"status": "dry_run", **resumo}
    # D-47: áudios do prefixo (seq <= seq_ate) passam pelo motor local antes de qualquer lote; grava no SQLite e segue.
    # Motor ausente ou falhando = marcador [áudio] como antes; Evolution fora/credencial recusada sobem ("não rodou").
    if transcritor is not None and transcritor.ativo():
        resumo["transcricao"] = transcritor.rodar([ln for ln in fila if ln["seq"] <= seq_ate], obter_banco=obter_banco,
                                                  obter_evo=obter_evo, desde=desde,
                                                  bloqueados=frozenset(bloqueados))
    if not uteis:
        if not sem_claude:
            cursor.avancar(seq_ate)
            cursor.salvar()
        log.info("fila=%d toda descartada pelos filtros; seq_ate=%d", len(fila), seq_ate)
        return {"status": "sem_novidade", **resumo}

    ponte_crua = obter_ponte()
    ponte = _PonteEmCache(ponte_crua)
    banco = obter_banco()
    resumo["vinculos"] = _vinculos_do_ciclo(banco, ponte_crua, psql, dados)
    # Remontagem: msg_id já classificado (lote anterior que o Claude gravou mas não fechou) vira [contexto].
    achar = getattr(banco, "classificacao_por_msg_id", None)
    ja = [ln for ln in uteis if callable(achar) and achar(ln["id"]) is not None]
    ids_ja = {ln["id"] for ln in ja}
    pendentes = [ln for ln in uteis if ln["id"] not in ids_ja]
    resumo.update(novas=len(pendentes), ja_classificadas=len(ja),
                  conversas=len({lotes.conversa_de(ln) for ln in pendentes}))
    if not pendentes:
        if not sem_claude:
            cursor.avancar(seq_ate)
            cursor.salvar()
        log.info("fila=%d toda já classificada; seq_ate=%d", len(fila), seq_ate)
        return {"status": "sem_novidade", **resumo}
    grupos = lotes.agrupar_por_conversa(pendentes)

    # Contexto comum gerado UMA vez por ciclo e copiado em cada conversa. Frentes fora = não rodou (antes de criar pasta).
    contexto_md = lotes.contexto_do_ciclo(banco, ponte, agora())
    evo = obter_evo()
    caixa = dados / "caixa"
    cache = caixa / ".midia"
    nome, pasta = _criar_pasta_do_lote(caixa, agora())
    teto = int(cfg_m["teto_ciclo"])
    baixadas = puladas = falhas_img = do_cache = audios = transcritos = 0
    try:
        usados: set[str] = set()
        for conversa, novas in grupos.items():
            ctx = lotes.contexto_da_conversa(psql, conversa, horas=int(cfg_c["horas"]), minimo=int(cfg_c["minimo"]),
                                             desde=desde, agora=agora())
            ja_da_conversa = [ln for ln in ja if lotes.conversa_de(ln) == conversa]
            ctx_ids = {c["id"] for c in ctx}
            ctx = ctx + [ln for ln in ja_da_conversa if ln["id"] not in ctx_ids]
            # D-47: transcrições já gravadas (deste ciclo ou de anteriores) das mensagens de áudio desta conversa.
            ids_audio = [x["id"] for x in novas + ctx if transcricao.eh_audio(x)]
            transcritas = banco.transcricoes(ids_audio) if ids_audio else {}
            r = lotes.montar_lote(pasta / lotes.slug_da_conversa(conversa, usados), conversa, novas, ctx, banco=banco,
                                  ponte=ponte, evo=evo, contexto_md=contexto_md, jid_proprio=jid_proprio,
                                  teto_imagens=max(0, teto - baixadas - falhas_img), cache_midia=cache,
                                  pausa_s=float(cfg_m["pausa_s"]), dormir=dormir, agora=agora(),
                                  baixou_antes=(baixadas + falhas_img) > 0, transcricoes=transcritas)
            baixadas += r["imagens_baixadas"]
            do_cache += r["imagens_cache"]
            falhas_img += r["imagens_falhas"]
            puladas += r["imagens_puladas"]
            audios += r["audios"]
            transcritos += r["audios_transcritos"]
        resumo.update(lote=nome, imagens=baixadas, imagens_cache=do_cache, imagens_falhas=falhas_img,
                      imagens_puladas=puladas, audios=audios, audios_transcritos=transcritos)
        if sem_claude:
            log.info("lote=%s montado sem Claude: conversas=%d novas=%d imagens=%d", nome, len(grupos), len(pendentes),
                     baixadas)
            return {"status": "sem_claude", **resumo}
        final = leitor.ler(pasta, modelo=str(cfg_x["modelo"]), prompt_sistema=prompt_sistema,   # pedido 68: motor de [ia]
                           executor=executor, timeout=float(cfg_x["timeout_claude_s"]), config=config, dados=dados)
    except BaseException as e:
        # Cursor intacto: o próximo ciclo remonta a mesma faixa (classificar é idempotente por msg_id). A mídia já
        # baixada fica em caixa/.midia para a próxima tentativa.
        shutil.rmtree(pasta, ignore_errors=True)
        if isinstance(e, lotes.ErroClaude):
            _anotar_falha(dados, cursor.ultimo_seq,
                          login=isinstance(e, lotes.ClaudeSemLogin) or getattr(e, "nao_e_do_lote", False))
        raise

    # JSON válido recebido: só agora registra e avança.
    banco.registrar_lote(nome=nome, conversas=len(grupos), novas=len(pendentes), itens=final["itens"],
                         atencao=final["atencao"], tokens_in=final.get("_tokens_in"),
                         tokens_out=final.get("_tokens_out"), modelo=leitor.modelo_usado(final, cfg_x["modelo"]),
                         custo_usd=final.get("_custo_usd"), duracao_ms=final.get("_duracao_ms"), turnos=final.get("_turnos"))
    registro = {
        "lote": nome, "em": int(agora()), "conversas": len(grupos), "novas": len(pendentes), "itens": final["itens"],
        "atencao": final["atencao"], "imagens": baixadas, "imagens_cache": do_cache, "imagens_puladas": puladas,
        "ja_classificadas": len(ja), "seq_ate": seq_ate, "modelo": leitor.modelo_usado(final, cfg_x["modelo"]),
        "audios": audios, "audios_transcritos": transcritos, "segundos": round(agora() - t0, 1)}
    registro.update({k: final[f"_{k}"] for k in ("custo_usd", "turnos") if final.get(f"_{k}") is not None})  # fase A
    if resumo.get("transcricao"):
        registro["transcricao"] = resumo["transcricao"]   # D-47: contagens e tempo de motor do ciclo (nunca texto)
    _registrar_jsonl(dados / "lotes.jsonl", registro)
    cursor.avancar(seq_ate)
    cursor.salvar()
    try:   # pedido 80: arquivos que a IA pediu para guardar numa seção; antes de apagar o cache e nunca derruba o ciclo
        from app import anexos
        anexados = anexos.buscar_pendentes(banco, psql, evo, dados, desde)
        if any(anexados.values()):
            log.info("arquivos de seção: %s", anexados)
    except Exception as e:  # noqa: BLE001
        log.warning("arquivos de seção não buscados (%s)", type(e).__name__)
    lotes.apagar_cache(cache, [ln["id"] for ln in uteis])
    _limpar_falhas(dados)
    log.info("lote=%s conversas=%d novas=%d itens=%d atencao=%d imagens=%d seq_ate=%d", nome, len(grupos),
             len(pendentes), final["itens"], final["atencao"], baixadas, seq_ate)
    return {"status": "ok", "itens": final["itens"], "atencao": final["atencao"], **resumo}


def _conferir_sem_fila(psql, desde: int) -> dict:
    """D-26: "Message" sem linha em vigia.fila (trigger falhou) -> `avisar`; nunca derruba o ciclo."""
    try:
        n = lotes.contar_sem_fila(psql, desde)
    except VigiaErro as e:
        log.warning("conferência sem_fila não rodou: %s", e)
        return {"sem_fila": None, "avisar": True}
    if n > 0:
        log.warning("%d mensagem(ns) em Message sem linha em vigia.fila", n)
    return {"sem_fila": n, "avisar": n > 0}


def _marcar_ultima_mensagem(psql, dados: Path, agora: int) -> int | None:
    """Pedido 41: grava dados/ultima-mensagem.json {"ultima_em", "conferido_em"} para o monitor (silêncio com a conexão
    "open") e a página Conexão ("Última mensagem recebida"). Nunca derruba o ciclo; sem resposta deixa o arquivo como está."""
    try:
        ultima = lotes.ultima_mensagem_em(psql)
    except Exception as e:  # noqa: BLE001  (consulta auxiliar: psql falso ou fora não pode parar a leitura)
        log.debug("última mensagem não conferida: %s", causa(e))
        return None
    if ultima is None:
        return None
    try:
        dados.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = dados / "ultima-mensagem.json.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"ultima_em": int(ultima), "conferido_em": int(agora)}, f)
        os.replace(tmp, dados / "ultima-mensagem.json")
    except OSError as e:
        log.warning("ultima-mensagem.json não gravado: %s", causa(e))
    return int(ultima)


def _marcar_ultima_leitura(dados: Path, resumo: dict, agora: int, pendente: bool = False) -> None:
    """Pedido 43: grava dados/ultima-leitura.json {"em","status","fila","novas","pendente"} (0600, troca atômica) ao fim
    de todo ciclo real, para o indicador da lateral ("Conectado, lido às HH:MM") e GET /conexao/lateral. `pendente`
    (pedido 81): ficou fila por ler, e app/agenda_leitura.py deixa a próxima leitura sair sem esperar o intervalo.
    Nunca derruba o ciclo."""
    try:
        dados.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = dados / "ultima-leitura.json.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"em": int(agora), "status": str(resumo.get("status") or ""), "fila": int(resumo.get("fila") or 0),
                       "novas": int(resumo.get("novas") or 0), "pendente": bool(pendente)}, f)
        os.replace(tmp, dados / "ultima-leitura.json")
    except (OSError, TypeError, ValueError) as e:
        log.warning("ultima-leitura.json não gravado: %s", causa(e))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _argumentos(argv):
    p = argparse.ArgumentParser(
        prog="coletor.py",
        description="Um ciclo de leitura do Greg: fila nova -> lote -> claude -p -> cursor. "
                    "Saída: 0 ok · 1 falhou · 2 não rodou (Postgres/Evolution/Frentes fora, sem pareamento).")
    p.add_argument("--dry-run", action="store_true",
                   help="só lê a fila e mostra contagens; não escreve nada (pasta, cursor, banco, log), não baixa mídia")
    p.add_argument("--sem-claude", action="store_true",
                   help="monta o lote (baixa imagens) mas não chama o Claude nem avança o cursor")
    p.add_argument("--config", default=str(RAIZ / "config.toml"), help="caminho do config.toml (padrão: config.toml da raiz)")
    p.add_argument("--agora", action="store_true",
                   help="lê mesmo fora do intervalo e do horário escolhidos em Ajustes (o mesmo que VIGIA_LER_AGORA=1)")
    return p.parse_args(argv)


def _configurar_log() -> None:
    """Só stderr: o vigia.sh grava em ~/Library/Logs/vigia-whatsapp.log (sem arquivo de log próprio, nem texto de mensagem)."""
    raiz = logging.getLogger("vigia")
    raiz.setLevel(logging.INFO)
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    raiz.addHandler(h)


def _saude(dados: Path, args, erro, resumo: dict | None = None) -> None:
    """Pedido 63, lote 2: o resultado do ciclo vai para a central de avisos e o que estiver na hora é notificado.
    dry-run e --sem-claude não escrevem em dados/ (contrato do coletor)."""
    if args.dry_run or args.sem_claude:
        return
    from app import saude
    saude.do_coletor(dados, erro, resumo)
    saude.notificar(dados)


def main(argv=None) -> int:
    os.umask(0o077)  # dados/ (conversa.md, mídia, banco, cursor) só do dono
    args = _argumentos(argv)
    _configurar_log()
    dados = Path(os.environ.get("VIGIA_DADOS") or RAIZ / "dados")
    # Pedido 81 (D-82): o agendador dispara de 30 em 30 minutos e é aqui que se decide se este disparo lê. Com o padrão
    # de Ajustes (30 min, o dia todo) todo disparo lê. dry-run e --sem-claude são conferência à mão: não passam por isso.
    if not args.dry_run and not args.sem_claude:
        from app import agenda_leitura
        ler, motivo = agenda_leitura.deste_disparo(
            dados, forcar=args.agora or os.environ.get("VIGIA_LER_AGORA") == "1",
            recuperacao=os.environ.get("VIGIA_SEGUNDA_LEITURA") == "1")
        if not ler:
            agenda_leitura.marcar_passada(dados, time.time(), motivo)
            log.info("sem leitura neste disparo: %s", motivo)
            print(json.dumps({"status": "fora_da_agenda", "motivo": motivo}, ensure_ascii=False))
            return 0
    env = ler_env(Path(os.environ.get("VIGIA_ENV") or RAIZ / ".env"))
    # D-45: Keychain opt-in (desligado por padrão) só completa as DUAS chaves do coletor; VIGIA_TOKEN_ENVIO nunca é pedido (R-9).
    segredos.completar(env, ("EVOLUTION_INSTANCE_APIKEY", "FRENTES_TOKEN"))
    for k in ("EVOLUTION_URL", "EVOLUTION_INSTANCE", "EVOLUTION_INSTANCE_APIKEY", "FRENTES_URL", "FRENTES_TOKEN",
              "DONO_WHATSAPP", "VIGIA_CONVERSAS_DO_SISTEMA"):
        env.setdefault(k, os.environ.get(k, ""))

    def fabrica_banco():
        from app.banco import Banco
        return Banco(str(dados / "vigia.db"))

    def fabrica_evo():
        from app.evolution_client import EvolutionClient
        if not env.get("EVOLUTION_INSTANCE_APIKEY"):
            raise NaoAutorizado("EVOLUTION_INSTANCE_APIKEY ausente no .env")
        return EvolutionClient(env.get("EVOLUTION_URL") or "http://127.0.0.1:8080",
                               env.get("EVOLUTION_INSTANCE") or "vigia", env["EVOLUTION_INSTANCE_APIKEY"])

    def fabrica_ponte():
        from app.frentes_ponte import criar_ponte
        return criar_ponte(env.get("FRENTES_URL"), env.get("FRENTES_TOKEN", ""), dados, padrao_url="http://127.0.0.1:8110")

    try:
        from app.cursor import Cursor
        config = com_conversas_do_sistema(carregar_config(Path(args.config)), env.get("VIGIA_CONVERSAS_DO_SISTEMA", ""))
        cursor = Cursor.carregar(dados / "cursor.json")
        psql = psql_ro_via_script(Path(os.environ.get("VIGIA_EVOLUTION_SH") or RAIZ / "bin/evolution.sh"))
        transcritor = transcricao.Transcritor(config.get("transcricao"), dados / "caixa" / ".audio")   # D-47
        r = ciclo(psql=psql, cursor=cursor, config=config, dados=dados, prompt_sistema=RAIZ / "prompts/vigia.md",
                  obter_banco=fabrica_banco, obter_evo=lambda: _Preguicoso(fabrica_evo), obter_ponte=fabrica_ponte,
                  dry_run=args.dry_run, sem_claude=args.sem_claude, jid_proprio=env.get("DONO_WHATSAPP") or None,
                  transcritor=transcritor)
    except (lotes.PostgresIndisponivel, lotes.NaoPareado, EvolutionIndisponivel, NaoAutorizado,
            FrentesIndisponivel, FileNotFoundError) as e:
        _saude(dados, args, e)
        log.error("não rodou: %s", e if isinstance(e, VigiaErro) else causa(e))
        print(f"coletor: não rodou ({e if isinstance(e, VigiaErro) else type(e).__name__})", file=sys.stderr)
        return 2
    except VigiaErro as e:
        _saude(dados, args, e)
        log.error("falhou: %s", e)
        print(f"coletor: falhou ({e})", file=sys.stderr)
        return 1
    except Exception as e:  # nunca imprime a mensagem da exceção: pode conter trecho de conversa
        _saude(dados, args, e)        # pedido 85: o aviso de leitura parada diz que a última tentativa deu erro
        log.error("falhou: %s", causa(e))
        print(f"coletor: falhou ({type(e).__name__})", file=sys.stderr)
        return 1
    _saude(dados, args, None, r)
    print(json.dumps(r, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
