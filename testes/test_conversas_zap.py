"""r02 (D-39): Conversas do WhatsApp dentro do Greg — regra de nome, grupos, fotos, conversa aberta, mídia e envio
com trava do Touch ID. Banco da Evolution simulado por um psql falso; Evolution pelo FakeEvolution."""
import json
import os
import re
import stat
import sys
import time
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import whatsapp as wa  # noqa: E402
from app.cursor import HISTORICO_S  # noqa: E402
from app.envio import Liberacao  # noqa: E402
from app.paginas import _comum as c  # noqa: E402
from app.paginas import conversas  # noqa: E402
from testes.fakes import imagem_jpeg_minima  # noqa: E402
from testes.test_paginas import XSS, Base, texto  # noqa: E402

TOKEN = "token-de-envio-teste"
PAR = int(time.time()) - 3 * 86400
AGORA = int(time.time()) - 60
G1 = "120363000000000001@g.us"
ANA = "5592900001111@s.whatsapp.net"
SEMNOME = "5592900002222@s.whatsapp.net"
LID = "998877665544@lid"
MARCOS = "5592900003333@s.whatsapp.net"


def k(remote, fromme=False, mid="X", **extra):
    d = {"remoteJid": remote, "fromMe": fromme, "id": mid}
    d.update(extra)
    return json.dumps(d)


class PsqlFalso:
    """Responde as três consultas do módulo whatsapp e guarda o texto de cada uma."""

    def __init__(self):
        self.consultas: list[str] = []
        self.lista: list[list[str]] = []
        self.msgs: dict[str, list[list[str]]] = {}
        self.chaves: dict[str, list[str]] = {}
        self.reacoes: dict[str, list[list[str]]] = {}   # jid -> [key, alvo, emoji, push], da mais nova para a mais antiga
        self.bloqueados: list[str] = []
        self.agenda: dict[str, list[list[str]]] = {t: [] for t in wa.TIPOS_AGENDA}   # /bloqueios (pedido 29)
        self.falhar = False
        self.falhar_bloqueados = False

    def __call__(self, consulta: str):
        self.consultas.append(consulta)
        if self.falhar:
            raise wa.lotes.PostgresIndisponivel("fora")
        if 'WHERE c."remoteJid" IN (' in consulta:   # nomes da agenda (pedido 54: com vigia.agenda na frente)
            todos = [l for t in wa.TIPOS_AGENDA for l in self.agenda[t]]
            return [[l[0], l[1]] for l in todos if f"'{l[0]}'" in consulta]
        if 'FROM public."Contact" c WHERE' in consulta:
            for tipo, sufixo in wa.TIPOS_AGENDA.items():
                if f"LIKE '%{sufixo}'" in consulta:
                    return self.agenda[tipo]
            return []
        if 'count(DISTINCT "remoteJid")' in consulta:
            return [[str(len(self.agenda[t])) for t in wa.TIPOS_AGENDA]]
        if "vigia.bloqueados" in consulta:
            if self.falhar_bloqueados:
                raise wa.lotes.PostgresIndisponivel("fora")
            return [[j] for j in self.bloqueados]
        if "DISTINCT ON (conversa)" in consulta:
            return self.lista
        if "WHERE m.id = '" in consulta:
            mid = consulta.split("WHERE m.id = '", 1)[1].split("'", 1)[0]
            return [self.chaves[mid]] if mid in self.chaves else []
        for jid, linhas in self.msgs.items():
            if f"= '{jid}'" in consulta:
                # as reações vêm na mesma consulta, como linhas reactionMessage (pedido 50, rodada 2)
                reacoes = [[f"r{i}", key, push, "reactionMessage", json.dumps({"alvo": alvo, "emoji": emoji}),
                            str(2_000_000_000 - i), "", ""]
                           for i, (key, alvo, emoji, push) in enumerate(self.reacoes.get(jid, []))]
                return linhas + reacoes
        return []


def linha_lista(jid, ts, mid, key, push_ult, tipo, msg, nao_lidas, push, contato, foto=""):
    return [jid, str(ts), mid, key, push_ult, tipo, json.dumps(msg), str(nao_lidas), push, contato, foto]


