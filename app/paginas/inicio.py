"""Primeira instalação guiada (pedido 69): as telas do assistente, uma por passo.
Quem chama: app/server.py (via ROTAS), app/paginas/ajustes.py (`html_grupo`, a seção "Início" de Ajustes).
Rotas: GET /inicio (vai ao passo em que a pessoa parou) e GET /inicio/<passo>; POST /inicio/avancar (Continuar ou Pular),
/inicio/whatsapp/qr (o clique que gasta QR, R-11), /inicio/ia/testar, /inicio/audio (WAV gravado -> texto),
/inicio/sugestao/pedir, GET /inicio/sugestao/estado, POST /inicio/revisao (o ÚNICO lugar que cria frente e vínculo),
/inicio/sair, /inicio/refazer, /inicio/descricao/salvar (Ajustes) e /inicio/roteiro/visto.
O texto de cada passo vem de app/inicio.py (PASSOS). A máquina do QR é a da Conexão (app/conexao.py e as funções de
app/paginas/conexao.py); aqui não há lógica de QR.
O que não faz: GET nunca gera QR, nunca chama a IA e nunca grava frente; nenhum jid vai para o HTML da revisão (os
campos levam só a posição na proposta guardada no servidor); não mostra caminho de arquivo nem o valor de segredo.
"""
import logging
import re
import time

from app import inicio
from app import preferencias as pf
from app import vinculos as vc
from app.erros import VigiaErro, causa
from app.http import json_resposta
from app.paginas import _comum as c
from app.paginas import conexao as pg_conexao

log = logging.getLogger("vigia.painel.inicio")
ROTULO_TIPO = (("cliente", "Cliente"), ("projeto", "Projeto"), ("empresa", "Negócio"), ("contrato", "Contrato"),
               ("interno", "Interno"), ("pessoal", "Pessoal"))
_MARCA = None


def _marca() -> str:
    """O nome Greg desenhado (o mesmo SVG da lateral, lido da base): a lateral não aparece no assistente."""
    global _MARCA
    if _MARCA is None:
        try:
            m = re.search(r'<svg class="nome".*?</svg>', (c.TEMPLATES / "_base.html").read_text(encoding="utf-8"), re.S)
            _MARCA = m.group(0) if m else ""
        except OSError:
            _MARCA = ""
    return _MARCA


# ------------------------------------------------------------------ casca do assistente

_CSS = """<style>
.ini-tema { margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--fio-fraco); }
.ini-tema-op { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
/* pedido 69: no caminho de quem está começando não aparecem a lateral, Ajustes, Sair nem o modo de gravação (T-2) */
.lateral, .lateral + .divisoria, .mostrar-lateral { display: none !important; }
.corpo.ini { background: var(--fundo-2); align-items: center; padding: 28px 20px 40px; }
.ini-topo .marca { padding: 0; }
.ini-topo .marca .nome { height: 22px; }
.ini-trilha { display: flex; gap: 4px; width: 100%; max-width: 620px; margin: 0 0 8px; padding: 0; list-style: none; }
.ini-trilha li { flex: 1; height: 4px; border-radius: 2px; background: var(--seg-fundo); }
.ini-trilha li.feito { background: var(--acento); opacity: .45; }
.ini-trilha li.atual { background: var(--acento); }
.ini-onde { width: 100%; max-width: 620px; margin: 0 0 14px; color: var(--meta); font: var(--t-meta) var(--fonte); }
.ini-onde b { color: var(--texto-2); font-weight: 600; }
.ini-cartao { width: 100%; max-width: 620px; padding: 26px 28px 22px; }
.ini-cartao h1 { margin: 0 0 12px; font: 700 1.6923rem/1.25 var(--fonte); letter-spacing: -.01em; }
.ini-cartao .ini-texto p { margin: 0 0 8px; max-width: 54ch; }
.ini-porque { margin: 14px 0 0; padding: 10px 12px; border-radius: var(--raio-controle); background: var(--fundo-2); color: var(--texto-2); }
.ini-porque b { display: block; font: var(--t-rotulo) var(--fonte); color: var(--rotulo); margin-bottom: 2px; }
.ini-area { margin-top: 18px; display: flex; flex-direction: column; gap: 12px; align-items: flex-start; }
.ini-area:empty { display: none; }
.ini-area .aviso-inline, .ini-area .erro-inline { display: flex; align-items: flex-start; line-height: 1.4; padding: 8px 10px; }
.ini-bom { display: flex; align-items: center; gap: 8px; font-weight: 600; color: var(--ok); }
.ini-bom svg { width: 16px; height: 16px; flex: none; }
.ini-passos { margin: 4px 0 0; padding-left: 18px; }
.ini-passos li { margin: 2px 0; }
.ini-erro { width: 100%; padding: 10px 12px; border-radius: var(--raio-controle); background: var(--atrasado-fundo); color: var(--atrasado-texto); }
.ini-erro b { display: block; }
.ini-acoes { display: flex; align-items: center; gap: 8px; margin-top: 22px; padding-top: 16px; border-top: 1px solid var(--fio-fraco); }
.ini-acoes .espaco { flex: 1; }
.ini-acoes form { display: contents; }
.ini-qr { display: flex; flex-direction: column; gap: 6px; align-items: flex-start; }
.ini-qr img { width: 232px; height: 232px; border-radius: var(--raio-grupo); background: var(--qr-fundo); box-shadow: 0 0 0 .5px var(--grupo-borda); }
.ini-qr #qr-msg { font: var(--t-meta) var(--fonte); color: var(--meta); }
.ini-qr #qr-msg.ruim { color: var(--destrutivo); }
.ini-desc { width: 100%; }
.ini-desc textarea { width: 100%; min-height: 148px; }
.ini-desc .ini-linha { display: flex; align-items: center; gap: 10px; margin-top: 8px; flex-wrap: wrap; }
.ini-desc #ini-audio-msg { color: var(--meta); font: var(--t-meta) var(--fonte); }
.ini-desc #ini-audio-msg.ruim { color: var(--destrutivo); }
.ini-desc .btn[aria-pressed="true"] { color: var(--destrutivo); }
.ini-lista { width: 100%; margin: 0; padding-left: 18px; color: var(--texto-2); }
.ini-lista li { margin: 3px 0; }
.ini-espera { width: 100%; }
.ini-espera .esqueleto { padding: 4px 0 0; }
.rv { width: 100%; display: flex; flex-direction: column; gap: 10px; }
.rv-frente { padding: 12px 14px; }
.rv-cab { display: flex; align-items: center; gap: 10px; }
.rv input[type=checkbox] { width: 16px; height: 16px; margin: 0; flex: none; accent-color: var(--acento); }
.rv-cab .campo { flex: 1; min-width: 0; font-weight: 600; }
.rv-cab select.campo { flex: 0 0 116px; font-weight: 400; }
.rv-motivo { margin: 6px 0 0 26px; color: var(--meta); font: var(--t-meta) var(--fonte); }
.rv-vinc { list-style: none; margin: 10px 0 0 26px; padding: 8px 0 0; border-top: 1px solid var(--fio-fraco); display: flex; flex-direction: column; gap: 7px; }
.rv-vinc li, .rv-grupos li { display: flex; align-items: flex-start; gap: 9px; }
.rv-vinc label, .rv-grupos label { min-width: 0; }
.rv-vinc label .pq, .rv-grupos label .pq { display: block; color: var(--meta); font: var(--t-meta) var(--fonte); }
.rv-vinc input, .rv-grupos input { margin-top: 2px !important; }
.rv-grupos { list-style: none; margin: 0; padding: 12px 14px; display: flex; flex-direction: column; gap: 8px; }
.rv .rotulo { margin: 8px 0 0; }
.rv-nota { color: var(--meta); font: var(--t-meta) var(--fonte); margin: 0; }
@media (max-width: 560px) { .ini-cartao { padding: 20px 18px; } .ini-acoes { flex-wrap: wrap; } .rv-cab { flex-wrap: wrap; } }
</style>"""

