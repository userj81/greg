"""Arquivos anexados a uma seção de frente (pedido 80, D-81), de qualquer tipo, guardados em dados/anexos/. Entram
pela tela (a pessoa anexa) ou pela leitura (a IA pede o arquivo de uma mensagem e o coletor o baixa da Evolution).
Quem chama: app/server.py (recebe o arquivo direto no disco), app/paginas/frentes.py (anexar, mostrar, baixar, apagar)
e app/secoes.py (só os nomes entram no resumo da IA).
O tipo é conferido pelos primeiros bytes do arquivo, nunca pela extensão nem pelo que o navegador disse.
Só imagem e vídeo abrem dentro do Greg; todo o resto (PDF, planilha, áudio, o que for) só sai como download.
O que não faz: não manda arquivo a lugar nenhum, não entrega conteúdo à IA, não entra na limpeza semanal.
"""
import hashlib
import logging
import os
import re
import secrets
import time
from pathlib import Path

log = logging.getLogger("vigia.anexos")
LIMITE = 100 * 1024 * 1024         # por arquivo (D-81)
TENTATIVAS = 5                     # quantas vezes o coletor procura a mídia de um pedido da IA antes de desistir
PEDACO = 4 * 1024 * 1024           # maior fatia devolvida de uma vez (o vídeo pede o resto em seguida)
NOME_MAX = 120
PASTA = "anexos"
CHEGANDO = ".chegando"             # onde o servidor grava enquanto o arquivo ainda está subindo
_NOME_RUIM = re.compile(r"[^\w .()\-]+")
_HEIC = (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1")
EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif", "image/heic": "heic",
       "video/mp4": "mp4", "video/webm": "webm", "application/pdf": "pdf", "application/octet-stream": "bin"}
GENERICO = "application/octet-stream"
_EXT_DA_EVOLUTION = {"audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/mp4": "m4a", "image/jpeg": "jpg", "image/png": "png",
                     "image/webp": "webp", "video/mp4": "mp4"}
_MSG_IMAGEM = ("imageMessage", "stickerMessage")
_MSG_AUDIO = ("audioMessage", "pttMessage")
_MSG_VIDEO = ("videoMessage", "ptvMessage")
_MSG_DOC = ("documentMessage", "documentWithCaptionMessage")


class AnexoRecusado(ValueError):
    """Arquivo vazio ou grande demais. A mensagem é para a pessoa."""


def pasta(dados) -> Path:
    p = Path(dados) / PASTA
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    return p


def pasta_chegando(dados) -> Path:
    p = pasta(dados) / CHEGANDO
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    return p


