"""Transcrição automática de áudio (pedido 30, D-47).
Motor LOCAL já instalado no Mac (`mlx_whisper`, modelo no cache do Hugging Face, `HF_HUB_OFFLINE=1`): nada sai da
máquina e nada é instalado. Um processo por lote de áudios (`bin/motor-transcricao.py`, que carrega o modelo uma vez e
grava um .txt por arquivo — o CLI `mlx_whisper` com N arquivos grava tudo no nome do primeiro), com `nice`, tetos do
config (`[transcricao]`) e o texto guardado no SQLite (`transcricoes`), apagado pela limpeza semanal junto com o cru (R-12).
Quem chama: bin/coletor.py (dentro do ciclo, antes de montar o lote), bin/transcrever.py (áudios atrasados) e os testes.
O que não faz: não decide classe, não loga texto (só ids, contagens, segundos e milissegundos), nunca roda dois motores
ao mesmo tempo, não baixa modelo (motor ou modelo ausente = o áudio segue como marcador `[áudio m:ss]`, como antes).
"""
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from app import lotes
from app.erros import EvolutionIndisponivel, NaoAutorizado, VigiaErro

log = logging.getLogger("vigia.transcricao")

PADROES = {
    "ativo": True,
    "grupos": True,              # transcreve áudio de grupo mesmo que a IA não leia o grupo (o painel mostra o texto)
    "teto_ciclo": 20,            # áudios por ciclo
    "teto_segundos_ciclo": 900,  # soma de áudio ouvido (s) por ciclo; o resto espera o próximo ciclo
    "max_segundos_audio": 600,   # áudio mais longo: só os primeiros N s (estado "cortado")
    "min_segundos": 2,           # mais curto que isso não vale o motor (estado "pulado")
    "timeout_s": 600,            # por processo do motor
    "tentativas": 3,             # falhas por áudio antes de desistir
    "modelo": "mlx-community/whisper-large-v3-turbo",
    "idioma": "pt",
    "nice": 15,
}
MOTOR = "mlx_whisper"
RUNNER = lotes.RAIZ / "bin" / "motor-transcricao.py"   # roda no interpretador que tem o mlx_whisper (achar_motor)
PYTHON_PADRAO = "/opt/homebrew/bin/python3"
TEXTO_MAX = 20000
ENV_PERMITIDO = ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "VIGIA_WHISPER_CPU_MODELO")
TIPOS_AUDIO = ("audioMessage", "pttMessage")
_CONTAGENS = ("audios", "transcritos", "cortados", "falhas", "pulados", "adiados", "segundos", "motor_ms")


class MotorFalhou(VigiaErro):
    """O motor não executou, saiu com erro ou estourou o tempo; os áudios daquele processo ficam como `falhou`."""


def achar_motor() -> str | None:
    """Interpretador Python que tem o mlx_whisper (é nele que `bin/motor-transcricao.py` roda): VIGIA_WHISPER, ou o da
    linha `#!` do CLI `mlx_whisper` achado no PATH ampliado (o LaunchAgent não herda /opt/homebrew/bin); CLI sem `#!`
    legível = PYTHON_PADRAO. Sem CLI instalado = None (sem transcrição)."""
    forcado = os.environ.get("VIGIA_WHISPER")
    if forcado:
        return forcado
    caminho = os.pathsep.join([os.environ.get("PATH", ""), "/opt/homebrew/bin", "/usr/local/bin"])
    cli = shutil.which(MOTOR, path=caminho)
    if not cli:
        return None
    try:
        with open(cli, "rb") as f:
            primeira = f.readline(300).decode("utf-8", "replace").strip()
    except OSError:
        primeira = ""
    if primeira.startswith("#!"):
        partes = primeira[2:].split()
        interp = (shutil.which(partes[1]) if partes and partes[0].endswith("/env") and len(partes) > 1
                  else (partes[0] if partes else None))
        if interp and os.access(interp, os.X_OK):
            return interp
    return PYTHON_PADRAO


def eh_audio(linha: dict) -> bool:
    return lotes.descrever(linha)["tipo"] == "audio"


def segundos_de(linha: dict) -> int:
    m = lotes._desembrulhar(linha.get("message") or {})
    a = m.get("audioMessage") if isinstance(m, dict) else None
    return max(0, lotes._int((a or {}).get("seconds")))


