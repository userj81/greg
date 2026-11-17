"""Exceções próprias do Greg do WhatsApp; todas herdam de VigiaErro.
Quem chama: todos os módulos de app/ e bin/ que falam com Evolution, Frentes ou Postgres.
O que não faz: não carrega texto de mensagem na exceção, só ids, códigos e contagens.
"""


class VigiaErro(Exception):
    """Base de todos os erros esperados do Greg."""


class EvolutionIndisponivel(VigiaErro):
    """A Evolution não respondeu (conexão recusada, timeout ou 5xx)."""


class NaoAutorizado(VigiaErro):
    """O serviço recusou a credencial (HTTP 401)."""


class MidiaInvalida(VigiaErro):
    """O conteúdo baixado não tem os magic bytes de uma imagem conhecida."""


class FrentesIndisponivel(VigiaErro):
    """O Frentes não respondeu (conexão recusada, timeout ou 5xx)."""


class LimpezaIncompleta(VigiaErro):
    """Após o DELETE ainda restam mensagens na janela; a lápide não é gravada."""


class FrentesRecusou(VigiaErro):
    """O Frentes respondeu 4xx a um pedido válido de rede (dado recusado), diferente de indisponível."""


class CursorCorrompido(VigiaErro):
    """dados/cursor.json existe mas não parseia; o arquivo foi renomeado e o ciclo deve parar."""


class QRIndisponivel(VigiaErro):
    """GET /instance/connect voltou sem base64 (já conectada, QR não pronto ou erro); nenhum QR foi exibido."""


class EnvioIncerto(VigiaErro):
    """sendText pode ter sido entregue: a requisição saiu e a resposta não voltou (timeout/reset)."""

class TetoQR(VigiaErro):
    """O teto diário de QRs foi atingido; nenhum QR novo é gerado até amanhã (R-11, P-3)."""


_RAIZ = __file__.rsplit("/app/", 1)[0] + "/"


def _onde(e: BaseException) -> str:
    """Arquivo e linha do ponto mais fundo da falha dentro do Greg (app/ ou bin/); sem variável nem mensagem."""
    achado, tb = "", e.__traceback__
    while tb is not None:
        arq = tb.tb_frame.f_code.co_filename
        if arq.startswith(_RAIZ):
            achado = f"{arq[len(_RAIZ):]}:{tb.tb_lineno}"
        tb = tb.tb_next
    return achado


def causa(e: BaseException) -> str:
    """Texto de uma exceção para o log (pedido 63, T2), sem dado de conversa.
    VigiaErro: nome e mensagem (o contrato deste módulo garante só ids, códigos e contagens), em uma linha, até 200
    caracteres. Qualquer outra: nome, arquivo e linha de onde saiu; a mensagem fica de fora porque exceção de terceiros
    pode carregar o valor que a causou."""
    nome = type(e).__name__
    if isinstance(e, VigiaErro):
        msg = " ".join(str(e).split())[:200]
        return f"{nome}: {msg}" if msg else nome
    onde = _onde(e)
    return f"{nome} em {onde}" if onde else nome
