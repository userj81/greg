-- Mensagens apagadas pelo remetente (D-63, pedido 50). Idempotente e aditivo: NÃO altera public."Message"; só observa
-- os UPDATEs dela. A Evolution 2.3.7 trata a exclusão recebida como edição: põe status 'EDITED', troca a hora e NÃO
-- mexe no conteúdo (numa edição de verdade o conteúdo muda). É essa diferença que o gatilho anota. Sem histórico: só
-- vale para o que for apagado depois do gatilho. O conteúdo continua no banco até a limpeza semanal; quem esconde é o
-- painel, que lê esta tabela.
CREATE TABLE IF NOT EXISTS vigia.apagadas (
    message_id text PRIMARY KEY,
    criado_em  timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT ON vigia.apagadas TO vigia_ro, vigia_limpeza;

CREATE OR REPLACE FUNCTION vigia.anotar_apagada() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status = 'EDITED' AND NEW."messageType" <> 'bloqueado'
       AND NEW.message IS NOT DISTINCT FROM OLD.message
       AND (OLD.status IS DISTINCT FROM 'EDITED' OR NEW."messageTimestamp" IS DISTINCT FROM OLD."messageTimestamp") THEN
        INSERT INTO vigia.apagadas (message_id) VALUES (NEW.id) ON CONFLICT (message_id) DO NOTHING;
        -- a limpeza semanal apaga as mensagens; aqui some a anotação de quem já não existe
        DELETE FROM vigia.apagadas a WHERE NOT EXISTS (SELECT 1 FROM public."Message" m WHERE m.id = a.message_id);
    ELSIF NEW.status = 'EDITED' AND NEW.message IS DISTINCT FROM OLD.message THEN
        -- editada de verdade depois de uma anotação errada: a anotação sai
        DELETE FROM vigia.apagadas WHERE message_id = NEW.id;
    END IF;
    RETURN NEW;
EXCEPTION WHEN others THEN
    -- como na fila (D-26): a Evolution nunca para de gravar por culpa desta anotação.
    RAISE WARNING 'vigia.anotar_apagada falhou para %: %', NEW.id, SQLERRM;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER vigia_apagada_apos_update
    AFTER UPDATE ON public."Message"
    FOR EACH ROW EXECUTE FUNCTION vigia.anotar_apagada();
