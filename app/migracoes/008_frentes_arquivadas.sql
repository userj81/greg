-- 008 — Arquivar frente pelo Greg (09/11/2026, pedido 41). Aditiva: uma tabela.
-- O dono do dado continua o Frentes (status 'arquivada' via PATCH /api/frentes/{id}); mas o /api/frentes e o /api/estado
-- não devolvem frente arquivada, então o Greg guarda aqui o que ELE arquivou (nome e tipo no momento) para listar em
-- "Arquivadas (N)" e oferecer "Desarquivar". Desarquivar apaga a linha. Frente arquivada direto no app Frentes não entra.
CREATE TABLE IF NOT EXISTS frentes_arquivadas (
    frente_id    INTEGER PRIMARY KEY,
    nome         TEXT    NOT NULL,
    tipo         TEXT    NOT NULL DEFAULT '',
    arquivada_em TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
