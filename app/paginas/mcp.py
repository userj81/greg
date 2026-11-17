"""Rota /mcp (o conector do Claude Code, pedido 71, D-71) e a seção "Claude Code" dos Ajustes (/ajustes/claude).
Quem chama: app/server.py (via ROTAS). Em /mcp o servidor aceita o token da integração no cabeçalho Authorization no
lugar do cookie; a rota confere de novo. /ajustes/claude só com a sessão do painel (cookie): o token da integração não
abre esta rota, então o Claude não liga chave, não gera token e não permite o próprio pedido.
O que não faz: não loga corpo nem argumento; não mostra o token depois da tela em que ele é gerado (em disco só o hash).
"""
import json
import time

from app import integracao, mcp
from app.http import Resposta, json_resposta
from app.paginas import _comum as c


def _token_da_req(req) -> str:
    cab = req.cabecalhos or {}
    return integracao.token_do_cabecalho(cab.get("authorization") or cab.get("Authorization"))


def mcp_rota(req):
    """POST /mcp: uma mensagem JSON-RPC (ou um lote). GET responde 405: não há fluxo SSE."""
    if (e := c.metodo_errado(req, "POST")):
        return e
    if not c.origem_ok(req):
        return c.proibido()
    if not integracao.token_ok(req.ctx, _token_da_req(req)):
        return json_resposta({"jsonrpc": "2.0", "id": None,
                              "error": {"code": -32001, "message": "token da integração ausente ou errado"}}, 401)
    corpo = req.corpo if isinstance(req.corpo, dict) else {}
    lote = corpo.get("_lote")
    if isinstance(lote, list):
        respostas = [r for r in (mcp.tratar(req.ctx, m) for m in lote[:50]) if r is not None]
        if not respostas:
            return Resposta(202, "", "application/json; charset=utf-8")
        return Resposta(200, json.dumps(respostas, ensure_ascii=False), "application/json; charset=utf-8")
    r = mcp.tratar(req.ctx, corpo)
    if r is None:
        return Resposta(202, "", "application/json; charset=utf-8")
    return json_resposta(r)


# ------------------------------------------------------------------ Ajustes: seção Claude Code

def _host(req) -> str:
    cab = req.cabecalhos or {}
    h = str(cab.get("host") or cab.get("Host") or "127.0.0.1:8120").strip()
    return h if c._netloc_loopback(h) else "127.0.0.1:8120"


CONECTOU = "Greg conectado ao Claude Code. Volte ao Greg e ligue o que o Claude pode fazer."
NAO_CONECTOU = "Não conectou. Confira se o Greg está aberto, gere o código de novo em Ajustes e rode o comando outra vez."


def comando(host: str, token: str) -> str:
    """Uma linha para colar no Terminal: tira o conector antigo (se houver), põe o novo, confere e diz o resultado."""
    return ('claude mcp remove greg --scope user >/dev/null 2>&1; '
            f'claude mcp add --transport http --scope user greg http://{host}/mcp '
            f'--header "Authorization: Bearer {token}" >/dev/null '
            f'&& claude mcp get greg | grep -q Connected && echo "{CONECTOU}" || echo "{NAO_CONECTOU}"')


def _form(acao: str, campos: dict, botao: str) -> str:
    ocultos = "".join(f'<input type="hidden" name="{c.esc(k)}" value="{c.esc(v)}">' for k, v in campos.items())
    return (f'<form method="post" action="/ajustes/claude" class="cl-form"><input type="hidden" name="acao" value="{c.esc(acao)}">'
            f'{ocultos}{botao}</form>')


def _interruptor(nome: str, ligado: bool, rotulo: str) -> str:
    botao = (f'<button type="submit" class="tog" role="switch" aria-checked="{"true" if ligado else "false"}" '
             f'aria-label="{c.esc(rotulo)}" title="{c.esc(rotulo)}"></button>')
    return _form("chave", {"nome": nome, "ligar": "0" if ligado else "1"}, botao)


def _linha(rotulo: str, sub: str, controle: str) -> str:
    s = f"<span>{c.esc(sub)}</span>" if sub else ""
    return f'<div class="opt cl"><div class="l">{c.esc(rotulo)}{s}</div><div class="r">{controle}</div></div>'


