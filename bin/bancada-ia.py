#!/opt/homebrew/bin/python3
"""Bancada de qualidade das outras IAs (pedido 68). Mede um modelo candidato com lotes FICTÍCIOS, em modo sombra
(nada é gravado no vigia.db nem no Frentes), e aplica a régua: classe certa em pelo menos 90% dos casos e nenhum caso
de atenção perdido. Detalhes em app/bancada.py.

Uso:
  bin/bancada-ia.py gerar                         só escreve os lotes fictícios e o gabarito (não fala com IA nenhuma)
  bin/bancada-ia.py rodar --teto-usd 2.00         roda contra a IA configurada em Ajustes (seção IA) ou em [ia]
  bin/bancada-ia.py rodar --teto-usd 2.00 --modelo <nome> [--endereco <base>]    troca o modelo só nesta rodada
  bin/bancada-ia.py rodar --casos atencao-site,cancela-reuniao --teto-usd 0.50   só alguns casos

`--teto-usd` é obrigatório quando o endereço não é deste Mac: é o gasto máximo desta rodada, contado à parte do teto
mensal da leitura. Endereço local (Ollama, LM Studio) não gasta e dispensa o teto. A chave é a guardada por Ajustes;
este comando nunca recebe nem imprime chave.
Saída: lotes, gabarito e relatório (Markdown e JSON) em <dados>/bancada/. Código: 0 aprovado · 1 reprovado · 2 não rodou.
"""
import argparse
import json
import sys
import time
import tomllib
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from app import bancada, leitor, leitor_api  # noqa: E402
from app.conexao import dados_dir  # noqa: E402


def _config() -> dict:
    try:
        return tomllib.loads((RAIZ / "config.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bin/bancada-ia.py", description="Bancada de qualidade das outras IAs (lotes fictícios, modo sombra).")
    sub = ap.add_subparsers(dest="cmd")
    g = sub.add_parser("gerar", help="escreve os lotes fictícios e o gabarito")
    r = sub.add_parser("rodar", help="roda o candidato em modo sombra e aplica a régua")
    for p in (g, r):
        p.add_argument("--saida", default=None, help="pasta de saída (padrão: <dados>/bancada)")
    r.add_argument("--teto-usd", type=float, default=None, help="gasto máximo desta rodada, em dólares")
    r.add_argument("--modelo", default=None, help="modelo só para esta rodada (padrão: o de Ajustes)")
    r.add_argument("--endereco", default=None, help="endereço só para esta rodada (padrão: o de Ajustes)")
    r.add_argument("--casos", default=None, help="ids separados por vírgula (padrão: todos)")
    r.add_argument("--sem-visao", action="store_true", help="não envia imagem (pula os casos que dependem dela)")
    args = ap.parse_args(argv)
    if args.cmd is None:
        ap.print_help()
        return 2
    config = _config()
    dados = dados_dir(RAIZ, config)
    saida = Path(args.saida) if args.saida else dados / "bancada"

    if args.cmd == "gerar":
        pastas = bancada.gerar(saida)
        feitos = sum(1 for p in pastas.values() if p is not None)
        print(f"{feitos} lote(s) fictício(s) em {saida / 'lotes'}; gabarito em {saida / 'gabarito.json'}")
        if feitos < len(pastas):
            print(f"{len(pastas) - feitos} caso(s) de imagem pulado(s): ImageMagick (magick) ou fonte do sistema não encontrados")
        return 0

    cfg = leitor.configuracao(config, dados)
    try:
        if args.endereco:
            cfg["endereco"] = leitor.validar_endereco(args.endereco)
            cfg["motor"] = "api"
        if args.modelo:
            cfg["modelo"] = leitor.validar_modelo(args.modelo)
            cfg["motor"] = "api"
    except leitor.ConfiguracaoInvalida as e:
        print(f"bancada: {e}", file=sys.stderr)
        return 2
    if cfg["motor"] != "api" or not cfg["endereco"] or not cfg["modelo"]:
        print("bancada: escolha a outra IA em Ajustes (seção IA) ou passe --endereco e --modelo. "
              "O Claude Code é a referência e não passa por aqui.", file=sys.stderr)
        return 2
    local = leitor.eh_local(cfg["endereco"])
    if not local:
        if args.teto_usd is None or args.teto_usd <= 0:
            print("bancada: o endereço não é deste Mac, então a rodada gasta. Diga o teto: --teto-usd <dólares>", file=sys.stderr)
            return 2
        cfg["teto_mes_usd"] = float(args.teto_usd)
    if args.sem_visao:
        cfg["visao"] = False
    casos = bancada.CASOS
    if args.casos:
        pedidos = [c.strip() for c in args.casos.split(",") if c.strip()]
        casos = [c for c in bancada.CASOS if c["id"] in pedidos]
        if len(casos) != len(pedidos):
            print("bancada: caso desconhecido em --casos. Ids: " + ", ".join(c["id"] for c in bancada.CASOS), file=sys.stderr)
            return 2
    chave = leitor.chave(dados)
    if not chave and not local:
        print("bancada: não há chave guardada. Cole a chave em Ajustes (seção IA) e salve.", file=sys.stderr)
        return 2
    try:
        (saida / leitor_api.ARQ_GASTO).unlink()      # o contador da bancada é por rodada
    except OSError:
        pass
    print(f"bancada: {len(casos)} caso(s), modelo {cfg['modelo']}, endereço {cfg['endereco']}"
          + ("" if local else f", teto US$ {cfg['teto_mes_usd']:.2f}"))
    placar, resultados = bancada.rodar(cfg, chave, saida, casos=casos, avisar=lambda m: print("  " + m, flush=True))
    gasto = leitor_api.gasto_do_mes(saida)["usd"]
    nome = "relatorio-" + "".join(ch if ch.isalnum() else "-" for ch in cfg["modelo"]) + time.strftime("-%Y%m%d-%H%M")
    (saida / f"{nome}.md").write_text(bancada.markdown(placar, modelo=cfg["modelo"], endereco=cfg["endereco"], gasto_usd=gasto),
                                      encoding="utf-8")
    (saida / f"{nome}.json").write_text(json.dumps({"placar": placar, "resultados": resultados, "gasto_usd": gasto,
                                                     "modelo": cfg["modelo"], "endereco": cfg["endereco"]},
                                                    ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{'APROVADO' if placar['aprovado'] else 'REPROVADO'}: {placar['certos']} de {placar['casos']} casos "
          f"({placar['acerto'] * 100:.1f}%), atenção perdida em {len(placar['atencao_perdidos'])}. "
          f"Gasto: US$ {gasto:.4f}. Relatório: {saida / (nome + '.md')}")
    if not placar["casos"]:
        return 2
    return 0 if placar["aprovado"] else 1


if __name__ == "__main__":
    sys.exit(main())
