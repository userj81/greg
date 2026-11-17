"""Cliente HTTP (stdlib) da Evolution v2.3.7: estado, QR, logout, mídia e envio de texto, mídia e áudio de voz.
Quem chama: app/conexao.py, bin/coletor.py e o painel (/enviar); a apikey vai só no header, em memória.
O que não faz: não gera QR em estado(), não loga texto de mensagem e não usa dependência externa.
"""
import base64
import binascii
import http.client
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

from app import rede
from app.erros import (EnvioIncerto, EvolutionIndisponivel, MidiaInvalida, NaoAutorizado,
                       QRIndisponivel, VigiaErro)

log = logging.getLogger("vigia.evolution")

_HOSTS_OK = ("127.0.0.1", "localhost")
TIMEOUT_MIDIA = 60.0          # baixar vídeo/documento: a Evolution busca no WhatsApp e devolve em base64
TIMEOUT_ENVIO_MIDIA = 90.0    # subir mídia: conversão (ffmpeg/sharp) + upload do lado dela


# Sem proxy de ambiente e sem seguir redirect (app/rede): a Evolution é sempre 127.0.0.1; 3xx vira EvolutionIndisponivel.
_ABRIDOR = rede.ABRIDOR


def _chave(key: dict) -> dict:
    """A key como a Evolution espera: remoteJid, fromMe booleano, id e, em grupo, o participante."""
    saida = {"remoteJid": str(key.get("remoteJid") or ""), "fromMe": key.get("fromMe") in (True, "true", 1),
             "id": str(key.get("id") or "")}
    if key.get("participant"):
        saida["participant"] = str(key["participant"])
    return saida


