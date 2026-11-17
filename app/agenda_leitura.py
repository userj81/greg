"""Quando o Greg lê (pedido 81, D-82): intervalo (30 min, 1 h, 2 h ou 4 h) e horário (das HH às HH) escolhidos em Ajustes.
Quem chama: bin/coletor.py (decide no começo do ciclo se este disparo lê), app/saude.py (o vigia conta o disparo que
passou sem ler), app/paginas/conexao.py e demandas.py (texto da próxima leitura), app/mcp_ferramentas.py (greg_status).
O que não faz: não agenda nada. O agendador (LaunchAgent no Mac, bin/agendador.py no Docker) continua disparando o
ciclo nos minutos :00 e :30; aqui só se decide se o disparo lê ou passa. Com o padrão (30 min, o dia todo) todo
disparo lê, como sempre foi.
"""
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from app import preferencias as pf

PASSO_MIN = 30                  # o agendador dispara de 30 em 30 minutos
SINAL = "LEITURA-PEDIDA"        # "Ler agora" e a leitura por nova conexão (D-73) deixam este arquivo em dados/
SINAL_VALE_S = 600              # sinal mais velho que isso não vale: o disparo que ele pedia não aconteceu
PASSADA = "ultima-passada.json"  # o ciclo rodou e decidiu não ler: o vigia (app/saude.py) conta a partir daqui


