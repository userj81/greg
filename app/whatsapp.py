"""Conversas do WhatsApp lidas do banco da Evolution (papel vigia_ro, só leitura) e a regra única de nome, grupo e foto
(r02, D-39). Método do Chatwoot (docs/specs/PLANO-R02-CONVERSAS.md): grupo pelo `subject` buscado ativamente, pessoa por
apelido local > nome salvo > pushName > número, LID pelo telefone alternativo, foto guardada em disco e baixada de novo
só quando o nome do arquivo na URL muda.
Quem chama: app/paginas/conversas.py (o psql é injetado; o servidor monta o real em montar_ctx).
O que não faz: não marca nada como lido (R-6), não lê antes do pareamento (R-2), não grava no banco da Evolution,
não loga texto de mensagem e não baixa nada fora de https://*.whatsapp.net.
"""
import base64
import hashlib
import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from app import lotes, rede
from app.erros import causa

log = logging.getLogger("vigia.whatsapp")

JID_OK = re.compile(r"^[0-9A-Za-z._:-]{1,80}@(s\.whatsapp\.net|g\.us|lid)$")
MSG_ID_OK = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
LIMITE_TEXTO = 4000

# ---------------------------------------------------------------------------
# psql somente leitura
# ---------------------------------------------------------------------------


def psql_ro(script: Path, timeout: float = 20.0, vivo: bool = False):
    """psql(query) -> list[list[str]] como vigia_ro, via `bin/evolution.sh psql-ro-stdin` (a query vai pelo stdin:
    o jid da conversa nunca aparece em argv/ps). Implementação única em lotes.psql_via_script."""
    if vivo:      # painel: uma sessão aberta para todas as consultas (pedido 52)
        return lotes.psql_sessao(script, "psql-ro-stdin", timeout)
    return lotes.psql_via_script(script, "psql-ro-stdin", timeout)


# ---------------------------------------------------------------------------
# Bloqueios (D-41): conversa sem leitura nenhuma
# ---------------------------------------------------------------------------

CONVITE = re.compile(r"^(?:https?://)?chat\.whatsapp\.com/(?:invite/)?([A-Za-z0-9]{10,40})(?:[/?#].*)?$")


def bloqueados(psql) -> set[str]:
    """jids em vigia.bloqueados. Falha do banco SOBE: quem lê para (leitura falha fechada)."""
    return {ln[0] for ln in psql("SELECT jid FROM vigia.bloqueados") if ln and JID_OK.match(ln[0])}


def comando_bloqueio(script: Path, timeout: float = 30.0):
    """rodar(acao, jid) via `bin/evolution.sh bloquear|desbloquear <jid>` (dono do banco; jid validado aqui e lá)."""
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(["/opt/homebrew/bin", "/usr/local/bin", env.get("PATH") or "/usr/bin:/bin"])

    def rodar(acao: str, jid: str) -> str:
        if acao not in ("bloquear", "desbloquear") or not JID_OK.match(jid or ""):
            raise ValueError("bloqueio: ação ou jid inválido")
        try:
            r = subprocess.run(["/bin/bash", str(script), acao, jid], capture_output=True, text=True, errors="replace",
                               timeout=timeout, stdin=subprocess.DEVNULL, env=env)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise lotes.PostgresIndisponivel(f"{acao} não executou ({type(e).__name__})") from None
        if r.returncode != 0:
            raise lotes.PostgresIndisponivel(f"{acao} saiu com código {r.returncode}")
        return r.stdout.strip()

    return rodar


def alvo_do_bloqueio(texto: str, evo=None) -> tuple[str, str]:
    """(jid, nome) a partir de link de convite, número ou jid. Link consulta o convite na Evolution (só lê, não entra
    no grupo). ValueError com a mensagem para a tela quando não dá para saber o alvo."""
    texto = (texto or "").strip()
    m = CONVITE.match(texto)
    if m:
        if evo is None:
            raise ValueError("Sem conexão com a Evolution para ler o convite.")
        g = evo.convite(m.group(1)) or {}
        jid = str(g.get("id") or "")
        if not (jid.endswith("@g.us") and JID_OK.match(jid)):
            raise ValueError("Esse link não abriu nenhum grupo (pode ter sido revogado).")
        return jid, lotes._nome_contato(str(g.get("subject") or ""), 80)
    if JID_OK.match(texto):
        return texto, ""
    n = normalizar_numero(texto)
    if 10 <= len(n) <= 15:
        return f"{n}@s.whatsapp.net", ""
    raise ValueError("Cole o link de convite do grupo ou o número com DDD.")


# ---------------------------------------------------------------------------
# SQL (só SELECT; todo valor interpolado passa por regex ou int())
# ---------------------------------------------------------------------------

_SEM = ", ".join(f"'{t}'" for t in sorted(lotes.SEM_CONTEUDO))
# LID (pessoa fora da agenda) -> telefone, pelo remoteJidAlt de qualquer mensagem que trouxe os dois
_MAPA = ('mapa AS (SELECT DISTINCT ON ("key"->>\'remoteJid\') "key"->>\'remoteJid\' AS lid, '
         '"key"->>\'remoteJidAlt\' AS tel FROM public."Message" '
         'WHERE "key"->>\'remoteJid\' LIKE \'%@lid\' AND coalesce("key"->>\'remoteJidAlt\', \'\') LIKE \'%@s.whatsapp.net\' '
         'ORDER BY "key"->>\'remoteJid\', "messageTimestamp" DESC)')
_CONVERSA = ("CASE WHEN m.\"key\"->>'remoteJid' LIKE '%@lid' THEN coalesce(nullif(m.\"key\"->>'remoteJidAlt', ''), "
             "mapa.tel, m.\"key\"->>'remoteJid') ELSE m.\"key\"->>'remoteJid' END")
# miniaturas embutidas pesam KB por mensagem e não são usadas aqui. O `#-` só vale em objeto: a Evolution grava
# `message` como JSON null em alguns documentMessage (09/11/2026, 15:39 e 19:36) e "cannot delete path in scalar"
# derrubava a lista inteira de Conversas ("O banco da Evolution não respondeu") por causa de UMA linha.
_MSG_LEVE = ("(CASE WHEN jsonb_typeof(m.message) = 'object' THEN m.message #- '{imageMessage,jpegThumbnail}' "
             "#- '{videoMessage,jpegThumbnail}' #- '{documentMessage,jpegThumbnail}' "
             "#- '{extendedTextMessage,jpegThumbnail}' #- '{stickerMessage,pngThumbnail}' "
             "#- '{associatedChildMessage,message,imageMessage,jpegThumbnail}' "
             "#- '{associatedChildMessage,message,videoMessage,jpegThumbnail}' "
             "ELSE m.message END)::text")
# Pedido 56: foto ou vídeo de álbum chega embrulhado em associatedChildMessage. Para a tela e para baixar a mídia, o
# tipo que vale é o de dentro (imageMessage, videoMessage).
_TIPO = ("(CASE WHEN m.\"messageType\" = 'associatedChildMessage' "
         "AND jsonb_typeof(m.message->'associatedChildMessage'->'message') = 'object' THEN coalesce("
         "(SELECT k FROM jsonb_object_keys(m.message->'associatedChildMessage'->'message') k "
         "WHERE k LIKE '%Message' ORDER BY k LIMIT 1), m.\"messageType\") ELSE m.\"messageType\" END)")
