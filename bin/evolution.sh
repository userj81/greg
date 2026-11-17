#!/bin/bash
# Controla a Evolution do Greg (compose em infra/). Quem chama: Dono, bin/vigia.sh, testes.
# O que não faz: não imprime segredo, não passa senha na linha de comando (só por ambiente/stdin).
# Saída: 0 ok · 1 falhou · 2 não rodou (uso errado, docker ausente, .env.compose ausente/placeholder).
set -u

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_YML="$RAIZ/infra/docker-compose.yml"
ENV_FILE="${VIGIA_ENV_COMPOSE:-$RAIZ/infra/.env.compose}"
APP_ENV="${VIGIA_ENV_APP:-$RAIZ/.env}"
CONFIG_TOML="${VIGIA_CONFIG:-$RAIZ/config.toml}"
USAR_DC_HIFEN=0
SQL_DIR="$RAIZ/infra/sql"
# Modo contêiner (docker-compose.yml da raiz, serviço vigia): não há docker nem .env.compose aqui dentro. Os segredos
# chegam pelo ambiente do contêiner, o psql fala direto com o serviço db e o reinício é pela API da Evolution.
EM_CONTAINER=0
# Python: VIGIA_PYTHON > o do Homebrew (Mac) > python3 do PATH (Linux, CI, contêiner).
PY="${VIGIA_PYTHON:-/opt/homebrew/bin/python3}"
[ -x "$PY" ] || PY="$(command -v python3 || echo python3)"
[ "${VIGIA_CONTAINER:-0}" = 1 ] && EM_CONTAINER=1
PG_HOST="${VIGIA_PG_HOST:-db}"
DRY=0

