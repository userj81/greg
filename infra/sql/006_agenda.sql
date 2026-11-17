-- Nome SALVO na agenda do WhatsApp (pedido 54). Idempotente e aditivo: NÃO altera public."Contact" nem
-- public."Message"; só observa. A Evolution 2.3.7 guarda o nome da agenda em "Contact"."pushName" quando o WhatsApp
-- manda a agenda (contacts.upsert / contacts.update), mas troca esse mesmo campo pelo nome do PERFIL da pessoa a cada
-- mensagem que ela envia, e o nome salvo se perde. Esta tabela guarda o nome salvo à parte, e o painel o usa antes do
-- nome do perfil. Como separar um do outro: se o nome novo é igual ao nome de perfil de uma mensagem daquela pessoa,
-- é perfil e não entra aqui. As duas ordens de gravação estão cobertas (contato antes ou depois da mensagem).
CREATE TABLE IF NOT EXISTS vigia.agenda (
    remote_jid    text PRIMARY KEY,
    nome          text NOT NULL,
    anterior      text,
    atualizado_em timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT ON vigia.agenda TO vigia_ro, vigia_limpeza;

CREATE OR REPLACE FUNCTION vigia.anotar_agenda() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    n text := btrim(coalesce(NEW."pushName", ''));
BEGIN
    IF n = '' OR n !~ '[^0-9+() .-]' OR NEW."remoteJid" !~ '@(s\.whatsapp\.net|lid)$' THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE' AND NEW."pushName" IS NOT DISTINCT FROM OLD."pushName" THEN
        RETURN NEW;
    END IF;
    -- nome de perfil: alguma mensagem dessa pessoa já veio com ele
    IF EXISTS (SELECT 1 FROM public."Message" m
                WHERE (m."key"->>'remoteJid' = NEW."remoteJid" OR m."key"->>'remoteJidAlt' = NEW."remoteJid")
                  AND coalesce(m."key"->>'fromMe', 'false') <> 'true' AND m."pushName" = NEW."pushName") THEN
        RETURN NEW;
    END IF;
    INSERT INTO vigia.agenda (remote_jid, nome) VALUES (NEW."remoteJid", n)
    ON CONFLICT (remote_jid) DO UPDATE SET anterior = vigia.agenda.nome, nome = EXCLUDED.nome, atualizado_em = now()
        WHERE vigia.agenda.nome <> EXCLUDED.nome;
    RETURN NEW;
EXCEPTION WHEN others THEN
    RAISE WARNING 'vigia.anotar_agenda falhou para %: %', NEW."remoteJid", SQLERRM;
    RETURN NEW;
END;
$$;

-- BEFORE, e não AFTER: quando a agenda chega para um contato que JÁ existe, a Evolution faz INSERT ... ON CONFLICT DO
-- NOTHING (createMany com skipDuplicates) e a linha não muda. O gatilho BEFORE INSERT roda mesmo assim, antes da
-- conferência de conflito, e é a única hora em que o nome salvo passa pelo banco. O nome do gatilho ficou o da 1ª versão.
CREATE OR REPLACE TRIGGER vigia_agenda_apos_contato
    BEFORE INSERT OR UPDATE ON public."Contact"
    FOR EACH ROW EXECUTE FUNCTION vigia.anotar_agenda();

-- A mensagem chegou DEPOIS de o contato ser trocado pelo nome do perfil: o que acabou de entrar na agenda com esse
-- mesmo nome era perfil. Volta o nome salvo anterior, se havia; senão tira a linha.
CREATE OR REPLACE FUNCTION vigia.desfazer_agenda() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF coalesce(NEW."key"->>'fromMe', 'false') = 'true' OR coalesce(NEW."pushName", '') = '' THEN
        RETURN NEW;
    END IF;
    UPDATE vigia.agenda a SET nome = a.anterior, anterior = NULL
     WHERE a.remote_jid IN (NEW."key"->>'remoteJid', NEW."key"->>'remoteJidAlt')
       AND a.nome = btrim(NEW."pushName") AND a.anterior IS NOT NULL AND a.atualizado_em > now() - interval '10 minutes';
    DELETE FROM vigia.agenda a
     WHERE a.remote_jid IN (NEW."key"->>'remoteJid', NEW."key"->>'remoteJidAlt')
       AND a.nome = btrim(NEW."pushName") AND a.anterior IS NULL AND a.atualizado_em > now() - interval '10 minutes';
    RETURN NEW;
EXCEPTION WHEN others THEN
    RAISE WARNING 'vigia.desfazer_agenda falhou: %', SQLERRM;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER vigia_agenda_apos_mensagem
    AFTER INSERT ON public."Message"
    FOR EACH ROW EXECUTE FUNCTION vigia.desfazer_agenda();

-- Carga inicial com o que ainda está preservado em "Contact": nome com letra que nenhuma mensagem da pessoa trouxe
-- como nome de perfil. Quem já teve o nome salvo trocado pelo do perfil só volta quando o WhatsApp reenviar a agenda.
INSERT INTO vigia.agenda (remote_jid, nome)
SELECT DISTINCT ON (c."remoteJid") c."remoteJid", btrim(c."pushName")
  FROM public."Contact" c
 WHERE c."remoteJid" ~ '@(s\.whatsapp\.net|lid)$' AND btrim(coalesce(c."pushName", '')) ~ '[^0-9+() .-]'
   AND NOT EXISTS (SELECT 1 FROM public."Message" m
                    WHERE (m."key"->>'remoteJid' = c."remoteJid" OR m."key"->>'remoteJidAlt' = c."remoteJid")
                      AND coalesce(m."key"->>'fromMe', 'false') <> 'true' AND m."pushName" = c."pushName")
 ORDER BY c."remoteJid", c."updatedAt" DESC NULLS LAST
ON CONFLICT (remote_jid) DO NOTHING;
