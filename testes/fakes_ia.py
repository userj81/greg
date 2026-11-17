"""Servidor HTTP falso do protocolo de conversa da OpenAI (pedido 68), só em 127.0.0.1, para os testes do leitor por
API e da bancada. Nada sai do Mac. `roteiro`: lista de respostas na ordem dos pedidos; cada item é um dict (corpo JSON,
status 200), uma tupla (status, corpo[, cabeçalhos]) ou uma função `f(pedido) -> item`. Esgotado o roteiro, repete o
último item. `pedidos` guarda o que chegou: {"caminho", "cabecalhos", "corpo"}.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def resposta(texto=None, chamadas=None, *, entrada=100, saida=20, custo=None, fim=None):
    """Corpo de uma resposta do protocolo. `chamadas`: [(nome, argumentos dict | str)] vira tool_calls."""
    msg = {"role": "assistant", "content": texto}
    if chamadas:
        msg["tool_calls"] = [{"id": f"call_{i}", "type": "function",
                              "function": {"name": n, "arguments": a if isinstance(a, str) else json.dumps(a, ensure_ascii=False)}}
                             for i, (n, a) in enumerate(chamadas)]
    uso = {"prompt_tokens": entrada, "completion_tokens": saida, "total_tokens": entrada + saida}
    if custo is not None:
        uso["cost"] = custo
    return {"id": "x", "object": "chat.completion",
            "choices": [{"index": 0, "message": msg, "finish_reason": fim or ("tool_calls" if chamadas else "stop")}],
            "usage": uso}


def erro(status, codigo="", mensagem="recusado"):
    return (status, {"error": {"message": mensagem, "type": codigo or "erro", "code": codigo or None}})


class ServidorIA:
    def __init__(self, roteiro=None, atraso_s: float = 0.0):
        self.roteiro = list(roteiro or [])
        self.pedidos: list[dict] = []
        self.atraso_s = atraso_s
        dono = self

        class Tratador(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                bruto = self.rfile.read(n)
                try:
                    corpo = json.loads(bruto.decode("utf-8"))
                except ValueError:
                    corpo = None
                pedido = {"caminho": self.path, "cabecalhos": {k.lower(): v for k, v in self.headers.items()}, "corpo": corpo}
                dono.pedidos.append(pedido)
                if dono.atraso_s:
                    time.sleep(dono.atraso_s)
                i = min(len(dono.pedidos) - 1, len(dono.roteiro) - 1)
                item = dono.roteiro[i] if dono.roteiro else resposta("ok")
                if callable(item):
                    item = item(pedido)
                status, dados, cab = 200, item, {}
                if isinstance(item, tuple):
                    status, dados = item[0], item[1]
                    cab = item[2] if len(item) > 2 else {}
                saida = dados if isinstance(dados, bytes) else json.dumps(dados, ensure_ascii=False).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(saida)))
                    for k, v in cab.items():
                        self.send_header(k, v)
                    self.end_headers()
                    self.wfile.write(saida)
                except OSError:
                    pass      # o cliente desistiu (teste de tempo esgotado)

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), Tratador)
        self._srv.daemon_threads = True
        self._thread = threading.Thread(target=lambda: self._srv.serve_forever(poll_interval=0.02), daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._srv.server_address[1]}/v1"

    def iniciar(self):
        self._thread.start()
        return self

    def parar(self):
        self._srv.shutdown()
        self._srv.server_close()
