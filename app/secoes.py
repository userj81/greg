"""Seções dentro da frente (pedido 79, D-80): o resumo que a IA recebe para escolher frente e seção, e a regra de
em qual seção um item pode entrar.
Quem chama: app/lotes.py (contexto do ciclo), app/ponte/classificar.py e app/lancamento.py (a seção escolhida pela
IA), app/paginas/frentes.py (tela) e app/mcp_ferramentas.py (conector).
O resumo é montado na hora, sem IA e sem guardar nada: sai dos itens que estão em aberto agora. Quando a pessoa move
um item de seção, o exemplo muda no ciclo seguinte e é isso que ensina a IA.
O que não faz: não cria nem apaga seção sozinho, não fala com a rede, não decide a frente.
"""
from app.erros import VigiaErro

EXEMPLOS = 3            # itens de exemplo por seção
EXEMPLO_MAX = 80        # caracteres de cada exemplo
ARQUIVOS = 5            # nomes de arquivo por seção
RESUMO_MAX = 12000      # teto do bloco inteiro no contexto.md
NOME_MAX = 60
DESCRICAO_MAX = 200
TITULO = "## O que tem em cada frente"


def limpar_nome(nome) -> str:
    """Nome de seção em uma linha; vazio ou longo demais levanta ValueError."""
    nome = " ".join(str(nome or "").split())
    if not nome:
        raise ValueError("a seção precisa de um nome")
    if len(nome) > NOME_MAX:
        raise ValueError(f"nome de seção com mais de {NOME_MAX} letras")
    return nome


def limpar_descricao(descricao) -> str:
    descricao = " ".join(str(descricao or "").split())
    if len(descricao) > DESCRICAO_MAX:
        raise ValueError(f"descrição com mais de {DESCRICAO_MAX} letras")
    return descricao


def valida_para(banco, secao_id, frente_id) -> int | None:
    """O id da seção se ela existe e é da frente dada; senão None (o item fica sem seção, nunca na seção errada)."""
    if secao_id is None or frente_id is None:
        return None
    buscar = getattr(banco, "secao", None)
    if not callable(buscar):
        return None
    try:
        s = buscar(int(secao_id))
    except (TypeError, ValueError):
        return None
    return int(s["id"]) if s and int(s["frente_id"]) == int(frente_id) else None


def _curto(texto, n: int = EXEMPLO_MAX) -> str:
    texto = " ".join(str(texto or "").split())
    return texto if len(texto) <= n else texto[:n - 1].rstrip() + "…"


def _exemplos(itens: list[dict]) -> str:
    """Os mais novos primeiro: é o que mostra o que tem entrado ali."""
    recentes = sorted(itens, key=lambda i: i.get("id") or 0, reverse=True)[:EXEMPLOS]
    return " | ".join(_curto(i.get("texto")) for i in recentes)


def _linha(rotulo: str, itens: list[dict]) -> str:
    if not itens:
        return f"- {rotulo} · vazia"
    return f"- {rotulo} · {len(itens)} em aberto · ex.: {_exemplos(itens)}"


def distribuir(abertos: list[dict], secoes: list[dict], onde: dict) -> tuple[dict, list[dict]]:
    """({secao_id: [itens]}, [itens sem seção]) de UMA frente. Item apontando para seção que não é desta frente
    (foi movido de frente) conta como sem seção."""
    por = {int(s["id"]): [] for s in secoes}
    soltos = []
    for it in abertos:
        sid = onde.get(it.get("id"))
        (por[sid] if sid in por else soltos).append(it)
    return por, soltos


def resumo_md(banco, ponte) -> str:
    """Bloco do contexto.md: para cada frente, o que ela é, as seções (com descrição) e exemplos do que tem em aberto
    em cada uma. Frente parada e sem seção fica de fora (ela já está na lista de frentes). Frentes fora = ""."""
    ler_secoes = getattr(banco, "secoes", None)
    try:
        frentes = (ponte.estado() or {}).get("frentes") or []
        abertos = ponte.itens_abertos()
        secoes = ler_secoes() if callable(ler_secoes) else []
        onde = banco.secao_dos_itens() if callable(ler_secoes) else {}
    except (VigiaErro, AttributeError, TypeError):
        return ""
    arquivos = {}
    ler_anexos = getattr(banco, "anexos", None)
    if callable(ler_anexos):           # D-81: da seção a IA recebe só o NOME dos arquivos, nunca o conteúdo
        try:
            for a in ler_anexos():
                arquivos.setdefault(a["secao_id"], []).append(_curto(a.get("nome"), 40))
        except Exception:  # noqa: BLE001
            arquivos = {}
    por_frente, secoes_de = {}, {}
    for it in abertos:
        por_frente.setdefault(it.get("frente_id"), []).append(it)
    for s in secoes:
        secoes_de.setdefault(s["frente_id"], []).append(s)
    blocos = []
    # quem tem seção vem primeiro (é onde há escolha a fazer); depois quem tem mais coisa em aberto
    ordem = sorted(frentes, key=lambda f: (0 if secoes_de.get(f.get("id")) else 1, -len(por_frente.get(f.get("id"), [])),
                                           str(f.get("nome", "")).lower()))
    for f in ordem:
        fid = f.get("id")
        itens, minhas = por_frente.get(fid, []), secoes_de.get(fid, [])
        contexto = _curto(f.get("contexto"), DESCRICAO_MAX)
        if not itens and not minhas and not contexto:
            continue
        linhas = [f"### frente {fid} · {f.get('nome', '?')}"]
        if contexto:
            linhas.append(f"o que é: {contexto}")
        if minhas:
            por, soltos = distribuir(itens, minhas, onde)
            for s in minhas:
                desc = f" ({_curto(s.get('descricao'), DESCRICAO_MAX)})" if s.get("descricao") else ""
                linha = _linha(f"seção {s['id']} · {s['nome']}{desc}", por[int(s["id"])])
                nomes = arquivos.get(s["id"]) or []
                if nomes:
                    resto = f" e mais {len(nomes) - ARQUIVOS}" if len(nomes) > ARQUIVOS else ""
                    linha += f" · arquivos: {', '.join(nomes[:ARQUIVOS])}{resto}"
                linhas.append(linha)
            if soltos:
                linhas.append(_linha("sem seção", soltos))
        elif itens:
            linhas.append(f"- {len(itens)} em aberto · ex.: {_exemplos(itens)}")
        blocos.append("\n".join(linhas))
    if not blocos:
        return ""
    cabeca = [TITULO, "",
              "Resumo montado agora, dos itens em aberto. Serve para escolher `--frente` e, quando a frente tem "
              "seções, `--secao <id>`: ponha o item na seção cujos exemplos e descrição combinam com ele.", ""]
    saida, usado, cortadas = [], sum(len(x) + 1 for x in cabeca), 0
    for b in blocos:
        if usado + len(b) + 2 > RESUMO_MAX:
            cortadas += 1
            continue
        saida.append(b)
        usado += len(b) + 2
    if cortadas:
        saida.append(f"(mais {cortadas} frente(s) sem resumo por falta de espaço; elas estão na lista de frentes)")
    return "\n".join(cabeca) + "\n\n".join(saida) + "\n"
