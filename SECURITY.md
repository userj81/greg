# Segurança

O Greg roda na máquina de quem o usa e lida com o conteúdo de um WhatsApp. O modelo de ameaça é local: nada escutar
fora do computador, o cru ser apagado depois do prazo que você escolhe e nenhuma mensagem de terceiro virar ordem.

## O que sai do computador
O Greg não tem servidor recebendo nada e não tem telemetria. O que sai é a conversa com o WhatsApp e o lote que a sua
IA lê:

1. **WhatsApp.** O tráfego da sua conta, pela Evolution API (telemetria dela desligada).
2. **O lote de cada leitura.** As mensagens novas, o contexto recente da conversa, a transcrição dos áudios e as
   imagens novas (até o teto por lote) vão à IA que você escolheu. Junto vai o contexto do ciclo: as suas frentes, o
   resumo das seções, os itens dos últimos 14 dias e as últimas classificações da conversa. Por padrão é a Anthropic,
   pelo Claude Code da sua conta. Se você escolher outro provedor em Ajustes › IA (OpenRouter ou outro compatível), o
   lote vai a ele, com a sua chave. Com um modelo local, o lote não sai.
3. **Foto de perfil.** O painel baixa a foto dos contatos direto do endereço do WhatsApp (`https` em `whatsapp.net`).
4. **Só se você configurar.** O aviso opcional (contadores e um link, nunca texto de mensagem) vai à Evolution de um
   segundo número seu; e as demandas vão ao seu sistema de tarefas, se você apontar um no lugar do Frentes embutido.
5. **Downloads da instalação.** Imagens Docker, o Docker Desktop quando o instalador o baixa e o modelo de transcrição.
   Depois de instalada, a transcrição roda offline.

## O que o projeto garante
- **Rede.** Painel e Evolution API publicados só em `127.0.0.1`. Postgres e Redis não publicam porta: ficam na rede
  interna do Docker.
- **Dados.** Só o último dia antes do pareamento e o que chega depois é lido. A IA não lê grupo por padrão: só o que
  você liberar em `[grupos] permitidos` do `config.toml`. As mensagens de grupo ficam guardadas e aparecem em
  Conversas (`[conversas] grupos`), e saem na mesma limpeza. O cru (mensagens, mídia, áudio, transcrição) fica
  guardado pelo prazo que você escolhe em Ajustes (7, 14, 30 ou 90 dias, ou não apagar; o padrão é 7) e é apagado na
  limpeza semanal seguinte: o prazo é o mínimo. Fica fora da limpeza o arquivo anexado a uma seção, por você ou pela
  IA de leitura quando ela entende que o arquivo pertence à seção: esse sai só quando você apaga. O banco local guarda
  os itens, as classificações, os resumos, as decisões, os apelidos de contatos, as seções e as fichas dos anexos.
- **Arquivos.** O Greg não cifra o que guarda. Os arquivos ficam com permissão só para o seu usuário; a proteção do
  disco é a do sistema (no Mac, o FileVault).
- **Segredos.** Tokens em arquivos `0600` (`.env`, `docker.env`, `~/.vigia-envio.env`); nunca em argumento de linha
  de comando, log, exceção ou navegador. Guardar nas Chaves do macOS é opcional e vem desligado. O `bin/ci.sh` e o
  hook de pre-commit barram segredo em commit (gitleaks).
- **Entrada hostil.** Mensagem de terceiro é dado. O Claude Code que lê o lote roda em modo restrito (`--restricted`:
  ignora os arquivos de configuração do usuário e do projeto), sem servidores MCP (`--strict-mcp-config`), sem guardar
  sessão, com `Read` só no lote e um único executável (`bin/vigia-ponte`); não recebe o token de envio. Outro
  provedor recebe o lote já montado, sem terminal, arquivo nem rede, e só consegue pedir as funções declaradas para a
  tarefa; pedido fora da lista é recusado.
- **Envio.** Só a pedido explícito, com liberação interativa (Touch ID no aplicativo do Mac; fora dele, o token de
  envio digitado) e tetos por dia; a API local recusa disparo em massa.
- **Conector do Claude Code (`/mcp`).** Desligado de fábrica. Só existe depois que você gera o código em Ajustes; o
  código abre somente `/mcp` e fica em disco só como SHA-256. Cada área (enviar, ler, demandas, contatos) tem chave
  própria, desligada até você ligar. Enviar ainda exige a liberação de envio aberta. Com leitura e envio ligados
  juntos, mensagem para número que nunca conversou com você e "apagar para todos" esperam o seu clique no aplicativo.
  O registro guarda nome da ação e hora, nunca texto. Risco que fica com quem liga: o Claude que lê conversas recebe
  texto de terceiros; o servidor avisa que é dado e não ordem, mas a defesa que não depende do modelo é a chave
  desligada e o aval no aplicativo.
- **Painel.** Login por token; a sessão vale 30 dias.
- **QR.** Só por clique, 60 s, no máximo 3 por clique e 10 por dia; toda tentativa termina em conexão ou em logout.
- **Instalador.** Arquivo baixado pelo aplicativo recebe a marca de quarentena do macOS. O instalador confere a
  assinatura do Docker Desktop antes de copiá-lo. O `Greg.dmg` sai com assinatura ad-hoc, sem notarização da Apple:
  confira o SHA-256 publicado junto da versão (`shasum -a 256 Greg.dmg`) antes de abrir.

## Fora do escopo
- Os Termos do WhatsApp e o comportamento da Evolution API (projeto independente).
- O que o provedor de IA que você escolheu faz com o lote: vale a política dele.
- Proteção contra quem já tem acesso à sua conta de usuário no computador ou ao seu celular.
- Computador com mais de uma conta de usuário: o Greg foi feito para um computador de uso pessoal, e as outras contas
  da mesma máquina ficam fora do que o projeto garante.
- Uso com número que não seja o seu.

## Como reportar
Use **Security → Report a vulnerability** (aviso privado do GitHub) neste repositório. Não cole mensagem, número,
token ou captura com dado real: descreva a reprodução com dados fictícios (a suíte de testes mostra como).

## Versões cobertas
Só a `main` e a última versão publicada.
