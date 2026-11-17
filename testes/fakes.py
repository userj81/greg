"""Servidores HTTP falsos (Evolution v2.3.7 e Frentes) e fixtures compartilhadas pelos testes.
Quem chama: todo testes/test_*.py; nenhum teste fala com a Evolution ou o Frentes reais.
O que não faz: não imita regra de negócio além do contrato; nunca registra texto de mensagem em log.
"""
import base64
import json
import re
import struct
import threading
import time
import zlib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


# ---------------------------------------------------------------------------
# Fixtures de bytes
# ---------------------------------------------------------------------------

def imagem_jpeg_minima() -> bytes:
    """JPEG 1x1 em tons de cinza: começa em FFD8FF e termina em FFD9."""
    def seg(marca: int, dados: bytes) -> bytes:
        return bytes([0xFF, marca]) + struct.pack(">H", len(dados) + 2) + dados

    soi = b"\xff\xd8"
    app0 = seg(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
    dqt = seg(0xDB, b"\x00" + b"\x01" * 64)
    sof0 = seg(0xC0, b"\x08\x00\x01\x00\x01\x01\x01\x11\x00")
    dht_dc = seg(0xC4, b"\x00" + b"\x01" + b"\x00" * 15 + b"\x00")
    dht_ac = seg(0xC4, b"\x10" + b"\x01" + b"\x00" * 15 + b"\x00")
    sos = seg(0xDA, b"\x01\x01\x00\x00\x3f\x00")
    return soi + app0 + dqt + sof0 + dht_dc + dht_ac + sos + b"\x00" + b"\xff\xd9"


def _png_minimo() -> bytes:
    """PNG 1x1 branco (usado como 'QR' do fake)."""
    def chunk(tipo: bytes, dados: bytes) -> bytes:
        return (struct.pack(">I", len(dados)) + tipo + dados
                + struct.pack(">I", zlib.crc32(tipo + dados) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)
    idat = zlib.compress(b"\x00\xff")
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def fila_fixture(n: int, inicio_ts: int) -> list[dict]:
    """Linhas no formato da §E1 (vigia.fila junto de "Message"), uma por mensagem.

    Variações determinísticas: tipo cicla conversation/imageMessage/audioMessage (i % 3);
    i % 6 == 3 vira mensagem de grupo (@g.us); i % 6 == 4 vira fromMe=True.
    Com n <= 3 não há grupo nem fromMe. messageTimestamp = inicio_ts + 60 * i (epoch segundos).
    """
    tipos = ("conversation", "imageMessage", "audioMessage")
    linhas = []
    for i in range(n):
        tipo = tipos[i % 3]
        grupo = i % 6 == 3
        de_mim = i % 6 == 4
        jid = f"1203630{i:08d}@g.us" if grupo else f"55929{80000000 + i:08d}@s.whatsapp.net"
        if tipo == "conversation":
            message = {"conversation": f"texto de teste {i}"}
        elif tipo == "imageMessage":
            message = {"imageMessage": {"mimetype": "image/jpeg", "caption": f"legenda {i}",
                                        "url": "https://mmg.whatsapp.net/fake", "fileLength": "1234"}}
        else:
            message = {"audioMessage": {"mimetype": "audio/ogg; codecs=opus", "seconds": 40, "ptt": True}}
        linhas.append({
            "seq": i + 1,
            "id": f"cm{i:010d}fake",
            "key": {"id": f"3EB0FAKE{i:08d}", "remoteJid": jid, "fromMe": de_mim},
            "pushName": f"Contato {i}",
            "messageType": tipo,
            "message": message,
            "messageTimestamp": inicio_ts + 60 * i,
        })
    return linhas


# ---------------------------------------------------------------------------
# Base dos servidores
# ---------------------------------------------------------------------------

class _Manipulador(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    servidor_fake = None  # preenchido na subclasse criada em iniciar()

    def log_message(self, formato, *args):  # silêncio: nenhum log com texto
        pass

    def _despachar(self, metodo: str) -> None:
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        tamanho = int(self.headers.get("Content-Length") or 0)
        bruto = self.rfile.read(tamanho) if tamanho else b""
        try:
            corpo = json.loads(bruto.decode("utf-8")) if bruto else {}
        except ValueError:
            corpo = {}
        status, obj = self.servidor_fake._tratar(metodo, url.path, query, corpo, self.headers)
        dados = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)

    def do_GET(self):
        self._despachar("GET")

    def do_POST(self):
        self._despachar("POST")

    def do_PATCH(self):
        self._despachar("PATCH")

    def do_PUT(self):
        self._despachar("PUT")

    def do_DELETE(self):
        self._despachar("DELETE")


class _ServidorFake:
    """Sobe um ThreadingHTTPServer em 127.0.0.1 (porta 0 = livre) numa thread daemon."""

    def __init__(self, porta: int = 0):
        self._porta_pedida = porta
        self._trava = threading.RLock()
        self._httpd = None
        self._thread = None

    def iniciar(self):
        fake = self

        class Manipulador(_Manipulador):
            servidor_fake = fake

        self._httpd = ThreadingHTTPServer(("127.0.0.1", self._porta_pedida), Manipulador)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self._thread.start()
        return self

    def parar(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._thread.join(timeout=5)
            self._httpd = None

    @property
    def porta(self) -> int:
        return self._httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.porta}"

    def __enter__(self):
        return self.iniciar()

    def __exit__(self, *exc):
        self.parar()

    def _tratar(self, metodo, caminho, query, corpo, cabecalhos):  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# FakeEvolution
# ---------------------------------------------------------------------------

class FakeEvolution(_ServidorFake):
    """Imita a Evolution v2.3.7 (rotas do CONTRATOS.md).

    Atributos: apikey (exigida no header `apikey`), instancia, estado ('open|close|connecting'),
    qrs_gerados, chamadas (lista de (método, caminho) das requisições autorizadas),
    negadas (nº de 401), reason_code (int|None; ao definir, grava disconnectionAt = agora),
    enviadas (corpos de sendText), midia_base64_texto (se definido, devolvido no lugar do JPEG).
    """

    def __init__(self, porta: int = 0, apikey: str = "chave-fake-evolution", instancia: str = "vigia"):
        super().__init__(porta)
        self.apikey = apikey
        self.instancia = instancia
        self.estado = "close"
        self.qrs_gerados = 0
        self.chamadas: list[tuple[str, str]] = []
        self.negadas = 0
        self.enviadas: list[dict] = []
        self.midia_base64_texto: str | None = None
        # r02 (Conversas): grupos (findGroupInfos, um por vez), números com WhatsApp, URL de foto, mídia convertida em MP4
        self.grupos: list[dict] = []
        self.convites: dict[str, dict] = {}   # código do link -> dados do grupo (inviteInfo)
        self.grupos_consultados: list[str] = []   # groupJid de cada findGroupInfos (D-41: bloqueado nunca entra)
        self.numeros: dict[str, bool] = {}
        self.fotos: dict[str, str] = {}
        self.midia_corpos: list[dict] = []
        # pedido 50: bytes por id de mensagem (vídeo, documento) e o que saiu por sendMedia / sendWhatsAppAudio
        self.midia_por_id: dict[str, bytes] = {}
        self.midias_enviadas: list[dict] = []
        self.audios_enviados: list[dict] = []
        self.reacoes_enviadas: list[dict] = []   # corpos de sendReaction (pedido 50, rodada 2)
        self.edicoes_enviadas: list[dict] = []   # corpos de updateMessage (pedido 50, rodada 3)
        self.apagadas_enviadas: list[dict] = []  # corpos de deleteMessageForEveryone (D-63)
        self.falhar_midia: int | None = None   # código HTTP para sendMedia/sendWhatsAppAudio (500 = envio incerto)
        self.recusar_envio = False   # D-44: True = sendText responde 400 (nada sai), para testar a pausa da fila
        self.sonda_falha = False     # pedido 41: True = fetchProfilePictureUrl responde 500 (socket zumbi visto pela sonda)
        self._reason_code = None
        self.reason_em: str | None = None

    @property
    def reason_code(self):
        return self._reason_code

    @reason_code.setter
    def reason_code(self, valor):
        self._reason_code = valor
        self.reason_em = datetime.now(timezone.utc).isoformat() if valor is not None else None

    def _tratar(self, metodo, caminho, query, corpo, cabecalhos):
        with self._trava:
            if cabecalhos.get("apikey") != self.apikey:
                self.negadas += 1
                return 401, {"status": 401, "error": "Unauthorized", "response": {"message": "Unauthorized"}}
            self.chamadas.append((metodo, caminho))
            m = re.fullmatch(r"/instance/connectionState/([^/]+)", caminho)
            if metodo == "GET" and m:
                return 200, {"instance": {"instanceName": m.group(1), "state": self.estado}}
            m = re.fullmatch(r"/instance/connect/([^/]+)", caminho)
            if metodo == "GET" and m:
                self.qrs_gerados += 1
                png = base64.b64encode(_png_minimo()).decode("ascii")
                return 200, {"base64": "data:image/png;base64," + png, "code": f"2@fakeqr{self.qrs_gerados}"}
            m = re.fullmatch(r"/instance/logout/([^/]+)", caminho)
            if metodo == "DELETE" and m:
                self.estado = "close"
                return 200, {"status": "SUCCESS", "error": False, "response": {"message": "Instance logged out"}}
            if metodo == "GET" and caminho == "/instance/fetchInstances":
                nome = query.get("instanceName", self.instancia)
                return 200, [{"name": nome, "connectionStatus": self.estado,
                              "disconnectionReasonCode": self._reason_code,
                              "disconnectionAt": self.reason_em}]
            m = re.fullmatch(r"/chat/getBase64FromMediaMessage/([^/]+)", caminho)
            if metodo == "POST" and m:
                self.midia_corpos.append(corpo)
                proprio = self.midia_por_id.get(str(((corpo.get("message") or {}).get("key") or {}).get("id")))
                if proprio is not None:
                    return 200, {"base64": base64.b64encode(proprio).decode("ascii")}
                if corpo.get("convertToMp4"):
                    mp4 = b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00M4A mp42isom" + b"\x00" * 64
                    return 200, {"base64": base64.b64encode(mp4).decode("ascii"), "mimetype": "audio/mp4"}
                b64 = self.midia_base64_texto or base64.b64encode(imagem_jpeg_minima()).decode("ascii")
                return 200, {"base64": b64, "mimetype": "image/jpeg"}
            m = re.fullmatch(r"/group/fetchAllGroups/([^/]+)", caminho)
            if metodo == "GET" and m:  # D-40: o Greg não pode chamar isto; o teste confere em self.chamadas
                return 200, list(self.grupos)
            m = re.fullmatch(r"/group/findGroupInfos/([^/]+)", caminho)
            if metodo == "GET" and m:
                self.grupos_consultados.append(str(query.get("groupJid")))
                for g in self.grupos:
                    if g.get("id") == query.get("groupJid"):
                        return 200, dict(g)
                return 404, {"status": 404, "error": "Not Found", "response": {"message": "Group not found"}}
            m = re.fullmatch(r"/group/inviteInfo/([^/]+)", caminho)
            if metodo == "GET" and m:
                g = self.convites.get(query.get("inviteCode", ""))
                return (200, dict(g)) if g else (400, {"status": 400, "error": "Bad Request", "response": {"message": "No invite info"}})
            m = re.fullmatch(r"/chat/whatsappNumbers/([^/]+)", caminho)
            if metodo == "POST" and m:
                saida = []
                for n in corpo.get("numbers") or []:
                    existe = self.numeros.get(str(n), False)
                    saida.append({"exists": existe, "jid": f"{n}@s.whatsapp.net" if existe else "", "number": str(n)})
                return 200, saida
            m = re.fullmatch(r"/chat/fetchProfilePictureUrl/([^/]+)", caminho)
            if metodo == "POST" and m:
                if self.sonda_falha:
                    return 500, {"status": 500, "error": "Internal Server Error", "response": {"message": "Timed Out"}}
                return 200, {"wuid": corpo.get("number"), "profilePictureUrl": self.fotos.get(corpo.get("number"))}
            m = re.fullmatch(r"/message/sendText/([^/]+)", caminho)
            if metodo == "POST" and m:
                if self.recusar_envio:
                    return 400, {"status": 400, "error": "Bad Request", "response": {"message": ["recusado"]}}
                self.enviadas.append(corpo)
                return 201, {"key": {"id": f"3EB0{len(self.enviadas):020X}"}}
            m = re.fullmatch(r"/chat/deleteMessageForEveryone/([^/]+)", caminho)
            if metodo == "DELETE" and m:
                if self.recusar_envio:
                    return 400, {"status": 400, "error": "Bad Request", "response": {"message": ["recusado"]}}
                self.apagadas_enviadas.append(corpo)
                return 201, {"key": {"id": f"3EB4{len(self.apagadas_enviadas):020X}"}}
            m = re.fullmatch(r"/chat/updateMessage/([^/]+)", caminho)
            if metodo == "POST" and m:
                if self.recusar_envio:
                    return 400, {"status": 400, "error": "Bad Request", "response": {"message": ["recusado"]}}
                self.edicoes_enviadas.append(corpo)
                return 201, {"key": {"id": f"3EB3{len(self.edicoes_enviadas):020X}"}}
            m = re.fullmatch(r"/message/sendReaction/([^/]+)", caminho)
            if metodo == "POST" and m:
                if self.recusar_envio:
                    return 400, {"status": 400, "error": "Bad Request", "response": {"message": ["recusado"]}}
                self.reacoes_enviadas.append(corpo)
                return 201, {"key": {"id": f"3EB2{len(self.reacoes_enviadas):020X}"}}
            m = re.fullmatch(r"/message/(sendMedia|sendWhatsAppAudio)/([^/]+)", caminho)
            if metodo == "POST" and m:
                if self.falhar_midia:
                    return self.falhar_midia, {"status": self.falhar_midia, "error": "erro", "response": {"message": ["falhou"]}}
                destino = self.midias_enviadas if m.group(1) == "sendMedia" else self.audios_enviados
                destino.append(corpo)
                return 201, {"key": {"id": f"3EB1{len(self.midias_enviadas) + len(self.audios_enviados):020X}"}}
            return 404, {"status": 404, "error": "Not Found"}


# ---------------------------------------------------------------------------
# FakeFrentes
# ---------------------------------------------------------------------------

class FakeFrentes(_ServidorFake):
    """Imita ~/frentes/app/server.py (rotas do CONTRATOS.md).

    Atributos: token (exigido em `X-Frentes-Token`), frentes (lista), itens ({id: dict}),
    capturas (corpos de POST /api/captura), patches (lista de {"id": int, "corpo": dict} por PATCH),
    chamadas ((método, caminho) autorizadas), negadas (nº de 401).
    POST /api/itens (real: 201 {id,item}; 400 texto vazio) e POST /api/captura (legado) criam item confirmado=1.
    GET /api/itens: sem frente_id = todas + Inbox; frente_id=inbox só Inbox; status ausente = aberto.
    PATCH descarta campos desconhecidos (ex.: nota) e responde {item}; `patches` guarda o corpo cru recebido.
    """

    def __init__(self, porta: int = 0, token: str = "token-fake-frentes"):
        super().__init__(porta)
        self.token = token
        self.frentes = [
            {"id": 1, "nome": "Agência", "tipo": "trabalho", "apelidos": ["agencia", "exemplo"],
             "prioridade": 1, "status": "ativa"},
            {"id": 2, "nome": "Casa", "tipo": "pessoal", "apelidos": ["casa"],
             "prioridade": 2, "status": "ativa"},
        ]
        self.itens: dict[int, dict] = {}
        self.capturas: list[dict] = []
        self.patches: list[dict] = []
        self.frentes_patches: list[dict] = []   # pedido 41: {"id", "corpo"} de cada PATCH /api/frentes/{id}
        self.chamadas: list[tuple[str, str]] = []
        self.negadas = 0
        self._proximo_item = 1
        self._proxima_captura = 1

    def _ativas(self) -> list[dict]:
        """Como o real (db.frentes()): arquivada não aparece em /api/frentes nem em /api/estado."""
        return [f for f in self.frentes if f.get("status", "ativa") != "arquivada"]

    def semear_item(self, **campos) -> dict:
        """Cria um item direto no estado do fake (sem passar por captura)."""
        with self._trava:
            item = {"id": self._proximo_item, "frente_id": None, "tipo": "tarefa", "texto": "",
                    "prazo": None, "prioridade": 2, "origem": "manual", "confirmado": 1, "status": "aberto"}
            item.update(campos)
            self.itens[item["id"]] = item
            self._proximo_item += 1
            return dict(item)

    def _estado(self) -> dict:
        abertos = [i for i in self.itens.values() if i["status"] == "aberto"]
        frentes = []
        for f in self._ativas():
            meus = [i for i in abertos if i["frente_id"] == f["id"]]
            frentes.append({**f, "n_abertos": len(meus),
                            "n_feitos": sum(1 for i in self.itens.values()
                                            if i["frente_id"] == f["id"] and i["status"] == "feito"),
                            "n_atrasados": 0, "n_urgentes": sum(1 for i in meus if i["prioridade"] == 1),
                            "n_confirmar": sum(1 for i in meus if not i["confirmado"]),
                            "proximo_prazo": None, "destaques": meus[:2], "progresso": 0})
        inbox = [i for i in abertos if i["frente_id"] is None]
        return {"gerado_em": datetime.now(timezone.utc).isoformat(), "hoje": datetime.now().date().isoformat(),
                "frentes": frentes, "inbox": inbox,
                "agenda": [i for i in abertos if i.get("prazo")],
                "totais": {"frentes": len(frentes), "abertos": len(abertos), "inbox": len(inbox), "atrasados": 0}}

    def _tratar(self, metodo, caminho, query, corpo, cabecalhos):
        with self._trava:
            if cabecalhos.get("X-Frentes-Token") != self.token:
                self.negadas += 1
                return 401, {"erro": "não autorizado"}
            self.chamadas.append((metodo, caminho))
            if metodo == "GET" and caminho == "/api/frentes":
                return 200, {"frentes": self._ativas()}
            if metodo == "POST" and caminho == "/api/frentes":   # real: 201 {id, frente}; 400 nome vazio; 409 nome repetido
                nome = str(corpo.get("nome") or "").strip()
                if not nome:
                    return 400, {"erro": "nome da frente vazio"}
                if any(f["nome"] == nome for f in self.frentes):
                    return 409, {"erro": "não criou: UNIQUE constraint failed: frentes.nome"}
                nova = {"id": max((f["id"] for f in self.frentes), default=0) + 1, "nome": nome,
                        "tipo": corpo.get("tipo", "cliente"), "apelidos": list(corpo.get("apelidos") or []),
                        "prioridade": int(corpo.get("prioridade", 2)), "status": "ativa", "contexto": corpo.get("contexto", "")}
                self.frentes.append(nova)
                return 201, {"id": nova["id"], "frente": dict(nova)}
            m = re.fullmatch(r"/api/frentes/(\d+)", caminho)
            if metodo in ("PATCH", "POST") and m:   # real: PATCH e POST tratados igual; 404 inexistente; 400 status inválido
                fid = int(m.group(1))
                f = next((x for x in self.frentes if x["id"] == fid), None)
                if f is None:
                    return 404, {"erro": "frente inexistente"}
                if "status" in corpo and corpo["status"] not in ("ativa", "pausada", "arquivada"):
                    return 400, {"erro": f"status inválido: {corpo['status']}"}
                self.frentes_patches.append({"id": fid, "corpo": corpo})
                for campo in ("nome", "tipo", "contexto", "status", "prioridade", "ordem", "apelidos"):
                    if campo in corpo and corpo[campo] is not None:
                        f[campo] = corpo[campo]
                return 200, {"frente": dict(f)}
            if metodo == "GET" and caminho == "/api/estado":
                return 200, self._estado()
            if metodo == "GET" and caminho == "/api/itens":
                itens = list(self.itens.values())
                fid = query.get("frente_id", "-1")
                if fid in ("inbox", "null"):
                    itens = [i for i in itens if i["frente_id"] is None]
                elif fid != "-1":
                    itens = [i for i in itens if str(i["frente_id"]) == fid]
                status = query.get("status", "aberto")  # como o real: ausente = aberto; vazio = todos
                if status:
                    itens = [i for i in itens if i["status"] == status]
                return 200, {"itens": itens}
            if metodo == "POST" and caminho == "/api/itens":
                texto = (corpo.get("texto") or "").strip()
                if not texto:
                    return 400, {"erro": "texto vazio"}
                self.capturas.append(corpo)
                item = self.semear_item(texto=texto, origem=corpo.get("origem", "api"),
                                        frente_id=corpo.get("frente_id"), tipo=corpo.get("tipo", "tarefa"),
                                        prazo=corpo.get("prazo"), prioridade=int(corpo.get("prioridade", 2)),
                                        confirmado=1)
                return 201, {"id": item["id"], "item": item}
            if metodo == "POST" and caminho == "/api/captura":  # legado: o real roda parser; o fake só cria 1 item
                texto = (corpo.get("texto") or "").strip()
                if not texto:
                    return 400, {"erro": "nenhum item"}
                self.capturas.append(corpo)
                item = self.semear_item(texto=texto, origem=corpo.get("origem", "manual"),
                                        frente_id=corpo.get("frente_id"), confirmado=1)
                captura_id = self._proxima_captura
                self._proxima_captura += 1
                return 200, {"itens": [item], "captura_id": captura_id}
            m = re.fullmatch(r"/api/itens/(\d+)", caminho)
            if metodo == "PATCH" and m:
                item_id = int(m.group(1))
                item = self.itens.get(item_id)
                if item is None:
                    return 404, {"erro": "item inexistente"}
                self.patches.append({"id": item_id, "corpo": corpo})  # corpo cru: o que a ponte mandou
                for campo in ("frente_id", "tipo", "texto", "status", "prioridade", "prazo", "confirmado"):
                    if campo in corpo:  # campos desconhecidos (ex.: nota) são descartados, como no real
                        item[campo] = corpo[campo]
                return 200, {"item": dict(item)}
            return 404, {"erro": "rota inexistente"}


def agora_epoch() -> int:
    """Epoch em segundos (atalho para os testes montarem fixtures)."""
    return int(time.time())
