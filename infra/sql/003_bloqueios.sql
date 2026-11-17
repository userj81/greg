-- Bloqueios (D-41, pedidos 21 e 22): conversa bloqueada não tem leitura nenhuma.
-- Idempotente e aditivo. A Evolution continua gravando a LINHA da mensagem (chave, hora e status: ela precisa disso para
-- recibos e atualizações), mas o CONTEÚDO (message, contextInfo, pushName) é trocado por vazio ANTES de ir para o disco,
-- e messageType vira 'bloqueado'. O gatilho da fila (001) não enfileira 'bloqueado'; o coletor e o painel descartam o
-- tipo (lotes.SEM_CONTEUDO). A limpeza semanal apaga as linhas como apaga todas as outras.
CREATE TABLE IF NOT EXISTS vigia.bloqueados (
    jid       text PRIMARY KEY CHECK (jid ~ '^[0-9A-Za-z._:-]{1,80}@(s\.whatsapp\.net|g\.us|lid)$'),
    criado_em timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT ON vigia.bloqueados TO vigia_ro, vigia_limpeza;

-- Contato pode chegar como @lid com o telefone em remoteJidAlt: confere os dois.
CREATE OR REPLACE FUNCTION vigia.apagar_conteudo_bloqueado() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM vigia.bloqueados b
                WHERE b.jid IN (NEW.key->>'remoteJid', NEW.key->>'remoteJidAlt')) THEN
        NEW.message := '{}'::jsonb;
        NEW."contextInfo" := NULL;
        NEW."pushName" := NULL;
        NEW."messageType" := 'bloqueado';
    END IF;
    RETURN NEW;
EXCEPTION WHEN others THEN
    -- Mesmo princípio do D-26: a Evolution nunca para de gravar por culpa do Greg. Quem LÊ (painel e coletor)
    -- consulta vigia.bloqueados de novo e para com erro se a tabela sumir: a leitura falha fechada.
    RAISE WARNING 'vigia.apagar_conteudo_bloqueado falhou para %: %', NEW.id, SQLERRM;
    RETURN NEW;
END;
$$;

-- INSERT e UPDATE: edição de mensagem (UPDATE da Evolution) também não traz o texto de volta.
CREATE OR REPLACE TRIGGER vigia_bloqueio_antes
    BEFORE INSERT OR UPDATE ON public."Message"
    FOR EACH ROW EXECUTE FUNCTION vigia.apagar_conteudo_bloqueado();

-- Bloquear apaga também o conteúdo do que já estava guardado daquela conversa (o UPDATE passa pelo gatilho acima).
CREATE OR REPLACE FUNCTION vigia.bloquear(alvo text) RETURNS int
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, vigia
AS $$
DECLARE
    n int;
BEGIN
    INSERT INTO vigia.bloqueados (jid) VALUES (alvo) ON CONFLICT (jid) DO NOTHING;
    WITH u AS (
        UPDATE public."Message" SET "messageType" = 'bloqueado'
         WHERE (key->>'remoteJid' = alvo OR key->>'remoteJidAlt' = alvo)
           AND "messageType" IS DISTINCT FROM 'bloqueado'
        RETURNING 1)
    SELECT count(*) INTO n FROM u;
    RETURN n;
END;
$$;

-- Desbloquear só vale para o que chegar depois: o conteúdo apagado não volta.
CREATE OR REPLACE FUNCTION vigia.desbloquear(alvo text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, vigia
AS $$
BEGIN
    DELETE FROM vigia.bloqueados WHERE jid = alvo;
    RETURN FOUND;
END;
$$;

REVOKE EXECUTE ON FUNCTION vigia.bloquear(text), vigia.desbloquear(text) FROM PUBLIC;
