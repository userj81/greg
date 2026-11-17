"""Abridor HTTP comum do Greg: sem proxy de ambiente e sem seguir redirect.
Quem chama: app/evolution_client.py, app/frentes_ponte.py, app/whatsapp.py (foto de perfil) e app/aviso.py.
O que não faz: não conhece host nem token; cada chamador decide a URL e trata o HTTPError (3xx vira erro do serviço).
"""
import urllib.request


class SemRedirect(urllib.request.HTTPRedirectHandler):
    """Recusa redirect: o 3xx chega como HTTPError. Um redirect levaria cabeçalho (apikey, token) a outro host."""

    def redirect_request(self, *args, **kwargs):
        return None


# Todos os serviços são locais (127.0.0.1) ou um CDN fixo: proxy de ambiente (http_proxy) nunca entra no caminho.
ABRIDOR = urllib.request.build_opener(urllib.request.ProxyHandler({}), SemRedirect)
