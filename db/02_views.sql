USE flightdeck;

-- one row per month per category: the spine of every chart on the Overview tab
CREATE OR REPLACE VIEW v_month_category AS
SELECT t.ym,
       COALESCE(c.cat_key,'unassigned') AS cat_key,
       COALESCE(c.label,'Unassigned')   AS label,
       COALESCE(c.kind,'variable')      AS kind,
       COUNT(*)                          AS n,
       SUM(CASE WHEN t.amount < 0 THEN -t.amount ELSE 0 END) AS out_amt,
       SUM(CASE WHEN t.amount > 0 THEN  t.amount ELSE 0 END) AS in_amt,
       SUM(t.amount)                     AS net_amt
FROM txn t LEFT JOIN category c ON c.id = t.category_id
WHERE t.is_transfer = 0
GROUP BY t.ym, cat_key, label, kind;

-- money in, money out and what was left, per month
CREATE OR REPLACE VIEW v_month_summary AS
SELECT ym,
       SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END)  AS money_in,
       SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END) AS money_out,
       SUM(amount)                                       AS net,
       COUNT(*)                                          AS n
FROM txn WHERE is_transfer = 0
GROUP BY ym;

-- per account, per month
CREATE OR REPLACE VIEW v_month_account AS
SELECT a.code, a.name, t.ym,
       SUM(CASE WHEN t.amount > 0 THEN t.amount ELSE 0 END)  AS money_in,
       SUM(CASE WHEN t.amount < 0 THEN -t.amount ELSE 0 END) AS money_out,
       SUM(t.amount) AS net, COUNT(*) AS n
FROM txn t JOIN account a ON a.id = t.account_id
GROUP BY a.code, a.name, t.ym;

-- one row per merchant: this is what the review checklist is built from
CREATE OR REPLACE VIEW v_merchant_summary AS
SELECT m.id, m.name, m.reviewed, m.is_recurring, m.is_bill,
       c.cat_key, c.label AS category,
       COUNT(t.id)                       AS charges,
       COUNT(DISTINCT t.ym)              AS months_seen,
       SUM(-t.amount)                    AS total_out,
       ROUND(AVG(-t.amount),2)           AS avg_charge,
       MIN(-t.amount)                    AS min_charge,
       MAX(-t.amount)                    AS max_charge,
       MIN(t.posted_on)                  AS first_seen,
       MAX(t.posted_on)                  AS last_seen
FROM merchant m
LEFT JOIN txn t ON t.merchant_id = m.id AND t.amount < 0
LEFT JOIN category c ON c.id = m.category_id
GROUP BY m.id, m.name, m.reviewed, m.is_recurring, m.is_bill, c.cat_key, c.label;

-- charges that repeat on a steady amount: the subscription hunt
CREATE OR REPLACE VIEW v_recurring_candidates AS
SELECT m.id AS merchant_id, m.name,
       COUNT(*)                                  AS charges,
       COUNT(DISTINCT t.ym)                      AS months_seen,
       ROUND(AVG(-t.amount),2)                   AS avg_charge,
       ROUND(STDDEV_SAMP(-t.amount),2)           AS amount_sd,
       MIN(-t.amount) AS min_charge, MAX(-t.amount) AS max_charge,
       ROUND(AVG(DAYOFMONTH(t.posted_on)))       AS typical_day,
       MIN(t.posted_on) AS first_seen, MAX(t.posted_on) AS last_seen,
       TIMESTAMPDIFF(MONTH, MIN(t.posted_on), MAX(t.posted_on)) + 1 AS span_months
FROM txn t JOIN merchant m ON m.id = t.merchant_id
WHERE t.amount < 0 AND t.is_transfer = 0
GROUP BY m.id, m.name
HAVING COUNT(DISTINCT t.ym) >= 2
   AND (STDDEV_SAMP(-t.amount) IS NULL OR STDDEV_SAMP(-t.amount) <= GREATEST(1.00, AVG(-t.amount) * 0.15));

-- everything still waiting on a decision from you
CREATE OR REPLACE VIEW v_review_queue AS
SELECT t.id, t.posted_on, a.code AS account, t.amount, t.description, t.bank_category
FROM txn t JOIN account a ON a.id = t.account_id
WHERE t.merchant_id IS NULL OR t.category_id IS NULL
ORDER BY t.posted_on DESC;

-- the plan against the last three complete months
CREATE OR REPLACE VIEW v_budget_vs_actual AS
SELECT b.code, b.label, b.grp, b.kind, b.amount AS planned,
       ROUND(COALESCE(act.monthly_avg,0),2) AS actual_monthly_avg,
       ROUND(b.amount - COALESCE(act.monthly_avg,0),2) AS delta
FROM budget_line b
LEFT JOIN (
  SELECT category_id, SUM(-amount) / NULLIF(COUNT(DISTINCT ym),0) AS monthly_avg
  FROM txn
  WHERE amount < 0 AND is_transfer = 0
    AND posted_on >= (SELECT DATE_SUB(MAX(posted_on), INTERVAL 3 MONTH) FROM txn)
  GROUP BY category_id
) act ON act.category_id = b.category_id
WHERE b.active = 1;
