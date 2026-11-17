#!/opt/homebrew/bin/python3
"""Histórico de análise do Greg (D-58, pedido 46): o que cada conversa trouxe e em que pé ficou.
Responde "o que aconteceu essa semana?", "quem está falando comigo?" e "ficou alguém sem resposta?".
Uso: historico.py [--dias N] [--pendentes] [--conversa NOME] [--json | --contagem]
  --dias N        janela, 1 a 90 (padrão 7)
  --pendentes     só quem aguarda resposta (sua ou do contato), sem o dia a dia
  --conversa NOME só as conversas cujo contato contém NOME
  --json          a mesma leitura em JSON (uma linha)
  --contagem      só números (nenhum nome, nenhum resumo)
Saída: 0 ok · 1 falhou · 2 não rodou (sem banco).
Quem chama: o dono (bin/vigia.sh historico) e o Claude, quando o dono pergunta o que aconteceu.
O que não faz: não lê mensagem crua, não chama Evolution, Frentes nem Claude, não escreve no banco (só aplica migração
pendente ao abrir), não grava nada em log.
"""
import argparse
import json
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from app import analises  # noqa: E402


def _argumentos(argv):
    p = argparse.ArgumentParser(description="Histórico de análise das conversas (D-58).")
    p.add_argument("--dias", type=int, default=analises.DIAS_PADRAO, help="janela em dias, 1 a 90 (padrão 7)")
    p.add_argument("--pendentes", action="store_true", help="só quem aguarda resposta, sem o dia a dia")
    p.add_argument("--conversa", default=None, help="só as conversas cujo contato contém este texto")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--json", action="store_true", help="saída em JSON (uma linha)")
    g.add_argument("--contagem", action="store_true", help="só números: nenhum nome, nenhum resumo")
    return p.parse_args(argv)


def main(argv=None) -> int:
    os.umask(0o077)
    args = _argumentos(argv)
    dados = Path(os.environ.get("VIGIA_DADOS") or RAIZ / "dados")
    caminho = dados / "vigia.db"
    if not caminho.is_file():
        print("historico: não rodou (vigia.db não existe)", file=sys.stderr)
        return 2
    try:
        from app.banco import Banco
        h = analises.montar(Banco(str(caminho)), dias=args.dias, conversa=args.conversa)
    except Exception as e:  # nunca imprime a mensagem: pode carregar trecho de conversa
        print(f"historico: falhou ({type(e).__name__})", file=sys.stderr)
        return 1
    if args.contagem:
        print(json.dumps({"periodo": h["periodo"], "analises": len(h["analises"]), "conversas": h["conversas"],
                          "aguardando_dono": len(h["aguardando_dono"]), "aguardando_contato": len(h["aguardando_contato"]),
                          "cartoes": h["cartoes"]}, ensure_ascii=False))
    elif args.json:
        print(json.dumps(h, ensure_ascii=False))
    else:
        sys.stdout.write(analises.texto(h, so_pendentes=args.pendentes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
