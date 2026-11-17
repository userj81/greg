"""Página /ajustes (estilo Papel, pedido 39): dois grupos no padrão dos Ajustes do macOS. Aparência (Tema, Tamanho da letra,
Cor de destaque: a do Mac ou uma de poucas cores fixas, pedido 66) e Privacidade (Modo de gravação, Conversas bloqueadas). A escolha grava sozinha
(pedido 19) e o CSS que o servidor devolve no POST é o mesmo que monta as páginas, então o que se vê é o que fica.
Quem chama: app/server.py (via ROTAS); ⌘, e o menu do Greg.app abrem esta página (ela saiu da lateral).
O que não faz: não mexe em nada além de tema, letra e cor de destaque; valores fora do permitido caem no padrão (app/preferencias.validar).
Não há mais seletor de estilo, papel de parede, tom do vidro nem cor própria (o Papel é o único estilo).
"""
import json

from app import preferencias as pf
from app.http import Resposta
from app.paginas import _comum as c
from app.paginas import bloqueios

# Prévia ao vivo: aplica a escolha na hora e grava em seguida. CSP permite script inline; nada externo.
_JS = """<script>
(function () {
  var f = document.getElementById('form-ajustes'), raiz = document.documentElement;
  function pintaSeg() { f.querySelectorAll('.seg').forEach(function (s) { s.querySelectorAll('label').forEach(function (l) {
    var i = l.querySelector('input'); l.classList.toggle('on', !!(i && i.checked)); }); }); }
  function imediato() {
    var fonte = f.querySelector('input[name=fonte]:checked'), tema = f.querySelector('input[name=tema]:checked');
    if (fonte) raiz.style.fontSize = fonte.value + 'px';
    if (tema) { if (tema.value === 'sistema') raiz.removeAttribute('data-tema'); else raiz.setAttribute('data-tema', tema.value); }
    pintaSeg();
    f.querySelectorAll('.cor').forEach(function (l) { var i = l.querySelector('input'); l.classList.toggle('on', !!(i && i.checked));
      if (i && i.checked) { var n = document.getElementById('acento-nome'); if (n) n.textContent = l.getAttribute('title'); } });
  }
  function aplicar(r) {
    if (r.attrs && r.attrs['data-tema']) raiz.setAttribute('data-tema', r.attrs['data-tema']); else raiz.removeAttribute('data-tema');
    var st = document.getElementById('prefs'); if (st) st.textContent = r.css || '';
    raiz.style.fontSize = '';
  }
  var espera = null;
  function gravar() {
    clearTimeout(espera);
    espera = setTimeout(function () {
      window.vigiaPost('/ajustes', Object.fromEntries(new FormData(f))).then(function (j) {
        if (!j.ok) { window.vigiaToast('Não salvou (' + j.status + ').'); return; }
        aplicar(j);
      }).catch(function () { window.vigiaToast('Não salvou: o painel não respondeu.'); });
    }, 250);
  }
  f.addEventListener('change', function () { imediato(); gravar(); });
  f.addEventListener('submit', function (e) { e.preventDefault(); gravar(); });
  pintaSeg();
  var g = document.getElementById('vigia-gravar');
  if (g) { g.setAttribute('aria-checked', raiz.hasAttribute('data-gravacao') ? 'true' : 'false');
    g.addEventListener('click', function () { window.vigiaGravar(); }); }
})();
</script>"""


def _seg(nome: str, opcoes, atual) -> str:
    """Segmentado com rádios escondidos: funciona sem JS (o form grava no submit) e o JS pinta o ativo."""
    partes = []
    for valor, rotulo in opcoes:
        on = str(valor) == str(atual)
        partes.append(f'<label{" class=\"on\"" if on else ""}><input type="radio" name="{nome}" value="{c.esc(valor)}"'
                      f'{" checked" if on else ""}>{c.esc(rotulo)}</label>')
    return f'<div class="seg" role="radiogroup" aria-label="{c.esc(nome.capitalize())}">{"".join(partes)}</div>'


