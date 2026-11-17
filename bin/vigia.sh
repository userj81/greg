#!/bin/bash
# Operação do Greg do WhatsApp: ciclo agendado, painel, fechamento semanal, status e logs.
# Quem chama: LaunchAgent io.vigia.vigia-whatsapp (ciclo), o dono e o RUNBOOK.
# O que não faz: não imprime segredo, não registra texto de mensagem, não instala LaunchAgent.
# Saída: 0 ok (ou pausado) · 1 falhou · 2 não rodou (Colima/Evolution indisponível, lock ocupado, uso errado).
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DADOS="${VIGIA_DADOS:-$RAIZ/dados}"
LOG="${VIGIA_LOG_CICLO:-$HOME/Library/Logs/vigia-whatsapp.log}"
LOCK="$DADOS/vigia.lock"
PYTHON="${VIGIA_PYTHON:-/opt/homebrew/bin/python3}"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3 || echo python3)"   # fora do Mac (Linux, CI): python3 do PATH
COLETOR="${VIGIA_COLETOR:-$RAIZ/bin/coletor.py}"
FECHAMENTO="${VIGIA_FECHAMENTO:-$RAIZ/bin/fechar-dia.py}"   # D-57: fechamento do dia, roda depois do coletor
EVOLUTION_SH="${VIGIA_EVOLUTION_SH:-$RAIZ/bin/evolution.sh}"
LOCK_EXPIRA_S=3600        # 1 h: só vale para lock sem pid legível; com pid, vale kill -0
ESPERA_EVOLUTION_S=90
ESPERA_RECUPERACAO_S="${VIGIA_ESPERA_RECUPERACAO_S:-60}"   # D-54: espera antes da segunda leitura após recuperação
ESPERA_LOCK_S=120         # semanal espera o ciclo corrente soltar o lock
export PATH="${PATH:-/usr/bin:/bin}:/opt/homebrew/bin:$HOME/.local/bin:/usr/local/bin"