_SEM_TAB = "translate(coalesce({}, ''), E'\\t\\n\\r', '   ')"
# Pedido 50, rodada 2: a mensagem citada mora na coluna contextInfo (stanzaId = key.id da original). Vai só o que a
# tela usa, sem miniatura; mensagem sem citação devolve NULL (coluna vazia no psql).
_CITADA = ("(CASE WHEN jsonb_typeof(m.\"contextInfo\") = 'object' AND coalesce(m.\"contextInfo\"->>'stanzaId', '') <> '' "
           "THEN jsonb_build_object('id', m.\"contextInfo\"->>'stanzaId', 'de', coalesce(m.\"contextInfo\"->>'participant', ''), "
           "'msg', CASE WHEN jsonb_typeof(m.\"contextInfo\"->'quotedMessage') = 'object' THEN m.\"contextInfo\"->'quotedMessage' "
           "#- '{documentMessage,jpegThumbnail}' "
           "#- '{stickerMessage,pngThumbnail}' ELSE '{}'::jsonb END) END)::text")


def _filtro(desde: int) -> str:
    return (f'm."messageTimestamp" >= {int(desde)} AND m."messageType" NOT IN ({_SEM}) '
            "AND coalesce(m.\"key\"->>'remoteJid', '') <> '' "
            "AND m.\"key\"->>'remoteJid' NOT LIKE '%@broadcast' AND m.\"key\"->>'remoteJid' NOT LIKE '%@newsletter'")


def _de_mim(key: dict) -> bool:
    return key.get("fromMe") in (True, "true", 1)


def sql_lista(desde: int, limite: int = 300) -> str:
    """Uma linha por conversa: a última mensagem, não lidas (como o celular conta) e o nome que o contato tem."""
    return (
        f"WITH {_MAPA}, base AS (SELECT m.id, m.\"key\", m.\"pushName\", {_TIPO} AS \"messageType\", {_MSG_LEVE} AS msg, "
        f"m.\"messageTimestamp\" AS ts, {_STATUS} AS status, {_CONVERSA} AS conversa, {_APAGADA} AS apagada "
        f"FROM public.\"Message\" m LEFT JOIN mapa ON mapa.lid = m.\"key\"->>'remoteJid' {_APAGADA_JOIN} {_ENTREGA_JOIN} WHERE {_filtro(desde)}), "
        "ult AS (SELECT DISTINCT ON (conversa) * FROM base ORDER BY conversa, ts DESC, id DESC), "
        "cont AS (SELECT conversa, count(*) FILTER (WHERE coalesce(\"key\"->>'fromMe', 'false') <> 'true' "
        "AND status = 'DELIVERY_ACK') AS nao_lidas, "
        # pedido 60: a hora da não lida mais recente, para a bolinha sumir quando a conversa já foi aberta no Greg
        "coalesce(max(ts) FILTER (WHERE coalesce(\"key\"->>'fromMe', 'false') <> 'true' AND status = 'DELIVERY_ACK'), 0) AS nl_ts "
        "FROM base GROUP BY conversa) "
        f"SELECT u.conversa, u.ts, u.id, u.\"key\"::text, {_SEM_TAB.format('u.\"pushName\"')}, u.\"messageType\", u.msg, "
        f"c.nao_lidas, {_SEM_TAB.format('p.push')}, {_SEM_TAB.format('coalesce(ag.nome, ct.\"pushName\")')}, coalesce(ct.\"profilePicUrl\", ''), u.apagada, c.nl_ts "
        "FROM ult u JOIN cont c USING (conversa) "
        "LEFT JOIN LATERAL (SELECT b.\"pushName\" AS push FROM base b WHERE b.conversa = u.conversa "
        "AND coalesce(b.\"key\"->>'fromMe', 'false') <> 'true' AND coalesce(b.\"pushName\", '') <> '' "
        "ORDER BY b.ts DESC LIMIT 1) p ON true "
        "LEFT JOIN LATERAL (SELECT x.\"pushName\", x.\"profilePicUrl\" FROM public.\"Contact\" x "
        "WHERE x.\"remoteJid\" = u.conversa ORDER BY x.\"updatedAt\" DESC NULLS LAST LIMIT 1) ct ON true "
        # pedido 54: o nome SALVO na agenda (infra/sql/006) vale antes do que está em "Contact", que a Evolution troca
        # pelo nome do perfil a cada mensagem; a agenda pode estar no telefone ou no LID da conversa
        "LEFT JOIN LATERAL (SELECT a.nome FROM vigia.agenda a WHERE a.remote_jid IN (u.conversa, u.\"key\"->>'remoteJid') "
        "ORDER BY (a.remote_jid = u.conversa) DESC LIMIT 1) ag ON true "
        f"ORDER BY u.ts DESC, u.id DESC LIMIT {int(limite)}")


# D-63: mensagem que o remetente apagou (anotada pelo gatilho de infra/sql/004). O painel esconde o conteúdo dela.
_APAGADA_JOIN = "LEFT JOIN vigia.apagadas ap ON ap.message_id = m.id"
# 'DELETED' é o que a Evolution grava quando o próprio dono apaga para todos (com exclusão lógica ligada).
_APAGADA = "(ap.message_id IS NOT NULL OR coalesce(m.status, '') = 'DELETED')::text"


# Pedido 51: a Evolution 2.3.7 não atualiza o status da mensagem PRÓPRIA em "Message" (o UPDATE dela exige !fromMe);
# o avanço (enviada, entregue, lida, ouvida) só passa por "MessageUpdate". O gatilho de infra/sql/005 anota o nível mais
# alto em vigia.entrega e o painel mostra o maior entre os dois.
_ENTREGA_JOIN = "LEFT JOIN vigia.entrega en ON en.message_id = m.id"
_STATUS = ("(CASE WHEN en.nivel IS NOT NULL AND coalesce(m.status, '') IN ('', 'PENDING', 'SERVER_ACK', 'DELIVERY_ACK', 'READ', 'PLAYED') "
           "AND en.nivel > (CASE coalesce(m.status, '') WHEN 'SERVER_ACK' THEN 1 WHEN 'DELIVERY_ACK' THEN 2 WHEN 'READ' THEN 3 "
           "WHEN 'PLAYED' THEN 4 ELSE 0 END) THEN en.status ELSE coalesce(m.status, '') END)")


def sql_mensagens(conversa: str, desde: int, limite: int = 200, limite_reacoes: int = 1000) -> str:
    """As mensagens da conversa e, na MESMA consulta (cada psql é um subprocesso), as reações dela (pedido 50,
    rodada 2): linhas `reactionMessage` com {"alvo": key.id da mensagem, "emoji"} no lugar do conteúdo. As reações
    ficam fora de `_filtro`, que descarta esse tipo."""
    if not JID_OK.match(conversa or ""):
        raise ValueError("conversa fora do padrão")
    de = (f"FROM public.\"Message\" m LEFT JOIN mapa ON mapa.lid = m.\"key\"->>'remoteJid' "
          f"{_APAGADA_JOIN} {_ENTREGA_JOIN} ")
    dona = f"({_CONVERSA} = '{conversa}' OR m.\"key\"->>'remoteJidAlt' = '{conversa}')"
    push = _SEM_TAB.format('m."pushName"')
    return (
        f"WITH {_MAPA} (SELECT m.id, m.\"key\"::text, {push}, {_TIPO}::text, "
        f"{_MSG_LEVE}, m.\"messageTimestamp\", {_STATUS}::text, {_CITADA}, {_APAGADA} "
        f"{de}WHERE {_filtro(desde)} AND {dona} "
        f'ORDER BY m."messageTimestamp" DESC, m.id DESC LIMIT {int(limite)}) '
        f"UNION ALL (SELECT m.id, m.\"key\"::text, {push}, 'reactionMessage', "
        "jsonb_build_object('alvo', coalesce(m.message->'reactionMessage'->'key'->>'id', ''), "
        "'emoji', coalesce(m.message->'reactionMessage'->>'text', ''))::text, m.\"messageTimestamp\", '', NULL, 'false' "
        f"{de}WHERE m.\"messageTimestamp\" >= {int(desde)} AND m.\"messageType\" = 'reactionMessage' AND {dona} "
        f'ORDER BY m."messageTimestamp" DESC, m.id DESC LIMIT {int(limite_reacoes)})')


