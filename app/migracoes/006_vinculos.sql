-- 006 — Vínculo conversa → frente (08/11/2026, D-49). Aditiva: uma tabela nova.
-- Diz à IA de qual frente é cada conversa. `dono` = marcado pelo dono (na página Contatos, em Demandas ou movendo o item
-- no Frentes); `auto` = sugestão pelo nome na agenda (nunca sobrescreve `dono`). frente_id NULL com origem `dono` =
-- "conferido, sem frente": a sugestão não volta.
CREATE TABLE IF NOT EXISTS vinculos (
    conversa TEXT PRIMARY KEY,
    frente_id INTEGER,
    origem TEXT NOT NULL CHECK (origem IN ('dono', 'auto')),
    atualizado_em TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS ix_vinculos_frente ON vinculos (frente_id);