def escolha(dados) -> tuple[int, int, int]:
    """(intervalo em minutos, hora inicial, hora final) de dados/preferencias.json. Arquivo ausente, ilegível ou valor
    fora da lista caem no padrão (30, 0, 0): ler como sempre é o lado seguro, nada deixa de ser lido por engano."""
    try:
        bruto = json.loads((Path(dados) / "preferencias.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        bruto = {}
    p = pf.validar(bruto if isinstance(bruto, dict) else {})
    return p["leitura_min"], p["leitura_de"], p["leitura_ate"]


def e_padrao(intervalo: int, de: int, ate: int) -> bool:
    return intervalo <= PASSO_MIN and de == ate


def _minuto_do_dia(agora: datetime) -> int:
    return agora.hour * 60 + agora.minute


def no_horario(agora: datetime, de: int, ate: int) -> bool:
    """Horas iguais = o dia todo. A leitura da hora final ainda sai ("das 8 às 18" lê às 18:00; às 18:30 não).
    Faixa que atravessa a meia-noite (das 22 às 6) vale do mesmo jeito."""
    if de == ate:
        return True
    duracao = ((ate - de) % 24) * 60 + PASSO_MIN
    return (_minuto_do_dia(agora) - de * 60) % 1440 < duracao


def inicio_do_trecho(agora: datetime, intervalo: int, de: int, ate: int) -> datetime:
    """Começo do trecho de `intervalo` minutos em que `agora` cai. Os trechos contam da hora inicial do horário (ou da
    meia-noite, com o dia todo): 2 h das 9 às 18 dá 9, 11, 13, 15, 17."""
    ancora = 0 if de == ate else de * 60
    dentro = (_minuto_do_dia(agora) - ancora) % 1440 % intervalo
    return agora.replace(second=0, microsecond=0) - timedelta(minutes=dentro)


def decidir(agora: datetime, ultima_em: float | None, intervalo: int, de: int, ate: int, *, pedida: bool = False,
            recuperacao: bool = False, pendente: bool = False) -> tuple[bool, str]:
    """Este disparo lê? Devolve (sim ou não, motivo em uma frase para o log; nunca texto de conversa).
    `pedida`: "Ler agora" ou nova conexão, lê fora do intervalo e do horário. `recuperacao`: segunda leitura do ciclo
    depois de a conexão voltar (D-54), vale dentro do horário. `pendente`: a leitura anterior não esvaziou a fila
    (o lote tem teto), então a seguinte não espera o intervalo. `ultima_em`: hora (epoch) da última leitura boa."""
    if pedida:
        return True, "leitura pedida"
    if not no_horario(agora, de, ate):
        return False, f"fora do horário de leitura ({texto_horario(de, ate)})"
    if intervalo <= PASSO_MIN or recuperacao or pendente:
        return True, "no horário"
    if ultima_em is None or float(ultima_em) < inicio_do_trecho(agora, intervalo, de, ate).timestamp():
        return True, "intervalo novo"
    return False, f"já leu neste intervalo ({pf.LEITURA_MIN[intervalo]})"


def proxima(agora: datetime, ultima_em: float | None, intervalo: int, de: int, ate: int,
            pendente: bool = False) -> datetime | None:
    """Hora do próximo disparo (:00 ou :30) que vai ler, olhando até dois dias à frente."""
    t = agora.replace(second=0, microsecond=0)
    t += timedelta(minutes=PASSO_MIN - t.minute % PASSO_MIN)
    for _ in range(2 * 24 * 60 // PASSO_MIN):
        if decidir(t, ultima_em, intervalo, de, ate, pendente=pendente)[0]:
            return t
        t += timedelta(minutes=PASSO_MIN)
    return None


def texto_horario(de: int, ate: int) -> str:
    return "o dia todo" if de == ate else f"das {de}h às {ate}h"


def texto_escolha(intervalo: int, de: int, ate: int) -> str:
    """'A cada 2 horas, das 8h às 18h' (ou só 'A cada 30 minutos' com o dia todo)."""
    base = f"A cada {pf.LEITURA_MIN.get(intervalo, pf.LEITURA_MIN[PASSO_MIN])}"
    return base if de == ate else f"{base}, {texto_horario(de, ate)}"


def texto_proxima(agora: datetime, quando: datetime | None) -> str:
    if quando is None:
        return ""
    hora = f"{quando.hour}:{quando.minute:02d}"
    if quando.date() == agora.date():
        return f"Próxima leitura às {hora}"
    if quando.date() == (agora + timedelta(days=1)).date():
        return f"Próxima leitura amanhã às {hora}"
    return f"Próxima leitura em {quando.day:02d}/{quando.month:02d} às {hora}"


# ------------------------------------------------------------------ estado em dados/

def _json(arq: Path) -> dict:
    try:
        d = json.loads(arq.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def ultima_leitura(dados) -> tuple[float | None, bool]:
    """(hora da última leitura boa, se ela deixou fila por ler) de dados/ultima-leitura.json."""
    d = _json(Path(dados) / "ultima-leitura.json")
    try:
        em = float(d.get("em")) if d.get("em") else None
    except (TypeError, ValueError):
        em = None
    return em, bool(d.get("pendente"))


def pedir(dados) -> None:
    """Deixa o sinal de leitura pedida (0600). Quem chama trata OSError: sem o sinal, o disparo segue a agenda."""
    arq = Path(dados) / SINAL
    fd = os.open(arq, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(fd)
    os.utime(arq, None)


def consumir_pedido(dados, agora: float | None = None) -> bool:
    """Há leitura pedida nos últimos 10 minutos? O sinal é apagado de qualquer jeito: vale para um disparo só."""
    arq = Path(dados) / SINAL
    try:
        idade = float(agora if agora is not None else time.time()) - arq.stat().st_mtime
        arq.unlink()
    except OSError:
        return False
    return idade <= SINAL_VALE_S


def marcar_passada(dados, agora: float, motivo: str) -> None:
    """O ciclo rodou e não leu (fora do horário ou do intervalo). Nunca derruba o ciclo."""
    dados = Path(dados)
    try:
        dados.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = dados / (PASSADA + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"em": int(agora), "motivo": str(motivo)}, f, ensure_ascii=False)
        os.replace(tmp, dados / PASSADA)
    except OSError:
        pass


def ultima_passada(dados) -> float:
    try:
        return float(_json(Path(dados) / PASSADA).get("em") or 0)
    except (TypeError, ValueError):
        return 0.0


def deste_disparo(dados, *, agora: float | None = None, forcar: bool = False, recuperacao: bool = False) -> tuple[bool, str]:
    """Decisão do ciclo real: junta a escolha de Ajustes, a última leitura e o sinal de leitura pedida."""
    t = float(agora if agora is not None else time.time())
    pedida = consumir_pedido(dados, t) or forcar          # o sinal é sempre consumido, mesmo com o padrão
    intervalo, de, ate = escolha(dados)
    em, pendente = ultima_leitura(dados)
    return decidir(datetime.fromtimestamp(t), em, intervalo, de, ate, pedida=pedida, recuperacao=recuperacao,
                   pendente=pendente)


def proxima_fora_do_padrao(dados, agora: float | None = None) -> str | None:
    """'Próxima leitura às 14:00' pela escolha de Ajustes; None com o padrão (a tela mantém a conta de sempre: último
    lote mais 30 minutos) ou se algo não pôde ser lido."""
    try:
        r = resumo(dados, agora)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return None if r["padrao"] else (r["proxima"] or None)


def resumo(dados, agora: float | None = None) -> dict:
    """Para a tela e o conector: o que está escolhido e quando sai a próxima leitura."""
    t = datetime.fromtimestamp(float(agora if agora is not None else time.time()))
    intervalo, de, ate = escolha(dados)
    em, pendente = ultima_leitura(dados)
    return {"intervalo_min": intervalo, "intervalo": pf.LEITURA_MIN[intervalo], "horario": texto_horario(de, ate),
            "padrao": e_padrao(intervalo, de, ate), "texto": texto_escolha(intervalo, de, ate),
            "proxima": texto_proxima(t, proxima(t, em, intervalo, de, ate, pendente))}