# I7: porta do painel = VIGIA_PORT do ambiente, senão do .env, senão 8120. Exportada para o aviso do monitor e o status.
if [ -z "${VIGIA_PORT:-}" ] && [ -f "${VIGIA_ENV:-$RAIZ/.env}" ]; then
  VIGIA_PORT="$(grep -E '^VIGIA_PORT=' "${VIGIA_ENV:-$RAIZ/.env}" 2>/dev/null | tail -n1 | cut -d= -f2- \
    | sed -e 's/ #.*//' -e "s/['\"[:space:]]//g" || true)"
fi
case "${VIGIA_PORT:-}" in ''|*[!0-9]*) VIGIA_PORT=8120 ;; esac
export VIGIA_PORT
# I6: o número do aviso não tem default no código; vem do ambiente ou do .env (só dígitos).
if [ -z "${DONO_WHATSAPP:-}" ] && [ -f "${VIGIA_ENV:-$RAIZ/.env}" ]; then
  DONO_WHATSAPP="$(grep -E '^DONO_WHATSAPP=' "${VIGIA_ENV:-$RAIZ/.env}" 2>/dev/null | tail -n1 | cut -d= -f2- \
    | sed -e 's/ #.*//' -e "s/['\"[:space:]]//g" || true)"
fi
case "${DONO_WHATSAPP:-}" in ''|*[!0-9]*) unset DONO_WHATSAPP ;; *) export DONO_WHATSAPP ;; esac
# Pedido 37 levou a instância do bot dos avisos para o .env; o monitor (avisar_se_preciso) roda daqui com o ambiente do
# LaunchAgent, então ela tem de ser exportada como o número — sem isso todo aviso do monitor falhava em silêncio
# ("AVISO_INSTANCIA ausente", visto no ciclo real de 09/11 11:00, pedido 41). Só letras, dígitos, '-', '_' e '.'.
if [ -z "${AVISO_INSTANCIA:-}" ] && [ -f "${VIGIA_ENV:-$RAIZ/.env}" ]; then
  AVISO_INSTANCIA="$(grep -E '^AVISO_INSTANCIA=' "${VIGIA_ENV:-$RAIZ/.env}" 2>/dev/null | tail -n1 | cut -d= -f2- \
    | sed -e 's/ #.*//' -e "s/['\"[:space:]]//g" || true)"
fi
case "${AVISO_INSTANCIA:-}" in ''|*[!A-Za-z0-9._-]*) unset AVISO_INSTANCIA ;; *) export AVISO_INSTANCIA ;; esac
# Aviso direto numa Evolution (sem cofre): URL e chave da instância do bot, só se estiverem no .env (app/aviso.py).
for _chave in AVISO_EVOLUTION_URL AVISO_EVOLUTION_APIKEY; do
  if [ -z "${!_chave:-}" ] && [ -f "${VIGIA_ENV:-$RAIZ/.env}" ]; then
    printf -v "$_chave" '%s' "$(grep -E "^${_chave}=" "${VIGIA_ENV:-$RAIZ/.env}" 2>/dev/null | tail -n1 | cut -d= -f2- \
      | sed -e 's/ #.*//' -e "s/['\"[:space:]]//g" || true)"
  fi
  if [ -n "${!_chave:-}" ]; then export "${_chave?}"; else unset "$_chave"; fi
done
unset _chave

uso() {
  cat <<'AJUDA'
Uso: bin/vigia.sh [subcomando]

Subcomandos:
  ciclo (padrão)   lock + Colima + Evolution + coletor + monitor de conexão + fechamento do dia (bin/fechar-dia.py,
                   só depois de [horarios].diario: consolida cartões provisórios e sobe ao Frentes); com dados/PAUSADO
                   pula o coletor e roda só o monitor (se a Evolution já estiver de pé; sem colima start)
  painel           sobe o painel em 127.0.0.1:VIGIA_PORT (.env; padrão 8120) (python3 -m app.server), em primeiro plano
  semanal [--dry-run | --executar-limpeza | --segurar [DIAS]]
                   fechamento semanal (bin/fechar-semana.py) com Colima + lock + Evolution como o ciclo:
                   sem opção = relatório; limpeza só com --executar-limpeza; --segurar adia a limpeza.
                   Com dados/PAUSADO nunca sobe Colima/Evolution (Evolution fora = adiado, exit 0);
                   --segurar e --dry-run não exigem Colima, Evolution nem lock
  historico [--dias N] [--pendentes] [--conversa NOME] [--json | --contagem]
                   histórico de análise das conversas (bin/historico.py): o que cada conversa trouxe e em que pé
                   ficou; --pendentes = só quem aguarda resposta. Só leitura do vigia.db: sem Colima, Evolution nem lock
  status           pausa, lock, Colima, Evolution, painel e últimas linhas do log
  logs [N]         últimas N linhas (padrão 40) de vigia-whatsapp.log (ciclo + coletor + monitor)
  -h, --help       esta ajuda

Pausar: touch dados/PAUSADO · Retomar: rm dados/PAUSADO
Saída: 0 ok (ou pausado) · 1 falhou · 2 não rodou.
Log: ~/Library/Logs/vigia-whatsapp.log (só ids e contagens, nunca texto de mensagem).
AJUDA
}

registrar() {
  mkdir -p "$(dirname "$LOG")"
  printf '%s vigia.sh %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"
}

# Lock atômico por mkdir; o dono grava o próprio pid em $LOCK/pid.
# Recolhe só lock de dono morto (pid inexistente) ou, sem pid legível, com mais de LOCK_EXPIRA_S.
# kill -0 com EPERM (processo de outro usuário, ex.: pid reaproveitado) = existe: não recolhe (I-EPERM).
pegar_lock() {
  (umask 077; mkdir -p "$DADOS")      # I8: dados/ nasce 0700
  chmod 700 "$DADOS" 2>/dev/null || true
  if mkdir "$LOCK" 2>/dev/null; then echo "$$" > "$LOCK/pid"; return 0; fi
  local mtime agora idade dono="" morto=0
  mtime="$(stat -c %Y "$LOCK" 2>/dev/null || stat -f %m "$LOCK" 2>/dev/null || echo 0)"   # GNU primeiro: no Linux "stat -f" é outra coisa
  agora="$(date +%s)"
  idade=$(( agora - mtime ))
  dono="$(cat "$LOCK/pid" 2>/dev/null || true)"
  case "$dono" in
    ''|*[!0-9]*) [ "$idade" -gt "$LOCK_EXPIRA_S" ] && morto=1 ;;
    *)
      if kill -0 "$dono" 2>/dev/null; then :
      elif ps -p "$dono" >/dev/null 2>&1; then registrar "lock com dono pid $dono existe mas não é nosso (EPERM); mantido"
      else morto=1
      fi ;;
  esac
  if [ "$morto" -eq 1 ]; then
    registrar "lock de dono morto (pid ${dono:-?}, ${idade}s); recolhendo"
    rm -rf "$LOCK"
    if mkdir "$LOCK" 2>/dev/null; then echo "$$" > "$LOCK/pid"; return 0; fi
  fi
  registrar "outro ciclo em andamento (lock com ${idade}s); saindo"
  return 1
}

