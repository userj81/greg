"""Fechamento do dia (D-57, pedido 45; é a "leitura diária consolidando ordens" de R-12): depois de [horarios].diario,
os cartões PROVISÓRIOS do dia (atenção/demanda/lembrar ainda sem item) são consolidados e sobem ao Frentes, sozinhos e
confirmados (D-58, pedido 46: a IA decide; ninguém confirma no painel).
1. Um passe do Claude (prompts/consolidar.md) sobre TODA conversa com cartão provisório (D-58: é o último filtro antes
   de subir). Com a conversa do dia inteira na frente, ele:
   - funde o que é o mesmo assunto (`vigia-ponte consolidar`): os absorvidos viram substituídos pelo que fica; se o que
     fica JÁ ESTÁ no Frentes (item aberto), o item de lá ganha a versão nova em vez de nascer um segundo item;
   - segura o que era só conversa (`vigia-ponte rebaixar`): ruído, pergunta já respondida, assunto resolvido ali mesmo
     viram aviso/sem_acao e NÃO sobem; ficam no histórico.
2. Tudo o que continua provisório (criado antes do fechamento) sobe ao Frentes (app.lancamento.lancar).
Idempotente e guiado pelo estado: nada de marcador de "já rodou" — o que subiu tem lancado_em; o que foi absorvido tem
substituida_por; o que o dono decidiu não é provisório. Dia perdido (Mac desligado às 21 h) é alcançado no próximo ciclo.
Cartão criado depois do fechamento de hoje é de amanhã. Claude falhando 3 vezes no mesmo fechamento: sobe sem consolidar
(e sem o filtro de ruído: melhor um cartão a mais do que um pedido perdido).
Quem chama: bin/fechar-dia.py (via bin/vigia.sh ciclo, depois do coletor) e os testes.
O que não faz: não absorve cartão decidido pelo dono, com item ou substituído; não lê conversa que não tem cartão
provisório; não baixa mídia; não loga texto de mensagem (só ids e contagens).
"""
import json
import logging
import math
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

from app import lancamento, leitor, lotes

log = logging.getLogger("vigia.consolidacao")

MAX_TENTATIVAS_CLAUDE = 3          # falhas no mesmo fechamento antes de subir sem consolidar
CAMPOS_RESULTADO = ("conversas", "absorvidos", "rebaixados")
MODELO_PADRAO = "sonnet"
TIMEOUT_PADRAO_S = 900.0
_ARQ_FALHAS = "fechamento-falhas.json"
JANELA_LANCADOS_DIAS = 14          # D-58: cartões já no Frentes que o fechamento ainda pode corrigir (a janela dos itens do WhatsApp)


def pendentes(banco, limite: str) -> dict[str, list[dict]]:
    """{conversa: [cartões provisórios criados antes de `limite`]}, na ordem das mensagens."""
    grupos: dict[str, list[dict]] = {}
    for r in banco.provisorias(ate=limite):
        grupos.setdefault(str(r["conversa"]), []).append(r)
    return grupos


def lancados_abertos(banco, ponte, conversas, *, conferir_frentes: bool = True) -> dict[str, list[dict]]:
    """D-58: {conversa: [cartões já no Frentes que a IA pode corrigir]} só das `conversas` dadas (as que têm provisório).
    Com `conferir_frentes`, uma chamada ao Frentes deixa só os de item ABERTO (fechado/cancelado não se corrige: o
    provisório sobe sozinho). Sem ela (dry-run), vale o que o vigia.db sabe."""
    conversas = set(conversas)
    if not conversas:
        return {}
    candidatos = [r for r in banco.lancadas(dias=JANELA_LANCADOS_DIAS) if str(r["conversa"]) in conversas]
    if candidatos and conferir_frentes:
        abertos = {lotes._int(i.get("id")) for i in ponte.itens_abertos(origem="whatsapp")}
        candidatos = [r for r in candidatos if int(r["item_id"]) in abertos]
    saida: dict[str, list[dict]] = {}
    for r in candidatos:
        saida.setdefault(str(r["conversa"]), []).append(r)
    return saida


# ---------------------------------------------------------------------------
# Pasta do fechamento (uma subpasta por conversa a conferir): cartoes.md + conversa.md
# ---------------------------------------------------------------------------

def _contato(cartoes: list[dict]) -> str:
    nome = next((str(c.get("contato")) for c in reversed(cartoes) if c.get("contato")), "?")
    return lotes._nome_contato(nome, 80) or "?"


def _linha_cartao(c: dict) -> str:
    prazo = f" · prazo {str(c['prazo'])[:10]}" if c.get("prazo") else ""
    return (f"- #{c['id']} · {lotes._hora(lotes._int(c.get('ts')))} · {c.get('classe')} · {c.get('confianca')} · "
            f"{lotes._limpo(c.get('resumo'), 300)}{prazo}")


