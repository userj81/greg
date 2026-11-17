"""Banco SQLite do Greg: classificações, lotes, QR, envios, lápides e visão de conversas.
Quem chama: bin/coletor.py, app/ponte/*, app/conexao.py, app/paginas/*, bin/fechar-semana.py.
O que não faz: não grava texto bruto de mensagem (só resumo da IA) — exceção única: a transcrição do áudio feita pelo
motor local (D-47, tabela `transcricoes`, apagada na limpeza semanal); não loga conteúdo, não fala com rede.
"""
import os
import sqlite3
import time
from datetime import date, datetime, time as hora, timedelta
from pathlib import Path

from app.classes import CLASSES, CLASSES_ITEM

MIGRACOES = Path(__file__).resolve().parent / "migracoes"
TS_MIN = 0            # contrato histórico usa ts pequenos; a emenda F4 pede 2010 (1262304000): subir aqui quando o contrato mudar
TS_MAX = 4102444800   # 2100-01-01; barra timestamps em milissegundos
RESUMO_MAX = 300
TRANSCRICAO_MAX = 20000   # D-47: 10 min de fala ≈ 9 000 caracteres; acima disso é ruído do motor
DECISOES = ("confirmado", "recusado")
MOTIVOS_SUBSTITUICAO = ("atualizada", "cancelada", "consolidada")   # D-57
CLASSES_QUIETAS = ("aviso", "sem_acao")                             # D-58: para onde a IA rebaixa o que era só conversa
SITUACOES = ("aguarda_dono", "aguarda_contato", "encerrada")        # D-58: em que pé a conversa ficou (histórico)
MOTIVO_MAX = 200


def _validar_ts(ts) -> int:
    if isinstance(ts, bool) or not isinstance(ts, int) or not TS_MIN <= ts <= TS_MAX:
        raise ValueError("ts deve ser inteiro em segundos (epoch) dentro da faixa válida")
    return ts