uso() {
  cat <<'AJUDA'
Uso: bin/evolution.sh [--dry-run] <comando> [argumento]

Comandos:
  up               sobe api/db/redis, espera o Postgres e a tabela "Message", aplica infra/sql/*.sql
  down             derruba os serviços (volumes ficam)
  status           serviços db e api em running + pg_isready + GET na API (200); senão exit 1
  schema           imprime \d public."Message" e \d public."Contact" (prova de colunas, F1)
  sql "<query>"    roda a query como dono do banco evolution (escrita; use com cuidado)
  sql-stdin        lê a query do stdin (psql -f -) como dono evolution; nada de SQL em argv/ps (usado por
                   fechar-semana.py --restaurar). Mesma saída de `sql`
  psql-ro "<query>" roda a query como vigia_ro (somente leitura), saída -At separada por TAB
  psql-ro-stdin    igual a psql-ro, com a query no stdin (psql -f -): é o que o painel, o coletor e o
                   fechar-semana usam, porque a query carrega jid de conversa e argv aparece no ps
  psql-limpeza-stdin  igual a psql-limpeza, com a query no stdin
  restart          docker compose restart api (caso 428 / conexão presa)
  puxar-agenda     pede ao WhatsApp a agenda inteira de novo, sem QR (pedido 54): cria o arquivo-sinal e reconecta a
                   instância pela API; os nomes salvos entram em vigia.agenda. Só fora do contêiner
  aplicar-sql      reaplica infra/sql/*.sql (idempotente) sem mexer nos containers
  bloquear <jid>   D-41: conversa sem leitura nenhuma (apaga o conteúdo do que já está guardado e do que chegar)
  desbloquear <jid> volta a guardar o que chegar depois (o conteúdo apagado não volta)
  psql-limpeza "<query>"  como vigia_limpeza (DELETE na janela; só o fechar-semana usa)
  instancia        cria a instância vigia (qrcode:false) e aplica settings; grava a apikey
                   da instância em .env (EVOLUTION_INSTANCE_APIKEY, 0600) sem imprimi-la;
                   groupsIgnore=false se config.toml [grupos].permitidos tem grupo ou [conversas].grupos = true

Opções:
  --dry-run        mostra o que faria (sem segredo) e não executa
  -h, --help       esta ajuda

Lê infra/.env.compose (precisa ser 0600; grupo/outros com acesso = recusa). Códigos de saída: 0 ok, 1 falhou, 2 não rodou.
AJUDA
}

falha() { echo "evolution.sh: $*" >&2; }

# Lê uma chave do .env.compose sem executar o arquivo e sem ecoar o valor.
valor() {
  if [ "$EM_CONTAINER" = 1 ]; then printenv "$1" || true; return 0; fi
  grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -n1 | cut -d= -f2-
}

exigir_env() {
  if [ "$EM_CONTAINER" = 1 ]; then
    local k
    for k in EVOLUTION_GLOBAL_KEY POSTGRES_PASSWORD VIGIA_RO_PASSWORD VIGIA_LIMPEZA_PASSWORD; do
      [ -n "$(valor "$k")" ] || { falha "$k ausente no ambiente do contêiner"; exit 2; }
    done
    return 0
  fi
  if [ ! -f "$ENV_FILE" ]; then
    falha "falta $ENV_FILE (copie infra/.env.compose.exemplo e preencha)"; exit 2
  fi
  local chave v modo
  modo="$(stat -c %a "$ENV_FILE" 2>/dev/null || stat -f %Lp "$ENV_FILE" 2>/dev/null)"   # GNU primeiro (no Linux "stat -f" é outra coisa)
  case "$modo" in
    ""|*[!0-7]*) falha "não consegui ler a permissão de $ENV_FILE"; exit 2 ;;
  esac
  if [ $(( 8#$modo & 8#077 )) -ne 0 ]; then
    falha "$ENV_FILE com permissão $modo (grupo/outros); rode: chmod 600 $ENV_FILE"; exit 2
  fi
  for chave in EVOLUTION_GLOBAL_KEY POSTGRES_PASSWORD VIGIA_RO_PASSWORD VIGIA_LIMPEZA_PASSWORD; do
    v="$(valor "$chave")"
    if [ -z "$v" ] || [ "${v#TROQUE}" != "$v" ]; then
      falha "$chave vazio ou placeholder em $ENV_FILE"; exit 2
    fi
    if ! [[ "$v" =~ ^[A-Za-z0-9_-]+$ ]]; then
      falha "$chave com caractere fora de [A-Za-z0-9_-] (use openssl rand -hex 24)"; exit 2
    fi
  done
}

dc() {
  if [ "$USAR_DC_HIFEN" = 1 ]; then
    docker-compose -f "$COMPOSE_YML" --env-file "$ENV_FILE" "$@"
  else
    docker compose -f "$COMPOSE_YML" --env-file "$ENV_FILE" "$@"
  fi
}

exigir_docker() {
  [ "$EM_CONTAINER" = 1 ] && return 0
  if ! command -v docker >/dev/null 2>&1; then
    falha "docker não encontrado (colima parado ou não instalado?)"; exit 2
  fi
  if docker compose version >/dev/null 2>&1; then
    USAR_DC_HIFEN=0
  elif command -v docker-compose >/dev/null 2>&1; then
    USAR_DC_HIFEN=1
  else
    falha "compose ausente: brew install docker-compose e, em ~/.docker/config.json, \"cliPluginsExtraDirs\": [\"/opt/homebrew/lib/docker/cli-plugins\"]"; exit 2
  fi
  if ! docker info >/dev/null 2>&1; then
    falha "daemon parado (colima start)"; exit 2
  fi
}

# psql dentro do container db. $1 = usuário, $2 = chave do .env com a senha; resto = args do psql.
# A senha vai por PGPASSWORD herdado do ambiente (nunca aparece no ps).
psql_como() {
  local usuario="$1" chave="$2"; shift 2
  if [ "$EM_CONTAINER" = 1 ]; then
    PGPASSWORD="$(valor "$chave")" psql -h "$PG_HOST" -U "$usuario" -d evolution -v ON_ERROR_STOP=1 "$@"
    return
  fi
  PGPASSWORD="$(valor "$chave")" dc exec -T -e PGPASSWORD db \
    psql -h 127.0.0.1 -U "$usuario" -d evolution -v ON_ERROR_STOP=1 "$@"
}


aplicar_sql() {
  # 001 sem variáveis; 002 recebe as senhas por \set na entrada padrão.
  local f ro lim
  ro="$(valor VIGIA_RO_PASSWORD)"; lim="$(valor VIGIA_LIMPEZA_PASSWORD)"
  for f in "$SQL_DIR"/*.sql; do
    {
      printf "\\\\set ro_pass '%s'\n\\\\set limpeza_pass '%s'\n" "$ro" "$lim"
      cat "$f"
    } | psql_como evolution POSTGRES_PASSWORD -q >/dev/null || { falha "falhou aplicar $(basename "$f")"; return 1; }
  done
}

esperar_tabela() {
  local i out
  for i in $(seq 1 60); do
    out="$(psql_como evolution POSTGRES_PASSWORD -At -c "SELECT to_regclass('public.\"Message\"') IS NOT NULL" 2>/dev/null)"
    [ "$out" = "t" ] && return 0
    sleep 3
  done
  return 1
}

cmd_up() {
  if [ "$EM_CONTAINER" = 0 ]; then
    dc up -d || { falha "compose up falhou"; return 1; }
  fi
  esperar_tabela || { falha "tabela \"Message\" não apareceu em 180 s (veja: docker compose logs api)"; return 1; }
  aplicar_sql || return 1
  echo "ok: serviços no ar, SQL aplicado"
}

api_200() {
  local url cod
  url="${EVOLUTION_URL:-http://127.0.0.1:8080}/"
  cod="$(printf 'url = "%s"\nsilent\noutput = "/dev/null"\nmax-time = 10\nwrite-out = "%%{http_code}"\n' "$url" | curl --config - 2>/dev/null | tail -n1)"
  [ "$cod" = "200" ] || { falha "API em $url devolveu HTTP ${cod:-sem resposta} (esperado 200)"; return 1; }
}

cmd_status() {
  local rodando url cod
  if [ "$EM_CONTAINER" = 1 ]; then
    psql_como evolution POSTGRES_PASSWORD -At -c 'SELECT 1' >/dev/null || { falha "Postgres não respondeu"; return 1; }
    api_200 || return 1
    echo "ok: Postgres pronto, API 200"; return 0
  fi
  dc ps || return 1
  rodando="$(dc ps --status running --services)" || { falha "não consegui listar serviços em execução"; return 1; }
  case "$(printf '\n%s\n' "$rodando")" in *$'\ndb\n'*) ;; *) falha "serviço db não está em running"; return 1 ;; esac
  case "$(printf '\n%s\n' "$rodando")" in *$'\napi\n'*) ;; *) falha "serviço api não está em running"; return 1 ;; esac
  dc exec -T db pg_isready -U evolution >/dev/null || { falha "pg_isready falhou no db"; return 1; }
  url="${EVOLUTION_URL:-http://127.0.0.1:8080}/"
  cod="$(printf 'url = "%s"\nsilent\noutput = "/dev/null"\nmax-time = 10\nwrite-out = "%%{http_code}"\n' "$url" | curl --config - 2>/dev/null | tail -n1)"
  [ "$cod" = "200" ] || { falha "API em $url devolveu HTTP ${cod:-sem resposta} (esperado 200)"; return 1; }
  echo "ok: db e api em running, Postgres pronto, API 200"
}

cmd_schema() {
  psql_como evolution POSTGRES_PASSWORD -c '\d public."Message"' -c '\d public."Contact"'
}

cmd_sql() {
  [ $# -ge 1 ] || { falha "sql exige a query entre aspas"; exit 2; }
  psql_como evolution POSTGRES_PASSWORD -At -F $'\t' -c "$1"
}

cmd_sql_stdin() {
  psql_como evolution POSTGRES_PASSWORD -At -F $'\t' -f -
}

cmd_psql_ro() {
  [ $# -ge 1 ] || { falha "psql-ro exige a query entre aspas"; exit 2; }
  psql_como vigia_ro VIGIA_RO_PASSWORD -At -F $'\t' -c "$1"
}

# Variantes com a query no stdin: nada de SQL (jid, número, datas) em argv, logo nada no ps. Mesma saída -At/TAB;
# ON_ERROR_STOP=1 (psql_como) faz o psql sair 3 em erro, então o chamador continua vendo código != 0.
cmd_psql_ro_stdin() {
  psql_como vigia_ro VIGIA_RO_PASSWORD -At -F $'\t' -f -
}

cmd_psql_limpeza_stdin() {
  psql_como vigia_limpeza VIGIA_LIMPEZA_PASSWORD -At -F $'\t' -f -
}

# D-41. O jid passa por regex estrita aqui E no CHECK da tabela; vai ao psql por \\set na entrada padrão.
JID_RE='^[0-9A-Za-z._:-]{1,80}@(s\.whatsapp\.net|g\.us|lid)$'
cmd_bloqueio() {
  local funcao="$1" jid="${2:-}"
  printf '%s' "$jid" | grep -Eq "$JID_RE" || { falha "jid inválido"; exit 2; }
  printf "\\set alvo '%s'\nSELECT vigia.%s(:'alvo');\n" "$jid" "$funcao" \
    | psql_como evolution POSTGRES_PASSWORD -At -f -
}

cmd_psql_limpeza() {
  [ $# -ge 1 ] || { falha "psql-limpeza exige a query entre aspas"; exit 2; }
  psql_como vigia_limpeza VIGIA_LIMPEZA_PASSWORD -At -F $'\t' -c "$1"
}

# POST na Evolution com a chave GLOBAL. Cabeçalho e corpo vão por `curl --config -` (stdin):
# nada de segredo em argv. Resposta: corpo na variável RESP_CORPO, status em RESP_COD.
evo_post() {
  local caminho="$1" corpo="$2" chave resp
  chave="$(valor EVOLUTION_GLOBAL_KEY)"
  corpo="${corpo//\\/\\\\}"; corpo="${corpo//\"/\\\"}"
  resp="$(printf 'url = "%s%s"\nheader = "apikey: %s"\nheader = "Content-Type: application/json"\ndata = "%s"\nsilent\nshow-error\nmax-time = 30\nwrite-out = "\\n%%{http_code}"\n' \
      "${EVOLUTION_URL:-http://127.0.0.1:8080}" "$caminho" "$chave" "$corpo" | curl --config -)" || return 1
  RESP_COD="${resp##*$'\n'}"
  RESP_CORPO="${resp%$'\n'*}"
  return 0
}

# 1 se config.toml [grupos].permitidos tem ao menos um grupo; 0 se vazia/ausente (D-10).
grupos_permitidos() {
  local n py="$PY"
  if [ -f "$CONFIG_TOML" ] && [ -x "$py" ]; then
    n="$("$py" -I -c 'import sys,tomllib
d=tomllib.load(open(sys.argv[1],"rb"))
print(len(d.get("grupos",{}).get("permitidos",[])))' "$CONFIG_TOML" 2>/dev/null)" || n=""
    case "$n" in ""|*[!0-9]*) ;; *) [ "$n" -gt 0 ] && echo 1 || echo 0; return 0 ;; esac
  fi
  # fallback tolerante: lista vazia na própria linha (ou ausente) = 0; qualquer outra coisa = 1
  if grep -Eq '^[[:space:]]*permitidos[[:space:]]*=[[:space:]]*\[[[:space:]]*(#.*)?\]' "$CONFIG_TOML" 2>/dev/null \
     || ! grep -Eq '^[[:space:]]*permitidos[[:space:]]*=' "$CONFIG_TOML" 2>/dev/null; then
    echo 0
  else
    echo 1
  fi
}

# 1 se config.toml [conversas].grupos = true (r02, D-39: grupos aparecem na tela de Conversas); 0 se false/ausente.
grupos_na_tela() {
  local n py="$PY"
  if [ -f "$CONFIG_TOML" ] && [ -x "$py" ]; then
    n="$("$py" -I -c 'import sys,tomllib
d=tomllib.load(open(sys.argv[1],"rb"))
print(1 if d.get("conversas",{}).get("grupos") is True else 0)' "$CONFIG_TOML" 2>/dev/null)" || n=""
    case "$n" in 0|1) echo "$n"; return 0 ;; esac
  fi
  echo 0
}

# Grava EVOLUTION_INSTANCE_APIKEY em $APP_ENV (0600), trocando a linha se existir. Keychain só com VIGIA_KEYCHAIN=on
# (opt-in, D-45; valor pelo stdin de app/segredos.py). O valor nunca vai em argv nem é impresso.
gravar_apikey() {
  local chave="$1" tmp
  if ! [[ "$chave" =~ ^[A-Za-z0-9_-]+$ ]]; then
    falha "apikey devolvida com formato inesperado; não gravei"; return 1
  fi
  if [ "${VIGIA_KEYCHAIN:-off}" = "on" ] && (cd "$RAIZ" && printf '%s\n' "$chave" \
       | "$PY" -m app.segredos gravar EVOLUTION_INSTANCE_APIKEY 2>/dev/null \
       | grep -qx gravado); then
    APIKEY_ONDE="Keychain"
    return 0
  fi
  APIKEY_ONDE=".env"
  (
    umask 077
    tmp="$(mktemp "${APP_ENV}.XXXXXX")" || exit 1
    if [ -f "$APP_ENV" ]; then
      NOVA_CHAVE="$chave" awk 'BEGIN{f=0}
        /^EVOLUTION_INSTANCE_APIKEY=/ { if(!f) print "EVOLUTION_INSTANCE_APIKEY=" ENVIRON["NOVA_CHAVE"]; f=1; next }
        { print }
        END { if(!f) print "EVOLUTION_INSTANCE_APIKEY=" ENVIRON["NOVA_CHAVE"] }' "$APP_ENV" > "$tmp" || { rm -f "$tmp"; exit 1; }
    else
      NOVA_CHAVE="$chave" awk 'BEGIN{print "EVOLUTION_INSTANCE_APIKEY=" ENVIRON["NOVA_CHAVE"]}' > "$tmp" || { rm -f "$tmp"; exit 1; }
    fi
    chmod 600 "$tmp" && mv -f "$tmp" "$APP_ENV"
  ) || { falha "não consegui gravar $APP_ENV"; return 1; }
}

cmd_instancia() {
  local inst="${EVOLUTION_INSTANCE:-$(grep -E '^EVOLUTION_INSTANCE=' "$APP_ENV" 2>/dev/null | tail -n1 | cut -d= -f2- | sed -e 's/ #.*//' -e "s/['\"[:space:]]//g")}" ajustes criar apikey="" ignorar
  inst="${inst:-vigia}"
  [[ "$inst" =~ ^[A-Za-z0-9._-]+$ ]] || { falha "EVOLUTION_INSTANCE com caractere fora de [A-Za-z0-9._-]"; return 1; }
  # A Evolution guarda mensagem de grupo se a IA lê algum grupo (D-10) OU se os grupos aparecem em Conversas (D-39).
  if [ "$(grupos_permitidos)" = 1 ] || [ "$(grupos_na_tela)" = 1 ]; then ignorar=false; else ignorar=true; fi
  ajustes='"syncFullHistory":false,"readMessages":false,"readStatus":false,"alwaysOnline":false,"rejectCall":false,"groupsIgnore":'"$ignorar"
  criar="{\"instanceName\":\"$inst\",\"integration\":\"WHATSAPP-BAILEYS\",\"qrcode\":false,$ajustes}"
  evo_post /instance/create "$criar" || { falha "curl falhou ao criar a instância"; return 1; }
  case "$RESP_COD" in
    2??)
      apikey="$(printf '%s' "$RESP_CORPO" | grep -Eo '"(hash|apikey)" *: *"[^"]+"' | head -n1 | sed 's/.*: *"\(.*\)"$/\1/')"
      echo "instância $inst criada (groupsIgnore=$ignorar)" ;;
    403|409) echo "instância $inst já existe; só reaplicando settings (groupsIgnore=$ignorar)" ;;
    *) falha "criar instância devolveu HTTP $RESP_COD"; return 1 ;;
  esac
  if [ -n "$apikey" ]; then
    gravar_apikey "$apikey" || return 1
    apikey=""
    echo "apikey gravada em ${APIKEY_ONDE:-.env}"
  fi
  evo_post "/settings/set/$inst" "{$ajustes}" || { falha "curl falhou nos settings"; return 1; }
  case "$RESP_COD" in 2??) echo "settings aplicados" ;; *) falha "settings devolveu HTTP $RESP_COD"; return 1 ;; esac
}

