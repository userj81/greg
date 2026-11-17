#!/usr/bin/env python3
"""Motor de transcrição do Greg (pedido 30, D-47): roda o mlx_whisper em UM processo sobre N arquivos e grava um
`<stem>.txt` por arquivo em --output-dir. Existe porque o CLI `mlx_whisper` 0.4.3 com vários arquivos grava todos no nome
do PRIMEIRO (`output_name = output_name or stem` dentro do laço de cli.py; visto na rodada real de 08/11/2026) e porque
carregar o modelo uma vez por rodada custa ~3 s em vez de ~3 s por áudio (ModelHolder guarda o modelo no processo).
Quem chama: app/transcricao.py, por subprocesso com `nice` e ambiente offline (HF_HUB_OFFLINE=1). Não é parte do painel.
O que não faz: nunca imprime o texto (stdout só com contagens; um arquivo com erro vai para stderr só com o tipo da
exceção e o seguinte continua). Saída: 0 rodou · 2 nenhum motor importável neste interpretador.
Motores, nesta ordem: mlx_whisper (Mac com Apple Silicon, GPU) e faster_whisper (qualquer CPU: Linux, Docker, Intel). No
faster_whisper o modelo vem de VIGIA_WHISPER_CPU_MODELO (padrão "small"), lido só do cache local, como no MLX.
"""
import argparse
import sys
from pathlib import Path


def _argumentos(argv):
    p = argparse.ArgumentParser(prog="motor-transcricao.py", description="mlx_whisper sobre N arquivos, um .txt por arquivo")
    p.add_argument("arquivos", nargs="+")
    p.add_argument("--model", required=True, help="repositório HF ou pasta do modelo (lido só do cache local)")
    p.add_argument("--language", default="pt")
    p.add_argument("--output-dir", required=True)
    p.add_argument("--clip-s", type=int, default=0, help="só os primeiros N segundos de cada arquivo (0 = inteiro)")
    return p.parse_args(argv)


def _motor(a):
    """Função arquivo -> texto do primeiro motor importável; None (com o motivo no stderr) se não houver nenhum."""
    try:
        import mlx_whisper
    except ImportError:
        mlx_whisper = None
    if mlx_whisper is not None:
        clip = [0.0, float(a.clip_s)] if a.clip_s > 0 else "0"

        def com_mlx(arq):
            r = mlx_whisper.transcribe(arq, path_or_hf_repo=a.model, language=a.language, verbose=None,
                                       condition_on_previous_text=False, clip_timestamps=clip)
            return r.get("text")
        return com_mlx
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        print(f"motor: nem mlx_whisper nem faster_whisper importáveis ({type(e).__name__})", file=sys.stderr)
        return None
    import os
    modelo = WhisperModel(os.environ.get("VIGIA_WHISPER_CPU_MODELO") or "small", device="cpu", compute_type="int8")

    def com_cpu(arq):
        partes, _ = modelo.transcribe(arq, language=a.language, condition_on_previous_text=False,
                                      clip_timestamps=[0.0, float(a.clip_s)] if a.clip_s > 0 else "0")
        return " ".join(p.text.strip() for p in partes)
    return com_cpu


def main(argv=None) -> int:
    a = _argumentos(argv)
    transcrever = _motor(a)
    if transcrever is None:
        return 2
    saida = Path(a.output_dir)
    saida.mkdir(parents=True, exist_ok=True)
    feitos = pulados = 0
    for arq in a.arquivos:
        try:
            texto = transcrever(arq)
            (saida / f"{Path(arq).stem}.txt").write_text(str(texto or "").strip() + "\n", encoding="utf-8")
            feitos += 1
        except Exception as e:  # um arquivo ruim não derruba a rodada; o conteúdo nunca vai para o stderr
            print(f"motor: pulado {Path(arq).name} ({type(e).__name__})", file=sys.stderr)
            pulados += 1
    print(f"motor: {feitos} ok, {pulados} pulado(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