def cartoes_md(conversa: str, cartoes: list[dict], agora_dt: datetime, fecho: datetime, lancados: list[dict] | None = None) -> str:
    """Os cartões provisórios da conversa e (D-58) os que já estão no Frentes com item aberto, cada um com `#id`."""
    linhas = [f"conversa: {conversa}", f"contato: {_contato(cartoes)}", f"hoje: {agora_dt:%Y-%m-%d}",
              f"fechamento: {fecho:%Y-%m-%d %H:%M}", "", "## Cartões provisórios desta conversa", ""]
    linhas += [_linha_cartao(c) for c in cartoes]
    linhas += ["", "## Cartões desta conversa que já estão no Frentes (item aberto)", ""]
    linhas += [f"{_linha_cartao(c)} · no Frentes #{c['item_id']}" for c in (lancados or [])] or ["- (nenhum)"]
    return "\n".join(linhas) + "\n"


def conversa_md(psql, banco, conversa: str, cartoes: list[dict], *, desde: int, agora: float, horas: int) -> tuple[str, int]:
    """As mensagens da conversa desde o começo do dia dos cartões (todas como [contexto]; sem imagem baixada, áudio com
    a transcrição já gravada). `desde` é o piso de R-2 (cursor.desde). Devolve (markdown, nº de linhas)."""
    linhas = lotes.contexto_da_conversa(psql, conversa, horas=horas, minimo=0, desde=desde, agora=agora)
    ids_audio = [ln["id"] for ln in linhas if lotes.descrever(ln)["tipo"] == "audio"]
    transcritas = banco.transcricoes(ids_audio) if ids_audio else {}
    eh_grupo = conversa.endswith("@g.us")
    md = [lotes.linha_md(ln, "contexto", transcricao=transcritas.get(ln["id"]), eh_grupo=eh_grupo) for ln in linhas]
    md.sort(key=lambda t: (t[0], t[1]))
    cabeca = [f"conversa: {conversa}", f"contato: {_contato(cartoes)}",
              f"hoje: {datetime.fromtimestamp(agora):%Y-%m-%d}", ""]
    return "\n".join(cabeca + [t[2] for t in md]) + "\n", len(md)


def _criar_pasta(caixa: Path, agora: float) -> Path:
    caixa.mkdir(parents=True, exist_ok=True, mode=0o700)
    base = datetime.fromtimestamp(agora).strftime("%Y-%m-%d_%H%M") + "-fecho"
    n = 1
    while True:
        nome = base if n == 1 else f"{base}-{n}"
        try:
            (caixa / nome).mkdir(mode=0o700)
            return caixa / nome
        except FileExistsError:
            n += 1


def montar_pasta(dados: Path, duplicados: dict[str, list[dict]], psql, banco, *, desde: int, agora: float,
                 fecho: datetime, lancados: dict[str, list[dict]] | None = None) -> Path:
    """dados/caixa/<AAAA-MM-DD_HHMM>-fecho/<slug>/{cartoes.md, conversa.md}; apagada depois do passe.
    A conversa lida é a do dia dos cartões PROVISÓRIOS; do cartão já lançado a IA vê o resumo e o prazo, não a conversa
    antiga."""
    pasta = _criar_pasta(dados / "caixa", agora)
    agora_dt = datetime.fromtimestamp(agora)
    usados: set[str] = set()
    for conversa, cartoes in duplicados.items():
        inicio = min(min(lotes._int(c.get("ts")) for c in cartoes), int((fecho - timedelta(days=1)).timestamp())) - 3600
        horas = max(1, math.ceil((agora - inicio) / 3600))
        sub = pasta / lotes.slug_da_conversa(conversa, usados)
        sub.mkdir(mode=0o700)
        (sub / "cartoes.md").write_text(cartoes_md(conversa, cartoes, agora_dt, fecho, (lancados or {}).get(conversa)),
                                        encoding="utf-8")
        md, _n = conversa_md(psql, banco, conversa, cartoes, desde=desde, agora=agora, horas=horas)
        (sub / "conversa.md").write_text(md, encoding="utf-8")
    return pasta


def mensagem(pasta: Path) -> str:
    pasta = Path(pasta).resolve()
    subs = sorted(p.parent for p in pasta.rglob("cartoes.md"))
    linhas = [f"Feche o dia dos cartões em {pasta}: {len(subs)} conversa(s) a conferir.",
              "Para cada subpasta, leia cartoes.md e depois conversa.md da mesma pasta:"]
    for s in subs:
        linhas.append(f"- {s}/cartoes.md e {s}/conversa.md")
    linhas.append("Siga as instruções do sistema e termine com a linha JSON final.")
    return "\n".join(linhas)


# ---------------------------------------------------------------------------
# Falhas do Claude no mesmo fechamento
# ---------------------------------------------------------------------------

def _falhas(dados: Path, limite: str) -> int:
    try:
        d = json.loads((dados / _ARQ_FALHAS).read_text(encoding="utf-8"))
        return int(d.get("n") or 0) if d.get("fechamento") == limite else 0
    except (OSError, ValueError, TypeError, AttributeError):
        return 0


