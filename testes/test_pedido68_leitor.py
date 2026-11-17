"""Pedido 68: outra IA pelo protocolo de conversa da OpenAI. Motor, configuração, chave, laço de função, erros e tetos.
Tudo contra um servidor falso em 127.0.0.1 (testes/fakes_ia.py): nada sai do Mac e nenhum provedor é chamado.
"""
import base64
import importlib.machinery
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from app import leitor, leitor_api, lotes, saude, semana  # noqa: E402
from testes.fakes import imagem_jpeg_minima  # noqa: E402
from testes.fakes_ia import ServidorIA, erro, resposta  # noqa: E402

CHAVE = "sk-teste-0123456789_abc"
FINAL = '{"lote":"2026-11-16_0900","conversas":1,"novas":2,"itens":1,"atencao":0}'


def _parser_da_ponte():
    """O parser de verdade de bin/vigia-ponte (arquivo sem .py), para provar que o argv montado é o que ele lê."""
    carregador = importlib.machinery.SourceFileLoader("vigia_ponte_p68", str(RAIZ / "bin" / "vigia-ponte"))
    spec = importlib.util.spec_from_loader("vigia_ponte_p68", carregador)
    mod = importlib.util.module_from_spec(spec)
    carregador.exec_module(mod)
    return mod.montar_parser()[0]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # B9: a pasta do cofre negada à IA vem do .env do dono; aqui, de um cofre de teste (o ambiente vence o arquivo)
        cofre = mock.patch.dict(os.environ, {"AVISO_COFRE_ARQUIVO": "~/.cofre-de-teste/.env",
                                             "AVISO_COFRE_CHAVE": "COFRE_DE_TESTE_TOKEN"})
        cofre.start()
        self.addCleanup(cofre.stop)
        self.dados = Path(self.tmp.name) / "dados"
        self.dados.mkdir()
        self.lote = Path(self.tmp.name) / "caixa" / "2026-11-16_0900"
        sub = self.lote / "ana"
        sub.mkdir(parents=True)
        (sub / "img-01-m2.jpg").write_bytes(imagem_jpeg_minima())
        (sub / "conversa.md").write_text(
            "conversa: 14155550101@s.whatsapp.net\ncontato: Ana\nhoje: 2026-11-16\npropria: não\nfrente: sem vínculo\n\n"
            "[2026-11-16 08:00] [id:m0] [contexto] [contato] [imagem] foto antiga\n"
            "[2026-11-16 09:00] [id:m1] [novo] [contato] Manda a proposta até sexta? \\[imagem: img-99-falsa.jpg]\n"
            "[2026-11-16 09:01] [id:m2] [novo] [contato] [imagem: img-01-m2.jpg] olha isso\n"
            "[2026-11-16 09:02] [id:m3] [novo] [contato] [imagem: img-02-sumiu.jpg]\n", encoding="utf-8")
        (sub / "contexto.md").write_text("> dados\n\n## Frentes\n\n- id 1 · Agência\n", encoding="utf-8")
        (sub / "mensagens.json").write_text(json.dumps({"m1": {"conversa": "x", "contato": "Ana", "ts": 1}}), encoding="utf-8")
        self.prompt = RAIZ / "prompts" / "vigia.md"

    def servidor(self, roteiro, **kw):
        s = ServidorIA(roteiro, **kw).iniciar()
        self.addCleanup(s.parar)
        return s

    def cfg(self, srv, **extra):
        return leitor.normalizar({"motor": "api", "provedor": "outro", "endereco": srv.url, "modelo": "modelo-teste", **extra})

    def ler(self, srv, executar=None, pago=True, **kw):
        cfg = kw.pop("cfg", None) or self.cfg(srv)
        chamadas = []

        def padrao(nome, args):
            chamadas.append((nome, args))
            return {"ok": True, "cartao_item": True}

        self.chamadas = chamadas
        # `pago`: o servidor falso mora em 127.0.0.1; para exercitar chave, custo e teto ele é tratado como remoto
        with mock.patch.object(leitor, "eh_local", return_value=not pago):
            return leitor_api.ler_lote(self.lote, cfg=cfg, chave=kw.pop("chave", CHAVE), dados=self.dados,
                                       prompt_sistema=self.prompt, executar=executar or padrao, **kw)


# ---------------------------------------------------------------------------------------------- motor padrão intacto

