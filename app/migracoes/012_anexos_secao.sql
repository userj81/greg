-- 012 — Arquivos anexados à seção (15/11/2026, pedido 80, D-81). Aditiva: uma tabela.
-- O arquivo em si fica em dados/anexos/<arquivo> (fora do banco e fora de dados/caixa: a limpeza semanal não toca).
-- Aqui fica só a ficha: de qual seção é, o nome que a pessoa deu, o tipo conferido pelo conteúdo, o tamanho e o hash.
CREATE TABLE IF NOT EXISTS anexos_secao (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    secao_id  INTEGER NOT NULL,
    nome      TEXT    NOT NULL,
    tipo      TEXT    NOT NULL,
    tamanho   INTEGER NOT NULL,
    sha256    TEXT    NOT NULL,
    arquivo   TEXT    NOT NULL UNIQUE,
    origem    TEXT    NOT NULL DEFAULT 'dono',      -- 'dono' (anexou na tela) ou 'ia' (veio de uma conversa lida)
    msg_id    TEXT,                                 -- a mensagem de onde veio, quando origem = 'ia'
    criado_em TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS anexos_secao_secao ON anexos_secao (secao_id);
CREATE UNIQUE INDEX IF NOT EXISTS anexos_secao_msg ON anexos_secao (secao_id, msg_id) WHERE msg_id IS NOT NULL;
-- A IA de leitura só diz "guarde o arquivo desta mensagem nesta seção" (ela não tem acesso à Evolution). O pedido fica
-- aqui até o coletor (ou a janela da seção) baixar o arquivo; depois de 5 tentativas sem achar a mídia, o pedido sai.
CREATE TABLE IF NOT EXISTS anexos_pendentes (
    msg_id     TEXT    PRIMARY KEY,
    secao_id   INTEGER NOT NULL,
    tentativas INTEGER NOT NULL DEFAULT 0,
    pedido_em  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