# Só solta o lock que é seu (pid gravado == $$).
soltar_lock() {
  local dono
  dono="$(cat "$LOCK/pid" 2>/dev/null || true)"
  if [ "$dono" = "$$" ]; then rm -f "$LOCK/pid"; rmdir "$LOCK" 2>/dev/null || true; fi
}

# Mac não dorme enquanto este processo vive (caffeinate -w sai junto com o script).
manter_acordado() {
  if command -v caffeinate >/dev/null 2>&1; then
    caffeinate -i -w "$$" >/dev/null 2>&1 &
  fi
}

pegar_lock_esperando() {
  local ini=$SECONDS
  while ! pegar_lock; do
    [ $(( SECONDS - ini )) -lt "$ESPERA_LOCK_S" ] || return 1
    sleep 10
  done
}

garantir_colima() {
  [ "${VIGIA_CONTAINER:-0}" = 1 ] && return 0   # no contêiner quem mantém a Evolution de pé é o compose
  if ! command -v colima >/dev/null 2>&1; then
    registrar "colima não encontrado no PATH"; return 1
  fi
  if colima status >/dev/null 2>&1; then return 0; fi
  registrar "colima parado; colima start"
  colima start >>"$LOG" 2>&1 || { registrar "colima start falhou"; return 1; }
}

evolution_ok() { "$EVOLUTION_SH" status >/dev/null 2>&1; }

# Pedido 63, lote 2: peça que o shell enxerga (Docker, Evolution, fechamentos) vai para a central de avisos
# (app/saude.py -> dados/alertas.json). Nunca derruba o ciclo.
saude() { (cd "$RAIZ" && VIGIA_DADOS="$DADOS" "$PYTHON" -m app.saude "$@") >>"$LOG" 2>&1 || true; }

