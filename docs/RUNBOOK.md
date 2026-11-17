# RUNBOOK — Greg do WhatsApp

Operação diária do vigia no Mac. Tudo roda local (`127.0.0.1`); nada de conversa sai do Mac.
Comandos partem da raiz do repositório (`~/vigia-whatsapp`). Saídas: 0 ok · 1 falhou · 2 não rodou.

## 1. Subir

Primeira vez (F5, com OK do dono): preencher `.env` e `infra/.env.compose` (a partir dos `*.exemplo`). O
`infra/.env.compose` **precisa** estar em `0600` (`chmod 600`): o `evolution.sh` recusa arquivo legível por grupo/outros.

```bash
chmod 600 .env infra/.env.compose
bin/evolution.sh up                      # sobe api/db/redis e aplica infra/sql/*.sql
bin/evolution.sh instancia               # cria a instância vigia (uma vez); grava a apikey sozinho no .env (0600)
tmutil addexclusion ~/.colima ~/vigia-whatsapp/dados   # Time Machine não guarda o cru nem os volumes do Colima
bin/instalar-launchagent.sh              # só mostra o que faria (dry-run)
bin/instalar-launchagent.sh --sim        # instala os 4 LaunchAgents e liga
bin/vigia.sh status                      # pausa · lock · colima · evolution · painel · últimas linhas do log
```

`instancia` não imprime a apikey nem pede para copiá-la: ela vai direto para `EVOLUTION_INSTANCE_APIKEY` no `.env`.
O instalador recusa `~/vigia-whatsapp` em caminho com `| & \ < >` e recusa o painel se `app/server.py` não existir.

Agentes instalados (todos sem KeepAlive, exceto o painel):

| Agente | Quando | Faz |
|---|---|---|
| `vigia-whatsapp` | minutos `:00` e `:30` (`StartCalendarInterval`) e ao ligar o Mac | `bin/vigia.sh` (ciclo) |
| `vigia-painel` | sempre (KeepAlive); `python3 -P app/server.py` | painel em `http://127.0.0.1:8120` |
| `vigia-semanal` | **todo dia** 20:05 | `bin/vigia.sh semanal` (relatório) |
| `vigia-semanal-limpeza` | **todo dia** 20:35 | `bin/vigia.sh semanal --executar-limpeza` |

Por que `StartCalendarInterval` e não `StartInterval`: `StartInterval` perdido durante o sono do Mac **não** dispara ao
acordar (`man launchd.plist`); `StartCalendarInterval` dispara ao acordar (vários disparos perdidos viram um). Isso
cumpre R-3 (Mac dormindo não é falha).

Por que os semanais rodam todo dia (D-32, reversível): se o Mac estava desligado no horário, a semana não se perde; as
guardas do `fechar-semana.py` (relatório já existe, aviso com menos de 24 h, `SEGURA`, mensagens não lidas, lápide) tornam
a repetição inofensiva, e semanas atrasadas são alcançadas no dia seguinte.

Rodar um ciclo na mão: `bin/vigia.sh ciclo`. O lock é `dados/vigia.lock` (guarda o pid do dono em `dados/vigia.lock/pid`;
só é recolhido se o dono morreu, ou, sem pid legível, após 1 h). O ciclo e o semanal seguram o Mac acordado com
`caffeinate` enquanto rodam. `bin/vigia.sh` sozinho = `ciclo`.

## 2. Parear (protocolo do QR)

O QR só aparece quando o dono aperta o botão; nunca em ciclo automático (R-11, P-3).

1. Abrir `http://127.0.0.1:8120/conexao` (pede login em `/login`, com o `VIGIA_TOKEN` do `.env`) e apertar **Gerar QR**.
   Cada QR vale 60 s; máximo 3 por clique; teto de 10 por dia.
2. No celular: WhatsApp → Aparelhos conectados → Conectar um aparelho → ler o QR. O aparelho aparece como **Greg** (Chrome).
3. Estado `open` fecha o QR. Se expirar sem leitura, o vigia faz logout sozinho: não deixar o QR "girando" (incidente documentado).
4. Parear só o número pessoal (R-1). Pareou: `pareado_em` é gravado (de novo a cada pareamento) e a leitura começa em
   `pareado_em − 1 dia` (R-2 alterada, D-53, seção 20): o WhatsApp manda o histórico recente, a Evolution grava, o Greg lê
   só o último dia, com os mesmos filtros (bloqueados, grupos permitidos).

## 3. Ver saúde

```bash
bin/vigia.sh status                       # resumo
bin/vigia.sh logs 60                      # últimas 60 linhas do log
curl -s http://127.0.0.1:8120/saude       # JSON: conexão, cursor, último lote, lápides, erros, QRs hoje (sem cookie)
```

**Métricas da fase A (D-45):** `/metricas` no app (ou `/metricas?formato=json&dias=14`, com o cookie do painel): precisão
da IA pelos rótulos dele, custo nocional e tokens por dia, duração dos ciclos, quedas da conexão, QRs, envios, itens do
Frentes. Rotular é em Demandas ("Rotular" em cada linha). Plano e critérios: `docs/specs/PLANO-FASE-A.md`.

Log único: `~/Library/Logs/vigia-whatsapp.log` — ciclo, coletor, monitor e avisos escrevem nele. Só ids e contagens,
nunca texto de mensagem. (`vigia-*.out.log`/`.err.log` ao lado são só a saída bruta que o launchd captura de cada agente.)

**Aviso do monitor.** Queda de conexão vira um aviso curto ao dono pelo bot da agência. Se o envio falhar, o ciclo grava
`dados/aviso-pendente.json` (`evento`, `motivo`, `criado_em`; sem texto de conversa) e **todo ciclo seguinte reenvia e apaga
no sucesso**. Existir esse arquivo = há aviso que ainda não chegou; ver `bin/vigia.sh logs` e se o cofre
(`localhost:8000`) está de pé. O caminho pelo cofre só existe se o `.env` tiver `AVISO_COFRE_ARQUIVO` (arquivo onde está o
token) e `AVISO_COFRE_CHAVE` (nome da variável dentro dele); sem as duas, vale o aviso direto (`AVISO_EVOLUTION_URL`).

`/saude` é somente leitura: com `dados/cursor.json` ilegível ela mostra `"cursor":{"erro":"CursorCorrompido","estado":"corrompido"}`
e **não** renomeia o arquivo; quem preserva o original como `cursor.json.corrompido-<ts>` é o coletor/monitor no ciclo seguinte.

## 4. Pausar e retomar

```bash
touch dados/PAUSADO    # pausa as leituras: o coletor não roda
rm dados/PAUSADO       # volta no próximo disparo
```

O botão "Pausar vigia" em `/conexao` faz o mesmo arquivo.

**O que a pausa faz (D-33, reversível):** pula o coletor, mas **o monitor de conexão continua rodando se a Evolution já
estiver de pé** — queda de conexão durante a pausa ainda gera registro e aviso (R-11). Com a Evolution fora, a pausa não
sobe nada: sem `colima start`, sem `evolution.sh up`; o ciclo sai com 0. Os jobs semanais também respeitam a pausa: com
`dados/PAUSADO` e a Evolution fora, `bin/vigia.sh semanal` registra "PAUSADO e Evolution fora; semanal adiado" e sai com 0
(sem Colima, sem `up`); com a Evolution de pé, roda normalmente. Assim `touch dados/PAUSADO` + `bin/evolution.sh down`
continua sendo o kill switch (20:05 e 20:35 não o desfazem).

