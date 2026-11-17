# API local de envio (`/envios`) — pedido 10, R-15, D-44

Porta de saída do WhatsApp pessoal para **as ferramentas do dono**: Claude Code, scripts, cron, remarketing.
Não é disparo em massa (R-15). Todo envio é solicitação dele (R-9): a ferramenta carrega o token dele e diz quem é (`origem`).
O coletor (`claude -p` agendado) não tem o token e não alcança esta API.

- Endereço: `http://127.0.0.1:8120` (porta em `VIGIA_PORT` do `.env`). Só loopback; nada sai do Mac.
- Autenticação: header `X-Vigia-Envio: <VIGIA_TOKEN_ENVIO>` lido de `~/.vigia-envio.env` (arquivo `0600`).
  O token vale como sessão **só em `/envios*`**; nas outras rotas segue exigindo o login do painel.
- Texto da mensagem **nunca vai para o disco**: fica na memória do painel enquanto o lote vive; o banco guarda só `hash_texto`.
  Painel reiniciou → lotes ativos viram `interrompido`; a ferramenta repete o `POST` com a mesma `idempotencia` e o lote retoma de onde parou.

## Fluxo em uma linha (CLI)

```bash
echo '{"origem":"claude-code","texto":"Oi {nome}, segue a proposta.","destinatarios":[{"para":"+1 415 555 0100","nome":"Ana"}],"idempotencia":"prop-2026-11-07-ana"}' | ~/vigia-whatsapp/bin/vigia-enviar --esperar
```

`bin/vigia-enviar` lê o JSON pelo **stdin** e o token do arquivo: nada em argv, nada no `ps`.

| Comando | Faz | Código de saída |
|---|---|---|
| `vigia-enviar [--esperar [SEG]]` | cria o lote (stdin); com `--esperar` acompanha até o fim e imprime o estado final | 0 aceito/concluído limpo · 1 terminou com pendência (pausado, parado, interrompido, incerto, pulado, cancelado) ou estourou o tempo · 2 não rodou |
| `vigia-enviar estado <lote>` | estado do lote por destinatário | 0 / 1 como acima |
| `vigia-enviar parar <lote>` | o que saiu, saiu; o resto vira `cancelado` | 0 / 2 |
| `vigia-enviar retomar <lote>` | reenfileira um lote pausado, parado ou cancelado (texto ainda em memória) | 0 / 2 |
| `vigia-enviar fila` | resumo da fila e últimos lotes | 0 / 2 |

Ambiente opcional: `VIGIA_PORT` · `VIGIA_ENVIO_ENV` (outro caminho para o arquivo do token).

## Contrato HTTP

### `POST /envios` — criar lote

```json
{
  "origem": "claude-code",
  "texto": "Oi {nome}, segue a proposta.",
  "destinatarios": [{"para": "+1 415 555 0100", "nome": "Ana"}, {"para": "120363000000000001@g.us"}],
  "idempotencia": "prop-2026-11-07",
  "conferir": true
}
```

| Campo | Regra |
|---|---|
| `origem` | obrigatório; `^[a-z0-9][a-z0-9-]{0,39}$`; vai para o registro (`por = api:<origem>`) e para a tela |
| `texto` | obrigatório; 1–4000 caracteres; `{nome}` é trocado pelo nome do destinatário (sem nome: marcador sai e os espaços são limpos) |
| `destinatarios[]` | 1–20 (`teto_lote`); `para` = número (`92 90000-1111` ganha o 55; `+1 415 555 0100` para fora do Brasil) ou jid (`…@s.whatsapp.net`, `…@g.us`); `@lid` recusado; repetido é descartado; `nome` opcional. Os exemplos desta página usam `+1 415 555 01xx`, faixa reservada a ficção: nenhum número brasileiro plausível fica versionado (guarda em `testes/test_fechar_semana.py`) |
| `idempotencia` | opcional; `^[A-Za-z0-9._:-]{1,120}$`; por `origem`, 24 h |
| `conferir` | opcional (padrão `true`): número **novo** é conferido na Evolution antes de sair; sem WhatsApp → `pulado` |

Nome usado em `{nome}`: o do payload > apelido do Greg > nome da conversa no WhatsApp (nunca um número formatado).
**Conhecido** = grupo, ou tem apelido, ou já conversou com ele desde o pareamento. O resto é **novo** (teto menor, cadência dobrada).

Respostas:

