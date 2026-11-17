"""Página /conexao (estilo Papel, pedido 39): um painel só, com o estado em lista rótulo/valor, "Gerar QR" como única ação
primária, o menu ··· (pausar, segurar a limpeza, Métricas, reiniciar) e o QR só enquanto há um em andamento.
Quem chama: app/server.py (via ROTAS) e o indicador do rodapé da lateral (_comum.estado_conexao lê só o estado local).
Só o POST /conexao/qr consome QR (R-11, P-3).
O que não faz: GET nunca gera QR, não há polling de QR nem renovação automática fora do clique; não loga conteúdo de conversa.
"""
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from app import agenda_leitura
from app.erros import VigiaErro, causa
from app.http import json_resposta
from app.paginas import _comum as c
from app.semana import segurar, semana_fechada

log = logging.getLogger("vigia.painel.conexao")
INTERVALO_LEITURA_MIN = 30


def _evolution_estado(ctx) -> str:
    """estado() da Evolution (leitura pura, não consome QR); falha vira texto, nunca exceção."""
    try:
        return str(ctx.evo.estado().get("state", "?"))
    except VigiaErro as e:
        return "indisponível (" + type(e).__name__ + ")"


def _ultimo_lote(ctx) -> tuple[str, str]:
    """(quando, resumo) do último lote: ("Hoje às 16:28", "3 conversas, 7 mensagens novas, 2 demandas, 1 atenção")."""
    lotes = ctx.banco.lotes(1)
    if not lotes:
        return "Nenhum lote registrado", ""
    l = lotes[0]
    quando = _quando_iso(l.get("criado_em"))
    partes = [f"{c.esc(l.get('conversas'))} conversas", f"{c.esc(l.get('novas'))} mensagens novas",
              f"{c.esc(l.get('itens'))} demandas"]
    if l.get("atencao"):
        partes.append(f"{c.esc(l.get('atencao'))} atenção")
    return quando, ", ".join(partes)