**`up` automático (D-34, reversível):** o ciclo sobe Colima e Evolution sozinho quando estiverem fora. Por isso, **antes de
`bin/evolution.sh down`, pausar** (`touch dados/PAUSADO`), senão o próximo disparo do ciclo (até 30 min) sobe tudo de novo.

Mac desligado ou dormindo não é falha (R-3): o LaunchAgent dispara ao ligar/acordar e o coletor lê pelo cursor
`vigia.fila.seq`, sem perder mensagem.

**Kill switch pelo celular** (sem o Mac): WhatsApp → Aparelhos conectados → **Greg** → Desconectar. O vigia perde
a sessão na hora; o monitor registra a queda no próximo ciclo. Para parar de vez: pausar e `bin/evolution.sh down`.

## 5. Reparear

Quando o estado ficar `close` (desconectado, logout ou aparelho removido):

1. `bin/vigia.sh status` e `curl -s http://127.0.0.1:8120/saude` para confirmar `close`.
2. Se ainda aparecer "Greg" em Aparelhos conectados no celular, removê-lo lá.
3. Repetir a seção 2 (um clique, no máximo 3 QRs). Do período desconectado volta só o último dia antes do pareamento novo
   (D-53, seção 20; o que o WhatsApp mandar além disso fica sem leitura até a limpeza semanal); o cursor continua de onde parou.

## 6. Caso 428 ("Connection Closed" com estado `open`)

Sintoma: painel diz `open`, mas nada chega e o `fetchInstances` traz `disconnectionReasonCode` recente (socket morto).
Única saída documentada: **Reiniciar Evolution** no painel, ou na mão:

```bash
bin/evolution.sh restart                  # docker compose restart api
bin/vigia.sh status
```

Não gerar QR por causa de 428: a sessão está salva; reiniciar basta. Se persistir após 10 min, reparear (seção 5).
Socket que morre **sem** 428 (estado `open`, nada chega): é o caso da seção 17.

## 7. Fechamento semanal, limpeza e adiar

Todo dia 20:05 roda `bin/vigia.sh semanal` (relatório + aviso curto com contadores e link, uma vez por semana fechada) e
20:35 `bin/vigia.sh semanal --executar-limpeza`. A limpeza só apaga o cru da semana (`"Message"` e `vigia.fila`) se
o aviso tem 24 h ou mais, não há `SEGURA`, não restam mensagens não lidas; depois prova `count=0` e grava a lápide.
Classificações e itens do Frentes ficam. O `semanal` pega Colima, lock e Evolution como o ciclo (espera até 2 min o ciclo
corrente soltar o lock). Com `dados/PAUSADO` ele não sobe Colima nem Evolution (ver §4). `--segurar` e `--dry-run` não
exigem Colima, Evolution nem lock: o primeiro só grava `dados/segurar-<semana>` e o segundo só lê.

```bash
bin/vigia.sh semanal --dry-run             # ensaio: lista o que faria, não envia, não apaga
bin/vigia.sh semanal                       # relatório da última semana fechada
bin/vigia.sh semanal --segurar 7           # adia a limpeza 7 dias (DIAS opcional; padrão 7)
touch dados/SEGURA                         # adia por tempo indeterminado · rm dados/SEGURA para soltar
```

**Adiar é sempre por um desses dois comandos.** Responder "segura" ao aviso do bot **não** faz nada: o vigia não lê
resposta a aviso.

## 8. Restaurar o backup da semana

Antes de cada `DELETE`, a janela é salva em `dados/backup/<semana>-<epoch>.jsonl` (uma linha por mensagem, `0600`); backups
com mais de 7 dias são podados na limpeza seguinte (D-8). Restaurar só se a limpeza apagou algo que não devia, dentro desse prazo:

```bash
ls -l dados/backup/
bin/vigia.sh status                                              # Evolution tem de estar ok
python3 bin/fechar-semana.py --restaurar dados/backup/<semana>-<epoch>.jsonl --dry-run   # só valida e conta as linhas
python3 bin/fechar-semana.py --restaurar dados/backup/<semana>-<epoch>.jsonl             # insere em lotes
```

`--restaurar` reinsere em `"Message"` como dono `evolution` com `ON CONFLICT (id) DO NOTHING` (idempotente: rodar duas vezes
não duplica). O gatilho reenfileira em `vigia.fila` e o próximo ciclo do coletor relê as mensagens.

**O cru restaurado volta a ser pendente de limpeza.** Antes de inserir, o comando grava `dados/restaurada-<semana>`
(semana tirada do nome `<semana>-<epoch>.jsonl`, ou do `messageTimestamp` das linhas). Com essa marca, a lápide antiga da
semana deixa de bloquear: no próximo `semanal` / `--executar-limpeza` a semana entra como pendente, a limpeza roda de novo
(com backup novo e prova de `count=0`), uma lápide nova é acrescentada (`<semana>~relimpeza-<epoch>`; a antiga nunca é
apagada) e a marca é removida. Isso impede que o backup restaurado vire o histórico que R-2/R-12 proíbem. Para limpar sem
esperar o horário: `python3 bin/fechar-semana.py --executar-limpeza --semana <semana>` (as guardas de segura e não lidas valem).

**Como o SQL viaja.** A query vai pelo **stdin** de `bin/evolution.sh sql-stdin` (`psql -f -` dentro do container), em lotes de
até 200 linhas / 96 KiB medidos em **bytes**; uma linha maior que o teto vai sozinha. Nada de conteúdo de mensagem em `argv`:
não aparece no `ps` e não esbarra no limite de argumento do Linux (E2BIG).

## 9. Enviar mensagem pelo painel (`/enviar`, só a pedido do dono — R-9)

**Desde a D-44 (07/11/2026)** o caminho para ferramentas (Claude Code, scripts, cron) é a API `/envios` com fila, tetos e
idempotência: contrato e CLI (`bin/vigia-enviar`, JSON pelo stdin) em [`API-ENVIO.md`](API-ENVIO.md). O `/enviar` abaixo continua
valendo e passa pela mesma fila como lote de 1. Tela de acompanhamento: `/envios`.

Exige duas coisas: sessão do painel (`/login` com `VIGIA_TOKEN`) e o header `X-Vigia-Envio` com o token de
`~/.vigia-envio.env`. Com `curl --config` os segredos ficam em arquivo temporário `0600`, nunca na linha de comando (o `ps` mostraria):

```bash
umask 077; CFG="$(mktemp)"; JAR="$(mktemp)"
T="$(grep '^VIGIA_TOKEN=' .env | cut -d= -f2 | awk '{print $1}')"
E="$(grep '^VIGIA_TOKEN_ENVIO=' ~/.vigia-envio.env | cut -d= -f2 | awk '{print $1}')"
printf 'data = "token=%s"\n' "$T" > "$CFG"
curl -s -o /dev/null -c "$JAR" --config "$CFG" http://127.0.0.1:8120/login
printf 'header = "X-Vigia-Envio: %s"\nheader = "Content-Type: application/json"\ndata = "{\\"para\\":\\"55DDNNNNNNNNN\\",\\"texto\\":\\"Olá\\"}"\n' "$E" > "$CFG"
curl -s -b "$JAR" --config "$CFG" http://127.0.0.1:8120/enviar
rm -f "$CFG" "$JAR"
```

Número no padrão `55DDNNNNNNNNN`; o texto não pode conter aspas duplas nem barra invertida nesta receita. Resposta
`{"enviadas":1,...}` = enviado; `"incerto":true` = conferir no celular antes de repetir.