_JS_COMUM = """<script>
(function () {
  /* um clique só: o botão que mandou o formulário fica desligado e, quando o passo demora, diz o que está fazendo */
  document.querySelectorAll('form[data-ini]').forEach(function (f) {
    f.addEventListener('submit', function (ev) {
      var b = ev.submitter;
      setTimeout(function () {
        if (ev.defaultPrevented) { return; }
        f.querySelectorAll('button').forEach(function (x) { if (x !== b) { x.disabled = true; } });
        if (b) { if (b.getAttribute('data-espera')) { b.textContent = b.getAttribute('data-espera'); } b.setAttribute('aria-disabled', 'true'); }
      }, 0);
    });
  });
})();
</script>"""


def _trilha(pid: str) -> str:
    n = inicio.posicao(pid)
    itens = "".join(f'<li class="{"atual" if i == n else "feito" if i < n else ""}"></li>'
                    for i in range(1, len(inicio.PASSOS) + 1))
    p = inicio.passo(pid)
    return (f'<ol class="ini-trilha" aria-hidden="true">{itens}</ol>'
            f'<p class="ini-onde" id="ini-onde">Passo {n} de {len(inicio.PASSOS)} · <b>{c.esc(p["rotulo"])}</b></p>')


def _acoes(pid: str, *, continuar: bool = True, rotulo: str = "", pular: bool = True, principal: str = "",
           espera: str = "", form: str = "f-ini") -> str:
    """Rodapé do cartão: Voltar à esquerda; Pular este passo e a ação principal à direita. Tudo aponta para o mesmo
    formulário (`form`), que leva o passo; o botão leva a ação. `principal`: HTML de uma ação própria do passo (gerar o
    código, testar a IA) no lugar do Continuar."""
    p = inicio.passo(pid)
    ant = inicio.anterior(pid)
    voltar = f'<a class="btn" href="/inicio/{ant}">Voltar</a>' if ant else ""
    bt_pular = (f'<button type="submit" class="btn" form="{form}" name="acao" value="pular" formnovalidate>'
                'Pular este passo</button>') if pular else ""
    esp = f' data-espera="{c.esc(espera)}"' if espera else ""
    bt = (f'<button type="submit" class="btn primario" form="{form}" name="acao" value="continuar"{esp}>'
          f'{c.esc(rotulo or p["continuar"])}</button>') if continuar and not principal else ""
    return f'<div class="ini-acoes">{voltar}<span class="espaco"></span>{bt_pular}{principal or bt}</div>'


def _tela(req, pid: str, area: str = "", acoes: str | None = None, *, status: int = 200, toast: str = "",
          script: str = "", forms: str = ""):
    """Uma tela do assistente: trilha, título, texto do passo, o porquê, a área própria do passo e as ações."""
    ctx = req.ctx
    p = inicio.passo(pid)
    texto = "".join(f"<p>{c.esc(t)}</p>" for t in p["texto"])
    form = (f'<form id="f-ini" method="post" action="/inicio/avancar" data-ini hidden>'
            f'<input type="hidden" name="passo" value="{pid}"></form>')
    cartao = (f'<div class="grupo ini-cartao" id="ini-{pid}"><h1>{c.esc(p["titulo"])}</h1>'
              f'<div class="ini-texto">{texto}</div>'
              f'<div class="ini-porque"><b>Por quê</b>{c.esc(p["por_que"])}</div>'
              f'<div class="ini-area">{area}</div>{_acoes(pid) if acoes is None else acoes}</div>')
    sair = ('<form method="post" action="/inicio/sair" id="f-sair" '
            'data-confirmar="Sair do início? Você volta quando quiser, em Ajustes, Refazer o início.">'
            '<button type="submit" class="btn">Sair do início</button></form>')
    toolbar = c.toolbar(f'<span class="marca">{_marca()}</span>', c.espaco(), sair, classe="ini-topo")
    corpo = _trilha(pid) + cartao + form + forms + _CSS + _JS_COMUM + script
    painel = c.painel(corpo, toolbar_html=toolbar, aria="Primeiros passos", corpo_classe="ini",
                      id_="inicio")
    return c.pagina(p["titulo"], painel, "", status, ctx=ctx, toast=toast)


def _pre(req, *metodos):
    if (e := c.metodo_errado(req, *metodos)):
        return e
    if req.metodo.upper() == "POST" and not c.origem_ok(req):
        return c.proibido()
    return None


# ------------------------------------------------------------------ passo 1: boas-vindas

def boas_vindas(req):
    if (e := _pre(req, "GET", "HEAD")):
        return e
    area = ('<ul class="ini-lista"><li>Ligar o seu WhatsApp, com o celular na mão.</li>'
            '<li>Conferir a IA que lê as conversas.</li><li>Deixar o envio pronto.</li>'
            '<li>Contar o que você faz, para o Greg sugerir as suas frentes.</li></ul>')
    return _tela(req, "boas-vindas", area + _aparencia(), _acoes("boas-vindas", pular=True))


def _aparencia() -> str:
    """Pedido 76: o Greg abre no claro e a pessoa escolhe a aparência já no primeiro passo. Cada opção é um botão de
    um formulário próprio: grava e volta para a mesma tela, já no tema escolhido. Funciona sem JavaScript."""
    atual = pf.ler()["tema"]
    botoes = "".join(f'<button type="submit" name="tema" value="{c.esc(v)}" class="btn{" primario" if v == atual else ""}"'
                     f' aria-pressed="{"true" if v == atual else "false"}">{c.esc(r)}</button>'
                     for v, r in pf.TEMAS.items())
    return ('<div class="ini-tema"><b>Aparência</b>'
            '<p class="rv-nota">O Greg começa no tema claro. Escolha como prefere ver; dá para trocar depois, em Ajustes.</p>'
            f'<form method="post" action="/inicio/tema" data-ini class="ini-tema-op" role="group" aria-label="Aparência">'
            f'{botoes}</form></div>')


def tema(req):
    """POST /inicio/tema {tema}: grava só o tema (as outras preferências ficam) e volta ao primeiro passo."""
    if (e := _pre(req, "POST")):
        return e
    valor = str((req.corpo or {}).get("tema") or "")
    if valor not in pf.TEMAS:
        return c.texto_simples("pedido inválido", 400)
    pf.trocar("tema", valor)
    return c.redirecionar("/inicio/boas-vindas")


# ------------------------------------------------------------------ passo 2: WhatsApp