class TestRegras(unittest.TestCase):
    def test_numero_formatado(self):
        self.assertEqual(wa.numero_formatado("5592900001111@s.whatsapp.net"), "+55 92 90000-1111")
        self.assertEqual(wa.numero_formatado("559232345678@s.whatsapp.net"), "+55 92 3234-5678")
        self.assertEqual(wa.numero_formatado("14155550100@s.whatsapp.net"), "+14155550100")

    def test_nome_da_conversa_segue_a_ordem(self):
        self.assertEqual(wa.nome_da_conversa(ANA, apelido="Dra. Ana", contato="Ana P", push="Aninha"), "Dra. Ana")
        self.assertEqual(wa.nome_da_conversa(ANA, contato="Ana P", push="Aninha"), "Ana P")
        self.assertEqual(wa.nome_da_conversa(ANA, contato="", push="Aninha"), "Aninha")
        # nome que é o próprio número não conta como nome (regra do Chatwoot)
        self.assertEqual(wa.nome_da_conversa(ANA, contato="5592900001111", push="+55 92 90000-1111"), "+55 92 90000-1111")
        self.assertEqual(wa.nome_da_conversa(G1, contato="Marcos", push="Marcos", grupo="CBA · Marketing"), "CBA · Marketing")
        self.assertEqual(wa.nome_da_conversa(G1, contato="Marcos"), "Grupo")
        self.assertEqual(wa.nome_da_conversa(LID, push="Carla"), "~Carla")
        self.assertEqual(wa.nome_da_conversa(LID), "Contato sem número")

    def test_remetente_em_grupo_usa_telefone_do_lid(self):
        self.assertEqual(wa.remetente({"participant": "111@lid", "participantAlt": MARCOS}, G1), MARCOS)
        self.assertEqual(wa.remetente({"participant": MARCOS}, G1), MARCOS)
        self.assertEqual(wa.remetente({"participant": MARCOS}, ANA), ANA)

    def test_normalizar_numero(self):
        self.assertEqual(wa.normalizar_numero("92 90000 1111"), "5592900001111")
        self.assertEqual(wa.normalizar_numero("(92) 3234-5678"), "559232345678")
        self.assertEqual(wa.normalizar_numero("+1 415 555 0100"), "14155550100")
        self.assertEqual(wa.normalizar_numero("0044 20 7946 0000"), "442079460000")
        self.assertEqual(wa.normalizar_numero("abc"), "")

    def test_conteudo_mantem_quebra_e_tira_invisiveis(self):
        cont = wa.conteudo({"conversation": "linha 1\nlinha 2‮ invertido"})
        self.assertEqual(cont["texto"], "linha 1\nlinha 2 invertido")
        img = wa.conteudo({"imageMessage": {"caption": "olha"}}, "imageMessage")
        self.assertEqual((img["tipo"], img["texto"]), ("imagem", "olha"))
        self.assertEqual(wa.previa(wa.conteudo({"audioMessage": {"seconds": 42}}, "audioMessage")), "Áudio · 0:42")

    def test_sql_recusa_injecao_e_traz_trava_do_pareamento(self):
        with self.assertRaises(ValueError):
            wa.sql_mensagens("x' OR '1'='1@s.whatsapp.net", PAR)
        with self.assertRaises(ValueError):
            wa.sql_chave("1'; DROP TABLE x;--", PAR)
        for sql in (wa.sql_lista(PAR), wa.sql_mensagens(ANA, PAR)):
            self.assertIn(f'"messageTimestamp" >= {PAR}', sql)
            self.assertIsNone(re.search(r"\b(UPDATE|DELETE|INSERT|DROP|ALTER)\b", sql, re.IGNORECASE))
            self.assertIn("@broadcast", sql)

    def test_sql_tolera_message_nulo_e_a_lista_nao_cai_por_uma_linha(self):
        # 09/11/2026: dois documentMessage com `message` = JSON null; `#-` em escalar dá "cannot delete path in scalar"
        # e a lista inteira de Conversas virava "O banco da Evolution não respondeu agora" por causa de UMA linha.
        guarda = "CASE WHEN jsonb_typeof(m.message) = 'object' THEN m.message #- "
        self.assertIn(guarda, wa.sql_lista(PAR))
        self.assertIn(guarda, wa.sql_mensagens(ANA, PAR))
        self.assertIn("ELSE m.message END)::text", wa.sql_lista(PAR))
        linha = [ANA, str(AGORA), "ID1", k(ANA, mid="ID1"), "", "documentMessage", "null", "0", "", "", ""]
        itens = wa.listar(lambda q: [linha], desde=PAR)
        self.assertEqual((len(itens), itens[0]["message"], itens[0]["tipo_msg"]), (1, {}, "documentMessage"))
        self.assertIn("tipo", wa.conteudo(itens[0]["message"], itens[0]["tipo_msg"]))
        msgs = wa.mensagens(lambda q: [["ID1", k(ANA, mid="ID1"), "", "documentMessage", "null", str(AGORA), ""]],
                            ANA, desde=PAR)
        self.assertEqual((len(msgs), msgs[0]["message"]), (1, {}))

    def test_iniciais(self):
        self.assertEqual(wa.iniciais("Ana Paula"), "AP")
        self.assertEqual(wa.iniciais("~Carla"), "CA")
        self.assertEqual(wa.iniciais("+55 92"), "#")


class TestLiberacao(unittest.TestCase):
    def test_padrao_e_oito_horas_e_o_tempo_vem_de_ajustes(self):
        """Pedido 70: o tempo da trava é escolha da pessoa (Ajustes), lido a cada liberação; padrão 8 horas."""
        from app import envio, preferencias as pf
        self.assertEqual((envio.MINUTOS, pf.PADRAO["envio_min"]), (480, 480))
        t, escolha = [1000.0], [480]
        lib = Liberacao(minutos=lambda: escolha[0], relogio=lambda: t[0])
        lib.liberar(TOKEN, TOKEN)
        self.assertEqual(lib.restante(), 8 * 3600)
        escolha[0] = 60                      # mudou em Ajustes: a trava aberta segue; a próxima abre com o tempo novo
        self.assertEqual(lib.restante(), 8 * 3600)
        lib.liberar(TOKEN, TOKEN)
        self.assertEqual(lib.restante(), 3600)
        escolha[0] = "lixo"
        lib.liberar(TOKEN, TOKEN)
        self.assertEqual(lib.restante(), 8 * 3600)
        self.assertEqual(pf.validar({"envio_min": "60"})["envio_min"], 60)
        self.assertEqual(pf.validar({"envio_min": "7"})["envio_min"], 480)

    def test_abre_por_dez_minutos_so_com_token_certo(self):
        t = [1000.0]
        lib = Liberacao(minutos=10, relogio=lambda: t[0])
        self.assertFalse(lib.liberar("errado", TOKEN))
        self.assertFalse(lib.liberar("", ""))
        self.assertFalse(lib.liberada())
        self.assertTrue(lib.liberar(TOKEN, TOKEN))
        self.assertEqual(lib.restante(), 600)
        t[0] += 599
        self.assertTrue(lib.liberada())
        t[0] += 2
        self.assertFalse(lib.liberada())
        lib.liberar(TOKEN, TOKEN)
        lib.travar()
        self.assertFalse(lib.liberada())