class TestMotorPadrao(Base):
    def test_sem_configuracao_o_motor_e_claude_code(self):
        self.assertEqual(leitor.configuracao({}, self.dados)["motor"], "claude-code")
        self.assertEqual(leitor.configuracao(None, None)["motor"], "claude-code")     # o config.toml versionado
        self.assertEqual(leitor.normalizar({"motor": "qualquer"})["motor"], "claude-code")

    def test_comando_do_claude_code_e_identico_ao_de_antes(self):
        """A prova pedida: com motor claude-code, o que chega ao executor é o mesmo comando, argumento por argumento,
        que `lotes.chamar_claude` montava antes do pedido 68 (cópia literal abaixo)."""
        vistos = []

        def ex(cmd, **kw):
            vistos.append((cmd, kw))
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"type": "result", "result": FINAL}), "")

        r = leitor.ler(self.lote, modelo="sonnet", prompt_sistema=self.prompt, executor=ex, timeout=900.0,
                       config={}, dados=self.dados)
        self.assertEqual(r["itens"], 1)
        cmd, kw = vistos[0]
        pasta = self.lote.resolve()
        esperado = [cmd[0], "-p", "--model", "sonnet", "--tools", "Read,Bash",
                    "--allowedTools", f"Read(/{pasta}/**)", "Bash(vigia-ponte:*)",
                    "--disallowedTools", "Write,Edit,WebFetch,WebSearch,Agent", "Read(**/.env*)", "Read(~/.ssh/**)",
                    "Read(~/.cofre-de-teste/**)", "Read(~/.frentes.env)", "Read(~/.vigia-envio.env)",
                    "--permission-mode", "default",
                    "--no-session-persistence", "--strict-mcp-config", "--restricted", "--permission-prompts", "none",
                    "--add-dir", str(pasta), "--system-prompt-file", str(self.prompt.resolve()),
                    "--output-format", "json",
                    f"Classifique o lote em {pasta} (nome do lote: {pasta.name}).\n"
                    "O lote tem 1 conversa(s); para cada uma, leia contexto.md e conversa.md da mesma pasta:\n"
                    f"- {pasta}/ana/contexto.md e {pasta}/ana/conversa.md\n"
                    "Siga as instruções do sistema e termine com a linha JSON final."]
        self.assertEqual(cmd, esperado)
        self.assertEqual(cmd, lotes.montar_comando(self.lote, modelo="sonnet", prompt_sistema=self.prompt, claude=cmd[0]))
        self.assertEqual(kw["env"], lotes.ambiente_do_claude(pasta, cmd[0]))
        self.assertNotIn("_modelo", r)
        self.assertEqual(leitor.modelo_usado(r, "sonnet"), "sonnet")

    def test_fechamento_do_dia_passa_campos_e_mensagem_sem_mudar(self):
        with mock.patch.object(lotes, "chamar_claude", return_value={"ok": 1}) as m:
            leitor.ler(self.lote, modelo="sonnet", prompt_sistema=self.prompt, executor="E", timeout=7.0,
                       campos=("a", "b"), mensagem="tarefa", config={"ia": {"motor": "claude-code"}}, dados=self.dados)
        m.assert_called_once_with(self.lote, modelo="sonnet", prompt_sistema=self.prompt, executor="E", timeout=7.0,
                                  campos=("a", "b"), mensagem="tarefa")

    def test_lotes_py_nao_conhece_o_motor_novo(self):
        fonte = (RAIZ / "app" / "lotes.py").read_text(encoding="utf-8")
        self.assertNotRegex(fonte, r"leitor_api|import leitor|, leitor\b|pedido 68")      # o motor de sempre não conhece o novo


# ---------------------------------------------------------------------------------------------- configuração e chave

class TestConfiguracao(Base):
    def test_endereco_so_https_ou_este_mac(self):
        self.assertEqual(leitor.validar_endereco("https://openrouter.ai/api/v1/"), "https://openrouter.ai/api/v1")
        self.assertEqual(leitor.validar_endereco("http://localhost:11434/v1"), "http://localhost:11434/v1")
        for ruim in ("http://exemplo.com/v1", "ftp://x/v1", "https://usuario:senha@x.com/v1", "https://x.com/v1?k=1",
                     "https://x.com/v1#f", "sem-esquema", "", "https://x.com:porta/v1", "https://x.com/v1 /a"):
            with self.assertRaises(leitor.ConfiguracaoInvalida, msg=ruim):
                leitor.validar_endereco(ruim)
        self.assertTrue(leitor.eh_local("http://127.0.0.1:1234/v1"))
        self.assertFalse(leitor.eh_local("https://openrouter.ai/api/v1"))

    def test_ajustes_gravado_vence_o_config(self):
        self.assertEqual(leitor.configuracao({"ia": {"motor": "api", "endereco": "https://a.exemplo/v1", "modelo": "m"}},
                                             self.dados)["endereco"], "https://a.exemplo/v1")
        leitor.gravar_configuracao(self.dados, {"motor": "api", "provedor": "openrouter", "modelo": "x/y",
                                                "endereco": "https://openrouter.ai/api/v1", "teto_mes_usd": "1,5"})
        cfg = leitor.configuracao({"ia": {"motor": "claude-code"}}, self.dados)
        self.assertEqual((cfg["motor"], cfg["provedor"], cfg["modelo"], cfg["teto_mes_usd"]), ("api", "openrouter", "x/y", 1.5))
        self.assertEqual(leitor.rotulo(cfg), "OpenRouter")
        self.assertEqual(stat.S_IMODE((self.dados / "ia.json").stat().st_mode), 0o600)

    def test_api_sem_endereco_ou_modelo_nao_grava_e_nao_volta_calado_ao_claude(self):
        with self.assertRaises(leitor.ConfiguracaoInvalida):
            leitor.gravar_configuracao(self.dados, {"motor": "api", "endereco": "http://fora.exemplo/v1", "modelo": "m"})
        self.assertFalse((self.dados / "ia.json").exists())
        cfg = leitor.normalizar({"motor": "api", "endereco": "lixo", "modelo": "m"})
        self.assertEqual((cfg["motor"], cfg["endereco"]), ("api", ""))
        with self.assertRaises(leitor_api.ErroLeitorApi):
            leitor_api.conversar(cfg, CHAVE, [])

    def test_numero_fora_da_faixa_cai_no_padrao(self):
        cfg = leitor.normalizar({"teto_mes_usd": "-3", "voltas_max": 9999, "preco_saida_usd_milhao": "abc", "visao": "0"})
        self.assertEqual((cfg["teto_mes_usd"], cfg["voltas_max"], cfg["preco_saida_usd_milhao"], cfg["visao"]),
                         (5.0, 40, 15.0, False))

    def test_chave_fica_fora_do_config_em_arquivo_0600(self):
        self.assertEqual(leitor.chave(self.dados), "")
        self.assertEqual(leitor.gravar_chave(self.dados, CHAVE), "arquivo")
        arq = self.dados / ".env.ia"
        self.assertEqual(stat.S_IMODE(arq.stat().st_mode), 0o600)
        self.assertEqual(leitor.chave(self.dados), CHAVE)
        leitor.gravar_configuracao(self.dados, {"motor": "api", "endereco": "https://a.exemplo/v1", "modelo": "m"})
        self.assertNotIn(CHAVE, (self.dados / "ia.json").read_text())
        self.assertNotIn(CHAVE, (RAIZ / "config.toml").read_text(encoding="utf-8"))
        with self.assertRaises(leitor.ConfiguracaoInvalida):
            leitor.gravar_chave(self.dados, "com espaço dentro")
        leitor.apagar_chave(self.dados)
        self.assertFalse(arq.exists())

    def test_com_keychain_ligado_a_chave_vai_para_ele(self):
        guardado = {}
        with mock.patch.object(leitor.segredos, "ligado", return_value=True), \
                mock.patch.object(leitor.segredos, "gravar", side_effect=lambda n, v: guardado.update({n: v}) or True), \
                mock.patch.object(leitor.segredos, "ler", side_effect=lambda n: guardado.get(n, "")):
            self.assertEqual(leitor.gravar_chave(self.dados, "sk-or-v1-0123456789abcdef"), "keychain")
            self.assertEqual(leitor.chave(self.dados), "sk-or-v1-0123456789abcdef")
        self.assertEqual(list(guardado), ["VIGIA_IA_CHAVE"])
        self.assertFalse((self.dados / ".env.ia").exists())

    def test_git_ignora_o_arquivo_da_chave(self):
        r = subprocess.run(["git", "check-ignore", "-q", "dados/.env.ia"], cwd=RAIZ)
        if r.returncode not in (0, 1):
            self.skipTest("sem git")
        self.assertEqual(r.returncode, 0)


