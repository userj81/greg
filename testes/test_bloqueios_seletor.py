"""Pedido 29 (D-46): página /bloqueios — escolher na agenda (tabela Contact) quem fica fora, com abas, busca e marcação
múltipla. Cobre estados antes do feliz: banco fora, aba vazia, nada marcado, já fora, falha no meio do lote, CSRF, 405."""
import unittest

from app import whatsapp as wa
from app.paginas import bloqueios
from testes.test_conversas_zap import ANA, G1, LID, MARCOS, SEMNOME, BaseZap, BloqueioFalso
from testes.test_paginas import XSS, texto

G2 = "120363000000000002@g.us"


def local(r) -> str:
    return next((v for k, v in r.cabecalhos if k.lower() == "location"), "")


class BaseSeletor(BaseZap):
    def setUp(self):
        super().setUp()
        self.ctx.bloqueio = BloqueioFalso(self.psql)
        # pedido 65: em grupo a agenda (Contact.pushName) traz o nome de um participante; o nome certo é o assunto
        # guardado no cache de grupos
        from app.paginas import conversas
        self.psql.agenda["grupos"] = [[G1, "Fulano Participante", "t"], [G2, f"Família {XSS}", "f"]]
        conversas.servicos(self.ctx).grupos.lembrar(G1, "CBA · Marketing")
        self.psql.agenda["contatos"] = [[ANA, "Ana Paula", "t"], [SEMNOME, "", "f"], [MARCOS, "Marcos", "f"]]
        self.psql.agenda["outros"] = [[LID, "Carla", "f"]]

    def pagina(self, **query):
        return bloqueios.ROTAS["/bloqueios"](self.req("GET", "/bloqueios", query=query))

    def bl(self, corpo, cab=None):
        return bloqueios.ROTAS["/bloqueios"](self.req("POST", "/bloqueios", corpo, cab=cab))