**Sair do painel (`POST /sair`).** Revoga **todas** as sessões: incrementa a geração em `dados/sessao.json` (0600) e o
cookie é `<emitido>.HMAC(token, "painel-v2:<geração>:<emitido>")`, então um cookie copiado antes do `/sair` deixa de entrar, e qualquer cookie para de valer 30 dias depois de emitido (`VALIDADE_SESSAO` em `app/server.py`; o Greg.app entra de novo sozinho). Apagar
`dados/sessao.json` volta a geração a 0 (o que reativaria cookies antigos: não faça isso para "limpar").

**`vigia-ponte atualizar <item_id> 'nota' --msg <msg_id>`.** O `--msg` é obrigatório: conversa e contato vêm do
`mensagens.json` do lote (`VIGIA_LOTE`) e a nota fica gravada com o `msg_id` real (repetir não duplica). Uso manual fora
de um lote: `--msg` com qualquer id novo herda a conversa da última classificação do item. `VIGIA_DADOS` (ambiente) >
`dados_dir` do `config.toml` > `dados/` define a pasta de dados do painel, do monitor e da ponte.

## 10. Desinstalar

```bash
touch dados/PAUSADO                            # impede o ciclo de subir a Evolution de novo
bin/instalar-launchagent.sh --remover          # dry-run
bin/instalar-launchagent.sh --remover --sim    # launchctl bootout + apaga os 4 plists
bin/evolution.sh down                          # derruba api/db/redis (volumes ficam)
```

Apagar a sessão de vez: desconectar "Greg" no celular e `docker compose ... down -v` (apaga os volumes, inclusive o cru).

## 11. Segredos (Keychain desligado) e CI local (fase A, D-45)

Os quatro segredos moram em arquivos 0600: `VIGIA_TOKEN`, `EVOLUTION_INSTANCE_APIKEY` e `FRENTES_TOKEN` no `.env` do
projeto; `VIGIA_TOKEN_ENVIO` em `~/.vigia-envio.env`. O Keychain do macOS (`app/segredos.py`) está **desligado por decisão
do dono (07/11/2026)**: nenhum comando do Greg, da suíte, de LaunchAgent ou do Greg.app chama o `security` a menos que
`VIGIA_KEYCHAIN=on` esteja no ambiente. **Não ligar sem ele presente.** Trocou um segredo (editou o arquivo) →
`launchctl kickstart -k gui/$(id -u)/io.vigia.vigia-painel` e reabrir o Greg.app. Sintoma de chave ausente: painel
não sobe com `faltam no .env: …`; app avisa "Sem VIGIA_TOKEN".

Se um dia o Keychain for ligado (só com ele, `VIGIA_KEYCHAIN=on` no ambiente de cada leitor):

```bash
python3 -m app.segredos conferir          # por chave: está no Keychain? está no arquivo? (nunca imprime valor)
python3 -m app.segredos migrar            # arquivos → Keychain (confere relendo) e tira as linhas secretas do .env
python3 -m app.segredos exportar          # caminho de volta: Keychain → .env e ~/.vigia-envio.env (0600)
printf '%s\n' "$VALOR" | python3 -m app.segredos gravar NOME   # trocar um segredo (valor pelo stdin, nunca argv)
```

Suíte: sempre `python3 -W ignore -m unittest discover -s testes -t .` (ou `bin/ci.sh`). Sem o `-t .`, o
`testes/__init__.py` não é importado — foi assim que uma rodada paralela alcançou o `security` real em 07/11.

**CI local.** `bin/ci.sh` roda compila · gitleaks · compose · suíte (≈ 2 min) e é o VERDE da CONSTITUTION. O hook
`.githooks/pre-commit` chama `bin/ci.sh --pre-commit` em todo commit (ativado por `git config core.hooksPath .githooks`,
já feito neste clone; clone novo repete o comando). Nada sai do Mac. Pular só em emergência: `git commit --no-verify`,
dizendo por quê na mensagem.

## 12. Fora do Greg (`/bloqueios`, pedido 29, D-46)
Página para escolher **na agenda** quem fica fora: abas Grupos · Contatos · Sem número salvo (quem escreve por `@lid`),
busca na própria página e marcação múltipla. Mesma mecânica do cartão Bloqueios em Ajustes: `vigia.bloquear(jid)` por
conversa marcada, que apaga o que já estava guardado e barra o que chegar (D-41). No celular nada muda.
- **"O banco da Evolution não respondeu agora"** (sem formulário, abas com "–"): a página lê a tabela `Contact` com o
  usuário `vigia_ro`; ver a seção 3 (saúde) e o container. A página responde 200 mesmo assim.
- **Aba vazia**: a Evolution ainda não sincronizou esse tipo (grupo só entra na agenda quando o celular manda o assunto).
- **"O banco parou de confirmar depois disso"** (`?falhou=1`): o lote parou na primeira falha; os que saíram já aparecem em
  "Fora agora". Marcar de novo só os que faltaram.
- **Teto**: 200 conversas por confirmação (cada uma é um `psql`, ~0,1 s). Acima disso, marcar menos e repetir.
- **Nome "Grupo" ou só número**: o nome vem da agenda (`pushName`); apelido dado em Conversas vence a agenda.
- **Desfazer**: "Desbloquear" em "Fora agora" ou em Ajustes; vale só para o que chegar depois (o apagado não volta).
- **Prova depois de mexer**: `testes/test_bloqueios_seletor.py` na suíte (`-t .`) e, no painel, `GET /bloqueios` 200 com as
  três abas contadas.

## 13. Fora do Greg pela área de conversas (pedido 31, D-48)
Sem sair de `/conversas`: ⋯ no cabeçalho da conversa aberta (Ver demandas · Nome no Greg… · Deixar fora do Greg), ⋯ que
aparece ao passar o mouse na linha da lista ou **botão direito** na linha (Abrir · Deixar fora do Greg), e **Selecionar…**
no ⋯ do topo da lista (caixa em cada linha, "N marcadas" embaixo, um "Deixar fora do Greg" para todas). Tudo cai no mesmo
`POST /bloqueios` de D-41/D-46: apaga o guardado, barra o que chegar, a IA não lê. No celular nada muda.
- **A conversa sumiu da lista depois de confirmar**: é o esperado; a lista volta dizendo "1 conversa fora do Greg."
  Desfazer em `/bloqueios` → "Fora agora" (vale só para o que chegar depois; o apagado não volta).
- **"O banco falhou no meio: N saíram"**: mesma regra de D-46 — marcar de novo só os que faltaram.
- **O ⋯ da linha não aparece ou o botão direito abre o menu do navegador**: o JS da página não rodou (erro no console do
  Greg.app); recarregar. Sem JS a página só lê — deixar fora por `/bloqueios`.
- **A lista "congelou"**: o refresh de 15 s não troca a lista enquanto um menu de linha está aberto ou o modo Selecionar
  está ligado; Escape ou Cancelar destrava.
- **Prova depois de mexer**: `testes/test_conversas_menu.py` na suíte (`-t .`); no painel, abrir uma conversa, ⋯ → "Deixar
  fora do Greg" → Cancelar; na lista, ⋯ → Selecionar… → marcar duas → "2 marcadas" → Cancelar.

## 14. Aparência: preferências e estilo Papel (ds05)
- **Onde fica**: `dados/preferencias.json` (0600, troca atômica). Chaves: `fonte` (13–18; Ajustes mostra Pequena/Padrão/Grande/
  Muito grande) e `tema` (sistema|claro|escuro). Valor fora da lista cai no padrão (`app/preferencias.validar`). Chaves das rodadas
  anteriores (`estilo`, `acento`, `vidro`, `fundo`, `fundo_claro`, `fundo_escuro`) são ignoradas na leitura e somem na próxima gravação.
