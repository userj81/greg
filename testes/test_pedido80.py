"""Pedido 80 (D-81): arquivos dentro da seção. A pessoa anexa pela janela da seção (o arquivo vai do socket direto
para o disco); a IA pede o arquivo de uma mensagem e o coletor o baixa; só imagem e vídeo abrem no Greg, o resto é
download; apagar seção com arquivo pede confirmação. Nada sai do Mac nos testes."""
import http.client
import json
import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import anexos, secoes, server as srv_mod  # noqa: E402
from app import mcp_ferramentas as mf  # noqa: E402
from app.erros import EvolutionIndisponivel  # noqa: E402
from app.leitor_api import FUNCOES, argv_da_ponte  # noqa: E402
from app.server import _despachar, descobrir_rotas  # noqa: E402
from testes.test_paginas import XSS, Base as BasePaginas, texto  # noqa: E402
from testes.test_ponte import AMANHA, LONGE, Base as BasePonte  # noqa: E402
from testes.test_server import TOKEN, TestServidor as BaseServidor  # noqa: E402

RAIZ = Path(__file__).resolve().parents[1]
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 5000
PDF = b"%PDF-1.4 proposta comercial"
SVG = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"


class TestTipoENome(unittest.TestCase):
    def test_tipo_sai_do_conteudo(self):
        self.assertEqual([anexos.tipo_de(x) for x in (PNG, JPG, MP4, PDF, b"GIF89a....", b"RIFF\x00\x00\x00\x00WEBPVP8 ")],
                         ["image/png", "image/jpeg", "video/mp4", "application/pdf", "image/gif", "image/webp"])
        self.assertEqual(anexos.tipo_de(b"\x00\x00\x00\x18ftypheic"), "image/heic")
        for outro in (SVG, b"<html>", b"PK\x03\x04planilha", b""):
            self.assertEqual(anexos.tipo_de(outro), anexos.GENERICO)
        self.assertEqual([anexos.classe(t) for t in ("image/png", "video/mp4", "application/pdf", anexos.GENERICO)],
                         ["imagem", "video", "documento", "documento"])

    def test_nome_seguro(self):
        self.assertEqual(anexos.nome_seguro('../../etc/"pass\nwd".png', "image/png"), "_.._etc_pass_wd_.png")
        self.assertEqual(anexos.nome_seguro("", "application/pdf"), "arquivo.pdf")
        self.assertEqual(anexos.nome_seguro("contrato", "application/pdf"), "contrato.pdf")
        self.assertLessEqual(len(anexos.nome_seguro("x" * 500 + ".png", "image/png")), anexos.NOME_MAX)
        self.assertEqual(anexos.tamanho_humano(2048), "2 KB")
        self.assertEqual(anexos.tamanho_humano(5 * 1024 * 1024 + 300000), "5,3 MB")


class BaseAnexos(BasePaginas):
    def setUp(self):
        super().setUp()
        self.sid = self.banco.criar_secao(1, "Site", "páginas públicas")

    def rota(self, metodo, caminho, corpo=None, query=None, cab=None):
        exatas, padroes = descobrir_rotas()
        h = _despachar(exatas, padroes, caminho)
        self.assertIsNotNone(h, caminho)
        r = self.req(metodo, caminho, corpo=corpo, cab=cab)
        r.query = dict(query or {})
        return h(r)

    def subir(self, conteudo: bytes, nome="foto.png", secao=None):
        """Imita o servidor: o arquivo já está em dados/anexos/.chegando/ quando a rota roda."""
        tmp = anexos.pasta_chegando(self.dados) / f"sobe-{len(list(anexos.pasta_chegando(self.dados).iterdir()))}"
        tmp.write_bytes(conteudo)
        r = self.rota("POST", "/frentes/secao/anexar", {"_arquivo": str(tmp), "_tipo": "application/octet-stream"},
                      {"secao": str(self.sid if secao is None else secao), "nome": nome})
        return r, json.loads(texto(r)), tmp