| Código | Quando | Corpo |
|---|---|---|
| `202` | lote aceito | estado do lote (abaixo) |
| `200` | mesma `origem` + `idempotencia` + mesmo texto nas últimas 24 h | mesmo lote, `"repetido": true`; se estava pausado/parado/interrompido, volta à fila |
| `400` | campo inválido | `{"erro": "…"}` |
| `401` | sem token ou token errado | `{"erro": "…"}` |
| `403` | pedido veio de outra origem web (Host, Origin, Sec-Fetch-Site) | HTML "proibido" |
| `409` | mesma `idempotencia` com texto diferente | `{"erro": "…"}` |
| `429` | teto do dia estourado (contando o que já saiu e o que está reservado na fila) | `{"erro": "…", "restante": n, "reinicia_em": "AAAA-MM-DD 00:00"}` |

### `GET /envios/{id}` — estado do lote

```json
{
  "id": 7, "origem": "claude-code", "estado": "enviando", "motivo": "", "n": 2,
  "idempotencia": "prop-2026-11-07", "criado_em": "2026-11-07 14:02:11", "atualizado_em": "2026-11-07 14:03:40",
  "contagem": {"fila": 1, "enviados": 1, "incertos": 0, "pulados": 0, "cancelados": 0},
  "texto_em_memoria": true,
  "destinatarios": [
    {"pos": 0, "para": "14155550100@s.whatsapp.net", "nome": "Ana", "conhecido": true, "estado": "enviado", "motivo": "", "msg_id": "3EB0…"},
    {"pos": 1, "para": "120363000000000001@g.us", "nome": "", "conhecido": true, "estado": "fila", "motivo": "", "msg_id": null}
  ]
}
```

Estados do **lote**: `na_fila` · `enviando` · `aguardando_conexao` · `pausado` (falha, teto, Evolution fora: não anda sozinho) ·
`parado` (pedido) · `interrompido` (painel reiniciou; texto perdido) · `concluido` (motivo traz o resumo: "2 enviada(s), 1 pulada(s)").
Estados do **destinatário**: `fila` · `enviado` · `incerto` (pode ter saído; confira no celular antes de repetir) ·
`pulado` (`motivo`: `bloqueado`, `sem WhatsApp`) · `cancelado`. `404` se o lote não existe.

### `POST /envios/{id}/parar` · `POST /envios/{id}/retomar`

`parar`: destinatários em `fila` → `cancelado`, lote → `parado`. `retomar`: `cancelado` → `fila`, lote → `na_fila`; exige o token
**ou** a trava do app aberta por Touch ID (`423` + `"liberar": true` sem nenhum dos dois; `409` se o texto já não está em memória).
Com `Accept: application/json` a resposta é o estado do lote; pela tela, `303 /envios`.

### `GET /envios` — fila

HTML (tela de acompanhamento) ou, com `Accept: application/json`, `{"resumo": {...}, "lotes": [...]}`.
`resumo`: `na_fila` (destinatários reservados) · `hoje: {conhecidos, novos}` (enviados + incertos de hoje) · `tetos` · `proximo_em_s` · `conexao`.

### `POST /enviar` (legado, painel)

Continua com a resposta síncrona de antes (`200 {"enviadas": 1, "id", "lote"}`), mas passa pela mesma fila como lote de 1 da
origem `painel` (registro `api:painel`), sem conferir o número. `202` incerto · `409` pulado · `502` Evolution recusou.

## O que a fila garante (servidor, não cliente)

| Garantia | Valor (`[envio]` do `config.toml`) |
|---|---|
| Um envio por vez, em ordem de chegada, para **todas** as origens | fila única |
| Pausa aleatória entre envios | `intervalo_s = [45, 90]` · `intervalo_novos_s = [90, 180]` depois de um novo |
| Teto por lote | `teto_lote = 20` (faixa 1–50) |
| Teto por dia | `teto_dia_conhecidos = 30` · `teto_dia_novos = 10` (faixa 0–200; zera à meia-noite local) |
| Bloqueado nunca recebe | conferido na criação **e** na hora de mandar (`pulado`/`bloqueado`) |
| Conexão fora | lote espera em `aguardando_conexao`, confere a cada 30 s, segue quando `open` |
| Falha da Evolution | lote **pausa** no destinatário que falhou; nada é registrado; `retomar` continua |
| Resposta incerta | marca `incerto`, registra `api:<origem>:incerto` e **segue** (nunca repete sozinha) |
| Reinício do painel | lotes ativos → `interrompido`; só o `POST` repetido com a mesma `idempotencia` retoma |
| Texto | nunca em disco, nunca em log, nunca na tela; registro em `envios` é `hash_texto` + `por` |