# Costura F3 (Emenda F4(4) item 9 + F4(9) item 1): o JSON do monitor com avisar=true vira aviso ao dono pelo bot da
# agência (app/aviso.py: só motivo fixo + link, nunca texto de conversa — D-4). Falha grava
# dados/aviso-pendente.json {evento, motivo, criado_em}; todo ciclo reenvia o pendente e o apaga no sucesso.
# Falha no aviso nunca derruba o ciclo.
avisar_se_preciso() {
  local json="${1:-}"
  [ -n "$json" ] || [ -e "$DADOS/aviso-pendente.json" ] || return 0
  (cd "$RAIZ" && VIGIA_MONITOR_JSON="$json" VIGIA_DADOS_DIR="$DADOS" "$PYTHON" - <<'PY') >>"$LOG" 2>&1 || registrar "aviso do monitor falhou"
import json, os, sys, time
sys.path.insert(0, os.getcwd())
from app.aviso import enviar
pend = os.path.join(os.environ["VIGIA_DADOS_DIR"], "aviso-pendente.json")
porta = os.environ.get("VIGIA_PORT") or "8120"


# Pedido 41: os motivos novos do monitor viram frase; os antigos (caiu, continua_fora, …) seguem como estão.
FRASES = {
    "sonda_falhou_reiniciada": "sem resposta (socket zumbi); Evolution reiniciada sozinha",
    "sonda_sem_foto_reiniciada": "sem resposta (socket zumbi); Evolution reiniciada sozinha",
    "sonda_falhou_persistente": "continua sem resposta depois do reinício: gere o QR de novo",
    "sonda_sem_foto_persistente": "continua sem resposta depois do reinício: gere o QR de novo",
    "sonda_falhou_reinicio_falhou": "sem resposta e o reinício automático falhou",
    "sonda_sem_foto_reinicio_falhou": "sem resposta e o reinício automático falhou",
    # D-56 (pedido 44): o teste de entrega do bot não apareceu no Postgres
    "entrega_travada": "aberta, mas a entrega travou: o teste do bot não chegou; use Parear de novo",
    "entrega_travada_reiniciada": "aberta, mas a entrega travou: o teste do bot não chegou nem depois do reinício automático; use Parear de novo",
    "entrega_travada_reinicio_falhou": "aberta, mas a entrega travou e o reinício automático falhou",
    "entrega_travada_reinicio_recente": "aberta, mas a entrega travou: o teste do bot não chegou e houve reinício há pouco; use Parear de novo",
}


def frase(motivo):
    if motivo in FRASES:
        return FRASES[motivo]
    if motivo.startswith("sem_mensagem_"):
        h = motivo[len("sem_mensagem_"):].rstrip("h")
        return f"aberta, mas sem mensagem nova há {h} h: confira Aparelhos conectados no celular"
    return motivo


def mandar(evento, motivo):
    return enviar(f"Greg: conexão {frase(motivo)} · http://127.0.0.1:{porta}/conexao")


def gravar(evento, motivo):
    tmp = pend + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"evento": evento, "motivo": motivo, "criado_em": int(time.time())}, f)
    os.replace(tmp, pend)


def resumo(motivo):
    """Pedido 43: aviso pendente de um problema que já passou vira "conexão <resumo> às HH:MM, já voltou" — o "caiu"
    das 14:40 de 09/11 chegou às 16:14 com tudo normal, como se fosse novo."""
    if motivo.startswith("sonda_"):
        return "sem resposta"
    if motivo.startswith("sem_mensagem_"):
        return "sem mensagem nova"
    if motivo.startswith("entrega_travada"):
        return "entrega travada"
    return motivo.replace("_", " ")


try:
    d = json.loads(os.environ.get("VIGIA_MONITOR_JSON") or "{}")
except ValueError:
    d = {}
if not isinstance(d, dict):
    d = {}
ok = True
if os.path.exists(pend):
    try:
        p = json.load(open(pend))
        if not isinstance(p, dict):
            raise ValueError
        ev, mot = str(p.get("evento") or "conexao")[:40], str(p.get("motivo") or p.get("evento") or "conexao")[:40]
        if d.get("evento") == "ok" and isinstance(p.get("criado_em"), int):
            hora = time.strftime("%H:%M", time.localtime(p["criado_em"]))
            enviado = enviar(f"Greg: conexão {resumo(mot)} às {hora}, já voltou · http://127.0.0.1:{porta}/conexao")
        else:
            enviado = mandar(ev, mot)
        if enviado:
            os.unlink(pend)
            print(json.dumps({"aviso_pendente": "reenviado", "motivo": mot}))
        else:
            ok = False
    except (ValueError, OSError):
        os.unlink(pend)  # arquivo corrompido não reenvia nada
if d.get("avisar"):
    evento = str(d.get("evento") or "conexao")[:40]
    motivo = str(d.get("motivo") or d.get("evento") or "conexao")[:40]
    if mandar(evento, motivo):
        print(json.dumps({"aviso_monitor": "enviado", "motivo": motivo}))
    else:
        gravar(evento, motivo)
        print(json.dumps({"aviso_monitor": "falhou", "motivo": motivo}))
        ok = False