def candidatos(linhas, *, desde: int, bloqueados=frozenset(), grupos: bool = True,
               existentes: dict | None = None, tentativas: int = 3) -> list[dict]:
    """Áudios que ainda precisam do motor: dentro da janela de leitura (R-2/D-53), fora de status/broadcast/newsletter, não bloqueados
    (D-41, pelo jid ou pelo par em número), grupo só se `grupos`; sem linha em `transcricoes`, ou `falhou` com menos de
    `tentativas`. Ordem de chegada; o mesmo id entra uma vez."""
    existentes = existentes or {}
    saida, vistos = [], set()
    for ln in sorted(linhas, key=lambda x: (lotes._int(x.get("messageTimestamp")), lotes._int(x.get("seq")))):
        chave = ln.get("key") or {}
        jid = chave.get("remoteJid") or ""
        if not jid or jid.endswith(("@broadcast", "@newsletter")) or not ln.get("id") or ln["id"] in vistos:
            continue
        if jid in bloqueados or (chave.get("remoteJidAlt") or "") in bloqueados:
            continue
        if lotes._ts_segundos(lotes._int(ln.get("messageTimestamp"))) < int(desde):
            continue
        if jid.endswith("@g.us") and not grupos:
            continue
        if not eh_audio(ln):
            continue
        t = existentes.get(ln["id"])
        if t and (t.get("estado") != "falhou" or lotes._int(t.get("tentativas"), 1) >= int(tentativas)):
            continue
        vistos.add(ln["id"])
        saida.append(ln)
    return saida


def planejar(cands, *, teto_ciclo: int, teto_segundos_ciclo: int, min_segundos: int, max_segundos_audio: int) -> dict:
    """Divide em `pulados` (curtos demais), `fazer` (cabem nos tetos; cada item com `segundos` e `clip` quando passa do
    máximo) e `adiados` (não couberam: ficam para o próximo ciclo). Os tetos contam o que o motor vai OUVIR (o corte,
    não a duração); o primeiro áudio sempre cabe."""
    pulados, fazer, adiados = [], [], []
    soma = 0
    for ln in cands:
        seg = segundos_de(ln)
        if seg < int(min_segundos):
            pulados.append(ln)
            continue
        ouvir = min(seg, int(max_segundos_audio))
        if len(fazer) >= int(teto_ciclo) or (fazer and soma + ouvir > int(teto_segundos_ciclo)):
            adiados.append(ln)
            continue
        soma += ouvir
        fazer.append({"linha": ln, "segundos": seg, "clip": ouvir if seg > int(max_segundos_audio) else None})
    return {"pulados": pulados, "fazer": fazer, "adiados": adiados, "segundos": soma}


def comando(motor: str, arquivos, *, modelo: str, idioma: str, saida: Path, clip_s: int | None = None,
            nice: int = 15) -> list[str]:
    """Um processo para N arquivos: `nice -n 15 <python> bin/motor-transcricao.py a.ogg b.ogg --model … --language pt
    --output-dir … [--clip-s N]`. Em argv só caminhos (nome = id da mensagem) e opções: nunca texto nem jid (D-43)."""
    cmd = ["/usr/bin/nice", "-n", str(int(nice)), str(motor), str(RUNNER), *[str(a) for a in arquivos],
           "--model", str(modelo), "--language", str(idioma), "--output-dir", str(saida)]
    if clip_s:
        cmd += ["--clip-s", str(int(clip_s))]
    return cmd


def ambiente() -> dict:
    """Ambiente do motor construído do zero: lista branca + PATH com o Homebrew + modo offline do Hugging Face
    (modelo só do cache local; nada sai do Mac). Sem CLAUDE*, ANTHROPIC*, tokens ou o resto do ambiente."""
    env = {k: os.environ[k] for k in ENV_PERMITIDO if k in os.environ}
    env["PATH"] = os.pathsep.join([os.environ.get("PATH") or "/usr/bin:/bin", "/opt/homebrew/bin", "/usr/local/bin"])
    env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1", TOKENIZERS_PARALLELISM="false")
    return env


def _texto_limpo(txt) -> str:
    return " ".join(str(txt or "").split())[:TEXTO_MAX]