# ---------------------------------------------------------------------------------------------- laço de função

class TestLaco(Base):
    def test_laco_de_funcao_ate_o_json_final(self):
        srv = self.servidor([
            resposta(None, [("classificar", {"msg_id": "m1", "classe": "demanda", "resumo": "Ana pede proposta até sexta",
                                             "confianca": "alta", "frente": 1, "prazo": "2026-11-20"}),
                            ("historico", {"msg_id": "m3", "resumo": "Ana pediu proposta", "situacao": "aguarda_dono"})]),
            resposta(FINAL, entrada=300, saida=10)])
        r = self.ler(srv)
        self.assertEqual({k: r[k] for k in ("conversas", "novas", "itens", "atencao")},
                         {"conversas": 1, "novas": 2, "itens": 1, "atencao": 0})
        self.assertEqual((r["_tokens_in"], r["_tokens_out"], r["_turnos"], r["_modelo"], r["_motor"], r["_funcoes"]),
                         (400, 30, 2, "modelo-teste", "api", 2))
        self.assertGreater(r["_custo_usd"], 0)
        self.assertEqual(self.chamadas[0], ("classificar", {"msg_id": "m1", "classe": "demanda", "resumo": "Ana pede proposta até sexta",
                                                            "confianca": "alta", "frente": 1, "prazo": "2026-11-20"}))
        self.assertEqual(self.chamadas[1][0], "historico")
        p1, p2 = srv.pedidos
        self.assertEqual(p1["caminho"], "/v1/chat/completions")
        self.assertEqual(p1["cabecalhos"]["authorization"], f"Bearer {CHAVE}")
        self.assertEqual(p1["corpo"]["model"], "modelo-teste")
        self.assertEqual([t["function"]["name"] for t in p1["corpo"]["tools"]], ["classificar", "atualizar", "historico", "frentes"])
        papeis = [m["role"] for m in p2["corpo"]["messages"]]
        self.assertEqual(papeis, ["system", "user", "assistant", "tool", "tool"])
        self.assertEqual([m["tool_call_id"] for m in p2["corpo"]["messages"][3:]], ["call_0", "call_1"])
        self.assertEqual(json.loads(p2["corpo"]["messages"][3]["content"]), {"ok": True, "cartao_item": True})

    def test_prompt_e_o_mesmo_arquivo_com_a_nota_do_modo(self):
        srv = self.servidor([resposta(FINAL)])
        self.ler(srv)
        sistema = srv.pedidos[0]["corpo"]["messages"][0]["content"]
        self.assertTrue(sistema.startswith(self.prompt.read_text(encoding="utf-8").rstrip()))
        self.assertIn("Você não tem Read, Bash, terminal, arquivo nem rede", sistema)

    def test_lembrete_quando_falta_a_linha_final_e_erro_se_repetir(self):
        srv = self.servidor([resposta("Terminei de ler tudo."), resposta("```json\n" + FINAL + "\n```")])
        self.assertEqual(self.ler(srv)["novas"], 2)
        self.assertIn("Falta a mensagem final", srv.pedidos[1]["corpo"]["messages"][-1]["content"])
        srv2 = self.servidor([resposta("prosa"), resposta("mais prosa")])
        with self.assertRaisesRegex(leitor_api.ErroLeitorApi, "não terminou com a linha JSON"):
            self.ler(srv2)
        self.assertEqual(len(srv2.pedidos), 2)

    def test_teto_de_voltas(self):
        srv = self.servidor([resposta(None, [("frentes", {})])])
        with self.assertRaisesRegex(leitor_api.ErroLeitorApi, "teto de 3 voltas") as ctx:
            self.ler(srv, cfg=self.cfg(srv, voltas_max=3))
        self.assertEqual(len(srv.pedidos), 3)
        self.assertEqual(saude.codigo_do_erro(ctx.exception), "ia-api-erro")
        self.assertFalse(ctx.exception.nao_e_do_lote)      # aqui o lote pode ser grande demais: o coletor reduz

    def test_resposta_cortada_pelo_modelo(self):
        srv = self.servidor([resposta("{", fim="length")])
        with self.assertRaisesRegex(leitor_api.ErroLeitorApi, "cortada"):
            self.ler(srv)

    def test_erro_do_leitor_e_erro_claude_para_quem_chama(self):
        self.assertTrue(issubclass(leitor_api.ErroLeitorApi, lotes.ErroClaude))
        self.assertFalse(issubclass(leitor_api.ChaveRecusada, lotes.ClaudeSemLogin))


