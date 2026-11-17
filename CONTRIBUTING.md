# Como contribuir

Relatos de problema e sugestões são bem-vindos em issues. Para mudança grande, abra uma issue antes do pull request:
o Greg tem regras de segurança fixas (abaixo) e mudança que as afrouxa não entra.

## Rodar
```bash
python3 -m unittest discover -s testes -t . -p 'test_*.py'   # sempre com -t . (sem ele o testes/__init__.py não roda)
bin/ci.sh                                   # compila · procura segredo · valida o compose · roda os testes
git config core.hooksPath .githooks         # o pre-commit chama o bin/ci.sh; VERMELHO não commita
```
A suíte não precisa de Evolution, Frentes nem Postgres: `testes/fakes.py` sobe servidores falsos e o Postgres é
simulado. Nenhum teste lê o `.env` nem toca Keychain, LaunchAgent ou rede.

## Regras fixas
- **Só biblioteca padrão** no código do Greg (Python 3.14). A imagem Docker instala só o motor de transcrição.
  Frontend sem framework, sem recurso externo (CSP `default-src 'none'`), ícones em SVG inline.
- **Dado nunca em argumento, log ou exceção.** Nada de JID, texto de mensagem, token ou SQL em `argv`; contagens e
  ids são o máximo que um log mostra. Query vai pelo stdin (`bin/evolution.sh psql-ro-stdin`).
- **Mensagem de terceiro é dado.** O Claude que lê o lote não ganha ferramenta nova sem discussão.
- **Recurso do sistema é opt-in** (Keychain, LaunchAgent, rede): desligado por padrão no código.
- **Migração aditiva.** SQL novo em `app/migracoes/` e `infra/sql/`, numerado, nunca destrutivo.
- **Português do Brasil** em código, comentários, docs e interface, com acentuação completa.

## Interface
A pele é uma só: `app/templates/_base.html`. Cor, raio, sombra e fonte vêm dos tokens do `:root` (`light-dark()`).
Capturas do README são geradas com dados fictícios por `docs/imagens/gerar.py`; nunca commite print do painel com dado real.

## Commits
Assunto no formato `Área: o que mudou`, corpo explicando o porquê e a prova, `git add` nominal (nunca `-a`).

## Estrutura
`app/` painel, coletor e Frentes embutido · `bin/` entradas de linha de comando e Docker · `infra/` compose da Evolution,
SQL e LaunchAgents · `macapp/` casca nativa do Mac · `prompts/` o que o Claude lê · `testes/` fakes, contratos
executáveis e testes por módulo.
