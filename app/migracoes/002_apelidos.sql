-- Migração 002 (r02, D-39): nome que o dono dá a um contato DENTRO do Greg. A agenda do celular não é alcançável pela
-- Evolution, então "salvar contato" grava aqui. Só jid e nome; nada de mensagem.
CREATE TABLE IF NOT EXISTS apelidos (
    jid TEXT PRIMARY KEY,
    nome TEXT NOT NULL,
    criado_em TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