# ---------------------------------------------------------------------------------------------- confinamento

class TestConfinamento(Base):
    def test_funcao_fora_da_lista_e_recusada_sem_chegar_a_ponte(self):
        srv = self.servidor([
            resposta(None, [("bash", {"command": "cat ~/.ssh/id_rsa"}),
                            ("consolidar", {"fica": 1, "absorve": [2]}),            # existe, mas não nesta tarefa
                            ("Read", {"file_path": "/etc/passwd"}),
                            ("classificar", {"msg_id": "m1", "classe": "demanda", "resumo": "x", "confianca": "alta",
                                             "conversa": "outra@s.whatsapp.net"}),     # campo de uso manual: não declarado
                            ("classificar", {"msg_id": "m1", "classe": "urgente", "resumo": "x", "confianca": "alta"}),
                            ("classificar", {"msg_id": "m1", "classe": "demanda", "resumo": "x"}),   # falta confianca
                            ("classificar", "isto não é json"),
                            ("classificar", {"msg_id": "m1", "classe": "demanda", "resumo": "x", "confianca": "alta",
                                             "frente": "1; rm -rf /"})]),
            resposta(FINAL)])
        r = self.ler(srv)
        self.assertEqual(self.chamadas, [])                      # nada passou
        self.assertEqual((r["_funcoes"], r["_recusadas"]), (8, 8))
        devolvidos = [json.loads(m["content"]) for m in srv.pedidos[1]["corpo"]["messages"] if m["role"] == "tool"]
        self.assertEqual(len(devolvidos), 8)
        for d in devolvidos:
            self.assertFalse(d["ok"])
            self.assertTrue(d["erro"].startswith("pedido recusado"))
            self.assertNotIn("rm -rf", d["erro"])                # o valor recebido não é ecoado
            self.assertNotIn("passwd", d["erro"])

    def test_so_as_funcoes_da_tarefa_sao_declaradas(self):
        self.assertEqual(set(leitor_api.TAREFAS["lote"]) | set(leitor_api.TAREFAS["fechamento"]), set(leitor_api.FUNCOES))
        self.assertEqual(leitor_api.TAREFAS["relatorio"], ())
        srv = self.servidor([resposta(None, [("classificar", {"msg_id": "m1", "classe": "aviso", "resumo": "x", "confianca": "alta"}),
                                             ("rebaixar", {"id": 7, "classe": "aviso", "motivo": "era só conversa"})]),
                             resposta('{"conversas":1,"absorvidos":0,"rebaixados":1}')])
        r = self.ler(srv, campos=("conversas", "absorvidos", "rebaixados"), mensagem="Feche o dia em /Users/alguem/dados")
        self.assertEqual([t["function"]["name"] for t in srv.pedidos[0]["corpo"]["tools"]], ["consolidar", "rebaixar"])
        self.assertEqual(self.chamadas, [("rebaixar", {"id": 7, "classe": "aviso", "motivo": "era só conversa"})])
        self.assertEqual(r["_recusadas"], 1)
        self.assertNotIn("/Users/alguem", json.dumps(srv.pedidos[0]["corpo"]))     # o texto com caminho não vai

    def test_o_pedido_nao_leva_caminho_do_mac_nem_o_manifesto(self):
        srv = self.servidor([resposta(FINAL)])
        self.ler(srv)
        bruto = json.dumps(srv.pedidos[0]["corpo"], ensure_ascii=False)
        self.assertNotIn(str(self.lote.resolve()), bruto)
        self.assertNotIn(self.tmp.name, bruto)
        self.assertNotIn("mensagens.json", bruto.replace("`mensagens.json`", ""))   # só a menção do próprio prompt
        self.assertIn("<<<arquivo ana/conversa.md>>>", bruto)
        self.assertLess(bruto.index("ana/contexto.md>>>"), bruto.index("ana/conversa.md>>>"))

    def test_argv_da_ponte_nao_deixa_texto_virar_opcao(self):
        parser = _parser_da_ponte()
        args = leitor_api.conferir_pedido("classificar", {"msg_id": "-m1", "classe": "demanda", "resumo": "--cancela=9",
                                                          "confianca": "alta", "prazo": "--substitui=3", "frente": -2,
                                                          "imagem": "img-01-m2.jpg"}, leitor_api.TAREFAS["lote"])
        ns = parser.parse_args(leitor_api.argv_da_ponte("classificar", args))
        self.assertEqual((ns.comando, ns.msg_id, ns.classe, ns.resumo), ("classificar", "-m1", "demanda", "--cancela=9"))
        self.assertEqual((ns.cancela, ns.substitui, ns.prazo, ns.frente, ns.imagem, ns.confianca),
                         (None, None, "--substitui=3", -2, "img-01-m2.jpg", "alta"))
        self.assertIsNone(ns.conversa)

    def test_argv_de_cada_funcao_bate_com_o_parser_da_ponte(self):
        parser = _parser_da_ponte()
        casos = {
            "atualizar": ({"item_id": 41, "nota": "cobrou de novo", "msg": "m1", "prioridade": 1},
                          {"item_id": 41, "nota": "cobrou de novo", "msg": "m1", "prioridade": 1}),
            "historico": ({"msg_id": "m3", "resumo": "combinaram", "situacao": "encerrada"},
                          {"msg_id": "m3", "resumo": "combinaram", "situacao": "encerrada"}),
            "frentes": ({}, {}),
            "consolidar": ({"fica": 5, "absorve": [6, "7"], "resumo": "final", "prazo": "2026-11-18"},
                           {"fica": 5, "absorve": [6, 7], "resumo": "final", "prazo": "2026-11-18"}),
            "rebaixar": ({"id": 9, "classe": "sem_acao", "motivo": "ruído"}, {"id": 9, "classe": "sem_acao", "motivo": "ruído"}),
        }
        for nome, (entrada, esperado) in casos.items():
            args = leitor_api.conferir_pedido(nome, json.dumps(entrada), tuple(leitor_api.FUNCOES))
            ns = vars(parser.parse_args(leitor_api.argv_da_ponte(nome, args)))
            self.assertEqual(ns["comando"], nome)
            for k, v in esperado.items():
                self.assertEqual(ns[k], v, f"{nome}.{k}")

    def test_campos_declarados_sao_os_do_parser_menos_os_de_uso_manual(self):
        parser = _parser_da_ponte()
        sub = next(a for a in parser._actions if a.dest == "comando").choices
        for nome, (_, pos, opc, _) in leitor_api.FUNCOES.items():
            do_parser = {a.dest for a in sub[nome]._actions if a.dest != "help"}
            self.assertEqual(set(pos) | set(opc), do_parser - {"conversa", "contato", "ts"}, nome)

    def test_ponte_por_processo_sem_shell_e_com_ambiente_por_lista_branca(self):
        vistos = {}

        def ex(cmd, **kw):
            vistos.update(cmd=cmd, kw=kw)
            return subprocess.CompletedProcess(cmd, 0, 'aviso\n{"ok": true, "id": 3}\n', "")

        with mock.patch.dict(os.environ, {"VIGIA_IA_CHAVE": "segredo-no-ambiente", "ANTHROPIC_API_KEY": "outro-segredo"}):
            r = leitor_api.PontePorProcesso(self.lote, executor=ex)("historico", {"msg_id": "m3", "resumo": "x", "situacao": "encerrada"})
        self.assertEqual(r, {"ok": True, "id": 3})
        self.assertEqual(vistos["cmd"], [str(RAIZ / "bin" / "vigia-ponte"), "historico", "--situacao=encerrada", "--", "m3", "x"])
        self.assertNotIn("shell", vistos["kw"])
        env = vistos["kw"]["env"]
        self.assertEqual(env["VIGIA_LOTE"], str(self.lote.resolve()))
        self.assertNotIn("VIGIA_IA_CHAVE", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)

        def cai(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 1)

        self.assertFalse(leitor_api.PontePorProcesso(self.lote, executor=cai)("frentes", {})["ok"])

    def test_redirecionamento_nao_e_seguido(self):
        srv = self.servidor([(302, {}, {"Location": "/v1/outro"}), resposta(FINAL)])
        with self.assertRaisesRegex(leitor_api.ErroLeitorApi, "redirecionou"):
            self.ler(srv)
        self.assertEqual(len(srv.pedidos), 1)      # a chave não foi atrás do Location


