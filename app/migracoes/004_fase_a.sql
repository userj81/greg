-- 004 — Fase A (07/11/2026, D-45): medir antes de decidir produto. Aditiva: só colunas novas e uma tabela nova.

-- Custo e duração de cada chamada ao Claude (o CLI informa total_cost_usd, duration_ms e num_turns).
-- custo_usd é NOCIONAL: o dono usa assinatura; o número diz quanto custaria por API, insumo da decisão de produto.
ALTER TABLE lotes ADD COLUMN custo_usd REAL;
ALTER TABLE lotes ADD COLUMN duracao_ms INTEGER;
ALTER TABLE lotes ADD COLUMN turnos INTEGER;

-- Rotulagem (precisão da classificação): a classe CERTA segundo o dono. Igual à `classe` = acertou; diferente = errou.
-- NULL = ainda não rotulada. Não mexe em `decisao` (confirmado/recusado, que fala do item no Frentes).
ALTER TABLE classificacoes ADD COLUMN classe_curada TEXT;
ALTER TABLE classificacoes ADD COLUMN curado_em TEXT;

-- Histórico de quedas e voltas da conexão (o monitor só guardava o ÚLTIMO estado em conexao.json).
CREATE TABLE IF NOT EXISTS conexao_eventos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evento TEXT NOT NULL,            -- queda | socket_morto | logout_sem_pareamento | voltou
    estado TEXT NOT NULL,            -- open | close | connecting (estado da Evolution no momento)
    motivo TEXT,
    reason_code INTEGER,
    criado_em TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS ix_conexao_eventos_criado ON conexao_eventos (criado_em);
