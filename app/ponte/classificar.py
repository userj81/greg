"""Comando `vigia-ponte classificar`: valida a classe, grava no SQLite e, se a classe cria item, sobe para o Frentes na
hora (atenção, prazo em 24 h) ou deixa o cartão provisório para o fechamento do dia (D-57).
Quem chama: bin/vigia-ponte (acionado pelo Claude agendado do coletor, via prompts/vigia.md).
Conversa/contato/ts vêm do manifesto do lote (VIGIA_LOTE) pelo msg_id; msg_id fora dele é recusado.
Sem VIGIA_LOTE (uso manual/teste) exige --conversa e --ts. Classe `lembrando` exige --item aberto de origem whatsapp.
`--substitui ID` (a mensagem muda ou completa um assunto já classificado) e `--cancela ID` (o assunto caiu) marcam o
cartão antigo da MESMA conversa como substituído; o item do Frentes ganha a versão nova ou é cancelado (D-58: também
quando o dono editou o texto — as palavras dele ficam e a correção entra depois).
O que não faz: não toca Evolution, não loga nem devolve texto de mensagem além do resumo validado.
"""
import os

from app import lancamento, secoes, vinculos
from app.classes import cria_item, mapa_frentes, validar_classe, validar_confianca
from app.ponte import _comum


def configurar(sub):
    p = sub.add_parser("classificar", help="registra a classificação de uma mensagem (e cria item no Frentes se couber)")
    p.add_argument("msg_id")
    p.add_argument("classe")
    p.add_argument("resumo")
    p.add_argument("--conversa", default=None, help="só sem VIGIA_LOTE (uso manual)")
    p.add_argument("--contato", default=None, help="só sem VIGIA_LOTE (uso manual)")
    p.add_argument("--ts", type=int, default=None, help="só sem VIGIA_LOTE (uso manual)")
    p.add_argument("--frente", type=int, default=None)
    p.add_argument("--secao", type=int, default=None, help="pedido 79: id de uma seção da frente escolhida")
    p.add_argument("--anexar", default=None, choices=("sim",),
                   help="pedido 80: guarda na seção o arquivo desta mensagem (foto, vídeo, áudio, documento)")
    p.add_argument("--prazo", default=None, help="AAAA-MM-DD")
    p.add_argument("--confianca", default=None, help="alta|baixa (obrigatório: sem padrão silencioso, F8)")
    p.add_argument("--imagem", default=None)
    p.add_argument("--item", type=int, default=None, help="classe lembrando: item aberto do WhatsApp (obrigatório)")
    p.add_argument("--prioridade", type=int, choices=(1, 2), default=None, help="classe lembrando: sobe a prioridade (nunca rebaixa)")
    p.add_argument("--substitui", type=int, default=None,
                   help="D-57: id do cartão desta conversa cujo evento esta mensagem atualiza (data, hora, lugar…)")
    p.add_argument("--cancela", type=int, default=None, help="D-57: id do cartão desta conversa cujo evento foi cancelado")
    return p


def _g(args, nome, padrao=None):
    return getattr(args, nome, padrao)


def _origem(msg_id, args):
    """(conversa, contato, ts) pelo manifesto; sem VIGIA_LOTE, pelos argumentos explícitos."""
    com_lote, dados = _comum.procurar_no_manifesto(msg_id)
    if com_lote:
        if not dados:
            raise ValueError("msg_id fora do lote")
        conversa, contato, ts = dados.get("conversa"), dados.get("contato"), dados.get("ts")
        if not conversa or isinstance(ts, bool) or not isinstance(ts, int):
            raise ValueError("manifesto do lote inválido para este msg_id")
        return str(conversa), (str(contato).strip() or "contato"), ts
    conversa, ts = _g(args, "conversa"), _g(args, "ts")
    if not conversa or ts is None:
        raise ValueError("sem VIGIA_LOTE: informe --conversa e --ts")
    return str(conversa), ((_g(args, "contato") or "").strip() or "contato"), int(ts)


def aplicar_vinculo(banco, conversa: str, frente, confianca: str):
    """D-49: (frente, confiança, de_onde). Sem --frente, usa o vínculo da conversa. Marcado pelo dono e igual = mantém a
    confiança; IA diverge do dono = baixa; frente vinda de sugestão pelo nome = baixa. D-58: `baixa` é só registro (o
    cartão sobe na frente, confirmado, como qualquer outro)."""
    v = vinculos.frente_da_conversa(banco, conversa)
    if v is None:
        return frente, confianca, "ia" if frente is not None else None
    if frente is None:
        frente = v["frente_id"]
    if v["origem"] == "dono" and frente == v["frente_id"]:
        return frente, confianca, "dono"
    return frente, "baixa", ("ia" if frente != v["frente_id"] else "auto")