# ---------------------------------------------------------------------------------------------- imagem

class TestImagem(Base):
    def test_imagem_das_novas_vai_em_base64_com_o_nome(self):
        srv = self.servidor([resposta(FINAL)])
        self.ler(srv)
        partes = srv.pedidos[0]["corpo"]["messages"][1]["content"]
        imagens = [p for p in partes if p["type"] == "image_url"]
        self.assertEqual(len(imagens), 1)
        url = imagens[0]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/jpeg;base64,"))
        self.assertEqual(base64.b64decode(url.split(",", 1)[1]), imagem_jpeg_minima())
        i = partes.index(imagens[0])
        self.assertEqual(partes[i - 1], {"type": "text", "text": "Imagem ana/img-01-m2.jpg:"})
        texto = "".join(p["text"] for p in partes if p["type"] == "text")
        self.assertIn("[imagem: img-01-m2.jpg] olha isso", texto)
        self.assertIn("[id:m3] [novo] [contato] [imagem: não lida]", texto)       # arquivo que não existe: nunca inventada
        self.assertIn("\\[imagem: img-99-falsa.jpg]", texto)                      # marcador imitado pelo contato fica como texto
        self.assertIn("[contexto] [contato] [imagem] foto antiga", texto)

    def test_modelo_sem_visao_recebe_nao_lida_e_nenhuma_imagem(self):
        srv = self.servidor([resposta(FINAL)])
        self.ler(srv, cfg=self.cfg(srv, visao=False))
        partes = srv.pedidos[0]["corpo"]["messages"][1]["content"]
        self.assertEqual([p for p in partes if p["type"] == "image_url"], [])
        texto = "".join(p["text"] for p in partes if p["type"] == "text")
        self.assertIn("[id:m2] [novo] [contato] [imagem: não lida] olha isso", texto)
        self.assertNotIn("img-01-m2.jpg", texto)
        self.assertIn("nunca descreva o que não viu", srv.pedidos[0]["corpo"]["messages"][0]["content"])

    def test_imagem_grande_demais_ou_atalho_nao_vai(self):
        sub = self.lote / "ana"
        (sub / "img-01-m2.jpg").unlink()
        os.symlink(RAIZ / "config.toml", sub / "img-01-m2.jpg")
        srv = self.servidor([resposta(FINAL)])
        self.ler(srv)
        self.assertNotIn("image_url", json.dumps(srv.pedidos[0]["corpo"]))
        (sub / "img-01-m2.jpg").unlink()
        (sub / "img-01-m2.jpg").write_bytes(imagem_jpeg_minima())
        with mock.patch.object(leitor_api, "MAX_IMAGEM", 10):
            self.ler(srv)
        self.assertNotIn("image_url", json.dumps(srv.pedidos[1]["corpo"]))


