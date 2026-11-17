"""Seção "IA" dos Ajustes (pedido 68): quem lê as conversas. Claude Code (padrão), OpenRouter, outro provedor
compatível com o protocolo da OpenAI ou um modelo local (experimental).
Quem chama: app/paginas/ajustes.py (`html_grupo`) e app/server.py (via ROTAS: POST /ajustes/ia).
O que faz: grava a escolha em dados/ia.json (app/leitor.py), guarda a chave fora da tela (Keychain ou dados/.env.ia)
e, no botão Testar, faz uma chamada mínima ao provedor e responde em uma frase se funcionou e por que não.
O que não faz: nunca devolve a chave para a página (o campo volta sempre vazio), não chama provedor nenhum sem o
clique em Testar e não mexe na leitura em andamento (a troca vale a partir do próximo ciclo).
"""
import json

from app import leitor
from app.http import Resposta
from app.paginas import _comum as c

AVISOS = {"salvo": "Salvo. Vale a partir da próxima leitura.",
          "chave-apagada": "Chave apagada deste Mac."}


def _linha(rotulo: str, sub: str, controle: str, classe: str = "") -> str:
    s = f"<span>{c.esc(sub)}</span>" if sub else ""
    return f'<div class="opt ia {classe}"><div class="l">{c.esc(rotulo)}{s}</div><div class="r">{controle}</div></div>'


def _seg(nome: str, opcoes, atual, rotulo: str) -> str:
    partes = []
    for valor, texto in opcoes:
        on = str(valor) == str(atual)
        partes.append(f'<label{" class=\"on\"" if on else ""}><input type="radio" name="{nome}" value="{c.esc(valor)}"'
                      f'{" checked" if on else ""}>{c.esc(texto)}</label>')
    return f'<div class="seg" role="radiogroup" aria-label="{c.esc(rotulo)}">{"".join(partes)}</div>'


def _dinheiro(v: float) -> str:
    return f"US$ {v:.2f}".replace(".", ",")


def html_grupo(ctx, aviso: str = "", erro: bool = False) -> str:
    """O grupo "IA" dos Ajustes. `aviso`: frase de resultado (salvo, teste, erro de validação)."""
    from app import leitor_api
    dados = c.dados_dir(ctx)
    cfg = leitor.configuracao(None, dados)
    prov = cfg["provedor"]
    api = cfg["motor"] == "api"
    tem_chave = leitor.tem_chave(dados)
    gasto = leitor_api.gasto_do_mes(dados)
    partes = []
    if aviso:
        partes.append(f'<p class="ia-aviso{" erro" if erro else ""}" role="status">{c.esc(aviso)}</p>')
    partes.append(_linha(
        "Quem lê as conversas",
        "O Claude Code é o padrão. Nas outras opções, o texto e as imagens das conversas vão para o provedor escolhido; "
        "na opção local, nada sai deste Mac",
        _seg("provedor", [(k, v["rotulo"]) for k, v in leitor.PROVEDORES.items()], prov, "Quem lê as conversas"), "ia-quem"))
    endereco = cfg["endereco"] or leitor.PROVEDORES[prov]["endereco"]
    partes.append(_linha(
        "Endereço", "A base do provedor, terminada em /v1. No modelo local: Ollama usa a porta 11434, LM Studio a 1234",
        f'<input class="campo" type="url" name="endereco" id="ia-endereco" value="{c.esc(endereco)}" '
        f'placeholder="https://provedor/v1" autocomplete="off" spellcheck="false" aria-label="Endereço">', "so-api so-livre"))
    partes.append(_linha(
        "Modelo", "O nome exato do modelo no provedor. Ele precisa saber chamar função; para ler imagem, precisa de visão",
        f'<input class="campo" type="text" name="modelo" value="{c.esc(cfg["modelo"])}" placeholder="nome do modelo" '
        f'autocomplete="off" spellcheck="false" aria-label="Modelo">', "so-api"))
    estado_chave = ("Há uma chave guardada. Cole outra para trocar" if tem_chave
                    else "Cole a chave do provedor. No modelo local não precisa")
    apagar = ('<button type="submit" class="btn" name="acao" value="apagar-chave">Apagar chave</button>' if tem_chave else "")
    partes.append(_linha(
        "Chave", estado_chave + ". Ela fica guardada neste Mac e nunca volta para esta tela",
        f'<input class="campo" type="password" name="chave" value="" placeholder="{"guardada" if tem_chave else "chave"}" '
        f'autocomplete="off" aria-label="Chave do provedor">{apagar}', "so-api so-paga"))
    partes.append(_linha(
        "Imagens", "Se o modelo não lê imagem, o Greg marca a imagem como não lida em vez de deixar a IA supor",
        _seg("visao", [("1", "Envia"), ("0", "Não envia")], "1" if cfg["visao"] else "0", "Imagens"), "so-api"))
    partes.append(_linha(
        "Teto de gasto por mês",
        f"Gasto neste mês: {_dinheiro(gasto['usd'])} de {_dinheiro(cfg['teto_mes_usd'])}. Ao atingir o teto, a leitura para "
        "até você aumentar o valor ou o mês virar",
        f'<span class="ia-moeda">US$</span><input class="campo ia-num" type="number" name="teto_mes_usd" min="0.1" max="10000" '
        f'step="0.1" value="{cfg["teto_mes_usd"]:g}" aria-label="Teto de gasto por mês em dólares">', "so-api so-paga"))
    partes.append(_linha(
        "Preço por milhão de tokens", "Entrada e saída, em dólares. Vale quando o provedor não informa o custo na resposta",
        f'<span class="ia-moeda">entrada</span><input class="campo ia-num" type="number" name="preco_entrada_usd_milhao" min="0" max="1000" step="0.01" '
        f'value="{cfg["preco_entrada_usd_milhao"]:g}" aria-label="Preço de entrada por milhão de tokens">'
        f'<span class="ia-moeda">saída</span><input class="campo ia-num" type="number" name="preco_saida_usd_milhao" min="0" max="1000" step="0.01" '
        f'value="{cfg["preco_saida_usd_milhao"]:g}" aria-label="Preço de saída por milhão de tokens">', "so-api so-paga"))
    partes.append(
        '<div class="opt ia ia-acoes"><div class="l" id="ia-resultado" role="status" aria-live="polite"></div><div class="r">'
        '<button type="submit" class="btn so-api" name="acao" value="testar" id="ia-testar">Testar</button>'
        '<button type="submit" class="btn primario" name="acao" value="salvar">Salvar</button></div></div>')
    padroes = json.dumps({k: v["endereco"] for k, v in leitor.PROVEDORES.items()})
    return (f'<form method="post" action="/ajustes/ia" id="form-ia" data-provedor="{c.esc(prov)}" data-api="{"1" if api else "0"}" '
            f"data-enderecos='{c.esc(padroes)}'>{''.join(partes)}</form>{_CSS}{_JS}")