- **Cor de destaque**: no Greg.app é a do macOS (Ajustes do Sistema › Aparência): o app injeta `data-app="mac"` e `data-acento-nome`
  e a base usa `AccentColor`. No navegador fica o azul #007AFF / #0A84FF. Ajustes só informa qual é; não há escolha.
- **Voltar ao padrão**: apagar `dados/preferencias.json` e recarregar.
- **Mexeu em `app/templates/_base.html`?** É a única pele (D-25). Gate antes de mostrar: `python3 ~/ferramentas/fabricas/design/bin/
  lint-tokens.py app --config docs/design/lint-papel.toml --ext html,py` (0 erro) e `prova-visual.mjs --larguras 1120` sobre o HTML
  de `docs/imagens/gerar.py --html <pasta>` (0 erro nas 12 telas). Depois `python3 docs/imagens/gerar.py` refaz as capturas do README
  e `docs/design/canvas/gerar_pranchetas.py` as pranchetas do artefato Design «Greg · Papel».
- **Prova visual de uma mudança de tela**: capturar com `capturar-painel.mjs` e provar a 1120 (janela real do app) nos dois temas; antes
  de qualquer captura que troque preferências, copiar `dados/preferencias.json` e restaurar no fim (o painel é o vivo).
- **Greg.app**: a casca (`macapp/Vigia/main.swift`) tem o menu Visualizar (⌘1–6, ⌃⌘S, ⌘N, ⌘F, ⇧⌘P, ⇧⌘B, ⌘R) e as alças de arraste
  (topo da lateral, 46 pt de altura e da largura dela, §25; e faixa de 10 pt no topo). Mudou o Swift: `bin/construir-app.sh`. Reiniciar o painel:
  `launchctl kickstart -k gui/$(id -u)/io.vigia.vigia-painel`.

## 15. Transcrição automática de áudio (pedido 30, D-47)
Todo áudio que chega vira texto dentro do ciclo de 30 min, ANTES de montar o lote, por um motor LOCAL que já estava no Mac
(`mlx_whisper` + `mlx-community/whisper-large-v3-turbo` no cache do Hugging Face, modo offline: nada sai da máquina). O
coletor chama `bin/motor-transcricao.py` em UM processo por rodada, com `nice -n 15`, e grava o texto em `dados/vigia.db`
(tabela `transcricoes`). O texto entra na linha do lote (`[áudio 0:40] transcrição automática: "…"`) e aparece abaixo do
player na conversa aberta. Tetos em `config.toml [transcricao]` (20 áudios e 900 s por ciclo; áudio > 10 min só os
primeiros 10 min; < 2 s pulado). Áudio de grupo é transcrito mesmo que a IA não leia o grupo; bloqueado não.
- **Atrasados** (chegaram antes do D-47 ou os tetos adiaram): `/opt/homebrew/bin/python3 bin/transcrever.py --dry-run`
  conta; sem `--dry-run` faz uma rodada com os tetos do config (`--teto N --segundos S` mudam só esta rodada). Repetir
  até `adiados` chegar a 0. Um motor por vez é garantido pela trava do ciclo (`dados/vigia.lock`, `app/trava.py`, mesmo
  formato do `vigia.sh`): com o ciclo rodando o `transcrever.py` sai com 2 sem fazer nada, e com ele rodando o ciclo do
  LaunchAgent sai com 2 e volta na rodada seguinte. Medido 08/11/2026 na rodada padrão: 20 áudios, 876 s de áudio em 74 s
  de relógio (motor 69 s, ≈ 0,08 s por segundo de áudio). Dezenas de áudios de uma vez é lote: só com o dono avisado da
  duração (regra de 04/11/2026).
- **Ver o estado**: `sqlite3 dados/vigia.db "SELECT estado, count(*) FROM transcricoes GROUP BY 1"` (ok · cortado ·
  falhou · pulado). Nunca `SELECT texto` em terminal compartilhado: é conteúdo de conversa.
- **"falhou"**: o ciclo tenta de novo até 3 vezes (coluna `tentativas`). Causas vistas: Evolution devolveu mídia que
  não é áudio (`MidiaInvalida`), motor sem o modelo no cache (ver abaixo), timeout. Depois de 3, só `transcrever.py`
  depois de apagar a linha: `sqlite3 dados/vigia.db "DELETE FROM transcricoes WHERE estado='falhou'"`.
- **Motor ausente ou modelo fora do cache**: `ls ~/.cache/huggingface/hub | grep whisper-large-v3-turbo` e
  `/opt/homebrew/bin/mlx_whisper --help`. Sem motor o ciclo segue como antes (áudio como marcador), com aviso no log.
  Baixar modelo é tráfego externo: só com o dono ciente (regra "nada sai do Mac").
- **Desligar**: `ativo = false` em `[transcricao]`; o que já foi transcrito continua no banco até a limpeza semanal.
- **Limpeza semanal**: `fechar-semana` apaga as transcrições da janela (`transcricoes_apagadas` no resultado) e prova
  `count=0` antes da lápide; o cru em `dados/caixa/.audio/` só existe durante o processo do motor.
- **Prova depois de mexer**: `testes/test_transcricao.py` e `test_coletor.TestCicloTranscricao` na suíte (`-t .`);
  real: `transcrever.py --dry-run` e uma rodada pequena (`--teto 3`), depois a contagem por estado acima.

## 16. Frente de cada conversa (`/contatos`, pedido 34, D-49)
- **Marcar**: menu Contatos → aba (Conversas ativas é a padrão) → marcar uma ou várias (ou "Marcar os visíveis" depois de
  buscar) → escolher a frente embaixo → Mandar marcados. "Nenhuma frente (conferido)" grava que não tem frente e a
  sugestão não volta. "Tirar marcação" apaga o vínculo.
- **Sugestões**: o ciclo recalcula a cada rodada pela agenda (`vinculos.semear`); só sugestão **forte** é gravada
  (`origem=auto`) e vale para a IA; o item nasce na frente como qualquer outro (desde o D-58 nada nasce "a confirmar": a
  confiança baixa fica só registrada no cartão). "Confirmar as N sugestões fortes" na aba Sugestões
  passa todas para `dono`. Termo que gera falso positivo: incluir em `FRACOS` de `app/vinculos.py`.
- **Aprende sozinho**: mover um item do WhatsApp de frente no Frentes, ou usar o seletor Frente em Demandas, grava o
  vínculo da conversa como `dono` (resumo do ciclo: `vinculos.aprendidos`).
- **Ver o estado**: `sqlite3 dados/vigia.db "SELECT origem, frente_id IS NULL, count(*) FROM vinculos GROUP BY 1,2"`.
- **Desfazer tudo**: `DELETE FROM vinculos` volta ao comportamento anterior (a IA escolhe a frente sozinha).
- **Conversa do sistema** (`[leitura] conversas_do_sistema` em `config.toml` somada a `VIGIA_CONVERSAS_DO_SISTEMA` no `.env` (o jid real mora no `.env`, pedido 37)): a IA não lê; o painel mostra.

## 17. Socket zumbi e silêncio (pedido 41, D-50)

Caso de 08-09/11/2026: o Mac dormiu, o socket do Baileys morreu (`error in sending keep alive`, depois `1006 … init
queries` em loop), a Evolution ficou dizendo `open` e **17 h sem mensagem** passaram sem alarme. Agora o monitor
(`python3 -m app.conexao --monitorar`, dentro de cada ciclo) tem duas camadas:

| Sinal | Como detecta | O que faz sozinho | Aviso no WhatsApp |
|---|---|---|---|
| `socket_zumbi` | sonda `fetchProfilePictureUrl` do `DONO_WHATSAPP` falha 2× (timeout/5xx) ou volta vazia depois de já ter devolvido foto | `bin/evolution.sh restart`, no máximo 1 a cada 2 h | "sem resposta (socket zumbi); Evolution reiniciada sozinha" · persistente: "continua sem resposta depois do reinício: gere o QR de novo" (2 avisos por episódio) |
| `silencio` | `dados/ultima-mensagem.json` (gravado pelo coletor com `max(messageTimestamp)`) com ≥ 6 h | **nada** (pode ser domingo quieto) | 1×/dia, só entre 9 e 21 h: "aberta, mas sem mensagem nova há N h: confira Aparelhos conectados" |

- **Ver**: Conexão → "Última mensagem recebida" (vermelha a partir de 6 h) e o aviso no topo; `cat dados/conexao.json`
  (`sonda`, `sonda_ok_em`, `zumbi_avisos`, `silencio_avisado`); `dados/ultima-mensagem.json`.
- **Quando a sonda passa mas nada chega** (o caso real): o WhatsApp parou de entregar a este aparelho. No celular,
  WhatsApp › Aparelhos conectados: se o Greg não está lá, ou está mas nada entra, **Parear de novo…** (seção 18).
- **Desligar a sonda**: tirar `DONO_WHATSAPP` do `.env` (o monitor segue só com 428/queda/silêncio).
- **Ajustar**: constantes no topo de `app/conexao.py` (`SONDA_TENTATIVAS`, `REINICIO_SONDA_INTERVALO_S`, `SILENCIO_AVISO_S`,
  `SILENCIO_HORAS`).
- **Indicador da lateral (pedido 43, D-53)**: âmbar "Conectado, sem mensagem há N h" ou "Conectado, socket sem resposta"
  enquanto o último ciclo do monitor for `silencio`, `socket_zumbi` ou `socket_morto`; verde de novo no ciclo em que a
  mensagem chega. "Lido às HH:MM" = `dados/ultima-leitura.json`, gravado em **todo** ciclo real (fila vazia inclusive).
  A lateral se atualiza sozinha a cada 30 s e ao voltar à aba (`GET /conexao/lateral`, nunca chama a Evolution).
- **401 com a instância `open`** (Parear de novo…, ou aparelho removido no celular, seguido do QR novo entre dois ciclos)
  = `repareado`, evento `voltou`, sem aviso. Um 428 ou outro código novo com a instância `open` continua `socket_morto`.
- **O Mac dormiu, ficou sem internet, foi desligado (pedido 43, D-54) — ao ligar, recupera:** a Evolution reconecta, o
  WhatsApp entrega o que ficou retido e o ciclo lê e analisa. O monitor lê `sysctl -n kern.waketime` (quando acordou) e
  `pmset -g systemstate` (com *Graphics* = despertar pleno; sem = DarkWake de manutenção, o Mac segue dormindo para você).
  *DarkWake:* nada a julgar — `sonda_adiada: despertar_escuro`, `close` vira `caiu_apos_despertar` sem aviso, silêncio só
  âmbar. *Despertar pleno:* instância fechada → espera até 120 s a reconexão; sonda surda → resonda até 120 s e, se continuar
  surda, reinicia a Evolution na hora, sem aviso; com `recuperacao` no JSON (`reconectou`, `sonda_voltou`, `reiniciada`) o
  `vigia.sh` espera 60 s (`VIGIA_ESPERA_RECUPERACAO_S`) e roda o coletor de novo no mesmo ciclo (log: "segunda leitura").
  O aviso no WhatsApp só sai no ciclo seguinte, se o problema persistir acordado. `sem_foto` com mensagem nova desde o ciclo
  anterior também não condena (`sonda_adiada: mensagem_recente`). Aviso pendente de problema já resolvido chega como
  "conexão caiu às HH:MM, já voltou". Ver o sono: `pmset -g log | grep -E "Sleep|Wake"` (7 s e 20 MB: não é para o ciclo).

## 18. Ler agora e Parear de novo (pedido 41, D-51)

- **Ler agora** (Conexão → menu ··· ou o botão no vazio "Nada novo" de Demandas): `launchctl kickstart
  gui/$(id -u)/io.vigia.vigia-whatsapp`, o mesmo ciclo da meia hora; a trava do `vigia.sh` impede dois ao mesmo
  tempo. Com `dados/PAUSADO` não dispara (toast avisa). Em até um minuto o resultado aparece em Demandas (F5 ou o
  próprio polling).
- **Intervalo e horário de leitura (pedido 81, D-82):** Ajustes → Leitura. O agendador continua disparando em :00 e
  :30; `bin/coletor.py` decide no começo se o disparo lê. Disparo que não lê aparece no log como `sem leitura neste
  disparo: <motivo>` e grava `dados/ultima-passada.json`; o monitor e o fechamento do dia rodam do mesmo jeito. Ler
  agora e a leitura depois de parear deixam `dados/LEITURA-PEDIDA` e leem fora do intervalo e do horário. À mão:
  `VIGIA_LER_AGORA=1 bin/vigia.sh ciclo` ou `bin/coletor.py --agora`. Voltar ao de antes: 30 minutos e as duas horas
  iguais em Ajustes (ou apagar `leitura_min`, `leitura_de` e `leitura_ate` de `dados/preferencias.json`).
- **Parear de novo…** (Conexão → menu ···, só com a conexão `open`): `DELETE /instance/logout` com confirmação; o
  cursor fica (`pareado_em`, `ultimo_seq`), então nada é relido. Depois: **Gerar QR** com o celular na mão (seção 2).
  Reiniciar Evolution (seção 6) continua sendo o primeiro remédio; Parear de novo é para o socket que o reinício não cura.
- **Depois de mudar código do painel**: `launchctl kickstart -k gui/$(id -u)/io.vigia.vigia-painel` e reabrir o
  Greg.app — o processo antigo segue servindo o código velho (em 09/11 um `FileNotFoundError` de template apagado veio daí).

## 19. Arquivar frente e área Pessoal (pedido 41, D-52)

- **Arquivar**: Frentes → abrir a frente → menu ··· → "Arquivar frente" (confirmação). No Frentes a frente vira
  `status=arquivada` (itens ficam lá); no Greg ela some da árvore, dos cartões, dos seletores e do cabeçalho que a IA
  recebe (`frente: sem vínculo (a frente antes marcada foi arquivada…)`). O toast tem **Desfazer**.
- **Desarquivar**: grupo "Arquivadas (N)" no fim da árvore (fechado) → Desarquivar; ou abrir `/frentes/<id>` → Desarquivar.
- **Registro local**: `sqlite3 dados/vigia.db "SELECT * FROM frentes_arquivadas"` (migração 008). Só o Greg grava aqui; frente
  arquivada direto no Frentes não aparece na lista (a API só devolve ativas) — para trazê-la, `PATCH /api/frentes/<id>`
  `{"status":"ativa"}` com o token do dono, ou pela ponte: `FrentesPonte(...).alterar_frente(id, status="ativa")`.
