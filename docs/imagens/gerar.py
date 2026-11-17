#!/opt/homebrew/bin/python3
"""Gera as capturas de exemplo do README (docs/imagens/*.jpg) com DADOS FICTÍCIOS.
Sobe o painel REAL (app/server.py) numa porta livre, com Evolution, Frentes e Postgres falsos (os mesmos fakes da suíte),
banco SQLite, cursor e preferências em pasta temporária, e fotografa as telas a 1120×760 (janela do Greg.app) com o
Playwright já instalado no Mac (docs/imagens/capturar.mjs). Nada do painel vivo é lido: nenhum nome, número ou mensagem
real entra aqui; números na faixa +1 415 555 01xx (reservada a ficção). O token de login é sorteado por execução e vai
para o capturador pelo stdin, nunca por argv.
Uso: /opt/homebrew/bin/python3 docs/imagens/gerar.py [--html <pasta>]   (grava os .jpg ao lado deste arquivo; nada sai do Mac)
  --html <pasta>  também salva o HTML de cada tela na pasta (para a prova visual de ~/ferramentas/fabricas/design/bin/).
Saída: 0 ok · 1 falhou · 2 Playwright ausente.
"""
import json
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

RAIZ = Path(__file__).resolve().parents[2]
AQUI = Path(__file__).resolve().parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from app import fila_envio, layout, preferencias, server  # noqa: E402
from app.banco import Banco  # noqa: E402
from app.conexao import Conexao  # noqa: E402
from app.cursor import Cursor  # noqa: E402
from app.evolution_client import EvolutionClient  # noqa: E402
from app.frentes_ponte import FrentesPonte  # noqa: E402
from testes.fakes import FakeEvolution, FakeFrentes  # noqa: E402
from testes.test_conversas_zap import PsqlFalso, k, linha_lista  # noqa: E402

AGORA = int(time.time()) - 120


def _proximo(alvo) -> str:
    """Próxima data (nunca hoje) em que `alvo(data)` vale. Os prazos do exemplo são relativos ao dia da captura:
    com data fixa, a tela de Demandas saía cheia de "atrasado" em toda captura feita depois dela."""
    d = date.today() + timedelta(days=1)
    while not alvo(d):
        d += timedelta(days=1)
    return d.isoformat()


QUARTA, QUINTA, SEXTA, SABADO = (_proximo(lambda d, n=n: d.weekday() == n) for n in (2, 3, 4, 5))
DIA_20 = _proximo(lambda d: d.day == 20)
ANA = "14155550101@s.whatsapp.net"
BRUNO = "14155550102@s.whatsapp.net"
CARLA = "14155550103@s.whatsapp.net"
DIEGO = "14155550104@s.whatsapp.net"
SEM_NOME = "445566778899@lid"
OBRA = "120363000000000042@g.us"
AUDIO_OBRA = "ob2"
FOTO_OBRA = "ob4"
TRANSCRICAO = "Rafa, o eletricista confirmou pra quinta de manhã. Consegue liberar o portão às oito?"

CAPTURAS = [
    # Estilo Papel (pedido 39): só o tema muda (claro/escuro); não há mais estilo, vidro nem cor própria.
    {"nome": "demandas-claro", "rota": "/demandas", "pref": {"tema": "claro"}},
    {"nome": "conversas-claro", "rota": f"/conversas?c={OBRA}&f=todas", "pref": {"tema": "claro"}},
    {"nome": "conexao-claro", "rota": "/conexao", "pref": {"tema": "claro"}},
    {"nome": "frentes-claro", "rota": "/frentes", "pref": {"tema": "claro"}},
    {"nome": "envios-claro", "rota": "/envios", "pref": {"tema": "claro"}},
    {"nome": "contatos-claro", "rota": "/contatos", "pref": {"tema": "claro"}},
    {"nome": "historico-claro", "rota": "/historico", "pref": {"tema": "claro"}},
    {"nome": "ajustes-claro", "rota": "/ajustes", "pref": {"tema": "claro"}},
    # Janela dividida (pedido 48): lateral recolhida e a lista de Demandas alargada pela divisória.
    {"nome": "demandas-dividida", "rota": "/demandas", "pref": {"tema": "claro"},
     "layout": {"recolhida": True, "listas": {"/demandas": 470}}},
    {"nome": "demandas-escuro", "rota": "/demandas", "pref": {"tema": "escuro"}},
    {"nome": "conexao-escuro", "rota": "/conexao", "pref": {"tema": "escuro"}},
    # Modo de gravação (D-24): o capturador liga pelo interruptor de Ajustes e recarrega antes de fotografar.
    # Pedido 50: envio liberado (clipe, microfone e Enviar à mostra) com um rascunho no campo; a conversa já traz foto,
    # resposta citando e reação.
    {"nome": "conversas-responder", "rota": f"/conversas?c={OBRA}&f=todas", "pref": {"tema": "claro"}, "liberar": True,
     "rascunho": "Perfeito, combinado. Obrigado!"},
    {"nome": "conversas-gravacao", "rota": f"/conversas?c={OBRA}&f=todas", "pref": {"tema": "claro"}, "gravacao": True},
]