class TestAnexarPelaTela(BaseAnexos):
    def test_guarda_imagem_video_pdf_e_qualquer_outro(self):
        for conteudo, nome, tipo in ((PNG, "tela.png", "image/png"), (MP4, "demo.mov", "video/mp4"),
                                     (PDF, "proposta.pdf", "application/pdf"), (SVG, "logo.svg", anexos.GENERICO),
                                     (b"PK\x03\x04", "planilha.xlsx", anexos.GENERICO)):
            r, j, tmp = self.subir(conteudo, nome)
            self.assertEqual((r.status, j["ok"], j["nome"]), (200, True, nome))
            self.assertFalse(tmp.exists())                                # saiu da pasta de chegada
            ficha = self.banco.anexo(j["id"])
            self.assertEqual((ficha["tipo"], ficha["tamanho"], ficha["origem"]), (tipo, len(conteudo), "dono"))
            arq = anexos.caminho(self.dados, ficha)
            self.assertEqual(arq.read_bytes(), conteudo)
            self.assertEqual(arq.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(self.banco.anexos(self.sid)), 5)
        self.assertEqual(list(anexos.pasta_chegando(self.dados).iterdir()), [])

    def test_recusas_nao_deixam_resto(self):
        r, j, tmp = self.subir(b"", "vazio.png")
        self.assertEqual((r.status, j["ok"]), (415, False))
        self.assertFalse(tmp.exists())
        r, j, tmp = self.subir(PNG, "x.png", secao=987654)
        self.assertEqual(r.status, 404)
        self.assertFalse(tmp.exists())
        fora = self.dados / "de-fora.png"                                 # caminho que não é da pasta de chegada
        fora.write_bytes(PNG)
        r = self.rota("POST", "/frentes/secao/anexar", {"_arquivo": str(fora)}, {"secao": str(self.sid), "nome": "x.png"})
        self.assertEqual(r.status, 400)
        r = self.rota("POST", "/frentes/secao/anexar", {}, {"secao": str(self.sid), "nome": "x.png"})
        self.assertEqual(r.status, 400)
        self.assertEqual(self.banco.anexos(), [])
        with mock.patch.object(anexos, "LIMITE", 10):
            r, j, tmp = self.subir(PNG, "grande.png")
        self.assertEqual(r.status, 415)
        self.assertIn("100 MB", j["mensagem"])
        self.assertEqual([p.name for p in anexos.pasta(self.dados).iterdir()], [anexos.CHEGANDO])

    def test_origem_de_fora_e_recusada_e_o_pedaco_apagado(self):
        tmp = anexos.pasta_chegando(self.dados) / "sobe-x"
        tmp.write_bytes(PNG)
        r = self.rota("POST", "/frentes/secao/anexar", {"_arquivo": str(tmp)}, {"secao": str(self.sid), "nome": "x.png"},
                      cab={"host": "127.0.0.1:8120", "origin": "https://fora.example"})
        self.assertEqual(r.status, 403)
        self.assertFalse(tmp.exists())

    def test_pedaco_velho_e_varrido(self):
        velho = anexos.pasta_chegando(self.dados) / "sobe-velho"
        velho.write_bytes(b"x")
        self.assertEqual(anexos.varrer_chegando(self.dados, agora=velho.stat().st_mtime + 30), 0)
        self.assertEqual(anexos.varrer_chegando(self.dados, agora=velho.stat().st_mtime + 7200), 1)
        self.assertFalse(velho.exists())


