-- 007 — Triagem no estilo Papel (08/11/2026, pedido 39). Aditiva: uma coluna.
-- Quando o dono decidiu (confirmou ou recusou) a classificação. Com criado_em, mede quanto tempo uma demanda fica na
-- caixa ("5 triadas hoje em 2 min 40", rodapé do estado vazio). NULL = ainda sem decisão (ou decisão desfeita).
ALTER TABLE classificacoes ADD COLUMN decidido_em TEXT;