def _cores(atual: str) -> str:
    """Pedido 66: amostras de cor (rádios, gravam sozinhas como o resto). A primeira segue o acento do Mac."""
    partes = []
    for valor, (rotulo, par) in pf.ACENTOS.items():
        on = valor == atual
        fundo = f' style="--amostra: light-dark({par[0]}, {par[1]})"' if par else ""
        partes.append(f'<label class="cor{" on" if on else ""}{"" if par else " do-mac"}" title="{c.esc(rotulo)}"{fundo}>'
                      f'<input type="radio" name="acento" value="{c.esc(valor)}"{" checked" if on else ""}>'
                      f'<span class="sr-only">{c.esc(rotulo)}</span></label>')
    nome = pf.ACENTOS[atual][0]
    return (f'<div class="cores" role="radiogroup" aria-label="Cor de destaque">{"".join(partes)}</div>'
            f'<span class="meta-linha" id="acento-nome">{c.esc(nome)}</span>')


def _hora_do_fechamento(ctx) -> str:
    """'21h' (ou '21h30'): [horarios].diario do config.toml, a hora em que os itens do dia sobem ao Frentes (D-57)."""
    bruto = str((((getattr(ctx, "config", None) or {}).get("horarios") or {}).get("diario")) or "21:00")
    try:
        h, m = (int(x) for x in bruto.split(":", 1))
    except ValueError:
        h, m = 21, 0
    return f"{h}h{m:02d}" if m else f"{h}h"


def _horas(de: int, ate: int) -> str:
    """Pedido 81: 'Das [hora] às [hora]', em horas cheias; gravam sozinhas como o resto do formulário."""
    def lista(nome: str, atual: int, rotulo: str) -> str:
        ops = "".join(f'<option value="{h}"{" selected" if h == atual else ""}>{c.esc(txt)}</option>'
                      for h, txt in pf.HORAS.items())
        return f'<select name="{nome}" class="campo aj-hora" aria-label="{rotulo}">{ops}</select>'
    return (f'<div class="aj-horas"><span>das</span>{lista("leitura_de", de, "Ler a partir de")}'
            f'<span>às</span>{lista("leitura_ate", ate, "Ler até")}</div>')


def _opt(rotulo: str, controle: str, sub: str = "", classe: str = "") -> str:
    s = f"<span>{c.esc(sub)}</span>" if sub else ""
    cls = f' class="opt {classe}"' if classe else ' class="opt"'
    return f'<div{cls}><div class="l">{c.esc(rotulo)}{s}</div><div class="r">{controle}</div></div>'