def sql_pulso(desde: int) -> str:
    """Pedido 52: uma linha barata que muda quando qualquer coisa que a tela de Conversas mostra muda no banco
    (mensagem ou reação nova, lida no celular, edição, exclusão, avanço de entrega). Sem jsonb e sem conteúdo."""
    return ("SELECT coalesce(max(m.\"messageTimestamp\"), 0), count(*), "
            "count(*) FILTER (WHERE m.status = 'DELIVERY_ACK'), count(*) FILTER (WHERE m.status IN ('EDITED', 'DELETED')), "
            "(SELECT count(*) FROM vigia.apagadas), (SELECT count(*) FROM vigia.entrega), "
            "(SELECT coalesce(floor(extract(epoch FROM max(atualizado_em)) * 1000), 0)::bigint FROM vigia.entrega), "
            # muda quando uma mensagem sem confirmação passa de 1 minuto: a tela troca o relógio pelo "enviada"
            "count(*) FILTER (WHERE m.status = 'PENDING' AND m.\"messageTimestamp\" < extract(epoch FROM now()) - 60) "
            f"FROM public.\"Message\" m WHERE m.\"messageTimestamp\" >= {int(desde)}")


def pulso(psql, *, desde: int) -> str:
    linhas = psql(sql_pulso(desde))
    return ".".join(str(lotes._int(x)) for x in linhas[0]) if linhas else ""


def sql_chave(msg_id: str, desde: int) -> str:
    if not MSG_ID_OK.match(msg_id or ""):
        raise ValueError("id de mensagem fora do padrão")
    return (f"WITH {_MAPA} SELECT m.\"key\"::text, {_TIPO}, {_CONVERSA}, m.\"messageTimestamp\", {_MSG_LEVE}, {_APAGADA} "
            f"FROM public.\"Message\" m "
            f"LEFT JOIN mapa ON mapa.lid = m.\"key\"->>'remoteJid' {_APAGADA_JOIN} "
            f"WHERE m.id = '{msg_id}' AND m.\"messageTimestamp\" >= {int(desde)} LIMIT 1")


# Agenda que a Evolution sincronizou (tabela Contact), para /bloqueios (pedido 29, D-46): a aba é o sufixo do jid.
TIPOS_AGENDA = {"grupos": "@g.us", "contatos": "@s.whatsapp.net", "outros": "@lid"}


def sql_agenda(tipo: str, limite: int = 5000) -> str:
    """Um por jid (o mais recente, se repetido), nome sem tabulação (o psql separa colunas por tab), nomeados primeiro.
    Em grupo, pushName é o assunto; em @lid, o nome que a pessoa exibe. Só SELECT; nenhum valor vem de fora."""
    if tipo not in TIPOS_AGENDA:
        raise ValueError("tipo de agenda fora do padrão")
    nome = _SEM_TAB.format('c."pushName"')
    return ("SELECT jid, nome, foto FROM (SELECT DISTINCT ON (c.\"remoteJid\") c.\"remoteJid\" AS jid, "
            f"{nome} AS nome, (c.\"profilePicUrl\" IS NOT NULL) AS foto "
            f"FROM public.\"Contact\" c WHERE c.\"remoteJid\" LIKE '%{TIPOS_AGENDA[tipo]}' "
            "ORDER BY c.\"remoteJid\", c.\"updatedAt\" DESC NULLS LAST) t "
            f"ORDER BY (nome = ''), lower(nome), jid LIMIT {int(limite)}")


def sql_nomes_agenda(jids) -> str:
    """Nome da agenda de jids já conhecidos (os bloqueados, que podem estar em outra aba). Só jid no padrão JID_OK entra
    (sem aspas por construção), no máximo 500."""
    ok = sorted({j for j in jids if JID_OK.match(j or "")})[:500]
    if not ok:
        raise ValueError("nenhum jid no padrão")
    nome = _SEM_TAB.format('coalesce(ag.nome, c."pushName")')
    lista = ", ".join(f"'{j}'" for j in ok)
    return (f"SELECT DISTINCT ON (c.\"remoteJid\") c.\"remoteJid\", {nome} FROM public.\"Contact\" c "
            "LEFT JOIN vigia.agenda ag ON ag.remote_jid = c.\"remoteJid\" "   # pedido 54: nome salvo antes do perfil
            f"WHERE c.\"remoteJid\" IN ({lista}) ORDER BY c.\"remoteJid\", c.\"updatedAt\" DESC NULLS LAST")


def sql_contagem_agenda() -> str:
    """Quantos jids distintos por aba, numa linha só (grupos, contatos, outros), na ordem de TIPOS_AGENDA."""
    partes = ", ".join(f"count(DISTINCT \"remoteJid\") FILTER (WHERE \"remoteJid\" LIKE '%{s}')" for s in TIPOS_AGENDA.values())
    return f"SELECT {partes} FROM public.\"Contact\""


_json = lotes._json      # mesma regra do coletor: JSON inválido ou de outro tipo vira o padrão


def _col(cols, i, padrao=""):
    return cols[i] if len(cols) > i else padrao


def listar(psql, *, desde: int, limite: int = 300) -> list[dict]:
    """Conversas com mensagem desde o pareamento, da mais recente para a mais antiga."""
    saida = []
    for cols in psql(sql_lista(desde, limite)):
        jid = _col(cols, 0)
        if not JID_OK.match(jid):
            continue
        saida.append({
            "jid": jid, "ts": lotes._ts_segundos(lotes._int(_col(cols, 1))), "id": _col(cols, 2),
            "key": _json(_col(cols, 3), {}), "push_ultima": _col(cols, 4), "tipo_msg": _col(cols, 5),
            "message": _json(_col(cols, 6), {}), "nao_lidas": lotes._int(_col(cols, 7)),
            "push": _col(cols, 8), "contato": _col(cols, 9), "foto_url": _col(cols, 10), "apagada": _col(cols, 11) == "true",
            "nao_lida_ts": lotes._ts_segundos(lotes._int(_col(cols, 12))) if lotes._int(_col(cols, 12)) else 0,
        })
    return saida