def _quando(ts: int) -> str:
    return time.strftime("%d/%m %H:%M", time.localtime(ts)) if ts else ""


_RESULTADO = {"ok": "feito", "erro": "falhou", "recusado": "recusado", "aval": "esperando você", "incerto": "pode ter saído"}


def html_grupo(ctx, host: str = "127.0.0.1:8120", token_novo: str = "", aviso: str = "") -> str:
    """O grupo "Claude Code" dos Ajustes. `token_novo`: mostrado uma única vez, logo depois de gerar."""
    est = integracao.ler(ctx)
    tem_token = bool(est["token_sha256"])
    partes = []
    if aviso:
        partes.append(f'<p class="cl-aviso" role="status">{c.esc(aviso)}</p>')
    if token_novo:
        partes.append(
            '<div class="cl-novo" id="cl-novo" role="status"><p><strong>Conexão criada.</strong> Rode este comando uma vez no Terminal; '
            'ele mesmo diz se conectou. Depois abra o Claude Code. O comando aparece só agora: o Greg guarda apenas a impressão digital do código.</p>'
            f'<pre class="cl-cmd" id="cl-cmd">{c.esc(comando(host, token_novo))}</pre>'
            '<button type="button" class="btn" id="cl-copiar">Copiar comando</button></div>')
    if tem_token:
        estado = f"Conectável desde {_quando(est['criado_em'])}. Trocar o código desliga o Claude que usa o atual."
        botoes = (_form("gerar", {}, '<button type="submit" class="btn">Trocar código</button>')
                  + _form("revogar", {}, '<button type="submit" class="btn">Desconectar</button>'))
    else:
        estado = "Desligado. Gere o código e rode o comando que aparece para o seu Claude Code enxergar o Greg."
        botoes = _form("gerar", {}, '<button type="submit" class="btn primario">Conectar o Claude Code</button>')
    partes.append(_linha("Conexão", estado, f'<div class="cl-botoes">{botoes}</div>'))
    for nome, (rotulo, sub) in integracao.CHAVES.items():
        partes.append(_linha(rotulo, sub, _interruptor(nome, est["chaves"][nome], rotulo)))
    partes.append(_linha("Pedir permissão para número novo",
                         "Com Enviar e Ler ligados, mensagem para quem nunca conversou com você espera o seu aval aqui",
                         _interruptor("confirmar_novos", est["confirmar_novos"], "Pedir permissão para número novo")))
    pend = integracao.pedidos(ctx).pendentes()
    if pend:
        linhas = []
        for p in pend:
            acoes = (_form("decidir", {"id": p["id"], "permitir": "1"}, '<button type="submit" class="btn primario">Permitir</button>')
                     + _form("decidir", {"id": p["id"], "permitir": "0"}, '<button type="submit" class="btn">Recusar</button>'))
            linhas.append(f'<div class="cl-pedido"><span class="borravel">{c.esc(p["resumo"])}</span>'
                          f'<div class="cl-botoes">{acoes}</div></div>')
        partes.append(f'<div class="opt cl-bloco"><div class="l">Pedidos do Claude<span>Só saem se você permitir. '
                      f'Vencem em 30 minutos</span></div><div class="cl-lista">{"".join(linhas)}</div></div>')
    reg = integracao.registro(ctx, 8)
    if reg:
        itens = "".join(f'<div class="cl-reg"><span>{c.esc(_quando(r["ts"]))}</span><span>{c.esc(r["ferramenta"])}</span>'
                        f'<span>{c.esc(_RESULTADO.get(r["resultado"], r["resultado"]))}</span></div>' for r in reg)
        partes.append(f'<div class="opt cl-bloco"><div class="l">Últimas ações do Claude<span>Só o nome da ação e a hora; '
                      f'nenhum texto de mensagem é guardado</span></div><div class="cl-lista">{itens}</div></div>')
    return "".join(partes) + _CSS + _JS