- **Pessoal**: frentes de tipo `pessoal` entram na seção "Pessoal" (última da árvore). Hoje: Dono Neto (25), Saúde / Nutri
  (29), Família (31). Nova frente pessoal: `FrentesPonte(...).criar_frente("Nome", tipo="pessoal", apelidos=[...])` ou
  no próprio Frentes. A frente antiga "Pessoal" (9) está arquivada no Frentes desde antes; `alterar_frente(9, status="ativa")`
  se quiser de volta.

## 20. Histórico de 1 dia no pareamento (pedido 43, D-53)
- **Regra:** a cada pareamento o Greg lê a partir de `pareado_em − 24 h` (`Cursor.desde`, `HISTORICO_S` em `app/cursor.py`),
  com os mesmos filtros de sempre (bloqueados, `[grupos].permitidos`, tipos sem conteúdo). Antes de 09/11 era "nada antes
  do pareamento".
- **Como acontece:** o WhatsApp manda o histórico recente no pareamento (tamanho dele, em geral mais que 1 dia);
  `DATABASE_SAVE_DATA_HISTORIC=true` no compose faz a Evolution gravar; o gatilho da fila enfileira; o coletor descarta o que
  está antes de `desde` (avança o cursor, sem Claude) e lê o resto. Painel, transcrição e fila de envio também só olham
  `>= desde`. O excedente fica no Postgres local, sem leitura, até a limpeza semanal: ao limpar a semana mais antiga
  pendente, `fechar-semana.py` apaga também tudo anterior a ela (backup como o resto; lápide `anteriores-<semana>`).
  Essa varredura não roda com semana restaurada pendente (I2) e, se falhar, não derruba o fechamento da semana: fica
  um `varredura dos anteriores falhou` no log e ela tenta de novo na próxima semana.
- **`pareado_em` muda a cada pareamento:** no painel, quando um QR saiu e a instância abriu; pelo monitor, quando um 401
  é mais novo que o `pareado_em` gravado e a instância está `open` (log `pareado_em regravado pelo monitor`). "Gerar QR"
  com a instância já aberta não mexe. Ver: `cat dados/cursor.json`; `/saude` traz `desde`.
- **Ajustar a janela:** `HISTORICO_S` em `app/cursor.py` (testes em `test_coletor.py::…test_janela_de_um_dia…`).
- **Mudou o compose?** `bin/evolution.sh up` recria a `api` com o `.env` novo (a sessão do WhatsApp fica no Postgres e
  sobrevive; `restart` NÃO relê variáveis). Conferir: `docker exec vigia-whatsapp-api-1 sh -c 'env | grep HISTORIC'`.

## 21. Conexão "verde" e nenhuma mensagem nova: stream fechado pelo `<status>` (pedido 44, D-55)
- **Sintoma:** lateral verde ou âmbar "sem mensagem há N h", `/conexao` mostra `open`, sonda respondendo, Postgres sem
  linha nova há horas — nem as mensagens que você mesmo mandou aparecem. Nos logs da Evolution, a cada ~50 min:
  `stream errored out` com `reasonNode` `{"tag":"ack","attrs":{"class":"status",…}}`, e 30 s depois a Evolution regrava
  o **mesmo** lote antigo (`transaction failed, rolling back` = duplicata).
- **Causa:** o WhatsApp passou a mandar o status dos contatos como stanza de topo `<status>`. O Baileys 7.0.0-rc.9 (o da
  imagem v2.3.7) não a confirma; o servidor fecha o stream e, na reconexão, reentrega o lote pendente e segura o resto.
  Parear de novo zera a fila do servidor e "resolve" por algumas horas — foi o que aconteceu de 08 para 09/11.
- **Correção em vigor:** `infra/patches/baileys-messages-recv.js` montado `:ro` por cima de
  `/evolution/node_modules/baileys/lib/Socket/messages-recv.js` (compose, serviço `api`). Detalhe, origem e md5 em
  `infra/patches/README.md`.
- **Conferir que está valendo:**
  `docker exec vigia-whatsapp-api-1 md5sum /evolution/node_modules/baileys/lib/Socket/messages-recv.js` = `md5 -q infra/patches/baileys-messages-recv.js`;
  `docker logs vigia-whatsapp-api-1 --since 2h 2>&1 | grep -c 'class":"status'` tem que ser `0`.
- **Voltou a acontecer?** (1) conferir o md5 acima — montagem perdida é a primeira suspeita depois de mexer no compose;
  (2) `bin/evolution.sh up` recria a `api` com a montagem (a sessão fica no Postgres; não precisa de QR); (3) se o
  `reasonNode` tiver **outra** `class`, é stanza nova sem ouvinte: mesma receita, novo trecho no remendo, e procurar
  nos issues da evolution-api / Baileys.
- **Trocar a imagem da Evolution:** antes de mudar a tag, ler o README do remendo. Imagem com Baileys ≥ rc.10 com o
  `CB:status` já traz a correção: tirar a montagem do compose, o teste de contrato e o arquivo; senão, copiar o
  `messages-recv.js` da imagem nova (`docker cp`), reaplicar os 3 trechos e atualizar o md5 do README.

## 22. Canário de entrega (pedido 44, D-56)
- **Por quê:** `open` e sonda ok não provam entrega (§21). Só mensagem chegando prova. Então, quando a conexão fica
  calada, o Greg manda uma mensagem a si mesmo e confere se ela entra.
- **Quando dispara:** conexão `open`, número pareado, Mac acordado (não é o ciclo de um despertar), nenhuma mensagem
  nova há **2 h** (`CANARIO_SILENCIO_S`) e hora local entre 9 e 21 (`SILENCIO_HORAS`). No máximo **um teste a cada 6 h**
  (`CANARIO_REPETE_S`): num dia inteiro sem ninguém escrever saem 2 mensagens do bot, por volta de 11 h e 17 h.
- **O que acontece:** o bot da agência manda `Vigia: teste de entrega HH:MM` ao seu número (mesmo canal dos avisos, env
  `DONO_WHATSAPP` e `AVISO_INSTANCIA` exportados pelo `vigia.sh`). O monitor espera a linha em `"Message"` por 2 min
  (`lotes.canario_chegou`, `psql-ro-stdin`, só conta se o próprio texto apareceu).
  - **Chegou** → `canario=chegou` no JSON do monitor, ciclo `ok`, nenhum aviso (o silêncio é gente calada). O próximo
    ciclo lê o teste como mensagem normal e o relógio do silêncio zera.
  - **Não chegou** → reinicia a Evolution (`bin/evolution.sh restart`, nunca duas vezes em 2 h), espera reconectar (até
    2 min) e confere de novo (2 min). Destravou → `canario=chegou_apos_reinicio`, `recuperacao=reiniciada`: o `vigia.sh`
    roda o coletor de novo 60 s depois e lê o que estava retido; sem aviso; histórico ganha `entrega_travada /
    reiniciada_voltou`. Não destravou → evento **`entrega_travada`** (motivo `_reiniciada`, `_reinicio_falhou`,
    `_reinicio_recente` ou puro), aviso no WhatsApp "aberta, mas a entrega travou… use Parear de novo", lateral
    **vermelha** "Conectado, entrega travada".
  - **Bot ou cofre fora** → `canario=envio_falhou`, nada se conclui, tenta no próximo ciclo; o aviso de silêncio de
    6 h (§17) continua valendo como rede de segurança.
- **Ler no log:** `grep -E "canário|canario=" ~/Library/Logs/vigia-whatsapp.log` — `canário chegou: entrega ok`,
  `canário não chegou em 120 s`, `canário chegou depois do reinício`, `canário não enviado`.