def rodar_motor(motor: str, arquivos, *, modelo: str, idioma: str, saida: Path, timeout: float, executor=None,
                clip_s: int | None = None, nice: int = 15) -> dict:
    """Roda UM processo sobre `arquivos` e devolve {stem: texto} lendo `<saida>/<stem>.txt` (arquivo ausente = sem
    entrada). Processo em sessão própria, morto por grupo no timeout; qualquer falha = MotorFalhou. A saída do motor
    (stdout/stderr) nunca é logada: pode conter o texto."""
    cmd = comando(motor, arquivos, modelo=modelo, idioma=idioma, saida=saida, clip_s=clip_s, nice=nice)
    try:
        proc = lotes.executar_em_grupo(executor, cmd, capture_output=True, text=True, timeout=timeout,
                                       stdin=subprocess.DEVNULL, cwd=str(saida), env=ambiente())
    except subprocess.TimeoutExpired:
        raise MotorFalhou(f"motor excedeu {int(timeout)} s") from None
    except OSError as e:
        raise MotorFalhou(f"motor não executou ({type(e).__name__})") from None
    codigo = getattr(proc, "returncode", 1)
    if codigo != 0:
        raise MotorFalhou(f"motor saiu com código {codigo}")
    textos = {}
    for a in arquivos:
        stem = Path(a).stem
        try:
            textos[stem] = _texto_limpo((Path(saida) / f"{stem}.txt").read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return textos


class Transcritor:
    """Peça injetável do ciclo: `rodar(linhas, …)` escolhe, baixa, transcreve e grava. `motor=None` = procurar no PATH;
    `ativo()` é False quando desligado no config ou sem motor (o ciclo segue sem transcrição)."""

    def __init__(self, cfg: dict | None, pasta_tmp: Path, *, motor: str | None = None, executor=None, agora=time.time):
        self.cfg = {**PADROES, **(cfg or {})}
        self.pasta = Path(pasta_tmp)
        self.motor = motor if motor is not None else (achar_motor() if self.cfg.get("ativo") else None)
        self.executor = executor
        self.agora = agora
        self.rotulo = f"{MOTOR}/{str(self.cfg['modelo']).rsplit('/', 1)[-1]}"

    def ativo(self) -> bool:
        return bool(self.cfg.get("ativo")) and bool(self.motor)

    def _preparar_pasta(self) -> None:
        self.pasta.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._limpar_pasta()   # resto de uma execução que morreu no meio

    def _limpar_pasta(self) -> None:
        try:
            for p in self.pasta.iterdir():
                if p.is_file():
                    p.unlink()
        except OSError:
            log.warning("pasta temporária do áudio não pôde ser limpa")

    def _gravar(self, banco, ln: dict, *, estado: str, segundos: int, lidos: int, texto: str = "", tempo_ms=None) -> None:
        banco.gravar_transcricao(msg_id=ln["id"], conversa=lotes.conversa_de(ln),
                                 ts=lotes._ts_segundos(lotes._int(ln.get("messageTimestamp"))), estado=estado, texto=texto,
                                 segundos=segundos, segundos_lidos=lidos, motor=self.rotulo, tempo_ms=tempo_ms)

    def planejar(self, linhas, *, banco, desde: int, bloqueados=frozenset()) -> dict | None:
        """Candidatos + plano (None = nenhum áudio a fazer). Lê `transcricoes` para pular o que já foi feito."""
        audios = [ln for ln in linhas if ln.get("id") and eh_audio(ln)]
        if not audios:
            return None
        existentes = banco.transcricoes([ln["id"] for ln in audios])
        cands = candidatos(audios, desde=desde, bloqueados=bloqueados, grupos=bool(self.cfg["grupos"]),
                           existentes=existentes, tentativas=int(self.cfg["tentativas"]))
        if not cands:
            return None
        plano = planejar(cands, teto_ciclo=int(self.cfg["teto_ciclo"]),
                         teto_segundos_ciclo=int(self.cfg["teto_segundos_ciclo"]),
                         min_segundos=int(self.cfg["min_segundos"]), max_segundos_audio=int(self.cfg["max_segundos_audio"]))
        plano["candidatos"] = cands
        return plano

    def rodar(self, linhas, *, obter_banco, obter_evo, desde: int, bloqueados=frozenset()) -> dict:
        """Transcreve os áudios de `linhas` que ainda faltam, dentro dos tetos. Devolve só contagens:
        {audios, transcritos, cortados, falhas, pulados, adiados, segundos, motor_ms}. Banco e Evolution só são
        criados quando há áudio a tratar. Evolution fora ou credencial recusada SOBEM (o ciclo vira "não rodou");
        motor falhando ou mídia inválida viram `falhou` (tentado de novo até `tentativas`) e o ciclo segue."""
        r = dict.fromkeys(_CONTAGENS, 0)
        if not self.ativo() or not any(ln.get("id") and eh_audio(ln) for ln in linhas):
            return r
        banco = obter_banco()
        plano = self.planejar(linhas, banco=banco, desde=desde, bloqueados=bloqueados)
        if plano is None:
            return r
        r.update(audios=len(plano["candidatos"]), pulados=len(plano["pulados"]), adiados=len(plano["adiados"]),
                 segundos=plano["segundos"])
        for ln in plano["pulados"]:
            self._gravar(banco, ln, estado="pulado", segundos=segundos_de(ln), lidos=0)
        if not plano["fazer"]:
            return r
        evo = obter_evo()
        self._preparar_pasta()
        try:
            baixados = []
            for item in plano["fazer"]:
                ln = item["linha"]
                try:
                    dados, ext = evo.midia_audio(ln["key"])
                except NaoAutorizado:
                    raise
                except EvolutionIndisponivel as e:
                    if not lotes.evolution_responde(evo):     # pedido 85: só para a leitura se a Evolution caiu
                        raise
                    log.warning("áudio não baixado, a leitura segue (%s)", e)
                    self._gravar(banco, ln, estado="falhou", segundos=item["segundos"], lidos=0)
                    r["falhas"] += 1
                    continue
                except VigiaErro:
                    self._gravar(banco, ln, estado="falhou", segundos=item["segundos"], lidos=0)
                    r["falhas"] += 1
                    continue
                caminho = self.pasta / f"{lotes._nome_seguro(ln['id'], 'x')}.{ext}"
                caminho.write_bytes(dados)
                os.chmod(caminho, 0o600)
                baixados.append((item, caminho))
            # Um processo para os normais; cada longo (com corte) em processo próprio. Nunca dois ao mesmo tempo.
            processos = [[b for b in baixados if b[0]["clip"] is None]] + [[b] for b in baixados if b[0]["clip"]]
            for grupo in processos:
                if not grupo:
                    continue
                self._processar(banco, grupo, r)
        finally:
            self._limpar_pasta()
        log.info("transcrição: %d candidato(s), %d ok (%d cortado(s)), %d falha(s), %d pulado(s), %d adiado(s), "
                 "%d s de áudio, motor %d ms", r["audios"], r["transcritos"], r["cortados"], r["falhas"], r["pulados"],
                 r["adiados"], r["segundos"], r["motor_ms"])
        return r

    def _processar(self, banco, grupo, r: dict) -> None:
        clip = grupo[0][0]["clip"]
        t0 = self.agora()
        try:
            textos = rodar_motor(self.motor, [c for _, c in grupo], modelo=str(self.cfg["modelo"]),
                                 idioma=str(self.cfg["idioma"]), saida=self.pasta, timeout=float(self.cfg["timeout_s"]),
                                 executor=self.executor, clip_s=clip, nice=int(self.cfg["nice"]))
        except MotorFalhou as e:
            log.warning("motor falhou em %d áudio(s): %s", len(grupo), e)
            textos = None
        ms = int((self.agora() - t0) * 1000)
        r["motor_ms"] += ms
        total_s = sum((it["clip"] or it["segundos"]) for it, _ in grupo) or 1
        for item, caminho in grupo:
            ln = item["linha"]
            lidos = item["clip"] or item["segundos"]
            rateio = int(ms * lidos / total_s)
            texto = (textos or {}).get(caminho.stem)
            if texto is None:
                self._gravar(banco, ln, estado="falhou", segundos=item["segundos"], lidos=0, tempo_ms=rateio)
                r["falhas"] += 1
                continue
            estado = "cortado" if item["clip"] else "ok"
            self._gravar(banco, ln, estado=estado, segundos=item["segundos"], lidos=lidos, texto=texto, tempo_ms=rateio)
            r["transcritos"] += 1
            r["cortados"] += estado == "cortado"