def fio(psql, conversa: str, *, desde: int, limite: int = 200) -> tuple[list[dict], dict]:
    """(mensagens em ordem cronológica, reações) da conversa, desde o pareamento, numa consulta só.
    Reações: {key.id da mensagem: [{"emoji", "quem" ("eu" ou jid), "push"}]}; vale a última de cada pessoa em cada
    mensagem e reação vazia é a pessoa tirando a dela."""
    linhas, brutas = [], []
    for cols in psql(sql_mensagens(conversa, desde, limite)):
        if not MSG_ID_OK.match(_col(cols, 0)):
            continue
        ts = lotes._ts_segundos(lotes._int(_col(cols, 5)))
        if _col(cols, 3) == "reactionMessage":
            brutas.append((ts, cols[0], _json(_col(cols, 1), {}), _json(_col(cols, 4), {}), _col(cols, 2)))
            continue
        citada = _json(_col(cols, 7), {})
        if not (isinstance(citada.get("id"), str) and citada["id"]):
            citada = None
        message = _json(_col(cols, 4), {})
        linhas.append({"id": cols[0], "key": _json(_col(cols, 1), {}), "push": _col(cols, 2),
                       "tipo_msg": _col(cols, 3), "message": message, "encaminhada": encaminhada(message),
                       "ts": ts, "status": _col(cols, 6), "citada": citada, "apagada": _col(cols, 8) == "true"})
    ultima: dict = {}
    for _ts, _id, key, r, push in sorted(brutas, key=lambda b: (b[0], b[1])):      # da mais antiga para a mais nova
        alvo = r.get("alvo")
        if not isinstance(alvo, str) or not alvo:
            continue
        quem = "eu" if _de_mim(key) else (remetente(key, conversa) or "?")
        ultima[(alvo, quem)] = (_texto(r.get("emoji"), 16), push)
    reacoes: dict = {}
    for (alvo, quem), (emoji, push) in ultima.items():
        if emoji:
            reacoes.setdefault(alvo, []).append({"emoji": emoji, "quem": quem, "push": push})
    return sorted(linhas, key=lambda m: (m["ts"], m["id"])), reacoes


def encaminhada(message: dict) -> bool:
    """A mensagem chegou (ou saiu) com o selo de encaminhada do WhatsApp: contextInfo.isForwarded no conteúdo dela."""
    for v in (message or {}).values():
        ci = v.get("contextInfo") if isinstance(v, dict) else None
        if isinstance(ci, dict) and ci.get("isForwarded") in (True, "true"):
            return True
    return False


def mensagens(psql, conversa: str, *, desde: int, limite: int = 200) -> list[dict]:
    """Mensagens da conversa (desde o pareamento) em ordem cronológica."""
    return fio(psql, conversa, desde=desde, limite=limite)[0]


def chave_da_mensagem(psql, msg_id: str, *, desde: int) -> tuple[dict, str, dict] | None:
    """(key, tipo, message). O terceiro item existe para a mídia de álbum (pedido 56), que precisa da mensagem de dentro."""
    linhas = psql(sql_chave(msg_id, desde))
    if not linhas or _col(linhas[0], 5) == "true":      # apagada pelo remetente: nem a mídia é servida (D-63)
        return None
    return _json(_col(linhas[0], 0), {}), _col(linhas[0], 1), _json(_col(linhas[0], 4), {})


class Vistas:
    """Pedido 60: até que hora cada conversa já foi aberta no Greg ({jid: segundos}, dados/vistas.json). A bolinha de
    não lida some para o que chegou até essa hora; o WhatsApp não é avisado (o celular continua contando do jeito dele)."""

    def __init__(self, arquivo: Path):
        self.arquivo = Path(arquivo)
        self._trava = threading.Lock()
        self._cache: dict | None = None

    def todas(self) -> dict:
        with self._trava:
            if self._cache is None:
                try:
                    d = json.loads(self.arquivo.read_text(encoding="utf-8"))
                    self._cache = {j: int(t) for j, t in d.items() if JID_OK.match(j) and isinstance(t, (int, float))} \
                        if isinstance(d, dict) else {}
                except (OSError, ValueError):
                    self._cache = {}
            return dict(self._cache)

    def marcar(self, jid: str, ate: int) -> bool:
        """Anota que a conversa foi vista até `ate`. True se mudou (nunca volta no tempo)."""
        if not JID_OK.match(jid or "") or not isinstance(ate, int) or ate <= 0:
            return False
        atual = self.todas()
        if atual.get(jid, 0) >= ate:
            return False
        with self._trava:
            self._cache[jid] = ate
            if len(self._cache) > 5000:           # só as 5000 conversas vistas por último ficam guardadas
                self._cache = dict(sorted(self._cache.items(), key=lambda x: x[1])[-5000:])
            try:
                self.arquivo.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.arquivo.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._cache), encoding="utf-8")
                os.chmod(tmp, 0o600)
                tmp.replace(self.arquivo)
            except OSError as e:
                log.warning("vistas não gravadas: %s", causa(e))
        return True


_TIPOS_MIDIA = ("imageMessage", "videoMessage", "documentMessage", "documentWithCaptionMessage")


def sql_midias(conversa: str, desde: int, limite: int = 300) -> str:
    """Pedido 57: fotos, vídeos e documentos da conversa, mais recentes primeiro (só id e tipo; nada de conteúdo)."""
    if not JID_OK.match(conversa or ""):
        raise ValueError("conversa fora do padrão")
    tipos = ", ".join(f"'{t}'" for t in _TIPOS_MIDIA)
    return (f"WITH {_MAPA} SELECT m.id, {_TIPO}::text, {_APAGADA} FROM public.\"Message\" m "
            f"LEFT JOIN mapa ON mapa.lid = m.\"key\"->>'remoteJid' {_APAGADA_JOIN} "
            f"WHERE {_filtro(desde)} AND ({_CONVERSA} = '{conversa}' OR m.\"key\"->>'remoteJidAlt' = '{conversa}') "
            f"AND {_TIPO} IN ({tipos}) ORDER BY m.\"messageTimestamp\" DESC, m.id DESC LIMIT {int(limite)}")


def midias(psql, conversa: str, *, desde: int, limite: int = 300) -> dict:
    """{"fotos": [ids], "videos": n, "documentos": n, "cheio": bool}; mensagem apagada pelo remetente não conta (D-63)."""
    linhas = psql(sql_midias(conversa, desde, limite))
    saida = {"fotos": [], "videos": 0, "documentos": 0, "cheio": len(linhas) >= limite}
    for cols in linhas:
        if _col(cols, 2) == "true" or not MSG_ID_OK.match(_col(cols, 0)):
            continue
        tipo = _col(cols, 1)
        if tipo == "imageMessage":
            saida["fotos"].append(_col(cols, 0))
        elif tipo == "videoMessage":
            saida["videos"] += 1
        else:
            saida["documentos"] += 1
    return saida


def _curto(v, n: int) -> str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(v or ""))[:n].strip() if isinstance(v, (str, int)) else ""


