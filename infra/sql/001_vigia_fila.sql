-- Fila ordenada de mensagens novas (D-2): vigia.fila.seq é a ordem de leitura do coletor.
-- Idempotente e aditivo: NÃO altera public."Message"; só observa os INSERTs dela via trigger.
-- Aplicar depois que a Evolution criou as tabelas (bin/evolution.sh up faz isso).
-- Sem histórico (R-2): nada é preenchido retroativamente; só entra o que chegar depois do trigger.
CREATE SCHEMA IF NOT EXISTS vigia;

CREATE TABLE IF NOT EXISTS vigia.fila (
    seq        bigserial PRIMARY KEY,
    message_id text NOT NULL UNIQUE,
    criado_em  timestamptz DEFAULT now()
);

CREATE OR REPLACE FUNCTION vigia.enfileirar() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    -- D-41: conversa bloqueada não entra na fila (o gatilho de 003 já trocou o tipo antes de gravar).
    IF NEW."messageType" = 'bloqueado' THEN
        RETURN NEW;
    END IF;
    INSERT INTO vigia.fila (message_id) VALUES (NEW.id) ON CONFLICT (message_id) DO NOTHING;
    RETURN NEW;
EXCEPTION WHEN others THEN
    -- D-26 (reversível): a Evolution nunca para de gravar por culpa da fila.
    -- A coleta confere "Message sem fila" a cada ciclo e recupera o que faltar.
    RAISE WARNING 'vigia.enfileirar falhou para %: %', NEW.id, SQLERRM;
    RETURN NEW;
END;
$$;

-- PG 14+ (imagem postgres:16): troca atômica, sem janela sem trigger entre DROP e CREATE.
CREATE OR REPLACE TRIGGER vigia_fila_apos_insert
    AFTER INSERT ON public."Message"
    FOR EACH ROW EXECUTE FUNCTION vigia.enfileirar();
