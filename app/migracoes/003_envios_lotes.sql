-- Migração 003 (pedido 10, R-15, D-44): lotes da API local de envio e um registro por destinatário.
-- Nunca o texto da mensagem: só o hash, o destino, a origem (ferramenta que pediu) e o estado.
-- O texto fica em memória no painel enquanto o lote anda; reiniciar o painel marca o lote como interrompido.
CREATE TABLE IF NOT EXISTS lotes_envio (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    origem TEXT NOT NULL,
    idempotencia TEXT,
    hash_texto TEXT NOT NULL,
    n INTEGER NOT NULL,
    estado TEXT NOT NULL,              -- na_fila | enviando | aguardando_conexao | pausado | parado | interrompido | concluido
    motivo TEXT NOT NULL DEFAULT '',
    criado_em TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    atualizado_em TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS lotes_envio_idem ON lotes_envio (origem, idempotencia);
CREATE INDEX IF NOT EXISTS lotes_envio_estado ON lotes_envio (estado);

CREATE TABLE IF NOT EXISTS destinos_envio (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lote_id INTEGER NOT NULL REFERENCES lotes_envio(id),
    pos INTEGER NOT NULL,
    para TEXT NOT NULL,                -- jid (pessoa ou grupo)
    nome TEXT NOT NULL DEFAULT '',     -- só para preencher {nome}; vazio = sem nome conhecido
    conhecido INTEGER NOT NULL DEFAULT 0,  -- 1 = já conversou com ele (ou grupo, ou apelido salvo)
    estado TEXT NOT NULL,              -- fila | enviado | incerto | pulado | cancelado
    motivo TEXT NOT NULL DEFAULT '',
    msg_id TEXT,
    atualizado_em TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS destinos_envio_lote ON destinos_envio (lote_id, pos);
CREATE INDEX IF NOT EXISTS destinos_envio_estado ON destinos_envio (estado, atualizado_em);