# Reinício pela API (modo contêiner): POST /instance/restart/<instância> com a chave da instância, que vem do .env do
# app (EVOLUTION_INSTANCE_APIKEY). Nada em argv: cabeçalho pelo stdin do curl.
cmd_restart_api() {
  local inst chave cod
  inst="$(grep -E '^EVOLUTION_INSTANCE=' "$APP_ENV" 2>/dev/null | tail -n1 | cut -d= -f2- | sed -e 's/ #.*//' -e "s/['\"[:space:]]//g")"
  chave="$(grep -E '^EVOLUTION_INSTANCE_APIKEY=' "$APP_ENV" 2>/dev/null | tail -n1 | cut -d= -f2- | sed -e 's/ #.*//' -e "s/['\"[:space:]]//g")"
  [ -n "$inst" ] && [ -n "$chave" ] || { falha "EVOLUTION_INSTANCE ou a apikey ausente em $APP_ENV"; return 1; }
  cod="$(printf 'url = "%s/instance/restart/%s"\nrequest = "POST"\nheader = "apikey: %s"\nsilent\noutput = "/dev/null"\nmax-time = 60\nwrite-out = "%%{http_code}"\n' \
      "${EVOLUTION_URL:-http://127.0.0.1:8080}" "$inst" "$chave" | curl --config - 2>/dev/null | tail -n1)"
  chave=""
  case "$cod" in 2??) echo "instância $inst reiniciada pela API" ;; *) falha "restart pela API devolveu HTTP ${cod:-sem resposta}"; return 1 ;; esac
}

# --- argumentos ---
if [ "${1:-}" = "--dry-run" ]; then DRY=1; shift; fi
CMD="${1:-}"
case "$CMD" in
  ""|-h|--help|help) uso; [ -z "$CMD" ] && exit 2; exit 0 ;;
  up|down|status|schema|sql|sql-stdin|psql-ro|psql-ro-stdin|psql-limpeza|psql-limpeza-stdin|restart|puxar-agenda|instancia|aplicar-sql|bloquear|desbloquear) shift ;;
  *) falha "comando desconhecido: $CMD"; uso >&2; exit 2 ;;