class TestMostrarBaixarApagar(BaseAnexos):
    def test_imagem_abre_documento_so_baixa(self):
        _, j, _ = self.subir(PNG, "tela.png")
        r = self.rota("GET", f"/frentes/anexo/{j['id']}")
        cab = dict(r.cabecalhos)
        self.assertEqual((r.status, r.tipo, r.corpo, cab["X-Content-Type-Options"]), (200, "image/png", PNG, "nosniff"))
        self.assertNotIn("Content-Disposition", cab)
        r = self.rota("GET", f"/frentes/anexo/{j['id']}", query={"baixar": "1"})
        self.assertIn("attachment", dict(r.cabecalhos)["Content-Disposition"])
        for conteudo, nome in ((PDF, "proposta.pdf"), (SVG, "logo.svg")):
            _, j, _ = self.subir(conteudo, nome)
            r = self.rota("GET", f"/frentes/anexo/{j['id']}")
            cab = dict(r.cabecalhos)
            self.assertIn("attachment", cab["Content-Disposition"], nome)     # nunca é aberto na origem do painel
            self.assertEqual(cab["X-Content-Type-Options"], "nosniff")
        self.assertEqual(r.tipo, anexos.GENERICO)                         # o SVG sai como arquivo genérico
        self.assertEqual(self.rota("GET", "/frentes/anexo/987654").status, 404)

    def test_video_le_so_o_trecho_pedido(self):
        _, j, _ = self.subir(MP4, "demo.mp4")
        r = self.rota("GET", f"/frentes/anexo/{j['id']}", cab={"range": "bytes=0-1"})
        self.assertEqual((r.status, r.corpo, dict(r.cabecalhos)["Content-Range"]), (206, MP4[:2], f"bytes 0-1/{len(MP4)}"))
        with mock.patch.object(anexos, "PEDACO", 1000):
            r = self.rota("GET", f"/frentes/anexo/{j['id']}", cab={"range": "bytes=100-"})
        self.assertEqual((r.status, len(r.corpo), dict(r.cabecalhos)["Content-Range"]), (206, 1000, f"bytes 100-1099/{len(MP4)}"))
        r = self.rota("GET", f"/frentes/anexo/{j['id']}", cab={"range": f"bytes={len(MP4)}-"})
        self.assertEqual(r.status, 416)

    def test_ficha_com_nome_de_arquivo_forjado_nao_serve_nada(self):
        aid = self.banco.criar_anexo(self.sid, "x.png", "image/png", 3, "0" * 64, "../vigia.db")
        self.assertEqual(self.rota("GET", f"/frentes/anexo/{aid}").status, 404)

    def test_janela_da_secao_mostra_a_grade_e_a_frente_conta(self):
        self.subir(PNG, "tela " + XSS + ".png")
        _, video, _ = self.subir(MP4, "demo.mp4")
        _, pdf, _ = self.subir(PDF, "proposta.pdf")
        t = texto(self.rota("GET", f"/frentes/secao/{self.sid}"))
        self.assertNotIn("<script>alert", t)
        self.assertIn('id="arquivos"', t)
        self.assertIn('id="fr-arq-campo"', t)
        self.assertIn(f'<video src="/frentes/anexo/{video["id"]}"', t)
        self.assertIn(f'href="/frentes/anexo/{pdf["id"]}?baixar=1"', t)
        self.assertIn(">PDF<", t)
        self.assertIn("class=\"fr-luz\"", t)
        self.assertIn("3 arquivos", t)
        self.assertIn("3 arquivos serão apagados", t)                     # aviso no botão de apagar a seção
        self.assertIn("3 arquivos", texto(self.rota("GET", "/frentes/1")))

    def test_apagar_arquivo_tira_do_disco(self):
        _, j, _ = self.subir(PNG, "tela.png")
        arq = anexos.caminho(self.dados, self.banco.anexo(j["id"]))
        r = self.rota("POST", f"/frentes/anexo/{j['id']}/apagar")
        self.assertEqual((r.status, dict(r.cabecalhos)["Location"]), (303, f"/frentes/secao/{self.sid}#arquivos"))
        self.assertFalse(arq.exists())
        self.assertEqual(self.banco.anexos(), [])
        self.assertEqual(self.rota("POST", f"/frentes/anexo/{j['id']}/apagar").status, 404)

    def test_apagar_secao_com_arquivo_pede_confirmacao(self):
        _, j, _ = self.subir(PNG, "tela.png")
        arq = anexos.caminho(self.dados, self.banco.anexo(j["id"]))
        r = self.rota("POST", f"/frentes/secao/{self.sid}/apagar")
        self.assertEqual(r.status, 200)
        self.assertIn("Apagar seção e arquivos", texto(r))
        self.assertTrue(arq.exists())
        self.assertIsNotNone(self.banco.secao(self.sid))
        r = self.rota("POST", f"/frentes/secao/{self.sid}/apagar", {"confirmo": "1"})
        self.assertEqual(r.status, 303)
        self.assertFalse(arq.exists())
        self.assertEqual((self.banco.secoes(), self.banco.anexos()), ([], []))

    def test_secao_sem_arquivo_apaga_direto(self):
        self.assertEqual(self.rota("POST", f"/frentes/secao/{self.sid}/apagar").status, 303)

    def test_rotas_na_cobertura_e_fora_do_conector(self):
        for rota in ("/frentes/secao/anexar", "/frentes/anexo/{id}", "/frentes/anexo/{id}/apagar"):
            self.assertTrue(mf.COBERTURA[rota].startswith("tela:"), rota)


