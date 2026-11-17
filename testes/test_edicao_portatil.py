"""Edição portátil (Docker / sem Frentes externo / sem cofre): Frentes embutido, fábrica da ponte, aviso direto pela
Evolution, agendador do contêiner, bind do painel e "Ler agora" no contêiner. Nada aqui toca rede de verdade, Keychain,
launchctl ou o dados/ real."""
import importlib.util
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from app import aviso
from app.erros import FrentesRecusou
from app.frentes_local import FrentesLocal
from app.frentes_ponte import FrentesPonte, criar_ponte, embutido

RAIZ = Path(__file__).resolve().parents[1]


def _carregar_agendador():
    spec = importlib.util.spec_from_file_location("agendador", RAIZ / "bin" / "agendador.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FrentesEmbutido(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.f = FrentesLocal(Path(self.tmp.name) / "frentes.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_frente_item_estado_e_contexto(self):
        r = self.f.criar_frente("Casa", tipo="pessoal", apelidos=["lar"])
        fid = r["id"]
        self.assertEqual(r["frente"]["apelidos"], ["lar"])
        ontem = (date.today() - timedelta(days=1)).isoformat()
        c = self.f.capturar("Pagar a conta de luz", frente_id=fid, tipo="prazo", prazo=ontem, prioridade=1)
        item_id = c["captura_id"]
        self.f.capturar("Ligar para o encanador", frente_id=None)
        est = self.f.estado()
        casa = next(x for x in est["frentes"] if x["id"] == fid)
        self.assertEqual((casa["n_abertos"], casa["n_atrasados"], casa["n_urgentes"]), (1, 1, 1))
        self.assertEqual(est["totais"]["inbox"], 1)
        self.assertEqual(len(self.f.itens_da_frente(None)), 1)
        self.f.alterar(item_id, status="feito")
        self.assertEqual(self.f.itens_da_frente(fid, "feito")[0]["id"], item_id)
        self.assertIn("Casa", self.f.contexto_markdown())

    def test_frente_inexistente_cai_no_inbox(self):
        c = self.f.capturar("algo", frente_id=999)
        self.assertTrue(c["frente_invalida"])
        self.assertIsNone(c["itens"][0]["frente_id"])

    def test_confirmar_e_nota_descartada(self):
        item_id = self.f.capturar("revisar", frente_id=None)["captura_id"]
        self.f.marcar_nao_confirmado(item_id)
        self.assertEqual(self.f.itens_da_frente(None)[0]["confirmado"], 0)
        self.f.confirmar(item_id)
        self.assertEqual(self.f.itens_da_frente(None)[0]["confirmado"], 1)
        self.assertEqual(self.f.anotar(item_id, "nota sem prioridade"), {})

    def test_arquivar_some_das_listas_e_nome_repetido_recusa(self):
        fid = self.f.criar_frente("Cliente X")["id"]
        with self.assertRaises(FrentesRecusou):
            self.f.criar_frente("Cliente X")
        self.f.alterar_frente(fid, status="arquivada")
        self.assertEqual(self.f.frentes(), [])
        with self.assertRaises(FrentesRecusou):
            self.f.alterar_frente(fid, status="sumida")

    def test_persiste_entre_instancias(self):
        self.f.criar_frente("Saúde")
        outra = FrentesLocal(Path(self.tmp.name) / "frentes.db")
        self.assertEqual([x["nome"] for x in outra.frentes()], ["Saúde"])


    def test_cancelar_item_como_o_recusar_e_a_substituicao(self):
        fid = self.f.criar_frente("Casa")["id"]
        iid = self.f.capturar("trocar o filtro", frente_id=fid)["itens"][0]["id"]
        self.assertFalse(self.f.item_cancelado(iid))
        self.f.alterar(iid, texto="trocar o filtro · atualizado: sexta")
        self.f.alterar(iid, status="cancelado")
        self.assertTrue(self.f.item_cancelado(iid))
        self.assertEqual(self.f.itens_da_frente(fid), [])          # some dos abertos
        self.f.alterar(iid, status="aberto")                       # Restaurar
        self.assertFalse(self.f.item_cancelado(iid))
        with self.assertRaises(FrentesRecusou):
            self.f.alterar(iid, status="apagado")


class FabricaDaPonte(unittest.TestCase):
    def test_vazio_ou_embutido_e_local(self):
        self.assertTrue(embutido(""))
        self.assertTrue(embutido(" Embutido "))
        self.assertFalse(embutido("http://127.0.0.1:8110"))
        with tempfile.TemporaryDirectory() as d:
            self.assertIsInstance(criar_ponte("", "", d), FrentesLocal)
            self.assertIsInstance(criar_ponte(None, None, d, padrao_url="http://127.0.0.1:8110"), FrentesLocal)

    def test_url_ou_token_com_padrao_e_http(self):
        with tempfile.TemporaryDirectory() as d:
            p = criar_ponte("http://127.0.0.1:9", "tok", d)
            self.assertIs(type(p), FrentesPonte)
            legado = criar_ponte("", "tok", d, padrao_url="http://127.0.0.1:8110")   # instalação antiga sem FRENTES_URL
            self.assertIs(type(legado), FrentesPonte)
            self.assertEqual(legado.url, "http://127.0.0.1:8110")
            self.assertFalse((Path(d) / "frentes.db").exists())


class _Resposta:
    status = 201

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class AvisoDireto(unittest.TestCase):
    def test_vai_direto_para_a_evolution_do_bot_com_a_chave_no_header(self):
        pedidos = []

        class Abridor:
            def open(self, req, timeout=None):
                pedidos.append(req)
                return _Resposta()

        amb = {"AVISO_EVOLUTION_URL": "http://bot.local:8080/", "AVISO_EVOLUTION_APIKEY": "chave-bot",
               "AVISO_INSTANCIA": "bot", "DONO_WHATSAPP": "55DD000000000".replace("DD", "11")}
        token = mock.Mock(side_effect=AssertionError("não deveria ler o cofre"))
        with mock.patch.dict(os.environ, amb), \
                mock.patch.object(aviso.urllib.request, "build_opener", return_value=Abridor()):
            self.assertTrue(aviso.enviar("Greg: conexão caiu. http://127.0.0.1:8120", token_fn=token))
        req = pedidos[0]
        self.assertEqual(req.full_url, "http://bot.local:8080/message/sendText/bot")
        self.assertEqual(req.get_header("Apikey"), "chave-bot")
        self.assertEqual(json.loads(req.data)["number"], "55DD000000000".replace("DD", "11"))

    def test_sem_chave_nao_envia(self):
        amb = {"AVISO_EVOLUTION_URL": "http://bot.local:8080", "AVISO_EVOLUTION_APIKEY": "",
               "AVISO_INSTANCIA": "bot", "DONO_WHATSAPP": "55DD000000000".replace("DD", "11")}
        with mock.patch.dict(os.environ, amb), \
                mock.patch.object(aviso.urllib.request, "build_opener", side_effect=AssertionError("sem rede")):
            self.assertFalse(aviso.enviar("Greg: teste", token_fn=lambda: "x"))


class Agendador(unittest.TestCase):
    def setUp(self):
        self.ag = _carregar_agendador()

    def test_horarios(self):
        u = {}
        self.assertEqual(self.ag.devidos(datetime(2026, 11, 10, 14, 30), u), [["ciclo"]])
        self.assertEqual(self.ag.devidos(datetime(2026, 11, 10, 14, 30, 40), u), [])   # mesmo minuto: uma vez só
        self.assertEqual(self.ag.devidos(datetime(2026, 11, 10, 14, 31), u), [])
        self.assertEqual(self.ag.devidos(datetime(2026, 11, 10, 20, 5), u), [["semanal"]])
        self.assertEqual(self.ag.devidos(datetime(2026, 11, 10, 20, 35), u), [["semanal", "--executar-limpeza"]])
        self.assertEqual(self.ag.devidos(datetime(2026, 11, 10, 21, 0), u), [["ciclo"]])
        self.assertLessEqual(len(u), 1)


class PainelNoConteiner(unittest.TestCase):
    def test_bind_fora_do_loopback_so_no_conteiner(self):
        from app import server
        with mock.patch.dict(os.environ, {"VIGIA_CONTAINER": ""}):
            with self.assertRaises(ValueError):
                server.criar_servidor(object(), "t" * 24, porta=0, host="0.0.0.0")
        with mock.patch.dict(os.environ, {"VIGIA_CONTAINER": "1"}):
            with self.assertRaises(ValueError):
                server.criar_servidor(object(), "t" * 24, porta=0, host="192.168.0.10")

    def test_evolution_no_servico_api_so_no_conteiner(self):
        from app.erros import VigiaErro
        from app.evolution_client import EvolutionClient
        with mock.patch.dict(os.environ, {"VIGIA_CONTAINER": ""}):
            with self.assertRaises(VigiaErro):
                EvolutionClient("http://api:8080", "vigia", "chave")
        with mock.patch.dict(os.environ, {"VIGIA_CONTAINER": "1"}):
            EvolutionClient("http://api:8080", "vigia", "chave")
            with self.assertRaises(VigiaErro):
                EvolutionClient("http://exemplo.com:8080", "vigia", "chave")

    def test_ler_agora_no_conteiner_cria_gatilho_sem_launchctl(self):
        from types import SimpleNamespace
        from app.paginas import conexao as pagina
        with tempfile.TemporaryDirectory() as d:
            executor = mock.Mock(side_effect=AssertionError("launchctl não pode rodar no contêiner"))
            ctx = SimpleNamespace(config={"dados_dir": d}, executor=executor, raiz=str(RAIZ))
            req = SimpleNamespace(ctx=ctx, metodo="POST", corpo={"volta": "/demandas"}, cabecalhos={})
            with mock.patch.dict(os.environ, {"VIGIA_CONTAINER": "1"}), \
                    mock.patch.object(pagina.c, "metodo_errado", return_value=None), \
                    mock.patch.object(pagina.c, "origem_ok", return_value=True), \
                    mock.patch.object(pagina.c, "pediu_json", return_value=True), \
                    mock.patch.object(pagina.c, "dados_dir", return_value=Path(d)):
                r = pagina.ler_agora_post(req)
            self.assertTrue((Path(d) / "LER-AGORA").exists())
            executor.assert_not_called()
            self.assertIn(b"disparado", r.corpo if isinstance(r.corpo, bytes) else str(r.corpo).encode())


if __name__ == "__main__":
    unittest.main()


class PrepararDockerParaODmg(unittest.TestCase):
    """Pedido 49: o Greg.app do DMG gera o docker.env por bin/preparar-docker.sh --dono --tz, sem editor."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        (Path(self.tmp.name) / "bin").mkdir()
        self.script = Path(self.tmp.name) / "bin" / "preparar-docker.sh"
        self.script.write_bytes((RAIZ / "bin" / "preparar-docker.sh").read_bytes())
        self.script.chmod(0o755)
        self.env = Path(self.tmp.name) / "docker.env"

    def rodar(self, *args):
        import subprocess
        return subprocess.run(["/bin/bash", str(self.script), *args], capture_output=True, text=True, stdin=subprocess.DEVNULL)

    def valores(self):
        return dict(l.split("=", 1) for l in self.env.read_text().splitlines() if "=" in l and not l.startswith("#"))

    def test_cria_com_dono_e_fuso_e_depois_so_atualiza(self):
        self.assertEqual(self.rodar("--dono", "5592912345678", "--tz", "America/Manaus").returncode, 0)
        v = self.valores()
        self.assertEqual((v["DONO_WHATSAPP"], v["TZ"]), ("5592912345678", "America/Manaus"))
        self.assertEqual(self.env.stat().st_mode & 0o777, 0o600)
        token = v["VIGIA_TOKEN"]
        self.assertRegex(token, r"^[0-9a-f]{48}$")
        self.assertEqual(self.rodar("--dono", "5511912345678").returncode, 0)
        v2 = self.valores()
        self.assertEqual((v2["DONO_WHATSAPP"], v2["TZ"], v2["VIGIA_TOKEN"]), ("5511912345678", "America/Manaus", token))
        self.assertEqual(self.env.stat().st_mode & 0o777, 0o600)

    def test_recusa_valor_que_nao_e_numero_ou_fuso(self):
        for args in (("--dono", "55 11 9"), ("--dono", "x;rm -rf ~"), ("--tz", "../../etc"), ("--tz", "a b")):
            self.assertEqual(self.rodar(*args).returncode, 2, args)
        self.assertFalse(self.env.exists())

    # ---- porta ocupada no computador de quem instala (15/11/2026): o compose caía com "port is already
    # allocated". VIGIA_PORTAS_OCUPADAS diz ao script quais estão ocupadas, sem abrir porta de verdade.
    def portas(self, ocupadas="", *args):
        import subprocess
        r = subprocess.run(["/bin/bash", str(self.script), *args, "--portas-livres"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, env={**os.environ, "VIGIA_PORTAS_OCUPADAS": ocupadas})
        self.assertEqual(r.returncode, 0, r.stderr)
        return dict(l.split("=", 1) for l in r.stdout.splitlines() if "_PORTA_HOST=" in l), r

    def test_portas_livres_ficam_as_padrao_e_nada_e_gravado(self):
        self.rodar("--dono", "5592912345678")
        antes = self.env.read_text()
        p, r = self.portas("")
        self.assertEqual(p, {"VIGIA_PORTA_HOST": "8120", "EVOLUTION_PORTA_HOST": "8080"})   # o Postgres não publica porta
        self.assertEqual(self.env.read_text(), antes)
        self.assertNotIn("porta trocada", r.stderr)

    def test_porta_ocupada_vira_a_proxima_livre_e_fica_gravada(self):
        self.rodar("--dono", "5592912345678")
        token = self.valores()["VIGIA_TOKEN"]
        p, r = self.portas("8080 8081 8120")
        self.assertEqual(p, {"VIGIA_PORTA_HOST": "8121", "EVOLUTION_PORTA_HOST": "8082"})
        v = self.valores()
        self.assertEqual((v["VIGIA_PORTA_HOST"], v["EVOLUTION_PORTA_HOST"]), ("8121", "8082"))
        self.assertNotIn("PG_PORTA_HOST", v)
        self.assertEqual((v["VIGIA_TOKEN"], v["DONO_WHATSAPP"]), (token, "5592912345678"))   # o resto do arquivo fica
        self.assertEqual(self.env.stat().st_mode & 0o777, 0o600)
        self.assertIn("--env-file docker.env", r.stderr)          # fora do Greg.app, o compose precisa ler a porta dali
        # a porta trocada não volta sozinha para a padrão quando a antiga fica livre: o endereço já usado continua valendo
        p2, _ = self.portas("")
        self.assertEqual(p2, p)
        # e, ocupada de novo, anda a partir de onde estava
        p3, _ = self.portas("8121")
        self.assertEqual(p3["VIGIA_PORTA_HOST"], "8122")

    def test_duas_portas_nunca_caem_no_mesmo_numero(self):
        self.rodar("--dono", "5592912345678")
        p, _ = self.portas(" ".join(str(n) for n in range(8080, 8120)))    # a Evolution só acha livre a partir da 8120
        self.assertEqual((p["VIGIA_PORTA_HOST"], p["EVOLUTION_PORTA_HOST"]), ("8120", "8121"))

    def test_cria_o_arquivo_e_ja_escolhe_as_portas(self):
        p, _ = self.portas("8080", "--dono", "5592912345678")
        self.assertEqual(p["EVOLUTION_PORTA_HOST"], "8081")
        self.assertEqual(self.valores()["EVOLUTION_PORTA_HOST"], "8081")
        self.assertRegex(self.valores()["VIGIA_TOKEN"], r"^[0-9a-f]{48}$")

    def test_sem_porta_livre_por_perto_para_com_erro_e_nao_grava(self):
        import subprocess
        self.rodar("--dono", "5592912345678")
        antes = self.env.read_text()
        r = subprocess.run(["/bin/bash", str(self.script), "--portas-livres"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL,
                           env={**os.environ, "VIGIA_PORTAS_OCUPADAS": " ".join(str(n) for n in range(8120, 8322))})
        self.assertEqual(r.returncode, 1)
        self.assertIn("não achei porta livre", r.stderr)
        self.assertEqual(self.env.read_text(), antes)

    def test_confere_porta_de_verdade_quando_ha_alguem_escutando(self):
        """Sem a variável de teste: o script conecta em 127.0.0.1. Só portas que este teste abriu ou acabou de soltar."""
        import socket
        import subprocess

        def solta():
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            n = s.getsockname()[1]
            s.close()
            return n

        ocupada = 0
        for _ in range(200):     # o sistema sorteia a porta; no fim da faixa não sobram 200 acima, então sorteia de novo
            ouvindo = socket.socket()
            self.addCleanup(ouvindo.close)
            ouvindo.bind(("127.0.0.1", 0))
            if ouvindo.getsockname()[1] <= 65300:
                ouvindo.listen(1)
                ocupada = ouvindo.getsockname()[1]
                break
            ouvindo.close()
        self.assertTrue(ocupada, "o sistema só deu porta acima de 65300 em 200 sorteios")
        self.rodar("--dono", "5592912345678")
        livre = solta()
        with self.env.open("a") as f:
            f.write(f"VIGIA_PORTA_HOST={ocupada}\nEVOLUTION_PORTA_HOST={livre}\n")
        amb = {k: v for k, v in os.environ.items() if k != "VIGIA_PORTAS_OCUPADAS"}
        r = subprocess.run(["/bin/bash", str(self.script), "--portas-livres"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, env=amb)
        self.assertEqual(r.returncode, 0, r.stderr)
        p = dict(l.split("=", 1) for l in r.stdout.splitlines() if "_PORTA_HOST=" in l)
        self.assertGreater(int(p["VIGIA_PORTA_HOST"]), ocupada)
        self.assertEqual(self.valores()["VIGIA_PORTA_HOST"], p["VIGIA_PORTA_HOST"])

    def test_instalador_escolhe_a_porta_antes_de_subir_e_o_app_le_do_docker_env(self):
        """O Swift não roda na suíte: aqui fica o contrato entre o instalador, o app e o script."""
        inst = (RAIZ / "macapp" / "Vigia" / "instalador.swift").read_text(encoding="utf-8")
        principal = (RAIZ / "macapp" / "Vigia" / "main.swift").read_text(encoding="utf-8")
        # assertTrue com frase curta: assertIn despejaria o arquivo Swift inteiro na falha
        self.assertTrue('"--portas-livres"' in inst, "o instalador não chama preparar-docker.sh --portas-livres")
        self.assertTrue(inst.index('"--portas-livres"') < inst.index('["compose", "up", "-d"]'),
                        "a escolha de porta tem de vir antes do compose up")
        self.assertTrue('["compose", "ps", "-q"]' in inst, "com o Greg no ar as portas dele parecem ocupadas: conferir antes")
        for chave in ("VIGIA_PORTA_HOST", "EVOLUTION_PORTA_HOST"):
            self.assertTrue(f'"{chave}"' in inst, f"o instalador não repassa {chave} ao compose")
        self.assertFalse("PG_PORTA_HOST" in inst, "o Postgres não publica porta: o instalador não tem o que repassar")
        self.assertTrue('lerEnv(arquivoEnv)["VIGIA_PORTA_HOST"]' in principal, "o app não lê a porta do painel no docker.env")

    def test_portas_padrao_do_compose_nao_mudam(self):
        raiz = (RAIZ / "docker-compose.yml").read_text()
        infra = (RAIZ / "infra" / "docker-compose.yml").read_text()
        self.assertIn('"127.0.0.1:${VIGIA_PORTA_HOST:-8120}:8120"', raiz)
        self.assertIn('"127.0.0.1:${EVOLUTION_PORTA_HOST:-8080}:8080"', infra)
        # B4 da ciberseg r01 (15/11/2026): o Postgres não publica porta, nem em 127.0.0.1
        self.assertNotIn("PG_PORTA_HOST", infra)
        self.assertNotIn(":5432\"", infra)
