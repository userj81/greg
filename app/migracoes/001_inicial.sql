-- Migração 001: esquema inicial do Greg (aditivo, idempotente). Só ids, classes e resumos; nunca texto bruto de mensagem.
CREATE TABLE IF NOT EXISTS classificacoes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id TEXT NOT NULL,
    conversa TEXT NOT NULL,
    contato TEXT,
    ts INTEGER NOT NULL,
    classe TEXT NOT NULL,
    resumo TEXT,
    confianca TEXT,
    frente_id INTEGER,
    item_id INTEGER,
    prazo TEXT,
    imagem TEXT,
    decisao TEXT,  -- confirmado|recusado (decidido pelo dono no painel)
    criado_em TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_classificacoes_msg ON classificacoes (msg_id);
CREATE INDEX IF NOT EXISTS ix_classificacoes_conversa_ts ON classificacoes (conversa, ts);
CREATE INDEX IF NOT EXISTS ix_classificacoes_classe ON classificacoes (classe);

CREATE TABLE IF NOT EXISTS lotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nome TEXT NOT NULL,
    conversas INTEGER NOT NULL DEFAULT 0,
    novas INTEGER NOT NULL DEFAULT 0,
    itens INTEGER NOT NULL DEFAULT 0,
    atencao INTEGER NOT NULL DEFAULT 0,
    tokens_in INTEGER,
    tokens_out INTEGER,
    modelo TEXT,
    criado_em TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS qr_eventos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    origem TEXT NOT NULL,
    criado_em TEXT NOT NULL,  -- ISO local, ex. 2026-11-06 14:03:00
    epoch INTEGER  -- qrs_hoje() conta por ele; os gatilhos abaixo o mantêm coerente com criado_em
);
CREATE INDEX IF NOT EXISTS ix_qr_eventos_epoch ON qr_eventos (epoch);
CREATE TRIGGER IF NOT EXISTS tg_qr_epoch_ins AFTER INSERT ON qr_eventos WHEN NEW.epoch IS NULL
BEGIN UPDATE qr_eventos SET epoch = CAST(strftime('%s', NEW.criado_em, 'utc') AS INTEGER) WHERE id = NEW.id; END;
CREATE TRIGGER IF NOT EXISTS tg_qr_epoch_upd AFTER UPDATE OF criado_em ON qr_eventos
BEGIN UPDATE qr_eventos SET epoch = CAST(strftime('%s', NEW.criado_em, 'utc') AS INTEGER) WHERE id = NEW.id; END;

CREATE TABLE IF NOT EXISTS envios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    para TEXT NOT NULL,
    hash_texto TEXT NOT NULL,
    por TEXT NOT NULL,
    criado_em TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS lapides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    semana TEXT NOT NULL UNIQUE,
    n_msgs INTEGER NOT NULL,
    hash_relatorio TEXT NOT NULL,
    apagado_em INTEGER NOT NULL
);

-- Derivada de classificacoes (histórico completo; o recorte por dias é feito em Banco.conversas_recentes).
CREATE VIEW IF NOT EXISTS conversas AS
SELECT c.conversa AS conversa,
       (SELECT x.contato FROM classificacoes x WHERE x.conversa = c.conversa ORDER BY x.ts DESC, x.id DESC LIMIT 1) AS contato,
       COUNT(*) AS n_novas,
       (SELECT x.classe FROM classificacoes x WHERE x.conversa = c.conversa ORDER BY x.ts DESC, x.id DESC LIMIT 1) AS ultima_classe,
       MAX(c.ts) AS ultima_em
FROM classificacoes c
GROUP BY c.conversa;