class Banco:
    somente_leitura = False   # atributo de classe: Banco criado sem __init__ (testes) continua gravável

    def __init__(self, caminho: str, somente_leitura: bool = False):
        """`somente_leitura=True` (aditivo, E10 --dry-run): abre o arquivo existente em `mode=ro`, sem criar, sem chmod e
        sem migrar; qualquer escrita falha com sqlite3.OperationalError."""
        self.caminho = str(caminho)
        self.somente_leitura = bool(somente_leitura)
        if self.somente_leitura:
            if not os.path.exists(self.caminho):
                raise FileNotFoundError("banco inexistente (somente leitura não cria)")
            return
        pai = Path(self.caminho).parent
        pai.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not os.path.exists(self.caminho):
            os.close(os.open(self.caminho, os.O_CREAT | os.O_WRONLY, 0o600))
        self.migrar()

    def _con(self, autocommit: bool = False) -> sqlite3.Connection:
        if self.somente_leitura:
            con = sqlite3.connect(Path(self.caminho).resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
        elif autocommit:
            con = sqlite3.connect(self.caminho, timeout=30, autocommit=True)
        else:
            con = sqlite3.connect(self.caminho, timeout=30)
        con.row_factory = sqlite3.Row
        return con

    def migrar(self) -> list[str]:
        """Aplica app/migracoes/*.sql ainda não aplicados, em ordem; cada arquivo numa transação.
        Devolve os nomes aplicados por ESTA chamada (corrida entre processos: o perdedor devolve [])."""
        aplicadas = []
        con = self._con(autocommit=True)
        try:
            con.execute("CREATE TABLE IF NOT EXISTS _migracoes (nome TEXT PRIMARY KEY, aplicada_em TEXT NOT NULL)")
            for arq in sorted(MIGRACOES.glob("*.sql")):
                con.execute("BEGIN IMMEDIATE")  # lock de escrita: a checagem abaixo já é serializada
                try:
                    if con.execute("SELECT 1 FROM _migracoes WHERE nome=?", (arq.name,)).fetchone():
                        con.execute("COMMIT")
                        continue
                    con.executescript(arq.read_text(encoding="utf-8"))
                    cur = con.execute("INSERT OR IGNORE INTO _migracoes (nome, aplicada_em) VALUES (?, ?)",
                                      (arq.name, datetime.now().isoformat(sep=" ", timespec="seconds")))
                    con.execute("COMMIT")
                    if cur.rowcount:
                        aplicadas.append(arq.name)
                except BaseException:
                    if con.in_transaction:
                        con.execute("ROLLBACK")
                    raise
        finally:
            con.close()
        try:
            os.chmod(self.caminho, 0o600)
        except OSError:
            pass
        return aplicadas

    def _escrever(self, sql: str, params: tuple = ()) -> int:
        con = self._con()
        try:
            cur = con.execute(sql, params)
            con.commit()
            return cur.lastrowid
        finally:
            con.close()

    def _ler(self, sql: str, params: tuple = ()) -> list[dict]:
        con = self._con()
        try:
            return [dict(r) for r in con.execute(sql, params)]
        finally:
            con.close()

    def gravar_classificacao(self, *, msg_id, conversa, contato, ts, classe, resumo, confianca,
                             frente_id=None, item_id=None, prazo=None, imagem=None, secao_id=None) -> int:
        """Grava (ou atualiza, se o msg_id já existe) a classificação; devolve o id.
        item_id/frente_id/imagem/prazo nunca são apagados por um upsert com None."""
        ts = _validar_ts(ts)
        if isinstance(resumo, str) and len(resumo) > RESUMO_MAX:
            resumo = resumo[:RESUMO_MAX - 3] + "…"
        con = self._con()
        try:
            con.execute(
                "INSERT INTO classificacoes (msg_id, conversa, contato, ts, classe, resumo, confianca, frente_id, item_id, prazo, imagem, secao_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(msg_id) DO UPDATE SET "
                "conversa=excluded.conversa, contato=excluded.contato, ts=excluded.ts, classe=excluded.classe, "
                "resumo=excluded.resumo, confianca=excluded.confianca, "
                "frente_id=COALESCE(excluded.frente_id, classificacoes.frente_id), "
                "item_id=COALESCE(excluded.item_id, classificacoes.item_id), "
                "prazo=COALESCE(excluded.prazo, classificacoes.prazo), "
                "imagem=COALESCE(excluded.imagem, classificacoes.imagem), "
                "secao_id=COALESCE(excluded.secao_id, classificacoes.secao_id)",
                (msg_id, conversa, contato, ts, classe, resumo, confianca, frente_id, item_id, prazo, imagem, secao_id))
            con.commit()
            return con.execute("SELECT id FROM classificacoes WHERE msg_id=?", (msg_id,)).fetchone()[0]
        finally:
            con.close()

    def classificacao_por_msg_id(self, msg_id) -> dict | None:
        r = self._ler("SELECT * FROM classificacoes WHERE msg_id=?", (msg_id,))
        return r[0] if r else None

    def classificacao(self, id) -> dict | None:
        r = self._ler("SELECT * FROM classificacoes WHERE id=?", (id,))
        return r[0] if r else None

    def decidir(self, id, decisao: str) -> None:
        """Grava a decisão do dono ('confirmado'|'recusado') na classificação, com a hora (007: mede a triagem)."""
        if decisao not in DECISOES:
            raise ValueError("decisao deve ser 'confirmado' ou 'recusado'")
        agora = datetime.now().isoformat(sep=" ", timespec="seconds")
        self._escrever("UPDATE classificacoes SET decisao=?, decidido_em=? WHERE id=?", (decisao, agora, id))

    def desfazer_decisao(self, id) -> None:
        """Desfaz a decisão (toast "Desfazer" / ⌘Z): a classificação volta a 'sem decisão'."""
        self._escrever("UPDATE classificacoes SET decisao=NULL, decidido_em=NULL WHERE id=?", (id,))

    def triagem_hoje(self) -> dict:
        """Quantas decisões hoje e o tempo médio (s) entre a classificação entrar e ser decidida: o número do rodapé
        do estado vazio das Demandas. {"n": 0, "media_s": None} sem decisão hoje."""
        hoje = date.today().isoformat()
        r = self._ler("SELECT COUNT(*) AS n, AVG(strftime('%s', decidido_em) - strftime('%s', criado_em)) AS media_s "
                      "FROM classificacoes WHERE decidido_em IS NOT NULL AND substr(decidido_em, 1, 10)=?", (hoje,))
        n = int(r[0]["n"] or 0) if r else 0
        media = r[0]["media_s"] if r and n else None
        return {"n": n, "media_s": (max(0, int(media)) if media is not None else None)}

    def precisao_hoje(self) -> dict:
        """Rótulos de hoje (classe_curada): quantos acertos da IA em quantos rotulados. {"acertos": 0, "total": 0}."""
        hoje = date.today().isoformat()
        r = self._ler("SELECT COUNT(*) AS total, SUM(CASE WHEN classe_curada=classe THEN 1 ELSE 0 END) AS acertos "
                      "FROM classificacoes WHERE classe_curada IS NOT NULL AND substr(curado_em, 1, 10)=?", (hoje,))
        if not r:
            return {"acertos": 0, "total": 0}
        return {"acertos": int(r[0]["acertos"] or 0), "total": int(r[0]["total"] or 0)}

    def classificacoes(self, *, conversa=None, classe=None, desde=None, ate=None, limite=200,
                       excluir_classe: str | None = None, substituidas: bool | None = False) -> list[dict]:
        """`substituidas` (D-57): False (padrão) só os cartões vigentes; True só os substituídos; None todos."""
        onde, params = [], []
        if substituidas is False:
            onde.append("substituida_por IS NULL")
        elif substituidas is True:
            onde.append("substituida_por IS NOT NULL")
        if excluir_classe is not None:
            onde.append("classe<>?"); params.append(excluir_classe)
        if conversa is not None:
            onde.append("conversa=?"); params.append(conversa)
        if classe is not None:
            onde.append("classe=?"); params.append(classe)
        if desde is not None:
            onde.append("ts>=?"); params.append(desde)
        if ate is not None:
            onde.append("ts<=?"); params.append(ate)
        sql = "SELECT * FROM classificacoes" + (" WHERE " + " AND ".join(onde) if onde else "")
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        return self._ler(sql, tuple(params) + (int(limite),))

    def ultimas_da_conversa(self, conversa: str, n: int = 10) -> list[dict]:
        return self.classificacoes(conversa=conversa, limite=n)

    # ------------------------------------------------------------- D-57: substituição, consolidação e lançamento adiado

    def substituir(self, velho_id, novo_id, motivo: str) -> None:
        """O cartão `novo_id` toma o lugar de `velho_id` (mesma conversa). O velho sai de Demandas (filtro "Substituídos")
        e nunca é apagado. Recusa: motivo fora da lista, cartão inexistente, conversas diferentes, cartão recusado pelo dono
        (intocável), novo já substituído, ou o próprio. Repetir a mesma substituição é inócuo."""
        if motivo not in MOTIVOS_SUBSTITUICAO:
            raise ValueError(f"motivo de substituição inválido (use {', '.join(MOTIVOS_SUBSTITUICAO)})")
        velho_id, novo_id = int(velho_id), int(novo_id)
        if velho_id == novo_id:
            raise ValueError("um cartão não substitui a si mesmo")
        velho, novo = self.classificacao(velho_id), self.classificacao(novo_id)
        if velho is None or novo is None:
            raise ValueError("cartão inexistente")
        if velho.get("conversa") != novo.get("conversa"):
            raise ValueError("cartões de conversas diferentes")
        if velho.get("decisao") == "recusado":
            raise ValueError("cartão recusado pelo dono não se substitui")
        if novo.get("substituida_por") is not None:
            raise ValueError("o cartão novo já foi substituído")
        if velho.get("substituida_por") == novo_id:
            return
        if velho.get("substituida_por") is not None:
            raise ValueError("cartão já substituído por outro")
        agora = datetime.now().isoformat(sep=" ", timespec="seconds")
        self._escrever("UPDATE classificacoes SET substituida_por=?, substituicao_motivo=?, substituida_em=? WHERE id=?",
                       (novo_id, motivo, agora, velho_id))

    def restaurar(self, id) -> None:
        """Restaurar (D-57): o cartão substituído volta a Demandas; o que o substituiu fica como está."""
        self._escrever("UPDATE classificacoes SET substituida_por=NULL, substituicao_motivo=NULL, substituida_em=NULL WHERE id=?",
                       (int(id),))

    def substituidas_de(self, novo_id) -> list[dict]:
        """Os cartões que `novo_id` substituiu diretamente (o "antes" do cartão atualizado)."""
        return self._ler("SELECT * FROM classificacoes WHERE substituida_por=? ORDER BY id", (int(novo_id),))

    def anteriores_de(self, ids) -> dict[int, list[dict]]:
        """{novo_id: [cartões substituídos por ele]} para uma lista de ids (uma consulta; a lista de Demandas)."""
        ids = [int(i) for i in ids]
        if not ids:
            return {}
        saida: dict[int, list[dict]] = {}
        for i in range(0, len(ids), 400):   # abaixo do limite de variáveis do SQLite
            fatia = ids[i:i + 400]
            marcas = ",".join("?" * len(fatia))
            for r in self._ler(f"SELECT * FROM classificacoes WHERE substituida_por IN ({marcas}) ORDER BY id", tuple(fatia)):
                saida.setdefault(int(r["substituida_por"]), []).append(r)
        return saida

    def marcar_lancado(self, id, item_id, *, frente_id=None, confianca: str | None = None) -> None:
        """O cartão subiu ao Frentes (ou herdou o item do cartão que substituiu): grava item_id e lancado_em.
        `frente_id`/`confianca` só quando mudaram no lançamento (frente arquivada vira Inbox + baixa)."""
        agora = datetime.now().isoformat(sep=" ", timespec="seconds")
        self._escrever("UPDATE classificacoes SET item_id=?, lancado_em=?, frente_id=?, confianca=COALESCE(?, confianca) WHERE id=?",
                       (int(item_id), agora, frente_id, confianca, int(id)))

    def atualizar_resumo_prazo(self, id, *, resumo=None, prazo=None, lancado: bool = False) -> None:
        """Consolidação do dia (D-57): resumo/prazo finais do cartão que fica. Por padrão só cartão provisório (sem item,
        sem decisão). `lancado=True` (D-58): vale também para o cartão que já está no Frentes (a IA corrige o que subiu);
        cartão substituído ou recusado pelo dono nunca é reescrito."""
        reg = self.classificacao(id)
        if reg is None:
            raise ValueError("cartão inexistente")
        if reg.get("substituida_por") is not None or reg.get("decisao") == "recusado":
            raise ValueError("cartão substituído ou recusado não tem resumo e prazo reescritos")
        if not lancado and (reg.get("decisao") or reg.get("item_id") is not None or reg.get("lancado_em")):
            raise ValueError("só cartão provisório pode ter resumo e prazo reescritos")
        if isinstance(resumo, str) and len(resumo) > RESUMO_MAX:
            resumo = resumo[:RESUMO_MAX - 3] + "…"
        if resumo is None and prazo is None:
            return
        self._escrever("UPDATE classificacoes SET resumo=COALESCE(?, resumo), prazo=COALESCE(?, prazo) WHERE id=?",
                       (resumo, prazo, int(id)))

    # ------------------------------------------------------------- D-58: régua do fechamento e histórico de análise

    def rebaixar(self, id, classe: str, motivo: str = "") -> None:
        """No fechamento do dia a IA segura o cartão PROVISÓRIO que era só conversa: a classe vira aviso/sem_acao (a da
        leitura fica em classe_original) e o cartão não sobe ao Frentes. Não mexe em cartão com item, decidido, substituído
        ou rotulado pelo dono. Repetir o mesmo rebaixamento é inócuo; nada é apagado."""
        if classe not in CLASSES_QUIETAS:
            raise ValueError(f"só se rebaixa para {' ou '.join(CLASSES_QUIETAS)}")
        reg = self.classificacao(id)
        if reg is None:
            raise ValueError("cartão inexistente")
        if reg.get("rebaixada_em") and reg.get("classe") == classe:
            return
        if reg.get("classe_curada"):
            raise ValueError("cartão rotulado pelo dono não é rebaixado")
        if (reg.get("classe") not in CLASSES_ITEM or reg.get("item_id") is not None or reg.get("lancado_em")
                or reg.get("decisao") or reg.get("substituida_por") is not None):
            raise ValueError("só cartão provisório pode ser rebaixado")
        motivo = " ".join(str(motivo or "").split())
        if len(motivo) > MOTIVO_MAX:
            motivo = motivo[:MOTIVO_MAX - 1] + "…"
        agora = datetime.now().isoformat(sep=" ", timespec="seconds")
        self._escrever("UPDATE classificacoes SET classe_original=COALESCE(classe_original, classe), classe=?, "
                       "rebaixada_em=?, rebaixada_motivo=? WHERE id=?", (classe, agora, motivo or None, int(id)))

    def gravar_analise(self, *, msg_id, conversa, contato, ts, resumo, situacao, lote=None) -> tuple[int, bool]:
        """Uma linha do histórico de análise (uma por conversa por leitura). Devolve (id, criou). UNIQUE(msg_id): repetir
        o mesmo msg_id não cria linha nem muda a que existe."""
        if situacao not in SITUACOES:
            raise ValueError(f"situação inválida (use {', '.join(SITUACOES)})")
        resumo = " ".join(str(resumo or "").split())
        if not resumo:
            raise ValueError("resumo vazio")
        if len(resumo) > RESUMO_MAX:
            resumo = resumo[:RESUMO_MAX - 3] + "…"
        existente = self._ler("SELECT id FROM analises WHERE msg_id=?", (str(msg_id),))
        if existente:
            return int(existente[0]["id"]), False
        novo = self._escrever("INSERT INTO analises (msg_id, lote, conversa, contato, ts, resumo, situacao) VALUES (?,?,?,?,?,?,?)",
                              (str(msg_id), lote, str(conversa), contato, int(ts), resumo, situacao))
        return int(novo), True

    def analises(self, *, desde=None, ate=None, conversa=None, situacao=None, limite: int = 500) -> list[dict]:
        """O histórico de análise, mais recente primeiro. `desde`/`ate` em epoch (hora da última mensagem analisada)."""
        onde, params = ["1=1"], []
        if desde is not None:
            onde.append("ts >= ?"); params.append(int(desde))
        if ate is not None:
            onde.append("ts < ?"); params.append(int(ate))
        if conversa is not None:
            onde.append("conversa=?"); params.append(conversa)
        if situacao is not None:
            onde.append("situacao=?"); params.append(situacao)
        return self._ler("SELECT * FROM analises WHERE " + " AND ".join(onde) + " ORDER BY ts DESC, id DESC LIMIT ?",
                         tuple(params) + (int(limite),))

    def ultima_analise_por_conversa(self, *, desde=None, situacao=None, limite: int = 500) -> list[dict]:
        """A análise MAIS RECENTE de cada conversa (é ela que diz em que pé a conversa está hoje). `situacao` filtra depois
        de escolher a última: "aguarda_dono" = quem ficou sem resposta dele. Mais recente primeiro."""
        linhas = self._ler("SELECT a.* FROM analises a JOIN (SELECT conversa, MAX(ts) AS mts FROM analises GROUP BY conversa) u "
                           "ON u.conversa = a.conversa AND u.mts = a.ts ORDER BY a.ts DESC, a.id DESC")
        vistos, saida = set(), []
        for r in linhas:
            if r["conversa"] in vistos:
                continue
            vistos.add(r["conversa"])
            if desde is not None and int(r["ts"]) < int(desde):
                continue
            if situacao is not None and r["situacao"] != situacao:
                continue
            saida.append(r)
        return saida[:int(limite)]

    def lancadas(self, *, conversa: str | None = None, dias: int = 14, limite: int = 500) -> list[dict]:
        """Cartões que já estão no Frentes e a IA ainda pode corrigir (D-58): classe efetiva que vira item, com item, não
        substituídos, não recusados, lançados nos últimos `dias`. Se o item ainda está aberto, só o Frentes sabe.
        Ordem: conversa, hora da mensagem, id."""
        desde = (datetime.now() - timedelta(days=int(dias))).isoformat(sep=" ", timespec="seconds")
        onde = ["COALESCE(classe_curada, classe) IN (" + ",".join("?" * len(CLASSES_ITEM)) + ")",
                "item_id IS NOT NULL", "substituida_por IS NULL", "COALESCE(decisao, '') <> 'recusado'",
                "COALESCE(lancado_em, criado_em) >= ?"]
        params: list = list(CLASSES_ITEM) + [desde]
        if conversa is not None:
            onde.append("conversa=?"); params.append(conversa)
        return self._ler("SELECT * FROM classificacoes WHERE " + " AND ".join(onde) + " ORDER BY conversa, ts, id LIMIT ?",
                         tuple(params) + (int(limite),))

    def provisorias(self, *, ate: str | None = None, conversa: str | None = None, limite: int = 500) -> list[dict]:
        """Cartões provisórios (D-57): classe efetiva (rótulo do dono, senão a da IA) que vira item, sem item, não lançados,
        sem decisão e não substituídos. `ate` = só criados antes desse 'AAAA-MM-DD HH:MM:SS' (o fechamento do dia).
        Ordem: conversa, hora da mensagem, id."""
        onde = ["COALESCE(classe_curada, classe) IN (" + ",".join("?" * len(CLASSES_ITEM)) + ")",
                "item_id IS NULL", "lancado_em IS NULL", "decisao IS NULL", "substituida_por IS NULL"]
        params: list = list(CLASSES_ITEM)
        if ate is not None:
            onde.append("criado_em < ?"); params.append(str(ate))
        if conversa is not None:
            onde.append("conversa=?"); params.append(conversa)
        return self._ler("SELECT * FROM classificacoes WHERE " + " AND ".join(onde) + " ORDER BY conversa, ts, id LIMIT ?",
                         tuple(params) + (int(limite),))

    def registrar_lote(self, *, nome, conversas, novas, itens, atencao, tokens_in=None, tokens_out=None, modelo=None,
                       custo_usd=None, duracao_ms=None, turnos=None) -> int:
        return self._escrever(
            "INSERT INTO lotes (nome, conversas, novas, itens, atencao, tokens_in, tokens_out, modelo, custo_usd, duracao_ms, turnos) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (nome, conversas, novas, itens, atencao, tokens_in, tokens_out, modelo, custo_usd, duracao_ms, turnos))

    def lotes(self, limite: int = 20) -> list[dict]:
        return self._ler("SELECT * FROM lotes ORDER BY id DESC LIMIT ?", (int(limite),))

    def registrar_qr(self, origem: str) -> int:
        """Registra um QR gerado (origem: clique|renovacao); devolve quantos QRs há hoje."""
        agora = datetime.now().isoformat(sep=" ", timespec="seconds")
        self._escrever("INSERT INTO qr_eventos (origem, criado_em) VALUES (?, ?)", (origem, agora))
        return self.qrs_hoje()

    def qrs_hoje(self) -> int:
        """Conta qr_eventos com epoch >= meia-noite local de hoje."""
        meia_noite = int(datetime.combine(date.today(), hora.min).timestamp())
        r = self._ler("SELECT COUNT(*) AS n FROM qr_eventos WHERE epoch>=?", (meia_noite,))
        return r[0]["n"]

    # ------------------------------------------------------------- fase A (D-45): medir antes de decidir produto

    def curar(self, id, classe: str) -> None:
        """Grava a classe CERTA segundo o dono (rotulagem). Igual à prevista = acerto; não toca em `decisao`."""
        if classe not in CLASSES:
            raise ValueError(f"classe inválida: {classe!r} (use {', '.join(CLASSES)})")
        agora = datetime.now().isoformat(sep=" ", timespec="seconds")
        self._escrever("UPDATE classificacoes SET classe_curada=?, curado_em=? WHERE id=?", (classe, agora, id))

    def registrar_evento_conexao(self, *, evento: str, estado: str, motivo=None, reason_code=None) -> int:
        """Histórico de quedas e voltas (queda | socket_morto | logout_sem_pareamento | voltou)."""
        agora = datetime.now().isoformat(sep=" ", timespec="seconds")
        return self._escrever(
            "INSERT INTO conexao_eventos (evento, estado, motivo, reason_code, criado_em) VALUES (?,?,?,?,?)",
            (evento, estado, motivo, reason_code, agora))

    def eventos_conexao(self, *, dias: int = 14, limite: int = 200) -> list[dict]:
        return self._ler("SELECT * FROM conexao_eventos WHERE criado_em >= datetime('now','localtime',?) "
                         "ORDER BY id DESC LIMIT ?", (f"-{int(dias)} days", int(limite)))

    def metricas_lotes_por_dia(self, dias: int = 14) -> list[dict]:
        """Um registro por dia (mais recente primeiro): ciclos com Claude, mensagens, tokens, custo nocional, duração média."""
        return self._ler(
            "SELECT substr(criado_em,1,10) AS dia, COUNT(*) AS lotes, SUM(conversas) AS conversas, SUM(novas) AS novas, "
            "SUM(itens) AS itens, SUM(atencao) AS atencao, SUM(COALESCE(tokens_in,0)) AS tokens_in, "
            "SUM(COALESCE(tokens_out,0)) AS tokens_out, SUM(custo_usd) AS custo_usd, SUM(custo_usd IS NOT NULL) AS com_custo, "
            "AVG(duracao_ms) AS duracao_ms "
            "FROM lotes WHERE criado_em >= datetime('now','localtime',?) GROUP BY dia ORDER BY dia DESC",
            (f"-{int(dias)} days",))

    def metricas_curadoria(self, dias: int = 14) -> dict:
        """Precisão da classificação no período: total, rotuladas, acertos, matriz prevista × curada, itens no Frentes."""
        p = (f"-{int(dias)} days",)
        base = "FROM classificacoes WHERE criado_em >= datetime('now','localtime',?)"
        t = self._ler(f"SELECT COUNT(*) AS n, SUM(classe_curada IS NOT NULL) AS curadas, SUM(classe_curada = classe) AS acertos, "
                      f"SUM(item_id IS NOT NULL) AS itens, SUM(decisao='confirmado') AS confirmados, "
                      f"SUM(decisao='recusado') AS recusados {base}", p)[0]
        matriz = self._ler(f"SELECT classe, classe_curada, COUNT(*) AS n {base} AND classe_curada IS NOT NULL "
                           f"GROUP BY classe, classe_curada ORDER BY classe, classe_curada", p)
        por_classe = self._ler(f"SELECT classe, COUNT(*) AS n, SUM(classe_curada IS NOT NULL) AS curadas, "
                               f"SUM(classe_curada = classe) AS acertos {base} GROUP BY classe ORDER BY n DESC", p)
        for r in por_classe:
            r["curadas"], r["acertos"] = r["curadas"] or 0, r["acertos"] or 0
        return {"total": t["n"] or 0, "curadas": t["curadas"] or 0, "acertos": t["acertos"] or 0,
                "itens": t["itens"] or 0, "confirmados": t["confirmados"] or 0, "recusados": t["recusados"] or 0,
                "matriz": matriz, "por_classe": por_classe}

    def qrs_por_dia(self, dias: int = 14) -> list[dict]:
        return self._ler("SELECT substr(criado_em,1,10) AS dia, COUNT(*) AS n FROM qr_eventos "
                         "WHERE criado_em >= datetime('now','localtime',?) GROUP BY dia ORDER BY dia DESC", (f"-{int(dias)} days",))

    def envios_por_dia(self, dias: int = 14) -> list[dict]:
        return self._ler("SELECT substr(criado_em,1,10) AS dia, COUNT(*) AS n, SUM(por LIKE 'api:%' AND por NOT LIKE 'api:painel%') AS api "
                         "FROM envios WHERE criado_em >= datetime('now','localtime',?) GROUP BY dia ORDER BY dia DESC",
                         (f"-{int(dias)} days",))

    def registrar_envio(self, *, para, hash_texto, por) -> int:
        return self._escrever("INSERT INTO envios (para, hash_texto, por) VALUES (?,?,?)", (para, hash_texto, por))

    def gravar_lapide(self, *, semana, n_msgs, hash_relatorio, apagado_em) -> int:
        """Uma lápide por semana: repetir a semana é idempotente e devolve o id existente."""
        con = self._con()
        try:
            con.execute("INSERT OR IGNORE INTO lapides (semana, n_msgs, hash_relatorio, apagado_em) VALUES (?,?,?,?)",
                        (semana, n_msgs, hash_relatorio, apagado_em))
            con.commit()
            return con.execute("SELECT id FROM lapides WHERE semana=?", (semana,)).fetchone()[0]
        finally:
            con.close()

    def gravar_lapide_relimpeza(self, *, semana, n_msgs, hash_relatorio, apagado_em) -> int:
        """Nova lápide (a anterior nunca é apagada) para a limpeza repetida de uma semana restaurada (I2): a chave
        única `semana` vira `<semana>~relimpeza-<epoch>`; quem lê pela semana corta no `~`."""
        return self.gravar_lapide(semana=f"{semana}~relimpeza-{int(apagado_em)}", n_msgs=n_msgs,
                                  hash_relatorio=hash_relatorio, apagado_em=apagado_em)

    def lapides(self) -> list[dict]:
        return self._ler("SELECT * FROM lapides ORDER BY id")

    def apelidos(self) -> dict:
        """{jid: nome} dos contatos que o dono nomeou no Greg (r02)."""
        return {r["jid"]: r["nome"] for r in self._ler("SELECT jid, nome FROM apelidos")}

    def definir_apelido(self, jid: str, nome: str) -> None:
        """Grava ou troca o nome; nome vazio apaga (volta para o nome do WhatsApp ou o número)."""
        nome = " ".join(str(nome or "").split())[:80]
        if not nome:
            self._escrever("DELETE FROM apelidos WHERE jid=?", (jid,))
            return
        self._escrever("INSERT INTO apelidos (jid, nome) VALUES (?, ?) ON CONFLICT(jid) DO UPDATE SET nome=excluded.nome",
                       (jid, nome))

    # ---------------------------------------------------------------- vínculo conversa → frente (migração 006, D-49)

    def vinculos(self) -> dict:
        """{conversa: {"frente_id": int|None, "origem": "dono"|"auto"}}."""
        return {r["conversa"]: {"frente_id": r["frente_id"], "origem": r["origem"]}
                for r in self._ler("SELECT conversa, frente_id, origem FROM vinculos")}

    def vinculo(self, conversa: str) -> dict | None:
        r = self._ler("SELECT frente_id, origem FROM vinculos WHERE conversa=?", (str(conversa),))
        return r[0] if r else None

    # ---------------------------------------------------------------- frentes arquivadas pelo Greg (migração 008, pedido 41)

    def frentes_arquivadas(self) -> list[dict]:
        """[{frente_id, nome, tipo, arquivada_em}] das frentes arquivadas por aqui (o /api/frentes não devolve arquivada;
        este registro é o que permite listar e desarquivar). Mais recente primeiro."""
        return self._ler("SELECT frente_id, nome, tipo, arquivada_em FROM frentes_arquivadas ORDER BY arquivada_em DESC, frente_id DESC")

    def arquivar_frente(self, frente_id: int, nome: str, tipo: str = "") -> None:
        self._escrever("INSERT INTO frentes_arquivadas (frente_id, nome, tipo) VALUES (?, ?, ?) "
                       "ON CONFLICT(frente_id) DO UPDATE SET nome=excluded.nome, tipo=excluded.tipo, "
                       "arquivada_em=datetime('now','localtime')", (int(frente_id), str(nome or ""), str(tipo or "")))

    def desarquivar_frente(self, frente_id: int) -> dict | None:
        """Tira do registro local e devolve a linha que havia (None se não estava)."""
        r = self._ler("SELECT frente_id, nome, tipo, arquivada_em FROM frentes_arquivadas WHERE frente_id=?", (int(frente_id),))
        self._escrever("DELETE FROM frentes_arquivadas WHERE frente_id=?", (int(frente_id),))
        return r[0] if r else None

    # -- seções dentro da frente (011, pedido 79) -----------------------------------------------------------------
    def secoes(self, frente_id=None) -> list[dict]:
        """[{id, frente_id, nome, descricao, ordem}] de uma frente (ou de todas), na ordem em que foram criadas."""
        if frente_id is None:
            return self._ler("SELECT id, frente_id, nome, descricao, ordem FROM secoes ORDER BY frente_id, ordem, id")
        return self._ler("SELECT id, frente_id, nome, descricao, ordem FROM secoes WHERE frente_id=? ORDER BY ordem, id",
                         (int(frente_id),))

    def secao(self, secao_id) -> dict | None:
        r = self._ler("SELECT id, frente_id, nome, descricao, ordem FROM secoes WHERE id=?", (int(secao_id),))
        return r[0] if r else None

    def criar_secao(self, frente_id: int, nome: str, descricao: str = "") -> int:
        """Cria a seção; nome repetido na mesma frente (sem diferenciar maiúscula) levanta ValueError."""
        try:
            return self._escrever("INSERT INTO secoes (frente_id, nome, descricao, ordem) VALUES (?, ?, ?, "
                                  "(SELECT COALESCE(MAX(ordem), 0) + 1 FROM secoes WHERE frente_id=?))",
                                  (int(frente_id), str(nome), str(descricao or ""), int(frente_id)))
        except sqlite3.IntegrityError:
            raise ValueError("já existe uma seção com esse nome nesta frente") from None

    def alterar_secao(self, secao_id: int, *, nome: str | None = None, descricao: str | None = None) -> None:
        try:
            if nome is not None:
                self._escrever("UPDATE secoes SET nome=? WHERE id=?", (str(nome), int(secao_id)))
            if descricao is not None:
                self._escrever("UPDATE secoes SET descricao=? WHERE id=?", (str(descricao), int(secao_id)))
        except sqlite3.IntegrityError:
            raise ValueError("já existe uma seção com esse nome nesta frente") from None

    def apagar_secao(self, secao_id: int) -> int:
        """Apaga a seção e solta os itens dela (voltam a "sem seção"). Devolve quantos itens soltou. Item nenhum some.
        As fichas dos arquivos da seção também saem; o arquivo no disco é com quem chama (app/anexos.py)."""
        con = self._con()
        try:
            n = con.execute("DELETE FROM itens_secao WHERE secao_id=?", (int(secao_id),)).rowcount
            con.execute("DELETE FROM anexos_secao WHERE secao_id=?", (int(secao_id),))
            con.execute("DELETE FROM anexos_pendentes WHERE secao_id=?", (int(secao_id),))
            con.execute("UPDATE classificacoes SET secao_id=NULL WHERE secao_id=?", (int(secao_id),))
            con.execute("DELETE FROM secoes WHERE id=?", (int(secao_id),))
            con.commit()
            return n
        finally:
            con.close()

    # -- arquivos da seção (012, pedido 80) ------------------------------------------------------------------------
    _ANEXO = "id, secao_id, nome, tipo, tamanho, sha256, arquivo, criado_em, origem, msg_id"

    def anexos(self, secao_id=None) -> list[dict]:
        """Fichas dos arquivos de uma seção (ou de todas), do mais novo para o mais antigo."""
        if secao_id is None:
            return self._ler(f"SELECT {self._ANEXO} FROM anexos_secao ORDER BY id DESC")
        return self._ler(f"SELECT {self._ANEXO} FROM anexos_secao WHERE secao_id=? ORDER BY id DESC", (int(secao_id),))

    def anexo(self, anexo_id) -> dict | None:
        r = self._ler(f"SELECT {self._ANEXO} FROM anexos_secao WHERE id=?", (int(anexo_id),))
        return r[0] if r else None

    def criar_anexo(self, secao_id: int, nome: str, tipo: str, tamanho: int, sha256: str, arquivo: str,
                    origem: str = "dono", msg_id: str | None = None) -> int:
        return self._escrever("INSERT INTO anexos_secao (secao_id, nome, tipo, tamanho, sha256, arquivo, origem, msg_id) "
                              "VALUES (?,?,?,?,?,?,?,?)",
                              (int(secao_id), str(nome), str(tipo), int(tamanho), str(sha256), str(arquivo),
                               "ia" if origem == "ia" else "dono", msg_id))

    def pedir_anexo(self, msg_id: str, secao_id: int) -> None:
        """A IA pediu para guardar na seção o arquivo desta mensagem. Repetir o pedido não duplica."""
        self._escrever("INSERT INTO anexos_pendentes (msg_id, secao_id) VALUES (?, ?) ON CONFLICT(msg_id) DO NOTHING",
                       (str(msg_id), int(secao_id)))

    def anexos_pendentes(self) -> list[dict]:
        return self._ler("SELECT msg_id, secao_id, tentativas FROM anexos_pendentes ORDER BY pedido_em, msg_id")

    def anexo_tentado(self, msg_id: str) -> None:
        self._escrever("UPDATE anexos_pendentes SET tentativas = tentativas + 1 WHERE msg_id=?", (str(msg_id),))

    def anexo_resolvido(self, msg_id: str) -> None:
        self._escrever("DELETE FROM anexos_pendentes WHERE msg_id=?", (str(msg_id),))

    def apagar_anexo(self, anexo_id: int) -> None:
        self._escrever("DELETE FROM anexos_secao WHERE id=?", (int(anexo_id),))

    def secao_dos_itens(self) -> dict:
        """{item_id: secao_id} de tudo que está em alguma seção."""
        return {r["item_id"]: r["secao_id"] for r in self._ler("SELECT item_id, secao_id FROM itens_secao")}

    def por_na_secao(self, item_id: int, secao_id, origem: str = "dono") -> None:
        """Põe o item na seção (None = tira de qualquer seção). A IA (origem 'ia') nunca troca o que a pessoa pôs."""
        if secao_id is None:
            self._escrever("DELETE FROM itens_secao WHERE item_id=?", (int(item_id),))
            return
        if origem == "ia":
            self._escrever("INSERT INTO itens_secao (item_id, secao_id, origem) VALUES (?, ?, 'ia') "
                           "ON CONFLICT(item_id) DO UPDATE SET secao_id=excluded.secao_id, "
                           "posto_em=datetime('now','localtime') WHERE itens_secao.origem='ia'",
                           (int(item_id), int(secao_id)))
            return
        self._escrever("INSERT INTO itens_secao (item_id, secao_id, origem) VALUES (?, ?, 'dono') "
                       "ON CONFLICT(item_id) DO UPDATE SET secao_id=excluded.secao_id, origem='dono', "
                       "posto_em=datetime('now','localtime')", (int(item_id), int(secao_id)))

    def vincular(self, conversa: str, frente_id, origem: str = "dono") -> bool:
        """Grava o vínculo. `auto` nunca sobrescreve `dono`. Devolve True se gravou."""
        if origem not in ("dono", "auto"):
            raise ValueError("origem deve ser dono ou auto")
        conversa = str(conversa or "").strip()
        if not conversa:
            raise ValueError("conversa vazia")
        fid = None if frente_id in (None, "") else int(frente_id)
        if origem == "auto":
            antes = self.vinculo(conversa)
            if antes is not None and (antes["origem"] == "dono" or antes["frente_id"] == fid):
                return False
            self._escrever(
                "INSERT INTO vinculos (conversa, frente_id, origem) VALUES (?, ?, 'auto') "
                "ON CONFLICT(conversa) DO UPDATE SET frente_id=excluded.frente_id, "
                "atualizado_em=datetime('now','localtime') WHERE vinculos.origem='auto' "
                "AND vinculos.frente_id IS NOT excluded.frente_id", (conversa, fid))
            return True
        self._escrever(
            "INSERT INTO vinculos (conversa, frente_id, origem) VALUES (?, ?, 'dono') "
            "ON CONFLICT(conversa) DO UPDATE SET frente_id=excluded.frente_id, origem='dono', "
            "atualizado_em=datetime('now','localtime')", (conversa, fid))
        return True

    def desvincular(self, conversa: str) -> None:
        self._escrever("DELETE FROM vinculos WHERE conversa=?", (str(conversa),))

    def mudar_frente_da_classificacao(self, item_id: int, frente_id) -> None:
        self._escrever("UPDATE classificacoes SET frente_id=? WHERE item_id=?",
                       (None if frente_id is None else int(frente_id), int(item_id)))

    # ---------------------------------------------------------------- API de envio (migração 003, D-44)

    LOTE_ATIVOS = ("na_fila", "enviando", "aguardando_conexao")

    def criar_lote_envio(self, *, origem: str, idempotencia: str | None, hash_texto: str, destinos: list[dict]) -> int:
        """Lote + um destino por item, numa transação. `destinos`: [{para, nome, conhecido, estado, motivo}]."""
        con = self._con()
        try:
            cur = con.execute("INSERT INTO lotes_envio (origem, idempotencia, hash_texto, n, estado) VALUES (?,?,?,?,'na_fila')",
                              (origem, idempotencia, hash_texto, len(destinos)))
            lote_id = cur.lastrowid
            con.executemany(
                "INSERT INTO destinos_envio (lote_id, pos, para, nome, conhecido, estado, motivo) VALUES (?,?,?,?,?,?,?)",
                [(lote_id, i, d["para"], d.get("nome", ""), 1 if d.get("conhecido") else 0,
                  d.get("estado", "fila"), d.get("motivo", "")) for i, d in enumerate(destinos)])
            con.commit()
            return lote_id
        finally:
            con.close()

    def lote_envio(self, lote_id: int) -> dict | None:
        r = self._ler("SELECT * FROM lotes_envio WHERE id=?", (int(lote_id),))
        return r[0] if r else None

    def lotes_envio(self, limite: int = 50) -> list[dict]:
        return self._ler("SELECT * FROM lotes_envio ORDER BY id DESC LIMIT ?", (int(limite),))

    def lote_envio_por_idempotencia(self, origem: str, chave: str, horas: int = 24) -> dict | None:
        r = self._ler("SELECT * FROM lotes_envio WHERE origem=? AND idempotencia=? "
                      "AND criado_em >= datetime('now', 'localtime', ?) ORDER BY id DESC LIMIT 1",
                      (origem, chave, f"-{int(horas)} hours"))
        return r[0] if r else None

    def atualizar_lote_envio(self, lote_id: int, estado: str, motivo: str = "") -> None:
        self._escrever("UPDATE lotes_envio SET estado=?, motivo=?, atualizado_em=datetime('now','localtime') WHERE id=?",
                       (estado, motivo, int(lote_id)))

    def destinos_envio(self, lote_id: int) -> list[dict]:
        return self._ler("SELECT * FROM destinos_envio WHERE lote_id=? ORDER BY pos", (int(lote_id),))

    def atualizar_destino_envio(self, destino_id: int, estado: str, motivo: str = "", msg_id: str | None = None) -> None:
        self._escrever("UPDATE destinos_envio SET estado=?, motivo=?, msg_id=COALESCE(?, msg_id), "
                       "atualizado_em=datetime('now','localtime') WHERE id=?", (estado, motivo, msg_id, int(destino_id)))

    def mudar_destinos_envio(self, lote_id: int, de: str, para: str, motivo: str = "") -> int:
        """Troca o estado de todos os destinos do lote que estão em `de` (parar: fila→cancelado; retomar: cancelado→fila)."""
        con = self._con()
        try:
            cur = con.execute("UPDATE destinos_envio SET estado=?, motivo=?, atualizado_em=datetime('now','localtime') "
                              "WHERE lote_id=? AND estado=?", (para, motivo, int(lote_id), de))
            con.commit()
            return cur.rowcount
        finally:
            con.close()

    def proximo_destino_envio(self) -> dict | None:
        """Primeiro destino 'fila' do lote ativo mais antigo (ordem de chegada, depois posição)."""
        marcas = ",".join("?" for _ in self.LOTE_ATIVOS)
        r = self._ler(f"SELECT d.*, l.origem, l.estado AS estado_lote FROM destinos_envio d JOIN lotes_envio l ON l.id=d.lote_id "
                      f"WHERE d.estado='fila' AND l.estado IN ({marcas}) ORDER BY l.id, d.pos LIMIT 1", self.LOTE_ATIVOS)
        return r[0] if r else None

    def envios_api_hoje(self) -> dict:
        """{'conhecidos': n, 'novos': n} de destinos enviados ou incertos hoje (hora local)."""
        r = self._ler("SELECT conhecido, COUNT(*) AS n FROM destinos_envio WHERE estado IN ('enviado','incerto') "
                      "AND date(atualizado_em) = date('now','localtime') GROUP BY conhecido")
        saida = {"conhecidos": 0, "novos": 0}
        for linha in r:
            saida["conhecidos" if linha["conhecido"] else "novos"] = int(linha["n"])
        return saida

    def reservados_envio(self) -> dict:
        """Destinos ainda na fila de lotes que podem andar hoje (ativos ou pausados), por categoria."""
        estados = self.LOTE_ATIVOS + ("pausado",)
        marcas = ",".join("?" for _ in estados)
        r = self._ler(f"SELECT d.conhecido, COUNT(*) AS n FROM destinos_envio d JOIN lotes_envio l ON l.id=d.lote_id "
                      f"WHERE d.estado='fila' AND l.estado IN ({marcas}) GROUP BY d.conhecido", estados)
        saida = {"conhecidos": 0, "novos": 0}
        for linha in r:
            saida["conhecidos" if linha["conhecido"] else "novos"] = int(linha["n"])
        return saida

    def interromper_lotes_envio(self, motivo: str) -> int:
        """Lotes ativos de um processo anterior (o texto morreu com ele) viram 'interrompido'. Devolve quantos."""
        marcas = ",".join("?" for _ in self.LOTE_ATIVOS)
        con = self._con()
        try:
            cur = con.execute(f"UPDATE lotes_envio SET estado='interrompido', motivo=?, atualizado_em=datetime('now','localtime') "
                              f"WHERE estado IN ({marcas})", (motivo,) + self.LOTE_ATIVOS)
            con.commit()
            return cur.rowcount
        finally:
            con.close()

    # ------------------------------------------------------- transcrição de áudio (migração 005, pedido 30, D-47)

    ESTADOS_TRANSCRICAO = ("ok", "cortado", "falhou", "pulado")

    def gravar_transcricao(self, *, msg_id, conversa, ts, estado, texto="", segundos=0, segundos_lidos=0, motor=None,
                           tempo_ms=None) -> None:
        """Upsert por msg_id; repetir o mesmo msg_id soma `tentativas`. É o ÚNICO texto de mensagem que o SQLite guarda:
        vem do motor local e sai na limpeza semanal (apagar_transcricoes)."""
        if estado not in self.ESTADOS_TRANSCRICAO:
            raise ValueError(f"estado inválido: {estado!r} (use {', '.join(self.ESTADOS_TRANSCRICAO)})")
        ts = _validar_ts(ts)
        texto = str(texto or "")[:TRANSCRICAO_MAX]
        self._escrever(
            "INSERT INTO transcricoes (msg_id, conversa, ts, estado, texto, segundos, segundos_lidos, motor, tempo_ms) "
            "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(msg_id) DO UPDATE SET estado=excluded.estado, texto=excluded.texto, "
            "segundos=excluded.segundos, segundos_lidos=excluded.segundos_lidos, motor=excluded.motor, "
            "tempo_ms=excluded.tempo_ms, tentativas=transcricoes.tentativas+1, em=datetime('now','localtime')",
            (str(msg_id), str(conversa), ts, estado, texto, int(segundos or 0), int(segundos_lidos or 0), motor, tempo_ms))

    def transcricoes(self, msg_ids) -> dict:
        """{msg_id: linha} das mensagens pedidas (blocos de 400 para não estourar o limite de parâmetros do SQLite)."""
        ids = [str(i) for i in dict.fromkeys(msg_ids) if i]
        saida = {}
        for i in range(0, len(ids), 400):
            bloco = ids[i:i + 400]
            marcas = ",".join("?" for _ in bloco)
            for r in self._ler(f"SELECT * FROM transcricoes WHERE msg_id IN ({marcas})", tuple(bloco)):
                saida[r["msg_id"]] = r
        return saida

    def contar_transcricoes(self, ini_ts: int | None = None, fim_ts: int | None = None) -> int:
        """Quantas há na janela [ini, fim); sem janela, todas."""
        onde, params = [], []
        if ini_ts is not None:
            onde.append("ts>=?"); params.append(int(ini_ts))
        if fim_ts is not None:
            onde.append("ts<?"); params.append(int(fim_ts))
        sql = "SELECT COUNT(*) AS n FROM transcricoes" + (" WHERE " + " AND ".join(onde) if onde else "")
        return int(self._ler(sql, tuple(params))[0]["n"])

    def apagar_transcricoes(self, ini_ts: int, fim_ts: int) -> int:
        """Limpeza semanal (R-12 vale para a transcrição): apaga a janela [ini, fim) pelo ts da mensagem; devolve quantas."""
        con = self._con()
        try:
            cur = con.execute("DELETE FROM transcricoes WHERE ts>=? AND ts<?", (int(ini_ts), int(fim_ts)))
            con.commit()
            return cur.rowcount
        finally:
            con.close()

    def metricas_transcricao(self, dias: int = 14) -> dict:
        """Fase A: quantos áudios o motor tratou no período, por estado, segundos ouvidos e tempo de motor."""
        r = self._ler("SELECT COUNT(*) AS n, SUM(estado='ok') AS ok, SUM(estado='cortado') AS cortados, "
                      "SUM(estado='falhou') AS falhas, SUM(estado='pulado') AS pulados, "
                      "SUM(segundos_lidos) AS segundos, SUM(COALESCE(tempo_ms,0)) AS tempo_ms "
                      "FROM transcricoes WHERE em >= datetime('now','localtime',?)", (f"-{int(dias)} days",))[0]
        return {k: int(r[k] or 0) for k in ("n", "ok", "cortados", "falhas", "pulados", "segundos", "tempo_ms")}

    def conversas_recentes(self, dias: int = 7) -> list[dict]:
        """Conversas com movimento nos últimos `dias`: {conversa, contato, n_novas, ultima_classe, ultima_em}."""
        corte = int(time.time()) - int(dias) * 86400
        return self._ler(
            "SELECT c.conversa AS conversa, "
            "(SELECT x.contato FROM classificacoes x WHERE x.conversa=c.conversa AND x.ts>=? ORDER BY x.ts DESC, x.id DESC LIMIT 1) AS contato, "
            "COUNT(*) AS n_novas, "
            "(SELECT x.classe FROM classificacoes x WHERE x.conversa=c.conversa AND x.ts>=? ORDER BY x.ts DESC, x.id DESC LIMIT 1) AS ultima_classe, "
            "MAX(c.ts) AS ultima_em FROM classificacoes c WHERE c.ts>=? GROUP BY c.conversa ORDER BY ultima_em DESC",
            (corte, corte, corte))