def ajustes_get(req, aviso: str = "", bloqueio_aviso: str = "", status: int = 200, claude_token: str = "",
                ia_aviso: str = "", ia_erro: bool = False):
    p = pf.ler()
    ctx = getattr(req, "ctx", None)
    aparencia = (_opt("Tema", _seg("tema", pf.TEMAS.items(), p["tema"]))
                 + _opt("Tamanho da letra", _seg("fonte", pf.FONTES.items(), p["fonte"]))
                 + _opt("Cor de destaque", _cores(p["acento"]), "Botões, seleção e as suas mensagens"))
    envio = _opt("Envio liberado por", _seg("envio_min", pf.ENVIO_MIN.items(), p["envio_min"]),
                 "Depois do Touch ID, o envio fica aberto por este tempo. Vale a partir da próxima liberação")
    leitura = (_opt("Ler as conversas a cada", _seg("leitura_min", pf.LEITURA_MIN.items(), p["leitura_min"]),
                    "Intervalo maior gasta menos da sua conta de IA. Ler agora vale a qualquer hora")
               + _opt("Horário de leitura", _horas(p["leitura_de"], p["leitura_ate"]),
                      f"Horas iguais: o dia todo. O fechamento do dia continua às {_hora_do_fechamento(ctx)}, "
                      "uma passada de IA por dia"))
    dados = _opt("Guardar as mensagens por", _seg("retencao_dias", pf.RETENCAO_DIAS.items(), p["retencao_dias"]),
                 "O Greg apaga as mensagens mais antigas que este prazo e mantém as demandas. "
                 "Em Não apagar, tudo fica guardado neste Mac")
    gravacao = ('<button type="button" class="tog" role="switch" id="vigia-gravar" aria-checked="false" aria-pressed="false" '
                'aria-label="Modo de gravação" title="Modo de gravação (⌘⇧B)"></button>')
    privacidade = (_opt("Modo de gravação", gravacao, "Borra o que vem do WhatsApp em todas as telas")
                   + (bloqueios.html_grupo(ctx, bloqueio_aviso) if ctx is not None else ""))
    # O formulário envolve só Aparência: Privacidade tem formulários próprios (bloquear/desbloquear), e form dentro de
    # form o navegador descarta (o "+" passaria a mandar o POST para /ajustes).
    corpo = (f'<div class="miolo aj">{aviso}'
             f'<form id="form-ajustes" method="post" action="/ajustes">'
             f'<p class="rotulo">Aparência</p><div class="grupo">{aparencia}</div>'
             f'<p class="rotulo">Leitura</p><div class="grupo">{leitura}</div>'
             f'<p class="rotulo">Envio</p><div class="grupo">{envio}</div>'
             f'<p class="rotulo">Dados</p><div class="grupo">{dados}</div>'
             f'<button type="submit" class="sr-only">Salvar</button></form>'
             f'<p class="rotulo">Privacidade</p><div class="grupo">{privacidade}</div>'
             f'{_ia(req, ctx, ia_aviso, ia_erro)}{_claude(req, ctx, claude_token)}{_inicio(req, ctx)}</div>{_CSS}{_JS}')
    toolbar = c.toolbar(c.botao_icone("voltar", "Voltar para Demandas", href="/demandas"), c.titulo("Ajustes"))
    painel = c.painel(corpo, toolbar_html=toolbar, status_html=c.rodape("As alterações valem na hora"), aria="Ajustes",
                      corpo_classe="ajustes", id_="ajustes")
    return c.pagina("Ajustes", painel, "/ajustes", status, ctx=ctx)


def _ia(req, ctx, aviso: str = "", erro: bool = False) -> str:
    """Pedido 68: a seção IA (quem lê as conversas: Claude Code, outro provedor ou modelo local)."""
    if ctx is None:
        return ""
    from app.paginas import ia as pagina_ia
    aviso = aviso or pagina_ia.AVISOS.get(c.query(req, "ia"), "")
    return f'<p class="rotulo" id="ia">IA</p><div class="grupo">{pagina_ia.html_grupo(ctx, aviso, erro)}</div>'


def _inicio(req, ctx) -> str:
    """Pedido 69: a seção Início (refazer os primeiros passos, ver o roteiro de novo, editar ou apagar a descrição)."""
    if ctx is None:
        return ""
    from app.paginas import inicio as pagina_inicio
    grupo = pagina_inicio.html_grupo(ctx, pagina_inicio.AVISOS_AJUSTES.get(c.query(req, "inicio"), ""))
    return f'<p class="rotulo" id="inicio">Início</p><div class="grupo">{grupo}</div>'


def _claude(req, ctx, token_novo: str = "") -> str:
    """Pedido 71: a seção Claude Code (conexão, o que o Claude pode fazer, pedidos esperando aval, últimas ações)."""
    if ctx is None:
        return ""
    from app.paginas import mcp as pagina_mcp
    aviso = pagina_mcp.AVISOS.get(c.query(req, "claude"), "")
    grupo = pagina_mcp.html_grupo(ctx, pagina_mcp._host(req), token_novo, aviso)
    return f'<p class="rotulo" id="claude">Claude Code</p><div class="grupo">{grupo}</div>'