def _quando_iso(iso) -> str:
    """'2026-11-08 16:28:03' -> 'Hoje às 16:28'."""
    try:
        d = datetime.strptime(str(iso)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return c.esc(iso or "-")
    return c.quando_longo(int(d.timestamp()))


def proxima_leitura(ctx) -> str:
    """Texto do rodapé das Demandas e da lateral: 'Próxima leitura às 17:28' a partir do último lote (a cada 30 min),
    'Leituras pausadas' com dados/PAUSADO, 'Leitura ainda não rodou' sem lote."""
    try:
        if (c.dados_dir(ctx) / "PAUSADO").exists():
            return "Leituras pausadas"
        lotes = ctx.banco.lotes(1)
    except Exception as e:  # noqa: BLE001  (rodapé nunca derruba a página)
        log.debug("próxima leitura ilegível: %s", causa(e))
        return ""
    if not lotes:
        return "Leitura ainda não rodou"
    pela_agenda = agenda_leitura.proxima_fora_do_padrao(c.dados_dir(ctx))   # pedido 81: intervalo ou horário escolhido
    if pela_agenda:
        return pela_agenda
    try:
        d = datetime.strptime(str(lotes[0].get("criado_em"))[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ""
    prox = d + timedelta(minutes=INTERVALO_LEITURA_MIN)
    if prox < datetime.now():
        return "Próxima leitura em instantes"
    return f"Próxima leitura às {prox.hour}:{prox.minute:02d}"


def _aviso_inline(texto: str) -> str:
    return f'<div class="aviso-inline" role="status">{texto}</div>'


# pedido 75: o erro traz o botão que resolve, em vez de mandar a pessoa procurar no menu ···
BT_REINICIAR = '<button type="submit" class="btn" form="f-reiniciar">Reiniciar Evolution</button>'
BT_QR = '<form method="post" action="/conexao/qr"><button type="submit" class="btn primario">Gerar QR</button></form>'
ESPERA_LEITURA_S = 150      # o WhatsApp levou 2 min 13 s para mandar o histórico no pareamento de 13/11/2026
ESPERA_REINICIO_S = 75      # a API da Evolution voltou 38 s depois do reinício em 13/11/2026


def _avisos_monitor(cx, monitor: dict, agora: float) -> str:
    """Aviso vermelho se o monitor viu socket morto (428), socket zumbi (sonda, pedido 41) ou queda com reason_code
    recente; âmbar para silêncio longo com a conexão aberta e para o reinício automático que acabou de acontecer."""
    rc = monitor.get("reason_code")
    atualizado = monitor.get("atualizado_em")
    recente = isinstance(atualizado, (int, float)) and 0 <= agora - atualizado <= getattr(cx, "janela_428_s", 600)
    evento, motivo = monitor.get("evento"), str(monitor.get("motivo") or "")
    if evento == "socket_morto":
        return c.erro_inline("A ligação com o WhatsApp travou (428). Reiniciar a Evolution resolve.", BT_REINICIAR)
    if evento == "socket_zumbi":
        quando = c.hora(monitor.get("reiniciado_em")) if monitor.get("reiniciado_em") else ""
        if motivo.endswith("_reiniciada"):
            return _aviso_inline(f"O socket não respondeu à sonda{(' às ' + c.esc(quando)) if quando else ''}: a Evolution foi "
                                 "reiniciada sozinha. Se as mensagens não voltarem no próximo ciclo, use Parear de novo… no menu ···.")
        if motivo.endswith("_reinicio_falhou"):
            return c.erro_inline("O socket não responde e o reinício automático falhou (veja o log). "
                                 "Reinicie a Evolution; se não voltar, use Parear de novo… no menu ···.", BT_REINICIAR)
        return c.erro_inline("O socket continua sem resposta depois do reinício automático: a Evolution diz \"open\", mas o "
                             "WhatsApp não entrega nada. Use Parear de novo… no menu ··· com o celular na mão.")
    if evento == "entrega_travada":
        # D-56: a Evolution diz "open" e a sonda responde, mas o teste de entrega mandado pelo bot não apareceu no Postgres
        if motivo.endswith("_reinicio_falhou"):
            return c.erro_inline("A conexão está aberta, mas a entrega travou: o teste de entrega não chegou e o reinício "
                                 "automático falhou (veja o log). Reinicie a Evolution; se não voltar, use Parear de "
                                 "novo… no menu ···.", BT_REINICIAR)
        if motivo.endswith("_reiniciada"):
            return c.erro_inline("A conexão está aberta, mas a entrega travou: o teste de entrega não chegou nem depois do "
                                 "reinício automático. Use Parear de novo… no menu ··· com o celular na mão.")
        return c.erro_inline("A conexão está aberta, mas a entrega travou: o teste de entrega mandado pelo bot não chegou. "
                             "Use Parear de novo… no menu ··· com o celular na mão.")
    if evento == "silencio":
        h = motivo[len("sem_mensagem_"):].rstrip("h") if motivo.startswith("sem_mensagem_") else "?"
        return _aviso_inline(f"Conexão aberta, mas nenhuma mensagem nova há {c.esc(h)} h. Confira no celular, em "
                             "WhatsApp › Aparelhos conectados, se o Greg aparece como ativo; se não aparecer, use Parear de novo… no menu ···.")
    if rc is not None and recente and evento == "queda":
        if rc == 401:   # o WhatsApp encerrou a sessão: reiniciar não devolve, só um QR novo
            return c.erro_inline("O WhatsApp encerrou a sessão do Greg (401). Reiniciar não resolve: gere um QR e "
                                 "escaneie com o celular.", "" if getattr(cx, "estado", "") in ("gerando", "renovando") else BT_QR)
        return c.erro_inline(f"Conexão caída (código {rc}). Se não voltar sozinha em alguns minutos, reinicie a Evolution.",
                             BT_REINICIAR)
    return ""


def _ultima_mensagem(cx, agora: float) -> str:
    """Valor da linha "Última mensagem recebida": quando + há quanto tempo, do dados/ultima-mensagem.json do coletor."""
    fn = getattr(cx, "ultima_mensagem_em", None)
    ts = fn() if callable(fn) else None
    if not ts:
        return 'Ainda não conferida<span class="sub">O coletor anota a cada leitura</span>'
    atraso = max(0, int(agora - ts))
    cls = ' class="ruim"' if atraso >= 6 * 3600 else ""
    return f'<b{cls}>{c.esc(c.quando_longo(ts))}</b><span class="sub">há {c.esc(c.duracao(atraso))}</span>'


def _semana_texto(ctx) -> tuple[str, str]:
    """(texto, rótulo da semana) do estado do 'segurar': até quando a limpeza da semana fechada está adiada."""
    semana = semana_fechada()
    dados = Path(ctx.banco.caminho).resolve().parent
    if (dados / "SEGURA").exists():
        return f"Semana {c.esc(semana)}: adiada por tempo indeterminado (arquivo SEGURA)", semana
    from app import semana as sem
    prazo = sem.prazo_dias(dados)
    if prazo == 0:   # pedido 74: "Não apagar" em Ajustes
        return "Desligada em Ajustes: nenhuma mensagem é apagada", semana
    marca = dados / f"segurar-{semana}"
    if not marca.exists():
        if prazo > 7:   # a tela não promete limpeza no domingo quando o prazo escolhido é maior
            return f"Semana {c.esc(semana)}: guardada por {prazo} dias, como está em Ajustes; sai depois disso", semana
        return f"Semana {c.esc(semana)}: no prazo normal, domingo às 3h", semana
    try:
        ate = float(marca.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return f"Semana {c.esc(semana)}: segurada (marca ilegível, tratada como segurada)", semana
    if ate <= time.time():
        return f"Semana {c.esc(semana)}: a espera acabou em {c.hora(ate)}; a limpeza volta à fila", semana
    return f"Semana {c.esc(semana)}: segurada até {c.hora(ate)}", semana


def _menu(pausado: bool, conectado: bool = False) -> str:
    itens = ((c.item_menu("Ler agora", attrs='form="f-ler"') if not pausado else "")
             + c.item_menu("Retomar leituras" if pausado else "Pausar leituras", attrs='form="f-pausar"', atalho="⌘⇧P")
             + c.item_menu("Segurar limpeza desta semana", attrs='form="f-segurar"')
             + c.item_menu("Métricas", href="/metricas")
             + c.item_menu("Saúde do Greg", href="/avisos")
             + c.separador_menu()
             + c.item_menu("Reiniciar Evolution…", attrs='form="f-reiniciar"', destrutivo=True)
             + (c.item_menu("Parear de novo…", attrs='form="f-desparear"', destrutivo=True) if conectado else ""))
    forms = ('<form id="f-ler" method="post" action="/conexao/ler-agora" hidden></form>'
             '<form id="f-pausar" method="post" action="/conexao/pausar" hidden></form>'
             '<form id="f-segurar" method="post" action="/conexao/segurar" hidden></form>'
             '<form id="f-reiniciar" method="post" action="/conexao/reiniciar" hidden '
             'data-confirmar="Reiniciar a Evolution? A conexão com o WhatsApp cai por cerca de um minuto."></form>'
             '<form id="f-desparear" method="post" action="/conexao/desparear" hidden '
             'data-confirmar="Encerrar a sessão do WhatsApp? Você vai precisar escanear um QR novo com o celular na mão. '
             'O que já foi lido fica."></form>'
             # lote 5: reiniciar leva até um minuto e a tela ficava parada sem dizer nada
             '<script>(function () { var f = document.getElementById("f-reiniciar"); if (!f) { return; } '
             'f.addEventListener("submit", function (ev) { setTimeout(function () { if (!ev.defaultPrevented && window.vigiaToast) { '
             'window.vigiaToast("Reiniciando a Evolution. Leva cerca de um minuto; esta tela volta sozinha."); } }, 0); }); '
             # pedido 75: gerar o QR pode levar um minuto quando o Greg precisa reiniciar a Evolution sozinho
             'document.addEventListener("submit", function (ev) { var a = ev.target && ev.target.getAttribute && ev.target.getAttribute("action"); '
             'if (a !== "/conexao/qr") { return; } setTimeout(function () { if (!ev.defaultPrevented && window.vigiaToast) { '
             'window.vigiaToast("Preparando o QR. Se a Evolution precisar reiniciar, leva até um minuto."); } }, 1500); }); })();</script>')
    return c.menu_icone("mais", "Mais ações", itens, id_="cx-menu") + forms


def _pagina(req, *, erro: str = "", status: int = 200):
    ctx = req.ctx
    cx = ctx.conexao
    estado = cx.estado
    pausado = (c.dados_dir(ctx) / "PAUSADO").exists()
    qrs = ctx.banco.qrs_hoje()
    teto = getattr(cx, "teto_dia", 10)
    evolution = _evolution_estado(ctx)
    ativo = estado in ("gerando", "renovando")
    vigente = cx.qr_vigente() if ativo else None
    src = ""
    if vigente and str(vigente.get("base64", "")).startswith("data:image/"):
        src = c.esc(vigente["base64"])
    ttl = cx.ttl() if ativo and src else 0
    restantes = vigente["restantes"] if vigente else 0
    qr_max = int(getattr(cx, "qr_max", 3))
    conectado = estado == "conectado" or (estado == "ocioso" and evolution == "open")
    if conectado:
        wa, wa_sub = "Conectado", "Evolution aberta"
    elif ativo:
        wa, wa_sub = "Aguardando o QR", f"Evolution: {c.esc(evolution)}"
    elif estado == "expirado":
        wa, wa_sub = "Desconectado", "QR expirado; gere outro"
    else:
        wa, wa_sub = "Desconectado", f"Evolution: {c.esc(evolution)}"
    primeiro_uso = getattr(getattr(cx, "cursor", None), "pareado_em", None) is None and not conectado
    if conectado:
        motivo_botao = "já conectado"
    elif qrs >= teto:
        motivo_botao = "teto do dia"
    elif ativo:
        motivo_botao = "QR em andamento"
    else:
        motivo_botao = ""
    avisos = c.erro_inline(erro) if erro else ""
    _agendar_leitura_do_pareamento(ctx)
    para = getattr(ctx, "leitura_agendada_para", 0) or 0
    if conectado and para > time.time():   # pedido 75: a pessoa acabou de parear e sabe o que vem agora
        avisos += _aviso_inline(f"WhatsApp conectado. O Greg lê as conversas sozinho às {c.esc(time.strftime('%H:%M', time.localtime(para)))}, "
                                "quando o WhatsApp terminar de mandar o histórico. Não precisa fazer nada.")
    if estado == "expirado" or (c.query(req, "expirou") and not ativo):
        avisos += '<div class="aviso-inline" role="status">QR expirou. Gere outro quando estiver com o celular na mão.</div>'
    if c.query(req, "logout") == "0" or (estado in ("expirado", "ocioso") and evolution == "connecting"):
        avisos += c.erro_inline("A Evolution ficou presa tentando conectar. Reiniciar resolve.", BT_REINICIAR)
    if qrs >= teto and not conectado:
        avisos += '<div class="aviso-inline" role="status">Teto de QRs do dia atingido. Tente amanhã.</div>'
    monitor = cx.ultimo_monitor() or {}
    avisos += _avisos_monitor(cx, monitor, time.time())
    ciclo = monitor.get("atualizado_em")
    semana_txt, _ = _semana_texto(ctx)
    ultimo_quando, ultimo_resumo = _ultimo_lote(ctx)
    toast = ""
    if c.query(req, "reiniciado"):
        toast = "Evolution reiniciada. A conexão volta em até um minuto."
    elif c.query(req, "pausado") == "1":
        toast = "Leituras pausadas. O monitor não lê até você retomar."
    elif c.query(req, "pausado") == "0":
        toast = "Leituras retomadas."
    elif c.query(req, "segurado"):
        toast = "Limpeza desta semana segurada por 7 dias. Nada foi apagado."
    elif c.query(req, "lendo") == "1":
        toast = toast_ler(ctx)
    elif c.query(req, "lendo") == "0":
        toast = TOAST_PAUSADO
    elif c.query(req, "despareado"):
        toast = "Sessão do WhatsApp encerrada. Gere o QR com o celular na mão."
    kv = c.kv([
        ("WhatsApp", f'<b class="{"ok" if conectado else ""}">{wa}</b><span class="sub">{wa_sub}</span>'),
        ("Última mensagem recebida", _ultima_mensagem(cx, time.time())),
        ("Leitura automática", f'<b>{"Pausada" if pausado else "Ativa"}</b><span class="sub">'
                               + ("Até você retomar, no menu ···" if pausado else f"{c.esc(agenda_leitura.texto_escolha(*agenda_leitura.escolha(c.dados_dir(ctx))))} " + ("com o contêiner no ar" if os.environ.get("VIGIA_CONTAINER") == "1" else "com o Mac ligado")) + "</span>"),
        ("Última leitura", f"{ultimo_quando}" + (f'<span class="sub">{ultimo_resumo}</span>' if ultimo_resumo else "")),
        ("Último ciclo do monitor", c.esc(c.quando_longo(ciclo)) if ciclo else "Ainda não rodou"),
        ("QRs gerados hoje", f'<b id="qrs-hoje" class="{"ruim" if qrs >= teto else ""}">{qrs}</b> de {teto}'),
        ("Limpeza semanal", semana_txt),
    ])
    parear = ("Primeiro uso: gere o QR, abra <b>Aparelhos conectados</b> no celular e escaneie. "
              if primeiro_uso else "Gere o QR, abra <b>Aparelhos conectados</b> no celular e escaneie. ")
    parear += f"O código vale {int(cx.qr_segundos)} segundos e renova até {qr_max} vezes."
    corpo = c.render(
        "conexao.html",
        avisos=avisos, kv=kv, parear=parear,
        estado_cru=c.esc(estado), teto=teto, ttl=ttl, restantes=restantes, qr_max=qr_max, qr_segundos=int(cx.qr_segundos),
        qr_visivel="" if ttl > 0 else " hidden", qr_src=src,
        qr_mensagem=f"QR {qr_max - restantes} de {qr_max}, expira em {ttl} s" if ttl > 0 else "")
    botao_qr = c.botao("Gerar QR", classe="primario", tipo="submit",
                       attrs=(f'disabled title="{c.esc(motivo_botao)}"' if motivo_botao else ""))
    toolbar = c.toolbar(c.botao_icone("voltar", "Voltar para Demandas", href="/demandas"), c.titulo("Conexão"), c.espaco(),
                        f'<form method="post" action="/conexao/qr" class="acoes" id="f-qr">{botao_qr}</form>',
                        _menu(pausado, conectado=conectado))
    painel = c.painel(corpo, toolbar_html=toolbar, status_html=c.rodape("Cada QR gasto aproxima o número do bloqueio"),
                      aria="Conexão", id_="conexao")
    return c.pagina("Conexão", painel, "/conexao", status, ctx=ctx, toast=toast)


def conexao_get(req):
    """Mostra estado(); NUNCA gera QR."""
    if (e := c.metodo_errado(req, "GET", "HEAD")):
        return e
    return _pagina(req)


def qr_post(req):
    """Clique em Gerar QR: exatamente 1 iniciar_qr(); sucesso responde 303 -> /conexao (o GET desenha o QR vigente)."""
    if (e := c.metodo_errado(req, "POST")):
        return e
    if not c.origem_ok(req):
        return c.proibido()
    cx = req.ctx.conexao
    try:
        cx.iniciar_qr()
    except VigiaErro as e:  # TetoQR, QRIndisponivel, EvolutionIndisponivel…
        log.warning("gerar qr recusado: %s", causa(e))
        nome = type(e).__name__
        if nome == "TetoQR":
            return _pagina(req, erro="Teto diário de QRs atingido; tente amanhã.", status=429)
        # pedido 75: a Evolution não entregou o QR. Em vez de mandar a pessoa achar "Reiniciar Evolution" no menu, o
        # Greg reinicia sozinho, espera a API voltar e pede o QR de novo. Uma tentativa por clique.
        if _reiniciar_e_gerar_qr(req.ctx):
            return c.redirecionar("/conexao")
        return _pagina(req, erro="A Evolution não devolveu o QR, nem depois de reiniciada sozinha. Espere um minuto e "
                                 "gere o QR de novo; se repetir, abra a Saúde do Greg no menu ···.", status=502)
    return c.redirecionar("/conexao")


def _reiniciar_e_gerar_qr(ctx, dormir=time.sleep, relogio=time.monotonic) -> bool:
    """Reinicia a Evolution e tenta o QR até ESPERA_REINICIO_S. True = QR emitido (ou já conectado). Nunca levanta."""
    cx = ctx.conexao

    def executor(cmd, cwd=None):
        return subprocess.run(cmd, cwd=cwd, check=True, timeout=180, capture_output=True)

    try:
        cx.reiniciar(getattr(ctx, "executor", None) or executor)
    except (VigiaErro, subprocess.SubprocessError, OSError) as e:
        log.warning("reinício automático antes do QR falhou: %s", causa(e))
        return False
    log.info("evolution reiniciada sozinha para gerar o QR")
    fim = relogio() + ESPERA_REINICIO_S
    while relogio() < fim:
        dormir(5)
        try:
            cx.iniciar_qr()
            return True
        except VigiaErro as e:
            if type(e).__name__ == "TetoQR":
                return False
            ultimo = causa(e)
    log.warning("qr não saiu depois do reinício automático: %s", ultimo if "ultimo" in locals() else "sem tentativa")
    return False


def disparar_leitura(ctx) -> bool:
    """Dispara um ciclo de leitura agora (o mesmo do botão Ler agora). False = pausado ou não deu para disparar."""
    dados = c.dados_dir(ctx)
    if (dados / "PAUSADO").exists():
        return False
    try:      # pedido 81: esta leitura sai fora do intervalo e do horário escolhidos em Ajustes
        agenda_leitura.pedir(dados)
    except OSError as e:
        log.warning("sinal de leitura pedida não gravado: %s", causa(e))   # o disparo segue; vale a agenda
    try:
        if os.environ.get("VIGIA_CONTAINER") == "1":
            (dados / "LER-AGORA").touch()
            return True
        cmd = ["launchctl", "kickstart", f"gui/{os.getuid()}/{LABEL_COLETOR}"]
        executor = getattr(ctx, "executor", None) or (
            lambda cmd, cwd=None: subprocess.run(cmd, cwd=cwd, check=True, timeout=30, capture_output=True))
        executor(cmd, cwd=getattr(ctx, "raiz", None))
        return True
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("leitura automática não disparou: %s", causa(e))
        return False


def _agendar_leitura_do_pareamento(ctx) -> None:
    """Pedido 75: pareou (primeira vez ou de novo) → a leitura sai sozinha, sem esperar a meia hora nem o clique.
    Espera ESPERA_LEITURA_S porque o WhatsApp ainda está mandando o histórico; uma vez por pareamento."""
    cx = ctx.conexao
    em = getattr(cx, "pareou_em", None)
    if not em or not getattr(ctx, "leitura_automatica", False) or getattr(ctx, "leitura_do_pareamento", None) == em:
        return
    ctx.leitura_do_pareamento = em
    ctx.leitura_agendada_para = time.time() + ESPERA_LEITURA_S
    t = threading.Timer(ESPERA_LEITURA_S, lambda: log.info("leitura automática do pareamento: %s",
                                                           "disparada" if disparar_leitura(ctx) else "não disparada"))
    t.daemon = True
    t.start()
    log.info("pareamento novo: leitura automática em %d s", ESPERA_LEITURA_S)


def tick_get(req):
    """Chamado pelo JS a cada 5 s só enquanto gerando/renovando; consulta estado(), nunca gera QR novo por GET
    (a renovação dentro do tick é da máquina de estado, limitada a qr_max por clique)."""
    if (e := c.metodo_errado(req, "GET")):
        return e
    if not c.origem_ok(req):  # GET com efeito (renova, expira, faz logout): mesma defesa do POST
        return c.proibido()
    cx = req.ctx.conexao
    try:
        r = cx.tick()
    except VigiaErro as e:
        log.warning("tick falhou: %s", causa(e))
        return json_resposta({"erro": "evolution indisponível", "estado": cx.estado}, 502)
    saida = {k: r.get(k) for k in ("estado", "base64", "restantes", "qrs_hoje", "evolution", "motivo",
                                   "logout_confirmado")}
    saida["ttl"] = cx.ttl()
    _agendar_leitura_do_pareamento(req.ctx)
    return json_resposta(saida)


def lateral_get(req):
    """GET /conexao/lateral (pedido 43): {"classe","texto","demandas"} da lateral, lidos do disco e do banco (nunca chama a
    Evolution). O JS da base chama a cada 30 s e ao voltar à aba, para o indicador e o contador mudarem sem recarregar."""
    if (e := c.metodo_errado(req, "GET")):
        return e
    return json_resposta(c.estado_lateral(req.ctx))


def segurar_post(req):
    """D-29: adia a limpeza da semana fechada (semana.segurar); nenhum código lê resposta do WhatsApp."""
    if (e := c.metodo_errado(req, "POST")):
        return e
    if not c.origem_ok(req):
        return c.proibido()
    try:
        segurar(req.ctx.banco, semana_fechada())
    except (OSError, ValueError) as e:
        log.warning("segurar semana falhou: %s", causa(e))
        return _pagina(req, erro="Não consegui segurar a semana (veja o log).", status=500)
    return c.redirecionar("/conexao?segurado=1")


def reiniciar_post(req):
    if (e := c.metodo_errado(req, "POST")):
        return e
    if not c.origem_ok(req):
        return c.proibido()

    def executor(cmd, cwd=None):
        return subprocess.run(cmd, cwd=cwd, check=True, timeout=180, capture_output=True)

    try:
        req.ctx.conexao.reiniciar(getattr(req.ctx, "executor", None) or executor)
    except (VigiaErro, subprocess.SubprocessError, OSError) as e:
        log.warning("reiniciar falhou: %s", causa(e))
        return _pagina(req, erro="Falha ao reiniciar a Evolution (veja o log).", status=502)
    return c.redirecionar("/conexao?reiniciado=1")


def pausar_post(req):
    """Alterna dados/PAUSADO (o vigia.sh sai 0 sem ler enquanto o arquivo existir). Com X-Vigia-Auto (⌘⇧P de qualquer
    tela) responde JSON {"ok", "pausado"}; senão volta à Conexão com o toast."""
    if (e := c.metodo_errado(req, "POST")):
        return e
    if not c.origem_ok(req):
        return c.proibido()
    pasta = c.dados_dir(req.ctx)
    pasta.mkdir(parents=True, exist_ok=True, mode=0o700)
    arq = pasta / "PAUSADO"
    if arq.exists():
        arq.unlink()
        pausado = False
    else:
        arq.touch(mode=0o600)
        pausado = True
    if c.pediu_json(req):
        return json_resposta({"ok": True, "pausado": pausado})
    return c.redirecionar(f"/conexao?pausado={1 if pausado else 0}")


LABEL_COLETOR = "io.vigia.vigia-whatsapp"   # LaunchAgent do ciclo (bin/instalar-launchagent.sh)
TOAST_LENDO = "Leitura disparada. O que chegou aparece em Demandas em até um minuto."
# pedido 62: com o login do Claude vencido a leitura é disparada mas não classifica nada; o botão diz isso na hora
TOAST_SEM_LOGIN = "A IA não vai ler: o login do Claude neste Mac expirou. No Terminal, rode: claude auth login."


def toast_ler(ctx) -> str:
    """O que o botão Ler agora responde: com aviso da IA aberto na central (login, limite, Claude ausente), diz que a
    leitura não vai classificar e por quê; senão, que a leitura foi disparada."""
    aviso = c.aviso_leitura(ctx)
    if not aviso:
        return TOAST_LENDO
    return TOAST_SEM_LOGIN if "login do Claude" in aviso else "A IA não vai ler agora. " + aviso
TOAST_PAUSADO = "Leituras pausadas. Retome na Conexão (menu ···) antes de ler agora."
_VOLTA_LER = {"/demandas", "/conexao", "/conversas"}


def ler_agora_post(req):
    """Pedido 41 (tempo real): dispara o ciclo do coletor agora, pelo LaunchAgent (`launchctl kickstart`), em vez de
    esperar a meia hora. A trava do vigia.sh impede dois ciclos ao mesmo tempo. Com PAUSADO não dispara (volta com aviso).
    `volta` só /demandas ou /conexao. Com X-Vigia-Auto responde JSON {"ok", "disparado"}."""
    if (e := c.metodo_errado(req, "POST")):
        return e
    if not c.origem_ok(req):
        return c.proibido()
    volta = str((req.corpo or {}).get("volta") or "/conexao")
    if volta not in _VOLTA_LER:
        volta = "/conexao"
    pausado = (c.dados_dir(req.ctx) / "PAUSADO").exists()
    if pausado:
        if c.pediu_json(req):
            return json_resposta({"ok": True, "disparado": False, "toast": TOAST_PAUSADO})
        return c.redirecionar(f"{volta}?lendo=0")

    try:      # pedido 81: "Ler agora" vale fora do intervalo e do horário escolhidos em Ajustes
        agenda_leitura.pedir(c.dados_dir(req.ctx))
    except OSError as e:
        log.warning("sinal de leitura pedida não gravado: %s", causa(e))   # o disparo segue; vale a agenda
    if os.environ.get("VIGIA_CONTAINER") == "1":   # Docker: sem launchctl; o bin/agendador.py vê o gatilho em até 5 s
        try:
            (c.dados_dir(req.ctx) / "LER-AGORA").touch()
        except OSError as e:
            log.warning("ler agora falhou: %s", causa(e))
            if c.pediu_json(req):
                return json_resposta({"ok": False, "erro": "não deu para disparar a leitura"}, 502)
            return _pagina(req, erro="Não deu para disparar a leitura agora (veja o log).", status=502)
        if c.pediu_json(req):
            return json_resposta({"ok": True, "disparado": True, "toast": toast_ler(req.ctx)})
        return c.redirecionar(f"{volta}?lendo=1")

    def executor(cmd, cwd=None):
        return subprocess.run(cmd, cwd=cwd, check=True, timeout=30, capture_output=True)

    cmd = ["launchctl", "kickstart", f"gui/{os.getuid()}/{LABEL_COLETOR}"]
    try:
        (getattr(req.ctx, "executor", None) or executor)(cmd, cwd=getattr(req.ctx, "raiz", None))
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("ler agora falhou: %s", causa(e))
        if c.pediu_json(req):
            return json_resposta({"ok": False, "erro": "não deu para disparar a leitura"}, 502)
        return _pagina(req, erro="Não deu para disparar a leitura agora (veja o log).", status=502)
    if c.pediu_json(req):
        return json_resposta({"ok": True, "disparado": True, "toast": toast_ler(req.ctx)})
    return c.redirecionar(f"{volta}?lendo=1")


def desparear_post(req):
    """Pedido 41: "Parear de novo…" — logout na Evolution (DELETE /instance/logout) para o socket zumbi que o reinício não
    resolve; a página volta com o botão Gerar QR liberado. Só por clique dele (R-11: nenhuma saída de sessão automática
    fora dos casos do monitor); `ultimo_seq` fica (a leitura continua de onde parou) e `pareado_em` é regravado quando o QR
    novo for lido (D-53: a janela de 1 dia acompanha cada pareamento)."""
    if (e := c.metodo_errado(req, "POST")):
        return e
    if not c.origem_ok(req):
        return c.proibido()
    try:
        req.ctx.evo.logout()
    except VigiaErro as e:
        log.warning("desparear falhou: %s", causa(e))
        return _pagina(req, erro="A Evolution não encerrou a sessão (veja o log). Tente Reiniciar Evolution.", status=502)
    cx = req.ctx.conexao
    with cx._trava:
        cx.estado = "ocioso"
        cx._qr = None
    return c.redirecionar("/conexao?despareado=1")


ROTAS = {"/conexao": conexao_get, "/conexao/qr": qr_post, "/conexao/tick": tick_get,
         "/conexao/reiniciar": reiniciar_post, "/conexao/pausar": pausar_post,
         "/conexao/segurar": segurar_post, "/conexao/ler-agora": ler_agora_post,
         "/conexao/desparear": desparear_post, "/conexao/lateral": lateral_get}