sys.exit(0 if ok else 1)
PY
}

esperar_evolution() {
  local ini=$SECONDS
  if evolution_ok; then return 0; fi
  registrar "Evolution fora; evolution.sh up"
  "$EVOLUTION_SH" up >>"$LOG" 2>&1 || true
  while [ $(( SECONDS - ini )) -lt "$ESPERA_EVOLUTION_S" ]; do
    if evolution_ok; then return 0; fi
    sleep 3
  done
  registrar "Evolution não respondeu em ${ESPERA_EVOLUTION_S}s"
  return 1
}

# Monitor de conexão + aviso. Devolve o exit do monitor em MON_RC.
rodar_monitor() {
  MON_RC=0
  MON_SAIDA=""
  MON_SAIDA=$(cd "$RAIZ" && "$PYTHON" -m app.conexao --monitorar 2>>"$LOG") || MON_RC=$?
  printf '%s\n' "$MON_SAIDA" >>"$LOG"
  registrar "monitor exit $MON_RC"
  avisar_se_preciso "$MON_SAIDA"
}

ciclo() {
  local pausado=0
  if [ -e "$DADOS/PAUSADO" ]; then
    pausado=1
    # D-33: pausa pula o coletor, mas o monitor segue se a Evolution já está de pé (nunca colima start/up).
    if ! evolution_ok; then
      registrar "PAUSADO presente e Evolution fora; ciclo ignorado"
      return 0
    fi
  fi
  pegar_lock || return 2
  trap soltar_lock EXIT
  manter_acordado

  if [ "$pausado" -eq 1 ]; then
    registrar "PAUSADO presente; coletor pulado, só monitor"
    rodar_monitor
    [ "$MON_RC" -eq 0 ] && return 0
    [ "$MON_RC" -eq 1 ] && return 1
    return 2
  fi
  registrar "ciclo iniciado"

  garantir_colima || { saude abrir docker-parado; saude notificar; return 2; }
  saude fechar docker-parado
  esperar_evolution || { saude abrir evolution-fora; saude notificar; return 2; }

  local rc_col=0
  "$PYTHON" "$COLETOR" >>"$LOG" 2>&1 || rc_col=$?
  registrar "coletor exit $rc_col"

  # O monitor roda mesmo se o coletor falhou: desconexão não pode ficar sem registro (R-11).
  rodar_monitor
  local rc_mon="$MON_RC"

  # D-54 (pedido 43): o monitor recuperou a conexão neste ciclo (a instância reabriu, a sonda voltou ou a Evolution surda
  # foi reiniciada depois do despertar do Mac): o WhatsApp entrega agora o que ficou retido — espera e lê de novo, para a
  # análise sair neste ciclo e não daqui a meia hora. Premissa do Greg: ao ligar, recupera.
  if [ "$rc_mon" -eq 0 ] && printf '%s' "$MON_SAIDA" | grep -q '"recuperacao": "'; then
    registrar "conexão recuperada pelo monitor; segunda leitura em ${ESPERA_RECUPERACAO_S}s"
    sleep "$ESPERA_RECUPERACAO_S"
    rc_col=0
    # pedido 81: a segunda leitura não espera o intervalo escolhido em Ajustes, mas respeita o horário de leitura
    VIGIA_SEGUNDA_LEITURA=1 "$PYTHON" "$COLETOR" >>"$LOG" 2>&1 || rc_col=$?
    registrar "coletor (segunda leitura) exit $rc_col"
  fi

  # D-57 (pedido 45): fechamento do dia. Depois de [horarios].diario, consolida os cartões provisórios do dia (um passe do
  # Claude só nas conversas com 2+ cartões) e sobe o que sobrou ao Frentes; antes da hora, sai em 0 sem fazer nada.
  # Só roda com o coletor ok (Postgres e Frentes de pé); dia perdido é alcançado no próximo ciclo.
  local rc_fec=0
  if [ "$rc_col" -eq 0 ]; then
    "$PYTHON" "$FECHAMENTO" >>"$LOG" 2>&1 || rc_fec=$?
    if [ "$rc_fec" -ne 0 ]; then registrar "fechamento do dia exit $rc_fec"; fi
    saude fechamento dia "$rc_fec"
  fi
  saude notificar

  if [ "$rc_col" -eq 1 ] || [ "$rc_mon" -eq 1 ] || [ "$rc_fec" -eq 1 ]; then return 1; fi
  if [ "$rc_col" -ne 0 ] || [ "$rc_mon" -ne 0 ] || [ "$rc_fec" -ne 0 ]; then return 2; fi
  registrar "ciclo concluído"
  return 0
}

