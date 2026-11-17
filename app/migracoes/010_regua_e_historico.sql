-- 010 — D-58 (pedido 46, 10/11/2026): a IA decide o que sobe, e toda análise deixa histórico. Aditiva: três colunas em
-- classificacoes e uma tabela nova.
-- classe_original / rebaixada_em / rebaixada_motivo: no fechamento do dia a IA segura o cartão provisório que era só
--   conversa (ruído, pergunta já respondida, assunto resolvido na própria conversa): a classe vira aviso ou sem_acao e o
--   cartão NÃO sobe ao Frentes. A classe que a leitura tinha dado fica em classe_original; nada é apagado. O dono desfaz
--   rotulando a classe certa em Demandas (o cartão volta a provisório).
-- analises: o histórico de análise. Uma linha por conversa por leitura: o que aconteceu ali (resumo escrito pela IA, sem
--   texto literal de mensagem) e em que pé a conversa ficou. É o que responde "o que aconteceu essa semana?", "tem alguém
--   esperando resposta minha?". Derivado como as classificações: fica quando o cru é limpo (P-2).
ALTER TABLE classificacoes ADD COLUMN classe_original TEXT;
ALTER TABLE classificacoes ADD COLUMN rebaixada_em TEXT;
ALTER TABLE classificacoes ADD COLUMN rebaixada_motivo TEXT;
CREATE TABLE IF NOT EXISTS analises (
    id        INTEGER PRIMARY KEY,
    msg_id    TEXT NOT NULL UNIQUE,   -- última mensagem nova da conversa naquela leitura (idempotência)
    lote      TEXT,                   -- AAAA-MM-DD_HHMM da leitura
    conversa  TEXT NOT NULL,
    contato   TEXT,
    ts        INTEGER NOT NULL,       -- hora da última mensagem analisada (epoch s)
    resumo    TEXT NOT NULL,          -- o que aconteceu na conversa nesta leitura (uma linha)
    situacao  TEXT NOT NULL,          -- aguarda_dono | aguarda_contato | encerrada
    criado_em TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS ix_analises_ts ON analises (ts);
CREATE INDEX IF NOT EXISTS ix_analises_conversa_ts ON analises (conversa, ts);
