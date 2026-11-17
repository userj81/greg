-- 005 — Transcrição automática de áudio (08/11/2026, D-47). Aditiva: uma tabela nova.
-- É o ÚNICO texto de mensagem que o SQLite guarda (o resto é resumo da IA): texto gerado pelo motor LOCAL
-- (mlx_whisper, nada sai do Mac) a partir do áudio cru. A limpeza semanal apaga a janela junto com o cru (R-12).
CREATE TABLE IF NOT EXISTS transcricoes (
    msg_id TEXT PRIMARY KEY,
    conversa TEXT NOT NULL,
    ts INTEGER NOT NULL,                      -- messageTimestamp em segundos: é por ele que a limpeza semanal apaga
    estado TEXT NOT NULL,                     -- ok | cortado (só os primeiros N s) | falhou | pulado (curto demais)
    texto TEXT NOT NULL DEFAULT '',
    segundos INTEGER NOT NULL DEFAULT 0,      -- duração declarada do áudio
    segundos_lidos INTEGER NOT NULL DEFAULT 0,-- quanto o motor ouviu (= segundos quando ok; o corte quando cortado)
    motor TEXT,                               -- ex.: mlx_whisper/whisper-large-v3-turbo
    tempo_ms INTEGER,                         -- quanto o motor levou (rateado por áudio quando foi um processo só)
    tentativas INTEGER NOT NULL DEFAULT 1,
    em TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS ix_transcricoes_conversa ON transcricoes (conversa, ts);
CREATE INDEX IF NOT EXISTS ix_transcricoes_ts ON transcricoes (ts);
