"""Pedido 54: catálogo e modelo legíveis, nome salvo na agenda antes do nome do perfil, foto de quem não tem URL."""
import tempfile
import unittest
from pathlib import Path

from app import lotes
from app import whatsapp as wa

RAIZ = Path(__file__).resolve().parent.parent


class Estruturadas(unittest.TestCase):
    def test_produto_do_catalogo_vira_cartao_com_foto_preco_e_descricao_recolhida(self):
        from app.paginas import conversas
        m = {"productMessage": {"product": {"title": "Case 13", "description": "Capa rígida", "currencyCode": "BRL",
                                            "priceAmount1000": {"low": 1234500, "high": 0}, "productImage": {"mediaKey": "k"}}}}
        c = wa.conteudo(m, "productMessage")
        self.assertEqual((c["tipo"], c["titulo"], c["preco"], c["descricao"], c["foto"]),
                         ("produto", "Case 13", "R$ 1.234,50", "Capa rígida", True))
        self.assertEqual(wa.previa(c), "Produto · Case 13 · R$ 1.234,50")
        h = conversas.html_conteudo({"id": "ABC"}, c)
        self.assertIn('<details class="zap-prod-desc"><summary>Ver descrição</summary>', h)   # descrição só ao clicar
        self.assertIn('src="/conversas/midia?m=ABC"', h)
        self.assertEqual(lotes.interna_de_album(m), {"imageMessage": {"mediaKey": "k"}})
        # a IA continua lendo o produto como texto
        self.assertEqual(lotes.descrever({"message": m, "messageType": "productMessage"})["texto"],
                         "Produto do catálogo ⏎ Case 13 ⏎ R$ 1.234,50 ⏎ Capa rígida")
        sem_foto = wa.conteudo({"productMessage": {"product": {"title": "X"}}}, "productMessage")
        self.assertNotIn("<img", conversas.html_conteudo({"id": "ABC"}, sem_foto))

    def test_o_que_nao_abre_ganha_um_aviso_so_sem_nome_tecnico(self):
        from app.paginas import conversas
        c = wa.conteudo({"senderKeyDistributionMessage": {}}, "unknown")
        h = conversas.html_conteudo({"id": "ABC"}, c)
        self.assertIn("Esta mensagem só abre no celular", h)
        self.assertNotIn("nknown", h)
        self.assertEqual(wa.previa(c), wa.NAO_ABRE)

    def test_produto_sem_nada_continua_como_tipo_nao_lido(self):
        self.assertEqual(wa.conteudo({"productMessage": {"product": {}}}, "productMessage")["tipo"], "outro")

    def test_modelo_com_botoes_e_modelo_interativo(self):
        t = {"templateMessage": {"hydratedTemplate": {"hydratedTitleText": "Olá", "hydratedContentText": "Seu pedido saiu",
             "hydratedFooterText": "Loja", "hydratedButtons": [{"urlButton": {"displayText": "Rastrear", "url": "https://x.test/1"}},
                                                                {"quickReplyButton": {"displayText": "Ok"}}]}}}
        self.assertEqual(wa.conteudo(t, "templateMessage")["texto"],
                         "Olá\nSeu pedido saiu\nLoja\n[ Rastrear ] https://x.test/1\n[ Ok ]")
        i = {"templateMessage": {"interactiveMessageTemplate": {"header": {"title": "T"}, "body": {"text": "Corpo"},
             "nativeFlowMessage": {"buttons": [{"buttonParamsJson": '{"display_text": "Abrir"}'}, {"buttonParamsJson": "{quebrado"}]}}}}
        self.assertEqual(wa.conteudo(i, "templateMessage")["texto"], "T\nCorpo\n[ Abrir ]")

    def test_lista_botoes_e_respostas(self):
        ls = {"listMessage": {"description": "Escolha", "sections": [{"rows": [{"title": "A", "description": "um"}, {"title": "B"}]}]}}
        self.assertEqual(wa.conteudo(ls, "listMessage")["texto"], "Escolha\n• A: um\n• B")
        self.assertEqual(wa.conteudo({"buttonsResponseMessage": {"selectedDisplayText": "Sim"}})["texto"], "Sim")

    def test_a_ia_tambem_le_o_modelo(self):
        t = {"templateMessage": {"hydratedTemplate": {"hydratedContentText": "linha 1\nlinha 2"}}}
        d = lotes.descrever({"message": t, "messageType": "templateMessage"})
        self.assertEqual((d["tipo"], d["texto"]), ("texto", "linha 1 ⏎ linha 2"))

    def test_tipo_desconhecido_segue_como_antes(self):
        self.assertEqual(wa.conteudo({"senderKeyDistributionMessage": {}}, "unknown"), {"tipo": "outro", "texto": "", "extra": "unknown"})


