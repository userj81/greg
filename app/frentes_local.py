"""Frentes embutido: a mesma API do Frentes (frentes, itens, estado, captura) num SQLite dentro de dados/, sem servidor.
Existe para o Greg rodar sozinho (Docker, outra máquina) quando não há um Frentes externo: FRENTES_URL vazio ou
"embutido" (ver `criar_ponte`). Implementa as rotas que `FrentesPonte` chama, com as mesmas respostas, por isso o resto
do Greg não sabe com qual dos dois está falando.
Quem chama: app/frentes_ponte.criar_ponte (painel, coletor, ponte do Claude, fechamento do dia).
O que não faz: não abre porta, não fala com a rede e não interpreta linguagem natural na captura livre (o item nasce
com o texto como veio, na frente pedida ou no Inbox).
"""
import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from app.erros import FrentesRecusou
from app.frentes_ponte import FrentesPonte

ESQUEMA = """
CREATE TABLE IF NOT EXISTS frentes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nome TEXT NOT NULL UNIQUE,
    tipo TEXT NOT NULL DEFAULT 'cliente',
    contexto TEXT NOT NULL DEFAULT '',
    apelidos TEXT NOT NULL DEFAULT '[]',
    prioridade INTEGER NOT NULL DEFAULT 2,
    status TEXT NOT NULL DEFAULT 'ativa',
    ordem INTEGER NOT NULL DEFAULT 0,
    criado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS itens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    frente_id INTEGER REFERENCES frentes(id),
    tipo TEXT NOT NULL DEFAULT 'tarefa',
    texto TEXT NOT NULL,
    prazo TEXT,
    prioridade INTEGER NOT NULL DEFAULT 2,
    origem TEXT NOT NULL DEFAULT 'manual',
    confirmado INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'aberto',
    criado_em TEXT NOT NULL,
    feito_em TEXT
);
"""
STATUS_FRENTE = ("ativa", "pausada", "arquivada")
STATUS_ITEM = ("aberto", "feito", "cancelado")   # cancelado: Recusar em Demandas e substituição (D-57/D-58)
CAMPOS_FRENTE = ("nome", "tipo", "contexto", "status", "prioridade", "ordem", "apelidos")
CAMPOS_ITEM = ("frente_id", "tipo", "texto", "status", "prioridade", "prazo", "confirmado")


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class FrentesLocal(FrentesPonte):
    """FrentesPonte que responde de um SQLite local em vez de HTTP. Mesmos métodos, mesmas respostas."""

    def __init__(self, caminho, timeout: float = 10.0):
        super().__init__("embutido", "embutido", timeout)
        self.caminho = Path(caminho)
        self.caminho.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._trava = threading.Lock()
        with self._con() as con:
            con.executescript(ESQUEMA)

    @contextmanager
    def _con(self):
        con = sqlite3.connect(self.caminho, timeout=self.timeout, isolation_level=None)
        try:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys = ON")
            yield con
        finally:
            con.close()

    # -- transporte: as mesmas rotas do Frentes HTTP ------------------------------------------------------------
    def _chamar(self, metodo: str, caminho: str, query: dict | None = None, corpo: dict | None = None):
        query, corpo = query or {}, corpo or {}
        with self._trava, self._con() as con:
            if metodo == "GET" and caminho == "/api/frentes":
                return {"frentes": self._frentes(con)}
            if metodo == "POST" and caminho == "/api/frentes":
                return self._criar_frente(con, corpo)
            m = re.fullmatch(r"/api/frentes/(\d+)", caminho)
            if metodo == "PATCH" and m:
                return self._alterar_frente(con, int(m.group(1)), corpo)
            if metodo == "GET" and caminho == "/api/estado":
                return self._estado(con)
            if metodo == "GET" and caminho == "/api/itens":
                return {"itens": self._itens(con, query)}
            if metodo == "POST" and caminho in ("/api/itens", "/api/captura"):
                item = self._criar_item(con, corpo, origem_padrao="api" if caminho == "/api/itens" else "manual")
                if caminho == "/api/itens":
                    return {"id": item["id"], "item": item}
                return {"itens": [item], "captura_id": item["id"]}
            m = re.fullmatch(r"/api/itens/(\d+)", caminho)
            if metodo == "PATCH" and m:
                return {"item": self._alterar_item(con, int(m.group(1)), corpo)}
        raise FrentesRecusou(f"Frentes embutido: rota inexistente {metodo} {caminho}")

    # -- frentes --------------------------------------------------------------------------------------------
    @staticmethod
    def _frente(linha: sqlite3.Row) -> dict:
        f = dict(linha)
        try:
            f["apelidos"] = list(json.loads(f.get("apelidos") or "[]"))
        except ValueError:
            f["apelidos"] = []
        return f

    def _frentes(self, con, *, todas: bool = False) -> list[dict]:
        sql = "SELECT * FROM frentes" + ("" if todas else " WHERE status != 'arquivada'") + " ORDER BY ordem, prioridade, nome"
        return [self._frente(r) for r in con.execute(sql)]

    def _criar_frente(self, con, corpo: dict) -> dict:
        nome = " ".join(str(corpo.get("nome") or "").split())
        if not nome:
            raise FrentesRecusou("Frentes embutido: nome da frente vazio")
        try:
            cur = con.execute(
                "INSERT INTO frentes (nome, tipo, contexto, apelidos, prioridade, criado_em) VALUES (?, ?, ?, ?, ?, ?)",
                (nome, str(corpo.get("tipo") or "cliente"), str(corpo.get("contexto") or ""),
                 json.dumps(list(corpo.get("apelidos") or []), ensure_ascii=False), int(corpo.get("prioridade", 2)),
                 _agora()))
        except sqlite3.IntegrityError:
            raise FrentesRecusou("Frentes embutido: já existe frente com esse nome") from None
        f = self._frente(con.execute("SELECT * FROM frentes WHERE id = ?", (cur.lastrowid,)).fetchone())
        return {"id": f["id"], "frente": f}

    def _alterar_frente(self, con, fid: int, corpo: dict) -> dict:
        if con.execute("SELECT 1 FROM frentes WHERE id = ?", (fid,)).fetchone() is None:
            raise FrentesRecusou("Frentes embutido: frente inexistente")
        if "status" in corpo and corpo["status"] not in STATUS_FRENTE:
            raise FrentesRecusou("Frentes embutido: status de frente inválido")
        for campo in CAMPOS_FRENTE:
            if campo in corpo and corpo[campo] is not None:
                v = corpo[campo]
                if campo == "apelidos":
                    v = json.dumps(list(v), ensure_ascii=False)
                try:
                    con.execute(f"UPDATE frentes SET {campo} = ? WHERE id = ?", (v, fid))
                except sqlite3.IntegrityError:
                    raise FrentesRecusou("Frentes embutido: já existe frente com esse nome") from None
        return {"frente": self._frente(con.execute("SELECT * FROM frentes WHERE id = ?", (fid,)).fetchone())}

    # -- itens ----------------------------------------------------------------------------------------------
    def _itens(self, con, query: dict) -> list[dict]:
        sql, args = "SELECT * FROM itens WHERE 1 = 1", []
        fid = str(query.get("frente_id", "-1"))
        if fid in ("inbox", "null"):
            sql += " AND frente_id IS NULL"
        elif fid != "-1":
            sql += " AND frente_id = ?"
            args.append(int(fid))
        status = query.get("status", "aberto")   # como o Frentes: ausente = aberto; vazio = todos
        if status:
            sql += " AND status = ?"
            args.append(status)
        sql += " ORDER BY prioridade, COALESCE(prazo, '9999-12-31'), id"
        return [dict(r) for r in con.execute(sql, args)]

    def _criar_item(self, con, corpo: dict, *, origem_padrao: str) -> dict:
        texto = " ".join(str(corpo.get("texto") or "").split())
        if not texto:
            raise FrentesRecusou("Frentes embutido: texto vazio")
        fid = corpo.get("frente_id")
        if fid is not None and con.execute("SELECT 1 FROM frentes WHERE id = ?", (int(fid),)).fetchone() is None:
            fid = None
        cur = con.execute(
            "INSERT INTO itens (frente_id, tipo, texto, prazo, prioridade, origem, confirmado, criado_em) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
            (None if fid is None else int(fid), str(corpo.get("tipo") or "tarefa"), texto, corpo.get("prazo") or None,
             int(corpo.get("prioridade", 2)), str(corpo.get("origem") or origem_padrao), _agora()))
        return dict(con.execute("SELECT * FROM itens WHERE id = ?", (cur.lastrowid,)).fetchone())

    def _alterar_item(self, con, item_id: int, corpo: dict) -> dict:
        atual = con.execute("SELECT * FROM itens WHERE id = ?", (item_id,)).fetchone()
        if atual is None:
            raise FrentesRecusou("Frentes embutido: item inexistente")
        if "status" in corpo and corpo["status"] not in STATUS_ITEM:
            raise FrentesRecusou("Frentes embutido: status de item inválido")
        for campo in CAMPOS_ITEM:   # campos desconhecidos (ex.: nota) são descartados, como no Frentes
            if campo in corpo:
                con.execute(f"UPDATE itens SET {campo} = ? WHERE id = ?", (corpo[campo], item_id))
        if corpo.get("status") == "feito" and atual["status"] != "feito":
            con.execute("UPDATE itens SET feito_em = ? WHERE id = ?", (_agora(), item_id))
        return dict(con.execute("SELECT * FROM itens WHERE id = ?", (item_id,)).fetchone())

    # -- foto do painel -------------------------------------------------------------------------------------
    def _estado(self, con) -> dict:
        hoje = date.today().isoformat()
        abertos = [dict(r) for r in con.execute("SELECT * FROM itens WHERE status = 'aberto'")]
        feitos = {r[0]: r[1] for r in con.execute(
            "SELECT frente_id, COUNT(*) FROM itens WHERE status = 'feito' GROUP BY frente_id")}

        def atrasado(i):
            return bool(i.get("prazo")) and str(i["prazo"])[:10] < hoje

        frentes = []
        for f in self._frentes(con):
            meus = [i for i in abertos if i["frente_id"] == f["id"]]
            n_feitos = int(feitos.get(f["id"], 0))
            prazos = sorted(str(i["prazo"])[:10] for i in meus if i.get("prazo"))
            total = len(meus) + n_feitos
            frentes.append({**f, "n_abertos": len(meus), "n_feitos": n_feitos,
                            "n_atrasados": sum(1 for i in meus if atrasado(i)),
                            "n_urgentes": sum(1 for i in meus if i["prioridade"] == 1),
                            "n_confirmar": sum(1 for i in meus if not i["confirmado"]),
                            "proximo_prazo": prazos[0] if prazos else None,
                            "destaques": sorted(meus, key=lambda i: (i["prioridade"], i["id"]))[:2],
                            "progresso": round(100 * n_feitos / total) if total else 0})
        inbox = [i for i in abertos if i["frente_id"] is None]
        return {"gerado_em": _agora(), "hoje": hoje, "frentes": frentes, "inbox": inbox,
                "agenda": sorted((i for i in abertos if i.get("prazo")), key=lambda i: str(i["prazo"])),
                "totais": {"frentes": len(frentes), "abertos": len(abertos), "inbox": len(inbox),
                           "atrasados": sum(1 for i in abertos if atrasado(i))}}
