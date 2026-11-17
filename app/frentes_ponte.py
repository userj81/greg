"""Cliente HTTP do Frentes (GET frentes/estado/itens, POST captura, PATCH item) com header X-Frentes-Token.
Quem chama: bin/vigia-ponte, o coletor/lote e a página de painel, para subir itens e montar o contexto.md.
O que não faz: não decide classe nem frente, não guarda estado e nunca loga nem põe texto de mensagem em exceção.
"""
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from app import rede
from app.erros import FrentesIndisponivel, FrentesRecusou, NaoAutorizado

# Ignora proxy de ambiente (o Frentes é local) e não segue redirect (app/rede): 3xx vira FrentesIndisponivel.
_ABRIDOR = rede.ABRIDOR



SEM_PRAZO = object()   # pedido 86: `prazo` não dado na captura livre (o parser do Frentes tira do texto)

class FrentesPonte:
    """Fala com a API do Frentes (~/frentes) usando só urllib."""

    def __init__(self, url: str, token: str, timeout: float = 10.0):
        self.url = url.rstrip("/")
        self._token = token or ""
        self.timeout = timeout

    # -- transporte -------------------------------------------------------
    def _chamar(self, metodo: str, caminho: str, query: dict | None = None, corpo: dict | None = None):
        if not self._token:
            raise NaoAutorizado("Frentes: token ausente")
        alvo = self.url + caminho
        if query:
            alvo += "?" + urllib.parse.urlencode(query)
        dados = json.dumps(corpo, ensure_ascii=False).encode("utf-8") if corpo is not None else None
        req = urllib.request.Request(alvo, data=dados, method=metodo)
        req.add_header("X-Frentes-Token", self._token)
        req.add_header("Accept", "application/json")
        if dados is not None:
            req.add_header("Content-Type", "application/json; charset=utf-8")
        try:
            with _ABRIDOR.open(req, timeout=self.timeout) as r:
                bruto = r.read()
        except urllib.error.HTTPError as e:
            e.close()
            if e.code == 401:
                raise NaoAutorizado("Frentes: HTTP 401") from None
            if 400 <= e.code < 500:
                raise FrentesRecusou(f"Frentes: HTTP {e.code} em {metodo} {caminho}") from None
            raise FrentesIndisponivel(f"Frentes: HTTP {e.code} em {metodo} {caminho}") from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError):
            raise FrentesIndisponivel(f"Frentes: sem resposta em {metodo} {caminho}") from None
        try:
            return json.loads(bruto.decode("utf-8")) if bruto else {}
        except ValueError:
            raise FrentesIndisponivel(f"Frentes: resposta inválida em {metodo} {caminho}") from None

    # -- leitura ----------------------------------------------------------
    def frentes(self) -> list[dict]:
        """Lista as frentes."""
        r = self._chamar("GET", "/api/frentes")
        return r.get("frentes", []) if isinstance(r, dict) else list(r)

    def estado(self) -> dict:
        """Foto do painel. Shape real: {gerado_em, hoje: "AAAA-MM-DD", frentes: [{id, nome, tipo, apelidos,
        prioridade, n_abertos, n_feitos, n_atrasados, n_urgentes, n_confirmar, ...}], inbox: [item],
        agenda: [item], totais: {frentes, abertos, inbox, atrasados}}."""
        return self._chamar("GET", "/api/estado")

    def itens_abertos(self, *, origem: str | None = None, dias: int = 14) -> list[dict]:
        """Uma chamada GET /api/itens?status=aberto (todas as frentes + Inbox); filtra origem e,
        quando há filtro de origem, a janela de `dias` (só se o item traz criado_em)."""
        r = self._chamar("GET", "/api/itens", {"status": "aberto"})
        limite = datetime.now(timezone.utc) - timedelta(days=dias)
        saida = []
        for it in r.get("itens", []):
            if origem is not None:
                if it.get("origem") != origem or not _dentro_da_janela(it, limite):
                    continue
            saida.append(it)
        return saida

    def itens_da_frente(self, frente_id: int | None, status: str = "aberto") -> list[dict]:
        """GET /api/itens?frente_id=<id|inbox>&status=<aberto|feito>. Painel de frentes (D-36)."""
        fid = "inbox" if frente_id is None else str(int(frente_id))
        r = self._chamar("GET", "/api/itens", {"frente_id": fid, "status": status})
        return list(r.get("itens", [])) if isinstance(r, dict) else []

    # -- escrita ----------------------------------------------------------
    def alterar(self, item_id: int, **campos) -> dict:
        """PATCH genérico do painel (status feito/aberto, frente_id) e da substituição D-57 (texto, prazo). Só campos
        que o Frentes aceita (db.atualizar_item)."""
        permitidos = {"status", "frente_id", "confirmado", "prioridade", "prazo", "tipo", "texto",
                      "lembrete_em", "lembrete_enviado"}   # pedido 86: prazo novo leva o lembrete junto
        corpo = {k: v for k, v in campos.items() if k in permitidos}
        if not corpo:
            return {}
        return self._chamar("PATCH", f"/api/itens/{int(item_id)}", corpo=corpo)

    def captura_livre(self, texto: str, *, frente_id: int | None = None, origem: str = "vigia", prazo=SEM_PRAZO,
                      item_unico: bool = False) -> dict:
        """POST /api/captura: o Frentes roda o parser dele (frente por apelido, prazo em linguagem natural).
        É o que a caixa 'Fala aqui' do painel usa; o coletor NÃO usa isto (ele usa capturar(), sem parser).
        Pedido 86: `prazo` ("AAAA-MM-DD" ou None) desliga a data tirada do texto e faz um item só; `item_unico` faz um
        item só. Sem os dois, o corpo é o de sempre."""
        corpo = {"texto": texto, "origem": origem}
        if frente_id is not None:
            corpo["frente_id"] = int(frente_id)
        if prazo is not SEM_PRAZO:
            corpo["prazo"] = prazo
        if item_unico:
            corpo["item_unico"] = True
        r = self._chamar("POST", "/api/captura", corpo=corpo)
        return r if isinstance(r, dict) else {}

    def capturar(self, texto: str, *, frente_id: int | None, tipo: str = "tarefa", prazo: str | None = None,
                 prioridade: int = 2, origem: str = "whatsapp") -> dict:
        """POST /api/itens (sem o parser da /api/captura). Devolve {"itens": [item], "captura_id": id do item};
        frente_id inexistente cai no Inbox (None) e o retorno traz "frente_invalida": True."""
        invalida = False
        if frente_id is not None and frente_id not in {f["id"] for f in self.frentes()}:
            frente_id, invalida = None, True
        corpo = {"texto": texto, "frente_id": frente_id, "tipo": tipo, "prazo": prazo,
                 "prioridade": prioridade, "origem": origem}
        r = self._chamar("POST", "/api/itens", corpo=corpo)
        item = r.get("item") or {"id": r.get("id")}
        saida = {"itens": [item], "captura_id": r.get("id", item.get("id"))}
        if invalida:
            saida["frente_invalida"] = True
        return saida

    def criar_frente(self, nome: str, *, tipo: str = "cliente", contexto: str = "", apelidos=None,
                     prioridade: int = 2) -> dict:
        """POST /api/frentes (só o dono; o token da ponte é o dono). Devolve {"id", "frente"}; nome repetido ou tipo
        inválido = FrentesRecusou (409/400). Pedido 41: frentes Pessoal/Família criadas pelo Greg."""
        corpo = {"nome": " ".join(str(nome or "").split()), "tipo": tipo, "contexto": contexto or "",
                 "apelidos": list(apelidos or []), "prioridade": int(prioridade)}
        r = self._chamar("POST", "/api/frentes", corpo=corpo)
        return r if isinstance(r, dict) else {}

    def alterar_frente(self, frente_id: int, **campos) -> dict:
        """PATCH /api/frentes/{id}: só nome, tipo, contexto, status (ativa|pausada|arquivada), prioridade, ordem, apelidos.
        Arquivar = status "arquivada" (o Frentes não apaga nada: some das listas e dos cartões, volta com "ativa")."""
        permitidos = {"nome", "tipo", "contexto", "status", "prioridade", "ordem", "apelidos"}
        corpo = {k: v for k, v in campos.items() if k in permitidos and v is not None}
        if not corpo:
            return {}
        r = self._chamar("PATCH", f"/api/frentes/{int(frente_id)}", corpo=corpo)
        return r if isinstance(r, dict) else {}

    def confirmar(self, item_id: int) -> dict:
        """PATCH confirmado=1 (dono validou o item pelo painel; R-8). Costura F3."""
        return self._chamar("PATCH", f"/api/itens/{int(item_id)}", corpo={"confirmado": 1})

    def marcar_nao_confirmado(self, item_id: int) -> dict:
        """PATCH confirmado=0 (item sobe para revisão do dono). D-58: o Greg não usa mais isto para os cartões dele
        (nada nasce "a confirmar"); fica para o painel de Frentes e os testes de contrato."""
        return self._chamar("PATCH", f"/api/itens/{int(item_id)}", corpo={"confirmado": 0})

    def item_cancelado(self, item_id: int) -> bool:
        """O item está com status `cancelado` no Frentes? (GET /api/itens?status=cancelado). D-58: Restaurar um cartão
        cujo evento a IA cancelou reabre o item, mas só se ele estiver mesmo cancelado (feito não se reabre)."""
        r = self._chamar("GET", "/api/itens", {"status": "cancelado"})
        itens = r.get("itens", []) if isinstance(r, dict) else []
        return any(i.get("id") == int(item_id) for i in itens)

    def anotar(self, item_id: int, nota: str, *, prioridade: int | None = None) -> dict:
        """Só `prioridade` vai ao Frentes (a API real não tem campo nota; `nota` fica no vigia.db).
        Sem prioridade, não chama o Frentes e devolve {}."""
        if prioridade is None:
            return {}
        return self._chamar("PATCH", f"/api/itens/{int(item_id)}", corpo={"prioridade": prioridade})

    # -- contexto ---------------------------------------------------------
    def contexto_markdown(self) -> str:
        """Markdown compacto: frentes (id, nome, tipo, apelidos, n_abertos do /api/estado) e itens whatsapp 14 dias."""
        est = self.estado()
        frentes = est.get("frentes", [])
        linhas = ["## Frentes", ""]
        for f in frentes:
            apel = ", ".join(f.get("apelidos") or []) or "-"
            linhas.append(f"- id {f['id']} · {f.get('nome', '?')} · {f.get('tipo', '?')} · "
                          f"apelidos: {apel} · abertos: {f.get('n_abertos', 0)}")
        nomes = {f["id"]: f.get("nome", "?") for f in frentes}
        zap = self.itens_abertos(origem="whatsapp")
        linhas += ["", "## Itens do WhatsApp (14 dias)", ""]
        if not zap:
            linhas.append("- (nenhum)")
        for it in zap:
            fid = it.get("frente_id")
            frente = "Inbox" if fid is None else nomes.get(fid, "?")
            texto = " ".join(str(it.get("texto", "")).split())
            linhas.append(f"- #{it.get('id')} [{frente}] {texto}")
        return "\n".join(linhas) + "\n"


def _dentro_da_janela(item: dict, limite: datetime) -> bool:
    """True se o item não traz data legível ou se a data é posterior ao limite."""
    bruto = item.get("criado_em") or item.get("criado")
    if not bruto:
        return True
    try:
        dt = datetime.fromisoformat(str(bruto).replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt >= limite


def embutido(url) -> bool:
    """FRENTES_URL vazio ou "embutido" = Frentes embutido (app/frentes_local.py, SQLite em dados/)."""
    return str(url or "").strip().lower() in ("", "embutido")


def criar_ponte(url, token, dados, *, padrao_url: str | None = None):
    """FrentesPonte (HTTP) quando há FRENTES_URL; senão o Frentes embutido em <dados>/frentes.db.
    `padrao_url`: compatibilidade com quem já usava um Frentes local sem pôr a URL no .env (só vale com token)."""
    if embutido(url) and padrao_url and token:
        url = padrao_url
    if embutido(url):
        from app.frentes_local import FrentesLocal
        from pathlib import Path
        return FrentesLocal(Path(dados) / "frentes.db")
    return FrentesPonte(url, token)