# ---------------------------------------------------------------------------------------------- erros do provedor

class TestErros(Base):
    def falha(self, roteiro, **kw):
        srv = self.servidor(roteiro, **{k: kw.pop(k) for k in ("atraso_s",) if k in kw})
        with self.assertRaises(leitor_api.ErroLeitorApi) as ctx:
            self.ler(srv, **kw)
        self.assertNotIn(CHAVE, str(ctx.exception))
        self.assertNotIn("recusado pelo provedor com texto livre", str(ctx.exception))
        return ctx.exception, srv

    def test_401_chave_recusada(self):
        e, srv = self.falha([erro(401, "invalid_api_key", "recusado pelo provedor com texto livre")])
        self.assertIsInstance(e, leitor_api.ChaveRecusada)
        self.assertEqual(saude.codigo_do_erro(e), "ia-chave")
        self.assertTrue(e.nao_e_do_lote)
        self.assertIn("respondeu 401 (invalid_api_key)", str(e))
        self.assertEqual(len(srv.pedidos), 1)

    def test_402_sem_saldo(self):
        e, _ = self.falha([erro(402, "", "recusado pelo provedor com texto livre")])
        self.assertIsInstance(e, leitor_api.SemSaldo)
        self.assertEqual(saude.codigo_do_erro(e), "ia-saldo")

    def test_429_limite_e_429_de_cota_e_sem_saldo(self):
        e, _ = self.falha([erro(429, "rate_limit_exceeded")])
        self.assertIsInstance(e, leitor_api.LimiteDeUso)
        self.assertEqual(saude.codigo_do_erro(e), "ia-api-limite")
        e, _ = self.falha([erro(429, "insufficient_quota")])
        self.assertIsInstance(e, leitor_api.SemSaldo)

    def test_500_e_endereco_fora_do_ar(self):
        e, _ = self.falha([erro(503)])
        self.assertEqual(saude.codigo_do_erro(e), "ia-api-fora")
        srv = ServidorIA([]).iniciar()
        url = srv.url
        srv.parar()
        cfg = leitor.normalizar({"motor": "api", "endereco": url, "modelo": "m"})
        with self.assertRaises(leitor_api.IaForaDoAr) as ctx:
            leitor_api.ler_lote(self.lote, cfg=cfg, chave="", dados=self.dados, prompt_sistema=self.prompt,
                                executar=lambda n, a: {"ok": True})
        self.assertEqual(saude.codigo_do_erro(ctx.exception), "ia-api-fora")

    def test_404_e_resposta_que_nao_e_json(self):
        e, _ = self.falha([erro(404, "model_not_found")])
        self.assertIn("não encontrado", str(e))
        self.assertEqual(saude.codigo_do_erro(e), "ia-api-erro")
        e, _ = self.falha([(200, b"<html>pagina de login</html>")])
        self.assertIn("não devolveu JSON", str(e))
        e, _ = self.falha([{"choices": []}])
        self.assertIn("sem mensagem", str(e))

    def test_tempo_esgotado_no_pedido(self):
        cfg = dict(leitor.normalizar({"motor": "api", "modelo": "m"}), tempo_pedido_s=1)
        srv = self.servidor([resposta(FINAL)], atraso_s=2.5)
        cfg["endereco"] = srv.url
        with self.assertRaisesRegex(leitor_api.ErroLeitorApi, "não respondeu em 1 s") as ctx:
            self.ler(srv, cfg=cfg)
        self.assertEqual(saude.codigo_do_erro(ctx.exception), "ia-api-erro")

    def test_tempo_total_esgotado(self):
        srv = self.servidor([resposta(None, [("frentes", {})])])
        relogio = iter([0.0, 0.0, 5.0, 99.0, 99.0, 99.0])
        with mock.patch.object(leitor, "eh_local", return_value=False), \
                self.assertRaisesRegex(leitor_api.ErroLeitorApi, "excedeu 10 s"):
            leitor_api.conversar(self.cfg(srv), CHAVE, [{"role": "user", "content": "x"}], funcoes=("frentes",),
                                 executar=lambda n, a: {"ok": True}, dados=self.dados, timeout=10.0,
                                 campos=("conversas",), relogio=lambda: next(relogio))
        self.assertEqual(len(srv.pedidos), 2)

    def test_sem_chave_em_endereco_pago_nem_chama(self):
        srv = self.servidor([resposta(FINAL)])
        with self.assertRaises(leitor_api.ChaveRecusada):
            self.ler(srv, chave="")
        self.assertEqual(srv.pedidos, [])

    def test_endereco_local_funciona_sem_chave(self):
        srv = self.servidor([resposta(FINAL)])
        r = self.ler(srv, chave="", pago=False)
        self.assertEqual(r["_custo_usd"], 0.0)
        self.assertNotIn("authorization", srv.pedidos[0]["cabecalhos"])


# ---------------------------------------------------------------------------------------------- gasto