def foto_ficticia() -> bytes | None:
    """Foto de exemplo desenhada na hora (piso de madeira), para a conversa mostrar uma imagem sem usar foto de ninguém."""
    try:
        r = subprocess.run(["magick", "-seed", "7", "-size", "640x960", "xc:gray50", "-attenuate", "0.8", "+noise", "Gaussian",
                            "-colorspace", "Gray", "-motion-blur", "0x90+90", "-rotate", "90", "+repage", "-auto-level",
                            "+level-colors", "#8E6A44,#D9BC93", "-fill", "none", "-stroke",
                            "rgba(60,40,20,0.35)", "-strokewidth", "3", "-draw",
                            "line 0,160 960,160 line 0,320 960,320 line 0,480 960,480", "-strip", "jpg:-"],
                           capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 and r.stdout[:2] == b"\xff\xd8" else None


def _texto(s: str) -> str:
    return json.dumps({"conversation": s})


def semear_postgres(psql: PsqlFalso) -> None:
    """Lista de conversas e o fio do grupo, como a consulta ao Postgres da Evolution devolveria."""
    psql.lista = [
        linha_lista(OBRA, AGORA - 60, "ob3", k(OBRA, True, "ob3"), "", "conversation",
                    {"conversation": "Chegou sim, mando no fim do dia. Portão liberado quinta às 8."}, 0, "", ""),
        linha_lista(ANA, AGORA - 600, "a2", k(ANA, mid="a2"), "Ana Souza", "conversation",
                    {"conversation": "Consegue mandar a arte do cardápio até sexta?"}, 2, "Ana Souza", "Ana Souza"),
        linha_lista(CARLA, AGORA - 1800, "c1", k(CARLA, mid="c1"), "Carla Menezes", "conversation",
                    {"conversation": "Lembra de renovar o domínio da loja até o dia 20"}, 1, "Carla Menezes", "Carla Menezes"),
        linha_lista(BRUNO, AGORA - 5400, "b1", k(BRUNO, mid="b1"), "Bruno Lima", "conversation",
                    {"conversation": "Entrega do material remarcada para segunda"}, 0, "Bruno Lima", "Bruno Lima"),
        linha_lista(SEM_NOME, AGORA - 9000, "l1", k(SEM_NOME, mid="l1"), "", "audioMessage",
                    {"audioMessage": {"seconds": 42}}, 0, "", ""),
    ]
    psql.msgs[OBRA] = [
        ["ob1", k(OBRA, mid="ob1", participant=ANA), "Ana Souza", "conversation",
         _texto("Pessoal, o orçamento do piso que pedi na terça chegou?"), str(AGORA - 300), "READ"],
        [AUDIO_OBRA, k(OBRA, mid=AUDIO_OBRA, participant=BRUNO), "Bruno Lima", "audioMessage",
         json.dumps({"audioMessage": {"seconds": 18}}), str(AGORA - 120), "READ"],
        [FOTO_OBRA, k(OBRA, mid=FOTO_OBRA, participant=BRUNO), "Bruno Lima", "imageMessage",
         json.dumps({"imageMessage": {"caption": "O piso da sala chegou, olha como ficou."}}), str(AGORA - 100), "READ"],
        # resposta citando a pergunta da Ana (coluna 7 = citada, como a consulta devolve)
        ["ob3", k(OBRA, True, "ob3"), "", "conversation",
         _texto("Chegou sim, mando no fim do dia. Portão liberado quinta às 8."), str(AGORA - 60), "READ",
         json.dumps({"id": "ob1", "de": ANA, "msg": {"conversation": "Pessoal, o orçamento do piso que pedi na terça chegou?"}})],
    ]
    psql.chaves[FOTO_OBRA] = [k(OBRA, mid=FOTO_OBRA, participant=BRUNO), "imageMessage"]
    psql.reacoes[OBRA] = [[k(OBRA, mid="rx1", participant=ANA), "ob3", "\U0001F44D", "Ana Souza"]]
    psql.chaves[AUDIO_OBRA] = [k(OBRA, mid=AUDIO_OBRA, participant=BRUNO), "audioMessage"]


def semear_frentes(ff: FakeFrentes) -> dict:
    ff.frentes.append({"id": 3, "nome": "Loja do bairro", "tipo": "trabalho", "apelidos": ["loja"], "prioridade": 2,
                       "status": "ativa"})
    itens = {}
    itens["arte"] = ff.semear_item(frente_id=1, texto="Mandar a arte do cardápio para a Ana", prazo=SEXTA,
                                  origem="whatsapp", confirmado=1)
    itens["portao"] = ff.semear_item(frente_id=2, texto="Liberar o portão para o eletricista quinta às 8h",
                                    prazo=QUINTA, origem="whatsapp", confirmado=1, prioridade=1)
    itens["dominio"] = ff.semear_item(frente_id=None, texto="Renovar o domínio da loja até o dia 20", prazo=DIA_20,
                                     origem="whatsapp", confirmado=0)
    ff.semear_item(frente_id=1, texto="Fechar a proposta do site novo", prazo=SABADO)
    ff.semear_item(frente_id=2, texto="Trocar o filtro da água")
    ff.semear_item(frente_id=3, texto="Conferir o estoque de embalagens", prazo=QUARTA)
    # D-57: item herdado pelo cartão que substituiu o da reunião remarcada
    itens["reuniao"] = ff.semear_item(frente_id=1, tipo="prazo", prazo=QUINTA,
                                      texto=f"WA Ana Souza: Reunião de alinhamento remarcada para quinta às 15h (prazo {QUINTA})")
    return itens


def semear_banco(banco: Banco, itens: dict) -> None:
    banco.migrar()
    banco.gravar_classificacao(msg_id="a2", conversa=ANA, contato="Ana Souza", ts=AGORA - 600, classe="demanda",
                               resumo="Pede a arte do cardápio até sexta", confianca="alta", frente_id=1,
                               item_id=itens["arte"]["id"], prazo=SEXTA)
    banco.gravar_classificacao(msg_id=AUDIO_OBRA, conversa=OBRA, contato="Bruno Lima", ts=AGORA - 120, classe="atencao",
                               resumo="Eletricista vem quinta às 8h: liberar o portão", confianca="alta", frente_id=2,
                               item_id=itens["portao"]["id"], prazo=QUINTA)
    banco.gravar_classificacao(msg_id="c1", conversa=CARLA, contato="Carla Menezes", ts=AGORA - 1800, classe="lembrar",
                               resumo="Renovar o domínio da loja até o dia 20", confianca="baixa",
                               item_id=itens["dominio"]["id"], prazo=DIA_20)
    banco.gravar_classificacao(msg_id="b1", conversa=BRUNO, contato="Bruno Lima", ts=AGORA - 5400, classe="aviso",
                               resumo="Entrega do material remarcada para segunda", confianca="alta")
    banco.gravar_classificacao(msg_id="ob1", conversa=OBRA, contato="Ana Souza", ts=AGORA - 300, classe="lembrando",
                               resumo="Cobra o orçamento do piso pedido na terça", confianca="alta", frente_id=2)
    # D-57: cartão provisório (sobe ao Frentes no fechamento do dia) e evento remarcado (o antigo vira "substituído")
    banco.gravar_classificacao(msg_id="c2", conversa=CARLA, contato="Carla Menezes", ts=AGORA - 900, classe="demanda",
                               resumo="Pede o orçamento do site novo até quinta", confianca="alta", frente_id=1)
    # D-58: cartão que a IA segurou no fechamento (era só conversa) e uma linha de histórico por conversa
    seg = banco.gravar_classificacao(msg_id="b0", conversa=BRUNO, contato="Bruno Lima", ts=AGORA - 7200, classe="demanda",
                                     resumo="Pergunta se o portão novo já chegou", confianca="alta", frente_id=2)
    banco.rebaixar(seg, "aviso", "você já respondeu na conversa")
    banco.gravar_analise(msg_id="a3", conversa=ANA, contato="Ana Souza", ts=AGORA - 240, situacao="aguarda_dono",
                         resumo="Ana remarcou a reunião para quinta às 15h e pede a arte do cardápio até sexta")
    banco.gravar_analise(msg_id="b1", conversa=BRUNO, contato="Bruno Lima", ts=AGORA - 5400, situacao="encerrada",
                         resumo="Bruno avisou que a entrega do material ficou para segunda")
    # Histórico (/historico): dias anteriores, quem ficou sem resposta e a mesma conversa mudando de pé
    for msg_id, conversa, contato, ha, situacao, resumo in (
            ("c2", CARLA, "Carla Menezes", 900, "aguarda_dono", "Carla pediu o orçamento do site novo até quinta"),
            ("ob3", OBRA, "Obra da casa nova", 60, "aguarda_contato",
             "Você liberou o portão para quinta às 8h; o grupo ficou de confirmar o horário do eletricista"),
            ("b0", BRUNO, "Bruno Lima", 7200, "encerrada", "Bruno perguntou se o portão novo já chegou e você respondeu que chega segunda"),
            ("h-a2", ANA, "Ana Souza", 22 * 3600, "encerrada", "Você marcou a reunião de alinhamento para quinta às 10h e a Ana confirmou"),
            ("h-a1", ANA, "Ana Souza", 26 * 3600, "aguarda_dono", "Ana pediu para marcar a reunião de alinhamento desta semana"),
            ("h-o1", OBRA, "Obra da casa nova", 27 * 3600, "aguarda_dono", "O mestre de obras pediu a liberação do portão para o eletricista"),
            ("h-d1", DIEGO, "Diego Prado", 3 * 86400 + 1800, "aguarda_dono", "Diego mandou a proposta revisada do telhado e pediu retorno"),
            ("h-c1", CARLA, "Carla Menezes", 3 * 86400 + 5400, "aguarda_contato",
             "Você pediu os acessos da loja e a Carla ficou de mandar")):
        banco.gravar_analise(msg_id=msg_id, conversa=conversa, contato=contato, ts=AGORA - ha, situacao=situacao, resumo=resumo)
    velho = banco.gravar_classificacao(msg_id="a0", conversa=ANA, contato="Ana Souza", ts=AGORA - 3600, classe="lembrar",
                                       resumo="Reunião de alinhamento quinta às 10h", confianca="alta", frente_id=1,
                                       item_id=itens["reuniao"]["id"], prazo=QUINTA)
    novo = banco.gravar_classificacao(msg_id="a3", conversa=ANA, contato="Ana Souza", ts=AGORA - 240, classe="lembrar",
                                      resumo="Reunião de alinhamento remarcada para quinta às 15h", confianca="alta",
                                      frente_id=1, prazo=QUINTA)
    banco.marcar_lancado(novo, itens["reuniao"]["id"], frente_id=1)
    banco.substituir(velho, novo, "atualizada")
    banco.gravar_transcricao(msg_id=AUDIO_OBRA, conversa=OBRA, ts=AGORA - 120, estado="ok", texto=TRANSCRICAO,
                             segundos=18, segundos_lidos=18, motor="mlx_whisper/whisper-large-v3-turbo", tempo_ms=1900)
    banco.registrar_lote(nome=time.strftime("%Y-%m-%dT%H-%M", time.localtime(AGORA)), conversas=3, novas=7, itens=2,
                         atencao=1, tokens_in=5400, tokens_out=620, modelo="claude-sonnet-5-5", custo_usd=0.021,
                         duracao_ms=8400, turnos=1)


def main() -> int:
    tmp = tempfile.TemporaryDirectory()
    dados = Path(tmp.name) / "dados"
    dados.mkdir(mode=0o700)
    (dados / "cursor.json").write_text(json.dumps({"ultimo_seq": 1042, "pareado_em": AGORA - 36 * 3600}))
    # Cache de nomes de grupo já quente (a busca real é em segundo plano e perderia a primeira foto) e último ciclo do
    # monitor, para o cartão de Conexão não dizer "monitor ainda não rodou".
    (dados / "grupos.json").write_text(json.dumps({"grupos": {OBRA: {"subject": "Obra da casa nova", "foto": "",
                                                                     "membros": 6, "em": AGORA}}}))
    (dados / "conexao.json").write_text(json.dumps({"evento": "ok", "motivo": None, "reason_code": None,
                                                    "ultimo_estado": "open", "atualizado_em": AGORA - 1500,
                                                    "logout_pendente": False}))
    preferencias.ARQUIVO = dados / "preferencias.json"
    layout.ARQUIVO = dados / "layout.json"   # na pasta temporária: a captura nunca escreve no layout vivo

    fe = FakeEvolution().iniciar()
    ff = FakeFrentes().iniciar()
    try:
        fe.estado = "open"
        fe.grupos = [{"id": OBRA, "subject": "Obra da casa nova", "pictureUrl": "", "size": 6}]
        psql = PsqlFalso()
        semear_postgres(psql)
        foto = foto_ficticia()
        if foto:
            fe.midia_por_id[FOTO_OBRA] = foto
        itens = semear_frentes(ff)
        banco = Banco(str(dados / "vigia.db"))
        semear_banco(banco, itens)

        evo = EvolutionClient(fe.url, fe.instancia, fe.apikey)
        ponte = FrentesPonte(ff.url, ff.token)
        conexao = Conexao(evo, banco, cursor=Cursor.carregar(dados / "cursor.json"), estado_arq=dados / "conexao.json")
        token = secrets.token_hex(16)   # só desta execução; nunca o VIGIA_TOKEN real
        # Token de envio também sorteado: só para a conversa mostrar a caixa de responder em vez de "Envio desligado".
        # A fila nasce sem a thread, então nada é enviado (e a Evolution aqui é a falsa).
        token_envio = secrets.token_hex(16)
        ctx = SimpleNamespace(banco=banco, ponte=ponte, conexao=conexao, evo=evo, config={"dados_dir": str(dados)},
                              tokens=SimpleNamespace(envio=token_envio, painel=token), executor=None,
                              raiz=str(RAIZ), psql=psql, bloqueio=None)
        ctx.fila = fila_envio.Fila(ctx, limites=fila_envio.limites_de_config({}))   # sem thread: nada envia

        srv = server.criar_servidor(ctx, token, porta=0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            plano = {"base": f"http://127.0.0.1:{srv.server_address[1]}", "token": token,
                     "preferencias": str(preferencias.ARQUIVO), "layout": str(layout.ARQUIVO), "saida": str(AQUI),
                     "token_envio": token_envio, "capturas": CAPTURAS}
            if "--html" in sys.argv:
                plano["html_saida"] = str(Path(sys.argv[sys.argv.index("--html") + 1]).resolve())
                Path(plano["html_saida"]).mkdir(parents=True, exist_ok=True)
            r = subprocess.run(["node", str(AQUI / "capturar.mjs")], input=json.dumps(plano), text=True,
                               capture_output=True, timeout=300)
            sys.stdout.write(r.stdout)
            if r.returncode != 0:
                sys.stderr.write(r.stderr[-2000:])
                return r.returncode
        finally:
            srv.shutdown()
            srv.server_close()
    finally:
        fe.parar()
        ff.parar()
        tmp.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