class Perfis:
    """Pedido 57: o que o WhatsApp diz de um contato ou grupo (recado, conta comercial, descrição, participantes),
    pedido só quando o dono abre as informações da conversa. Guarda por `ttl` em dados/perfis.json para não repetir a
    pergunta, respeita um teto por hora e nunca consulta número escondido (@lid)."""

    def __init__(self, arquivo: Path, evo, *, ttl: int = 86400, teto_hora: int = 60, refazer_falha_s: int = 600,
                 relogio=time.time):
        self.arquivo, self.evo, self.ttl, self.teto_hora = Path(arquivo), evo, ttl, teto_hora
        self.refazer_falha_s, self.relogio = refazer_falha_s, relogio
        self._trava = threading.Lock()
        self._feitas: list[float] = []
        self._falhas: dict[str, float] = {}

    def _ler(self) -> dict:
        try:
            d = json.loads(self.arquivo.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def obter(self, jid: str) -> tuple[dict, str]:
        """(dados, estado). estado: "novo" (acabou de perguntar), "guardado", "teto", "indisponivel" ou "sem" (não se
        pergunta: @lid, jid fora do padrão ou sem Evolution). Com falha, devolve o que estava guardado, se havia."""
        if self.evo is None or not JID_OK.match(jid or "") or jid.endswith("@lid"):
            return {}, "sem"
        with self._trava:
            agora = self.relogio()
            todos = self._ler()
            tem = todos.get(jid) if isinstance(todos.get(jid), dict) else {}
            if tem and agora - float(tem.get("em") or 0) <= self.ttl:
                return tem, "guardado"
            if agora - self._falhas.get(jid, -1e12) < self.refazer_falha_s:
                return tem, "indisponivel"
            self._feitas = [x for x in self._feitas if agora - x < 3600]
            if len(self._feitas) >= self.teto_hora:
                return tem, "teto"
            self._feitas.append(agora)
            try:
                novo = self._grupo(jid) if jid.endswith("@g.us") else self._pessoa(jid)
            except Exception as e:  # a tela de informações nunca derruba a conversa; só registra o tipo
                log.warning("perfil não consultado: %s", causa(e))
                novo = None
            if not novo:
                self._falhas[jid] = agora
                return tem, "indisponivel"
            novo["em"] = agora
            todos[jid] = novo
            limite = agora - 30 * 86400            # perfil não reaberto em 30 dias sai do arquivo
            todos = {j: p for j, p in todos.items() if isinstance(p, dict) and float(p.get("em") or 0) >= limite}
            try:
                self.arquivo.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.arquivo.with_suffix(".tmp")
                tmp.write_text(json.dumps(todos, ensure_ascii=False), encoding="utf-8")
                os.chmod(tmp, 0o600)
                tmp.replace(self.arquivo)
            except OSError as e:
                log.warning("perfis não gravados: %s", causa(e))
            return novo, "novo"

    def _pessoa(self, jid: str) -> dict | None:
        r = self.evo.perfil(jid)
        if not r:
            return None
        st = r.get("status") if isinstance(r.get("status"), dict) else {}
        d = {"tipo": "pessoa", "nome": _curto(r.get("name"), 80), "recado": _curto(st.get("status"), 300),
             "recado_em": _curto(st.get("setAt"), 40), "comercial": r.get("isBusiness") is True,
             "descricao": _curto(r.get("description"), 1200), "categoria": "", "sites": [], "horario": []}
        if d["comercial"]:
            b = self.evo.perfil_comercial(jid)
            d["descricao"] = _curto(b.get("description"), 1200) or d["descricao"]
            d["categoria"] = _curto(b.get("category"), 80)
            sites = b.get("website") if isinstance(b.get("website"), list) else []
            d["sites"] = [x for x in (_curto(w, 200) for w in sites[:4] if isinstance(w, str)) if x]
            horas = b.get("business_hours") if isinstance(b.get("business_hours"), dict) else {}
            cfg = horas.get("business_config") if isinstance(horas.get("business_config"), list) else []
            d["horario"] = [{"dia": _curto(h.get("day_of_week"), 12), "modo": _curto(h.get("mode"), 24),
                             "abre": _curto(h.get("open_time"), 8), "fecha": _curto(h.get("close_time"), 8)}
                            for h in cfg[:14] if isinstance(h, dict)]
        return d

    def _grupo(self, jid: str) -> dict | None:
        r = self.evo.grupo(jid)
        if not r:
            return None
        partes = r.get("participants") if isinstance(r.get("participants"), list) else []
        gente = []
        for x in partes[:1100]:
            if not isinstance(x, dict):
                continue
            tel, pid = str(x.get("phoneNumber") or ""), str(x.get("id") or "")
            pj = tel if JID_OK.match(tel) else (pid if JID_OK.match(pid) else "")
            if pj:
                gente.append({"jid": pj, "admin": bool(x.get("admin"))})
        return {"tipo": "grupo", "assunto": _curto(r.get("subject"), 120), "descricao": _curto(r.get("desc"), 2500),
                "criado": lotes._int(r.get("creation")), "membros": lotes._int(r.get("size")) or len(gente),
                "participantes": gente, "so_admin_fala": r.get("announce") is True,
                "comunidade": r.get("isCommunity") is True}


def sql_capa(msg_id: str) -> str:
    if not MSG_ID_OK.match(msg_id or ""):
        raise ValueError("id de mensagem fora do padrão")
    return ("SELECT coalesce(m.message->'videoMessage'->>'jpegThumbnail', "
            "m.message#>>'{associatedChildMessage,message,videoMessage,jpegThumbnail}', '') "
            f"FROM public.\"Message\" m WHERE m.id = '{msg_id}' LIMIT 1")


def capa_do_video(psql, msg_id: str) -> bytes | None:
    """Pedido 59: a miniatura JPEG que o WhatsApp manda dentro da mensagem de vídeo (poucos KB, já no banco). Quem
    chama já conferiu com chave_da_mensagem que a mensagem pode ser mostrada."""
    linhas = psql(sql_capa(msg_id))
    b64 = _col(linhas[0], 0) if linhas else ""
    if not b64 or len(b64) > 200_000:
        return None
    try:
        dados = base64.b64decode(b64, validate=True)
    except (ValueError, TypeError):
        return None
    return dados if dados[:3] == b"\xff\xd8\xff" else None


def mensagem_na_conversa(psql, msg_id: str, conversa: str, *, desde: int) -> dict | None:
    """{"key", "tipo_msg", "ts", "message"} de uma mensagem, só se ela é desta conversa (para citar, reagir, editar ou
    encaminhar: o id nunca aponta para outra conversa)."""
    linhas = psql(sql_chave(msg_id, desde))
    if not linhas:
        return None
    cols = linhas[0]
    key = _json(_col(cols, 0), {})
    donas = {_col(cols, 2), key.get("remoteJid"), key.get("remoteJidAlt")}
    if not key.get("id") or conversa not in donas or _col(cols, 5) == "true":      # apagada não se cita nem encaminha
        return None
    return {"key": key, "tipo_msg": _col(cols, 1), "ts": lotes._ts_segundos(lotes._int(_col(cols, 3))),
            "message": _json(_col(cols, 4), {})}


def chave_na_conversa(psql, msg_id: str, conversa: str, *, desde: int) -> dict | None:
    achada = mensagem_na_conversa(psql, msg_id, conversa, desde=desde)
    return achada["key"] if achada else None


def agenda(psql, tipo: str, limite: int = 5000) -> list[dict]:
    """Linhas da agenda de um tipo: {jid, nome (como veio, '' se não tem), foto (bool)}. Falha do banco SOBE."""
    saida = []
    for cols in psql(sql_agenda(tipo, limite)):
        jid = _col(cols, 0)
        if JID_OK.match(jid):
            saida.append({"jid": jid, "nome": _col(cols, 1), "foto": _col(cols, 2) in ("t", "true", "1")})
    return saida


def nomes_agenda(psql, jids) -> dict:
    """{jid: nome} dos jids pedidos que têm nome na agenda. Sem jid válido: {} sem consultar. Falha do banco SOBE."""
    if not any(JID_OK.match(j or "") for j in jids):
        return {}
    saida = {}
    for cols in psql(sql_nomes_agenda(jids)):
        if _col(cols, 1):
            saida[_col(cols, 0)] = _col(cols, 1)
    return saida


def contagem_agenda(psql) -> dict:
    """{'grupos': n, 'contatos': n, 'outros': n} de jids distintos na agenda."""
    linhas = psql(sql_contagem_agenda())
    cols = linhas[0] if linhas else []
    return {t: lotes._int(_col(cols, i)) for i, t in enumerate(TIPOS_AGENDA)}


# ---------------------------------------------------------------------------
# Nome, número e conteúdo
# ---------------------------------------------------------------------------

def tipo_de(jid: str) -> str:
    if jid.endswith("@g.us"):
        return "grupo"
    if jid.endswith("@lid"):
        return "lid"
    return "pessoa"


def digitos(jid_ou_numero: str) -> str:
    return re.sub(r"\D", "", str(jid_ou_numero or "").split("@", 1)[0].split(":", 1)[0])


def numero_formatado(jid: str) -> str:
    """+55 92 99123-4567 (celular), +55 92 3234-5678 (fixo), +<dígitos> fora do Brasil."""
    d = digitos(jid)
    if d.startswith("55") and len(d) == 13:
        return f"+55 {d[2:4]} {d[4:9]}-{d[9:]}"
    if d.startswith("55") and len(d) == 12:
        return f"+55 {d[2:4]} {d[4:8]}-{d[8:]}"
    return "+" + d if d else ""


def _bom(nome: str) -> str:
    """Nome aproveitável: não vazio e que não seja só número (regra do Chatwoot: um telefone no lugar do nome não é nome)."""
    n = lotes._nome_contato(nome, 60)
    if not n:
        return ""
    so_num = re.sub(r"[\s+()\-.]", "", n)
    if so_num.isdigit():
        return ""
    return n


def nome_da_conversa(jid: str, *, apelido: str = "", contato: str = "", push: str = "", grupo: str = "") -> str:
    """Regra única: apelido do Greg > nome do grupo / nome salvo > pushName > número. LID sem telefone: '~pushName'."""
    if apelido:
        return apelido
    t = tipo_de(jid)
    if t == "grupo":
        return lotes._nome_contato(grupo, 80) or "Grupo"
    nome = _bom(contato) or _bom(push)
    if t == "lid":
        return ("~" + nome) if nome else "Contato sem número"
    return nome or numero_formatado(jid)


def remetente(key: dict, conversa: str) -> str:
    """jid de quem mandou: em grupo é o participante (telefone alternativo se for LID); fora de grupo, a conversa."""
    if not conversa.endswith("@g.us"):
        return conversa
    part = key.get("participant") or ""
    alt = key.get("participantAlt") or ""
    if part.endswith("@lid") and alt:
        return alt
    return part or alt


def nome_do_remetente(jid: str, push: str, apelidos: dict) -> str:
    if jid in apelidos:
        return apelidos[jid]
    nome = _bom(push)
    if jid.endswith("@lid"):
        return ("~" + nome) if nome else "Participante"
    return nome or numero_formatado(jid) or "Participante"


def iniciais(nome: str) -> str:
    partes = [p for p in re.split(r"[\s·\-_.]+", nome.lstrip("~+")) if p and not p.isdigit()]
    if not partes:
        return "#"
    if len(partes) == 1:
        return partes[0][:2].upper()
    return (partes[0][0] + partes[1][0]).upper()


def _texto(txt, limite: int = LIMITE_TEXTO) -> str:
    """Texto para mostrar: mantém quebras de linha, tira controle e invisíveis (bidi), corta no limite."""
    txt = "".join(c for c in str(txt or "") if (c >= " " or c in "\n\t")
                  and not (0x200B <= ord(c) <= 0x200F or 0x202A <= ord(c) <= 0x202E or 0x2066 <= ord(c) <= 0x2069))
    txt = txt.strip()
    return txt if len(txt) <= limite else txt[:limite] + "…"


def _coord(v, teto: int) -> str:
    """Coordenada como texto com 6 casas, ou '' se não é número dentro da faixa."""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return ""
    return f"{n:.6f}" if -teto <= n <= teto else ""


_TEL_VCARD = re.compile(r"^(?:item\d+\.)?TEL[^:\n]*:(.+)$", re.I | re.M)


def _contato(c: dict) -> dict:
    """{"nome", "numeros"} de um contato compartilhado: o nome exibido e os telefones do vCard (no máximo 4)."""
    numeros = []
    for bruto in _TEL_VCARD.findall(str(c.get("vcard") or ""))[:4]:
        n = _texto(bruto, 30)
        if n and n not in numeros and re.fullmatch(r"[0-9+()\-. ]{5,30}", n):
            numeros.append(n)
    return {"nome": _texto(c.get("displayName"), 80) or "Contato", "numeros": numeros}


def conteudo(message: dict, tipo_msg: str = "") -> dict:
    """{"tipo": texto|imagem|audio|video|documento|figurinha|local|contato|enquete|outro, "texto", "extra"} com quebras
    de linha preservadas. local: extra = "lat,lon"; contato: "contatos"; enquete: "opcoes" (pedido 50, rodada 3)."""
    m = lotes._desembrulhar(message or {})
    if isinstance(m.get("conversation"), str):
        return {"tipo": "texto", "texto": _texto(m["conversation"]), "extra": ""}
    if isinstance(m.get("extendedTextMessage"), dict):
        return {"tipo": "texto", "texto": _texto(m["extendedTextMessage"].get("text")), "extra": ""}
    for chave in ("locationMessage", "liveLocationMessage"):
        loc = m.get(chave)
        if isinstance(loc, dict):
            lat, lon = _coord(loc.get("degreesLatitude"), 90), _coord(loc.get("degreesLongitude"), 180)
            nome = " · ".join(x for x in (_texto(loc.get("name"), 80), _texto(loc.get("address"), 160)) if x)
            return {"tipo": "local", "texto": nome, "extra": f"{lat},{lon}" if lat and lon else "",
                    "vivo": chave == "liveLocationMessage"}
    if isinstance(m.get("contactMessage"), dict):
        return {"tipo": "contato", "texto": "", "extra": "", "contatos": [_contato(m["contactMessage"])]}
    if isinstance(m.get("contactsArrayMessage"), dict):
        lista = [x for x in (m["contactsArrayMessage"].get("contacts") or []) if isinstance(x, dict)][:20]
        return {"tipo": "contato", "texto": "", "extra": "", "contatos": [_contato(x) for x in lista]}
    for chave in ("pollCreationMessage", "pollCreationMessageV2", "pollCreationMessageV3"):
        enq = m.get(chave)
        if isinstance(enq, dict):
            opcoes = [_texto(o.get("optionName"), 100) for o in (enq.get("options") or []) if isinstance(o, dict)][:12]
            return {"tipo": "enquete", "texto": _texto(enq.get("name"), 300), "extra": "", "opcoes": [o for o in opcoes if o]}
    pm = m.get("productMessage")
    if isinstance(pm, dict) and isinstance(pm.get("product"), dict):   # pedido 57: cartão de produto, como no WhatsApp
        pr = pm["product"]
        titulo = _texto(pr.get("title"), 200)
        if titulo:
            return {"tipo": "produto", "texto": "", "extra": "", "titulo": titulo,
                    "preco": lotes._preco(pr.get("priceAmount1000"), pr.get("currencyCode")),
                    "descricao": _texto(pr.get("description")), "foto": lotes.interna_de_album(m) is not None}
    partes = lotes.partes_estruturadas(m)
    if partes:   # pedido 54: catálogo, modelo, lista e botões viram texto legível, com as quebras de linha
        return {"tipo": "texto", "texto": _texto("\n".join(partes)), "extra": ""}
    d = lotes.descrever({"message": message, "messageType": tipo_msg})
    for chave in ("imageMessage", "videoMessage", "documentMessage"):
        if isinstance(m.get(chave), dict):
            d["texto"] = _texto(m[chave].get("caption"))
    return d


APAGADA = "Mensagem apagada"
NAO_ABRE = "Mensagem que só abre no celular"


def previa(c: dict) -> str:
    """Uma linha para a lista."""
    if c["tipo"] == "texto":
        t = " ".join(c["texto"].split())
    elif c["tipo"] == "imagem":
        t = "Foto" + (": " + " ".join(c["texto"].split()) if c["texto"] else "")
    elif c["tipo"] == "audio":
        t = f"Áudio · {c['extra']}"
    elif c["tipo"] == "video":
        t = f"Vídeo · {c['extra']}"
    elif c["tipo"] == "documento":
        t = "Documento" + (f" · {c['extra']}" if c["extra"] else "")
    elif c["tipo"] == "figurinha":
        t = "Figurinha"
    elif c["tipo"] == "local":
        t = ("Localização ao vivo" if c.get("vivo") else "Localização") + (f" · {c['texto']}" if c["texto"] else "")
    elif c["tipo"] == "contato":
        t = "Contato · " + ", ".join(x["nome"] for x in c.get("contatos") or [])
    elif c["tipo"] == "enquete":
        t = "Enquete" + (f" · {c['texto']}" if c["texto"] else "")
    elif c["tipo"] == "produto":
        t = "Produto · " + c["titulo"] + (f" · {c['preco']}" if c.get("preco") else "")
    else:
        t = NAO_ABRE   # pedido 57: nunca o nome técnico do tipo
    return t[:140]


def normalizar_numero(bruto: str) -> str:
    """Digitado pelo dono -> dígitos com DDI: DDD + número (10 ou 11 dígitos) ganha 55; '+DDI…' fica como veio."""
    txt = str(bruto or "").strip()
    d = re.sub(r"\D", "", txt)
    if not d:
        return ""
    if txt.startswith("+") or txt.startswith("00"):
        return d[2:] if txt.startswith("00") else d
    if len(d) in (10, 11):
        return "55" + d
    return d


# ---------------------------------------------------------------------------
# Grupos: nome buscado UM grupo por vez, com teto (D-40)
# ---------------------------------------------------------------------------

class Grupos:
    """Nome, foto e tamanho dos grupos que APARECEM na lista, um por vez (`/group/findGroupInfos`), cache em
    dados/grupos.json por 7 dias. Por que não fetchAllGroups: no Baileys da Evolution 2.3.7 ele emite `groups.update`
    com todos os grupos e a Evolution consulta cada um; em 06/11 isso deu ~243 consultas por chamada e o WhatsApp
    respondeu rate-overlimit. Aqui: pausa entre consultas, teto por hora, falha só volta a tentar depois de 6 h, e a
    página nunca espera (a busca roda numa thread; o nome aparece na atualização seguinte)."""

    def __init__(self, arquivo: Path, evo, *, ttl: int = 7 * 86400, pausa: float = 4.0, teto_hora: int = 30,
                 refazer_falha_s: int = 6 * 3600, relogio=time.time, dormir=time.sleep):
        self.arquivo = Path(arquivo)
        self.evo = evo
        self.ttl = ttl
        self.pausa = pausa
        self.teto_hora = teto_hora
        self.refazer_falha_s = refazer_falha_s
        self.relogio = relogio
        self.dormir = dormir
        self._trava = threading.Lock()
        self._rodando = False
        self._pendentes: list[str] = []
        self._feitas: list[float] = []
        self._falhas: dict[str, float] = {}

    def _ler(self) -> dict:
        try:
            d = json.loads(self.arquivo.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) and isinstance(d.get("grupos"), dict) else {"grupos": {}}
        except (OSError, ValueError):
            return {"grupos": {}}

    def _precisa(self, jid: str, grupos: dict, agora: float) -> bool:
        if not (jid.endswith("@g.us") and JID_OK.match(jid)):
            return False
        if agora - self._falhas.get(jid, -1e12) < self.refazer_falha_s:
            return False
        g = grupos.get(jid)
        return not g or agora - float(g.get("em") or 0) > self.ttl

    def nomes(self, vistos=(), *, sincrono: bool = False) -> dict:
        """{jid: {"subject", "foto", "membros", "em"}} do cache; pede em segundo plano os que faltam ou venceram."""
        d = self._ler()
        agora = self.relogio()
        faltam = [j for j in dict.fromkeys(vistos) if self._precisa(j, d["grupos"], agora)]
        if faltam:
            if sincrono:
                for j in faltam:
                    self.buscar(j)
                d = self._ler()
            else:
                with self._trava:
                    for j in faltam:
                        if j not in self._pendentes:
                            self._pendentes.append(j)
                    if self._rodando:
                        return d["grupos"]
                    self._rodando = True
                threading.Thread(target=self._rodar, name="vigia-grupos", daemon=True).start()
        return d["grupos"]

    def _rodar(self) -> None:
        try:
            primeira = True
            while True:
                with self._trava:
                    if not self._pendentes:
                        return
                    jid = self._pendentes.pop(0)
                if not primeira:
                    self.dormir(self.pausa)
                primeira = False
                if not self.buscar(jid) and self._no_teto():
                    with self._trava:
                        self._pendentes.clear()  # teto da hora: o resto volta a ser pedido na próxima página
                    return
        except Exception as e:  # nunca derruba o painel; só registra o tipo
            log.warning("grupos não atualizados: %s", causa(e))
        finally:
            with self._trava:
                self._rodando = False

    def _no_teto(self) -> bool:
        agora = self.relogio()
        self._feitas = [x for x in self._feitas if agora - x < 3600]
        return len(self._feitas) >= self.teto_hora

    def buscar(self, jid: str) -> bool:
        """Uma consulta (respeita o teto). True se gravou o nome."""
        if self._no_teto():
            return False
        agora = self.relogio()
        self._feitas.append(agora)
        try:
            g = self.evo.grupo(jid)
        except Exception as e:
            log.warning("grupo não consultado: %s", causa(e))
            g = {}
        assunto = lotes._nome_contato(str((g or {}).get("subject") or ""), 80)
        if not assunto:
            self._falhas[jid] = agora
            return False
        with self._trava:
            d = self._ler()
            d["grupos"][jid] = {"subject": assunto, "foto": str(g.get("pictureUrl") or ""),
                                "membros": lotes._int(g.get("size")), "em": int(agora)}
            _gravar_json(self.arquivo, d)
        return True

    def lembrar(self, jid: str, assunto: str) -> None:
        """Grava um nome já conhecido (ex.: o do link de convite) sem consultar o WhatsApp."""
        assunto = lotes._nome_contato(str(assunto or ""), 80)
        if not (assunto and jid.endswith("@g.us") and JID_OK.match(jid)):
            return
        with self._trava:
            d = self._ler()
            g = d["grupos"].get(jid) or {}
            d["grupos"][jid] = {"subject": assunto, "foto": g.get("foto", ""), "membros": g.get("membros", 0),
                                "em": int(self.relogio())}
            _gravar_json(self.arquivo, d)


def _gravar_json(arquivo: Path, obj) -> None:
    arquivo.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = arquivo.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, arquivo)