class TestGasto(Base):
    def test_custo_pelos_tokens_e_preco_configurado(self):
        srv = self.servidor([resposta(FINAL, entrada=1_000_000, saida=100_000)])
        r = self.ler(srv, cfg=self.cfg(srv, preco_entrada_usd_milhao=0.5, preco_saida_usd_milhao=2, teto_mes_usd=50))
        self.assertAlmostEqual(r["_custo_usd"], 0.7)
        g = leitor_api.gasto_do_mes(self.dados)
        self.assertEqual((g["tokens_in"], g["tokens_out"], g["pedidos"]), (1_000_000, 100_000, 1))
        self.assertAlmostEqual(g["usd"], 0.7)

    def test_custo_informado_pelo_provedor_vence(self):
        srv = self.servidor([resposta(FINAL, entrada=1000, saida=1000, custo=0.0123)])
        self.assertAlmostEqual(self.ler(srv)["_custo_usd"], 0.0123)

    def test_teto_do_mes_para_no_meio_do_lote_e_nada_mais_e_gasto(self):
        srv = self.servidor([resposta(None, [("frentes", {})], entrada=100_000, saida=0), resposta(FINAL)])
        cfg = self.cfg(srv, teto_mes_usd=0.2, preco_entrada_usd_milhao=3)       # 1ª resposta custa US$ 0,30
        with self.assertRaises(leitor_api.TetoDeGasto) as ctx:
            self.ler(srv, cfg=cfg)
        self.assertEqual(len(srv.pedidos), 1)                 # o segundo pedido não saiu
        self.assertEqual(saude.codigo_do_erro(ctx.exception), "ia-teto")
        self.assertTrue(ctx.exception.nao_e_do_lote)
        with self.assertRaises(leitor_api.TetoDeGasto):       # e a leitura seguinte do mês nem começa
            self.ler(srv, cfg=cfg)
        self.assertEqual(len(srv.pedidos), 1)
        self.assertIn("teto de gasto do mês atingido (US$ 0.30 de US$ 0.20)", str(ctx.exception))
        with mock.patch.object(leitor, "eh_local", return_value=False):     # o Testar também não gasta
            self.assertEqual(leitor_api.testar(cfg, CHAVE, self.dados)[0], False)
        self.assertEqual(len(srv.pedidos), 1)
        self.assertAlmostEqual(leitor_api.gasto_do_mes(self.dados)["usd"], 0.3)
        # a pessoa aumenta o teto em Ajustes: a leitura volta no mesmo mês, sem zerar o que já foi gasto
        self.assertEqual(self.ler(srv, cfg=self.cfg(srv, teto_mes_usd=5, preco_entrada_usd_milhao=3))["novas"], 2)
        self.assertEqual(len(srv.pedidos), 2)
        self.assertGreater(leitor_api.gasto_do_mes(self.dados)["usd"], 0.3)

    def test_gasto_acumula_no_mes_e_zera_na_virada(self):
        srv = self.servidor([resposta(FINAL, entrada=100_000, saida=0)])          # US$ 0,30 por leitura
        cfg = self.cfg(srv, teto_mes_usd=0.5, preco_entrada_usd_milhao=3)
        with mock.patch.object(leitor_api, "mes_corrente", return_value="2026-11"):
            self.ler(srv, cfg=cfg)                                                # dia 13
            self.ler(srv, cfg=cfg)                                                # dia 30: soma no mesmo mês
            g = leitor_api.gasto_do_mes(self.dados)
            self.assertEqual((g["mes"], g["pedidos"]), ("2026-11", 2))
            self.assertAlmostEqual(g["usd"], 0.6)
            with self.assertRaises(leitor_api.TetoDeGasto):                       # teto de novembro atingido
                self.ler(srv, cfg=cfg)
            self.assertEqual(len(srv.pedidos), 2)
        with mock.patch.object(leitor_api, "mes_corrente", return_value="2026-12"):
            self.assertEqual(leitor_api.gasto_do_mes(self.dados)["usd"], 0.0)     # virou o mês: zerou
            self.assertEqual(self.ler(srv, cfg=cfg)["novas"], 2)                  # e a leitura volta sozinha
            g = leitor_api.gasto_do_mes(self.dados)
            self.assertEqual((g["mes"], g["pedidos"]), ("2026-12", 1))
            self.assertAlmostEqual(g["usd"], 0.3)
        self.assertEqual(len(srv.pedidos), 3)
        self.assertEqual(json.loads((self.dados / "ia-gasto.json").read_text())["mes"], "2026-12")

    def test_mes_corrente_e_o_do_relogio_e_arquivo_antigo_nao_conta(self):
        self.assertRegex(leitor_api.mes_corrente(), r"^20\d\d-(0[1-9]|1[0-2])$")
        self.assertEqual(leitor_api.mes_corrente(), leitor_api.date.today().isoformat()[:7])
        (self.dados / "ia-gasto.json").write_text(json.dumps({"dia": "2026-11-13", "usd": 999}))   # formato sem "mes"
        self.assertEqual(leitor_api.gasto_do_mes(self.dados)["usd"], 0.0)

    def test_teto_padrao_e_cinco_dolares_por_mes(self):
        self.assertEqual(leitor.PADRAO["teto_mes_usd"], 5.0)
        self.assertEqual(leitor.configuracao({}, self.dados)["teto_mes_usd"], 5.0)
        self.assertNotIn("teto_dia_usd", leitor.PADRAO)
        self.assertIn("# teto_mes_usd = 5.0", (RAIZ / "config.toml").read_text(encoding="utf-8"))

    def test_endereco_local_nao_tem_teto(self):
        (self.dados / "ia-gasto.json").write_text(json.dumps({"mes": leitor_api.mes_corrente(), "usd": 999}))
        srv = self.servidor([resposta(FINAL)])
        self.assertEqual(self.ler(srv, pago=False)["novas"], 2)


# ---------------------------------------------------------------------------------------------- despacho, relatório, teste