def tipo_de(cabeca: bytes) -> str:
    """O tipo pelo começo do arquivo. O que não é imagem, vídeo nem PDF é GENERICO: fica guardado e só sai como
    download (SVG e HTML caem aqui de propósito: o painel nunca abre na própria origem algo que possa rodar código)."""
    if cabeca.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if cabeca.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if cabeca.startswith(b"RIFF") and cabeca[8:12] == b"WEBP":
        return "image/webp"
    if cabeca[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if cabeca[4:8] == b"ftyp":
        return "image/heic" if cabeca[8:12] in _HEIC else "video/mp4"       # MP4 e MOV tocam do mesmo jeito
    if cabeca.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    if cabeca.startswith(b"%PDF-"):
        return "application/pdf"
    return GENERICO


def classe(tipo: str) -> str:
    """'imagem', 'video' ou 'documento' (documento só sai como download)."""
    return "imagem" if tipo.startswith("image/") else "video" if tipo.startswith("video/") else "documento"


def nome_seguro(bruto, tipo: str) -> str:
    """Nome para mostrar e para o download: sem barra, aspas nem quebra de linha, com a extensão do tipo real."""
    nome = _NOME_RUIM.sub("_", str(bruto or "")).strip(" .")
    ext = EXT.get(tipo, "bin")
    if not nome:
        return f"arquivo.{ext}"
    base, ponto, fim = nome.rpartition(".")
    if ponto and base and 0 < len(fim) <= 8 and fim.isalnum():
        ext = fim                                   # a extensão que a pessoa deu fica, mesmo cortando o nome
    else:
        base = nome
    return f"{base[:NOME_MAX - len(ext) - 1]}.{ext}"


def tamanho_humano(n: int) -> str:
    n = int(n or 0)
    if n < 1024 * 1024:
        return f"{max(1, round(n / 1024))} KB"
    return f"{n / (1024 * 1024):.1f} MB".replace(".", ",")


def guardar(banco, dados, secao_id: int, origem: Path, nome, *, de: str = "dono", msg_id: str | None = None) -> dict:
    """Move o arquivo que acabou de subir para dados/anexos/ e grava a ficha. Devolve a ficha.
    Recusa (AnexoRecusado) apaga o que subiu; nada fica pela metade."""
    origem = Path(origem)
    try:
        tamanho = origem.stat().st_size
        if tamanho <= 0:
            raise AnexoRecusado("O arquivo está vazio.")
        if tamanho > LIMITE:
            raise AnexoRecusado("O arquivo passa de 100 MB.")
        sha = hashlib.sha256()
        with origem.open("rb") as f:
            cabeca = f.read(32)
            sha.update(cabeca)
            for bloco in iter(lambda: f.read(1024 * 1024), b""):
                sha.update(bloco)
        tipo = tipo_de(cabeca)
        arquivo = f"{secrets.token_hex(12)}.{EXT[tipo]}"
        destino = pasta(dados) / arquivo
        os.replace(origem, destino)
        os.chmod(destino, 0o600)
    except BaseException:
        origem.unlink(missing_ok=True)
        raise
    try:
        aid = banco.criar_anexo(int(secao_id), nome_seguro(nome, tipo), tipo, tamanho, sha.hexdigest(), arquivo,
                                origem=de, msg_id=msg_id)
    except BaseException:
        destino.unlink(missing_ok=True)
        raise
    return banco.anexo(aid)


def caminho(dados, ficha: dict) -> Path | None:
    """O arquivo no disco, só se o nome gravado for mesmo um arquivo direto de dados/anexos/."""
    nome = str(ficha.get("arquivo") or "")
    if not re.fullmatch(r"[0-9a-f]{24}\.[a-z0-9]{2,5}", nome):
        return None
    p = pasta(dados) / nome
    return p if p.is_file() else None


def apagar(banco, dados, ficha: dict) -> None:
    """Apaga a ficha e o arquivo (de verdade, sem lixeira). Arquivo que já sumiu não impede apagar a ficha."""
    p = caminho(dados, ficha)
    banco.apagar_anexo(int(ficha["id"]))
    if p is not None:
        p.unlink(missing_ok=True)


def fatia(arq: Path, faixa: str) -> tuple[int, bytes, list]:
    """(status, bytes, cabeçalhos) lendo do disco só o trecho pedido em `Range`. Sem Range devolve o arquivo inteiro;
    com Range devolve no máximo PEDACO bytes (o player pede o resto), para vídeo grande não ir inteiro para a memória."""
    total = arq.stat().st_size
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", str(faixa or "").strip())
    if not (m and total and (m.group(1) or m.group(2))):
        return 200, arq.read_bytes(), [("Accept-Ranges", "bytes")]
    if m.group(1):
        ini = int(m.group(1))
        fim = min(int(m.group(2)) if m.group(2) else total - 1, total - 1)
    else:
        ini, fim = max(0, total - int(m.group(2))), total - 1
    if ini > fim or ini >= total:
        return 416, b"", [("Content-Range", f"bytes */{total}")]
    fim = min(fim, ini + PEDACO - 1)
    with arq.open("rb") as f:
        f.seek(ini)
        dados = f.read(fim - ini + 1)
    return 206, dados, [("Content-Range", f"bytes {ini}-{fim}/{total}"), ("Accept-Ranges", "bytes")]


def varrer_chegando(dados, agora: float | None = None, idade: int = 3600) -> int:
    """Apaga envios que ficaram pela metade há mais de uma hora (aba fechada no meio). Devolve quantos."""
    agora = time.time() if agora is None else agora
    n = 0
    for p in pasta_chegando(dados).iterdir():
        try:
            if p.is_file() and agora - p.stat().st_mtime > idade:
                p.unlink()
                n += 1
        except OSError:
            continue
    return n


# ------------------------------------------------------------------ arquivo que a IA pediu de uma conversa

def _nome_do_documento(message: dict) -> str:
    doc = message.get("documentMessage") or ((message.get("documentWithCaptionMessage") or {}).get("message") or {}).get("documentMessage") or {}
    return str(doc.get("fileName") or doc.get("title") or "")


def _baixar(evo, key: dict, tipo_msg: str, message: dict) -> tuple[bytes, str]:
    """(bytes, nome sugerido) da mídia da mensagem; ValueError se a mensagem não traz arquivo."""
    from app import lotes
    chave = {k: key.get(k) for k in ("id", "remoteJid", "fromMe", "participant") if key.get(k) is not None}
    album = {"interna": interna} if (interna := lotes.interna_de_album(message)) else {}
    curto = str(key.get("id") or "")[-6:] or "arquivo"
    if tipo_msg == "productMessage" and album:
        tipo_msg = "imageMessage"
    if tipo_msg in _MSG_DOC:
        return evo.midia_documento(chave), _nome_do_documento(message) or f"documento-{curto}"
    if tipo_msg in _MSG_VIDEO:
        dados, _ = evo.midia_video(chave, **album)
        return dados, f"video-{curto}.mp4"
    if tipo_msg in _MSG_IMAGEM + _MSG_AUDIO:
        dados, mime = evo.midia_bytes(chave, audio=tipo_msg in _MSG_AUDIO, **album)
        rotulo = "audio" if tipo_msg in _MSG_AUDIO else "foto"
        return dados, f"{rotulo}-{curto}.{_EXT_DA_EVOLUTION.get(mime, 'bin')}"
    raise ValueError("mensagem sem arquivo")


def buscar_pendentes(banco, psql, evo, dados, desde: int, limite: int = 20) -> dict:
    """Baixa da Evolution os arquivos que a IA pediu para guardar numa seção e os guarda. Devolve
    {"guardados", "desistidos", "adiados"}. Nunca levanta: um arquivo com problema não segura os outros nem o ciclo.
    Seção apagada, mensagem sem arquivo ou já guardada = o pedido sai. Evolution fora ou mensagem não achada = tenta
    de novo no próximo ciclo, até TENTATIVAS."""
    from app import whatsapp as wa
    from app.erros import VigiaErro
    saida = {"guardados": 0, "desistidos": 0, "adiados": 0}
    try:
        pendentes = banco.anexos_pendentes()[:limite]
    except Exception:  # noqa: BLE001
        return saida
    for p in pendentes:
        mid = p["msg_id"]
        try:
            if banco.secao(p["secao_id"]) is None or any(a.get("msg_id") == mid for a in banco.anexos(p["secao_id"])):
                banco.anexo_resolvido(mid)
                continue
            if p["tentativas"] >= TENTATIVAS:
                banco.anexo_resolvido(mid)
                saida["desistidos"] += 1
                continue
            achou = wa.chave_da_mensagem(psql, mid, desde=desde)
            if not achou:
                banco.anexo_tentado(mid)
                saida["adiados"] += 1
                continue
            try:
                conteudo, nome = _baixar(evo, achou[0], achou[1], achou[2] if len(achou) > 2 else {})
            except ValueError:                      # a mensagem não tem arquivo (ou a mídia veio ilegível)
                banco.anexo_resolvido(mid)
                saida["desistidos"] += 1
                continue
            if not conteudo or len(conteudo) > LIMITE:
                banco.anexo_resolvido(mid)
                saida["desistidos"] += 1
                continue
            tmp = pasta_chegando(dados) / f"ia-{secrets.token_hex(8)}"
            tmp.write_bytes(conteudo)
            guardar(banco, dados, p["secao_id"], tmp, nome, de="ia", msg_id=mid)
            banco.anexo_resolvido(mid)
            saida["guardados"] += 1
        except (VigiaErro, OSError) as ex:
            log.warning("arquivo pedido pela IA não guardado agora (%s)", type(ex).__name__)
            try:
                banco.anexo_tentado(mid)
            except Exception:  # noqa: BLE001
                pass
            saida["adiados"] += 1
        except Exception as ex:  # noqa: BLE001
            log.warning("arquivo pedido pela IA falhou (%s)", type(ex).__name__)
            saida["adiados"] += 1
    return saida