_JS_QR = """<script>
(function () {
  var raiz = document.getElementById('ini-qr');
  if (!raiz) { return; }
  var estado = raiz.dataset.estado, maximo = parseInt(raiz.dataset.max, 10) || 3, ttl = parseInt(raiz.dataset.ttl, 10) || 0;
  var restantes = parseInt(raiz.dataset.restantes, 10) || 0;
  var img = document.getElementById('qr-img'), msg = document.getElementById('qr-msg'), falhas = 0, saiu = false;
  function ativo() { return estado === 'gerando' || estado === 'renovando'; }
  function sair(extra) { if (!saiu) { saiu = true; location.replace('/inicio/whatsapp' + (extra || '')); } }
  function pintar() {
    if (ttl <= 0) { return; }
    msg.className = '';
    msg.textContent = 'Código ' + (maximo - restantes) + ' de ' + maximo + '. Vale por mais ' + ttl + ' s';
  }
  function contagem() { if (!ativo() || saiu) { return; } if (ttl > 0) { ttl -= 1; pintar(); } setTimeout(contagem, 1000); }
  function falhou(texto) {
    falhas += 1; msg.className = 'ruim'; msg.textContent = texto;
    if (falhas < 3) { setTimeout(consultar, 5000); } else { msg.textContent = texto + ' Recarregue esta tela.'; }
  }
  function consultar() {
    if (!ativo() || saiu) { return; }
    fetch('/conexao/tick', { credentials: 'same-origin' })
      .then(function (r) { if (!r.ok) { var e = new Error('http'); e.status = r.status; throw e; } return r.json(); })
      .then(function (d) {
        falhas = 0; estado = d.estado;
        if (typeof d.restantes === 'number') { restantes = d.restantes; }
        if (d.estado === 'conectado') { sair(); return; }
        if (!ativo()) { sair(d.logout_confirmado === false ? '?presa=1' : '?expirou=1'); return; }
        if (d.base64 && String(d.base64).indexOf('data:image/') === 0 && img.getAttribute('src') !== d.base64) { img.src = d.base64; }
        if (typeof d.ttl === 'number') { ttl = d.ttl; }
        pintar(); setTimeout(consultar, 5000);
      })
      .catch(function (e) {
        if (e && e.status === 502) { falhou('A ponte com o WhatsApp não respondeu.'); }
        else if (e && e.status === 401) { msg.className = 'ruim'; msg.textContent = 'Sua sessão terminou. Entre de novo.'; }
        else { falhou('O Greg não respondeu.'); }
      });
  }
  if (ativo()) { pintar(); setTimeout(contagem, 1000); setTimeout(consultar, 5000); }
})();
</script>"""


def whatsapp(req, *, erro: str = "", status: int = 200):
    """Mostra o estado do pareamento; NUNCA gera QR (R-11). O QR vigente e o tick são os da Conexão."""
    if (e := _pre(req, "GET", "HEAD", "POST")):
        return e
    ctx = req.ctx
    cx = ctx.conexao
    estado = cx.estado
    evolution = pg_conexao._evolution_estado(ctx)
    ativo = estado in ("gerando", "renovando")
    conectado = estado == "conectado" or (estado == "ocioso" and evolution == "open")
    qrs, teto = ctx.banco.qrs_hoje(), getattr(cx, "teto_dia", 10)
    area = c.erro_inline(erro) if erro else ""
    if conectado:
        pg_conexao._agendar_leitura_do_pareamento(ctx)     # pedido 75: a leitura sai sozinha depois de parear
        para = getattr(ctx, "leitura_agendada_para", 0) or 0
        area += f'<p class="ini-bom">{c.icone("check")}WhatsApp ligado.</p>'
        if para > time.time():
            area += ('<div class="aviso-inline" role="status">O Greg está puxando o último dia de conversas e começa a ler '
                     f'às {c.esc(time.strftime("%H:%M", time.localtime(para)))}. Você pode seguir sem esperar.</div>')
        else:
            from app import agenda_leitura      # pedido 81: quem já escolheu outro intervalo vê o dele
            esc = agenda_leitura.escolha(c.dados_dir(req.ctx))
            ritmo = ("a cada meia hora" if agenda_leitura.e_padrao(*esc)
                     else agenda_leitura.texto_escolha(*esc).replace("A cada", "a cada", 1))
            area += ('<p class="rv-nota">O Greg puxa o último dia de conversas quando liga e lê o que chegar daqui para a '
                     f'frente, {c.esc(ritmo)}.</p>')
        return _tela(req, "whatsapp", area, _acoes("whatsapp", pular=False), status=status)
    vigente = cx.qr_vigente() if ativo else None
    src = c.esc(vigente["base64"]) if vigente and str(vigente.get("base64", "")).startswith("data:image/") else ""
    ttl = cx.ttl() if ativo and src else 0
    qr_max = int(getattr(cx, "qr_max", 3))
    restantes = vigente["restantes"] if vigente else 0
    if ativo and src:
        area += (f'<div class="ini-qr" id="ini-qr" data-estado="{c.esc(estado)}" data-ttl="{ttl}" data-max="{qr_max}" '
                 f'data-restantes="{restantes}"><img id="qr-img" alt="Código do WhatsApp para ler com o celular" '
                 f'width="232" height="232" src="{src}">'
                 f'<p id="qr-msg" role="status">Código {qr_max - restantes} de {qr_max}. Vale por mais {ttl} s</p></div>')
        return _tela(req, "whatsapp", area, _acoes("whatsapp", continuar=False), status=status, script=_JS_QR)
    if estado == "expirado" or c.query(req, "expirou"):
        area += ('<div class="aviso-inline" role="status">O código venceu sem ser lido. Peça outro quando estiver com o '
                 'celular na mão.</div>')
    if c.query(req, "presa") or (estado in ("expirado", "ocioso") and evolution == "connecting"):
        area += ('<div class="aviso-inline" role="status">A ligação ficou presa no meio. Peça o código de novo: o Greg '
                 'reinicia a ponte com o WhatsApp sozinho, e isso leva até um minuto.</div>')
    if qrs >= teto:
        area += ('<div class="aviso-inline" role="status">O limite de códigos de hoje foi atingido, para proteger o seu '
                 'número. Volte amanhã, ou pule este passo e ligue depois, na tela Conexão.</div>')
    bt = ('<form method="post" action="/inicio/whatsapp/qr" data-ini><button type="submit" class="btn primario" '
          f'data-espera="Preparando o código…"{" disabled" if qrs >= teto else ""}>Mostrar o código</button></form>')
    return _tela(req, "whatsapp", area, _acoes("whatsapp", principal=bt), status=status)


def whatsapp_qr(req):
    """O clique que gasta QR: exatamente um iniciar_qr(); se a Evolution recusar, o reinício automático da Conexão
    (pedido 75) e uma segunda tentativa. Volta sempre para a tela do passo."""
    if (e := _pre(req, "POST")):
        return e
    cx = req.ctx.conexao
    try:
        cx.iniciar_qr()
    except VigiaErro as ex:
        log.warning("início: código recusado: %s", causa(ex))
        if type(ex).__name__ == "TetoQR":
            return whatsapp(req, erro="O limite de códigos de hoje foi atingido. Tente amanhã.", status=429)
        if not pg_conexao._reiniciar_e_gerar_qr(req.ctx):
            return whatsapp(req, erro="A ponte com o WhatsApp não entregou o código, nem depois de reiniciada. Espere um "
                                      "minuto e peça de novo.", status=502)
    return c.redirecionar("/inicio/whatsapp")