- **Ver no estado:** `cat dados/conexao.json` traz `canario_em` (último teste) e `canario_ok_em` (último que chegou).
- **Desligar ou ajustar:** constantes `CANARIO_*` em `app/conexao.py`; para desligar de vez, tirar `canario_enviar` do
  `main` (o monitor volta a só avisar o silêncio, como antes). O painel nunca manda canário.
- **Custo:** zero em dinheiro (instância própria do bot). Em ruído: no máximo 2 mensagens do bot num dia inteiro calado.

## 23. Fechamento do dia e cartões provisórios (pedido 45, D-57)
- **Por quê:** uma conversa muda ao longo do dia ("14h… melhor 15h… fica para quinta"); subindo ao Frentes a cada leitura,
  o mesmo evento virava dois ou três itens. Agora a IA lê a cada 30 min, mas só **martela no fechamento do dia**.
- **O que você vê em Demandas:** cartão de demanda/lembrar nasce com a pílula cinza **provisório** e "Sobe para o Frentes às
  21:00"; o rodapé diz "N sobem às 21:00". Ele é editável como qualquer outro (classe, frente) e **Subir agora** o leva ao
  Frentes já confirmado. **Atenção** e **prazo até amanhã** não esperam: sobem na hora, como antes. Recusar um provisório
  não toca o Frentes ("não chegou a subir"). Cartão que a IA substituiu (evento remarcado ou cancelado) sai da lista e
  o novo traz a marca **atualizado** com "Antes: …"; o filtro **Substituídos** mostra os antigos e **Restaurar** devolve
  qualquer um em um clique.
- **Quando roda:** `bin/vigia.sh ciclo` chama `bin/fechar-dia.py` depois do coletor e do monitor, a cada 30 min, **só se o
  coletor saiu em 0**. Antes de `[horarios].diario` (`config.toml`, 21:00) ele sai sem fazer nada; depois, fecha o que
  estiver pendente — se o Mac estava desligado às 21 h, fecha no primeiro ciclo em que ligar. Cartão criado depois do
  fechamento é do dia seguinte.
- **O que o fechamento faz:** (1) toda conversa com cartão provisório ganha um passe do Claude (`prompts/consolidar.md`)
  que lê os cartões e a conversa do dia, **segura o que era só conversa** (`vigia-ponte rebaixar`), funde o que é o mesmo
  assunto (`vigia-ponte consolidar`) e corrige o cartão que já está no Frentes em vez de criar outro (§24); (2) tudo o que
  sobrou sobe ao Frentes, já confirmado (frente arquivada vai ao Inbox). O passe entra em `/metricas` como lote
  `fecho-<dia>`.
- **Rodar à mão / conferir:**
  ```bash
  cd ~/vigia-whatsapp && /opt/homebrew/bin/python3 bin/fechar-dia.py --dry-run
  ```
  `--dry-run` só conta (`conversas`, `cartoes`, `com_repetidos`, `com_lancados`) sem chamar Claude nem Frentes. Sem a flag, fecha de
  verdade (idempotente: rodar duas vezes não sobe nada duas vezes). Saída 0 ok (inclui antes da hora, nada pendente e
  adiada) · 1 falhou · 2 não rodou (Postgres/Frentes fora, sem pareamento).
- **Ler no log:** `grep -E "fechamento" ~/Library/Logs/vigia-whatsapp.log` — `fechamento 2026-11-09 21:00: conversas=…
  cartoes=… repetidas=… absorvidos=… rebaixados=… lancados=…`; `claude falhou (…), tentativa n/3` = ficou para o próximo ciclo;
  `3 falhas do Claude; sobe sem conferir` = desistiu do passe naquele dia e subiu tudo. O contador das falhas vive em
  `dados/fechamento-falhas.json` e some quando o fechamento passa.
- **Se o Frentes estiver fora às 21 h:** `fechar-dia` sai em 2, nada se perde: os cartões continuam provisórios e sobem no
  ciclo seguinte. O mesmo vale para uma leitura de 30 min: atenção que não conseguiu subir fica provisória.
- **Ajustar:** a hora em `config.toml` `[horarios].diario`; a janela "prazo até amanhã sobe na hora" em
  `app/lancamento.py` (`JANELA_PRAZO_DIAS`); o número de tentativas do Claude em `app/consolidacao.py`
  (`MAX_TENTATIVAS_CLAUDE`). Para voltar ao comportamento antigo (subir na leitura), `lanca_na_hora` devolvendo `True`.
- **Medir (uma semana depois):** em `/metricas`, cartões por dia e lotes `fecho-*`; no SQLite, `substituicao_motivo`
  (atualizada/cancelada/consolidada) e restaurações. Linha de base de 09/11: 19 de 54 cartões eram repetição (≈ 35 %).

## 24. A IA decide: nada a confirmar, filtro de ruído e histórico (pedido 46, D-58)
- **O que mudou:** você não confirma mais nada. A IA classifica, lança no Frentes e corrige o que ela mesma lançou. A
  tela Demandas é para **revisar** o que ela fez, não para liberar.
- **O que sobe:** só o que deixa uma ação para você depois que a conversa acabou (pedido não atendido, compromisso com
  data, decisão que esperam de você, risco). Conversa em andamento, pergunta que você já respondeu, assunto resolvido ali
  mesmo e novidade/opinião/link ficam quietos. Atenção e prazo até amanhã sobem na leitura; o resto, no fechamento.
