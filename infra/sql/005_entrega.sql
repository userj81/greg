-- Status de entrega das mensagens PRÓPRIAS (pedido 51). Idempotente e aditivo: NÃO altera public."Message" nem
-- public."MessageUpdate"; só observa os INSERTs desta última. A Evolution 2.3.7 recebe do WhatsApp o avanço
-- (enviada, entregue, lida, ouvida) mas só o grava em "Message" quando a mensagem NÃO é própria; para a própria ele
-- fica apenas em "MessageUpdate" (com DATABASE_SAVE_MESSAGE_UPDATE=true). O gatilho guarda o nível mais alto de cada
-- mensagem, uma linha por mensagem, e o painel lê esta tabela. Sem histórico: vale para o que for enviado depois.
CREATE TABLE IF NOT EXISTS vigia.entrega (
    message_id    text PRIMARY KEY,
    status        text NOT NULL,
    nivel         smallint NOT NULL,
    atualizado_em timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT ON vigia.entrega TO vigia_ro, vigia_limpeza;

CREATE OR REPLACE FUNCTION vigia.anotar_entrega() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    n smallint;
BEGIN
    IF NOT NEW."fromMe" THEN
        RETURN NEW;
    END IF;
    n := CASE NEW.status WHEN 'SERVER_ACK' THEN 1 WHEN 'DELIVERY_ACK' THEN 2 WHEN 'READ' THEN 3 WHEN 'PLAYED' THEN 4 ELSE 0 END;
    IF n = 0 THEN
        RETURN NEW;
    END IF;
    -- os avisos chegam fora de ordem (lida antes de entregue): o nível só sobe
    INSERT INTO vigia.entrega (message_id, status, nivel) VALUES (NEW."messageId", NEW.status, n)
    ON CONFLICT (message_id) DO UPDATE SET status = EXCLUDED.status, nivel = EXCLUDED.nivel, atualizado_em = now()
        WHERE vigia.entrega.nivel < EXCLUDED.nivel;
    -- a limpeza semanal apaga as mensagens; de vez em quando some a anotação de quem já não existe
    IF random() < 0.01 THEN
        DELETE FROM vigia.entrega e WHERE NOT EXISTS (SELECT 1 FROM public."Message" m WHERE m.id = e.message_id);
    END IF;
    RETURN NEW;
EXCEPTION WHEN others THEN
    -- como na fila (D-26): a Evolution nunca para de gravar por culpa desta anotação.
    RAISE WARNING 'vigia.anotar_entrega falhou para %: %', NEW."messageId", SQLERRM;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER vigia_entrega_apos_insert
    AFTER INSERT ON public."MessageUpdate"
    FOR EACH ROW EXECUTE FUNCTION vigia.anotar_entrega();