# ------------------------------------------------------------------ passo 3: IA

def area_ia(ctx, estado: dict) -> tuple[str, bool]:
    """PONTO DE TROCA (pedido 68): o conteúdo do passo da IA, numa função só. Devolve (html da área, testou com
    sucesso). Hoje mostra o resultado do teste do Claude Code; com outras IAs, vira a escolha do provedor, e o botão
    Testar continua chamando `inicio.testar_ia`."""
    t = estado.get("ia_teste") if isinstance(estado.get("ia_teste"), dict) else None
    qual = (f'<p class="rv-nota">Quem lê suas conversas: <b>{c.esc(inicio.nome_da_ia(ctx))}</b>. '
            '<a href="/ajustes#ia">Trocar a IA em Ajustes</a></p>')
    if t is None:
        return qual + '<p class="rv-nota">Ainda não testada. O teste leva alguns segundos.</p>', False
    if t.get("ok"):
        return qual + f'<p class="ini-bom">{c.icone("check")}A IA respondeu. Está tudo certo.</p>', True
    passos = "".join(f"<li>{c.esc(p)}</li>" for p in t.get("passos") or [])
    return (qual + f'<div class="ini-erro" role="alert"><b>{c.esc(t.get("titulo"))}</b>{c.esc(t.get("por_que"))}'
            f'<ol class="ini-passos">{passos}</ol></div>'
            '<p class="rv-nota">Você pode seguir assim mesmo: as conversas chegam, e os cartões aparecem quando a IA voltar.</p>'), False


def ia(req):
    if (e := _pre(req, "GET", "HEAD")):
        return e
    estado = inicio.ler(req.ctx)
    area, ok = area_ia(req.ctx, estado)
    if ok:
        return _tela(req, "ia", area, _acoes("ia", pular=False))
    testou = isinstance(estado.get("ia_teste"), dict)
    bt = ('<form method="post" action="/inicio/ia/testar" data-ini><button type="submit" class="btn primario" '
          f'data-espera="Testando…">{"Testar de novo" if testou else "Testar"}</button></form>')
    return _tela(req, "ia", area, _acoes("ia", principal=bt))


def ia_testar(req):
    if (e := _pre(req, "POST")):
        return e
    r = inicio.testar_ia(req.ctx)
    r["em"] = int(time.time())
    inicio.mudar(req.ctx, lambda e: e.__setitem__("ia_teste", r))
    return c.redirecionar("/inicio/ia")


# ------------------------------------------------------------------ passo 4: envio

def envio(req, *, erro: str = "", status: int = 200):
    if (e := _pre(req, "GET", "HEAD", "POST")):
        return e
    tem = bool(getattr(getattr(req.ctx, "tokens", None), "envio", ""))
    area = c.erro_inline(erro) if erro else ""
    if tem:
        area += f'<p class="ini-bom">{c.icone("check")}O envio já está pronto neste computador.</p>'
    else:
        area += ('<p class="rv-nota">Ao continuar, o Greg cria a chave de envio e guarda neste computador. '
                 'Nada é enviado agora.</p>')
    return _tela(req, "envio", area, _acoes("envio"), status=status)


# ------------------------------------------------------------------ passo 5: o que você faz

_JS_AUDIO = """<script>
(function () {
  var bt = document.getElementById('ini-gravar'), txt = document.getElementById('ini-texto'), msg = document.getElementById('ini-audio-msg');
  var conta = document.getElementById('ini-conta');
  function contar() { if (conta && txt) { conta.textContent = txt.value.length + ' de ' + txt.maxLength; } }
  if (txt) { txt.addEventListener('input', contar); contar(); }
  if (!bt) { return; }
  var pode = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
  if (!pode) { bt.hidden = true; return; }
  var TETO = parseInt(bt.getAttribute('data-teto'), 10) || 300;
  var gravador = null, fluxo = null, pedacos = [], relogio = null, inicio = 0, mandar = false;
  function dizer(t, ruim) { msg.textContent = t || ''; msg.className = ruim ? 'ruim' : ''; }
  function mmss(s) { s = Math.max(0, Math.round(s)); return Math.floor(s / 60) + ':' + ('0' + (s % 60)).slice(-2); }
  /* a mesma conversão da tela Conversas (pedido 51): decodifica o que o navegador gravou e monta um WAV mono de 16 kHz */
  function paraWav(blob) {
    var AC = window.AudioContext || window.webkitAudioContext, OAC = window.OfflineAudioContext || window.webkitOfflineAudioContext;
    if (!AC || !OAC || !blob.arrayBuffer) { return Promise.reject(new Error('sem decodificador')); }
    return blob.arrayBuffer().then(function (ab) {
      var ac = new AC();
      return new Promise(function (ok, nao) { ac.decodeAudioData(ab, ok, nao); }).then(function (buf) {
        try { ac.close(); } catch (e) {}
        var taxa = 16000, n = Math.max(1, Math.ceil(buf.duration * taxa));
        var off = new OAC(1, n, taxa), fonte = off.createBufferSource();
        fonte.buffer = buf; fonte.connect(off.destination); fonte.start(0);
        return off.startRendering().then(function (r) {
          var pcm = r.getChannelData(0), dv = new DataView(new ArrayBuffer(44 + pcm.length * 2)), i, s;
          function asc(p, tx) { for (var k = 0; k < tx.length; k++) { dv.setUint8(p + k, tx.charCodeAt(k)); } }
          asc(0, 'RIFF'); dv.setUint32(4, 36 + pcm.length * 2, true); asc(8, 'WAVE'); asc(12, 'fmt ');
          dv.setUint32(16, 16, true); dv.setUint16(20, 1, true); dv.setUint16(22, 1, true); dv.setUint32(24, taxa, true);
          dv.setUint32(28, taxa * 2, true); dv.setUint16(32, 2, true); dv.setUint16(34, 16, true);
          asc(36, 'data'); dv.setUint32(40, pcm.length * 2, true);
          for (i = 0; i < pcm.length; i++) { s = Math.max(-1, Math.min(1, pcm[i])); dv.setInt16(44 + i * 2, s < 0 ? s * 32768 : s * 32767, true); }
          return new Blob([dv.buffer], {type: 'audio/wav'});
        });
      });
    });
  }
  function soltar() {
    if (relogio) { clearInterval(relogio); relogio = null; }
    if (fluxo) { fluxo.getTracks().forEach(function (t) { t.stop(); }); fluxo = null; }
    gravador = null; bt.setAttribute('aria-pressed', 'false'); bt.textContent = 'Gravar áudio';
  }
  function transcrever(partes, tipo) {
    dizer('Passando o áudio para texto. Leva alguns segundos…'); bt.disabled = true;
    paraWav(new Blob(partes, {type: tipo})).then(function (wav) {
      return fetch('/inicio/audio', { method: 'POST', credentials: 'same-origin', headers: { 'X-Vigia-Auto': '1', 'Content-Type': 'audio/wav' }, body: wav });
    }).then(function (r) { return r.json().then(function (j) { j.ok = r.ok; return j; }); }).then(function (j) {
      bt.disabled = false;
      if (!j.ok || !j.texto) { dizer(j.mensagem || 'Não consegui transcrever. Grave de novo ou escreva o texto.', true); return; }
      txt.value = (txt.value.trim() ? txt.value.trim() + '\\n' : '') + j.texto; contar();
      dizer('Confira o texto e corrija o que a transcrição errou.'); txt.focus();
    }).catch(function () { bt.disabled = false; dizer('Não consegui transcrever. Grave de novo ou escreva o texto.', true); });
  }
  function parar(enviar) { mandar = !!enviar; if (gravador && gravador.state !== 'inactive') { gravador.stop(); } else { soltar(); } }
  function comecar() {
    navigator.mediaDevices.getUserMedia({audio: true}).then(function (s) {
      fluxo = s; pedacos = [];
      try { gravador = new MediaRecorder(s); } catch (e) { soltar(); dizer('Este navegador não grava áudio. Escreva o texto.', true); return; }
      gravador.addEventListener('dataavailable', function (ev) { if (ev.data && ev.data.size) { pedacos.push(ev.data); } });
      gravador.addEventListener('stop', function () {
        var tipo = (gravador && gravador.mimeType) || 'audio/mp4', partes = pedacos, m = mandar;
        pedacos = []; soltar();
        if (m && partes.length) { transcrever(partes, tipo); } else { dizer(''); }
      });
      inicio = Date.now(); gravador.start(); bt.setAttribute('aria-pressed', 'true'); bt.textContent = 'Parar e transcrever';
      dizer('Gravando 0:00');
      relogio = setInterval(function () {
        var seg = Math.floor((Date.now() - inicio) / 1000); dizer('Gravando ' + mmss(seg));
        if (seg >= TETO) { parar(true); }
      }, 500);
    }).catch(function () { soltar(); dizer('O microfone não foi liberado. Autorize o Greg nos Ajustes do Sistema, em Privacidade, Microfone. Ou escreva o texto.', true); });
  }
  bt.addEventListener('click', function () { if (gravador) { parar(true); } else { comecar(); } });
  window.addEventListener('pagehide', function () { parar(false); });
})();
</script>"""