def executar(args, *, banco, ponte) -> dict:
    msg_id = str(args.msg_id).strip()
    if not msg_id:
        raise ValueError("msg_id vazio")
    try:
        classe = validar_classe(args.classe)
        if not _g(args, "confianca"):
            raise ValueError("informe --confianca alta ou baixa")
        confianca = validar_confianca(_g(args, "confianca"))
    except ValueError:
        raise ValueError("classe ou confiança inválida") from None
    resumo = _comum.validar_resumo(args.resumo)
    prazo = _comum.validar_prazo(_g(args, "prazo"))
    conversa, contato, ts = _origem(msg_id, args)
    frente = _g(args, "frente")
    imagem = os.path.basename(_g(args, "imagem")) if _g(args, "imagem") else None
    item_ref = _g(args, "item")
    prioridade_pedida = _g(args, "prioridade")
    if prioridade_pedida is not None and int(prioridade_pedida) not in (1, 2):
        raise ValueError("prioridade deve ser 1 ou 2")

    substitui, cancela = _g(args, "substitui"), _g(args, "cancela")
    if substitui is not None and cancela is not None:
        raise ValueError("use --substitui ou --cancela, não os dois")
    if classe == "lembrando" and (substitui is not None or cancela is not None):
        raise ValueError("lembrando não substitui cartão: use atualizar")

    anterior = banco.classificacao_por_msg_id(msg_id)
    if anterior is not None:  # idempotência: não toca o Frentes
        return {"ok": True, "repetido": True, "id": anterior["id"], "msg_id": msg_id,
                "classe": anterior["classe"], "item_id": anterior["item_id"]}

    if classe == "lembrando":
        if item_ref is None:
            raise ValueError("classe lembrando exige --item")
        item = _comum.item_whatsapp_aberto(ponte, int(item_ref))
        if item is None:
            raise ValueError("item não está aberto entre os itens do WhatsApp")
        enviar = _comum.prioridade_a_enviar(item, prioridade_pedida)
        gid = banco.gravar_classificacao(  # antes de anotar: a nota vive no vigia.db (D-13)
            msg_id=msg_id, conversa=conversa, contato=contato, ts=ts, classe="lembrando", resumo=resumo,
            confianca=confianca, item_id=int(item_ref), imagem=imagem)
        ponte.anotar(int(item_ref), resumo, prioridade=enviar)
        return {"ok": True, "repetido": False, "id": gid, "msg_id": msg_id, "classe": "lembrando",
                "item_id": int(item_ref), "criou_item": False, "prioridade": enviar}

    frente, confianca, frente_de = aplicar_vinculo(banco, conversa, frente, confianca)

    velho, motivo, nao_substituiu = _cartao_anterior(banco, conversa, substitui, cancela)
    mapa = mapa_frentes(classe) if cria_item(classe) else None
    if motivo == "atualizada" and mapa is None:
        raise ValueError("--substitui exige classe que vira item (atencao, demanda, lembrar); evento cancelado = --cancela")
    invalida = False
    if mapa is not None and frente is not None and frente not in {f.get("id") for f in ponte.frentes()}:
        invalida, frente, confianca = True, None, "baixa"   # frente arquivada/inexistente: cai na Inbox (confiança baixa)
    # pedido 79: seção só vale se for da frente final; senão o item sobe sem seção (nunca na seção de outra frente)
    secao_pedida = _g(args, "secao")
    secao = secoes.valida_para(banco, secao_pedida, frente) if mapa is not None else None

    # D-57/D-58: o item do cartão antigo é herdado (ganha a versão nova), cancelado ou deixado como está (já fechou).
    heranca = _herdar_item(ponte, velho, motivo, contato, resumo, prazo) if velho is not None else {"item_id": None, "frentes": None}
    item_herdado = heranca["item_id"] if (mapa is not None and motivo == "atualizada") else None

    gid = banco.gravar_classificacao(
        msg_id=msg_id, conversa=conversa, contato=contato, ts=ts, classe=classe, resumo=resumo,
        confianca=confianca, frente_id=(velho.get("frente_id") if item_herdado is not None else frente),
        item_id=item_herdado, prazo=prazo, imagem=imagem, secao_id=(None if item_herdado is not None else secao))
    if velho is not None:
        banco.substituir(velho["id"], gid, motivo)
    item_id, lancado = item_herdado, item_herdado is not None
    if item_herdado is not None:
        banco.marcar_lancado(gid, item_herdado, frente_id=velho.get("frente_id"))
        frente = velho.get("frente_id")
    elif mapa is not None and lancamento.lanca_na_hora(classe, prazo):
        # Frentes fora aqui = exceção (exit 1) com o cartão já gravado como provisório: sobe no fechamento do dia.
        r = lancamento.lancar(banco, ponte, banco.classificacao(gid))
        item_id, lancado = r["item_id"], True
        if r["frente_invalida"]:
            invalida, frente, confianca = True, None, "baixa"
    saida = {"ok": True, "repetido": False, "id": gid, "msg_id": msg_id, "classe": classe,
             "item_id": item_id, "criou_item": item_id is not None, "cartao_item": mapa is not None,
             "lancado": lancado, "prioridade": (mapa or {}).get("prioridade"),
             "frente": frente, "frente_de": frente_de, "confianca": confianca}
    if secao is not None and item_herdado is None and not invalida:
        saida["secao"] = secao
    if _g(args, "anexar") == "sim" and mapa is not None:
        # pedido 80: a ponte não fala com a Evolution; o pedido fica no banco e o coletor baixa o arquivo no fim do ciclo
        alvo = secao if (secao is not None and not invalida) else None
        if alvo is None and item_herdado is not None:
            alvo = secoes.valida_para(banco, secao_pedida, velho.get("frente_id"))
        if alvo is not None:
            banco.pedir_anexo(msg_id, alvo)
            saida["anexo"] = "pedido"
        else:
            saida["anexo"] = "sem_secao"     # arquivo só é guardado dentro de uma seção
    elif secao_pedida is not None and mapa is not None and secao is None:
        saida["secao_invalida"] = True   # não é seção desta frente: subiu sem seção
    if mapa is not None and not lancado:
        saida["provisorio"] = True   # sobe ao Frentes no fechamento do dia, sozinho (D-58)
    if invalida:
        saida["frente_invalida"] = True
    if velho is not None:
        saida["substituiu"] = velho["id"]
        saida["substituicao"] = motivo
        saida["frentes"] = heranca["frentes"]
    if nao_substituiu:
        saida["nao_substituiu"] = nao_substituiu
    return saida