# ---------------------------------------------------------------------------
# Fotos de perfil: guardadas em disco, baixadas de novo só quando mudam
# ---------------------------------------------------------------------------

def _host_ok(url: str) -> bool:
    try:
        p = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return p.scheme == "https" and bool(p.hostname) and (p.hostname == "whatsapp.net"
                                                          or p.hostname.endswith(".whatsapp.net"))


def chave_da_foto(url: str) -> str:
    """Nome do arquivo na URL (sem a query que expira): muda quando a pessoa troca a foto."""
    try:
        return urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1][:120]
    except ValueError:
        return ""


def baixar_foto(url: str, *, limite: int = 2_000_000, timeout: float = 10.0, abridor=None) -> bytes:
    """GET da foto no CDN do WhatsApp. Recusa host fora de *.whatsapp.net, redirect, corpo grande e não-imagem."""
    if not _host_ok(url):
        raise ValueError("host da foto fora de *.whatsapp.net")
    req = urllib.request.Request(url, headers={"User-Agent": "Greg"})
    with (abridor or rede.ABRIDOR).open(req, timeout=timeout) as r:
        dados = r.read(limite + 1)
    if len(dados) > limite:
        raise ValueError("foto acima do limite")
    # Mesma tabela de magic bytes do coletor (lotes._MAGICOS); RIFF só vale se for WEBP (não WAV/AVI).
    if not any(dados.startswith(m) for m, _ in lotes._MAGICOS) or (dados.startswith(b"RIFF") and dados[8:12] != b"WEBP"):
        raise ValueError("conteúdo não é imagem")
    return dados