def descricao(req, *, erro: str = "", texto: str | None = None, status: int = 200):
    if (e := _pre(req, "GET", "HEAD", "POST")):
        return e
    valor = inicio.descricao(req.ctx) if texto is None else texto
    gravar = ""
    if inicio.pode_transcrever():
        gravar = (f'<button type="button" class="btn" id="ini-gravar" aria-pressed="false" data-teto="{inicio.MAX_AUDIO_S}">'
                  'Gravar áudio</button><span id="ini-audio-msg" role="status"></span>')
    area = ((c.erro_inline(erro) if erro else "")
            + '<div class="ini-desc"><label for="ini-texto" class="campo-rotulo">O seu trabalho, com as suas palavras</label>'
            f'<textarea class="campo" id="ini-texto" name="texto" form="f-ini" maxlength="{inicio.MAX_DESCRICAO}" '
            'placeholder="Exemplo: tenho uma agência de marketing. Atendo a Padaria Central e a Clínica Vida. Também cuido '
            f'da reforma da minha casa.">{c.esc(valor)}</textarea>'
            f'<div class="ini-linha">{gravar}<span class="espaco"></span><span class="meta-linha" id="ini-conta"></span></div></div>')
    if gravar:
        area += ('<p class="rv-nota">O áudio vira texto aqui mesmo, neste computador, e não fica guardado. Confira o texto '
                 'antes de seguir.</p>')
    return _tela(req, "descricao", area, _acoes("descricao"), status=status, script=_JS_AUDIO)


def audio(req):
    """POST /inicio/audio: o WAV gravado no passo 5 vira texto (motor local). Responde JSON {texto} ou {mensagem}."""
    if (e := _pre(req, "POST")):
        return e
    dados = (req.corpo or {}).get("_bruto")
    if not isinstance(dados, (bytes, bytearray)) or not dados:
        return json_resposta({"mensagem": "O áudio não chegou. Grave de novo ou escreva o texto."}, 400)
    try:
        return json_resposta({"texto": inicio.transcrever_audio(req.ctx, bytes(dados))})
    except inicio.AudioRecusado as ex:
        return json_resposta({"mensagem": str(ex)}, 422)


# ------------------------------------------------------------------ passo 6: sugestão

_JS_ESPERA = """<script>
(function () {
  var falhas = 0;
  function ver() {
    fetch('/inicio/sugestao/estado', { credentials: 'same-origin', headers: { 'X-Vigia-Auto': '1' } })
      .then(function (r) { if (!r.ok) { throw new Error('http'); } return r.json(); })
      .then(function (d) {
        falhas = 0;
        if (d.estado === 'rodando') { setTimeout(ver, 3000); return; }
        location.replace(d.estado === 'pronta' ? '/inicio/revisao' : '/inicio/sugestao');
      })
      .catch(function () {
        falhas += 1;
        if (falhas < 6) { setTimeout(ver, 5000); return; }
        var cx = document.querySelector('.ini-espera');
        if (cx) {
          cx.className = 'ini-erro'; cx.setAttribute('role', 'alert');
          cx.innerHTML = '<b>O Greg parou de responder</b>Não consegui saber se a proposta ficou pronta. Nada foi criado. ' +
            '<a href="/inicio/sugestao">Ver de novo</a> ou use Pular este passo.';
        }
      });
  }
  setTimeout(ver, 3000);
})();
</script>"""


def _proposta(s: dict) -> dict | None:
    """A proposta pronta, ou None. Arquivo de estado mexido à mão (ou de versão antiga) não derruba a tela: o que não
    tiver a forma esperada é tratado como "sem proposta"."""
    p = s.get("proposta") if s.get("estado") == "pronta" else None
    if not isinstance(p, dict):
        return None
    frentes = [f for f in p.get("frentes") or [] if isinstance(f, dict) and f.get("nome")] \
        if isinstance(p.get("frentes"), list) else []
    for f in frentes:
        f["vinculos"] = [v for v in f.get("vinculos") or [] if isinstance(v, dict) and v.get("jid")] \
            if isinstance(f.get("vinculos"), list) else []
    grupos = [g for g in p.get("grupos") or [] if isinstance(g, dict) and g.get("jid")] \
        if isinstance(p.get("grupos"), list) else []
    return {"frentes": frentes, "grupos": grupos}


def _resumo_proposta(s: dict) -> str:
    p = _proposta(s) or {}
    nf = len(p.get("frentes") or [])
    nv = sum(len(f.get("vinculos") or []) for f in p.get("frentes") or [])
    if not nf:
        return "A IA não encontrou frentes para sugerir."
    return (f"{nf} frente{'s' if nf != 1 else ''} sugerida{'s' if nf != 1 else ''}"
            + (f", com {nv} conversa{'s' if nv != 1 else ''} ligada{'s' if nv != 1 else ''} a elas." if nv else "."))