class TestIAPedeOArquivo(BasePonte):
    def setUp(self):
        super().setUp()
        self.dados = Path(self.tmp.name)
        self.sid = self.banco.criar_secao(1, "Site")

    def buscar(self, achou, evo=None):
        with mock.patch("app.whatsapp.chave_da_mensagem", return_value=achou):
            return anexos.buscar_pendentes(self.banco, object(), evo, self.dados, 0)

    def test_classificar_registra_o_pedido_so_com_secao(self):
        r = self.cls("m1", "demanda", resumo="conferir comprovante", frente=1, secao=self.sid, anexar="sim", prazo=AMANHA)
        self.assertEqual(r["anexo"], "pedido")
        r = self.cls("m2", "demanda", resumo="provisório também", frente=1, secao=self.sid, anexar="sim", prazo=LONGE)
        self.assertEqual(r["anexo"], "pedido")
        r = self.cls("m3", "demanda", resumo="sem seção", frente=1, anexar="sim", prazo=AMANHA)
        self.assertEqual(r["anexo"], "sem_secao")
        r = self.cls("m4", "aviso", resumo="só aviso", frente=1, secao=self.sid, anexar="sim")
        self.assertNotIn("anexo", r)
        self.assertEqual([(p["msg_id"], p["secao_id"]) for p in self.banco.anexos_pendentes()],
                         [("m1", self.sid), ("m2", self.sid)])
        self.banco.pedir_anexo("m1", self.sid)                             # repetir não duplica
        self.assertEqual(len(self.banco.anexos_pendentes()), 2)

    def test_a_ia_de_api_e_o_prompt_conhecem(self):
        self.assertIn("anexar", FUNCOES["classificar"][2])
        self.assertIn("--anexar=sim", argv_da_ponte("classificar", {"msg_id": "m", "classe": "demanda", "resumo": "r",
                                                                    "confianca": "alta", "anexar": "sim"}))
        self.assertIn("[--anexar sim]", (RAIZ / "prompts" / "vigia.md").read_text(encoding="utf-8"))

    def test_coletor_baixa_foto_documento_video_e_audio(self):
        evo = mock.Mock()
        evo.midia_bytes.side_effect = lambda chave, audio=False, **k: (b"OggS....", "audio/ogg") if audio else (JPG, "image/jpeg")
        evo.midia_documento.return_value = PDF
        evo.midia_video.return_value = (MP4, "video/mp4")
        casos = (("AAA111", "imageMessage", {}, "foto-AAA111.jpg", "image/jpeg"),
                 ("BBB222", "documentMessage", {"documentMessage": {"fileName": "contrato final.pdf"}}, "contrato final.pdf", "application/pdf"),
                 ("CCC333", "videoMessage", {}, "video-CCC333.mp4", "video/mp4"),
                 ("DDD444", "pttMessage", {}, "audio-DDD444.ogg", anexos.GENERICO))
        for mid, tipo_msg, message, nome, tipo in casos:
            self.banco.pedir_anexo(mid, self.sid)
            r = self.buscar(({"id": mid, "remoteJid": "55@s.whatsapp.net", "fromMe": False}, tipo_msg, message), evo)
            self.assertEqual(r, {"guardados": 1, "desistidos": 0, "adiados": 0}, mid)
            ficha = next(a for a in self.banco.anexos(self.sid) if a["msg_id"] == mid)
            self.assertEqual((ficha["nome"], ficha["tipo"], ficha["origem"]), (nome, tipo, "ia"))
            self.assertTrue(anexos.caminho(self.dados, ficha).is_file())
        self.assertEqual(self.banco.anexos_pendentes(), [])
        self.assertEqual(list(anexos.pasta_chegando(self.dados).iterdir()), [])
        self.banco.pedir_anexo("AAA111", self.sid)                         # o mesmo arquivo não entra duas vezes
        self.assertEqual(self.buscar(({"id": "AAA111"}, "imageMessage", {}), evo)["guardados"], 0)
        self.assertEqual(len(self.banco.anexos(self.sid)), 4)

    def test_nao_achou_tenta_de_novo_e_desiste_na_quinta(self):
        self.banco.pedir_anexo("m1", self.sid)
        for _ in range(anexos.TENTATIVAS):
            self.assertEqual(self.buscar(None)["adiados"], 1)
        self.assertEqual(self.buscar(None), {"guardados": 0, "desistidos": 1, "adiados": 0})
        self.assertEqual(self.banco.anexos_pendentes(), [])

    def test_evolution_fora_adia_e_mensagem_sem_arquivo_sai(self):
        fora = mock.Mock()
        fora.midia_bytes.side_effect = EvolutionIndisponivel("fora")
        self.banco.pedir_anexo("m1", self.sid)
        self.assertEqual(self.buscar(({"id": "m1"}, "imageMessage", {}), fora)["adiados"], 1)
        self.assertEqual(self.banco.anexos_pendentes()[0]["tentativas"], 1)
        self.assertEqual(self.buscar(({"id": "m1"}, "conversation", {}), mock.Mock())["desistidos"], 1)
        self.assertEqual(self.banco.anexos_pendentes(), [])

    def test_secao_apagada_leva_o_pedido_junto(self):
        self.banco.pedir_anexo("m1", self.sid)
        self.banco.apagar_secao(self.sid)
        self.assertEqual(self.banco.anexos_pendentes(), [])

    def test_ia_recebe_so_o_nome_do_arquivo(self):
        self.banco.criar_anexo(self.sid, "briefing do site.pdf", "application/pdf", 10, "0" * 64, "a" * 24 + ".pdf")
        md = secoes.resumo_md(self.banco, self.ponte)
        self.assertIn(f"- seção {self.sid} · Site · vazia · arquivos: briefing do site.pdf", md)
        for i in range(7):
            self.banco.criar_anexo(self.sid, f"foto{i}.png", "image/png", 10, "0" * 64, f"{i:024d}.png")
        self.assertIn("e mais 3", secoes.resumo_md(self.banco, self.ponte))