class EvolutionClient:
    def __init__(self, url: str, instancia: str, apikey: str, timeout: float = 15.0):
        partes = urllib.parse.urlsplit(url)
        hosts = _HOSTS_OK + (("api",) if os.environ.get("VIGIA_CONTAINER") == "1" else ())   # Docker: o serviço "api" do compose
        if partes.scheme not in ("http", "https") or partes.hostname not in hosts:
            raise VigiaErro("URL da Evolution recusada: o host precisa ser 127.0.0.1 ou localhost")
        chave = (apikey or "").strip()
        if not chave or any(not (0x21 <= ord(c) <= 0x7E) for c in chave):
            raise VigiaErro("apikey inválida: vazia ou com caractere fora do ASCII visível")
        self._url = url.rstrip("/")
        self._instancia = instancia
        self._apikey = chave
        self._timeout = timeout

    def _pedir(self, metodo: str, caminho: str, corpo: dict | None = None, *,
               tolerar: dict | None = None, invalido: tuple = (), incerto: bool = False,
               lista_ok: bool = False, timeout: float | None = None):
        """Requisição JSON. `tolerar` {código: (trechos do corpo)} devolve None; `invalido` (códigos)
        vira MidiaInvalida; `incerto` marca falha pós-escrita como EnvioIncerto."""
        dados = json.dumps(corpo).encode("utf-8") if corpo is not None else None
        req = urllib.request.Request(self._url + caminho, data=dados, method=metodo)
        req.add_header("apikey", self._apikey)
        if dados is not None:
            req.add_header("Content-Type", "application/json")
        rotulo = f"{metodo} {caminho.split('?')[0]}"
        try:
            with _ABRIDOR.open(req, timeout=timeout or self._timeout) as r:
                bruto = r.read()
        except urllib.error.HTTPError as e:
            codigo = e.code
            try:
                texto = e.read(4096).decode("utf-8", "replace").lower()
            except Exception:  # corpo ilegível não muda a decisão
                texto = ""
            e.close()
            if codigo == 401:
                raise NaoAutorizado("Evolution recusou a apikey (401)") from None
            trechos = (tolerar or {}).get(codigo)
            if trechos is not None and (not trechos or any(t in texto for t in trechos)):
                return None
            if codigo in invalido:
                raise MidiaInvalida(f"Evolution respondeu HTTP {codigo} em {rotulo}") from None
            if codigo >= 500 and incerto:
                raise EnvioIncerto(f"HTTP {codigo} em {rotulo}: a mensagem pode ter sido enviada") from None
            if codigo >= 500 or 300 <= codigo < 400:
                raise EvolutionIndisponivel(f"Evolution respondeu HTTP {codigo} em {rotulo}") from None
            raise VigiaErro(f"Evolution respondeu HTTP {codigo} em {rotulo}") from None
        except urllib.error.URLError as e:
            # Falha ao conectar/enviar: nada saiu (exceto reset explícito, tratado abaixo).
            if incerto and isinstance(e.reason, ConnectionResetError):
                raise EnvioIncerto(f"conexão reiniciada em {rotulo}: a mensagem pode ter sido enviada") from None
            raise EvolutionIndisponivel(f"Evolution inacessível em {rotulo}: {type(e.reason).__name__}") from None
        except (OSError, TimeoutError, http.client.HTTPException, ValueError) as e:
            if incerto:
                raise EnvioIncerto(f"{type(e).__name__} em {rotulo}: a mensagem pode ter sido enviada") from None
            raise EvolutionIndisponivel(f"Evolution inacessível em {rotulo}: {type(e).__name__}") from None
        try:
            obj = json.loads(bruto.decode("utf-8")) if bruto else {}
        except ValueError:
            if incerto:
                raise EnvioIncerto(f"resposta ilegível em {rotulo}: a mensagem pode ter sido enviada") from None
            raise EvolutionIndisponivel("resposta inesperada") from None
        if not isinstance(obj, dict) and not (lista_ok and isinstance(obj, list)):
            if incerto:
                raise EnvioIncerto(f"resposta inesperada em {rotulo}: a mensagem pode ter sido enviada") from None
            raise EvolutionIndisponivel("resposta inesperada")
        return obj

    def _i(self) -> str:
        return urllib.parse.quote(self._instancia, safe="")

    def estado(self) -> dict:
        """{"state","reason_code","reason_em"}; só leitura, nunca chama /instance/connect."""
        r = self._pedir("GET", f"/instance/connectionState/{self._i()}")
        state = ((r or {}).get("instance") or {}).get("state") or (r or {}).get("state") or "close"
        lista = self._pedir("GET", "/instance/fetchInstances?instanceName=" + self._i(), lista_ok=True)
        if isinstance(lista, dict):
            lista = [lista]
        reason_code, reason_em = None, None
        for inst in lista or []:
            if not isinstance(inst, dict):
                continue
            if inst.get("name", inst.get("instanceName", self._instancia)) == self._instancia:
                reason_code = inst.get("disconnectionReasonCode")
                reason_em = inst.get("disconnectionAt")
                break
        return {"state": state, "reason_code": reason_code, "reason_em": reason_em}

    def gerar_qr(self) -> dict:
        """Exatamente 1 GET /instance/connect (consome 1 QR); devolve {"base64","code"}."""
        r = self._pedir("GET", f"/instance/connect/{self._i()}") or {}
        b64 = r.get("base64") or ""
        if not b64 or r.get("error"):
            exc = QRIndisponivel("Evolution não devolveu QR (já conectada, QR não pronto ou erro)")
            inst = r.get("instance")
            exc.state = r.get("state") or (inst.get("state") if isinstance(inst, dict) else None)
            raise exc
        return {"base64": b64, "code": r.get("code") or ""}

    def logout(self) -> None:
        """DELETE /instance/logout; idempotente (instância já fora = 400/404 tolerado)."""
        self._pedir("DELETE", f"/instance/logout/{self._i()}", tolerar={404: (), 400: ("not connected", "already")})

    def _midia(self, key: dict, *, converter_mp4: bool, timeout: float | None = None, interna: dict | None = None) -> bytes:
        """POST getBase64FromMediaMessage já decodificado (sem validar o tipo); base64 ilegível = MidiaInvalida."""
        r = self._pedir("POST", f"/chat/getBase64FromMediaMessage/{self._i()}",
                        {"message": {"key": key, "message": interna} if interna else {"key": key},   # álbum: pedido 56
                         "convertToMp4": bool(converter_mp4)}, invalido=(400, 404),
                        timeout=timeout) or {}
        b64 = r.get("base64") or ""
        if b64.startswith("data:") and "," in b64:
            b64 = b64.split(",", 1)[1]
        try:
            return base64.b64decode(b64, validate=False)
        except (binascii.Error, ValueError):
            raise MidiaInvalida(f"base64 ilegível para a mensagem {key.get('id')}") from None

    @staticmethod
    def _tipo_audio(dados: bytes) -> str | None:
        if dados[4:8] == b"ftyp":
            return "audio/mp4"
        if dados.startswith(b"OggS"):
            return "audio/ogg"
        if dados.startswith(b"ID3") or dados[:2] in (b"\xff\xfb", b"\xff\xf3"):
            return "audio/mpeg"
        return None

    def midia_base64(self, key: dict, *, interna: dict | None = None) -> bytes:
        """Baixa a mídia da mensagem e valida os magic bytes de imagem; senão MidiaInvalida."""
        dados = self._midia(key, converter_mp4=False, interna=interna)
        ok = (dados.startswith(b"\xff\xd8\xff") or dados.startswith(b"\x89PNG")
              or (dados.startswith(b"RIFF") and dados[8:12] == b"WEBP"))
        if not ok:
            raise MidiaInvalida(f"conteúdo sem magic bytes de imagem para a mensagem {key.get('id')}")
        return dados

    def midia_bytes(self, key: dict, *, audio: bool = False, interna: dict | None = None) -> tuple[bytes, str]:
        """Mídia para a conversa aberta (r02): imagem/figurinha como midia_base64; áudio convertido em MP4 pela própria
        Evolution (o WebKit não toca Ogg/Opus em todas as versões). Devolve (bytes, content-type); senão MidiaInvalida."""
        if not audio:
            dados = self.midia_base64(key, interna=interna)
            tipo = "image/png" if dados.startswith(b"\x89PNG") else (
                "image/webp" if dados.startswith(b"RIFF") else "image/jpeg")
            return dados, tipo
        dados = self._midia(key, converter_mp4=True)
        tipo = self._tipo_audio(dados)
        if tipo is None:
            raise MidiaInvalida(f"conteúdo sem magic bytes de áudio para a mensagem {key.get('id')}")
        return dados, tipo

    def midia_video(self, key: dict, *, interna: dict | None = None) -> tuple[bytes, str]:
        """Pedido 50: o vídeo da mensagem para tocar no painel. Só MP4/MOV (caixa ftyp); senão MidiaInvalida."""
        dados = self._midia(key, converter_mp4=False, timeout=TIMEOUT_MIDIA, interna=interna)
        if dados[4:8] != b"ftyp":
            raise MidiaInvalida(f"conteúdo sem magic bytes de vídeo para a mensagem {key.get('id')}")
        return dados, "video/mp4"

    def midia_documento(self, key: dict) -> bytes:
        """Pedido 50: o arquivo da mensagem, cru. Não valida o tipo: o painel só o entrega como download
        (application/octet-stream + attachment), nunca o abre na própria origem."""
        dados = self._midia(key, converter_mp4=False, timeout=TIMEOUT_MIDIA)
        if not dados:
            raise MidiaInvalida(f"documento vazio para a mensagem {key.get('id')}")
        return dados

    def midia_audio(self, key: dict) -> tuple[bytes, str]:
        """D-47: o áudio CRU (ogg/opus como chegou, sem converter) para o motor de transcrição.
        Devolve (bytes, extensão ogg|m4a|mp3); senão MidiaInvalida."""
        dados = self._midia(key, converter_mp4=False)
        tipo = self._tipo_audio(dados)
        if tipo is None:
            raise MidiaInvalida(f"conteúdo sem magic bytes de áudio para a mensagem {key.get('id')}")
        return dados, {"audio/mp4": "m4a", "audio/ogg": "ogg", "audio/mpeg": "mp3"}[tipo]

    # D-40: NÃO existe método para /group/fetchAllGroups. No Baileys da 2.3.7 ele emite `groups.update` com TODOS os
    # grupos e a Evolution responde consultando cada um (groupMetadata): ~243 consultas de uma vez, o WhatsApp devolveu
    # rate-overlimit em 06/11. Nome de grupo sai de `grupo(jid)`, um por vez, com teto (whatsapp.Grupos).
    def grupo(self, jid: str) -> dict:
        """GET /group/findGroupInfos: {id, subject, pictureUrl, size} de UM grupo (2 consultas ao WhatsApp: dados e foto)."""
        r = self._pedir("GET", f"/group/findGroupInfos/{self._i()}?groupJid={urllib.parse.quote(jid, safe='@.')}",
                        tolerar={400: (), 404: ()})
        return r if isinstance(r, dict) else {}

    def convite(self, codigo: str) -> dict:
        """GET /group/inviteInfo: dados do grupo de um link de convite, SEM entrar nele (acceptInviteCode não existe aqui)."""
        r = self._pedir("GET", f"/group/inviteInfo/{self._i()}?inviteCode={urllib.parse.quote(codigo, safe='')}",
                        tolerar={400: (), 404: ()})
        return r if isinstance(r, dict) else {}

    def numero_no_whatsapp(self, numero: str) -> dict | None:
        """POST /chat/whatsappNumbers: {"exists", "jid", "number"} do número, ou None se a Evolution não respondeu nada."""
        r = self._pedir("POST", f"/chat/whatsappNumbers/{self._i()}", {"numbers": [numero]}, lista_ok=True)
        if isinstance(r, dict):
            r = [r]
        for item in r or []:
            if isinstance(item, dict):
                return {"exists": item.get("exists") in (True, "true"), "jid": str(item.get("jid") or ""),
                        "number": str(item.get("number") or numero)}
        return None

    def foto_perfil(self, jid: str) -> str:
        """POST /chat/fetchProfilePictureUrl: URL atual da foto (vazia se a pessoa não tem foto ou esconde)."""
        r = self._pedir("POST", f"/chat/fetchProfilePictureUrl/{self._i()}", {"number": jid},
                        tolerar={400: (), 404: ()}) or {}
        return str(r.get("profilePictureUrl") or "")

    def perfil(self, jid: str) -> dict:
        """POST /chat/fetchProfile (pedido 57): {name, picture, status{status,setAt}, isBusiness, description, numberExists}."""
        r = self._pedir("POST", f"/chat/fetchProfile/{self._i()}", {"number": jid}, tolerar={400: (), 404: ()})
        return r if isinstance(r, dict) else {}

    def perfil_comercial(self, jid: str) -> dict:
        """POST /chat/fetchBusinessProfile: {description, website[], category, business_hours{business_config[]}}."""
        r = self._pedir("POST", f"/chat/fetchBusinessProfile/{self._i()}", {"number": jid}, tolerar={400: (), 404: ()})
        return r if isinstance(r, dict) else {}

    @staticmethod
    def _citar(corpo: dict, citar: dict | None) -> dict:
        """Responder citando (pedido 50, rodada 2): `quoted.key` basta, a Evolution busca a mensagem no banco dela."""
        if citar:
            corpo["quoted"] = {"key": _chave(citar)}
        return corpo

    def enviar_texto(self, numero: str, texto: str, *, citar: dict | None = None) -> str:
        """POST /message/sendText; devolve key.id (o texto nunca é logado)."""
        r = self._pedir("POST", f"/message/sendText/{self._i()}", self._citar({"number": numero, "text": texto}, citar),
                        incerto=True) or {}
        mid = (r.get("key") or {}).get("id")
        if not mid:
            # B4 (robustez 16/11): a resposta foi 2xx, então a mensagem pode ter saído; "não saiu" faria repetir
            raise EnvioIncerto("sendText respondeu sem key.id; pode ter saído")
        log.info("texto enviado id=%s", mid)
        return mid

    def enviar_midia(self, numero: str, tipo: str, dados: bytes, *, nome: str, mime: str, legenda: str = "",
                     citar: dict | None = None) -> str:
        """POST /message/sendMedia (pedido 50): foto, vídeo ou documento em base64; devolve key.id.
        Nem os bytes nem a legenda são logados. A Evolution regrava a foto em JPEG antes de subir."""
        if tipo not in ("image", "video", "document"):
            raise VigiaErro("tipo de mídia fora do contrato")
        corpo = {"number": numero, "mediatype": tipo, "mimetype": mime, "fileName": nome,
                 "media": base64.b64encode(dados).decode("ascii")}
        if legenda:
            corpo["caption"] = legenda
        r = self._pedir("POST", f"/message/sendMedia/{self._i()}", self._citar(corpo, citar), incerto=True,
                        timeout=TIMEOUT_ENVIO_MIDIA) or {}
        mid = (r.get("key") or {}).get("id")
        if not mid:
            # B4 (robustez 16/11): a resposta foi 2xx, então a mensagem pode ter saído; "não saiu" faria repetir
            raise EnvioIncerto("sendMedia respondeu sem key.id; pode ter saído")
        log.info("mídia enviada id=%s tipo=%s bytes=%d", mid, tipo, len(dados))
        return mid

    def enviar_audio(self, numero: str, dados: bytes, *, citar: dict | None = None) -> str:
        """POST /message/sendWhatsAppAudio (pedido 50): a Evolution converte para Ogg/Opus com o ffmpeg dela e manda
        como mensagem de voz (ptt). Devolve key.id; o áudio nunca é logado."""
        r = self._pedir("POST", f"/message/sendWhatsAppAudio/{self._i()}",
                        self._citar({"number": numero, "audio": base64.b64encode(dados).decode("ascii"),
                                     "encoding": True}, citar),
                        incerto=True, timeout=TIMEOUT_ENVIO_MIDIA) or {}
        mid = (r.get("key") or {}).get("id")
        if not mid:
            # B4 (robustez 16/11): a resposta foi 2xx, então a mensagem pode ter saído; "não saiu" faria repetir
            raise EnvioIncerto("sendWhatsAppAudio respondeu sem key.id; pode ter saído")
        log.info("áudio enviado id=%s bytes=%d", mid, len(dados))
        return mid

    def reagir(self, key: dict, emoji: str) -> str:
        """POST /message/sendReaction (pedido 50, rodada 2): um emoji na mensagem; emoji vazio tira a reação.
        Devolve key.id da reação."""
        r = self._pedir("POST", f"/message/sendReaction/{self._i()}", {"key": _chave(key), "reaction": emoji},
                        incerto=True) or {}
        mid = (r.get("key") or {}).get("id")
        if not mid:
            raise VigiaErro("sendReaction sem key.id na resposta")
        log.info("reação enviada id=%s", mid)
        return mid

    def editar_texto(self, numero: str, key: dict, texto: str) -> str:
        """POST /chat/updateMessage (pedido 50, rodada 3): troca o texto de uma mensagem própria. O WhatsApp só aceita
        até 15 min depois do envio. Devolve key.id da edição; o texto nunca é logado."""
        r = self._pedir("POST", f"/chat/updateMessage/{self._i()}", {"number": numero, "text": texto, "key": _chave(key)},
                        incerto=True) or {}
        mid = (r.get("key") or {}).get("id")
        if not mid:
            raise VigiaErro("updateMessage sem key.id na resposta")
        log.info("edição enviada id=%s", mid)
        return mid

    def apagar_para_todos(self, key: dict) -> None:
        """DELETE /chat/deleteMessageForEveryone (D-63): apaga uma mensagem própria no celular de todos. Sem volta; a
        Evolution marca a linha como DELETED (ou a remove, conforme a configuração dela)."""
        self._pedir("DELETE", f"/chat/deleteMessageForEveryone/{self._i()}", _chave(key), incerto=True)
        log.info("mensagem apagada para todos")
