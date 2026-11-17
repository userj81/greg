-- 009 — D-57 (pedido 45, 09/11/2026): consolidação do dia. Aditiva: só colunas novas em classificacoes e um índice.
-- substituida_por: id do cartão que tomou o lugar deste (a mensagem nova mudou data/hora/lugar do mesmo evento, cancelou o
--   evento, ou o fechamento do dia fundiu cartões repetidos). O cartão substituído sai de Demandas (filtro "Substituídos",
--   volta com Restaurar) e nunca é apagado.
-- substituicao_motivo: atualizada | cancelada | consolidada. substituida_em: quando.
-- lancado_em: quando o item subiu ao Frentes. Classe que vira item + item_id NULL + lancado_em NULL + sem decisão =
--   cartão "provisório": sobe no fechamento do dia ([horarios].diario), ou na hora se for atenção, prazo em 24 h ou
--   confirmado pelo dono. Cartões que já tinham item ganham lancado_em = criado_em (já estavam no Frentes).
ALTER TABLE classificacoes ADD COLUMN substituida_por INTEGER;
ALTER TABLE classificacoes ADD COLUMN substituicao_motivo TEXT;
ALTER TABLE classificacoes ADD COLUMN substituida_em TEXT;
ALTER TABLE classificacoes ADD COLUMN lancado_em TEXT;
CREATE INDEX IF NOT EXISTS ix_classificacoes_substituida ON classificacoes (substituida_por);
UPDATE classificacoes SET lancado_em = criado_em WHERE item_id IS NOT NULL AND lancado_em IS NULL;