_CSS = """<style>
.opt.ia .r { gap: 8px; }
.opt.ia.ia-quem { grid-template-columns: 1fr; gap: 8px; }
.opt.ia.ia-acoes .r { justify-content: flex-end; }
.opt.ia .campo { flex: 1; min-width: 0; max-width: 340px; }
.opt.ia .campo.ia-num { flex: none; width: 92px; text-align: right; }
.opt.ia .seg label { position: relative; }
.opt.ia .seg input { position: absolute; inset: 0; opacity: 0; margin: 0; pointer-events: none; }
.opt.ia .seg label:focus-within { outline: 3px solid var(--foco); outline-offset: 1px; }
.ia-moeda { color: var(--meta); }
.ia-aviso { margin: 0; padding: 8px 14px; color: var(--meta); }
.ia-aviso.erro, #ia-resultado.erro { color: var(--destrutivo); }
.ia-acoes .l { color: var(--meta); font: var(--t-meta) var(--fonte); }
#form-ia[data-provedor="claude-code"] .so-api,
#form-ia[data-provedor="openrouter"] .so-livre,
#form-ia[data-provedor="local"] .so-paga { display: none; }
</style>"""

# Sem JS o formulário funciona inteiro (os campos escondidos pelo provedor gravado continuam coerentes). Com JS: trocar
# o provedor mostra os campos dele na hora e o Testar responde na própria linha, sem recarregar.
_JS = """<script>
(function () {
  var f = document.getElementById('form-ia'); if (!f) { return; }
  var res = document.getElementById('ia-resultado'), end = document.getElementById('ia-endereco');
  var padroes = JSON.parse(f.getAttribute('data-enderecos') || '{}');
  function pinta() { f.querySelectorAll('.seg label').forEach(function (l) { var i = l.querySelector('input'); l.classList.toggle('on', !!(i && i.checked)); }); }
  f.addEventListener('change', function (e) {
    pinta();
    if (e.target && e.target.name === 'provedor') {
      f.setAttribute('data-provedor', e.target.value);
      if (end && (padroes[e.target.value] || e.target.value === 'outro')) { end.value = padroes[e.target.value] || ''; }
    }
  });
  var t = document.getElementById('ia-testar');
  if (t) { t.addEventListener('click', function (e) {
    e.preventDefault();
    var d = Object.fromEntries(new FormData(f)); d.acao = 'testar';
    t.disabled = true; res.classList.remove('erro'); res.textContent = 'Testando…';
    window.vigiaPost('/ajustes/ia', d).then(function (j) {
      res.textContent = j.frase || j.erro || ('Não testou (' + j.status + ').');
      res.classList.toggle('erro', !j.funcionou);
      var c = f.querySelector('input[name=chave]'); if (c && j.salvo) { c.value = ''; c.placeholder = j.tem_chave ? 'guardada' : 'chave'; }
    }).catch(function () { res.textContent = 'Não testou: o painel não respondeu.'; res.classList.add('erro'); })
      .then(function () { t.disabled = false; });
  }); }
  if (location.hash === '#ia') { var alvo = document.getElementById('ia'); if (alvo) { alvo.scrollIntoView({block: 'start'}); } }
})();
</script>"""