def _anotar_falha(dados: Path, limite: str) -> int:
    n = _falhas(dados, limite) + 1
    try:
        (dados / _ARQ_FALHAS).write_text(json.dumps({"fechamento": limite, "n": n}), encoding="utf-8")
    except OSError:
        pass
    return n


def _limpar_falhas(dados: Path) -> None:
    try:
        (dados / _ARQ_FALHAS).unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# O fechamento
# ---------------------------------------------------------------------------

def rodar(*, banco, ponte, psql, config: dict, dados: Path, prompt_sistema: Path, desde: int, executor=None,
          agora: float | None = None, dry_run: bool = False) -> dict:
    """Um fechamento. Devolve um resumo com `status`:
    antes_da_hora (nada a fazer) · nada_pendente · dry_run · adiada (Claude falhou; tenta no próximo ciclo) · ok.
    Exceções de rede (Postgres, Frentes) sobem para o main virar exit 2: o que já subiu está gravado; o resto fica para o
    próximo ciclo."""
    agora = time.time() if agora is None else agora
    agora_dt = datetime.fromtimestamp(agora)
    fecho = lancamento.fechamento_do_dia(agora_dt, config)
    saida = {"fechamento": fecho.strftime("%Y-%m-%d %H:%M")}
    if agora_dt < fecho:
        return {"status": "antes_da_hora", **saida}
    limite = fecho.strftime("%Y-%m-%d %H:%M:%S")
    grupos = pendentes(banco, limite)
    if not grupos:
        return {"status": "nada_pendente", **saida}
    # D-58: o passe confere TODA conversa com provisório (régua do que sobe) e também contra o que JÁ subiu dela.
    ja_lancados = lancados_abertos(banco, ponte, grupos, conferir_frentes=not dry_run)
    ids = [int(c["id"]) for cs in grupos.values() for c in cs]
    total = len(ids)
    saida.update(conversas=len(grupos), cartoes=total,
                 com_repetidos=sum(1 for c, cs in grupos.items() if len(cs) >= 2 or ja_lancados.get(c)),
                 com_lancados=sum(1 for c in grupos if ja_lancados.get(c)), absorvidos=0, rebaixados=0, lancados=0)
    if dry_run:
        return {"status": "dry_run", **saida}

    cfg_x = config.get("coletor") or {}
    modelo = str(cfg_x.get("modelo") or MODELO_PADRAO)
    falhas = _falhas(dados, limite)
    if falhas >= MAX_TENTATIVAS_CLAUDE:
        log.warning("fechamento %s: %d falhas do Claude; sobe sem conferir", limite[:16], falhas)
        saida["claude"] = "pulado_apos_falhas"
        _limpar_falhas(dados)
    else:
        pasta = montar_pasta(dados, grupos, psql, banco, desde=desde, agora=agora, fecho=fecho, lancados=ja_lancados)
        try:
            final = leitor.ler(pasta, modelo=modelo, prompt_sistema=prompt_sistema, executor=executor,   # pedido 68
                               timeout=float(cfg_x.get("timeout_claude_s") or TIMEOUT_PADRAO_S),
                               campos=CAMPOS_RESULTADO, mensagem=mensagem(pasta), config=config, dados=dados)
        except lotes.ErroClaude as e:
            n = _anotar_falha(dados, limite)
            log.warning("fechamento %s: claude falhou (%s), tentativa %d/%d; fica para o próximo ciclo",
                        limite[:16], e, n, MAX_TENTATIVAS_CLAUDE)
            return {"status": "adiada", "claude": "falhou", "tentativa": n, **saida}
        finally:
            shutil.rmtree(pasta, ignore_errors=True)   # a conversa do dia já está nos lotes do coletor
        _limpar_falhas(dados)
        saida["claude"] = "ok"
        depois = [banco.classificacao(i) or {} for i in ids]   # o que a ponte GRAVOU, não o que o Claude disse
        saida["absorvidos"] = sum(1 for r in depois if r.get("substituida_por") is not None)
        saida["rebaixados"] = sum(1 for r in depois if r.get("rebaixada_em") and r.get("substituida_por") is None)
        banco.registrar_lote(nome=f"fecho-{fecho:%Y-%m-%d}", conversas=len(grupos), novas=0, itens=0, atencao=0,
                             tokens_in=final.get("_tokens_in"), tokens_out=final.get("_tokens_out"),
                             modelo=leitor.modelo_usado(final, modelo),
                             custo_usd=final.get("_custo_usd"), duracao_ms=final.get("_duracao_ms"),
                             turnos=final.get("_turnos"))

    lancados = 0
    for reg in banco.provisorias(ate=limite):
        lancamento.lancar(banco, ponte, reg)
        lancados += 1
    saida["lancados"] = lancados
    log.info("fechamento %s: conversas=%d cartoes=%d repetidas=%d absorvidos=%d rebaixados=%d lancados=%d", limite[:16],
             len(grupos), total, saida["com_repetidos"], saida["absorvidos"], saida["rebaixados"], lancados)
    return {"status": "ok", **saida}