class TestPagina(BaseSeletor):
    def test_aba_padrao_e_grupos_com_contagem_nome_e_estado(self):
        self.psql.bloqueados = [G1]
        r = self.pagina()
        t = texto(r)
        self.assertEqual(r.status, 200)
        self.assertIn('href="/bloqueios?tipo=grupos" aria-current="page">Grupos <span class="suave">2</span>', t)
        self.assertIn('href="/bloqueios?tipo=contatos">Contatos <span class="suave">3</span>', t)
        self.assertIn('href="/bloqueios?tipo=outros">Sem número salvo <span class="suave">1</span>', t)
        self.assertIn("Fora agora (1)", t)
        # já fora: caixa desabilitada e selo; dentro: caixa livre
        self.assertIn(f'name="j:{G1}" value="1" disabled>', t)
        self.assertIn('<span class="pilula atrasado">fora</span>', t)
        self.assertIn(f'name="j:{G2}" value="1">', t)
        self.assertIn('<span class="meta-linha">dentro</span>', t)
        self.assertIn(">CBA · Marketing</label>", t)
        self.assertNotIn("Fulano Participante", t)                 # pedido 65: nome de participante nunca vira nome do grupo
        self.assertIn('>Grupo</label><div class="meta-linha">nome ainda não carregado</div>', t)   # G2: sem assunto no cache
        self.assertNotIn(XSS, t)                                   # nome da agenda é dado, escapado
        self.assertIn('value="bloquear_varios"', t)
        self.assertIn('id="fora-botao" disabled', t)               # nada marcado → botão desligado
        self.assertNotIn("Nenhuma conversa bloqueada", t)
        self.assertIn(">Desbloquear</button>", t)                  # lista dos que estão fora, com volta à página
        self.assertIn('name="volta" value="/bloqueios"', t)

    def test_contatos_mostram_numero_formatado_e_sem_nome_cai_no_numero(self):
        t = texto(self.pagina(tipo="contatos"))
        self.assertIn('aria-current="page">Contatos', t)
        self.assertIn(">Ana Paula</label>", t)
        self.assertIn(wa.numero_formatado(ANA), t)
        self.assertIn(f">{wa.numero_formatado(SEMNOME)}</label>", t)   # sem pushName → número como nome
        self.assertIn(f'data-busca="ana paula {wa.numero_formatado(ANA).lower()}"', t)

    def test_apelido_vence_a_agenda(self):
        self.ctx.banco.definir_apelido(ANA, "Ana do Lab")
        t = texto(self.pagina(tipo="contatos"))
        self.assertIn(">Ana do Lab</label>", t)
        self.assertNotIn(">Ana Paula</label>", t)

    def test_outros_sao_lid_com_tilde(self):
        t = texto(self.pagina(tipo="outros"))
        self.assertIn(">~Carla</label>", t)
        self.assertNotIn('<div class="suave"></div>', t)          # grupo e @lid não repetem o que a aba já diz

    def test_bloqueado_de_outra_aba_mostra_o_nome_da_agenda(self):
        self.psql.bloqueados = [G1, ANA]
        t = texto(self.pagina(tipo="outros"))
        self.assertIn('<span class="bl-nome">CBA · Marketing</span>', t)
        self.assertNotIn("Fulano Participante", t)
        self.assertIn('<span class="bl-nome">Ana Paula</span>', t)
        self.assertIn("Fora agora (2)", t)
        consultas = [q for q in self.psql.consultas if '"remoteJid" IN (' in q]
        self.assertEqual(len(consultas), 1)                       # uma consulta para todos os que faltam
        self.assertIn(f"'{G1}'", consultas[0])
        self.assertIn(f"'{ANA}'", consultas[0])
        # na aba em que o bloqueado está, o nome já veio com a aba: nenhuma consulta extra
        self.psql.consultas.clear()
        self.psql.bloqueados = [G1]
        texto(self.pagina(tipo="grupos"))
        self.assertFalse([q for q in self.psql.consultas if '"remoteJid" IN (' in q])

    def test_cartao_de_ajustes_usa_o_nome_da_agenda(self):
        from app.paginas import ajustes
        self.psql.bloqueados = [ANA]
        t = texto(ajustes.ajustes_get(self.req("GET", "/ajustes")))
        self.assertIn('<span class="bl-nome">Ana Paula</span>', t)
        self.psql.agenda["contatos"] = []                          # sem agenda: cai no número, como antes
        t = texto(ajustes.ajustes_get(self.req("GET", "/ajustes")))
        self.assertIn(f'<span class="bl-nome">{wa.numero_formatado(ANA)}</span>', t)

    def test_tipo_invalido_cai_na_aba_padrao(self):
        t = texto(self.pagina(tipo="../etc"))
        self.assertIn('href="/bloqueios?tipo=grupos" aria-current="page"', t)
        self.assertNotIn("../etc", t)

    def test_aba_vazia_tem_estado_e_nao_tem_formulario(self):
        self.psql.agenda["outros"] = []
        t = texto(self.pagina(tipo="outros"))
        self.assertIn('<b>Nada em &quot;Sem número salvo&quot;</b><span>na agenda sincronizada</span>', t)
        self.assertNotIn('id="form-fora"', t)
        self.assertIn('Sem número salvo <span class="suave">0</span>', t)

    def test_banco_fora_responde_200_com_estado_e_sem_formulario(self):
        self.psql.falhar = True
        r = self.pagina()
        t = texto(r)
        self.assertEqual(r.status, 200)
        self.assertIn("O banco da Evolution não respondeu agora", t)
        self.assertNotIn('id="form-fora"', t)
        self.assertIn("Fora agora (–)", t)
        self.assertIn('Grupos <span class="suave">–</span>', t)

    def test_head_tambem_e_pagina(self):
        r = bloqueios.ROTAS["/bloqueios"](self.req("HEAD", "/bloqueios"))
        self.assertEqual(r.status, 200)

    def test_avisos_apos_redirecionar(self):
        self.assertIn("2 conversa(s) agora fora do Greg", texto(self.pagina(fora="2")))
        self.assertIn("Nenhuma conversa saiu.", texto(self.pagina(fora="0")))
        t = texto(self.pagina(fora="1", falhou="1"))
        self.assertIn('class="erro-inline" role="alert"><span>1 conversa(s) agora fora', t)   # Papel: falha fica inline
        self.assertIn('data-toast><span>2 conversa(s) agora fora', texto(self.pagina(fora="2")))   # sucesso vira toast
        self.assertIn("O banco parou de confirmar depois disso", t)
        self.assertIn("Desbloqueada. O que chegar daqui para a frente", texto(self.pagina(desbloqueado="1")))
        self.assertNotIn('data-toast><span>', texto(self.pagina(fora="abc")))     # lixo na query não vira aviso

    def test_cartao_de_ajustes_aponta_para_o_seletor(self):
        from app.paginas import ajustes
        t = texto(ajustes.ajustes_get(self.req("GET", "/ajustes")))
        self.assertIn('<a class="btn" href="/bloqueios">Escolher na lista…</a>', t)


