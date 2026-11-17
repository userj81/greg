# Remendos na imagem da Evolution

Arquivos montados por cima dos da imagem `evoapicloud/evolution-api:v2.3.7` (ver `volumes:` do serviço `api` em
`../docker-compose.yml`). Cada um existe porque a imagem traz um defeito que a gente não pode esperar ser corrigido
upstream. **Ao trocar a tag da imagem, revisar cada remendo**: se a imagem nova já traz a correção, a montagem sai
(senão ela sobrescreve o arquivo novo com o antigo).

## `baileys-messages-recv.js` (D-55, pedido 44, 09/11/2026)

- **Original:** `/evolution/node_modules/baileys/lib/Socket/messages-recv.js` da imagem v2.3.7 (Baileys
  `7.0.0-rc.9`), md5 `0ad2438af7c908c0e5a4f789f8143f10`.
- **Defeito:** o WhatsApp passou a entregar o status dos contatos como stanza de topo `<status>`, e não mais como
  `<message from="status@broadcast">`. O rc.9 registra ouvinte só para `message`, `call`, `receipt`, `notification`
  e `ack`; a stanza `<status>` nunca recebe `ack`, e cerca de 50 min depois o servidor fecha o stream
  (`stream errored out`, `reasonNode` com `ack class=status`). A Evolution reconecta, o servidor reentrega o **mesmo
  lote pendente** (mensagens antigas) e segura as novas: instância `open`, sonda respondendo, Postgres parado.
- **Correção (3 trechos, marcados `Greg (D-55)`):** `handleStatus` que só manda o `ack`; `status` no mapa da fila
  offline; ouvinte `ws.on('CB:status', …)` passando pelo `processNode`. Backport de
  [evolution-foundation/evolution-api#2785](https://github.com/evolution-foundation/evolution-api/pull/2785)
  (que porta [WhiskeySockets/Baileys#2883](https://github.com/WhiskeySockets/Baileys/pull/2883)); relato em
  [evolution-api#2778](https://github.com/evolution-foundation/evolution-api/issues/2778). Nada do conteúdo do status
  é lido (a Evolution roda com `readStatus` desligado).
- **Como provar que está valendo:** `docker exec vigia-whatsapp-api-1 md5sum
  /evolution/node_modules/baileys/lib/Socket/messages-recv.js` tem que ser o md5 deste arquivo, e
  `docker logs vigia-whatsapp-api-1 --since 1h 2>&1 | grep -c 'class":"status'` tem que ficar em 0 depois do
  primeiro ciclo de 50 min.
- **Contrato:** `testes/test_contrato_infra.py::test_remendo_do_baileys_montado_e_integro` (sintaxe pelo `node --check`,
  os três trechos presentes, montagem `:ro` no caminho certo, e amarrado à tag `v2.3.7`).

## `baileys-chats.js` (D-65, pedido 54, 12/11/2026)

- **Original:** `/evolution/node_modules/baileys/lib/Socket/chats.js` da imagem v2.3.7 (Baileys `7.0.0-rc.9`), md5
  `78f668ff34e73ae8b7539a98e1f222d6`.
- **Por quê:** a agenda (nomes que o dono salvou) só chega no pareamento; a Evolution não tem rota para pedi-la de
  novo. O remendo acrescenta `gregPuxarAgenda`: quando existe o arquivo-sinal `/evolution/instances/greg-puxar-agenda`
  (criado por `bin/evolution.sh puxar-agenda`), ao conectar ele apaga o sinal, zera a versão guardada da coleção
  `critical_unblock_low` e chama `resyncAppState`. Sem o arquivo-sinal o comportamento é o da imagem.
- **Diferença para o original:** só o bloco marcado `GREG (pedido 54, D-65)` e a chamada dele em `connection === 'open'`.
- **Onde o nome vai parar:** a Evolution ignora o nome de contato que já existe; quem guarda é o gatilho BEFORE INSERT
  de `infra/sql/006_agenda.sql`, em `vigia.agenda`.