class Agenda(unittest.TestCase):
    def test_gatilho_e_aditivo_e_cobre_as_duas_ordens(self):
        sql = (RAIZ / "infra" / "sql" / "006_agenda.sql").read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS vigia.agenda", sql)
        self.assertIn('BEFORE INSERT OR UPDATE ON public."Contact"', sql)   # pega o INSERT ignorado por conflito
        self.assertIn('AFTER INSERT ON public."Message"', sql)
        self.assertIn("GRANT SELECT ON vigia.agenda TO vigia_ro", sql)
        self.assertNotIn("ALTER TABLE public", sql)
        self.assertNotIn("DROP ", sql)

    def test_a_lista_e_os_nomes_usam_a_agenda_primeiro(self):
        self.assertIn('coalesce(ag.nome, ct."pushName")', wa.sql_lista(0))
        self.assertIn("vigia.agenda", wa.sql_lista(0))
        self.assertIn('coalesce(ag.nome, c."pushName")', wa.sql_nomes_agenda(["5511999990000@s.whatsapp.net"]))


class FotoSemUrl(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.agora = [1_000_000.0]
        self.perguntas = []

    def _av(self, url_do_whatsapp, **kw):
        fora = self

        class Evo:
            def foto_perfil(self, jid):
                fora.perguntas.append(jid)
                return url_do_whatsapp
        return wa.Avatares(Path(self.tmp.name), Evo(), baixar=lambda u: b"\xff\xd8\xff" + b"0" * 20,
                           relogio=lambda: self.agora[0], **kw)

    def test_pergunta_ao_whatsapp_baixa_e_so_confere_de_novo_em_7_dias(self):
        jid = "5511999990000@s.whatsapp.net"
        av = self._av("https://pps.whatsapp.net/v/t61/abc.jpg?oh=1")
        self.assertTrue(av.precisa(jid, "", av.indice()))
        self.assertTrue(av.processar(jid, ""))
        self.assertIsNotNone(av.arquivo(jid))
        self.assertFalse(av.precisa(jid, "", av.indice()))
        self.agora[0] += 7 * 24 * 3600 + 1
        self.assertTrue(av.precisa(jid, "", av.indice()))
        self.assertFalse(av.processar(jid, ""))          # mesma foto: não baixa de novo, só anota a hora
        self.assertIsNotNone(av.arquivo(jid))
        self.assertFalse(av.precisa(jid, "", av.indice()))

    def test_sem_foto_pergunta_de_novo_so_depois_de_24_horas_e_grupo_nunca(self):
        jid = "5511999990001@s.whatsapp.net"
        av = self._av("")
        self.assertFalse(av.processar(jid, ""))
        self.assertFalse(av.precisa(jid, "", av.indice()))
        self.agora[0] += 24 * 3600 + 1
        self.assertTrue(av.precisa(jid, "", av.indice()))
        self.assertFalse(av.precisa("120363000000000000@g.us", "", av.indice()))
        self.assertFalse(wa.Avatares(Path(self.tmp.name)).precisa(jid, "", {}))   # sem cliente da Evolution

    def test_teto_de_perguntas_por_hora(self):
        av = self._av("", teto_hora=2)
        for n in range(4):
            av.processar(f"551199999010{n}@s.whatsapp.net", "")
        self.assertEqual(len(self.perguntas), 2)


class PuxarAgenda(unittest.TestCase):
    """A agenda inteira vem sem novo pareamento: remendo no Baileys montado por cima do arquivo da imagem v2.3.7."""

    def test_remendo_montado_e_amarrado_a_imagem(self):
        compose = (RAIZ / "infra" / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("./patches/baileys-chats.js:/evolution/node_modules/baileys/lib/Socket/chats.js:ro", compose)
        self.assertIn("evoapicloud/evolution-api:v2.3.7", compose, "tag mudou: reveja infra/patches/README.md")
        self.assertIn("78f668ff34e73ae8b7539a98e1f222d6", (RAIZ / "infra" / "patches" / "README.md").read_text(encoding="utf-8"))

    def test_remendo_so_age_com_o_sinal_e_corrige_a_chave(self):
        js = (RAIZ / "infra" / "patches" / "baileys-chats.js").read_text(encoding="utf-8")
        self.assertIn("/evolution/instances/greg-puxar-agenda", js)
        self.assertLess(js.index("if (!existsSync(sinal))"), js.index("await resyncAppState([nome], false)"))
        self.assertIn("unlinkSync(sinal)", js)                                  # uma vez por pedido
        self.assertIn("Buffer.from(key.keyData, 'base64')", js)               # chave em texto -> bytes
        self.assertNotIn("infoOriginal", js)                                    # diagnóstico temporário não fica

    def test_comando_e_icone_com_nome_novo(self):
        sh = (RAIZ / "bin" / "evolution.sh").read_text(encoding="utf-8")
        self.assertIn("puxar-agenda)", sh)
        self.assertIn("<string>Greg</string>", (RAIZ / "macapp" / "Info.plist").read_text(encoding="utf-8").split("CFBundleIconFile")[1][:40])
        self.assertIn("Resources/Greg.icns", (RAIZ / "bin" / "construir-app.sh").read_text(encoding="utf-8"))
        self.assertIn("Resources/Greg.icns", (RAIZ / "bin" / "construir-dmg.sh").read_text(encoding="utf-8"))


class Album(unittest.TestCase):
    """Pedido 56: foto e vídeo de álbum (associatedChildMessage) aparecem como mídia; marcador de álbum e edição
    cifrada não viram balão "abra no celular"."""

    def test_filho_de_album_e_midia_comum(self):
        foto = {"messageContextInfo": {}, "associatedChildMessage": {"message": {"imageMessage": {"caption": "oi"}}}}
        self.assertEqual(wa.conteudo(foto, "imageMessage"), {"tipo": "imagem", "texto": "oi", "extra": ""})
        video = {"associatedChildMessage": {"message": {"videoMessage": {"seconds": 12}}}}
        self.assertEqual(wa.conteudo(video, "videoMessage")["tipo"], "video")
        self.assertEqual(lotes.interna_de_album(foto), {"imageMessage": {"caption": "oi"}})
        self.assertIsNone(lotes.interna_de_album({"imageMessage": {}}))
        self.assertIsNone(lotes.interna_de_album(None))

    def test_consultas_devolvem_o_tipo_de_dentro_e_escondem_o_que_nao_tem_conteudo(self):
        for sql in (wa.sql_lista(0), wa.sql_mensagens("5511999990000@s.whatsapp.net", 0), wa.sql_chave("ABC123", 0)):
            self.assertIn("associatedChildMessage'->'message') k", sql)
        self.assertIn("albumMessage", lotes.SEM_CONTEUDO)
        self.assertIn("secretEncryptedMessage", lotes.SEM_CONTEUDO)
        self.assertNotIn("associatedChildMessage", lotes.SEM_CONTEUDO)

    def test_pedido_de_midia_leva_a_mensagem_de_dentro_so_no_album(self):
        from app.evolution_client import EvolutionClient
        vistos = []

        class Cli(EvolutionClient):
            def __init__(self):
                pass

            def _i(self):
                return "x"

            def _pedir(self, metodo, caminho, corpo=None, **kw):
                vistos.append(corpo)
                return {"base64": "/9j/4AAQ"}
        cli = Cli()
        cli.midia_base64({"id": "A"})
        cli.midia_base64({"id": "B"}, interna={"imageMessage": {"mediaKey": "k"}})
        self.assertEqual(vistos[0]["message"], {"key": {"id": "A"}})
        self.assertEqual(vistos[1]["message"], {"key": {"id": "B"}, "message": {"imageMessage": {"mediaKey": "k"}}})


class FotoEMovimento(unittest.TestCase):
    def test_foto_tem_botao_de_baixar_direto(self):
        from app.paginas import conversas
        h = conversas.html_conteudo({"id": "ABC"}, {"tipo": "imagem", "texto": "", "extra": ""})
        self.assertIn('class="zap-foto"', h)
        self.assertIn('href="/conversas/midia?m=ABC&amp;baixar=1" download aria-label="Baixar a foto"', h)
        self.assertNotIn("zap-foto", conversas.html_conteudo({"id": "ABC"}, {"tipo": "figurinha", "texto": "", "extra": ""}))

    def test_foto_com_movimento_carrega_o_primeiro_quadro_e_video_comum_nao(self):
        from app.paginas import conversas
        mov = wa.conteudo({"associatedChildMessage": {"message": {"videoMessage": {
            "seconds": 3, "motionPhotoPresentationOffsetMs": {"low": 3074, "high": 0, "unsigned": True}}}}}, "videoMessage")
        self.assertTrue(mov.get("movimento"))
        h = conversas.html_conteudo({"id": "ABC"}, mov)
        self.assertIn("Foto com movimento · 0:03", h)
        self.assertIn('preload="metadata"', h)
        comum = wa.conteudo({"videoMessage": {"seconds": 9, "motionPhotoPresentationOffsetMs": {"low": 0}}}, "videoMessage")
        self.assertNotIn("movimento", comum)
        h = conversas.html_conteudo({"id": "ABC"}, comum)
        self.assertIn("Vídeo de 0:09", h)
        self.assertIn('preload="none"', h)


if __name__ == "__main__":
    unittest.main()


class CitacaoComMiniatura(unittest.TestCase):
    """Pedido 58: foto, figurinha e vídeo citados levam miniatura; tocar na citação leva até a original."""

    def _html(self, msg, orig=None):
        from app.paginas import conversas
        m = {"citada": {"id": "K1", "de": "", "msg": msg}}
        por_chave = {"K1": orig} if orig else {}
        return conversas.html_citada(m, por_chave, "5592999990000@s.whatsapp.net", {})

    def _orig(self, **extra):
        return {"id": "a1", "key": {"id": "K1", "fromMe": True, "remoteJid": "5592999990000@s.whatsapp.net"}, "push": "", **extra}

    def test_foto_que_esta_no_fio_usa_a_propria_midia_e_aponta_para_ela(self):
        t = self._html({"imageMessage": {"caption": "arte"}}, self._orig())
        self.assertIn('<a class="zap-citada mini" href="#m-a1">', t)
        self.assertIn('<img class="zap-cit-mini" loading="lazy" src="/conversas/midia?m=a1" alt="">', t)

    def test_figurinha_no_fio_ganha_miniatura_sem_corte(self):
        t = self._html({"stickerMessage": {"mimetype": "image/webp"}}, self._orig())
        self.assertIn('class="zap-cit-mini fig"', t)

    def test_fora_do_fio_vale_a_miniatura_que_veio_na_resposta(self):
        t = self._html({"imageMessage": {"jpegThumbnail": "/9j/4AAQSkZJRg=="}})
        self.assertIn('<div class="zap-citada mini">', t)
        self.assertIn('src="data:image/jpeg;base64,/9j/4AAQSkZJRg=="', t)

    def test_miniatura_que_nao_e_base64_nao_entra_na_pagina(self):
        t = self._html({"imageMessage": {"jpegThumbnail": '"><script>alert(1)</script>'}})
        self.assertNotIn("script", t)
        self.assertNotIn("zap-cit-mini", t)

    def test_original_apagada_nao_mostra_miniatura(self):
        t = self._html({"imageMessage": {"jpegThumbnail": "/9j/4AAQ"}}, self._orig(apagada=True))
        self.assertNotIn("zap-cit-mini", t)

    def test_texto_citado_continua_sem_miniatura(self):
        t = self._html({"conversation": "oi"}, self._orig())
        self.assertIn('<a class="zap-citada" href="#m-a1">', t)

    def test_a_consulta_guarda_a_miniatura_de_foto_e_video_citados(self):
        from app import whatsapp as wa
        self.assertNotIn("{imageMessage,jpegThumbnail}' #- '{videoMessage", wa._CITADA)
        self.assertIn("{documentMessage,jpegThumbnail}", wa._CITADA)

    def test_o_clique_na_citacao_rola_ate_a_original_e_destaca(self):
        from app.paginas import conversas
        self.assertIn("closest('a.zap-citada')", conversas._JS)
        self.assertIn("scrollIntoView", conversas._JS)
        self.assertIn(".zap-msg.alvo", conversas._CSS)


class VideoNoFormatoCerto(unittest.TestCase):
    """Pedido 59: vídeo gravado em pé aparece estreito, com capa, botão de tocar na bolha e botão de abrir grande."""

    def _html(self, video):
        from app import whatsapp as wa
        from app.paginas import conversas
        return conversas.html_conteudo({"id": "v9"}, wa.conteudo({"videoMessage": video}, "videoMessage"))

    def test_video_em_pe_fica_estreito_na_proporcao_gravada(self):
        t = self._html({"seconds": 4, "width": 480, "height": 848})
        self.assertIn('<span class="zap-vid vert" style="aspect-ratio: 480 / 848">', t)

    def test_video_deitado_fica_largo(self):
        t = self._html({"seconds": 4, "width": 848, "height": 480})
        self.assertIn('<span class="zap-vid" style="aspect-ratio: 848 / 480">', t)

    def test_sem_medidas_mantem_o_formato_padrao(self):
        t = self._html({"seconds": 4})
        self.assertIn('<span class="zap-vid">', t)

    def test_medida_absurda_ou_que_nao_e_numero_e_ignorada(self):
        for v in ({"width": "1;x", "height": 9}, {"width": 10**9, "height": 5}, {"width": -4, "height": 5}):
            self.assertIn('<span class="zap-vid">', self._html(v))

    def test_capa_vem_do_banco_e_so_aceita_jpeg(self):
        import base64
        from app import whatsapp as wa
        jpeg = b"\xff\xd8\xff\xe0" + b"0" * 20
        self.assertEqual(wa.capa_do_video(lambda sql: [[base64.b64encode(jpeg).decode()]], "abc123"), jpeg)
        self.assertIsNone(wa.capa_do_video(lambda sql: [[base64.b64encode(b"<svg onload=x>").decode()]], "abc123"))
        self.assertIsNone(wa.capa_do_video(lambda sql: [["não é base64"]], "abc123"))
        self.assertIsNone(wa.capa_do_video(lambda sql: [], "abc123"))
        with self.assertRaises(ValueError):
            wa.sql_capa("a' OR 1=1 --")

    def test_a_pagina_tem_o_video_grande_e_o_comportamento(self):
        from app.paginas import conversas
        self.assertIn("zap-video-grande", conversas._JS)
        self.assertIn("data-expandir", conversas._JS)
        self.assertIn(".zap-vid.vert", conversas._CSS)


class InformacoesDaConversa(unittest.TestCase):
    """Pedido 57: painel de informações do contato e do grupo, com o que a Evolution devolve e o que o banco tem."""

    PESSOA = "5592999990000@s.whatsapp.net"
    GRUPO = "120363000000000000@g.us"

    class Evo:
        def __init__(self, comercial=True, falha=False):
            self.comercial, self.falha, self.chamadas = comercial, falha, []

        def perfil(self, jid):
            self.chamadas.append(("perfil", jid))
            if self.falha:
                raise RuntimeError("fora do ar")
            return {"name": "Loja <b>Azul</b>", "status": {"status": "Atendemos de segunda a sexta", "setAt": "2026-03-04T12:00:00.000Z"},
                    "isBusiness": self.comercial, "description": "antiga"}

        def perfil_comercial(self, jid):
            self.chamadas.append(("comercial", jid))
            return {"description": "Relógios e pulseiras https://exemplo.com.br", "category": "Loja de relógios",
                    "website": ["https://exemplo.com.br", 7], "business_hours": {"business_config": [
                        {"day_of_week": "mon", "mode": "specific_hours", "open_time": "540", "close_time": "1080"},
                        {"day_of_week": "sun", "mode": "open_24h"}, {"day_of_week": "xx", "mode": "specific_hours"}]}}

        def grupo(self, jid):
            self.chamadas.append(("grupo", jid))
            return {"subject": "Equipe", "desc": "Avisos da equipe", "creation": 1700000000, "size": 3, "announce": True,
                    "participants": [{"id": "111@lid", "phoneNumber": "5592988887777@s.whatsapp.net", "admin": "admin"},
                                     {"id": "222@lid", "phoneNumber": "", "admin": None}, {"id": "';--", "admin": None}, "lixo"]}

    def _perfis(self, evo, **k):
        import tempfile
        from pathlib import Path
        from app import whatsapp as wa
        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d, True)
        return wa.Perfis(Path(d) / "perfis.json", evo, **k)

    def test_pessoa_comercial_traz_recado_categoria_site_e_horario(self):
        evo = self.Evo()
        p, estado = self._perfis(evo).obter(self.PESSOA)
        self.assertEqual(estado, "novo")
        self.assertEqual((p["recado"], p["categoria"], p["sites"]), ("Atendemos de segunda a sexta", "Loja de relógios", ["https://exemplo.com.br"]))
        self.assertEqual(p["descricao"], "Relógios e pulseiras https://exemplo.com.br")
        self.assertEqual(len(p["horario"]), 3)

    def test_segunda_abertura_usa_o_guardado_e_nao_pergunta_de_novo(self):
        evo = self.Evo(comercial=False)
        perfis = self._perfis(evo)
        perfis.obter(self.PESSOA)
        _, estado = perfis.obter(self.PESSOA)
        self.assertEqual(estado, "guardado")
        self.assertEqual(evo.chamadas, [("perfil", self.PESSOA)])

    def test_vencido_pergunta_de_novo(self):
        agora = [1000.0]
        evo = self.Evo(comercial=False)
        perfis = self._perfis(evo, relogio=lambda: agora[0])
        perfis.obter(self.PESSOA)
        agora[0] += 86401
        self.assertEqual(perfis.obter(self.PESSOA)[1], "novo")

    def test_numero_escondido_e_jid_torto_nunca_sao_consultados(self):
        evo = self.Evo()
        perfis = self._perfis(evo)
        self.assertEqual(perfis.obter("123456@lid"), ({}, "sem"))
        self.assertEqual(perfis.obter("x' OR 1=1"), ({}, "sem"))
        self.assertEqual(evo.chamadas, [])

    def test_teto_por_hora_e_falha_nao_repetem_a_pergunta(self):
        evo = self.Evo(comercial=False)
        perfis = self._perfis(evo, teto_hora=1)
        perfis.obter(self.PESSOA)
        self.assertEqual(perfis.obter("5592911112222@s.whatsapp.net")[1], "teto")
        ruim = self.Evo(falha=True)
        perfis = self._perfis(ruim)
        self.assertEqual(perfis.obter(self.PESSOA), ({}, "indisponivel"))
        self.assertEqual(perfis.obter(self.PESSOA), ({}, "indisponivel"))
        self.assertEqual(len(ruim.chamadas), 1)

    def test_grupo_guarda_descricao_e_participantes_validos(self):
        p, _ = self._perfis(self.Evo()).obter(self.GRUPO)
        self.assertEqual((p["assunto"], p["descricao"], p["membros"], p["so_admin_fala"]), ("Equipe", "Avisos da equipe", 3, True))
        self.assertEqual(p["participantes"], [{"jid": "5592988887777@s.whatsapp.net", "admin": True}, {"jid": "222@lid", "admin": False}])

    def test_painel_da_pessoa_escapa_o_que_vem_do_whatsapp(self):
        from app.paginas import conversas
        p, _ = self._perfis(self.Evo()).obter(self.PESSOA)
        t = conversas.html_perfil(self.PESSOA, "Ana", False, "", p, "novo", {"fotos": ["a1", "a2"], "videos": 1, "documentos": 0, "cheio": False})
        self.assertIn("No WhatsApp: ~Loja &lt;b&gt;Azul&lt;/b&gt;", t)
        self.assertNotIn("<b>Azul</b>", t)
        self.assertIn("Conta comercial", t)
        self.assertIn("<small>desde 04/03/2026</small>", t)
        self.assertIn('<tr><th scope="row">Segunda</th><td>09:00 às 18:00</td></tr>', t)
        self.assertIn('<tr><th scope="row">Domingo</th><td>24 horas</td></tr>', t)
        self.assertIn("2 fotos · 1 vídeo", t)
        self.assertEqual(t.count('data-ampliar="/conversas/midia?m='), 2)
        self.assertIn('action="/conversas/apelido"', t)

    def test_painel_do_grupo_lista_participantes_com_admin_primeiro(self):
        from app.paginas import conversas
        p, _ = self._perfis(self.Evo()).obter(self.GRUPO)
        t = conversas.html_perfil(self.GRUPO, "Equipe", False, "", p, "novo", None, {"5592988887777@s.whatsapp.net": "Bia Souza"}, {})
        self.assertIn("Grupo · 3 participantes", t)
        self.assertIn("<li><span>Bia Souza</span><small>admin</small></li>", t)
        self.assertIn("Só administradores enviam mensagens neste grupo.", t)
        self.assertNotIn('action="/conversas/apelido"', t)

    def test_sem_resposta_do_whatsapp_o_painel_avisa_e_mostra_o_que_tem(self):
        from app.paginas import conversas
        t = conversas.html_perfil(self.PESSOA, "Ana", False, "Aninha", {}, "indisponivel", {"fotos": [], "videos": 0, "documentos": 0, "cheio": False})
        self.assertIn("O WhatsApp não respondeu agora", t)
        self.assertIn("Nenhuma mídia no período que o Greg guarda", t)
        self.assertIn('value="Aninha"', t)

    def test_midias_da_conversa_ignoram_apagadas_e_recusam_jid_torto(self):
        from app import whatsapp as wa
        linhas = [["a1", "imageMessage", "false"], ["a2", "imageMessage", "true"], ["a3", "videoMessage", "false"],
                  ["a4", "documentMessage", "false"]]
        m = wa.midias(lambda sql: linhas, self.PESSOA, desde=0)
        self.assertEqual((m["fotos"], m["videos"], m["documentos"]), (["a1"], 1, 1))
        with self.assertRaises(ValueError):
            wa.sql_midias("x' OR 1=1", 0)

    def test_cabecalho_da_conversa_abre_o_painel(self):
        from app.paginas import conversas
        t = conversas._toolbar_fio(self.PESSOA, "Ana", False, "+55 92 99999-0000", perfil=True)
        self.assertIn('data-perfil-abrir aria-expanded="false" aria-controls="zap-perfil"', t)
        self.assertNotIn("data-perfil-abrir", conversas._toolbar_fio(self.PESSOA, "Ana", False, "Fora do Greg"))
        self.assertIn("/conversas/perfil?c=", conversas._JS)
        self.assertIn("/conversas/perfil", conversas.ROTAS)