def tipo_imagem(dados: bytes) -> str:
    if dados.startswith(b"\x89PNG"):
        return "image/png"
    if dados.startswith(b"RIFF") and dados[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


class Avatares:
    """dados/avatares/<sha1>.img + indice.json {jid: {"arq", "chave", "em"}}. Uma fila, um trabalhador, uma foto por vez.
    Sem foto ou falha: a página mostra iniciais. URL expirada (403/404/410): pede URL nova à Evolution uma vez."""

    def __init__(self, pasta: Path, evo=None, *, baixar=baixar_foto, pausa: float = 0.3, relogio=time.time,
                 refazer_falha_s: int = 6 * 3600, refazer_sem_foto_s: int = 24 * 3600, renovar_s: int = 7 * 24 * 3600,
                 teto_hora: int = 120, pausa_pergunta: float = 1.5):
        self.pasta = Path(pasta)
        self.evo = evo
        self.baixar = baixar
        self.pausa = pausa
        self.relogio = relogio
        self.refazer_falha_s = refazer_falha_s
        # pedido 54: quem não tem URL de foto no banco (a Evolution só a grava em alguns eventos) é perguntado ao
        # WhatsApp em segundo plano: sem foto, pergunta de novo em 24 h; com foto, confere a cada 7 dias; no máximo
        # 120 perguntas por hora, uma por vez.
        self.refazer_sem_foto_s = refazer_sem_foto_s
        self.renovar_s = renovar_s
        self.teto_hora = teto_hora
        self.pausa_pergunta = pausa_pergunta
        self._perguntas: list[float] = []
        self._fila: queue.Queue = queue.Queue()
        self._pendentes: set[str] = set()
        self._trava = threading.Lock()
        self._trabalhador: threading.Thread | None = None

    @property
    def _indice_arq(self) -> Path:
        return self.pasta / "indice.json"

    def indice(self) -> dict:
        try:
            d = json.loads(self._indice_arq.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def arquivo(self, jid: str, indice: dict | None = None) -> Path | None:
        e = (indice if indice is not None else self.indice()).get(jid) or {}
        arq = e.get("arq") or ""
        if not re.fullmatch(r"[0-9a-f]{40}\.img", arq):
            return None
        p = self.pasta / arq
        return p if p.is_file() else None

    def precisa(self, jid: str, url: str, indice: dict) -> bool:
        if not JID_OK.match(jid or ""):
            return False
        e = indice.get(jid) or {}
        if not url:   # sem URL no banco: perguntar ao WhatsApp (pedido 54)
            if self.evo is None or jid.endswith("@g.us"):
                return False
            visto = float(e.get("perguntado_em") or 0)
            return self.relogio() - visto > (self.renovar_s if e.get("arq") else self.refazer_sem_foto_s)
        if not _host_ok(url):
            return False
        if e.get("falhou_em") and self.relogio() - float(e["falhou_em"]) < self.refazer_falha_s and e.get("chave") == chave_da_foto(url):
            return False
        return e.get("chave") != chave_da_foto(url) or not e.get("arq")

    def pedir(self, pedidos) -> int:
        """pedidos: iterável de (jid, url). Enfileira só o que mudou; devolve quantos entraram na fila."""
        ind = self.indice()
        n = 0
        with self._trava:
            for jid, url in pedidos:
                if jid in self._pendentes or not self.precisa(jid, url, ind):
                    continue
                self._pendentes.add(jid)
                self._fila.put((jid, url))
                n += 1
            if n and (self._trabalhador is None or not self._trabalhador.is_alive()):
                self._trabalhador = threading.Thread(target=self._trabalhar, name="vigia-avatares", daemon=True)
                self._trabalhador.start()
        return n

    def _trabalhar(self) -> None:
        while True:
            try:
                jid, url = self._fila.get(timeout=2)
            except queue.Empty:
                return
            try:
                self.processar(jid, url)
            except Exception as e:  # nunca derruba o painel
                log.warning("foto não baixada: %s", causa(e))
            finally:
                with self._trava:
                    self._pendentes.discard(jid)
            time.sleep(self.pausa if url else max(self.pausa, self.pausa_pergunta))

    def processar(self, jid: str, url: str) -> bool:
        """Baixa e grava (síncrono). True se gravou. `url` vazia: pergunta a URL ao WhatsApp antes (pedido 54)."""
        perguntado = {}
        if not url:
            agora = self.relogio()
            self._perguntas = [x for x in self._perguntas if agora - x < 3600]
            if len(self._perguntas) >= self.teto_hora:
                return False   # teto da hora: volta a ser pedido numa próxima abertura da lista
            self._perguntas.append(agora)
            perguntado = {"perguntado_em": int(agora)}
            url = self._url_nova(jid)
            antes = dict(self.indice().get(jid) or {})
            if not url or antes.get("chave") == chave_da_foto(url) and antes.get("arq"):
                antes.update(perguntado)   # sem foto (ou escondida), ou a mesma de antes: só anota a hora
                self._atualizar(jid, antes)
                return False
        try:
            dados = self.baixar(url)
        except urllib.error.HTTPError as e:
            novo = self._url_nova(jid) if e.code in (403, 404, 410) else ""
            if not novo:
                self._marcar_falha(jid, url)
                return False
            url = novo
            try:
                dados = self.baixar(url)
            except (urllib.error.URLError, OSError, ValueError):
                self._marcar_falha(jid, url)
                return False
        except (urllib.error.URLError, OSError, ValueError):
            self._marcar_falha(jid, url)
            return False
        self.pasta.mkdir(parents=True, exist_ok=True, mode=0o700)
        arq = hashlib.sha1(jid.encode("utf-8")).hexdigest() + ".img"
        tmp = self.pasta / (arq + ".tmp")
        tmp.write_bytes(dados)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.pasta / arq)
        self._atualizar(jid, {"arq": arq, "chave": chave_da_foto(url), "em": int(self.relogio()), **perguntado})
        return True

    def _url_nova(self, jid: str) -> str:
        if self.evo is None:
            return ""
        try:
            url = self.evo.foto_perfil(jid) or ""
        except Exception as e:
            log.warning("URL de foto não renovada: %s", causa(e))
            return ""
        return url if _host_ok(url) else ""

    def _marcar_falha(self, jid: str, url: str) -> None:
        e = dict(self.indice().get(jid) or {})
        e.update({"chave": chave_da_foto(url), "falhou_em": int(self.relogio()), "perguntado_em": int(self.relogio())})
        self._atualizar(jid, e)

    def _atualizar(self, jid: str, entrada: dict) -> None:
        with self._trava:
            ind = self.indice()
            ind[jid] = entrada
            _gravar_json(self._indice_arq, ind)