def _base_da_sugestao(s: dict) -> str:
    """Uma frase dizendo de onde a proposta saiu: a pessoa precisa saber quando as conversas não entraram."""
    if not s.get("leu"):
        return "As conversas não foram lidas, como você pediu: a proposta saiu do seu texto e dos nomes da agenda."
    if not s.get("banco"):
        return "As conversas ainda não entraram no Greg: a proposta saiu só do seu texto."
    if not s.get("lidas"):
        return "Ainda não há conversa do último dia: a proposta saiu do seu texto e dos nomes da agenda."
    n = int(s.get("lidas") or 0)
    return f"A IA leu {n} conversa{'s' if n != 1 else ''} do último dia, o seu texto e os nomes da agenda."


def sugestao(req):
    if (e := _pre(req, "GET", "HEAD")):
        return e
    ctx = req.ctx
    s = inicio.sugestao(ctx)
    est = s.get("estado")
    if est == "rodando":
        area = ('<div class="ini-espera" role="status"><b>A IA está montando a proposta.</b> Leva cerca de um minuto. '
                f'Esta tela muda sozinha quando ficar pronta.{c.esqueleto(3)}</div>')
        return _tela(req, "sugestao", area, _acoes("sugestao", continuar=False), script=_JS_ESPERA)
    forms = ('<form id="f-pedir" method="post" action="/inicio/sugestao/pedir" data-ini hidden></form>')
    pedir = ('<button type="submit" class="btn" form="f-pedir" name="ler" value="0">Seguir sem ler as conversas</button>'
             '<button type="submit" class="btn primario" form="f-pedir" name="ler" value="1" '
             'data-espera="Pedindo…">{rotulo}</button>')
    if est == "pronta":
        area = (f'<p class="ini-bom">{c.icone("check")}A proposta está pronta.</p>'
                f'<p class="rv-nota">{c.esc(_resumo_proposta(s))} {c.esc(_base_da_sugestao(s))}</p>')
        ac = (f'<div class="ini-acoes"><a class="btn" href="/inicio/descricao">Voltar</a><span class="espaco"></span>'
              '<button type="submit" class="btn" form="f-pedir" name="ler" value="1">Pedir de novo</button>'
              '<a class="btn primario" href="/inicio/revisao">Revisar a proposta</a></div>')
        return _tela(req, "sugestao", area, ac, forms=forms)
    area = ""
    if est == "falhou":
        a = s.get("aviso") or {}
        passos = "".join(f"<li>{c.esc(p)}</li>" for p in a.get("passos") or [])
        area += (f'<div class="ini-erro" role="alert"><b>{c.esc(a.get("titulo") or "A sugestão não saiu")}</b>'
                 f'{c.esc(a.get("por_que") or "")}<ol class="ini-passos">{passos}</ol></div>'
                 '<p class="rv-nota">Nada foi criado. Você pode tentar de novo ou pular este passo e criar as frentes à '
                 'mão, na tela Frentes.</p>')
    area += ('<div class="aviso-inline" role="note"><span><b>O que vai para a IA:</b> o texto que você escreveu, os nomes '
             'da sua agenda e dos seus grupos, sem os números, e as conversas do último dia. Quem lê é o Claude, com a '
             'sua conta, do mesmo jeito que o Greg vai ler as conversas daqui para a frente. A IA só devolve uma '
             'proposta.</span></div>')
    if not inicio.descricao(ctx):
        area += ('<p class="rv-nota">Você ainda não contou o que faz. A sugestão fica melhor com o seu texto: '
                 '<a href="/inicio/descricao">voltar e escrever</a>.</p>')
    if not inicio.pareado(ctx):
        area += '<p class="rv-nota">O WhatsApp ainda não está ligado: a proposta vai sair só do seu texto.</p>'
    ac = (f'<div class="ini-acoes"><a class="btn" href="/inicio/descricao">Voltar</a><span class="espaco"></span>'
          '<button type="submit" class="btn" form="f-ini" name="acao" value="pular">Pular este passo</button>'
          + pedir.format(rotulo="Tentar de novo" if est == "falhou" else "Pedir a sugestão") + "</div>")
    return _tela(req, "sugestao", area, ac, forms=forms)


def sugestao_pedir(req):
    if (e := _pre(req, "POST")):
        return e
    ler = str((req.corpo or {}).get("ler") or "1") != "0"
    inicio.pedir_sugestao(req.ctx, ler)
    return c.redirecionar("/inicio/sugestao")


def sugestao_estado(req):
    if (e := _pre(req, "GET")):
        return e
    return json_resposta({"estado": inicio.sugestao(req.ctx).get("estado") or "nenhuma"})


# ------------------------------------------------------------------ passo 7: revisão

_JS_REVISAO = """<script>
(function () {
  var f = document.getElementById('f-rev'); if (!f) { return; }
  /* desmarcar a frente desliga os contatos dela: não existe vínculo para frente que não vai ser criada */
  function acertar(caixa) {
    var bloco = caixa.closest('.rv-frente'); if (!bloco) { return; }
    bloco.querySelectorAll('.rv-vinc input, .rv-cab .campo').forEach(function (x) { x.disabled = !caixa.checked; });
  }
  f.querySelectorAll('.rv-cab input[type=checkbox]').forEach(function (cx) { cx.addEventListener('change', function () { acertar(cx); contar(); }); acertar(cx); });
  var bt = document.getElementById('rv-criar');
  function contar() {
    var n = f.querySelectorAll('.rv-cab input[type=checkbox]:checked').length;
    if (bt) { bt.textContent = n ? 'Criar o que marquei (' + n + ')' : 'Seguir sem criar frente'; }
  }
  contar();
})();
</script>"""


def _html_revisao(p: dict) -> str:
    """O formulário da proposta. Nos campos vai só a posição (f0, v0_1, g2): nenhum jid sai do servidor."""
    blocos = []
    for i, f in enumerate(p.get("frentes") or []):
        opcoes = "".join(f'<option value="{v}"{" selected" if v == f.get("tipo") else ""}>{c.esc(r)}</option>'
                         for v, r in ROTULO_TIPO)
        vinc = ""
        for j, v in enumerate(f.get("vinculos") or []):
            forte = v.get("forca") == "forte"
            vinc += (f'<li><input type="checkbox" id="v{i}_{j}" name="v{i}_{j}" value="1"{" checked" if forte else ""}>'
                     f'<label for="v{i}_{j}">{c.esc(v.get("nome"))}{"" if forte else " " + c.pilula("talvez", "neutra")}'
                     f'<span class="pq">{c.esc(v.get("por_que"))}</span></label></li>')
        blocos.append(
            f'<div class="grupo rv-frente"><div class="rv-cab"><input type="checkbox" id="f{i}" name="f{i}" value="1" checked '
            f'aria-label="Criar a frente {c.esc(f.get("nome"))}">'
            f'<input class="campo" type="text" name="n{i}" value="{c.esc(f.get("nome"))}" maxlength="80" '
            f'aria-label="Nome da frente" autocomplete="off">'
            f'<select class="campo" name="t{i}" aria-label="Tipo da frente">{opcoes}</select></div>'
            f'<p class="rv-motivo">{c.esc(f.get("por_que"))}</p>'
            + (f'<ul class="rv-vinc" aria-label="Conversas desta frente">{vinc}</ul>' if vinc else "") + "</div>")
    grupos = ""
    for k, g in enumerate(p.get("grupos") or []):
        grupos += (f'<li><input type="checkbox" id="g{k}" name="g{k}" value="1">'
                   f'<label for="g{k}">{c.esc(g.get("nome"))}<span class="pq">{c.esc(g.get("por_que"))}</span></label></li>')
    if grupos:
        blocos.append(c.rotulo("Grupos")
                      + '<p class="rv-nota">O Greg não lê nenhum grupo sem você marcar. Os que você marcar aqui ficam '
                        'anotados; a leitura de grupo é ligada depois, por quem cuida da instalação.</p>'
                      + f'<ul class="grupo rv-grupos">{grupos}</ul>')
    return ('<form class="rv" id="f-rev" method="post" action="/inicio/revisao" data-ini>'
            + "".join(blocos) + "</form>")