class TestDespacho(Base):
    def test_leitor_ler_com_motor_api_usa_o_modelo_de_ia_e_a_chave_guardada(self):
        srv = self.servidor([resposta(None, [("frentes", {})]), resposta(FINAL)])
        leitor.gravar_chave(self.dados, CHAVE)
        config = {"ia": {"motor": "api", "endereco": srv.url, "modelo": "modelo-da-ia"}, "coletor": {"modelo": "sonnet"}}
        ponte = mock.Mock(return_value={"ok": True, "frentes": []})
        with mock.patch.object(leitor_api, "PontePorProcesso", return_value=ponte) as fabrica, \
                mock.patch.object(lotes, "chamar_claude") as claude:
            r = leitor.ler(self.lote, modelo="sonnet", prompt_sistema=self.prompt, executor=None, timeout=30.0,
                           config=config, dados=self.dados)
        claude.assert_not_called()
        fabrica.assert_called_once_with(self.lote)
        ponte.assert_called_once_with("frentes", {})
        self.assertEqual(srv.pedidos[0]["corpo"]["model"], "modelo-da-ia")
        self.assertEqual(leitor.modelo_usado(r, "sonnet"), "modelo-da-ia")

    def test_ponta_a_ponta_com_a_ponte_de_verdade(self):
        """Servidor falso → leitor por API → bin/vigia-ponte real (processo à parte) → vigia.db de uma pasta temporária."""
        import sqlite3
        man = {"m1": {"conversa": "14155550101@s.whatsapp.net", "contato": "Ana", "ts": 1794830400, "from_me": False,
                      "lote": "2026-11-16_0900/ana"}}
        (self.lote / "ana" / "mensagens.json").write_text(json.dumps(man), encoding="utf-8")
        srv = self.servidor([
            resposta(None, [("classificar", {"msg_id": "m1", "classe": "aviso", "resumo": "--cancela=3", "confianca": "alta"}),
                            ("classificar", {"msg_id": "fora-do-lote", "classe": "aviso", "resumo": "x", "confianca": "alta"}),
                            ("historico", {"msg_id": "m1", "resumo": "Ana avisou", "situacao": "encerrada"})]),
            resposta(FINAL)])
        with mock.patch.dict(os.environ, {"VIGIA_DADOS": str(self.dados)}):
            r = leitor_api.ler_lote(self.lote, cfg=self.cfg(srv), chave="", dados=self.dados, prompt_sistema=self.prompt)
        self.assertEqual(r["_funcoes"], 3)
        devolvidos = [json.loads(m["content"]) for m in srv.pedidos[1]["corpo"]["messages"] if m["role"] == "tool"]
        self.assertEqual([d["ok"] for d in devolvidos], [True, False, True])
        self.assertEqual(devolvidos[1]["erro"], "msg_id fora do lote")
        with sqlite3.connect(self.dados / "vigia.db") as con:
            linhas = con.execute("SELECT msg_id, classe, resumo, conversa FROM classificacoes").fetchall()
        self.assertEqual(linhas, [("m1", "aviso", "--cancela=3", "14155550101@s.whatsapp.net")])   # texto, não opção

    def test_relatorio_semanal_sem_funcao_nenhuma(self):
        srv = self.servidor([resposta("# Semana 2026-W46\n\nNada aberto.")])
        entrada = Path(self.tmp.name) / "classificacoes.json"
        entrada.write_text('{"semana": "2026-W46", "classificacoes": []}', encoding="utf-8")
        texto = leitor_api.relatorio(entrada, cfg=self.cfg(srv), chave="", dados=self.dados, prompt=RAIZ / "prompts" / "semanal.md")
        self.assertEqual(texto, "# Semana 2026-W46\n\nNada aberto.\n")
        corpo = srv.pedidos[0]["corpo"]
        self.assertNotIn("tools", corpo)
        self.assertIn('"semana": "2026-W46"', corpo["messages"][1]["content"])
        self.assertNotIn(self.tmp.name, json.dumps(corpo))

    def test_semana_relatorio_escolhe_o_motor_pela_configuracao(self):
        fonte = (RAIZ / "app" / "semana.py").read_text(encoding="utf-8")
        self.assertIn('if cfg_ia["motor"] == "api":', fonte)
        self.assertLess(fonte.index('if cfg_ia["motor"] == "api":'), fonte.index("cmd = montar_comando_relatorio("))
        entrada = Path(self.tmp.name) / "classificacoes.json"
        cmd = semana.montar_comando_relatorio(entrada, modelo="opus", prompt=RAIZ / "prompts" / "semanal.md", claude="claude")
        self.assertEqual(cmd[:6], ["claude", "-p", "--model", "opus", "--tools", "Read"])     # o de sempre, intacto

    def test_testar_diz_em_uma_frase(self):
        srv = self.servidor([resposta(None, [("confirmar", {"ok": True})]), resposta("ok"), erro(401), erro(402), erro(429)])
        cfg = self.cfg(srv)
        with mock.patch.object(leitor, "eh_local", return_value=False):
            self.assertEqual(leitor_api.testar(cfg, CHAVE, self.dados), (True, "Funcionou: o modelo respondeu e sabe chamar função."))
            ok, frase = leitor_api.testar(cfg, CHAVE, self.dados)
            self.assertFalse(ok)
            self.assertIn("não chamou a função", frase)
            self.assertEqual(leitor_api.testar(cfg, CHAVE, self.dados), (False, "Não funcionou: o provedor recusou a chave."))
            self.assertIn("sem saldo", leitor_api.testar(cfg, CHAVE, self.dados)[1])
            self.assertIn("limitando o uso", leitor_api.testar(cfg, CHAVE, self.dados)[1])
            self.assertEqual(leitor_api.testar(cfg, "", self.dados), (False, "Falta a chave: cole a chave do provedor e salve."))
        self.assertEqual(len(srv.pedidos), 5)
        self.assertNotIn("conversa", json.dumps(srv.pedidos[0]["corpo"]["messages"]))      # o teste não leva dado de conversa
        self.assertEqual(leitor_api.testar(leitor.normalizar({"motor": "api"}), "", self.dados)[1], "Falta o endereço ou o modelo.")


if __name__ == "__main__":
    unittest.main()
