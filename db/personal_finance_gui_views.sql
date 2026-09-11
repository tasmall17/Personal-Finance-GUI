-- Rollups over claudia_usaa_import.
--
-- Two rules run through this whole file.
--
-- 1. Nothing here is a table. Every rollup is a view: a saved query that stores
--    no rows and cannot drift, because it is computed the moment you select
--    from it. There is no "monthly" table and no "food" table.
--
-- 2. Every money figure carries both a monthly and an annual form, whatever
--    the real cadence is. A monthly charge is also shown times twelve. A yearly
--    charge is also shown divided by twelve. Anything on another rhythm is
--    normalised to both.
--
-- Run against the same schema as claudia_usaa_import.

-- ==========================================================================
-- The account list. A small dimension table, so acct_id means the same thing
-- everywhere and the GUI can offer a dropdown without scanning the fact table.
-- Edit the names to whatever you actually call these.
-- ==========================================================================

CREATE TABLE IF NOT EXISTS account (
  acct_id      VARCHAR(40)  NOT NULL,
  account_code VARCHAR(8)   NOT NULL,
  label        VARCHAR(80)  NOT NULL,
  kind         ENUM('checking','savings','credit','loan','mortgage','other') NOT NULL,
  is_own       TINYINT(1)   NOT NULL DEFAULT 1,   -- own account => moves are transfers
  PRIMARY KEY (acct_id),
  UNIQUE KEY uq_account_code (account_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- No accounts are seeded here. They arrive with your data: the Plaid link, the
-- CSV loader and the sample dataset each create their own. A fresh install has
-- an empty account table on purpose, so nobody inherits someone else's bank.

-- ==========================================================================
-- Step 1: flag transfers, with a rule that actually runs.
-- Money between your own accounts is neither income nor spending. Counted as
-- both, it inflates every total. Re-run this after each load.
-- ==========================================================================

-- Internal-movement rules live in a table, so they travel with the install and
-- anyone can add their own bank's wording without editing SQL.
CREATE TABLE IF NOT EXISTS internal_pattern (
  id      SMALLINT UNSIGNED NOT NULL AUTO_INCREMENT,
  pattern VARCHAR(160) NOT NULL,          -- matched against the description, case-insensitive
  why     VARCHAR(200) NOT NULL,          -- shown in the audit view, so a flag is never mysterious
  active  TINYINT(1)   NOT NULL DEFAULT 1,
  PRIMARY KEY (id),
  UNIQUE KEY uq_internal_pattern (pattern)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

INSERT IGNORE INTO internal_pattern (pattern, why) VALUES
  ('FUNDS TRANSFER',        'Bank wording for a move between your own accounts.'),
  ('INTERNET TRANSFER',     'Bank wording for a move between your own accounts.'),
  ('TRANSFER TO',           'Named destination is one of your own accounts.'),
  ('TRANSFER FROM',         'Named source is one of your own accounts.'),
  ('OD ADVANCE TRANSFER',   'Overdraft advance. Mirrors exactly, and the bank files it as a loan.'),
  ('CREDIT CRD EPAY',       'Paying a card that is itself in this dataset, so the spend is already counted.'),
  ('CREDIT CARD PAYMENT',   'Paying a card that is itself in this dataset, so the spend is already counted.');

-- Money between your own accounts is neither income nor spending. Counted as
-- both, it inflates every total. Re-run this after each load.
--
-- Two ways a row qualifies: it matches a known wording above, or its description
-- names one of your own accounts. The account list comes from the `account`
-- table, so this works for any bank without editing the query.
UPDATE claudia_usaa_import t
   SET t.is_transfer = 1
 WHERE t.is_transfer = 0
   AND (
        EXISTS (SELECT 1 FROM internal_pattern p
                 WHERE p.active = 1
                   AND UPPER(COALESCE(t.description, t.col_description, ''))
                       LIKE CONCAT('%', UPPER(p.pattern), '%'))
     OR EXISTS (SELECT 1 FROM account a
                 WHERE a.is_own = 1
                   AND CHAR_LENGTH(a.account_code) >= 4
                   AND COALESCE(t.description, t.col_description, '')
                       LIKE CONCAT('%', a.account_code, '%'))
   );

-- ==========================================================================
-- Step 2: monthly and yearly totals, each shown both ways.
-- ==========================================================================

CREATE OR REPLACE VIEW v_month_summary AS
SELECT t.ym,
       SUM(CASE WHEN t.direction = 'in'  THEN t.amount     ELSE 0 END) AS money_in,
       SUM(CASE WHEN t.direction = 'out' THEN t.abs_amount ELSE 0 END) AS money_out,
       SUM(t.amount)                                                   AS net,
       -- Times twelve, but ONLY for a month the data actually covers end to
       -- end. The first and last months of any feed are stubs: annualising a
       -- 9-day September by 12 produces a confident, plausible, wrong number.
       CASE WHEN cov.complete THEN ROUND(SUM(CASE WHEN t.direction='in' THEN t.amount ELSE 0 END)*12,2) END AS money_in_annual,
       CASE WHEN cov.complete THEN ROUND(SUM(CASE WHEN t.direction='out' THEN t.abs_amount ELSE 0 END)*12,2) END AS money_out_annual,
       CASE WHEN cov.complete THEN ROUND(SUM(t.amount)*12,2) END       AS net_annual,
       cov.complete                                                    AS is_complete_month,
       COUNT(*)                                                        AS txns
  FROM claudia_usaa_import t
  JOIN (SELECT x.ym,
               (STR_TO_DATE(CONCAT(x.ym,'-01'),'%Y-%m-%d') >= g.mn
                AND LAST_DAY(STR_TO_DATE(CONCAT(x.ym,'-01'),'%Y-%m-%d')) <= g.mx) AS complete
          FROM (SELECT DISTINCT ym FROM claudia_usaa_import WHERE txn_date IS NOT NULL) x
         CROSS JOIN (SELECT MIN(txn_date) mn, MAX(txn_date) mx
                       FROM claudia_usaa_import WHERE txn_date IS NOT NULL) g
       ) cov ON cov.ym = t.ym
 WHERE t.is_transfer = 0
   AND t.txn_date IS NOT NULL
 GROUP BY t.ym, cov.complete;

CREATE OR REPLACE VIEW v_month_over_month AS
SELECT ym, money_in, money_out, net, net_annual,
       LAG(net) OVER (ORDER BY ym)       AS net_prev,
       net - LAG(net) OVER (ORDER BY ym) AS net_change
  FROM v_month_summary;

CREATE OR REPLACE VIEW v_year_summary AS
SELECT LEFT(ym, 4) AS yr,
       SUM(CASE WHEN direction = 'in'  THEN amount     ELSE 0 END) AS money_in,
       SUM(CASE WHEN direction = 'out' THEN abs_amount ELSE 0 END) AS money_out,
       SUM(amount)                                                 AS net,
       -- and the monthly equivalent of each, so a yearly figure is readable
       ROUND(SUM(CASE WHEN direction = 'in'  THEN amount     ELSE 0 END) / 12, 2) AS money_in_monthly,
       ROUND(SUM(CASE WHEN direction = 'out' THEN abs_amount ELSE 0 END) / 12, 2) AS money_out_monthly,
       ROUND(SUM(amount) / 12, 2)                                   AS net_monthly,
       COUNT(*)                                                     AS txns
  FROM claudia_usaa_import
 WHERE is_transfer = 0
   AND txn_date IS NOT NULL
 GROUP BY yr;

CREATE OR REPLACE VIEW v_month_category AS
SELECT ym,
       COALESCE(NULLIF(TRIM(col_category), ''), 'uncategorised') AS category,
       SUM(abs_amount)                  AS spent,
       ROUND(SUM(abs_amount) * 12, 2)   AS spent_annual,
       COUNT(*)                         AS charges
  FROM claudia_usaa_import
 WHERE is_transfer = 0
   AND direction = 'out'
   AND txn_date IS NOT NULL
 GROUP BY ym, category;

-- ==========================================================================
-- Step 3: cadence. How often does each payee actually charge you?
--
-- The gap between one charge and the next is what tells you. LAG reaches back
-- to the previous charge for the same payee without a self-join.
-- ==========================================================================

CREATE OR REPLACE VIEW v_payee_gaps AS
SELECT COALESCE(NULLIF(TRIM(description), ''), '(no description)') AS payee,
       acct_id,
       txn_date,
       abs_amount,
       DATEDIFF(
         txn_date,
         LAG(txn_date) OVER (
           PARTITION BY COALESCE(NULLIF(TRIM(description), ''), '(no description)')
           ORDER BY txn_date)
       ) AS gap_days
  FROM claudia_usaa_import
 WHERE is_transfer = 0
   AND direction = 'out'
   AND txn_date IS NOT NULL;

-- Every payee, its rhythm, and the same money expressed both ways.
CREATE OR REPLACE VIEW v_payee_cadence AS
SELECT payee,
       COUNT(*)                       AS charges,
       MIN(txn_date)                  AS first_seen,
       MAX(txn_date)                  AS last_seen,
       ROUND(AVG(abs_amount), 2)      AS avg_amount,
       MIN(abs_amount)                AS min_amount,
       MAX(abs_amount)                AS max_amount,
       ROUND(SUM(abs_amount), 2)      AS total_charged,
       ROUND(AVG(gap_days), 1)        AS avg_gap_days,
       CASE
         WHEN AVG(gap_days) IS NULL            THEN 'once'
         WHEN AVG(gap_days) BETWEEN   5 AND  9 THEN 'weekly'
         WHEN AVG(gap_days) BETWEEN  12 AND 16 THEN 'biweekly'
         WHEN AVG(gap_days) BETWEEN  26 AND 35 THEN 'monthly'
         WHEN AVG(gap_days) BETWEEN  55 AND 70 THEN 'bimonthly'
         WHEN AVG(gap_days) BETWEEN  80 AND 100 THEN 'quarterly'
         WHEN AVG(gap_days) BETWEEN 170 AND 200 THEN 'semiannual'
         WHEN AVG(gap_days) BETWEEN 330 AND 400 THEN 'annual'
         ELSE 'irregular'
       END                            AS cadence,
       -- normalise whatever the rhythm is into the two numbers you want
       ROUND(AVG(abs_amount) * 365.25 / NULLIF(AVG(gap_days), 0), 2)      AS annual_equivalent,
       ROUND(AVG(abs_amount) * 365.25 / NULLIF(AVG(gap_days), 0) / 12, 2) AS monthly_equivalent
  FROM v_payee_gaps
 GROUP BY payee;

-- ==========================================================================
-- Step 4: the subscriptions view.
--
-- A subscription is a payee that charges on a steady rhythm for a steady
-- amount and has not stopped. Everything here is derived, so a cancelled
-- service drops off by itself once it stops appearing.
-- ==========================================================================

CREATE OR REPLACE VIEW v_monthly_subscriptions AS
SELECT payee,
       cadence,
       avg_amount            AS charge_amount,
       monthly_equivalent,
       annual_equivalent,
       charges,
       avg_gap_days,
       first_seen,
       last_seen,
       total_charged
  FROM v_payee_cadence
 WHERE cadence NOT IN ('once', 'irregular')
   -- three charges proves a rhythm, but a yearly bill only shows twice even
   -- in two years of data, so long cadences get a lower bar
   AND (charges >= 3 OR (cadence IN ('annual', 'semiannual') AND charges >= 2))
   -- the amount barely moves: within 15% of itself, or within a dollar
   AND (max_amount - min_amount) <= GREATEST(1.00, avg_amount * 0.15)
   -- still running: last charge is within two cycles of the latest data
   AND last_seen >= (SELECT MAX(txn_date) FROM claudia_usaa_import) - INTERVAL (avg_gap_days * 2) DAY;

-- What all the subscriptions cost together, both ways.
CREATE OR REPLACE VIEW v_subscription_total AS
SELECT COUNT(*)                          AS subscriptions,
       ROUND(SUM(monthly_equivalent), 2) AS monthly_total,
       ROUND(SUM(annual_equivalent), 2)  AS annual_total
  FROM v_monthly_subscriptions;

-- ==========================================================================
-- Step 5: data quality. Check this before trusting anything above it.
-- ==========================================================================

CREATE OR REPLACE VIEW v_data_quality AS
SELECT source_file,
       acct_id,
       COUNT(*)              AS rows_loaded,
       SUM(parse_ok = 0)     AS unparsed,
       SUM(txn_date IS NULL) AS no_date,
       SUM(amount IS NULL)   AS no_amount,
       MIN(txn_date)         AS first_date,
       MAX(txn_date)         AS last_date
  FROM claudia_usaa_import
 GROUP BY source_file, acct_id;

CREATE OR REPLACE VIEW v_top_payees AS
SELECT payee, charges, total_charged, monthly_equivalent, annual_equivalent,
       cadence, first_seen, last_seen
  FROM v_payee_cadence;

-- ##########################################################################
-- ##  PART 2 ADDITIONS -- 2026-09-10 audit + GUI layer                    ##
-- ##                                                                      ##
-- ##  Everything below hangs off ONE canonical fact view, v_txn. Glance,   ##
-- ##  standard and verbose all select from it, so a drill-down physically  ##
-- ##  cannot disagree with the summary above it: it is the same rows with  ##
-- ##  fewer GROUP BY columns.                                              ##
-- ##                                                                      ##
-- ##  Nothing below writes to claudia_usaa_import. The corrected transfer  ##
-- ##  classification lives in v_txn.is_internal, which supersedes the      ##
-- ##  is_transfer column without rewriting history. The migration that     ##
-- ##  would fold it back into the column is at the bottom, commented out.  ##
-- ##########################################################################


-- ==========================================================================
-- Step 6: period coverage. Derived, never hard-coded.
--
-- The bug this exists to kill: v_year_summary divides a year total by 12 and
-- v_month_summary multiplies a month total by 12, whatever the data actually
-- covers. 2025 holds 290 days, not 365. 2026-09 holds 9 days, not a month.
-- Annualising off those produces a number that is wrong and looks fine.
--
-- Every "both ways" figure below is derived from a measured daily rate:
--     annual  = total / days_covered * 365.25
--     monthly = annual / 12
-- which degrades gracefully on a stub period instead of lying about it.
-- ==========================================================================

CREATE OR REPLACE VIEW v_coverage_overall AS
SELECT MIN(txn_date)                              AS first_date,
       MAX(txn_date)                              AS last_date,
       DATEDIFF(MAX(txn_date), MIN(txn_date)) + 1 AS days_covered,
       ROUND((DATEDIFF(MAX(txn_date), MIN(txn_date)) + 1) / 30.436875, 2) AS months_covered,
       COUNT(DISTINCT ym)                         AS months_present,
       COUNT(*)                                   AS rows_total
  FROM claudia_usaa_import
 WHERE txn_date IS NOT NULL;

-- Per calendar month: how much of it do we actually hold? A month at the edge
-- of the feed is a stub and must not be annualised as if it were whole.
CREATE OR REPLACE VIEW v_coverage_month AS
SELECT m.ym,
       m.month_start,
       LAST_DAY(m.month_start)                                    AS month_end,
       GREATEST(m.month_start, c.first_date)                      AS covered_from,
       LEAST(LAST_DAY(m.month_start), c.last_date)                AS covered_to,
       DAY(LAST_DAY(m.month_start))                               AS days_in_month,
       DATEDIFF(LEAST(LAST_DAY(m.month_start), c.last_date),
                GREATEST(m.month_start, c.first_date)) + 1        AS days_covered,
       CAST(DATEDIFF(LEAST(LAST_DAY(m.month_start), c.last_date),
                GREATEST(m.month_start, c.first_date)) + 1
            = DAY(LAST_DAY(m.month_start)) AS UNSIGNED)           AS is_complete
  FROM (SELECT DISTINCT ym, STR_TO_DATE(CONCAT(ym, '-01'), '%Y-%m-%d') AS month_start
          FROM claudia_usaa_import WHERE txn_date IS NOT NULL) m
 CROSS JOIN v_coverage_overall c;

CREATE OR REPLACE VIEW v_coverage_year AS
SELECT LEFT(ym, 4)         AS yr,
       SUM(days_covered)   AS days_covered,
       SUM(is_complete)    AS complete_months,
       COUNT(*)            AS months_present,
       MIN(covered_from)   AS covered_from,
       MAX(covered_to)     AS covered_to,
       ROUND(SUM(days_covered) / 30.436875, 2) AS months_covered
  FROM v_coverage_month
 GROUP BY yr;


-- ==========================================================================
-- Step 7: cadence, rebuilt.
--
-- Three things the original v_payee_cadence gets wrong and this fixes:
--   1. A repeat charge a few days later at the same amount is a duplicate or
--      a split payment, not a cycle. It dragged HUNTINGTON's average gap down
--      and Appfolio's to 28.8 days. Those gaps are excluded from the rhythm.
--   2. Cadence was classified from a single gap with no stability evidence.
--      Gap spread is now measured and reported.
--   3. The amount-stability test had a GREATEST(1.00, ...) floor, which let
--      any pair of small charges within a dollar of each other through. That
--      is exactly how "3 B Ice Cream" ($5.72 then $6.24) became an annual
--      subscription. The floor is gone; see v_recurring_candidates.
-- ==========================================================================

CREATE OR REPLACE VIEW v_spend_gaps AS
SELECT COALESCE(NULLIF(TRIM(description), ''), '(no description)') AS payee,
       id,
       acct_id,
       COALESCE(NULLIF(TRIM(col_category), ''), 'uncategorised')   AS category,
       txn_date,
       abs_amount,
       LAG(txn_date) OVER w   AS prev_date,
       LAG(abs_amount) OVER w AS prev_amount,
       DATEDIFF(txn_date, LAG(txn_date) OVER w) AS gap_days,
       -- same payee, same amount, inside 5 days: a duplicate or a split, not
       -- a billing cycle. Kept in the money totals, dropped from the rhythm.
       CAST(DATEDIFF(txn_date, LAG(txn_date) OVER w) < 5
            AND LAG(abs_amount) OVER w = abs_amount AS UNSIGNED) AS is_dup_gap
  FROM claudia_usaa_import
 WHERE is_transfer = 0
   AND direction = 'out'
   AND txn_date IS NOT NULL
WINDOW w AS (PARTITION BY COALESCE(NULLIF(TRIM(description), ''), '(no description)')
             ORDER BY txn_date, id);

CREATE OR REPLACE VIEW v_payee_profile AS
SELECT payee,
       COUNT(*)                                                   AS charges,
       SUM(is_dup_gap)                                            AS dup_charges,
       COUNT(DISTINCT acct_id)                                    AS accounts,
       SUBSTRING_INDEX(GROUP_CONCAT(category ORDER BY category), ',', 1) AS category,
       MIN(txn_date)                                              AS first_seen,
       MAX(txn_date)                                              AS last_seen,
       ROUND(AVG(abs_amount), 2)                                  AS avg_amount,
       MIN(abs_amount)                                            AS min_amount,
       MAX(abs_amount)                                            AS max_amount,
       ROUND(MAX(abs_amount) - MIN(abs_amount), 2)                AS amount_spread,
       ROUND(SUM(abs_amount), 2)                                  AS total_charged,
       -- rhythm measured only from real cycles
       ROUND(AVG(CASE WHEN is_dup_gap = 0 THEN gap_days END), 1)  AS avg_gap_days,
       MIN(CASE WHEN is_dup_gap = 0 THEN gap_days END)            AS min_gap_days,
       MAX(CASE WHEN is_dup_gap = 0 THEN gap_days END)            AS max_gap_days,
       ROUND(STDDEV_SAMP(CASE WHEN is_dup_gap = 0 THEN gap_days END), 1) AS gap_stddev,
       COUNT(CASE WHEN is_dup_gap = 0 THEN gap_days END)          AS cycles
  FROM v_spend_gaps
 GROUP BY payee;

-- The most-charged single amount for each payee, and how much of its history
-- sits at that amount. This is the robust replacement for max-minus-min, which
-- one outlier destroys: Spotify's 16 charges look unstable on spread ($6.36 of
-- drift across a price rise) and perfectly stable on mode (the same $12.71
-- billed eight times). modal_n >= 2 -- the same price charged at least twice --
-- is what separates a subscription from a shop you happen to revisit.
CREATE OR REPLACE VIEW v_payee_amount_mode AS
SELECT payee, modal_amount, modal_n, charges,
       ROUND(modal_n / charges, 2) AS modal_share
  FROM (SELECT payee,
               abs_amount AS modal_amount,
               COUNT(*)   AS modal_n,
               SUM(COUNT(*)) OVER (PARTITION BY payee) AS charges,
               ROW_NUMBER() OVER (PARTITION BY payee
                                  ORDER BY COUNT(*) DESC, abs_amount DESC) AS rn
          FROM v_spend_gaps
         GROUP BY payee, abs_amount) x
 WHERE rn = 1;

-- Cadence label + the both-ways normalisation, off the cleaned rhythm.
CREATE OR REPLACE VIEW v_payee_cadence2 AS
SELECT p.*,
       md.modal_amount,
       md.modal_n,
       md.modal_share,
       -- Nearest standard cycle, split at the geometric midpoints, so there
       -- are no holes. The original bands covered 5-9, 12-16, 26-35, 55-70,
       -- 80-100, 170-200 and 330-400 days and dropped everything between:
       -- Netflix bills every 54 days and was called 'irregular' by a gap in
       -- the ladder rather than by anything about Netflix.
       CASE
         WHEN avg_gap_days IS NULL       THEN 'once'
         WHEN avg_gap_days <   4.55      THEN 'irregular'   -- faster than weekly: not a cycle
         WHEN avg_gap_days <   9.90      THEN 'weekly'      -- ~7d
         WHEN avg_gap_days <  20.65      THEN 'biweekly'    -- ~14d
         WHEN avg_gap_days <  43.05      THEN 'monthly'     -- ~30.44d
         WHEN avg_gap_days <  74.55      THEN 'bimonthly'   -- ~60.88d
         WHEN avg_gap_days < 129.14      THEN 'quarterly'   -- ~91.31d
         WHEN avg_gap_days < 258.28      THEN 'semiannual'  -- ~182.63d
         WHEN avg_gap_days <= 529.61     THEN 'annual'      -- ~365.25d
         ELSE 'irregular'                                   -- slower than yearly
       END AS cadence,
       ROUND(avg_amount * 365.25 / NULLIF(avg_gap_days, 0), 2)      AS annual_equivalent,
       ROUND(avg_amount * 365.25 / NULLIF(avg_gap_days, 0) / 12, 2) AS monthly_equivalent,
       -- how regular is the rhythm, 0 = metronomic. Reported, not filtered on.
       ROUND(gap_stddev / NULLIF(avg_gap_days, 0), 2)               AS gap_cv
  FROM v_payee_profile p
  LEFT JOIN v_payee_amount_mode md ON md.payee = p.payee;


-- ==========================================================================
-- Step 8: recurring charges, with every rejection stated out loud.
--
-- v_monthly_subscriptions returns 13 rows, two of which are not subscriptions.
-- This view returns every candidate and, for the ones it drops, the reason.
-- The GUI's verbose level shows the rejects; nothing disappears silently.
--
-- The tests, in the order they fire:
--   1. one charge only            -- cannot measure a rhythm
--   2. no steady rhythm           -- gaps do not land in any cadence band
--   3. too few charges            -- <3, and not a long cadence where 2 is all
--                                    two years of data can ever show
--   4. person-to-person rail      -- Plaid says TRANSFER_*; a friend is not a
--                                    vendor. Kills "APPLE CASH SENT MONEY".
--   5. generic bank label         -- one opaque label covering many merchants
--   6. amount varies              -- spread > 5% of the average, no dollar
--                                    floor. Kills "3 B Ice Cream" (5.72/6.24,
--                                    spread 0.52 vs a 0.30 budget) while
--                                    Incogni (77.88/77.88, spread 0.00) and
--                                    every other genuine annual survives.
--   7. same-day distinct charges  -- a real cycle is never under 5 days
-- ==========================================================================

CREATE OR REPLACE VIEW v_recurring_candidates AS
SELECT c.payee,
       c.category,
       c.cadence,
       c.charges,
       c.cycles,
       c.accounts,
       c.avg_amount        AS charge_amount,
       c.min_amount,
       c.max_amount,
       c.amount_spread,
       c.modal_amount,
       c.modal_n,
       c.modal_share,
       c.avg_gap_days,
       c.min_gap_days,
       c.max_gap_days,
       c.gap_cv,
       c.first_seen,
       c.last_seen,
       c.total_charged,
       c.monthly_equivalent,
       c.annual_equivalent,
       CAST(c.last_seen >= (SELECT MAX(txn_date) FROM claudia_usaa_import)
                          - INTERVAL GREATEST(ROUND(c.avg_gap_days * 2), 1) DAY
            AS UNSIGNED) AS is_active,
       CASE
         WHEN c.cycles >= 6 AND COALESCE(c.gap_cv, 0) <= 0.25 THEN 'high'
         WHEN c.cycles >= 3                                    THEN 'medium'
         ELSE 'low: two charges only, cadence inferred from a single gap'
       END AS confidence,
       CASE
         WHEN c.charges < 2
           THEN 'single charge: no rhythm to measure'
         WHEN c.cadence = 'irregular'
           THEN CONCAT('no steady rhythm: average gap ', c.avg_gap_days, 'd')
         WHEN c.charges < 3 AND c.cadence NOT IN ('annual', 'semiannual')
           THEN CONCAT('only ', c.charges, ' charges for a ', c.cadence, ' claim')
         WHEN c.category IN ('TRANSFER_IN', 'TRANSFER_OUT')
           THEN 'person-to-person transfer, not a vendor'
         WHEN UPPER(c.payee) REGEXP 'RECURRING DEB CARD PUR|DEPOSIT@MOBILE|^[0-9]+$|^PAI ATM|CHECK [0-9]'
           THEN 'generic bank label, not a single merchant'
         -- the same price must have been charged at least twice. A shop you
         -- happened to visit twice a year apart never repeats a price; a
         -- subscription always does. This one test is what drops
         -- "3 B Ice Cream" ($5.72 then $6.24) while keeping Incogni
         -- ($77.88 then $77.88) and every other genuine annual.
         WHEN c.modal_n < 2
           THEN CONCAT('no price ever repeated: ', c.charges, ' charges, ',
                       c.charges, ' different amounts')
         -- past that, either the price is mostly steady, or the rhythm is
         -- metronomic enough over enough cycles to carry a variable bill
         -- (an electricity or insurance bill is recurring but never level).
         WHEN c.modal_share < 0.40
              AND NOT (COALESCE(c.gap_cv, 9) <= 0.40 AND c.cycles >= 6)
           THEN CONCAT('amount and timing both vary: only ',
                       ROUND(c.modal_share * 100), '% of charges at ',
                       c.modal_amount, ', gap variation ', COALESCE(c.gap_cv, 0))
         -- two distinct charges inside five days is only damning when there
         -- is no price evidence either. Adobe and Statewide Proper both bill
         -- twice on the same day and are plainly real.
         WHEN c.min_gap_days < 5 AND c.modal_share < 0.60
           THEN CONCAT('distinct charges ', c.min_gap_days,
                       'd apart with no dominant price: label covers several merchants')
         ELSE NULL
       END AS reject_reason
  FROM v_payee_cadence2 c
 WHERE c.charges >= 2;

-- The accepted set, historic. Used to classify spending as fixed vs variable.
CREATE OR REPLACE VIEW v_recurring_payee AS
SELECT payee, cadence, charge_amount, monthly_equivalent, annual_equivalent
  FROM v_recurring_candidates
 WHERE reject_reason IS NULL;

-- The accepted set, still running. This is the subscriptions list the GUI shows.
CREATE OR REPLACE VIEW v_recurring_charges AS
SELECT payee, category, cadence, confidence, charge_amount, modal_amount,
       modal_share, monthly_equivalent, annual_equivalent,
       charges, cycles, avg_gap_days, gap_cv,
       first_seen, last_seen, total_charged, accounts
  FROM v_recurring_candidates
 WHERE reject_reason IS NULL
   AND is_active = 1;

CREATE OR REPLACE VIEW v_recurring_total AS
SELECT COUNT(*)                          AS recurring_charges,
       ROUND(SUM(monthly_equivalent), 2) AS monthly_total,
       ROUND(SUM(annual_equivalent), 2)  AS annual_total,
       SUM(cadence = 'monthly')          AS monthly_cadence,
       SUM(cadence IN ('annual', 'semiannual')) AS long_cadence
  FROM v_recurring_charges;


-- ==========================================================================
-- Step 9: what the transfer flag misses, as evidence rather than an assertion.
--
-- v_wash_pairs finds every mirrored pair -- equal magnitude, opposite sign,
-- within four days -- that is NOT already flagged. Some are genuine internal
-- movement the regex missed (OD ADVANCE TRANSFER IN/OUT); some are a payment
-- and its own reversal. Both inflate income and spending by the same amount.
-- Nothing here changes a row; it hands the user the list to adjudicate.
-- ==========================================================================

CREATE OR REPLACE VIEW v_wash_pairs AS
SELECT o.txn_date       AS out_date,
       o.acct_id        AS out_acct,
       o.description    AS out_payee,
       o.col_category   AS out_category,
       o.abs_amount     AS amount,
       i.txn_date       AS in_date,
       i.acct_id        AS in_acct,
       i.description    AS in_payee,
       i.col_category   AS in_category,
       DATEDIFF(i.txn_date, o.txn_date) AS days_apart,
       o.id             AS out_id,
       i.id             AS in_id,
       CASE
         WHEN UPPER(o.description) LIKE 'OD ADVANCE%' AND UPPER(i.description) LIKE 'OD ADVANCE%'
           THEN 'internal: overdraft advance between own accounts'
         WHEN o.acct_id = i.acct_id
           THEN 'same account: charge and its own reversal'
         ELSE 'review: equal and opposite across two own accounts'
       END AS verdict
  FROM claudia_usaa_import o
  JOIN claudia_usaa_import i
    ON i.amount = -o.amount
   AND i.id <> o.id
   AND i.txn_date BETWEEN o.txn_date - INTERVAL 4 DAY AND o.txn_date + INTERVAL 4 DAY
 WHERE o.amount < 0
   AND o.is_transfer = 0
   AND i.is_transfer = 0;

-- Are the flagged legs actually paired? An unpaired leg means one side of the
-- move is excluded and the other never existed, so the exclusion is not neutral.
CREATE OR REPLACE VIEW v_transfer_audit AS
SELECT t.id, t.txn_date, t.acct_id, t.description, t.amount,
       CASE WHEN EXISTS (SELECT 1 FROM claudia_usaa_import m
                          WHERE m.is_transfer = 1 AND m.id <> t.id
                            AND m.amount = -t.amount
                            AND m.txn_date BETWEEN t.txn_date - INTERVAL 5 DAY
                                               AND t.txn_date + INTERVAL 5 DAY)
            THEN 'paired' ELSE 'ORPHAN' END AS pairing
  FROM claudia_usaa_import t
 WHERE t.is_transfer = 1;


-- ==========================================================================
-- Step 10: v_txn -- the canonical fact view. THE VERBOSE LEVEL.
--
-- One row per transaction, enriched with everything the GUI filters on and
-- everything an audit needs to answer "why is this number what it is":
-- provenance, account, payee, category, flow classification, amount band,
-- and -- the point of the whole exercise -- excluded_from, which names what
-- this row does NOT contribute to and why.
--
-- counted_as_spend / counted_as_income are 0/1 so every rollup below is a
-- plain SUM over the same column set. There is no second definition of
-- "spending" anywhere in this file.
-- ==========================================================================

CREATE OR REPLACE VIEW v_txn AS
SELECT t.id,
       t.external_id,
       t.source_file,
       t.line_no,
       t.imported_at,
       t.txn_date,
       t.posted_date,
       t.ym,
       LEFT(t.ym, 4)                                   AS yr,
       CONCAT(YEAR(t.txn_date), '-Q', QUARTER(t.txn_date)) AS yq,
       DAYNAME(t.txn_date)                             AS dow,
       t.acct_id,
       t.account_code,
       COALESCE(a.label, t.acct_id)                    AS account_label,
       COALESCE(a.kind, 'other')                       AS account_kind,
       COALESCE(NULLIF(TRIM(t.description), ''), '(no description)') AS payee,
       COALESCE(NULLIF(TRIM(t.col_category), ''), 'uncategorised')   AS category,
       t.col_description                               AS bank_description,
       t.amount,
       t.abs_amount,
       t.direction,
       t.is_transfer                                   AS flagged_transfer,

       -- corrected internal classification. Supersedes is_transfer without
       -- touching it: the flag stays as the loader wrote it, this is derived.
       CAST(t.is_transfer = 1
            OR (UPPER(t.description) LIKE 'OD ADVANCE TRANSFER %'
                AND EXISTS (SELECT 1 FROM claudia_usaa_import m
                             WHERE m.amount = -t.amount
                               AND m.id <> t.id
                               AND UPPER(m.description) LIKE 'OD ADVANCE TRANSFER %'
                               AND m.txn_date BETWEEN t.txn_date - INTERVAL 4 DAY
                                                  AND t.txn_date + INTERVAL 4 DAY))
            AS UNSIGNED) AS is_internal,

       CASE
         WHEN t.is_transfer = 1
           THEN CASE WHEN t.col_category = 'LOAN_PAYMENTS' THEN 'card_payment' ELSE 'internal' END
         WHEN UPPER(t.description) LIKE 'OD ADVANCE TRANSFER %'
              AND EXISTS (SELECT 1 FROM claudia_usaa_import m
                           WHERE m.amount = -t.amount AND m.id <> t.id
                             AND UPPER(m.description) LIKE 'OD ADVANCE TRANSFER %'
                             AND m.txn_date BETWEEN t.txn_date - INTERVAL 4 DAY
                                                AND t.txn_date + INTERVAL 4 DAY)
           THEN 'internal'
         WHEN t.direction = 'in' THEN 'income'
         WHEN COALESCE(NULLIF(TRIM(t.description), ''), '') IN (SELECT payee FROM v_recurring_payee)
           OR t.col_category IN ('LOAN_PAYMENTS', 'RENT_AND_UTILITIES')
           THEN 'fixed'
         ELSE 'variable'
       END AS flow_type,

       -- money in is not all the same kind of money. A TSP withdrawal is your
       -- own savings arriving, not earnings; a dispute credit is a reversal.
       CASE
         WHEN t.direction <> 'in' OR t.is_transfer = 1 THEN NULL
         -- Descriptors, not names. These are how the payer identifies itself on
         -- a US bank statement, so they are the same for everyone who receives
         -- that kind of payment. Add your own employer's wording if it is not
         -- caught here; an unmatched deposit lands in 'other_in', not lost.
         WHEN UPPER(t.description) REGEXP
              'PAYROLL|DIRECT DEP|DIRECT DEPOSIT|SALARY|PAYCHECK|WAGES|BONUS|COMMISSION|DFAS|DEFENSE FINANCE'
                                                                            THEN 'earned'
         WHEN UPPER(t.description) REGEXP
              'VACP|VAED|XXVA|SSA TREAS|SOC SEC|UNEMPLOY|CHILD SUPPORT'     THEN 'benefit'
         WHEN UPPER(t.description) REGEXP
              'TSP TREAS|401K|IRA DISTRIB|ROLLOVER|BROKERAGE TRANSFER'      THEN 'asset_drawdown'
         WHEN UPPER(t.description) REGEXP 'IRS TREAS|TAX REF|STATE TAX REF' THEN 'tax_refund'
         WHEN UPPER(t.description) LIKE 'INTEREST PAID%'                    THEN 'interest'
         WHEN t.col_category = 'LOAN_DISBURSEMENTS'                         THEN 'borrowed'
         WHEN UPPER(t.description) REGEXP 'DISPUTE|REDEMPTION|CREDIT|REFUND' THEN 'refund'
         ELSE 'other_in'
       END AS income_kind,

       CASE
         WHEN t.abs_amount <    10 THEN '1 under $10'
         WHEN t.abs_amount <    25 THEN '2 $10-25'
         WHEN t.abs_amount <    50 THEN '3 $25-50'
         WHEN t.abs_amount <   100 THEN '4 $50-100'
         WHEN t.abs_amount <   250 THEN '5 $100-250'
         WHEN t.abs_amount <   500 THEN '6 $250-500'
         WHEN t.abs_amount <  1000 THEN '7 $500-1k'
         ELSE                           '8 $1k+'
       END AS amount_band,

       CAST(t.is_transfer = 0 AND t.direction = 'out' AS UNSIGNED) AS counted_as_spend,
       CAST(t.is_transfer = 0 AND t.direction = 'in'  AS UNSIGNED) AS counted_as_income,

       -- why a row is not in a total, in words, for the verbose level
       CASE
         WHEN t.is_transfer = 1 AND t.col_category = 'LOAN_PAYMENTS'
           THEN 'excluded from income and spending: payment to your own USAA card, whose purchases are already counted'
         WHEN t.is_transfer = 1
           THEN 'excluded from income and spending: movement between two accounts you hold'
         WHEN t.direction = 'in'  THEN 'counted as money in'
         ELSE                          'counted as money out'
       END AS excluded_from,

       t.parse_ok,
       t.parse_note
  FROM claudia_usaa_import t
  LEFT JOIN account a ON a.acct_id = t.acct_id
 WHERE t.txn_date IS NOT NULL;


-- ==========================================================================
-- Step 11: STANDARD level -- the rollup, its parts, and last period.
--
-- Every one of these is v_txn with a GROUP BY. Drill from any of them into
-- v_txn on the same filter and the rows add up to the number you clicked.
-- Every figure carries a monthly and an annual form, both derived from the
-- month's own measured day count so a stub month cannot lie.
-- ==========================================================================

CREATE OR REPLACE VIEW v_month_flow AS
SELECT t.ym,
       cm.days_covered,
       cm.is_complete,
       SUM(t.counted_as_income * t.abs_amount)                                     AS money_in,
       SUM(t.counted_as_spend  * t.abs_amount)                                     AS money_out,
       SUM(CASE WHEN t.flow_type = 'fixed'    THEN t.abs_amount ELSE 0 END)        AS fixed_out,
       SUM(CASE WHEN t.flow_type = 'variable' THEN t.abs_amount ELSE 0 END)        AS variable_out,
       -- the residual that makes fixed + variable + this = money_out exactly.
       -- Non-zero means the is_transfer flag missed internal movement that
       -- v_txn.is_internal caught. Today: $294.37 of OD ADVANCE transfers.
       SUM(CASE WHEN t.counted_as_spend = 1 AND t.is_internal = 1
                THEN t.abs_amount ELSE 0 END)                                      AS internal_unflagged_out,
       SUM(CASE WHEN t.counted_as_income = 1 AND t.is_internal = 1
                THEN t.abs_amount ELSE 0 END)                                      AS internal_unflagged_in,
       SUM(CASE WHEN t.income_kind = 'earned'  THEN t.abs_amount ELSE 0 END)       AS earned_in,
       SUM(CASE WHEN t.income_kind = 'benefit' THEN t.abs_amount ELSE 0 END)       AS benefit_in,
       SUM(CASE WHEN t.income_kind IN ('asset_drawdown','borrowed') THEN t.abs_amount ELSE 0 END) AS not_really_income,
       SUM(CASE WHEN t.is_internal = 1 THEN t.abs_amount ELSE 0 END)               AS internal_moved,
       SUM(t.counted_as_income * t.abs_amount) - SUM(t.counted_as_spend * t.abs_amount) AS net,
       -- both ways, off the measured daily rate rather than a hard-coded 12
       ROUND(SUM(t.counted_as_spend * t.abs_amount) / cm.days_covered * 365.25, 2)      AS money_out_annual,
       ROUND(SUM(t.counted_as_spend * t.abs_amount) / cm.days_covered * 30.436875, 2)   AS money_out_monthly_rate,
       ROUND(SUM(t.counted_as_income * t.abs_amount) / cm.days_covered * 365.25, 2)     AS money_in_annual,
       ROUND(SUM(t.counted_as_income * t.abs_amount) / cm.days_covered * 30.436875, 2)  AS money_in_monthly_rate,
       ROUND((SUM(t.counted_as_income * t.abs_amount)
              - SUM(t.counted_as_spend * t.abs_amount)) / cm.days_covered * 365.25, 2)  AS net_annual,
       COUNT(*)                                                                    AS txns,
       SUM(t.is_internal)                                                          AS internal_txns
  FROM v_txn t
  JOIN v_coverage_month cm ON cm.ym = t.ym
 GROUP BY t.ym, cm.days_covered, cm.is_complete;

-- Period over period, complete months only so the comparison is like for like.
CREATE OR REPLACE VIEW v_month_compare AS
SELECT ym, is_complete, money_in, money_out, fixed_out, variable_out, net,
       money_out_annual, money_in_annual, net_annual,
       LAG(money_out) OVER w  AS money_out_prev,
       LAG(net)       OVER w  AS net_prev,
       ROUND(money_out - LAG(money_out) OVER w, 2) AS money_out_change,
       ROUND(100 * (money_out - LAG(money_out) OVER w)
             / NULLIF(LAG(money_out) OVER w, 0), 1) AS money_out_change_pct,
       ROUND(net - LAG(net) OVER w, 2)              AS net_change,
       ROUND(AVG(money_out) OVER (ORDER BY ym ROWS BETWEEN 3 PRECEDING AND 1 PRECEDING), 2)
                                                    AS money_out_trailing3
  FROM v_month_flow
 WHERE is_complete = 1
WINDOW w AS (ORDER BY ym);

-- Whole-period truth. This is the number to quote when someone asks
-- "what do I actually spend", because it divides by the days we hold.
CREATE OR REPLACE VIEW v_run_rate AS
SELECT c.first_date, c.last_date, c.days_covered,
       ROUND(SUM(t.counted_as_spend  * t.abs_amount), 2)  AS spent_total,
       ROUND(SUM(t.counted_as_income * t.abs_amount), 2)  AS received_total,
       ROUND(SUM(t.counted_as_spend  * t.abs_amount) / c.days_covered * 30.436875, 2) AS spend_monthly,
       ROUND(SUM(t.counted_as_spend  * t.abs_amount) / c.days_covered * 365.25, 2)    AS spend_annual,
       ROUND(SUM(t.counted_as_income * t.abs_amount) / c.days_covered * 30.436875, 2) AS income_monthly,
       ROUND(SUM(t.counted_as_income * t.abs_amount) / c.days_covered * 365.25, 2)    AS income_annual,
       ROUND(SUM(CASE WHEN t.flow_type = 'fixed'    THEN t.abs_amount ELSE 0 END) / c.days_covered * 30.436875, 2) AS fixed_monthly,
       ROUND(SUM(CASE WHEN t.flow_type = 'fixed'    THEN t.abs_amount ELSE 0 END) / c.days_covered * 365.25, 2)    AS fixed_annual,
       ROUND(SUM(CASE WHEN t.flow_type = 'variable' THEN t.abs_amount ELSE 0 END) / c.days_covered * 30.436875, 2) AS variable_monthly,
       ROUND(SUM(CASE WHEN t.flow_type = 'variable' THEN t.abs_amount ELSE 0 END) / c.days_covered * 365.25, 2)    AS variable_annual,
       ROUND(SUM(CASE WHEN t.counted_as_spend = 1 AND t.is_internal = 1 THEN t.abs_amount ELSE 0 END), 2) AS internal_unflagged_out,
       ROUND((SUM(t.counted_as_income * t.abs_amount) - SUM(t.counted_as_spend * t.abs_amount))
             / c.days_covered * 30.436875, 2) AS net_monthly,
       ROUND((SUM(t.counted_as_income * t.abs_amount) - SUM(t.counted_as_spend * t.abs_amount))
             / c.days_covered * 365.25, 2)    AS net_annual
  FROM v_txn t CROSS JOIN v_coverage_overall c
 GROUP BY c.first_date, c.last_date, c.days_covered;

-- Where the money actually goes, over the whole period, both ways.
CREATE OR REPLACE VIEW v_category_rollup AS
SELECT t.category,
       t.flow_type,
       ROUND(SUM(t.abs_amount), 2)                                  AS spent,
       COUNT(*)                                                     AS charges,
       ROUND(AVG(t.abs_amount), 2)                                  AS avg_charge,
       MAX(t.abs_amount)                                            AS largest,
       COUNT(DISTINCT t.payee)                                      AS payees,
       ROUND(SUM(t.abs_amount) / c.days_covered * 30.436875, 2)     AS monthly,
       ROUND(SUM(t.abs_amount) / c.days_covered * 365.25, 2)        AS annual,
       ROUND(100 * SUM(t.abs_amount)
             / (SELECT SUM(abs_amount) FROM v_txn WHERE counted_as_spend = 1), 1) AS pct_of_spend
  FROM v_txn t CROSS JOIN v_coverage_overall c
 WHERE t.counted_as_spend = 1
 GROUP BY t.category, t.flow_type, c.days_covered;

CREATE OR REPLACE VIEW v_payee_rollup AS
SELECT t.payee,
       t.category,
       t.flow_type,
       COUNT(*)                                                 AS charges,
       ROUND(SUM(t.abs_amount), 2)                              AS spent,
       ROUND(AVG(t.abs_amount), 2)                              AS avg_charge,
       MIN(t.txn_date)                                          AS first_seen,
       MAX(t.txn_date)                                          AS last_seen,
       GROUP_CONCAT(DISTINCT t.account_label ORDER BY t.account_label SEPARATOR ', ') AS accounts,
       ROUND(SUM(t.abs_amount) / c.days_covered * 30.436875, 2) AS monthly,
       ROUND(SUM(t.abs_amount) / c.days_covered * 365.25, 2)    AS annual
  FROM v_txn t CROSS JOIN v_coverage_overall c
 WHERE t.counted_as_spend = 1
 GROUP BY t.payee, t.category, t.flow_type, c.days_covered;

CREATE OR REPLACE VIEW v_account_month AS
SELECT t.acct_id, t.account_label, t.account_kind, t.ym,
       cm.days_covered, cm.is_complete,
       ROUND(SUM(t.counted_as_income * t.abs_amount), 2)        AS money_in,
       ROUND(SUM(t.counted_as_spend  * t.abs_amount), 2)        AS money_out,
       ROUND(SUM(t.is_internal * t.abs_amount), 2)              AS internal_moved,
       ROUND(SUM(t.counted_as_income * t.abs_amount)
             - SUM(t.counted_as_spend * t.abs_amount), 2)       AS net,
       ROUND((SUM(t.counted_as_income * t.abs_amount)
             - SUM(t.counted_as_spend * t.abs_amount)) / cm.days_covered * 30.436875, 2) AS net_monthly_rate,
       ROUND((SUM(t.counted_as_income * t.abs_amount)
             - SUM(t.counted_as_spend * t.abs_amount)) / cm.days_covered * 365.25, 2)    AS net_annual,
       COUNT(*) AS txns
  FROM v_txn t JOIN v_coverage_month cm ON cm.ym = t.ym
 GROUP BY t.acct_id, t.account_label, t.account_kind, t.ym, cm.days_covered, cm.is_complete;

CREATE OR REPLACE VIEW v_amount_band_month AS
SELECT ym, amount_band,
       COUNT(*)                       AS charges,
       ROUND(SUM(abs_amount), 2)      AS spent,
       ROUND(SUM(abs_amount) * 12, 2) AS spent_annual_at_this_rate
  FROM v_txn
 WHERE counted_as_spend = 1
 GROUP BY ym, amount_band;

-- Biggest single movements. This is the query idx_claudia_big was built for.
CREATE OR REPLACE VIEW v_large_txn AS
SELECT txn_date, account_label, payee, category, flow_type, amount, abs_amount,
       amount_band, excluded_from, id
  FROM v_txn
 WHERE abs_amount >= 250;

CREATE OR REPLACE VIEW v_income_detail AS
SELECT t.income_kind, t.payee, t.account_label,
       COUNT(*)                                                 AS receipts,
       ROUND(SUM(t.abs_amount), 2)                              AS received,
       MIN(t.txn_date) AS first_seen, MAX(t.txn_date) AS last_seen,
       ROUND(SUM(t.abs_amount) / c.days_covered * 30.436875, 2) AS monthly,
       ROUND(SUM(t.abs_amount) / c.days_covered * 365.25, 2)    AS annual,
       CASE WHEN t.income_kind IN ('asset_drawdown', 'borrowed')
            THEN 'not earnings: your own money or the bank''s arriving'
            WHEN t.income_kind IN ('refund', 'tax_refund')
            THEN 'not earnings: money coming back'
            WHEN t.income_kind = 'other_in'
            THEN 'unclassified: check what this is before calling it earnings'
            ELSE 'earnings' END AS caveat
  FROM v_txn t CROSS JOIN v_coverage_overall c
 WHERE t.counted_as_income = 1
 GROUP BY t.income_kind, t.payee, t.account_label, c.days_covered;


-- ==========================================================================
-- Step 12: what is unusual this month.
--
-- A category is unusual when this month sits far from its own history in that
-- category's own units. Baseline uses complete months only; the current stub
-- month is measured but never used to set the bar it is judged against.
-- ==========================================================================

CREATE OR REPLACE VIEW v_category_month AS
SELECT t.ym, t.category,
       ROUND(SUM(t.abs_amount), 2) AS spent,
       COUNT(*)                    AS charges,
       cm.is_complete
  FROM v_txn t JOIN v_coverage_month cm ON cm.ym = t.ym
 WHERE t.counted_as_spend = 1
 GROUP BY t.ym, t.category, cm.is_complete;

CREATE OR REPLACE VIEW v_unusual_month AS
SELECT m.ym,
       m.category,
       m.spent,
       m.charges,
       b.baseline_months,
       b.baseline_avg,
       b.baseline_sd,
       ROUND(m.spent - b.baseline_avg, 2) AS delta,
       ROUND(100 * (m.spent - b.baseline_avg) / NULLIF(b.baseline_avg, 0), 1) AS delta_pct,
       ROUND((m.spent - b.baseline_avg) / NULLIF(b.baseline_sd, 0), 2)        AS z,
       ROUND((m.spent - b.baseline_avg) * 12, 2) AS delta_annualised,
       CASE
         WHEN b.baseline_months < 3 THEN 'too little history to judge'
         WHEN b.baseline_sd IS NULL OR b.baseline_sd = 0 THEN 'flat history'
         WHEN (m.spent - b.baseline_avg) / b.baseline_sd >=  2 THEN 'much higher than usual'
         WHEN (m.spent - b.baseline_avg) / b.baseline_sd >=  1 THEN 'higher than usual'
         WHEN (m.spent - b.baseline_avg) / b.baseline_sd <= -2 THEN 'much lower than usual'
         WHEN (m.spent - b.baseline_avg) / b.baseline_sd <= -1 THEN 'lower than usual'
         ELSE 'normal'
       END AS verdict
  FROM v_category_month m
  JOIN (SELECT category,
               COUNT(*)                       AS baseline_months,
               ROUND(AVG(spent), 2)           AS baseline_avg,
               ROUND(STDDEV_SAMP(spent), 2)   AS baseline_sd
          FROM v_category_month
         WHERE is_complete = 1
         GROUP BY category) b USING (category);


-- ==========================================================================
-- Step 13: GLANCE level -- one number, its direction, its trend.
--
-- Long form, one metric per row, so the GUI renders tiles from a single
-- select and every tile carries both a monthly and an annual value. The
-- comparison is last complete month against the one before it; the stub
-- month at the edge of the feed is deliberately not the headline.
-- ==========================================================================

CREATE OR REPLACE VIEW v_glance AS
WITH last2 AS (
  SELECT ym, money_in, money_out, fixed_out, variable_out, net,
         ROW_NUMBER() OVER (ORDER BY ym DESC) AS rn
    FROM v_month_flow WHERE is_complete = 1
),
cur  AS (SELECT * FROM last2 WHERE rn = 1),
prev AS (SELECT * FROM last2 WHERE rn = 2)
SELECT m.metric, m.period, m.monthly, m.annual, m.previous,
       ROUND(m.monthly - m.previous, 2) AS change_abs,
       ROUND(100 * (m.monthly - m.previous) / NULLIF(m.previous, 0), 1) AS change_pct,
       CASE WHEN m.previous IS NULL THEN 'no prior period'
            WHEN m.monthly > m.previous THEN 'up'
            WHEN m.monthly < m.previous THEN 'down'
            ELSE 'flat' END AS direction,
       m.good_when
  FROM (
    SELECT '1 money in'        AS metric, cur.ym AS period, cur.money_in     AS monthly,
           ROUND(cur.money_in * 12, 2)     AS annual, prev.money_in     AS previous, 'up'   AS good_when FROM cur, prev
    UNION ALL
    SELECT '2 money out',      cur.ym, cur.money_out,
           ROUND(cur.money_out * 12, 2),   prev.money_out,   'down' FROM cur, prev
    UNION ALL
    SELECT '3 fixed out',      cur.ym, cur.fixed_out,
           ROUND(cur.fixed_out * 12, 2),   prev.fixed_out,   'down' FROM cur, prev
    UNION ALL
    SELECT '4 variable out',   cur.ym, cur.variable_out,
           ROUND(cur.variable_out * 12, 2), prev.variable_out, 'down' FROM cur, prev
    UNION ALL
    SELECT '5 net',            cur.ym, cur.net,
           ROUND(cur.net * 12, 2),          prev.net,          'up'   FROM cur, prev
    UNION ALL
    SELECT '6 recurring charges', (SELECT MAX(ym) FROM v_month_flow WHERE is_complete = 1),
           (SELECT monthly_total FROM v_recurring_total),
           (SELECT annual_total  FROM v_recurring_total),
           NULL, 'down'
  ) m;


-- ==========================================================================
-- Step 14: indexes, derived from the queries above rather than guessed.
--
-- An index is not a subset of the data. It is a lookup structure over the
-- whole table, it costs something on every write, and every InnoDB secondary
-- index carries a copy of the primary key. So each one below names the query
-- it exists for, and the two removals name why they never earned their keep.
--
-- These are ALTER TABLE ... ADD/DROP KEY only. No row is read or written.
-- Already applied to the live schema; kept here so a clean install matches.
-- ==========================================================================

-- ADDED -----------------------------------------------------------------
-- The GUI's category filter and v_category_rollup / v_month_category.
-- A functional index, because the views expose the cleaned expression rather
-- than the raw column and a plain index on col_category can never be used
-- through COALESCE(NULLIF(TRIM(...))). Measured effect on
-- "SELECT * FROM v_txn WHERE category='FOOD_AND_DRINK'":
--   before  type=ALL,   key=NULL,                     rows=1960
--   after   type=range, key=idx_claudia_category_date, rows=316
-- ALTER TABLE claudia_usaa_import
--   ADD KEY idx_claudia_category_date ((COALESCE(NULLIF(TRIM(col_category),''),'uncategorised')), txn_date);

-- The GUI's payee filter, v_payee_rollup, and the PARTITION BY in
-- v_spend_gaps. Same reasoning. Measured on
-- "SELECT * FROM v_txn WHERE payee='Verizon'":
--   before  type=ALL,   key=NULL,                  rows=1960
--   after   type=range, key=idx_claudia_payee_date, rows=18
-- ALTER TABLE claudia_usaa_import
--   ADD KEY idx_claudia_payee_date ((COALESCE(NULLIF(TRIM(description),''),'(no description)')), txn_date);

-- Every rollup walks non-transfer rows in date order. idx_claudia_transfer
-- (is_transfer, ym) got chosen for these but only ever at key_len=1: the ym
-- half was never used, so it scanned 1943 of 2257 rows through an extra
-- indirection. This one carries direction and abs_amount so the common
-- money-in/money-out aggregate can be answered from the index alone.
-- ALTER TABLE claudia_usaa_import
--   ADD KEY idx_claudia_live_date (is_transfer, txn_date, direction, abs_amount);

-- DROPPED ---------------------------------------------------------------
-- idx_claudia_code_date (account_code, txn_date)
--   account_code is 1:1 with acct_id (account.uq_account_code enforces it),
--   and idx_claudia_acct_date (acct_id, txn_date) is the same access path
--   with the same cardinality (7, 746). Two indexes, one lookup.
-- ALTER TABLE claudia_usaa_import DROP KEY idx_claudia_code_date;

-- idx_claudia_file (source_sha256)
--   uq_claudia_line (source_sha256, line_no) already has source_sha256 as its
--   leftmost column. The optimizer can never prefer this one. It was pure
--   write cost on a CHAR(64).
-- ALTER TABLE claudia_usaa_import DROP KEY idx_claudia_file;

-- KEPT, with the reason ---------------------------------------------------
-- idx_claudia_big (abs_amount, txn_date)
--   Looked dead during the audit because no view used it. v_large_txn now
--   does: "WHERE abs_amount >= 250" plans as type=range, rows=291.
-- idx_claudia_unparsed (parse_ok, source_file)
--   parse_ok has cardinality 1 today because nothing has failed to parse.
--   That is exactly the low-cardinality-but-highly-selective case an index is
--   for: when a load does break, "WHERE parse_ok = 0" must not scan the table.
-- ft_claudia_desc (description, col_original_desc)
--   The only structure that can answer a free-text payee search. The GUI must
--   use MATCH(description, col_original_desc) AGAINST (? IN BOOLEAN MODE)
--   against the base table -- a LIKE '%x%' on the view cannot use it.
--   Note col_original_desc is NULL in all 2257 rows, so half of this index
--   currently indexes nothing.


-- ==========================================================================
-- Step 15: the transfer-rule corrections, NOT run here.
--
-- The audit found the flag misses internal movement. v_txn.is_internal
-- already corrects for it in every view above, so nothing below is required.
-- It is here for whoever decides the column itself should be fixed, and it is
-- commented out because this change was scoped read-only on the data.
--
-- Run v_wash_pairs first and read what it returns before running any of it.
-- ==========================================================================

-- (a) The committed rule and the live database disagree. The rule below flags
--     298 rows; 314 are flagged in the database. The 16 extra are
--     'USAA CREDIT CARD PAYMENT', which the regex does not match at all. On a
--     clean install every USAA card payment would be counted as spending on
--     top of the card purchases it settles. This clause is what is missing:
--
-- UPDATE claudia_usaa_import
--    SET is_transfer = 1
--  WHERE is_transfer = 0
--    AND UPPER(COALESCE(description, col_description, '')) = 'USAA CREDIT CARD PAYMENT';

-- (b) Overdraft advances move money between accounts you hold and are not
--     flagged: 'OD ADVANCE TRANSFER OUT' does not contain 'TRANSFER TO' or
--     'TRANSFER FROM'. Six rows, $294.37 counted as spending and the same
--     $294.37 counted as income.
--
-- UPDATE claudia_usaa_import t
--    SET is_transfer = 1
--  WHERE is_transfer = 0
--    AND UPPER(description) LIKE 'OD ADVANCE TRANSFER %'
--    AND EXISTS (SELECT 1 FROM (SELECT * FROM claudia_usaa_import) m
--                 WHERE m.amount = -t.amount AND m.id <> t.id
--                   AND UPPER(m.description) LIKE 'OD ADVANCE TRANSFER %'
--                   AND m.txn_date BETWEEN t.txn_date - INTERVAL 4 DAY
--                                      AND t.txn_date + INTERVAL 4 DAY);

-- (c) The account-code branch of the rule is both inert and dangerous.
--       ... REGEXP '(<your account codes>)'
--     It matches zero rows that the word branch did not already match, so it
--     contributes nothing. A hand-maintained list also drifts: it will name
--     accounts that were closed while missing ones that were opened later.
--     And a bare four-digit match against a free-text description will
--     eventually flag an order number or a street address as an internal
--     transfer, silently. The
--     branch should be deleted, or rebuilt to read the codes from the account
--     dimension and require a transfer word alongside them:
--
-- UPDATE claudia_usaa_import t
--    SET is_transfer = 1
--  WHERE is_transfer = 0
--    AND UPPER(COALESCE(t.description, t.col_description, '')) REGEXP 'TRANSFER|XFER'
--    AND EXISTS (SELECT 1 FROM account a
--                 WHERE UPPER(COALESCE(t.description, t.col_description, ''))
--                       LIKE CONCAT('%', a.account_code, '%'));

-- (d) The dedupe contract in claudia_usaa_import.sql does not hold.
--     source_sha256 is documented as "hash of the whole file" and
--     uq_claudia_line (source_sha256, line_no) is documented as making a
--     re-run safe. In the live data there are 2257 distinct source_sha256
--     values across 2257 rows and 7 files -- the hash is per row, so that key
--     can never collide and never skips anything. Dedupe currently rests
--     entirely on uq_claudia_external, which happens to be populated for all
--     2257 rows because this data came from Plaid. A CSV feed with no
--     external id would load twice, in full, with no error. Fix the loader,
--     not the schema.
