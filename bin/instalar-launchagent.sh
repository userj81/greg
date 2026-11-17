#!/bin/bash
# Instala ou remove os LaunchAgents do Greg (infra/launchagents/*.plist) em ~/Library/LaunchAgents.
# Quem chama: o dono, uma vez, na F5 (deploy). O que não faz: nunca age sem --sim; não grava segredo.
# Saída: 0 ok · 1 algum agente falhou (os demais seguem) · 2 não rodou (uso errado, plist ausente, caminho com caractere proibido).
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ORIGEM="${VIGIA_PLIST_ORIGEM:-$RAIZ/infra/launchagents}"
DESTINO="${VIGIA_LAUNCHAGENTS_DIR:-$HOME/Library/LaunchAgents}"
DOMINIO="gui/$(id -u)"
SIM=0; DRY=0; REMOVER=0

uso() {
  cat <<'AJUDA'
Uso: bin/instalar-launchagent.sh [--dry-run] [--sim] [--remover | --desinstalar]

  (sem opção)     igual a --dry-run: imprime o que faria e não muda nada
  --dry-run       imprime o que faria (cópia com o caminho do repo, bootout, bootstrap, print)
  --sim           executa de verdade (copia para ~/Library/LaunchAgents, launchctl bootout/bootstrap)
  --remover       com --sim: launchctl bootout e apaga os plists copiados (alias --desinstalar)

Agentes: io.vigia.vigia-whatsapp (ciclo :00 e :30) · vigia-painel (KeepAlive; recusado se app/server.py não existir) ·
vigia-semanal (todo dia 20:05, relatório) · vigia-semanal-limpeza (todo dia 20:35, --executar-limpeza).
AJUDA
}

for a in "$@"; do
  case "$a" in
    --sim) SIM=1 ;;
    --dry-run) DRY=1 ;;
    --remover|--desinstalar) REMOVER=1 ;;
    -h|--help) uso; exit 0 ;;
    *) echo "instalar-launchagent.sh: opção desconhecida: $a" >&2; uso >&2; exit 2 ;;
  esac
done
if [ "$SIM" -eq 0 ]; then DRY=1; fi

executar() {
  if [ "$DRY" -eq 1 ]; then echo "[dry-run] $*"; else "$@"; fi
}

# O sed que gera o plist usa | como delimitador: caminho com [|&\<>] ou quebra de linha quebraria ou injetaria.
for v in "$RAIZ" "$HOME"; do
  case "$v" in
    *[\|\&\\\<\>]*|*$'\n'*) echo "instalar-launchagent.sh: caminho com caractere proibido ([|&\\<>] ou quebra de linha): $v" >&2; exit 2 ;;
  esac
done

shopt -s nullglob
PLISTS=("$ORIGEM"/io.vigia.vigia-*.plist)
if [ "${#PLISTS[@]}" -eq 0 ]; then echo "nenhum plist em $ORIGEM" >&2; exit 2; fi

[ "$DRY" -eq 1 ] || mkdir -p "$DESTINO"

# Espera até 10 s o launchd esquecer o agente depois do bootout.
esperar_descarregar() {
  local label="$1" ini=$SECONDS
  while launchctl print "$DOMINIO/$label" >/dev/null 2>&1; do
    [ $(( SECONDS - ini )) -lt 10 ] || return 1
    sleep 1
  done
}

instalar_um() {
  local p="$1" nome label alvo tmp
  nome="$(basename "$p")"; label="${nome%.plist}"; alvo="$DESTINO/$nome"
  if [ "$REMOVER" -eq 1 ]; then
    echo "== removendo $label"
    executar launchctl bootout "$DOMINIO/$label" 2>/dev/null || true
    executar rm -f "$alvo"
    return 0
  fi
  echo "== instalando $label"
  if [ "$label" = "io.vigia.vigia-painel" ] && [ ! -f "$RAIZ/app/server.py" ]; then
    echo "RECUSADO $label: $RAIZ/app/server.py não existe (o painel não subiria)" >&2
    return 1
  fi
  if [ "$DRY" -eq 1 ]; then
    echo "[dry-run] sed s|/REPO_RAIZ|$RAIZ| e s|/REPO_HOME|$HOME| em $p > temporário, plutil -lint, mv para $alvo"
  else
    tmp="$(mktemp "$DESTINO/.${label}.XXXXXX")" || return 1
    if ! sed -e "s|/REPO_RAIZ|$RAIZ|g" -e "s|/REPO_HOME|$HOME|g" "$p" > "$tmp" \
       || ! plutil -lint "$tmp" >/dev/null || ! chmod 644 "$tmp" || ! mv -f "$tmp" "$alvo"; then
      rm -f "$tmp"; echo "falha ao gerar $alvo" >&2; return 1
    fi
  fi
  if [ "$DRY" -eq 1 ]; then
    echo "[dry-run] launchctl bootout $DOMINIO/$label (se carregado) e espera até 10 s"
  elif launchctl print "$DOMINIO/$label" >/dev/null 2>&1; then
    launchctl bootout "$DOMINIO/$label" 2>/dev/null || true
    esperar_descarregar "$label" || { echo "$label ainda carregado após 10 s do bootout" >&2; return 1; }
  fi
  if [ "$DRY" -eq 1 ]; then
    echo "[dry-run] launchctl bootstrap $DOMINIO $alvo (2 tentativas)"
    echo "[dry-run] launchctl print $DOMINIO/$label | grep -E 'state|runs|last exit'"
    return 0
  fi
  if ! launchctl bootstrap "$DOMINIO" "$alvo"; then
    sleep 2
    launchctl bootstrap "$DOMINIO" "$alvo" || { echo "bootstrap de $label falhou" >&2; return 1; }
  fi
  launchctl print "$DOMINIO/$label" 2>/dev/null | grep -E 'state =|runs =|last exit code' || true
}

FALHAS=0
for p in "${PLISTS[@]}"; do
  instalar_um "$p" || FALHAS=$(( FALHAS + 1 ))
done
if [ "$DRY" -eq 1 ]; then echo "(dry-run: nada foi alterado; use --sim para executar)"; fi
if [ "$FALHAS" -gt 0 ]; then echo "$FALHAS agente(s) com falha" >&2; exit 1; fi
exit 0
