-- Papéis de menor privilégio (CONSTITUTION: app lê com vigia_ro; vigia_limpeza só no fechar-semana).
-- Idempotente. Senhas chegam por variáveis psql :ro_pass e :limpeza_pass, definidas por
-- `\set` na entrada padrão por bin/evolution.sh a partir do .env.compose (nunca na linha de comando).
-- CREATE ROLE não tem IF NOT EXISTS: o papel só é criado se não existir (WHERE NOT EXISTS + \gexec);
-- o ALTER ROLE seguinte reaplica a senha, então rodar de novo é seguro.
SELECT format('CREATE ROLE vigia_ro LOGIN PASSWORD %L', :'ro_pass')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vigia_ro') \gexec
SELECT format('ALTER ROLE vigia_ro LOGIN PASSWORD %L', :'ro_pass') \gexec

SELECT format('CREATE ROLE vigia_limpeza LOGIN PASSWORD %L', :'limpeza_pass')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vigia_limpeza') \gexec
SELECT format('ALTER ROLE vigia_limpeza LOGIN PASSWORD %L', :'limpeza_pass') \gexec

GRANT USAGE ON SCHEMA public, vigia TO vigia_ro, vigia_limpeza;

-- vigia_ro: somente leitura.
GRANT SELECT ON public."Message", public."Contact", vigia.fila TO vigia_ro;

-- A fila é consumida por seq: o coletor lê o last_value da sequência (FilaRecriada).
GRANT SELECT ON SEQUENCE vigia.fila_seq_seq TO vigia_ro;

-- vigia_limpeza: lê as mesmas tabelas. DELETE direto foi revogado (D-25, P-1):
-- DELETE agora só via vigia.limpar_janela (lápide, R-12).
GRANT SELECT ON public."Message", public."Contact", vigia.fila TO vigia_limpeza;
REVOKE DELETE ON public."Message", vigia.fila FROM vigia_limpeza;

-- Função dona `evolution` (quem aplica este arquivo), SECURITY DEFINER: apaga só janela fechada.
-- Janela [ini, fim) em epoch (segundos). Recusa fim depois do início da semana ISO corrente.
CREATE OR REPLACE FUNCTION vigia.limpar_janela(ini int, fim int) RETURNS int
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, vigia
AS $$
DECLARE
    semana_corrente int := extract(epoch FROM date_trunc('week', now()))::int;
    apagadas int;
BEGIN
    IF ini IS NULL OR fim IS NULL OR ini >= fim THEN
        RAISE EXCEPTION 'janela invalida: ini=% fim=%', ini, fim;
    END IF;
    IF fim > semana_corrente THEN
        RAISE EXCEPTION 'janela abre a semana ISO corrente: fim=% > inicio da semana=%', fim, semana_corrente;
    END IF;
    DELETE FROM vigia.fila
     WHERE message_id IN (SELECT id FROM public."Message"
                           WHERE "messageTimestamp" >= ini AND "messageTimestamp" < fim);
    WITH m AS (
        DELETE FROM public."Message"
         WHERE "messageTimestamp" >= ini AND "messageTimestamp" < fim
        RETURNING 1)
    SELECT count(*) INTO apagadas FROM m;
    DELETE FROM vigia.fila f
     WHERE NOT EXISTS (SELECT 1 FROM public."Message" x WHERE x.id = f.message_id);
    RETURN apagadas;
END;
$$;

REVOKE EXECUTE ON FUNCTION vigia.limpar_janela(int, int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION vigia.limpar_janela(int, int) TO vigia_limpeza;