class _Abridor:
    def __init__(self, dados=b"", erro=None):
        self.dados, self.erro, self.urls = dados, erro, []

    def open(self, req, timeout=None):
        self.urls.append(req.full_url)
        if self.erro:
            raise self.erro
        import io
        return io.BytesIO(self.dados)


class EvoGrupos:
    """Falso mínimo: conta as consultas de grupo; nunca tem fetchAllGroups (D-40)."""

    def __init__(self, nomes=None, falhar=False):
        self.nomes = nomes or {}
        self.consultas: list[str] = []
        self.falhar = falhar

    def grupo(self, jid):
        self.consultas.append(jid)
        if self.falhar:
            raise wa.lotes.PostgresIndisponivel("fora")
        return {"id": jid, "subject": self.nomes.get(jid, ""), "pictureUrl": "", "size": 3} if jid in self.nomes else {}


class TestGruposUmPorVez(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.arq = Path(self.tmp.name) / "grupos.json"
        self.t = [1_800_000_000.0]
        self.dormidas: list[float] = []

    def tearDown(self):
        self.tmp.cleanup()

    def grupos(self, evo, **kw):
        return wa.Grupos(self.arq, evo, relogio=lambda: self.t[0], dormir=self.dormidas.append, **kw)

    def test_cliente_nao_tem_fetch_all_groups(self):
        from app.evolution_client import EvolutionClient
        self.assertFalse(hasattr(EvolutionClient, "grupos"))
        fonte = (Path(__file__).resolve().parents[1] / "app").rglob("*.py")
        for arq in fonte:
            codigo = "\n".join(l for l in arq.read_text(encoding="utf-8").splitlines() if not l.lstrip().startswith("#"))
            self.assertNotIn('"/group/fetchAllGroups', codigo, arq.name)

    def test_so_os_vistos_um_por_vez_com_pausa_e_cache(self):
        g2, g3 = "120363000000000002@g.us", "120363000000000003@g.us"
        evo = EvoGrupos({G1: "CBA · Marketing", g2: "Família", g3: "Velta"})
        gr = self.grupos(evo)
        self.assertEqual(gr.nomes([G1, g2, ANA, G1], sincrono=True)[G1]["subject"], "CBA · Marketing")
        self.assertEqual(evo.consultas, [G1, g2])           # contato não consulta; repetido não repete
        gr.nomes([G1, g2, g3], sincrono=True)
        self.assertEqual(evo.consultas, [G1, g2, g3])       # cache vale: só o novo
        self.t[0] += 8 * 86400
        gr.nomes([G1], sincrono=True)
        self.assertEqual(evo.consultas[-1], G1)             # venceu em 7 dias: consulta de novo

    def test_thread_dorme_entre_consultas(self):
        g2 = "120363000000000002@g.us"
        evo = EvoGrupos({G1: "A", g2: "B"})
        gr = self.grupos(evo, pausa=4.0)
        gr._pendentes = [G1, g2]
        gr._rodando = True
        gr._rodar()
        self.assertEqual(evo.consultas, [G1, g2])
        self.assertEqual(self.dormidas, [4.0])

    def test_teto_por_hora(self):
        jids = [f"1203630000000000{i:02d}@g.us" for i in range(10)]
        evo = EvoGrupos({j: "g" for j in jids})
        gr = self.grupos(evo, teto_hora=3)
        gr.nomes(jids, sincrono=True)
        self.assertEqual(len(evo.consultas), 3)
        self.t[0] += 3601
        gr.nomes(jids, sincrono=True)
        self.assertEqual(len(evo.consultas), 6)

    def test_falha_so_tenta_de_novo_depois_de_6h(self):
        evo = EvoGrupos(falhar=True)
        gr = self.grupos(evo)
        gr.nomes([G1], sincrono=True)
        gr.nomes([G1], sincrono=True)
        self.assertEqual(evo.consultas, [G1])
        self.t[0] += 6 * 3600 + 1
        gr.nomes([G1], sincrono=True)
        self.assertEqual(evo.consultas, [G1, G1])

    def test_lembrar_nao_consulta(self):
        evo = EvoGrupos()
        gr = self.grupos(evo)
        gr.lembrar(G1, "J. Grupo")
        self.assertEqual(gr.nomes([G1], sincrono=True)[G1]["subject"], "J. Grupo")
        self.assertEqual(evo.consultas, [])
        self.assertEqual(stat.S_IMODE(os.stat(self.arq).st_mode), 0o600)


class TestFotos(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pasta = Path(self.tmp.name) / "avatares"

    def test_baixar_so_do_cdn_do_whatsapp_e_so_imagem(self):
        jpg = imagem_jpeg_minima()
        ab = _Abridor(jpg)
        self.assertEqual(wa.baixar_foto("https://pps.whatsapp.net/v/t61/123_n.jpg?oe=1", abridor=ab), jpg)
        for url in ("http://pps.whatsapp.net/a.jpg", "https://evil.com/a.jpg", "https://whatsapp.net.evil.com/a.jpg",
                    "file:///etc/passwd", "https://127.0.0.1/a.jpg"):
            with self.assertRaises(ValueError, msg=url):
                wa.baixar_foto(url, abridor=ab)
        with self.assertRaises(ValueError):
            wa.baixar_foto("https://pps.whatsapp.net/a.jpg", abridor=_Abridor(b"<html>nao"))
        with self.assertRaises(ValueError):
            wa.baixar_foto("https://pps.whatsapp.net/a.jpg", abridor=_Abridor(jpg + b"x" * 100), limite=50)

    def test_baixa_uma_vez_e_de_novo_so_quando_o_arquivo_muda(self):
        baixadas = []

        def baixar(url):
            baixadas.append(url)
            return imagem_jpeg_minima()

        av = wa.Avatares(self.pasta, baixar=baixar)
        u1 = "https://pps.whatsapp.net/v/t61/111_n.jpg?oe=AAA"
        self.assertTrue(av.processar(ANA, u1))
        arq = av.arquivo(ANA)
        self.assertIsNotNone(arq)
        self.assertEqual(stat.S_IMODE(os.stat(arq).st_mode), 0o600)
        ind = av.indice()
        self.assertFalse(av.precisa(ANA, "https://pps.whatsapp.net/v/t61/111_n.jpg?oe=BBB", ind))  # só a validade mudou
        self.assertTrue(av.precisa(ANA, "https://pps.whatsapp.net/v/t61/222_n.jpg?oe=BBB", ind))   # foto nova
        self.assertFalse(av.precisa(ANA, "https://evil.com/222_n.jpg", ind))
        self.assertFalse(av.precisa("x'@s.whatsapp.net", u1, ind))

    def test_url_expirada_pede_url_nova_a_evolution_uma_vez(self):
        class Evo:
            pedidos = []

            def foto_perfil(self, jid):
                self.pedidos.append(jid)
                return "https://pps.whatsapp.net/v/t61/333_n.jpg?oe=NOVO"

        chamadas = []

        def baixar(url):
            chamadas.append(url)
            if "oe=VELHO" in url:
                raise urllib.error.HTTPError(url, 403, "expirou", {}, None)
            return imagem_jpeg_minima()

        evo = Evo()
        av = wa.Avatares(self.pasta, evo, baixar=baixar)
        self.assertTrue(av.processar(ANA, "https://pps.whatsapp.net/v/t61/333_n.jpg?oe=VELHO"))
        self.assertEqual(evo.pedidos, [ANA])
        self.assertEqual(len(chamadas), 2)

    def test_falha_nao_repete_a_cada_pagina(self):
        def baixar(url):
            raise urllib.error.URLError("sem rede")

        av = wa.Avatares(self.pasta, baixar=baixar)
        u = "https://pps.whatsapp.net/v/t61/444_n.jpg"
        self.assertFalse(av.processar(ANA, u))
        self.assertFalse(av.precisa(ANA, u, av.indice()))
        self.assertIsNone(av.arquivo(ANA))


class BaseZap(Base):
    def setUp(self):
        super().setUp()
        self.dados.mkdir(parents=True, exist_ok=True)
        (self.dados / "cursor.json").write_text(json.dumps({"ultimo_seq": 0, "pareado_em": PAR}))
        self.ctx.tokens.envio = TOKEN
        self.psql = PsqlFalso()
        self.ctx.psql = self.psql
        self.fe.grupos = [{"id": G1, "subject": "CBA · Marketing", "pictureUrl": "", "size": 8}]
        self.psql.lista = [
            linha_lista(G1, AGORA, "g1m3", k(G1, mid="A3", participant=MARCOS), "Marcos", "conversation",
                        {"conversation": "o link do convênio já está no ar?"}, 3, "Marcos", "Marcos"),
            linha_lista(ANA, AGORA - 600, "a1m2", k(ANA, mid="B2"), "Ana Paula", "conversation",
                        {"conversation": "Consegue mandar a arte até sexta?"}, 2, "Ana Paula", ""),
            linha_lista(SEMNOME, AGORA - 1200, "s1m1", k(SEMNOME, mid="C1"), "", "conversation",
                        {"conversation": "Bom dia, é o dono?"}, 1, "", "5592900002222"),
            linha_lista(LID, AGORA - 1800, "l1m1", k(LID, mid="D1"), "Carla", "audioMessage",
                        {"audioMessage": {"seconds": 42}}, 0, "Carla", ""),
            linha_lista(MARCOS, AGORA - 2400, "m1m1", k(MARCOS, True, "E1"), "", "conversation",
                        {"conversation": "chego às 19h"}, 0, "", ""),
        ]
        self.psql.msgs[G1] = [
            ["g1m3", k(G1, mid="A3", participant=MARCOS), "Marcos", "conversation",
             json.dumps({"conversation": f"o link já está no ar? {XSS}"}), str(AGORA), "DELIVERY_ACK"],
            ["g1m2", k(G1, mid="A2", participant="111@lid", participantAlt=SEMNOME), "", "audioMessage",
             json.dumps({"audioMessage": {"seconds": 18}}), str(AGORA - 30), "DELIVERY_ACK"],
            ["g1m1", k(G1, mid="A1", participant=ANA), "Ana Paula", "imageMessage",
             json.dumps({"imageMessage": {"caption": "Ficou assim"}}), str(AGORA - 60), "READ"],
            ["g1m4", k(G1, True, "A4"), "", "conversation", json.dumps({"conversation": "Sim, subiu hoje."}),
             str(AGORA + 10), "READ"],
        ]
        self.psql.chaves["g1m1"] = [k(G1, mid="A1", participant=ANA), "imageMessage"]
        self.psql.chaves["g1m2"] = [k(G1, mid="A2", participant="111@lid"), "audioMessage"]

    def get(self, caminho, **query):
        return conversas.ROTAS[caminho](self.req("GET", caminho, query=query))

    def post(self, caminho, corpo=None, cab=None):
        return conversas.ROTAS[caminho](self.req("POST", caminho, corpo or {}, cab=cab))

    def liberar(self):
        return self.post("/envio/liberar", cab={"x-vigia-envio": TOKEN})


class TestLista(BaseZap):
    def test_nomes_numero_grupo_lid_e_nao_lidas(self):
        conversas.servicos(self.ctx).grupos.nomes([G1], sincrono=True)
        t = texto(self.get("/conversas"))
        for esperado in ("CBA · Marketing", "Ana Paula", "+55 92 90000-2222", "~Carla", "Marcos: o link do convênio",
                         "Você: chego às 19h", "Áudio · 0:42"):
            self.assertIn(c.esc(esperado), t)
        self.assertIn('aria-label="3 não lidas"', t)
        self.assertIn("Não lidas <b>3</b>", t)
        self.assertNotIn("5592900002222</span>", t)   # sem nome mostra o número formatado, não os dígitos crus
        self.assertIn(f'"messageTimestamp" >= {PAR - HISTORICO_S}', self.psql.consultas[0])

    def test_filtros_e_busca(self):
        conversas.servicos(self.ctx).grupos.nomes([G1], sincrono=True)
        t = texto(self.get("/conversas", f="grupos"))
        self.assertIn("CBA · Marketing", t)
        self.assertNotIn(">Ana Paula<", t)
        t = texto(self.get("/conversas", f="naolidas"))
        self.assertNotIn("~Carla", t)
        t = texto(self.get("/conversas", q="ana"))
        self.assertIn(">Ana Paula<", t)
        self.assertNotIn(">~Carla<", t)
        t = texto(self.get("/conversas", q="2222"))
        self.assertIn("+55 92 90000-2222", t)

    def test_apelido_vence_e_grupo_desconhecido_dispara_busca(self):
        self.banco.definir_apelido(ANA, "Dra. Ana · arte")
        t = texto(self.get("/conversas"))
        self.assertIn("Dra. Ana · arte", t)
        r = self.post("/conversas/apelido", {"c": ANA, "nome": ""})
        self.assertEqual(r.status, 303)
        self.assertNotIn(ANA, self.banco.apelidos())
        self.assertEqual(self.post("/conversas/apelido", {"c": G1, "nome": "x"}).status, 400)

    def test_sem_pareamento_nao_consulta_e_banco_fora_avisa(self):
        (self.dados / "cursor.json").write_text(json.dumps({"ultimo_seq": 0, "pareado_em": None}))
        t = texto(self.get("/conversas"))
        self.assertIn("ainda não foi pareado", t)
        self.assertEqual(self.psql.consultas, [])
        (self.dados / "cursor.json").write_text(json.dumps({"ultimo_seq": 0, "pareado_em": PAR}))
        self.psql.falhar = True
        r = self.get("/conversas")
        self.assertEqual(r.status, 200)
        self.assertIn("não respondeu agora", texto(r))

    def test_nunca_marca_como_lida(self):
        conversas.servicos(self.ctx).grupos.nomes([G1], sincrono=True)
        self.get("/conversas")
        self.get("/conversas", c=G1)
        self.get("/conversas/midia", m="g1m1")
        self.assertFalse([ch for ch in self.fe.chamadas if "read" in ch[1].lower() or "markMessage" in ch[1]])


class TestConversaAberta(BaseZap):
    def test_baloes_remetentes_midia_e_selo_da_ia(self):
        conversas.servicos(self.ctx).grupos.nomes([G1], sincrono=True)
        self.classificar(msg_id="g1m3", conversa=G1, contato="Marcos", classe="demanda", resumo="link do convênio")
        t = texto(self.get("/conversas", c=G1))
        self.assertIn("Grupo com 8 participantes", t)
        self.assertIn('class="zap-quem"', t)
        self.assertIn(">Marcos<", t)
        self.assertIn(">+55 92 90000-2222<", t)       # participante LID mostrado pelo telefone alternativo
        self.assertIn('src="/conversas/midia?m=g1m1"', t)
        self.assertIn("<audio controls", t)
        self.assertIn("link do convênio", t)
        self.assertIn('class="classe demanda"', t)   # Papel: ponto + palavra, com link para a demanda
        self.assertNotIn("<script>alert", t)
        self.assertIn('class="zap-msg saida', t)
        self.assertIn('class="lido"', t)
        self.assertIn('data-para="120363000000000001@g.us"', t)

    def test_lid_sem_numero_nao_tem_caixa_de_envio(self):
        t = texto(self.get("/conversas", c=LID))
        self.assertIn("responda pelo celular", t)
        self.assertNotIn('id="zap-compor"', t)

    def test_conversa_invalida_e_ignorada(self):
        r = self.get("/conversas", c="x'><script>@g.us")
        self.assertEqual(r.status, 200)
        self.assertNotIn("<script>@", texto(r))

    def test_midia_imagem_com_cache_e_audio_com_faixa(self):
        r = self.get("/conversas/midia", m="g1m1")
        self.assertEqual((r.status, r.tipo), (200, "image/jpeg"))
        self.assertTrue(list((self.dados / "caixa" / ".midia").glob("g1m1.jpg")))
        n = len(self.fe.midia_corpos)
        self.assertEqual(self.get("/conversas/midia", m="g1m1").status, 200)
        self.assertEqual(len(self.fe.midia_corpos), n)   # segunda vez veio do cache
        req = self.req("GET", "/conversas/midia", query={"m": "g1m2"}, cab={"range": "bytes=0-1"})
        r = conversas.ROTAS["/conversas/midia"](req)
        self.assertEqual((r.status, r.tipo), (206, "audio/mp4"))
        self.assertEqual(len(r.corpo), 2)
        self.assertTrue(self.fe.midia_corpos[-1]["convertToMp4"])
        self.assertEqual(self.get("/conversas/midia", m="nao-existe").status, 404)
        self.assertEqual(self.get("/conversas/midia", m="../x").status, 404)

    def test_avatar_so_quando_baixado(self):
        self.assertEqual(self.get("/conversas/avatar", j=ANA).status, 404)
        av = conversas.servicos(self.ctx).avatares
        av.baixar = lambda url: imagem_jpeg_minima()
        av.processar(ANA, "https://pps.whatsapp.net/v/t61/1_n.jpg")
        r = self.get("/conversas/avatar", j=ANA)
        self.assertEqual((r.status, r.tipo), (200, "image/jpeg"))
        t = texto(self.get("/conversas"))
        self.assertIn(f'src="/conversas/avatar?j={c.esc(conversas._q(ANA))}"', t)
        self.assertEqual(self.get("/conversas/avatar", j="../../etc").status, 404)


class TestEnvio(BaseZap):
    def test_trava_fechada_423_e_token_errado_401(self):
        r = self.post("/conversas/enviar", {"para": ANA, "texto": "oi"})
        self.assertEqual(r.status, 423)
        self.assertTrue(json.loads(texto(r))["liberar"])
        self.assertEqual(self.post("/envio/liberar", cab={"x-vigia-envio": "errado"}).status, 401)
        self.assertEqual(self.post("/envio/liberar").status, 401)
        self.assertEqual(self.fe.enviadas, [])

    def test_liberado_envia_para_pessoa_e_grupo_e_registra_sem_texto(self):
        self.assertEqual(self.liberar().status, 200)
        estado = json.loads(texto(self.get("/envio/estado")))
        self.assertTrue(estado["liberado"])
        self.assertGreater(estado["restante_s"], 590)
        r = self.post("/conversas/enviar", {"para": ANA, "texto": "Oi, Ana!"})
        self.assertEqual(r.status, 200, texto(r))
        r = self.post("/conversas/enviar", {"para": G1, "texto": "Pessoal, subiu."})
        self.assertEqual(r.status, 200, texto(r))
        self.assertEqual([e["number"] for e in self.fe.enviadas], ["5592900001111", G1])
        envios = self.banco._ler("SELECT para, hash_texto, por FROM envios ORDER BY id")
        self.assertEqual([e["por"] for e in envios], ["painel:conversa", "painel:conversa"])
        self.assertNotIn("Oi, Ana", json.dumps(envios))
        self.post("/envio/travar")
        self.assertEqual(self.post("/conversas/enviar", {"para": ANA, "texto": "de novo"}).status, 423)

    def test_recusas(self):
        self.liberar()
        for corpo in ({"para": LID, "texto": "oi"}, {"para": "5592900001111", "texto": "oi"},
                      {"para": ANA, "texto": "   "}, {"para": ANA, "texto": "x" * 4001}, {"para": ANA}):
            self.assertEqual(self.post("/conversas/enviar", corpo).status, 400, corpo)
        r = self.post("/conversas/enviar", {"para": ANA, "texto": "oi"}, cab={"sec-fetch-site": "cross-site"})
        self.assertEqual(r.status, 403)
        r = self.post("/envio/liberar", cab={"x-vigia-envio": TOKEN, "sec-fetch-site": "cross-site"})
        self.assertEqual(r.status, 403)
        self.ctx.tokens.envio = ""
        self.assertEqual(self.post("/conversas/enviar", {"para": ANA, "texto": "oi"}).status, 503)
        self.assertEqual(self.fe.enviadas, [])

    def test_nova_mensagem_confere_numero_e_salva_contato_no_vigia(self):
        self.fe.numeros["5592900004444"] = True
        r = self.post("/conversas/verificar", {"numero": "92 90000 4444"})
        d = json.loads(texto(r))
        self.assertEqual((r.status, d["existe"], d["jid"]), (200, True, "5592900004444@s.whatsapp.net"))
        self.assertEqual(d["numero"], "+55 92 90000-4444")
        d = json.loads(texto(self.post("/conversas/verificar", {"numero": "92 90000 5555"})))
        self.assertFalse(d["existe"])
        self.assertEqual(self.post("/conversas/verificar", {"numero": "123"}).status, 400)
        self.liberar()
        r = self.post("/conversas/enviar", {"para": "5592900004444@s.whatsapp.net", "texto": "Olá!",
                                            "nome": "Dra. Luana · orçamento"})
        self.assertEqual(r.status, 200)
        self.assertEqual(self.banco.apelidos()["5592900004444@s.whatsapp.net"], "Dra. Luana · orçamento")

    def test_paginas_de_nova_mensagem_e_menu(self):
        t = texto(self.get("/conversas/nova"))
        self.assertIn('id="zap-nova"', t)
        self.assertIn("Salvar como contato no Greg", t)
        self.assertIn('href="/conversas"', t)
        self.assertNotIn('href="/enviar"', t)   # Enviar saiu do menu (r02)
        self.assertIn('<section class="painel lista estreita zap-lista"', t)   # Papel: dois painéis, lista de 300 px


if __name__ == "__main__":
    unittest.main()


class BloqueioFalso:
    def __init__(self, pacote):
        self.pacote = pacote
        self.chamadas: list[tuple[str, str]] = []
        self.falhar = False

    def __call__(self, acao, jid):
        if self.falhar:
            raise wa.lotes.PostgresIndisponivel("fora")
        self.chamadas.append((acao, jid))
        if acao == "bloquear" and jid not in self.pacote.bloqueados:
            self.pacote.bloqueados.append(jid)
        if acao == "desbloquear" and jid in self.pacote.bloqueados:
            self.pacote.bloqueados.remove(jid)
        return "0"


class TestRevisaoR03(BaseZap):
    """Achados da revisão de qualidade de 07/11: um psql de bloqueados por render, timestamp ruim não derruba o fio,
    contato só é salvo quando a mensagem saiu."""

    def test_conversa_aberta_consulta_bloqueados_uma_vez_so(self):
        r = self.get("/conversas", c=G1)
        self.assertEqual(r.status, 200)
        self.assertEqual(sum(1 for q in self.psql.consultas if "vigia.bloqueados" in q), 1)
        self.assertEqual(len(self.psql.consultas), 3)       # listar, bloqueados, mensagens: cada um é um subprocesso

    def test_timestamp_fora_da_faixa_nao_derruba_o_fio(self):
        self.psql.msgs[G1][0][5] = str(10 ** 17)        # ano ~3 bilhões: fromtimestamp levanta
        self.psql.msgs[G1][1][5] = "-99999999999999"
        r = self.get("/conversas", c=G1)
        self.assertEqual(r.status, 200)
        t = texto(r)
        self.assertIn("o link já está no ar?", t)        # a mensagem aparece, só sem hora
        self.assertEqual(conversas.dia(10 ** 17), "")
        self.assertEqual(conversas._hhmm("x"), "")

    def test_contato_so_e_salvo_se_a_mensagem_saiu(self):
        from types import SimpleNamespace
        from app.erros import EnvioIncerto, VigiaErro

        def falha(*a, **k):
            raise VigiaErro("fora")

        def incerto(*a, **k):
            raise EnvioIncerto("timeout")

        self.liberar()
        evo = self.ctx.evo
        self.ctx.evo = SimpleNamespace(enviar_texto=falha)
        r = self.post("/conversas/enviar", {"para": ANA, "texto": "oi", "nome": "Ana Teste"})
        self.assertEqual(r.status, 502)
        self.assertNotIn(ANA, self.banco.apelidos())
        self.ctx.evo = SimpleNamespace(enviar_texto=incerto)
        r = self.post("/conversas/enviar", {"para": ANA, "texto": "oi", "nome": "Ana Incerta"})
        self.assertEqual(r.status, 202)
        self.assertEqual(self.banco.apelidos()[ANA], "Ana Incerta")    # pode ter saído: o contato fica
        self.ctx.evo = evo
        r = self.post("/conversas/enviar", {"para": ANA, "texto": "oi", "nome": "Ana Certa"})
        self.assertEqual(r.status, 200, texto(r))
        self.assertEqual(self.banco.apelidos()[ANA], "Ana Certa")


class TestBloqueios(BaseZap):
    def setUp(self):
        super().setUp()
        from app.paginas import ajustes, bloqueios
        self.ajustes, self.bloqueios = ajustes, bloqueios
        self.ctx.bloqueio = BloqueioFalso(self.psql)
        self.fe.convites["ConviteDeExemplo000000"] = {"id": "559500000000-1500000000@g.us", "subject": "Grupo X"}

    def bl(self, corpo, cab=None):
        return self.bloqueios.ROTAS["/bloqueios"](self.req("POST", "/bloqueios", corpo, cab=cab))

    def test_bloqueada_some_da_lista_e_aberta_nao_le_mensagem(self):
        self.psql.bloqueados = [G1]
        t = texto(self.get("/conversas"))
        self.assertNotIn("CBA · Marketing", t)
        self.assertIn(">Ana Paula<", t)
        self.psql.consultas.clear()
        t = texto(self.get("/conversas", c=G1))
        self.assertIn("Conversa bloqueada", t)
        self.assertIn('value="desbloquear"', t)
        self.assertFalse([q for q in self.psql.consultas if "LIMIT 200" in q or "ORDER BY m.\"messageTimestamp\"" in q
                          and f"'{G1}'" in q])
        self.assertNotIn("o link do convênio", t)

    def test_grupo_bloqueado_fora_da_lista_nao_e_consultado_nem_pelo_nome(self):
        G9 = "120363000000000099@g.us"
        self.psql.bloqueados = [G9]
        self.fe.chamadas.clear()
        t = texto(self.get("/conversas", c=G9))
        self.assertIn("Conversa bloqueada", t)
        import time as _t
        _t.sleep(0.2)
        self.assertNotIn(G9, self.fe.grupos_consultados)

    def test_sem_saber_os_bloqueados_nao_mostra_nada(self):
        self.psql.falhar_bloqueados = True
        t = texto(self.get("/conversas"))
        self.assertIn("não respondeu agora", t)
        self.assertNotIn(">Ana Paula<", t)
        t = texto(self.get("/conversas", c=ANA))
        self.assertNotIn("Consegue mandar a arte", t)

    def test_bloquear_pela_conversa_pelo_link_e_pelo_numero(self):
        r = self.bl({"acao": "bloquear", "c": G1, "volta": "/conversas"})
        self.assertEqual((r.status, dict(r.cabecalhos)["Location"]), (303, "/conversas?fora=1"))  # D-48: a lista avisa
        r = self.bl({"acao": "bloquear", "alvo": "https://chat.whatsapp.com/ConviteDeExemplo000000?origem=exemplo", "volta": "/ajustes"})
        self.assertEqual(r.status, 303)
        self.assertIn("bloqueado=1", dict(r.cabecalhos)["Location"])
        r = self.bl({"acao": "bloquear", "alvo": "(92) 90000-1111"})
        self.assertEqual(r.status, 303)
        self.assertEqual(self.ctx.bloqueio.chamadas, [("bloquear", G1), ("bloquear", "559500000000-1500000000@g.us"),
                                                     ("bloquear", "5592900001111@s.whatsapp.net")])
        self.assertFalse([ch for ch in self.fe.chamadas if "acceptInvite" in ch[1]])  # o link só é lido
        t = texto(self.ajustes.ROTAS["/ajustes"](self.req("GET", "/ajustes", query={"bloqueado": "1"})))
        self.assertIn("Grupo X", t)            # nome do convite guardado sem consultar o grupo
        self.assertIn("Bloqueada.", t)

    def test_desbloquear_e_entradas_ruins(self):
        self.psql.bloqueados = [G1]
        r = self.bl({"acao": "desbloquear", "c": G1, "volta": "/conversas"})
        self.assertEqual(dict(r.cabecalhos)["Location"], f"/conversas?c={G1}")
        self.assertEqual(self.psql.bloqueados, [])
        self.assertEqual(self.bl({"acao": "bloquear", "c": "x'; drop@g.us"}).status, 400)
        self.assertEqual(self.bl({"acao": "apagar", "c": G1}).status, 400)
        r = self.bl({"acao": "bloquear", "alvo": "https://chat.whatsapp.com/CodigoQueNaoExiste1"})
        self.assertEqual(r.status, 400)
        self.assertIn("não abriu nenhum grupo", texto(r))
        self.assertEqual(self.bl({"acao": "bloquear", "alvo": "123"}).status, 400)
        r = self.bl({"acao": "bloquear", "c": G1, "volta": "https://fora"})
        self.assertTrue(dict(r.cabecalhos)["Location"].startswith("/ajustes"))
        self.ctx.bloqueio.falhar = True
        r = self.bl({"acao": "bloquear", "c": ANA})
        self.assertEqual(r.status, 400)
        self.assertIn("Nada mudou", texto(r))

    def test_origem_de_fora_recusa(self):
        r = self.bl({"acao": "bloquear", "c": G1}, cab={"origin": "http://evil.example", "host": "127.0.0.1:8120"})
        self.assertEqual(r.status, 403)
        self.assertEqual(self.ctx.bloqueio.chamadas, [])

    def test_tema_grava_sozinho_com_204(self):
        from app import preferencias as pf
        antigo = pf.ARQUIVO
        pf.ARQUIVO = self.dados / "preferencias.json"
        self.addCleanup(setattr, pf, "ARQUIVO", antigo)
        r = self.ajustes.ROTAS["/ajustes"](self.req("POST", "/ajustes", {"fonte": "14", "tema": "escuro",
                                                    "acento": "#2b2f36"}, cab={"x-vigia-auto": "1"}))
        self.assertEqual(r.status, 200)  # desde ds03 D-18 devolve JSON com o CSS da prévia (antes 204 vazio)
        self.assertIn("css", json.loads(r.corpo))
        self.assertEqual(pf.ler()["tema"], "escuro")
        t = texto(self.ajustes.ROTAS["/ajustes"](self.req("GET", "/ajustes")))
        self.assertIn('value="escuro" checked', t)
        self.assertIn("X-Vigia-Auto", t)


class TestBolinhaSomeNoGreg(BaseZap):
    """Pedido 60: abrir a conversa no Greg apaga a bolinha dela (marca local); o WhatsApp não é avisado."""

    JID = "5592900001111@s.whatsapp.net"

    def _com_lista(self, nl_ts, nao_lidas=2):
        from unittest import mock
        linha = {"jid": self.JID, "ts": nl_ts, "id": "m1", "key": {"remoteJid": self.JID, "id": "K"}, "push_ultima": "Ana",
                 "tipo_msg": "conversation", "message": {"conversation": "oi"}, "nao_lidas": nao_lidas, "push": "Ana",
                 "contato": "Ana", "foto_url": "", "apagada": False, "nao_lida_ts": nl_ts}
        return mock.patch.object(wa, "listar", lambda psql, desde: [dict(linha)])

    def test_sem_abrir_a_bolinha_fica(self):
        with self._com_lista(1000):
            todas = conversas.carregar(self.ctx)[0]
        self.assertEqual(todas[0]["nao_lidas"], 2)

    def test_abrir_apaga_e_continua_apagada_depois(self):
        with self._com_lista(1000):
            self.assertEqual(conversas.carregar(self.ctx, vista=self.JID)[0][0]["nao_lidas"], 0)
            self.assertEqual(conversas.carregar(self.ctx)[0][0]["nao_lidas"], 0)

    def test_mensagem_que_chega_depois_acende_de_novo(self):
        with self._com_lista(1000):
            conversas.carregar(self.ctx, vista=self.JID)
        with self._com_lista(1500, nao_lidas=3):
            self.assertEqual(conversas.carregar(self.ctx)[0][0]["nao_lidas"], 3)

    def test_abrir_outra_conversa_nao_apaga_esta(self):
        with self._com_lista(1000):
            self.assertEqual(conversas.carregar(self.ctx, vista="5592900009999@s.whatsapp.net")[0][0]["nao_lidas"], 2)

    def test_atualizacao_em_segundo_plano_nao_conta_como_vista(self):
        with self._com_lista(1000):
            conversas.conversas_get(self.req("GET", "/conversas", query={"c": self.JID}, cab={"x-greg-fundo": "1"}))
            self.assertEqual(conversas.carregar(self.ctx)[0][0]["nao_lidas"], 2)
            conversas.conversas_get(self.req("GET", "/conversas", query={"c": self.JID}))
            self.assertEqual(conversas.carregar(self.ctx)[0][0]["nao_lidas"], 0)

    def test_marca_nunca_volta_no_tempo_e_recusa_jid_torto(self):
        import tempfile
        from pathlib import Path
        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d, True)
        v = wa.Vistas(Path(d) / "vistas.json")
        self.assertTrue(v.marcar(self.JID, 500))
        self.assertFalse(v.marcar(self.JID, 400))
        self.assertFalse(v.marcar("x' OR 1=1", 900))
        self.assertEqual(wa.Vistas(Path(d) / "vistas.json").todas(), {self.JID: 500})

    def test_a_consulta_traz_a_hora_da_nao_lida_e_a_pagina_avisa_o_fundo(self):
        self.assertIn("AS nl_ts", wa.sql_lista(0))
        self.assertIn("X-Greg-Fundo", conversas._JS)