def _gravar(dados, d: dict) -> dict:
    """Valida e grava a escolha e, se veio, a chave. Levanta ConfiguracaoInvalida com a frase para a tela."""
    prov = str(d.get("provedor") or "").strip().lower()
    if prov not in leitor.PROVEDORES:
        raise leitor.ConfiguracaoInvalida("escolha quem lê as conversas")
    info = leitor.PROVEDORES[prov]
    bruto = {"motor": info["motor"], "provedor": prov, "modelo": str(d.get("modelo") or "").strip(),
             "endereco": info["endereco"] if prov == "openrouter" else str(d.get("endereco") or "").strip(),
             "visao": str(d.get("visao", "1")) != "0"}
    for campo in ("teto_mes_usd", "preco_entrada_usd_milhao", "preco_saida_usd_milhao"):
        if str(d.get(campo) or "").strip():
            bruto[campo] = d[campo]
    if info["motor"] == "api":
        if prov == "local" and not leitor.eh_local(bruto["endereco"]):
            raise leitor.ConfiguracaoInvalida("na opção local o endereço tem de ser deste Mac (localhost)")
        if not bruto["modelo"]:
            raise leitor.ConfiguracaoInvalida("informe o nome do modelo")
    else:       # Claude Code: endereço e modelo da outra IA ficam guardados para a volta, se forem válidos
        atual = leitor.configuracao(None, dados)
        bruto.update(endereco=atual["endereco"], modelo=atual["modelo"])
    nova = str(d.get("chave") or "").strip()
    if nova:                                    # valida a chave antes de gravar qualquer coisa
        if not leitor.CHAVE_OK.match(nova):
            raise leitor.ConfiguracaoInvalida("chave fora do formato aceito (8 a 512 caracteres, sem espaço nem aspas)")
    cfg = leitor.gravar_configuracao(dados, bruto)
    if nova:
        leitor.gravar_chave(dados, nova)
    return cfg


def ia_post(req):
    """POST /ajustes/ia {acao: salvar|testar|apagar-chave, provedor, endereco, modelo, chave, visao, teto…}.
    `testar` grava antes (o teste é do que está na tela) e responde com uma frase."""
    if (e := c.metodo_errado(req, "POST")):
        return e
    if not c.origem_ok(req):
        return c.proibido()
    cab = req.cabecalhos or {}
    if cab.get("authorization") or cab.get("Authorization"):    # token de integração (conector) nunca mexe na IA
        return c.proibido()
    from app.paginas import ajustes
    ctx, d = req.ctx, dict(req.corpo or {})
    dados = c.dados_dir(ctx)
    acao = str(d.get("acao") or "salvar")
    quer_json = c.pediu_json(req)

    def responder(frase: str, funcionou: bool, salvo: bool, status: int = 200):
        if quer_json:
            corpo = {"funcionou": funcionou, "salvo": salvo, "frase": frase, "tem_chave": leitor.tem_chave(dados)}
            return Resposta(status, json.dumps(corpo, ensure_ascii=False), "application/json; charset=utf-8",
                            [("Cache-Control", "no-store")])
        return ajustes.ajustes_get(req, ia_aviso=frase, ia_erro=not funcionou, status=status)

    if acao == "apagar-chave":
        leitor.apagar_chave(dados)
        return responder(AVISOS["chave-apagada"], True, True) if quer_json else c.redirecionar("/ajustes?ia=chave-apagada#ia")
    if acao not in ("salvar", "testar"):
        return c.texto_simples("pedido inválido", 400)
    try:
        cfg = _gravar(dados, d)
    except leitor.ConfiguracaoInvalida as e:
        return responder(f"Não salvou: {e}.", False, False, 400)
    if acao == "salvar":
        return responder(AVISOS["salvo"], True, True) if quer_json else c.redirecionar("/ajustes?ia=salvo#ia")
    if cfg["motor"] != "api":
        return responder("Com o Claude Code não há o que testar aqui: a leitura usa o login do Claude deste Mac.", True, True)
    from app import leitor_api
    funcionou, frase = leitor_api.testar(cfg, leitor.chave(dados), dados)
    if funcionou:
        from app import alertas, saude
        alertas.fechar(dados, *saude._IA)
    return responder(frase, funcionou, True)


ROTAS = {"/ajustes/ia": ia_post}
