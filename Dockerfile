# Greg no Docker: painel + ciclo de leitura + transcrição em CPU, num contêiner só. A Evolution, o Postgres e o Redis são
# os serviços de infra/docker-compose.yml, que o docker-compose.yml da raiz inclui.
# Construir: docker compose build   ·   sem transcrição de áudio (imagem ~850 MB menor): --build-arg TRANSCRICAO=0
# Claude Code exige Node 22+; o Debian traz o 20. Instala no estágio do Node oficial e copia só o necessário.
FROM node:22-slim AS node
ARG CLAUDE_CODE_VERSAO=2.1.322
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSAO}" && npm cache clean --force

FROM python:3.14-slim

ARG TRANSCRICAO=1
ARG WHISPER_MODELO=small

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIGIA_CONTAINER=1 \
    VIGIA_PYTHON=/usr/local/bin/python3 \
    VIGIA_WHISPER=/usr/local/bin/python3 \
    VIGIA_WHISPER_CPU_MODELO=/opt/whisper/modelo \
    TZ=America/Sao_Paulo

# psql (o Greg lê a Evolution com papel só-leitura), curl (evolution.sh), tzdata (fechamento do dia às 21:00 no fuso
# certo). O áudio é decodificado pelo PyAV, que vem com o faster-whisper (sem ffmpeg).
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client curl tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*
# Node 22 + Claude Code (quem lê o lote), do estágio acima.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/@anthropic-ai /usr/local/lib/node_modules/@anthropic-ai
RUN cd /usr/local/lib/node_modules/@anthropic-ai/claude-code \
 && ln -s "$PWD/$(node -p "const b=require('./package.json').bin; typeof b==='string'?b:b.claude")" /usr/local/bin/claude \
 && claude --version

# Transcrição local em CPU (faster-whisper). O modelo é baixado AQUI, na construção; em execução o motor roda offline.
# PyAV abaixo do 19 (16/11/2026): o faster-whisper 1.1.1 chama av.open(..., metadata_errors=...), argumento que o
# PyAV 19 tirou; com ele todo áudio falha e o motor sai sem texto. A conferência logo depois para a construção se o
# PyAV instalado não aceitar o argumento (TypeError); outro erro é só o arquivo vazio, que não é áudio.
RUN if [ "$TRANSCRICAO" = 1 ]; then \
      pip install --no-cache-dir "faster-whisper==1.1.1" "av>=11,<19" requests \
      && python3 -c "import io, sys, av; exec('try:\n av.open(io.BytesIO(b\"\"), metadata_errors=\"ignore\")\nexcept TypeError:\n sys.exit(\"PyAV \" + av.__version__ + \" sem metadata_errors: a transcrição falharia\")\nexcept Exception:\n pass'); print('PyAV', av.__version__, 'aceita metadata_errors')" \
      && python3 -c "from faster_whisper import download_model; download_model('${WHISPER_MODELO}', output_dir='/opt/whisper/modelo')"; \
    fi

RUN useradd --create-home --uid 1000 --shell /bin/bash vigia
WORKDIR /app
COPY --chown=vigia:vigia . /app
# No Mac os scripts apontam para o Python do Homebrew; aqui, para o do contêiner.
RUN sed -i '1s|^#!/opt/homebrew/bin/python3|#!/usr/bin/env python3|' bin/* \
 && chmod +x bin/* \
 && mkdir -p /app/dados && chown vigia:vigia /app/dados \
 && ln -s dados/vigia.env /app/.env   # o painel, o coletor e a ponte leem <raiz>/.env; o entrypoint escreve dados/vigia.env

USER vigia
EXPOSE 8120
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=5 \
  CMD curl -fsS -o /dev/null http://127.0.0.1:8120/login || exit 1
ENTRYPOINT ["/app/bin/docker-entrypoint.sh"]