- **O que você vê em Demandas:**
  - cartão que subiu: "No Frentes" e o botão **Recusar** (cancela o item; o aviso do rodapé tem **Desfazer**);
  - cartão **provisório**: sobe sozinho às 21:00; **Subir agora** adianta; **Recusar** impede;
  - cartão **segurado**: a IA não deixou subir no fechamento. A linha de baixo do título diz o motivo ("A IA segurou no
    fechamento do dia (era demanda): …"). **Subir assim mesmo** devolve a classe e sobe na hora. Os segurados como "sem
    ação" aparecem com o filtro de classe **sem ação**;
  - "A IA lançou com confiança baixa" é só informação: o item está no Frentes como os outros.
- **Correção de item já lançado:** quando a conversa muda o assunto de um cartão que já subiu (data, hora, detalhe), o
  item do Frentes ganha a versão nova. Se você editou o texto lá, as suas palavras ficam e a correção entra no fim,
  depois de " · atualizado: ". Evento cancelado cancela o item; **Restaurar** (filtro Substituídos) reabre.
- **Histórico de análise:** cada leitura grava uma linha por conversa (o que houve e em que pé ficou). Para ler:
  ```bash
  cd ~/vigia-whatsapp && bin/vigia.sh historico                 # últimos 7 dias: quem aguarda você, quem você aguarda, dia a dia
  ```
  ```bash
  cd ~/vigia-whatsapp && bin/vigia.sh historico --pendentes     # só quem ficou sem resposta
  ```
  ```bash
  cd ~/vigia-whatsapp && bin/vigia.sh historico --dias 30 --conversa Ana
  ```
  `--json` devolve a mesma leitura em uma linha; `--contagem` só números (nenhum nome, nenhum resumo). O histórico começa
  em 10/11/2026: não há linha para leituras anteriores. Ele fica guardado como as classificações (a limpeza semanal apaga
  só a mensagem crua).
- **Histórico no painel:** a página **Histórico** (último item da lateral; ⌘6 no app) mostra a mesma leitura. À esquerda,
  as conversas do período separadas em Aguardando você, Aguardando o contato e Encerradas (vale a última análise de
  cada uma); à direita, a linha do tempo por dia. O pull-down troca o período (24 horas, 7 dias, 30 dias) e a busca
  filtra o que está na tela. Clicar numa conversa mostra só ela, com **Abrir conversa** e os cartões dela no rodapé.
  A pílula âmbar **aguarda você** aparece só na pendência atual; "aguardava você" em cinza é pendência já superada.
- **Ler no log:** `grep -E "fechamento" ~/Library/Logs/vigia-whatsapp.log` — `rebaixados=N` é quantos cartões a IA segurou
  naquele dia; `absorvidos=N`, quantos fundiu.
- **Se a IA segurar demais ou de menos:** a régua está em `prompts/vigia.md` ("O que sobe e o que fica quieto") e em
  `prompts/consolidar.md` (seção 1). A regra de desempate é "dúvida = sobe". Para desligar só o filtro do fechamento,
  tire a seção 1 de `prompts/consolidar.md` (o passe continua fundindo repetidos).
- **Medir (17/11):** recusados depois de lançados, segurados por dia e quantos você mandou subir assim mesmo, absorvidos,
  restaurados, análises por dia (`bin/vigia.sh historico --contagem`).

## 25. Janela dividida: arrastar as colunas e recolher a lateral (pedido 48, D-59)
- **Arrastar:** passe o mouse na linha entre duas colunas (lateral | lista | detalhe); o cursor vira o de redimensionar.
  Arraste para alargar ou estreitar. **Duplo clique** na linha volta ao tamanho padrão.
- **Recolher a lateral:** o botão ao lado de "Greg", ⌃⌘S, menu Visualizar → "Mostrar ou ocultar a lateral", ou arrastar a
  lateral até quase fechar. Para mostrar de novo: o botão no canto de cima do primeiro painel, ou ⌃⌘S. Recolher
  arrastando não perde a largura: ao mostrar, ela volta como estava antes do arrasto.
- **Teclado:** Tab até a linha (ela engrossa na cor de destaque); ← → movem 16 px (com Shift, 48); Home e End vão ao
  mínimo e ao máximo; Enter na linha da lateral recolhe.
- **O que fica lembrado:** largura da lateral (168 a 360 px, a mesma em todas as páginas), lateral recolhida ou não, e a
  largura da lista **de cada página** (240 a 900 px; o detalhe fica com pelo menos 400 px). Sem largura escolhida a
  lateral é automática: 212 px ou o que o rodapé dela pedir para mostrar "Conectado, lido às 19:40" inteiro.
- **Onde fica:** `dados/layout.json` (0600), gravado por `POST /layout`. No servidor, e não no navegador, porque o
  Greg.app não guarda nada do navegador entre uma abertura e outra. Vale no app e no navegador ao mesmo tempo.
- **Voltar tudo ao padrão:** duplo clique em cada divisória, ou apagar `dados/layout.json` e recarregar (⌘R).
- **No Greg.app:** a faixa de arrastar a janela, no topo da lateral, acompanha a largura dela. Isso e os itens novos do
  menu Visualizar (Histórico ⌘6, lateral ⌃⌘S) valem depois de `bin/construir-app.sh` e de fechar e abrir o app. App
  antigo ainda aberto continua funcionando: arrasta as colunas, mas não recolhe a lateral nem a deixa menor que 212 px
  (a alça antiga cobriria o botão).
- **Não aparece a divisória da lista:** página de um painel só (Conexão, Envios, Contatos, Ajustes) tem só a da lateral; com a
  lateral recolhida, a dela some junto.
- **A largura não foi lembrada:** `ls -l dados/layout.json` tem de existir depois do primeiro arrasto (0600). A gravação
  exige sessão aberta e a origem do próprio painel; valor fora da faixa é ignorado, não gravado.
- **Prova depois de mexer:** `testes/test_layout.py` na suíte (`-t .`). No painel: arrastar a linha lista | detalhe em
  Demandas, recarregar (a largura fica), abrir Conversas (largura própria), duplo clique (volta), ⌃⌘S duas vezes.
- **Capturas e testes** nunca escrevem no `dados/layout.json` vivo: `docs/imagens/gerar.py` aponta o arquivo para a pasta
  temporária, como faz com as preferências (a captura `demandas-dividida` usa isso).

## 26. Fila recriada: leitura parada com motivo desconhecido (estudo de robustez de 16/11, achado B2)

- **Sintoma:** depois de 75 minutos sem leitura, a Central mostra o aviso de leitura parada com a causa "erro que o Greg
  não reconhece". O aviso se repete a cada ciclo. Em `bin/vigia.sh logs` aparece `FilaRecriada` com a frase
  `fila recriada: last_value=N < ultimo_seq=M; confira o volume do Postgres antes de mexer no cursor`
  (`app/lotes.py:232-239`).
- **Causa:** a sequência da fila (`vigia.fila_seq_seq`, no Postgres da Evolution) recomeçou abaixo do cursor do Greg
  (`ultimo_seq` em `dados/cursor.json`). Há dois casos:
  - o volume `evolution_pg` foi recriado, por `docker compose down -v`, por reinstalação do Docker ou do Colima, ou por
    um volume apagado à mão;
  - a pilha subiu com outro nome de projeto e, por isso, com outro volume. No Mac o projeto é `vigia-whatsapp`
    (`infra/docker-compose.yml:5`); no Docker da edição pública, `vigia` (`docker-compose.yml:11`).
- **Saída:** confira o volume ANTES de mexer no cursor. Baixar `ultimo_seq` com o volume errado faria o Greg reler a
  fila do volume certo quando ele voltar.

```bash
touch dados/PAUSADO                                     # 1. pausa as leituras (seção 4)
docker volume ls --format '{{.Name}}' | grep evolution_pg
docker volume inspect <nome-do-volume> --format '{{.CreatedAt}}'   # 2. criado agora = recriado
bin/evolution.sh sql 'SELECT last_value FROM vigia.fila_seq_seq'
bin/evolution.sh sql 'SELECT count(*) FROM public."Message"'
```

  - **Volume antigo, com mensagens e com `last_value` acima do cursor:** a pilha está no volume errado. Suba-a com o
    nome de projeto certo e não mexa no cursor. O ciclo seguinte lê normalmente.
  - **Volume recriado de fato:** as mensagens antigas não existem mais na Evolution e não há o que reler. Com a pausa
    ainda ligada, guarde uma cópia do cursor e volte `ultimo_seq` a 0. `pareado_em` e `janela_desde` ficam como estão,
    e a janela de leitura continua a mesma.

```bash
cp -p dados/cursor.json "dados/cursor.json.antes-fila-recriada-$(date +%s)"
python3 -c 'import json; p="dados/cursor.json"; d=json.load(open(p)); d["ultimo_seq"]=0; open(p,"w").write(json.dumps(d))'
rm dados/PAUSADO                                        # 3. volta no próximo disparo; ou "Ler agora" em /conexao
```

- **A pausa vem antes da edição** porque o Greg grava o cursor com o maior valor entre o disco e a memória
  (`app/cursor.py:71-78`). Um ciclo rodando durante a edição devolveria o número antigo.
- **Prova:** a leitura seguinte termina sem `FilaRecriada` no log e o aviso de leitura parada se fecha sozinho.
  `cat dados/cursor.json` mostra `ultimo_seq` subindo de novo.
- **Ainda não está no catálogo** `docs/PROBLEMAS.md`, que é gerado de `app/problemas.py`. O código `fila-recriada`, com
  causa e passos na Central, está na fila de depois do lançamento (estudo `docs/relatorios/robustez-evolution-16nov2026.md`,
  item B2).