def revisao(req, *, erro: str = "", status: int = 200):
    if (e := _pre(req, "GET", "HEAD", "POST")):
        return e
    s = inicio.sugestao(req.ctx)
    p = _proposta(s)
    if not p or not (p.get("frentes") or p.get("grupos")):
        if s.get("estado") == "pronta":
            area = ('<p class="rv-nota"><b>A IA não encontrou frentes para sugerir.</b> '
                    f'{c.esc(_base_da_sugestao(s))} Você cria as suas na tela Frentes, quando quiser.</p>')
        else:
            area = ('<p class="rv-nota"><b>Não há proposta para revisar.</b> Você pode pedir a sugestão no passo anterior ou '
                    'seguir e criar as frentes na tela Frentes, quando quiser.</p>')
        ac = ('<div class="ini-acoes"><a class="btn" href="/inicio/sugestao">Voltar</a><span class="espaco"></span>'
              '<button type="submit" class="btn primario" form="f-ini" name="acao" value="pular">Continuar</button></div>')
        return _tela(req, "revisao", area, ac, status=status)
    area = ((c.erro_inline(erro) if erro else "") + f'<p class="rv-nota">{c.esc(_base_da_sugestao(s))}</p>'
            + _html_revisao(p))
    ac = ('<div class="ini-acoes"><button type="submit" class="btn destrutivo" form="f-rev" name="acao" value="descartar" '
          'formnovalidate>Descartar tudo</button><span class="espaco"></span>'
          '<button type="submit" class="btn" form="f-ini" name="acao" value="pular">Pular este passo</button>'
          '<button type="submit" class="btn primario" form="f-rev" name="acao" value="criar" id="rv-criar" '
          'data-espera="Criando…">Criar o que marquei</button></div>')
    return _tela(req, "revisao", area, ac, status=status, script=_JS_REVISAO)


def revisao_post(req):
    """O clique da revisão: o único lugar do assistente que cria frente (`ponte.criar_frente`) e vínculo
    (`banco.vincular`, origem do dono). Sem proposta pronta no servidor, nada é criado, venha o que vier no corpo."""
    if (e := _pre(req, "POST")):
        return e
    ctx = req.ctx
    corpo = req.corpo or {}
    acao = str(corpo.get("acao") or "")
    s = inicio.sugestao(ctx)
    p = _proposta(s)
    if acao == "descartar":
        inicio.descartar_sugestao(ctx)
        return c.redirecionar("/inicio/sugestao")
    if acao != "criar":
        return c.texto_simples("pedido inválido", 400)
    if not p:
        return c.redirecionar("/inicio/revisao")
    escolhidas, nomes = [], set()
    for i, f in enumerate(p.get("frentes") or []):
        if str(corpo.get(f"f{i}") or "") != "1":
            continue
        nome = " ".join(str(corpo.get(f"n{i}") or f.get("nome") or "").split())[:80]
        chave = vc.normalizar(nome)
        if not chave:
            return revisao(req, erro="Uma frente marcada ficou sem nome. Escreva o nome ou desmarque a frente.", status=400)
        if chave in nomes:
            return revisao(req, erro=f"Duas frentes marcadas com o nome {nome}. Mude um dos nomes ou desmarque uma.", status=400)
        nomes.add(chave)
        tipo = str(corpo.get(f"t{i}") or f.get("tipo") or "cliente")
        escolhidas.append((i, f, nome, tipo if tipo in inicio.TIPOS_FRENTE else "cliente"))
    criadas = vinculadas = falhas = sem_ligar = 0
    existentes = None
    for i, f, nome, tipo in escolhidas:
        fid = None
        try:
            r = ctx.ponte.criar_frente(nome, tipo=tipo, apelidos=f.get("apelidos") or [])
            fid = r.get("id")
            if isinstance(fid, int):
                criadas += 1
        except VigiaErro as ex:
            log.warning("início: frente não criada (%s)", causa(ex))
            # nome que já existe (a pessoa criou a frente à mão no meio do caminho): os contatos vão para ela
            try:
                if existentes is None:
                    existentes = {vc.normalizar(x.get("nome")): x.get("id") for x in ctx.ponte.frentes()}
                fid = existentes.get(vc.normalizar(nome))
            except VigiaErro:
                fid = None
        if not isinstance(fid, int):
            falhas += 1
            continue
        for j, v in enumerate(f.get("vinculos") or []):
            if str(corpo.get(f"v{i}_{j}") or "") == "1":
                try:
                    ctx.banco.vincular(v["jid"], fid, "dono")
                    vinculadas += 1
                except Exception as ex:  # noqa: BLE001  (a frente já existe: uma conversa sem ligar não derruba o passo)
                    log.warning("início: conversa não ligada à frente: %s", causa(ex))
                    sem_ligar += 1
    grupos = [g for k, g in enumerate(p.get("grupos") or []) if str(corpo.get(f"g{k}") or "") == "1"]
    if grupos:
        inicio.anotar_grupos(ctx, grupos)
    inicio.descartar_sugestao(ctx)      # a proposta foi usada: voltar no navegador não cria tudo de novo
    inicio.marcar(ctx, "revisao", "feito")
    log.info("início: revisão aplicada (frentes=%d vínculos=%d grupos=%d falhas=%d)", criadas, vinculadas, len(grupos), falhas)
    return c.redirecionar(f"/inicio/roteiro?criadas={criadas}&vinculadas={vinculadas}&falhas={falhas}"
                          + (f"&semligar={sem_ligar}" if sem_ligar else ""))


# ------------------------------------------------------------------ passo 8: passeio

def roteiro(req):
    if (e := _pre(req, "GET", "HEAD")):
        return e
    toast = ""
    n, v, f = (c.query(req, k) for k in ("criadas", "vinculadas", "falhas"))
    if n.isdigit():
        n, v, f = int(n), int(v) if v.isdigit() else 0, int(f) if f.isdigit() else 0
        toast = (f"{n} frente{'s' if n != 1 else ''} criada{'s' if n != 1 else ''}"
                 + (f", {v} conversa{'s' if v != 1 else ''} ligada{'s' if v != 1 else ''}" if v else "") + ".")
        if f:
            toast += f" {f} não {'foram criadas' if f != 1 else 'foi criada'}: crie na tela Frentes."
        sl = c.query(req, "semligar")
        if sl.isdigit() and int(sl):
            toast += (f" {int(sl)} conversa{'s' if int(sl) != 1 else ''} não {'foram ligadas' if int(sl) != 1 else 'foi ligada'}: "
                      "ligue na tela Contatos.")
    area = "".join(f"<li><b>{c.esc(p['titulo'])}</b></li>" for p in inicio.ROTEIRO)
    return _tela(req, "roteiro", f'<ol class="ini-lista">{area}</ol>',
                 _acoes("roteiro", rotulo="Começar o passeio"), toast=toast)


