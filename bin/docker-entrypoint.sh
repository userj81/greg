#!/bin/bash
# Entrada do contêiner do Greg (Dockerfile). Prepara dados/vigia.env a partir do ambiente do compose, espera a Evolution,
# aplica o SQL do Greg no Postgres, cria a instância do WhatsApp (uma vez), sobe o agendador em segundo plano e deixa o
# painel em primeiro plano. Quem chama: docker compose (serviço vigia).
# O que não faz: não imprime segredo; não gera QR (isso é só por clique no painel); não fala com nada fora da rede do compose.
set -euo pipefail
RAIZ=/app
DADOS="${VIGIA_DADOS:-$RAIZ/dados}"
APP_ENV="$DADOS/vigia.env"
export VIGIA_ENV="$APP_ENV" VIGIA_ENV_APP="$APP_ENV"
export EVOLUTION_URL="${EVOLUTION_URL:-http://api:8080}"
umask 077
mkdir -p "$DADOS"

log() { echo "vigia: $*"; }

# Contêiner acabou de nascer: nenhum ciclo está rodando. Um lock no volume é de outra vida do contêiner, e o pid gravado
# nele pode coincidir com um processo vivo daqui (os pids recomeçam), o que travaria todos os ciclos.
rm -rf "$DADOS/vigia.lock"

# 1. dados/vigia.env: tudo vem do ambiente do compose (docker.env), menos a apikey da instância, que a Evolution devolve
#    na criação e fica guardada aqui entre reinícios.
/usr/local/bin/python3 - "$APP_ENV" <<'PY'
import os, re, sys
from pathlib import Path
arq = Path(sys.argv[1])
antigo = {}
if arq.exists():
    for ln in arq.read_text(encoding="utf-8").splitlines():
        if "=" in ln and not ln.startswith("#"):
            k, v = ln.split("=", 1)
            antigo[k.strip()] = v.strip()
token = os.environ.get("VIGIA_TOKEN", "")
if not re.fullmatch(r"[A-Za-z0-9_-]{16,}", token):
    sys.exit("vigia: VIGIA_TOKEN ausente ou curto em docker.env (rode bin/preparar-docker.sh)")
dono = re.sub(r"\D", "", os.environ.get("DONO_WHATSAPP", ""))
valores = {
    "EVOLUTION_URL": os.environ.get("EVOLUTION_URL", "http://api:8080"),
    "EVOLUTION_INSTANCE": os.environ.get("EVOLUTION_INSTANCE") or "vigia",
    "EVOLUTION_INSTANCE_APIKEY": antigo.get("EVOLUTION_INSTANCE_APIKEY", ""),
    "VIGIA_PORT": "8120",
    "VIGIA_TOKEN": token,
    "FRENTES_URL": os.environ.get("FRENTES_URL", ""),
    "FRENTES_TOKEN": os.environ.get("FRENTES_TOKEN", ""),
    "DONO_WHATSAPP": dono,
    "AVISO_INSTANCIA": os.environ.get("AVISO_INSTANCIA", ""),
    "AVISO_EVOLUTION_URL": os.environ.get("AVISO_EVOLUTION_URL", ""),
    "AVISO_EVOLUTION_APIKEY": os.environ.get("AVISO_EVOLUTION_APIKEY", ""),
    "VIGIA_CONVERSAS_DO_SISTEMA": os.environ.get("VIGIA_CONVERSAS_DO_SISTEMA", ""),
}
novo = arq.with_suffix(".tmp")
novo.write_text("# gerado por bin/docker-entrypoint.sh a cada partida; edite docker.env, não este arquivo\n"
                + "".join(f"{k}={v}\n" for k, v in valores.items()), encoding="utf-8")
os.chmod(novo, 0o600)
os.replace(novo, arq)
PY
[ "$(readlink "$RAIZ/.env" 2>/dev/null)" = "dados/vigia.env" ] || [ "$(readlink -f "$RAIZ/.env")" = "$(readlink -f "$APP_ENV")" ] \
  || { log "/app/.env não aponta para $APP_ENV (imagem montada fora do Dockerfile?)"; exit 2; }

# 2. Evolution de pé e SQL do Greg aplicado (papéis vigia_ro/vigia_limpeza, fila, bloqueios).
log "esperando a Evolution em $EVOLUTION_URL"
for _ in $(seq 1 120); do
  "$RAIZ/bin/evolution.sh" status >/dev/null 2>&1 && break
  sleep 3
done
"$RAIZ/bin/evolution.sh" up

# 3. Instância do WhatsApp: cria uma vez e guarda a apikey dela em dados/vigia.env (sem imprimir).
if ! grep -qE '^EVOLUTION_INSTANCE_APIKEY=.+' "$APP_ENV"; then
  "$RAIZ/bin/evolution.sh" instancia
fi

# 4. Claude Code: o login fica no volume da home (uma vez: docker compose exec -it vigia claude, depois /login).
if [ ! -s "$HOME/.claude/.credentials.json" ] && [ ! -s "$HOME/.claude.json" ]; then
  log "Claude Code sem login: rode 'docker compose exec -it vigia claude' e faça /login; até lá o ciclo lê e não classifica"
fi

# 5. Agendador (faz o papel dos LaunchAgents) em segundo plano; painel em primeiro plano.
/usr/local/bin/python3 "$RAIZ/bin/agendador.py" &
log "painel em http://127.0.0.1:${VIGIA_PORTA_HOST:-8120} (entre com o VIGIA_TOKEN do docker.env)"
exec /usr/local/bin/python3 -P "$RAIZ/app/server.py"