esac

if [ "$DRY" = 1 ]; then
  echo "dry-run: $CMD ${*:+(com argumento)} usando $COMPOSE_YML e $ENV_FILE; nada executado"
  exit 0
fi

[ "$CMD" = instancia ] || exigir_docker
exigir_env

case "$CMD" in
  up)      cmd_up || exit 1 ;;
  down)    dc down || exit 1 ;;
  status)  cmd_status || exit 1 ;;
  schema)  cmd_schema || exit 1 ;;
  sql)     cmd_sql "$@" || exit 1 ;;
  sql-stdin) cmd_sql_stdin || exit 1 ;;
  psql-ro) cmd_psql_ro "$@" || exit 1 ;;
  psql-ro-stdin) cmd_psql_ro_stdin || exit 1 ;;
  psql-limpeza) cmd_psql_limpeza "$@" || exit 1 ;;
  psql-limpeza-stdin) cmd_psql_limpeza_stdin || exit 1 ;;
  instancia) cmd_instancia || exit 1 ;;
  restart)
    if [ "$EM_CONTAINER" = 1 ]; then cmd_restart_api || exit 1; else dc restart api || exit 1; fi ;;
  puxar-agenda)
    [ "$EM_CONTAINER" = 1 ] && { falha "puxar-agenda só roda fora do contêiner"; exit 1; }
    dc exec -T api sh -c 'rm -f /evolution/instances/greg-agenda-puxada; : > /evolution/instances/greg-puxar-agenda' || exit 1
    cmd_restart_api || exit 1
    echo "agenda pedida: os nomes entram em vigia.agenda em cerca de um minuto" ;;
  aplicar-sql) aplicar_sql || exit 1; echo "infra/sql aplicado" ;;
  bloquear) cmd_bloqueio bloquear "$@" || exit 1 ;;
  desbloquear) cmd_bloqueio desbloquear "$@" || exit 1 ;;
esac
exit 0
