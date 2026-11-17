#!/bin/bash
# bin/ci.sh — o VERDE da CONSTITUTION executável, local (nada sai do Mac): compila, varre segredos,
# valida o compose e roda a suíte inteira. É o que .githooks/pre-commit chama antes de cada commit.
# Uso: bin/ci.sh [--pre-commit]      --pre-commit: gitleaks só sobre o que está staged (mais rápido)
# Saída: 0 tudo verde · 1 alguma etapa falhou (o trecho final do log da etapa sai na tela) · 2 não rodou
set -uo pipefail
RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${VIGIA_PYTHON:-/opt/homebrew/bin/python3}"
[ -x "$PY" ] || PY="$(command -v python3 || echo python3)"   # fora do Mac (Linux, CI): python3 do PATH
MODO="${1:-}"
[ -x "$PY" ] || { echo "ci: python não encontrado em $PY (VIGIA_PYTHON)"; exit 2; }
cd "$RAIZ" || exit 2
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
falhou=0

etapa() {  # etapa NOME comando...
  local nome="$1" ini=$SECONDS; shift
  if "$@" >"$TMP/$nome.out" 2>&1 </dev/null; then
    printf '  ✓ %-9s %4ds\n' "$nome" $((SECONDS - ini))
  else
    printf '  ✗ %-9s %4ds\n' "$nome" $((SECONDS - ini))
    falhou=1
    tail -n 30 "$TMP/$nome.out" | sed 's/^/      /'
  fi
}

echo "== ci $( [ "$MODO" = "--pre-commit" ] && echo '(pre-commit)' || echo '(completo)' ) · $(date '+%Y-%m-%d %H:%M:%S')"
etapa compila "$PY" -m compileall -q app bin testes

if command -v gitleaks >/dev/null 2>&1; then
  if [ "$MODO" = "--pre-commit" ]; then
    etapa segredos gitleaks git --pre-commit --staged --no-banner --redact .
  else
    etapa segredos gitleaks git --no-banner --redact .
  fi
else
  echo "  · segredos  pulado: gitleaks ausente (brew install gitleaks)"
fi

if command -v docker >/dev/null 2>&1 && [ -f infra/.env.compose ]; then
  etapa compose docker compose --env-file infra/.env.compose -f infra/docker-compose.yml config -q
else
  echo "  · compose   pulado: docker ou infra/.env.compose ausente"
fi

etapa testes "$PY" -W ignore -m unittest discover -v -s testes -t .
grep -E '^(Ran |OK|FAILED|FAIL: |ERROR: )' "$TMP/testes.out" | sed 's/^/      /'   # todos os nomes que falharam, não só o último traceback
# Teste pulado sai pelo nome e com o motivo: "OK (skipped=1)" sem dizer qual não vale como prova.
awk '/^test[^ ]* \(/ { nome = $1 " " $2 }
     / \.\.\. skipped / { motivo = $0; sub(/.* \.\.\. skipped /, "", motivo); print "      pulado: " nome " · " motivo }' "$TMP/testes.out"

if [ "$falhou" -ne 0 ]; then
  echo "== VERMELHO: corrija antes de commitar (pular só em emergência: git commit --no-verify, e diga por quê na mensagem)"
  exit 1
fi
echo "== VERDE"