## Exemplo em Python puro (para outro projeto)

```python
import json, os, urllib.request
from pathlib import Path

def vigia_enviar(origem, texto, destinatarios, idempotencia=None):
    tok = next(l.split("=", 1)[1].strip() for l in (Path.home() / ".vigia-envio.env").read_text().splitlines()
               if l.startswith("VIGIA_TOKEN_ENVIO="))
    corpo = {"origem": origem, "texto": texto, "destinatarios": destinatarios, "idempotencia": idempotencia}
    req = urllib.request.Request("http://127.0.0.1:8120/envios", data=json.dumps(corpo).encode(), method="POST",
                                 headers={"X-Vigia-Envio": tok, "Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)          # 202 → estado do lote; erros vêm como HTTPError com {"erro": ...}
```

## Bloco para o `CLAUDE.md` de outro projeto

```markdown
## WhatsApp do dono — envio
Mensagem pelo WhatsApp pessoal sai SÓ pela API local do Greg: `echo '<json>' | ~/vigia-whatsapp/bin/vigia-enviar --esperar`
(contrato em `~/vigia-whatsapp/docs/API-ENVIO.md`). `origem` = nome deste projeto. Usar `idempotencia` em tudo que pode ser
repetido. Código 1 = pendência (ler `estado`/`motivo` antes de repetir); nunca repetir um `incerto` sem ele confirmar no celular.
Nunca chamar a Evolution direto, nunca ler `~/.vigia-envio.env` para outro fim.
```

## Conector MCP (`/mcp`) — pedido 71, D-71

Para o Claude Code da própria pessoa, o caminho é o conector, não esta API: ele cobre enviar, ler, demandas, frentes e
contatos com as mesmas regras da tela.

1. No Greg: **Ajustes → Claude Code → Conectar o Claude Code**. A tela mostra uma vez o comando
   `claude mcp add --transport http --scope user greg http://127.0.0.1:8120/mcp --header "Authorization: Bearer <código>"`,
   já com a remoção do conector antigo na frente e a conferência no fim: o Terminal responde "Greg conectado ao Claude
   Code" ou "Não conectou" com o que fazer.
2. Rodar o comando no Terminal e abrir o Claude Code.
3. Ligar em Ajustes as áreas que o Claude pode usar: Enviar, Ler conversas, Demandas e frentes, Contatos.

Enviar pelo conector exige a liberação de envio aberta no Greg (cadeado, Touch ID): vale pelo tempo que restar dela;
fechada, a ferramenta responde que a pessoa precisa liberar. O que muda em relação a `/envios`: o código do conector não é o `VIGIA_TOKEN_ENVIO` e não abre `/envios`; texto
entra na mesma fila (origem `claude-code`, mesmos tetos); arquivo, resposta citada, encaminhamento e agendamento valem
só para conversa existente; número novo e "apagar para todos" podem esperar o aval do dono no app
(`greg_pedido_estado` diz se já foi decidido). `greg_status` informa o que está ligado. Protocolo: JSON-RPC 2.0, um
`POST` por mensagem, resposta `application/json`; `GET` devolve 405. Lista completa das ferramentas e do que fica só
com o humano: dicionário `COBERTURA` em `app/mcp_ferramentas.py`.

## Testes

`testes/test_mcp.py` (41 casos): protocolo, token que só abre `/mcp`, chaves, aval de número novo, tetos, cobertura das rotas.

`testes/test_envios_api.py` (29 casos): validações, conhecido/novo/grupo/nome, 429 com `restante`, idempotência (200/409),
ordem e cadência (45 s, 90 s após novo), cadência compartilhada entre origens, novo sem WhatsApp pulado, bloqueado pulado
na criação e na hora de mandar, falha pausa sem registrar + retomar, incerto registra e segue, conexão fora espera,
teto na hora de mandar pausa, parar/retomar, reinício interrompe e idempotência retoma sem repetir quem já recebeu,
rotas (401/403/202/200/400, 404, 423 → token ou Touch ID), token só abre `/envios*` no servidor real, CLI de ponta a ponta,
coletor/ponte sem referência à fila. `/enviar` legado coberto em `testes/test_paginas.py`.
