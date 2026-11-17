"""Trava de envio do painel (r02, D-39): o envio pela tela de Conversas só parte com a trava aberta, e ela só abre com
o token de envio (que o Greg.app lê do ~/.vigia-envio.env depois do Touch ID). Abre pelo tempo escolhido em Ajustes
(pedido 70; padrão 8 horas, um dia de trabalho) e fecha sozinha.
Quem chama: app/paginas/conversas.py (/envio/liberar, /envio/estado, /envio/travar e o envio da conversa).
O que não faz: não guarda o token (só compara), não persiste em disco (reiniciar o painel trava de novo) e não envia
nada sozinha: cada mensagem continua sendo um clique do dono (R-9).
"""
import hmac
import threading
import time

MINUTOS = 480


class Liberacao:
    def __init__(self, minutos=MINUTOS, relogio=time.time):
        """`minutos`: número ou função sem argumento que devolve o número (a escolha de Ajustes, lida a cada liberação:
        mudar o tempo vale a partir da próxima vez que a trava abrir)."""
        self._minutos = minutos if callable(minutos) else (lambda: minutos)
        self.relogio = relogio
        self._ate = 0.0
        self._trava = threading.Lock()

    def liberar(self, token_recebido: str, token_esperado: str) -> bool:
        recebido = str(token_recebido or "").encode("utf-8")
        esperado = str(token_esperado or "").encode("utf-8")
        if not esperado or not recebido or not hmac.compare_digest(recebido, esperado):
            return False
        with self._trava:
            self._ate = self.relogio() + self.segundos
        return True

    @property
    def segundos(self) -> int:
        try:
            return max(1, int(self._minutos())) * 60
        except (TypeError, ValueError):
            return MINUTOS * 60

    def restante(self) -> int:
        with self._trava:
            return max(0, int(self._ate - self.relogio()))

    def liberada(self) -> bool:
        return self.restante() > 0

    def travar(self) -> None:
        with self._trava:
            self._ate = 0.0
