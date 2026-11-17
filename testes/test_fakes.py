"""Testa os próprios fakes (testes/fakes.py): sobem, respondem, contam QR e recusam sem credencial.
Quem chama: unittest; é o primeiro verde da F0.
O que não faz: não testa nenhuma estação.
"""
import base64
import json
import sys
import tomllib
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from testes.fakes import FakeEvolution, FakeFrentes, fila_fixture, imagem_jpeg_minima  # noqa: E402
from testes.util_contrato import ABRIDOR, RAIZ  # noqa: E402


def chamar(url, metodo="GET", cabecalhos=None, corpo=None):
    dados = json.dumps(corpo).encode() if corpo is not None else None
    req = urllib.request.Request(url, data=dados, method=metodo, headers=cabecalhos or {})
    if dados is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with ABRIDOR.open(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        with e:
            return e.code, json.loads(e.read().decode() or "null")


class TestFakeEvolution(unittest.TestCase):
    def setUp(self):
        self.fake = FakeEvolution().iniciar()
        self.addCleanup(self.fake.parar)
        self.h = {"apikey": self.fake.apikey}
        self.i = self.fake.instancia

    def test_sobe_em_loopback_porta_livre(self):
        self.assertTrue(self.fake.url.startswith("http://127.0.0.1:"))
        self.assertGreater(self.fake.porta, 0)

    def test_recusa_sem_apikey(self):
        st, _ = chamar(f"{self.fake.url}/instance/connectionState/{self.i}")
        self.assertEqual(st, 401)
        st, _ = chamar(f"{self.fake.url}/instance/connectionState/{self.i}", cabecalhos={"apikey": "errada"})
        self.assertEqual(st, 401)
        self.assertEqual(self.fake.negadas, 2)
        self.assertEqual(self.fake.chamadas, [])

    def test_estado_nao_conta_qr(self):
        self.fake.estado = "open"
        st, obj = chamar(f"{self.fake.url}/instance/connectionState/{self.i}", cabecalhos=self.h)
        self.assertEqual(st, 200)
        self.assertEqual(obj, {"instance": {"instanceName": self.i, "state": "open"}})
        self.assertEqual(self.fake.qrs_gerados, 0)

    def test_connect_conta_qr_e_devolve_base64(self):
        for esperado in (1, 2):
            st, obj = chamar(f"{self.fake.url}/instance/connect/{self.i}", cabecalhos=self.h)
            self.assertEqual(st, 200)
            self.assertTrue(obj["base64"].startswith("data:image/png;base64,"))
            self.assertTrue(obj["code"])
            self.assertEqual(self.fake.qrs_gerados, esperado)
        self.assertEqual(self.fake.chamadas.count(("GET", f"/instance/connect/{self.i}")), 2)

    def test_logout_fecha_estado(self):
        self.fake.estado = "connecting"
        st, _ = chamar(f"{self.fake.url}/instance/logout/{self.i}", "DELETE", self.h)
        self.assertEqual(st, 200)
        self.assertEqual(self.fake.estado, "close")

    def test_fetch_instances_com_reason_code(self):
        self.fake.estado = "open"
        _, obj = chamar(f"{self.fake.url}/instance/fetchInstances?instanceName={self.i}", cabecalhos=self.h)
        self.assertEqual(obj[0]["connectionStatus"], "open")
        self.assertIsNone(obj[0]["disconnectionReasonCode"])
        self.fake.reason_code = 428
        _, obj = chamar(f"{self.fake.url}/instance/fetchInstances?instanceName={self.i}", cabecalhos=self.h)
        self.assertEqual(obj[0]["disconnectionReasonCode"], 428)
        self.assertTrue(obj[0]["disconnectionAt"])

    def test_midia_jpeg_valido_e_override(self):
        _, obj = chamar(f"{self.fake.url}/chat/getBase64FromMediaMessage/{self.i}", "POST", self.h,
                        {"message": {"key": {"id": "x"}}, "convertToMp4": False})
        bruto = base64.b64decode(obj["base64"])
        self.assertTrue(bruto.startswith(b"\xff\xd8\xff"))
        self.assertEqual(obj["mimetype"], "image/jpeg")
        self.fake.midia_base64_texto = base64.b64encode(b"isto nao e imagem").decode()
        _, obj = chamar(f"{self.fake.url}/chat/getBase64FromMediaMessage/{self.i}", "POST", self.h, {})
        self.assertEqual(base64.b64decode(obj["base64"]), b"isto nao e imagem")

    def test_send_text_guarda_enviadas(self):
        st, obj = chamar(f"{self.fake.url}/message/sendText/{self.i}", "POST", self.h,
                         {"number": "5592000000000", "text": "oi"})
        self.assertIn(st, (200, 201))
        self.assertTrue(obj["key"]["id"].startswith("3EB0"))
        self.assertEqual(self.fake.enviadas, [{"number": "5592000000000", "text": "oi"}])

    def test_parar_libera_porta(self):
        url = self.fake.url
        self.fake.parar()
        with self.assertRaises(Exception):
            chamar(f"{url}/instance/connectionState/{self.i}", cabecalhos=self.h)


class TestFakeFrentes(unittest.TestCase):
    def setUp(self):
        self.fake = FakeFrentes().iniciar()
        self.addCleanup(self.fake.parar)
        self.h = {"X-Frentes-Token": self.fake.token}

    def test_recusa_sem_token(self):
        st, _ = chamar(f"{self.fake.url}/api/frentes")
        self.assertEqual(st, 401)
        st, _ = chamar(f"{self.fake.url}/api/frentes", cabecalhos={"X-Frentes-Token": "x"})
        self.assertEqual(st, 401)

    def test_frentes_e_estado(self):
        st, obj = chamar(f"{self.fake.url}/api/frentes", cabecalhos=self.h)
        self.assertEqual(st, 200)
        self.assertEqual({"id", "nome", "tipo", "apelidos", "prioridade", "status"}, set(obj["frentes"][0]))
        _, est = chamar(f"{self.fake.url}/api/estado", cabecalhos=self.h)
        self.assertEqual(set(est), {"gerado_em", "hoje", "frentes", "inbox", "agenda", "totais"})
        self.assertIsInstance(est["hoje"], str)
        self.assertIn("n_abertos", est["frentes"][0])
        self.assertIn("n_confirmar", est["frentes"][0])

    def test_captura_cria_item_whatsapp(self):
        st, obj = chamar(f"{self.fake.url}/api/captura", "POST", self.h,
                         {"texto": "WA Maria: cobrar proposta", "origem": "whatsapp", "frente_id": 1})
        self.assertEqual(st, 200)
        self.assertEqual(obj["captura_id"], 1)
        item = obj["itens"][0]
        self.assertEqual((item["origem"], item["frente_id"], item["confirmado"]), ("whatsapp", 1, 1))
        self.assertEqual(len(self.fake.capturas), 1)
        self.assertIn(item["id"], self.fake.itens)

    def test_post_itens_e_inbox(self):
        st, obj = chamar(f"{self.fake.url}/api/itens", "POST", self.h,
                         {"texto": "x", "origem": "whatsapp", "frente_id": None, "tipo": "prazo",
                          "prazo": "2026-11-10", "prioridade": 1})
        self.assertEqual(st, 201)
        self.assertEqual(obj["id"], obj["item"]["id"])
        self.assertEqual((obj["item"]["tipo"], obj["item"]["prioridade"], obj["item"]["origem"]),
                         ("prazo", 1, "whatsapp"))
        self.fake.semear_item(texto="y", frente_id=1)
        _, todos = chamar(f"{self.fake.url}/api/itens", cabecalhos=self.h)
        self.assertEqual(len(todos["itens"]), 2)
        _, inbox = chamar(f"{self.fake.url}/api/itens?frente_id=inbox", cabecalhos=self.h)
        self.assertEqual([i["texto"] for i in inbox["itens"]], ["x"])
        st, _ = chamar(f"{self.fake.url}/api/itens", "POST", self.h, {"texto": " "})
        self.assertEqual(st, 400)

    def test_captura_vazia_400(self):
        st, _ = chamar(f"{self.fake.url}/api/captura", "POST", self.h, {"texto": "  ", "origem": "whatsapp"})
        self.assertEqual(st, 400)
        self.assertEqual(self.fake.capturas, [])

    def test_patch_e_filtros_de_itens(self):
        a = self.fake.semear_item(texto="a", frente_id=1)
        b = self.fake.semear_item(texto="b", frente_id=2, status="feito")
        st, obj = chamar(f"{self.fake.url}/api/itens/{a['id']}", "PATCH", self.h, {"confirmado": 0, "nota": "n"})
        self.assertEqual(st, 200)
        self.assertNotIn("ok", obj)
        self.assertEqual(obj["item"]["confirmado"], 0)
        self.assertNotIn("nota", obj["item"])
        self.assertEqual(self.fake.patches, [{"id": a["id"], "corpo": {"confirmado": 0, "nota": "n"}}])
        _, obj = chamar(f"{self.fake.url}/api/itens?status=aberto", cabecalhos=self.h)
        self.assertEqual([i["id"] for i in obj["itens"]], [a["id"]])
        _, obj = chamar(f"{self.fake.url}/api/itens?frente_id=2&status=feito", cabecalhos=self.h)
        self.assertEqual([i["id"] for i in obj["itens"]], [b["id"]])
        st, _ = chamar(f"{self.fake.url}/api/itens/999", "PATCH", self.h, {"confirmado": 0})
        self.assertEqual(st, 404)


class TestFixtures(unittest.TestCase):
    def test_imagem_jpeg_minima(self):
        b = imagem_jpeg_minima()
        self.assertTrue(b.startswith(b"\xff\xd8\xff"))
        self.assertTrue(b.endswith(b"\xff\xd9"))

    def test_fila_fixture_formato_e_variedade(self):
        linhas = fila_fixture(12, 1_000_000)
        self.assertEqual(len(linhas), 12)
        chaves = {"seq", "id", "key", "pushName", "messageType", "message", "messageTimestamp"}
        for ln in linhas:
            self.assertEqual(set(ln), chaves)
            self.assertEqual(set(ln["key"]), {"id", "remoteJid", "fromMe"})
            self.assertIsInstance(ln["message"], dict)
        self.assertEqual([ln["seq"] for ln in linhas], list(range(1, 13)))
        self.assertTrue(any(ln["key"]["remoteJid"].endswith("@g.us") for ln in linhas))
        self.assertTrue(any(ln["key"]["fromMe"] for ln in linhas))
        self.assertEqual({ln["messageType"] for ln in linhas}, {"conversation", "imageMessage", "audioMessage"})
        self.assertEqual(linhas[0]["messageTimestamp"], 1_000_000)

    def test_fixture_pequena_sem_grupo_nem_fromme(self):
        for ln in fila_fixture(3, 5):
            self.assertFalse(ln["key"]["remoteJid"].endswith("@g.us"))
            self.assertFalse(ln["key"]["fromMe"])


class TestConfig(unittest.TestCase):
    def test_config_toml_valores_do_contrato(self):
        with open(RAIZ / "config.toml", "rb") as f:
            c = tomllib.load(f)
        self.assertEqual((c["contexto"]["horas"], c["contexto"]["minimo"]), (48, 30))
        self.assertEqual((c["qr"]["segundos"], c["qr"]["maximo"], c["qr"]["teto_dia"]), (60, 3, 10))
        self.assertEqual(c["grupos"]["permitidos"], [])
        self.assertEqual((c["midia"]["teto_ciclo"], c["midia"]["pausa_s"]), (20, 1.0))
        self.assertEqual((c["horarios"]["diario"], c["horarios"]["semanal"]), ("21:00", "dom 20:00"))   # diario: D-57


if __name__ == "__main__":
    unittest.main()