def roteiro_visto(req):
    if (e := _pre(req, "POST")):
        return e
    inicio.mudar(req.ctx, lambda e: e.__setitem__("roteiro", "visto"))
    return json_resposta({"ok": True})


# ------------------------------------------------------------------ navegação

def revisao_rota(req):
    return revisao_post(req) if req.metodo.upper() == "POST" else revisao(req)


_TELAS = {"boas-vindas": boas_vindas, "whatsapp": whatsapp, "ia": ia, "envio": envio, "descricao": descricao,
          "sugestao": sugestao, "revisao": revisao_rota, "roteiro": roteiro}


def entrada(req):
    """GET /inicio: leva ao passo em que a pessoa parou (ou ao primeiro)."""
    if (e := _pre(req, "GET", "HEAD")):
        return e
    estado = inicio.ler(req.ctx)
    pid = estado.get("passo") if estado.get("estado") == "andando" and estado.get("passo") in inicio.IDS else inicio.IDS[0]
    return c.redirecionar(f"/inicio/{pid}")


def avancar(req):
    """POST /inicio/avancar {passo, acao}: Continuar ou Pular. O que cada passo faz ao continuar está aqui, num lugar só."""
    if (e := _pre(req, "POST")):
        return e
    ctx = req.ctx
    corpo = req.corpo or {}
    pid = str(corpo.get("passo") or "")
    acao = str(corpo.get("acao") or "continuar")
    if pid not in inicio.IDS or acao not in ("continuar", "pular"):
        return c.texto_simples("pedido inválido", 400)
    if pid == "roteiro":            # fim do assistente: com ou sem o passeio
        inicio.marcar(ctx, pid, "feito" if acao == "continuar" else "pulado")
        inicio.concluir(ctx)
        return c.redirecionar("/demandas?roteiro=1" if acao == "continuar" else "/demandas")
    if acao == "pular":
        inicio.marcar(ctx, pid, "pulado")
        if pid == "sugestao":       # sem sugestão não há o que revisar
            inicio.marcar(ctx, "revisao", "pulado")
            return c.redirecionar("/inicio/roteiro")
        return c.redirecionar(f"/inicio/{inicio.seguinte(pid)}")
    if pid == "envio":
        try:
            inicio.garantir_token_envio(ctx)
        except OSError as ex:
            log.warning("início: token de envio não gravado (%s)", causa(ex))
            return envio(req, erro="O Greg não conseguiu guardar a chave de envio neste computador. Tente de novo; se "
                                   "repetir, pule este passo.", status=500)
    elif pid == "descricao":
        texto = inicio.limpar_descricao(corpo.get("texto"))
        if not texto:
            return descricao(req, erro="Escreva ao menos uma frase, ou use Pular este passo.", texto="", status=400)
        inicio.gravar_descricao(ctx, texto)
    elif pid == "sugestao":         # o passo da sugestão anda pelo botão de pedir; continuar daqui só leva à revisão
        return c.redirecionar("/inicio/revisao")
    elif pid == "revisao":
        return c.redirecionar("/inicio/revisao")
    inicio.marcar(ctx, pid, "feito")
    return c.redirecionar(f"/inicio/{inicio.seguinte(pid)}")


def sair(req):
    """Sair do início no meio: o assistente não aparece mais sozinho. Volta por Ajustes."""
    if (e := _pre(req, "POST")):
        return e
    inicio.concluir(req.ctx, "dispensado")
    return c.redirecionar("/demandas")


def refazer(req):
    if (e := _pre(req, "POST")):
        return e
    inicio.refazer(req.ctx)
    return c.redirecionar("/inicio/boas-vindas")


def descricao_ajustes(req):
    """POST /inicio/descricao/salvar (seção Início de Ajustes): salva ou apaga o texto que a pessoa deu."""
    if (e := _pre(req, "POST")):
        return e
    corpo = req.corpo or {}
    if str(corpo.get("acao") or "") == "apagar":
        inicio.apagar_descricao(req.ctx)
        return c.redirecionar("/ajustes?inicio=apagado#inicio")
    inicio.gravar_descricao(req.ctx, corpo.get("texto"))
    return c.redirecionar("/ajustes?inicio=salvo#inicio")


AVISOS_AJUSTES = {"salvo": "Texto salvo neste computador.", "apagado": "Texto apagado."}


def html_grupo(ctx, aviso: str = "") -> str:
    """Seção "Início" de Ajustes: refazer o assistente, ver o passeio de novo e editar ou apagar a descrição."""
    texto = inicio.descricao(ctx)
    nota = f'<div class="aviso-inline" role="status">{c.esc(aviso)}</div>' if aviso else ""
    linha = ('<div class="opt"><div class="l">Primeiros passos<span>Ligar o WhatsApp, conferir a IA e sugerir frentes, '
             'do começo</span></div><div class="r"><form method="post" action="/inicio/refazer">'
             '<button type="submit" class="btn">Refazer o início</button></form>'
             '<a class="btn" href="/demandas?roteiro=1">Ver o roteiro de novo</a></div></div>')
    desc = ('<div class="opt linha-lista"><div class="l"><label for="aj-inicio-texto">O que você faz</label>'
            '<span>O texto que você deu ao Greg para ele sugerir as frentes. Fica só neste computador</span></div>'
            f'<div class="r">{nota}<form method="post" action="/inicio/descricao/salvar" style="width:100%">'
            f'<textarea class="campo" id="aj-inicio-texto" name="texto" maxlength="{inicio.MAX_DESCRICAO}" '
            f'style="width:100%;min-height:84px" placeholder="Ainda não há texto guardado">{c.esc(texto)}</textarea>'
            '<div class="acoes" style="margin-top:8px"><button type="submit" class="btn" name="acao" value="salvar">Salvar o texto</button>'
            + ('<button type="submit" class="btn destrutivo" name="acao" value="apagar">Apagar o texto</button>' if texto else "")
            + "</div></form></div></div>")
    return linha + desc


ROTAS = {"/inicio": entrada, "/inicio/tema": tema, "/inicio/avancar": avancar, "/inicio/sair": sair, "/inicio/refazer": refazer,
         "/inicio/whatsapp/qr": whatsapp_qr, "/inicio/ia/testar": ia_testar, "/inicio/audio": audio,
         "/inicio/sugestao/pedir": sugestao_pedir, "/inicio/sugestao/estado": sugestao_estado,
         "/inicio/descricao/salvar": descricao_ajustes, "/inicio/roteiro/visto": roteiro_visto,
         **{f"/inicio/{pid}": fn for pid, fn in _TELAS.items()}}