_CSS = """<style>
.cl-form { display: inline-flex; margin: 0; }
.cl-botoes { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
.cl-aviso { margin: 0; padding: 8px 12px; color: var(--meta); }
.cl-novo { padding: 10px 12px; border-bottom: 1px solid var(--fio-fraco); }
.cl-novo p { margin: 0 0 8px; }
.cl-cmd { margin: 0 0 8px; padding: 8px 10px; border-radius: 6px; background: var(--fundo-2); font: 400 .9231rem/1.45 ui-monospace, Menlo, monospace;
  white-space: pre-wrap; word-break: break-all; user-select: all; -webkit-user-select: all; }
.opt.cl { grid-template-columns: 1fr auto; }
.opt.cl .r { justify-content: flex-end; }
.opt.cl-bloco { grid-template-columns: 1fr; gap: 6px; }
.cl-lista { display: flex; flex-direction: column; width: 100%; }
.cl-pedido { display: flex; align-items: center; gap: 10px; padding: 6px 0; border-top: 1px solid var(--fio-fraco); }
.cl-pedido > span { flex: 1; min-width: 0; }
.cl-reg { display: grid; grid-template-columns: 84px 1fr auto; gap: 10px; padding: 4px 0; border-top: 1px solid var(--fio-fraco); color: var(--meta); font-size: .9231rem; }
.cl-reg span:nth-child(2) { color: var(--texto); font-family: ui-monospace, Menlo, monospace; overflow: hidden; text-overflow: ellipsis; }
</style>"""

_JS = """<script>
(function () {
  var b = document.getElementById('cl-copiar'), p = document.getElementById('cl-cmd');
  if (!b || !p) { return; }
  var alvo = document.getElementById('claude') || document.getElementById('cl-novo');   // a tela fica na seção, não volta ao topo
  if (alvo) { var rolar = function () { alvo.scrollIntoView({block: 'start'}); }; rolar(); window.addEventListener('load', rolar); }
  b.addEventListener('click', function () {
    var feito = function () { b.textContent = 'Copiado'; setTimeout(function () { b.textContent = 'Copiar comando'; }, 2000); };
    if (navigator.clipboard && navigator.clipboard.writeText) { navigator.clipboard.writeText(p.textContent).then(feito, function () {}); }
    else { var r = document.createRange(); r.selectNodeContents(p); var s = getSelection(); s.removeAllRanges(); s.addRange(r); }
  });
})();
</script>"""


def claude_post(req):
    """POST /ajustes/claude {acao: chave|gerar|revogar|decidir, …}. Só com a sessão do painel."""
    if (e := c.metodo_errado(req, "POST")):
        return e
    if not c.origem_ok(req):
        return c.proibido()
    if _token_da_req(req):      # defesa em profundidade: pedido com o token do conector nunca mexe na integração
        return c.proibido()
    from app.paginas import ajustes
    ctx, d = req.ctx, dict(req.corpo or {})
    acao = str(d.get("acao") or "")
    if acao == "gerar":
        token = integracao.gerar_token(ctx)
        return ajustes.ajustes_get(req, claude_token=token)
    if acao == "revogar":
        integracao.revogar(ctx)
        return c.redirecionar("/ajustes?claude=desconectado#claude")
    if acao == "chave":
        if not integracao.definir(ctx, str(d.get("nome") or ""), str(d.get("ligar") or "") == "1"):
            return c.texto_simples("pedido inválido", 400)
        return c.redirecionar("/ajustes#claude")
    if acao == "decidir":
        try:
            pid = int(str(d.get("id") or ""))
        except ValueError:
            return c.texto_simples("pedido inválido", 400)
        r = integracao.pedidos(ctx).decidir(pid, str(d.get("permitir") or "") == "1")
        if r is None:
            return c.redirecionar("/ajustes?claude=vencido#claude")
        return c.redirecionar(f"/ajustes?claude={'permitido' if r['estado'] in ('feito', 'falhou') else 'recusado'}#claude")
    return c.texto_simples("pedido inválido", 400)


AVISOS = {"desconectado": "Claude Code desconectado. O código antigo não vale mais e todas as áreas foram desligadas.",
          "vencido": "Esse pedido já tinha vencido ou sido decidido.",
          "permitido": "Permitido. O Claude já pode ver o resultado.",
          "recusado": "Recusado. Nada saiu."}

ROTAS = {"/mcp": mcp_rota, "/ajustes/claude": claude_post}