painel() {
  cd "$RAIZ"
  exec "$PYTHON" -m app.server
}

# D-32: roda todo dia (20:05 relatório, 20:35 limpeza); as guardas do fechar-semana.py tornam isso idempotente.
semanal() {
  cd "$RAIZ"
  local a so_leitura=0 pausado=0
  for a in "$@"; do
    case "$a" in --segurar|--dry-run) so_leitura=1 ;; esac
  done
  if [ "$so_leitura" -eq 1 ]; then
    # --segurar só grava dados/segurar-W e --dry-run é só leitura: sem Colima, Evolution nem lock.
    local rc0=0
    "$PYTHON" "$RAIZ/bin/fechar-semana.py" "$@" || rc0=$?
    return "$rc0"
  fi
  if [ -e "$DADOS/PAUSADO" ]; then pausado=1; fi
  if [ "$pausado" -eq 1 ] && ! evolution_ok; then
    # I1: a pausa é o kill switch (RUNBOOK §4): nunca colima start nem evolution.sh up.
    registrar "PAUSADO e Evolution fora; semanal adiado"
    return 0
  fi
  pegar_lock_esperando || return 2
  trap soltar_lock EXIT
  manter_acordado
  registrar "semanal iniciado ($*)"
  if [ "$pausado" -eq 1 ]; then
    registrar "PAUSADO presente; semanal sem colima start/evolution up"
  else
    garantir_colima || return 2
    esperar_evolution || return 2
  fi
  local rc=0
  "$PYTHON" "$RAIZ/bin/fechar-semana.py" "$@" || rc=$?
  registrar "semanal exit $rc"
  saude fechamento semana "$rc"
  saude notificar
  return "$rc"
}

status() {
  if [ -e "$DADOS/PAUSADO" ]; then echo "pausa:      SIM (dados/PAUSADO)"; else echo "pausa:      não"; fi
  if [ -d "$LOCK" ]; then echo "lock:       ocupado"; else echo "lock:       livre"; fi
  if command -v colima >/dev/null 2>&1 && colima status >/dev/null 2>&1; then echo "colima:     rodando"; else echo "colima:     parado"; fi
  if evolution_ok; then echo "evolution:  ok"; else echo "evolution:  fora"; fi
  if lsof -nP -iTCP:"$VIGIA_PORT" -sTCP:LISTEN >/dev/null 2>&1; then echo "painel:     escutando em $VIGIA_PORT"; else echo "painel:     parado (porta $VIGIA_PORT)"; fi
  echo "--- últimas linhas de $LOG"
  tail -n 8 "$LOG" 2>/dev/null || echo "(sem log ainda)"
}

logs() {
  local n="${1:-40}"
  echo "=== $LOG"; tail -n "$n" "$LOG" 2>/dev/null || echo "(vazio)"
}

sub="${1:-ciclo}"
[ "$#" -gt 0 ] && shift
case "$sub" in
  -h|--help|help) uso; exit 0 ;;
  ciclo)   rc=0; ciclo || rc=$?; exit "$rc" ;;
  painel)  painel ;;
  semanal) rc=0; semanal "$@" || rc=$?; exit "$rc" ;;
  historico) rc=0; "$PYTHON" "$RAIZ/bin/historico.py" "$@" || rc=$?; exit "$rc" ;;
  status)  status ;;
  logs)    logs "$@" ;;
  *) echo "vigia.sh: subcomando desconhecido: $sub" >&2; uso >&2; exit 2 ;;
esac