class TestSobeDiretoParaODisco(BaseServidor):
    """O servidor de verdade: o corpo cru vai para dados/anexos/.chegando/ em pedaços e a rota o guarda."""

    def enviar(self, caminho, conteudo, cookie=True, tamanho=None):
        con = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=10)
        cab = {"Host": f"127.0.0.1:{self.porta}", "Content-Type": "application/octet-stream",
               "Content-Length": str(len(conteudo) if tamanho is None else tamanho)}
        if cookie:
            cab["Cookie"] = f"{srv_mod.COOKIE}={srv_mod.valor_cookie(TOKEN)}"
        con.request("POST", caminho, body=conteudo, headers=cab)
        r = con.getresponse()
        corpo = r.read()
        con.close()
        return r.status, corpo

    def test_arquivo_maior_que_o_limite_antigo_sobe_e_e_servido(self):
        sid = self.ctx.banco.criar_secao(1, "Site")
        grande = b"\x00\x00\x00\x18ftypmp42" + b"v" * (17 * 1024 * 1024)       # acima dos 16 MB do corpo cru comum
        st, corpo = self.enviar(f"/frentes/secao/anexar?secao={sid}&nome=demo.mp4", grande)
        self.assertEqual(st, 200, corpo[:200])
        ficha = self.ctx.banco.anexos(sid)[0]
        self.assertEqual((ficha["tamanho"], ficha["tipo"], ficha["nome"]), (len(grande), "video/mp4", "demo.mp4"))
        st, _, parte = self._http("GET", f"/frentes/anexo/{ficha['id']}", cab={"Range": "bytes=0-11"})
        self.assertEqual((st, parte), (206, grande[:12]))

    def test_sem_sessao_o_arquivo_nem_e_lido(self):
        sid = self.ctx.banco.criar_secao(1, "Site")
        st, _ = self.enviar(f"/frentes/secao/anexar?secao={sid}&nome=x.png", PNG * 2000, cookie=False)
        self.assertIn(st, (303, 400, 401, 403))
        self.assertEqual(self.ctx.banco.anexos(), [])
        dados = Path(self.ctx.config["dados_dir"])
        self.assertFalse((dados / anexos.PASTA).exists() and any((dados / anexos.PASTA / anexos.CHEGANDO).iterdir()))

    def test_acima_de_100_mb_e_recusado_antes_de_ler(self):
        sid = self.ctx.banco.criar_secao(1, "Site")
        with mock.patch.object(anexos, "LIMITE", 1000):
            st, _ = self.enviar(f"/frentes/secao/anexar?secao={sid}&nome=x.png", PNG * 50)
        self.assertEqual(st, 400)
        self.assertEqual(self.ctx.banco.anexos(), [])


del BasePaginas, BasePonte, BaseServidor, BaseAnexos

if __name__ == "__main__":
    unittest.main()