class TestLote(BaseSeletor):
    def test_marcados_saem_e_volta_para_a_aba(self):
        r = self.bl({"acao": "bloquear_varios", "tipo": "grupos", f"j:{G1}": "1", f"j:{G2}": "1"})
        self.assertEqual(r.status, 303)
        self.assertEqual(local(r), "/bloqueios?tipo=grupos&fora=2")
        self.assertEqual(self.ctx.bloqueio.chamadas, [("bloquear", G1), ("bloquear", G2)])
        self.assertEqual(sorted(self.psql.bloqueados), sorted([G1, G2]))

    def test_so_chaves_marcadas_contam(self):
        r = self.bl({"acao": "bloquear_varios", "tipo": "contatos", f"j:{ANA}": "1", f"j:{MARCOS}": "0", "x": "1"})
        self.assertEqual(local(r), "/bloqueios?tipo=contatos&fora=1")
        self.assertEqual(self.ctx.bloqueio.chamadas, [("bloquear", ANA)])

    def test_nada_marcado_e_400_na_propria_pagina(self):
        r = self.bl({"acao": "bloquear_varios", "tipo": "contatos"})
        self.assertEqual(r.status, 400)
        t = texto(r)
        self.assertIn("Marque ao menos uma conversa antes de confirmar.", t)
        self.assertIn('aria-current="page">Contatos', t)          # fica na aba em que estava
        self.assertEqual(self.ctx.bloqueio.chamadas, [])

    def test_jid_fora_do_padrao_e_400_sem_tocar_o_banco(self):
        r = self.bl({"acao": "bloquear_varios", f"j:{G1}": "1", "j:'; DROP TABLE x; --": "1"})
        self.assertEqual(r.status, 400)
        self.assertIn("conversa inválida", texto(r))
        self.assertEqual(self.ctx.bloqueio.chamadas, [])

    def test_teto_por_vez(self):
        corpo = {"acao": "bloquear_varios"}
        corpo.update({f"j:55929{i:08d}@s.whatsapp.net": "1" for i in range(bloqueios.MAX_POR_VEZ + 1)})
        r = self.bl(corpo)
        self.assertEqual(r.status, 400)
        self.assertIn(f"No máximo {bloqueios.MAX_POR_VEZ} conversas por vez", texto(r))
        self.assertEqual(self.ctx.bloqueio.chamadas, [])

    def test_falha_no_meio_para_e_conta_so_o_que_saiu(self):
        pacote, n = self.psql, [0]

        def rodar(acao, jid):
            n[0] += 1
            if n[0] == 2:
                raise wa.lotes.PostgresIndisponivel("fora")
            pacote.bloqueados.append(jid)
        self.ctx.bloqueio = rodar
        r = self.bl({"acao": "bloquear_varios", "tipo": "grupos", f"j:{G1}": "1", f"j:{G2}": "1", f"j:{ANA}": "1"})
        self.assertEqual(r.status, 303)
        self.assertEqual(local(r), "/bloqueios?tipo=grupos&fora=1&falhou=1")
        self.assertEqual(pacote.bloqueados, [G1])
        self.assertEqual(n[0], 2)                                  # parou na falha; o terceiro não foi tentado

    def test_banco_fora_logo_no_primeiro(self):
        self.ctx.bloqueio.falhar = True
        r = self.bl({"acao": "bloquear_varios", f"j:{G1}": "1"})
        self.assertEqual(local(r), "/bloqueios?tipo=grupos&fora=0&falhou=1")

    def test_desbloquear_pela_pagina_volta_para_ela(self):
        self.psql.bloqueados = [G1]
        r = self.bl({"acao": "desbloquear", "c": G1, "volta": "/bloqueios"})
        self.assertEqual(local(r), "/bloqueios?desbloqueado=1")
        self.assertEqual(self.psql.bloqueados, [])
        r = self.bl({"acao": "bloquear", "c": G1, "volta": "/bloqueios"})
        self.assertEqual(local(r), "/bloqueios?fora=1")

    def test_csrf_e_metodo_errado(self):
        r = self.bl({"acao": "bloquear_varios", f"j:{G1}": "1"}, cab={"origin": "http://evil.example", "host": "127.0.0.1:8120"})
        self.assertEqual(r.status, 403)
        self.assertEqual(self.ctx.bloqueio.chamadas, [])
        r = bloqueios.ROTAS["/bloqueios"](self.req("PUT", "/bloqueios", {"acao": "bloquear_varios"}))
        self.assertEqual(r.status, 405)

    def test_sem_servico_de_bloqueio_e_400(self):
        del self.ctx.bloqueio
        r = self.bl({"acao": "bloquear_varios", f"j:{G1}": "1"})
        self.assertEqual(r.status, 400)


