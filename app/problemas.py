"""Catálogo de problemas do Greg (pedido 63): a ÚNICA fonte do texto que a pessoa lê quando uma peça para.
Quem chama: app/alertas.py (central de avisos); depois a faixa das telas, a notificação, a tela de saúde, a página de
erro e docs/PROBLEMAS.md.
O que não faz: não detecta nada nem guarda estado; texto fixo, sem dado de conversa. Passo a passo só com comando que
existe neste repositório ou botão que existe no painel.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

# Peças, na ordem em que a tela de saúde lista
PECAS = ("docker", "evolution", "whatsapp", "banco", "ia", "agendador", "transcricao", "fechamento", "envio")


@dataclass(frozen=True)
class Problema:
    codigo: str            # estável: vai para o log, para dados/alertas.json e para docs/PROBLEMAS.md
    peca: str              # uma de PECAS
    titulo: str            # o que parou, em uma linha (título da notificação e da faixa)
    por_que: str           # a causa, em uma frase
    passos: tuple          # o que fazer, em ordem
    imediato: bool = False  # True: só a pessoa resolve, avisa na primeira falha. False: avisa na segunda seguida (D-66)
    link: str = ""         # tela do painel que resolve, quando existe


_P = Problema
CATALOGO = {p.codigo: p for p in (
    _P("ia-login", "ia", "O Greg parou de ler as conversas",
       "O login do Claude neste Mac expirou.",
       ("Abra o Terminal.", "Rode: claude auth login", "Volte ao Greg e use Ler agora na tela Conexão."),
       imediato=True, link="/conexao"),
    _P("ia-limite", "ia", "O Greg parou de ler as conversas",
       "A conta do Claude atingiu o limite de uso.",
       ("Espere o limite da conta renovar.", "Depois, use Ler agora na tela Conexão. As mensagens deste período não se perdem."),
       imediato=True, link="/conexao"),
    _P("ia-ausente", "ia", "O Greg não encontrou o Claude neste Mac",
       "O programa claude não está instalado ou não está no caminho do sistema.",
       ("Instale o Claude Code.", "No Terminal, rode: claude auth login", "Volte ao Greg e use Ler agora na tela Conexão."),
       imediato=True, link="/conexao"),
    _P("ia-erro", "ia", "A leitura das conversas está falhando",
       "O Claude respondeu com erro ou não respondeu no tempo.",
       ("Use Ler agora na tela Conexão.", "Se repetir, rode no Terminal: bin/vigia.sh logs"),
       link="/conexao"),
    # Pedido 68: a IA é outra (motor por API, escolhida em Ajustes). Texto sem nome de provedor: o nome vai no detalhe.
    _P("ia-chave", "ia", "O Greg parou de ler: a chave da IA foi recusada",
       "O provedor de IA escolhido em Ajustes recusou a chave guardada.",
       ("Abra Ajustes, seção IA.", "Cole uma chave válida do provedor e use Testar.", "Depois, use Ler agora na tela Conexão."),
       imediato=True, link="/ajustes"),
    _P("ia-saldo", "ia", "O Greg parou de ler: a conta da IA está sem saldo",
       "O provedor de IA escolhido em Ajustes respondeu que a conta não tem saldo.",
       ("Ponha saldo na conta do provedor.", "Depois, use Ler agora na tela Conexão. As mensagens deste período não se perdem."),
       imediato=True, link="/ajustes"),
    _P("ia-api-limite", "ia", "A IA escolhida está limitando o uso",
       "O provedor de IA escolhido em Ajustes respondeu que o limite de pedidos foi atingido.",
       ("Espere alguns minutos: o Greg tenta de novo no próximo ciclo.", "Se repetir, veja o limite da sua conta no provedor."),
       link="/ajustes"),
    _P("ia-teto", "ia", "O Greg parou de ler: teto de gasto do mês atingido",
       "O gasto deste mês com a IA chegou ao teto definido em Ajustes.",
       ("Para voltar a ler, aumente o teto em Ajustes, seção IA, e use Ler agora na tela Conexão.",
        "Sem mudar o teto, a leitura volta sozinha na virada do mês. Nada é gasto até lá e as mensagens não se perdem."),
       imediato=True, link="/ajustes"),
    _P("ia-api-fora", "ia", "A IA escolhida não respondeu",
       "O endereço da IA definido em Ajustes está fora do ar ou não foi alcançado.",
       ("Se a IA roda neste Mac, abra o programa dela (Ollama, LM Studio).", "Abra Ajustes, seção IA, e use Testar.",
        "Depois, use Ler agora na tela Conexão."),
       link="/ajustes"),
    _P("ia-api-erro", "ia", "A leitura pela IA escolhida está falhando",
       "A IA definida em Ajustes respondeu com erro, demorou demais ou não terminou a leitura.",
       ("Abra Ajustes, seção IA, e use Testar.", "Se repetir, rode no Terminal: bin/vigia.sh logs"),
       link="/ajustes"),
    _P("leitura-parada", "agendador", "O Greg não lê as conversas há mais de uma hora",
       "Nenhuma leitura terminou bem nos últimos 75 minutos, por um motivo que o Greg não identificou.",
       ("Use Ler agora na tela Conexão.", "Se não resolver, rode no Terminal: bin/vigia.sh status"),
       imediato=True, link="/conexao"),
    _P("agendador-parado", "agendador", "As leituras automáticas estão desligadas",
       "O agendador do Greg não está rodando neste Mac.",
       ("Rode no Terminal: bin/vigia.sh status", "Para religar: bin/instalar-launchagent.sh --sim"),
       imediato=True),
    _P("docker-parado", "docker", "O Docker está parado",
       "O Greg depende do Docker para falar com o WhatsApp, e ele não está rodando.",
       ("Abra o Docker e espere ele ficar pronto.", "Depois, use Ler agora na tela Conexão."),
       imediato=True, link="/conexao"),
    _P("evolution-fora", "evolution", "A ponte com o WhatsApp não respondeu",
       "A Evolution, que liga o Greg ao WhatsApp, está fora do ar ou reiniciando.",
       ("Abra a tela Conexão.", "Use Reiniciar Evolution.", "Se não voltar em alguns minutos, rode no Terminal: bin/vigia.sh status"),
       link="/conexao"),
    _P("whatsapp-caiu", "whatsapp", "O WhatsApp desconectou do Greg",
       "O celular desfez o pareamento ou ficou tempo demais sem internet.",
       ("Abra a tela Conexão.", "Use Gerar QR e leia o código com o WhatsApp do celular."),
       imediato=True, link="/conexao"),
    _P("whatsapp-sem-resposta", "whatsapp", "O teste da conexão do WhatsApp não teve resposta",
       "O Greg perguntou o estado da conexão e a Evolution não respondeu.",
       ("Abra a tela Conexão e confira o estado.", "Se estiver parado, use Reiniciar Evolution."),
       link="/conexao"),
    _P("nao-pareado", "whatsapp", "O WhatsApp ainda não está ligado ao Greg",
       "Este Mac ainda não foi pareado com o WhatsApp do celular.",
       ("Abra a tela Conexão.", "Use Gerar QR e leia o código com o WhatsApp do celular."),
       imediato=True, link="/conexao"),
    _P("banco-fora", "banco", "O banco das mensagens não respondeu",
       "O banco de dados da Evolution, onde as mensagens ficam, não respondeu.",
       ("Abra a tela Conexão.", "Use Reiniciar Evolution.", "Se repetir, rode no Terminal: bin/vigia.sh status"),
       link="/conexao"),
    _P("transcricao-erro", "transcricao", "A transcrição de áudio está falhando",
       "O motor de transcrição não rodou ou terminou com erro.",
       ("Os áudios continuam chegando e tocando; só o texto deles não aparece.", "Rode no Terminal: bin/vigia.sh logs"),
       ),
    _P("fechamento-dia-erro", "fechamento", "O fechamento do dia falhou",
       "A consolidação diária das demandas terminou com erro.",
       ("As demandas de hoje continuam na tela Demandas.", "O Greg tenta de novo no próximo ciclo.", "Se repetir amanhã, rode no Terminal: bin/vigia.sh logs"),
       link="/demandas"),
    _P("fechamento-semana-erro", "fechamento", "O fechamento da semana falhou",
       "O relatório ou a limpeza semanal terminou com erro. Nada é apagado sem o backup conferido.",
       ("Abra a tela Conexão e veja o aviso da semana.", "Rode no Terminal: bin/vigia.sh logs"),
       link="/conexao"),
    _P("agendado-nao-saiu", "envio", "Um envio agendado não saiu",
       "O Greg não conseguiu mandar na hora marcada: o computador estava desligado, o WhatsApp estava desconectado ou "
       "a conversa está fora do Greg.",
       ("Abra a tela Envios e veja qual não saiu e o motivo.", "Se ainda fizer sentido, agende de novo ou mande pela conversa."),
       imediato=True, link="/envios"),
    _P("agendado-incerto", "envio", "Um envio agendado pode ter saído",
       "A ponte com o WhatsApp não confirmou se a mensagem agendada foi entregue.",
       ("Confira no celular se a mensagem chegou.", "Não mande de novo antes de conferir.", "Abra a tela Envios para ver qual foi."),
       imediato=True, link="/envios"),
)}


def problema(codigo: str) -> Problema | None:
    return CATALOGO.get(codigo)


MOTIVO_DESCONHECIDO = "desconhecido"


def leitura_parada(motivo: str = "") -> Problema:
    """Pedido 85: "leitura-parada" com o motivo da última tentativa que falhou (código do catálogo que o coletor
    registrou). Com motivo conhecido, diz a causa e os passos daquele problema; erro sem código, diz que houve erro e
    onde ver; sem tentativa registrada, o texto do catálogo."""
    base = CATALOGO["leitura-parada"]
    if motivo == MOTIVO_DESCONHECIDO:
        return replace(base, por_que="Nenhuma leitura terminou bem nos últimos 75 minutos. A última tentativa terminou "
                                     "com um erro que o Greg não reconhece.",
                       passos=("Use Ler agora na tela Conexão.", "Se repetir, rode no Terminal: bin/vigia.sh logs"))
    m = CATALOGO.get(motivo)
    if m is None or m.codigo == base.codigo:
        return base
    causa = m.por_que[:1].lower() + m.por_que[1:]
    return replace(base, por_que=f"Nenhuma leitura terminou bem nos últimos 75 minutos. Na última tentativa, {causa}",
                   passos=m.passos, link=m.link or base.link)


ROTULO_PECA_DOC = {"docker": "Docker", "evolution": "Ponte com o WhatsApp (Evolution)", "whatsapp": "WhatsApp",
                   "banco": "Banco de mensagens", "ia": "Leitura pela IA", "agendador": "Leituras automáticas",
                   "transcricao": "Transcrição de áudio", "fechamento": "Fechamento do dia e da semana", "envio": "Envios agendados"}


def markdown() -> str:
    """docs/PROBLEMAS.md inteiro, gerado do CATALOGO. Regerar: `python3 -m app.problemas > docs/PROBLEMAS.md`.
    O teste test_lote4 falha se o arquivo divergir do catálogo."""
    out = ["# Problemas do Greg e como resolver", "",
           "Gerado de `app/problemas.py` (`python3 -m app.problemas > docs/PROBLEMAS.md`). Não editar à mão.", "",
           "Quando uma peça para, o Greg mostra o aviso numa faixa em todas as telas, manda uma notificação do Mac e "
           "lista o estado de cada peça em **Saúde do Greg** (`/avisos`, no menu da tela Conexão). O código entre "
           "crases é o que aparece no log e em `dados/alertas.json`.", ""]
    for peca in PECAS:
        itens = [p for p in CATALOGO.values() if p.peca == peca]
        if not itens:
            continue
        out += [f"## {ROTULO_PECA_DOC.get(peca, peca)}", ""]
        for p in itens:
            out += [f"### {p.titulo} (`{p.codigo}`)", "", f"**Por quê:** {p.por_que}", "", "**O que fazer:**", ""]
            out += [f"{i}. {passo}" for i, passo in enumerate(p.passos, 1)]
            quando = "na primeira falha" if p.imediato else "na segunda falha seguida"
            out += ["", f"Avisa {quando}." + (f" Tela que resolve: `{p.link}`." if p.link else ""), ""]
    return "\n".join(out).rstrip("\n") + "\n"


if __name__ == "__main__":
    import sys
    sys.stdout.write(markdown())
