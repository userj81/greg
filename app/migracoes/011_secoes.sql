-- 011 — Seções dentro da frente (15/11/2026, pedido 79, D-80). Aditiva: duas tabelas e uma coluna.
-- O dono dos itens continua o Frentes (embutido ou externo); ele não tem seção. O Greg guarda aqui a divisão de cada
-- frente e em qual seção cada item está, do mesmo jeito que guarda as frentes arquivadas (008). Item sem linha em
-- itens_secao está "sem seção". Apagar a seção apaga só as linhas daqui: nenhum item some.
CREATE TABLE IF NOT EXISTS secoes (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    frente_id INTEGER NOT NULL,
    nome      TEXT    NOT NULL,
    descricao TEXT    NOT NULL DEFAULT '',
    ordem     INTEGER NOT NULL DEFAULT 0,
    criada_em TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE UNIQUE INDEX IF NOT EXISTS secoes_nome ON secoes (frente_id, nome COLLATE NOCASE);
-- origem: 'dono' (a pessoa pôs ou moveu) ou 'ia' (a leitura escolheu). O que a pessoa pôs a IA não tira.
CREATE TABLE IF NOT EXISTS itens_secao (
    item_id  INTEGER PRIMARY KEY,
    secao_id INTEGER NOT NULL,
    origem   TEXT    NOT NULL DEFAULT 'dono',
    posto_em TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS itens_secao_secao ON itens_secao (secao_id);
-- a seção que a IA escolheu fica no cartão até ele subir (cartão provisório sobe só no fechamento do dia)
ALTER TABLE classificacoes ADD COLUMN secao_id INTEGER;