class TestSql(unittest.TestCase):
    def test_agenda_so_select_por_tipo_e_sem_tab(self):
        for tipo, sufixo in wa.TIPOS_AGENDA.items():
            q = wa.sql_agenda(tipo)
            self.assertTrue(q.startswith("SELECT "), q)
            self.assertIn(f"LIKE '%{sufixo}'", q)
            self.assertIn('FROM public."Contact" c WHERE', q)
            self.assertIn("translate(coalesce(c.\"pushName\", ''), E'\\t\\n\\r', '   ')", q)
            self.assertIn("DISTINCT ON", q)
            self.assertIn("LIMIT 5000", q)
            for proibido in ("INSERT", "UPDATE", "DELETE", "DROP", ";"):
                self.assertNotIn(proibido, q)
        self.assertIn("LIMIT 7", wa.sql_agenda("grupos", limite=7))
        with self.assertRaises(ValueError):
            wa.sql_agenda("tudo")
        with self.assertRaises(ValueError):
            wa.sql_agenda("grupos' OR 1=1")

    def test_contagem_tem_as_tres_abas_na_ordem(self):
        q = wa.sql_contagem_agenda()
        self.assertTrue(q.startswith("SELECT count(DISTINCT"))
        self.assertLess(q.index("@g.us"), q.index("@s.whatsapp.net"))
        self.assertLess(q.index("@s.whatsapp.net"), q.index("@lid"))
        self.assertEqual(wa.contagem_agenda(lambda q: [["102", "988", "2063"]]), {"grupos": 102, "contatos": 988, "outros": 2063})
        self.assertEqual(wa.contagem_agenda(lambda q: []), {"grupos": 0, "contatos": 0, "outros": 0})

    def test_nomes_por_jid_so_com_jid_no_padrao(self):
        q = wa.sql_nomes_agenda([ANA, "x'; DROP TABLE y; --", G1, ANA])
        self.assertTrue(q.startswith("SELECT DISTINCT ON"))
        self.assertIn(f"IN ('{G1}', '{ANA}')", q)                  # ordenado, sem repetido, sem o inválido
        self.assertNotIn("DROP", q)
        with self.assertRaises(ValueError):
            wa.sql_nomes_agenda(["lixo"])
        chamadas = []

        def psql(consulta):
            chamadas.append(consulta)
            return [[ANA, "Ana Paula"], [G1, ""]]
        self.assertEqual(wa.nomes_agenda(psql, [ANA, G1]), {ANA: "Ana Paula"})   # nome vazio não entra
        self.assertEqual(wa.nomes_agenda(psql, ["lixo", ""]), {})
        self.assertEqual(len(chamadas), 1)                          # sem jid válido não consulta

    def test_agenda_filtra_jid_fora_do_padrao_e_le_foto(self):
        linhas = [[G1, "CBA", "t"], ["lixo sem arroba", "x", "f"], [ANA, "", "false"], ["a'b@g.us", "y", "t"]]
        saida = wa.agenda(lambda q: linhas, "grupos")
        self.assertEqual([r["jid"] for r in saida], [G1, ANA])
        self.assertEqual(saida[0], {"jid": G1, "nome": "CBA", "foto": True})
        self.assertEqual(saida[1]["foto"], False)
        with self.assertRaises(wa.lotes.PostgresIndisponivel):
            wa.agenda(_falha, "grupos")


def _falha(q):
    raise wa.lotes.PostgresIndisponivel("fora")


if __name__ == "__main__":
    unittest.main()