_CSS = """<style>
.corpo.ajustes { background: var(--fundo-2); }
.aj .grupo { margin-bottom: 4px; }
.aj .seg label { position: relative; }
.aj .seg input { position: absolute; inset: 0; opacity: 0; margin: 0; pointer-events: none; }
.aj .seg label:focus-within { outline: 3px solid var(--foco); outline-offset: 1px; }
.cores { display: flex; align-items: center; gap: 8px; }
.cor { position: relative; width: 18px; height: 18px; border-radius: 50%; background: var(--amostra); box-shadow: inset 0 0 0 .5px rgba(0,0,0,.25); }
.cor.do-mac { background: conic-gradient(#FF5F57, #FEBC2E, #28C840, #0A84FF, #BF5AF2, #FF5F57); }
.cor input { position: absolute; inset: 0; opacity: 0; margin: 0; }
.cor.on::after { content: ""; position: absolute; inset: 5.5px; border-radius: 50%; background: #FFFFFF; box-shadow: 0 0 1px rgba(0,0,0,.4); }
.cor:focus-within { outline: 3px solid var(--foco); outline-offset: 2px; }
#acento-nome { margin-left: 4px; }
.aj-horas { display: flex; align-items: center; gap: 8px; color: var(--texto-2); }
.aj-horas .aj-hora { width: auto; min-width: 78px; color: var(--texto); }
.bl-linhas { display: flex; flex-direction: column; gap: 0; width: 100%; }
.bl-linha { display: flex; align-items: center; gap: 10px; padding: 6px 0; border-top: 1px solid var(--fio-fraco); }
.bl-linha:first-child { border-top: 0; }
.bl-linha .bl-nome { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bl-linha .meta-linha { flex: none; }
.bl-novo { display: flex; align-items: center; gap: 8px; margin-top: 6px; padding-top: 8px; border-top: 1px solid var(--fio-fraco); }
.bl-novo .campo { flex: 1; }
.bl-escolher { display: flex; align-items: center; gap: 8px; margin-top: 8px; }
.bl-vazio { color: var(--meta); }
</style>"""


def ajustes_post(req):
    if not c.origem_ok(req):
        return c.proibido()
    d = dict(req.corpo or {})
    if d.get("restaurar"):
        pf.gravar(dict(pf.PADRAO))
        return c.redirecionar("/ajustes?salvo=1")
    p = pf.gravar(d)
    if c.pediu_json(req):
        # prévia viva: a página aplica o atributo e troca o <style id=prefs> sem recarregar
        corpo = {"ok": True, "attrs": {"data-tema": None if p["tema"] == "sistema" else p["tema"]},
                 "css": pf.css(p).removeprefix('<style id="prefs">').removesuffix("</style>")}
        return Resposta(200, json.dumps(corpo, ensure_ascii=False), "application/json; charset=utf-8",
                        [("Cache-Control", "no-store")])
    return c.redirecionar("/ajustes?salvo=1")


def ajustes(req):
    if req.metodo.upper() == "POST":
        return ajustes_post(req)
    if (e := c.metodo_errado(req, "GET", "HEAD")):
        return e
    toast = ""
    if c.query(req, "salvo") == "1":
        toast = "Salvo neste Mac."
    elif c.query(req, "bloqueado") == "1":
        toast = "Bloqueada. Daqui para a frente nada dessa conversa é guardado."
    elif c.query(req, "desbloqueado") == "1":
        toast = "Desbloqueada. O que chegar daqui para a frente volta a ser guardado."
    r = ajustes_get(req)
    if toast:
        # o toast entra pela página inteira: refaz com o aviso (ajustes_get não conhece a query)
        r = _com_toast(req, toast)
    return r


def _com_toast(req, toast: str):
    p = pf.ler()  # noqa: F841  (mantém a leitura única por requisição; ajustes_get relê do cache)
    resposta = ajustes_get(req)
    corpo = resposta.corpo.decode("utf-8") if isinstance(resposta.corpo, bytes) else resposta.corpo
    corpo = corpo.replace("</main>", c.toast_html(toast) + "</main>", 1)
    return Resposta(resposta.status, corpo, resposta.tipo, resposta.cabecalhos)


ROTAS = {"/ajustes": ajustes}