def _cartao_anterior(banco, conversa: str, substitui, cancela):
    """(cartão antigo, motivo, aviso): valida --substitui/--cancela. Cartão de outra conversa ou inexistente = erro.
    Cartão recusado pelo dono é intocável: não se substitui, o comando segue e avisa (`nao_substituiu`)."""
    if substitui is None and cancela is None:
        return None, None, None
    ref, motivo = (substitui, "atualizada") if substitui is not None else (cancela, "cancelada")
    velho = banco.classificacao(int(ref))
    if velho is None:
        raise ValueError("cartão a substituir não existe")
    if velho.get("conversa") != conversa:
        raise ValueError("cartão a substituir é de outra conversa")
    if velho.get("decisao") == "recusado":
        return None, None, "cartao_recusado_pelo_dono"
    if velho.get("substituida_por") is not None:
        raise ValueError("cartão já substituído; use o id do cartão atual")
    return velho, motivo, None


def _herdar_item(ponte, velho: dict, motivo: str, contato, resumo, prazo) -> dict:
    """O que acontece com o item do Frentes do cartão substituído. Devolve {"item_id", "frentes"}:
    nenhum (cartão provisório, sem item) · fechado (item já feito/cancelado: o novo cartão segue sozinho) ·
    atualizado (texto/prazo novos) · corrigido_editado (D-58: o dono tinha editado o texto; as palavras dele ficam e a
    correção entra depois) · cancelado (D-58: também quando o dono editou; Restaurar reabre)."""
    item_ref = velho.get("item_id")
    if item_ref is None:
        return {"item_id": None, "frentes": "nenhum"}
    item = _comum.item_whatsapp_aberto(ponte, int(item_ref))
    if item is None:
        return {"item_id": None, "frentes": "fechado"}
    if motivo == "cancelada":
        ponte.alterar(int(item_ref), status="cancelado")
        return {"item_id": None, "frentes": "cancelado"}
    rotulo = lancamento.corrigir_item(ponte, dict(item, id=int(item_ref)), velho, contato, resumo, prazo)
    return {"item_id": int(item_ref), "frentes": rotulo}
