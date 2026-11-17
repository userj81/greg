#!/bin/bash
# Gera docker.env (0600) com senhas aleatórias para o Greg no Docker. Não sobrescreve um docker.env que já existe.
# Quem chama: você, uma vez, antes do primeiro `docker compose up`; ou o Greg.app do DMG, com --dono e --tz.
# Uso: bin/preparar-docker.sh [--dono 55DDNNNNNNNNN] [--tz America/Sao_Paulo] [--portas-livres]
#      Com docker.env já existente, --dono/--tz só atualizam essas duas linhas (as senhas ficam). Não imprime senha.
#      --portas-livres: confere as duas portas que o compose publica em 127.0.0.1 (painel 8120, Evolution 8080).
#      Porta com outro programa escutando é trocada pela próxima livre e gravada no docker.env
#      (VIGIA_PORTA_HOST, EVOLUTION_PORTA_HOST). Rode com o Greg PARADO: com ele no ar as portas dele mesmo
#      aparecem ocupadas. Imprime as duas, uma por linha (CHAVE=porta). O Greg.app lê a porta do painel dali.
#      O Postgres não publica porta: linha PG_PORTA_HOST que sobrou num docker.env antigo não é mais lida.
set -euo pipefail
RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARQ="$RAIZ/docker.env"
DONO=""; FUSO=""; PORTAS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dono) DONO="${2:-}"; shift 2 ;;
    --tz)   FUSO="${2:-}"; shift 2 ;;
    --portas-livres) PORTAS=1; shift ;;
    *) echo "uso: $0 [--dono 55DDNNNNNNNNN] [--tz Região/Cidade] [--portas-livres]"; exit 2 ;;
  esac
done
[ -z "$DONO" ] || [[ "$DONO" =~ ^[0-9]{10,15}$ ]] || { echo "--dono: só dígitos, com DDI e DDD (10 a 15)"; exit 2; }
[ -z "$FUSO" ] || [[ "$FUSO" =~ ^[A-Za-z_]+(/[A-Za-z0-9_+-]+){0,2}$ ]] || { echo "--tz: fuso no formato Região/Cidade"; exit 2; }

# troca CHAVE=... no arquivo sem passar o valor por sed (o valor já foi validado acima)
gravar() {
  local chave="$1" valor="$2" tmp
  tmp="$(mktemp "$ARQ.XXXXXX")"
  awk -v k="$chave" -v v="$valor" 'BEGIN{feito=0} $0 ~ "^"k"=" {print k"="v; feito=1; next} {print} END{if(!feito) print k"="v}' "$ARQ" > "$tmp"
  chmod 600 "$tmp"; mv "$tmp" "$ARQ"
}

# Alguém escuta nesta porta do 127.0.0.1? (conectou = ocupada). VIGIA_PORTAS_OCUPADAS, lista separada por espaço,
# existe para o teste dizer quais estão ocupadas sem abrir porta de verdade.
porta_ocupada() {
  if [ -n "${VIGIA_PORTAS_OCUPADAS+x}" ]; then
    case " $VIGIA_PORTAS_OCUPADAS " in *" $1 "*) return 0 ;; *) return 1 ;; esac
  fi
  (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
}
valor_de() { awk -F= -v k="$1" '$1 == k {v = $2} END {print v}' "$ARQ"; }

# Para cada porta publicada: fica a que já está no docker.env (ou a padrão) se estiver livre; ocupada, sobe de uma em
# uma até achar livre (no máximo 200 acima) e grava. Porta já trocada não volta sozinha para a padrão: endereço
# que a pessoa já usou (conector do Claude Code, favorito) continua valendo.
escolher_portas() {
  local par chave padrao gravada inicio p usadas=" " mudou=0
  for par in VIGIA_PORTA_HOST:8120 EVOLUTION_PORTA_HOST:8080; do
    chave="${par%%:*}"; padrao="${par##*:}"
    gravada="$(valor_de "$chave")"
    [[ "$gravada" =~ ^[0-9]{4,5}$ ]] && [ "$gravada" -le 65535 ] || gravada=""
    inicio="${gravada:-$padrao}"; p="$inicio"
    while porta_ocupada "$p" || [[ "$usadas" == *" $p "* ]]; do
      p=$((p + 1))
      [ "$p" -le $((inicio + 200)) ] && [ "$p" -le 65535 ] || { echo "$chave: não achei porta livre entre $inicio e $((inicio + 200))" >&2; exit 1; }
    done
    usadas="$usadas$p "
    if [ "$p" != "$inicio" ]; then gravar "$chave" "$p"; mudou=1; fi
    echo "$chave=$p"
  done
  [ "$mudou" = 0 ] || echo "porta trocada: fora do Greg.app, suba com: docker compose --env-file docker.env up -d --build" >&2
}

if [ -e "$ARQ" ]; then
  if [ -n "$DONO$FUSO" ]; then
    [ -z "$DONO" ] || gravar DONO_WHATSAPP "$DONO"
    [ -z "$FUSO" ] || gravar TZ "$FUSO"
    echo "docker.env atualizado (senhas mantidas)"
  elif [ "$PORTAS" = 0 ]; then
    echo "docker.env já existe; nada feito (apague-o só se quiser senhas novas: o Postgres guarda as antigas no volume)"
  fi
  [ "$PORTAS" = 0 ] || escolher_portas
  exit 0
fi
command -v openssl >/dev/null 2>&1 || { echo "preciso do openssl para gerar as senhas"; exit 2; }
s() { openssl rand -hex 24; }
umask 077
cat > "$ARQ" <<ENV
# Greg no Docker. Gerado por bin/preparar-docker.sh em $(date '+%Y-%m-%d %H:%M'). Fica fora do git (0600).

# --- senhas (aleatórias; não precisa mexer) ---
EVOLUTION_GLOBAL_KEY=$(s)
POSTGRES_PASSWORD=$(s)
VIGIA_RO_PASSWORD=$(s)
VIGIA_LIMPEZA_PASSWORD=$(s)
# senha do painel (http://127.0.0.1:8120): é com ela que você entra
VIGIA_TOKEN=$(s)

# --- você ---
# fuso do fechamento do dia (21:00) e da semana
TZ=${FUSO:-America/Sao_Paulo}
# seu número com DDI e DDD, só dígitos (formato 55DDNNNNNNNNN): alvo da sonda que confere se a conexão entrega
DONO_WHATSAPP=${DONO}
# nome da instância do WhatsApp dentro da Evolution
EVOLUTION_INSTANCE=vigia

# --- opcionais ---
# Frentes externo (vazio = o Frentes embutido do Greg, num SQLite)
FRENTES_URL=
FRENTES_TOKEN=
# aviso por OUTRO número (nunca o pessoal) quando a conexão cai ou a semana fecha: Evolution do bot + instância + chave
AVISO_EVOLUTION_URL=
AVISO_INSTANCIA=
AVISO_EVOLUTION_APIKEY=
ENV
chmod 600 "$ARQ"
if [ -n "$DONO" ]; then echo "docker.env criado (0600). Rode: docker compose up -d --build"
else echo "docker.env criado (0600). Preencha DONO_WHATSAPP e rode: docker compose up -d --build"; fi
echo "A senha do painel é o VIGIA_TOKEN que está em docker.env."
[ "$PORTAS" = 0 ] || escolher_portas
