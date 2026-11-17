"""Cursor de leitura incremental do Greg (dados/cursor.json).
Quem chama: bin/coletor.py e app/conexao.py.
O que não faz: não lê mensagens nem fala com rede; só guarda o último seq lido e o instante do pareamento.
`desde` = pareado_em − HISTORICO_S é o limite inferior de TODA leitura (coletor, painel, transcrição): D-53 (pedido 43,
09/11/2026) — 1 dia de histórico a cada pareamento; antes era o próprio pareado_em (R-2 original).
"""
import fcntl
import json
import os
import tempfile
import time
from pathlib import Path

from app.erros import CursorCorrompido

HISTORICO_S = 24 * 3600   # D-53: quanto o Greg lê para trás do pareamento (1 dia); regravado a cada pareamento novo


def _ler_arquivo(p: Path, reparar: bool = True):
    """(ultimo_seq, pareado_em) do disco; ausente -> (0, None); ilegível -> renomeia e levanta CursorCorrompido.
    Com reparar=False (leitura de saúde, sem login) não renomeia: só levanta, o arquivo fica intacto."""
    try:
        bruto = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return 0, None
    try:
        dados = json.loads(bruto)
        seq, par = dados["ultimo_seq"], dados.get("pareado_em")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise ValueError
        if par is not None and (isinstance(par, bool) or not isinstance(par, int)):
            raise ValueError
        return seq, par
    except (ValueError, KeyError, TypeError, AttributeError):
        destino = p.with_name(f"{p.name}.corrompido-{int(time.time())}")
        if not reparar:
            raise CursorCorrompido("cursor.json ilegível") from None
        try:
            os.replace(p, destino)
        except OSError:
            pass
        raise CursorCorrompido("cursor.json ilegível; arquivo preservado como " + destino.name) from None


def _ler_janela(p: Path) -> int | None:
    """`janela_desde` do disco (pedido 75): limite inferior das TELAS, guardado quando há repareamento. Ausente,
    ilegível ou de tipo errado = None (as telas voltam a usar o limite da leitura); nunca levanta."""
    try:
        v = json.loads(p.read_text(encoding="utf-8")).get("janela_desde")
    except (OSError, ValueError, AttributeError):
        return None
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


class Cursor:
    def __init__(self, caminho, ultimo_seq: int = 0, pareado_em: int | None = None):
        self.caminho = Path(caminho)
        self.ultimo_seq = int(ultimo_seq)
        self.pareado_em = pareado_em  # epoch segundos
        self.janela_desde = None      # epoch segundos; só existe depois de um repareamento (pedido 75)

    @classmethod
    def carregar(cls, caminho, reparar: bool = True) -> "Cursor":
        """reparar=False: leitura sem efeito colateral (um arquivo ilegível não é renomeado)."""
        p = Path(caminho)
        seq, par = _ler_arquivo(p, reparar)
        cur = cls(p, seq, par)
        cur.janela_desde = _ler_janela(p)
        return cur

    def salvar(self) -> None:
        """Merge sob flock (dados/cursor.lock): ultimo_seq=max(disco, memória); pareado_em do disco se memória None."""
        pai = self.caminho.parent
        pai.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(pai / "cursor.lock", "a") as trava:
            fcntl.flock(trava, fcntl.LOCK_EX)
            try:
                seq_d, par_d = _ler_arquivo(self.caminho)
                self.ultimo_seq = max(self.ultimo_seq, seq_d)
                if self.pareado_em is None:
                    self.pareado_em = par_d
                # pedido 75 (D-74): repareamento (o pareado_em do disco é mais antigo que o que vai ser gravado) não
                # encolhe o que as telas mostram. Guarda o menor limite já visto; a leitura da IA segue `desde`.
                velhos = [v for v in (self.janela_desde, _ler_janela(self.caminho)) if v is not None]
                if par_d is not None and self.pareado_em is not None and par_d < self.pareado_em:
                    velhos.append(max(0, int(par_d) - HISTORICO_S))
                self.janela_desde = min(velhos) if velhos else None
                registro = {"ultimo_seq": self.ultimo_seq, "pareado_em": self.pareado_em}
                if self.janela_desde is not None:
                    registro["janela_desde"] = self.janela_desde
                corpo = json.dumps(registro)
                fd, tmp = tempfile.mkstemp(dir=pai, prefix=self.caminho.name + ".", suffix=".tmp")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(corpo)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(tmp, self.caminho)
                except BaseException:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
            finally:
                fcntl.flock(trava, fcntl.LOCK_UN)

    def avancar(self, seq: int) -> None:
        """Só cresce: seq menor ou igual ao atual é ignorado."""
        if seq > self.ultimo_seq:
            self.ultimo_seq = int(seq)

    def definir_pareado_em(self, ts: int) -> None:
        """Marca explicitamente o instante do (novo) pareamento; salvar() grava."""
        self.pareado_em = int(ts)

    @property
    def desde(self) -> int | None:
        """Limite inferior de leitura (epoch s): pareado_em − HISTORICO_S (D-53: 1 dia de histórico no pareamento);
        None sem pareamento — e sem pareamento nada é lido (R-2)."""
        return None if self.pareado_em is None else max(0, int(self.pareado_em) - HISTORICO_S)

    @property
    def desde_tela(self) -> int | None:
        """Limite inferior do que as TELAS mostram (pedido 75, D-74): o menor entre `desde` e a janela guardada antes de
        um repareamento. Parear de novo o mesmo número não esconde as conversas que já estavam no painel. A IA NÃO usa
        isto: coletor, transcrição e fechamento seguem `desde` (D-53), para não reler histórico antigo."""
        d = self.desde
        if d is None or self.janela_desde is None:
            return d
        return min(d, int(self.janela_desde))
