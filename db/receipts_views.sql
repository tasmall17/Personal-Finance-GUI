-- Receipt views. Same discipline as the rest: glance, standard and verbose are
-- three readings of one set of rows, every money figure carries a monthly and
-- an annual form, and anything uncertain says so rather than pretending.

-- ---------- GLANCE: did each receipt land cleanly? ------------------------
CREATE OR REPLACE VIEW v_receipt_summary AS
SELECT r.id,
       r.purchased_at,
       r.merchant,
       r.store_no,
       r.total,
       r.items_sold                             AS items_receipt_says,
       COUNT(i.id)                              AS items_parsed,
       ROUND(SUM(CASE WHEN i.is_discount = 0 THEN i.line_total ELSE 0 END), 2) AS gross,
       ROUND(SUM(CASE WHEN i.is_discount = 1 THEN i.line_total ELSE 0 END), 2) AS discounts,
       r.ocr_engine,
       CASE WHEN r.txn_id IS NULL THEN 'unmatched' ELSE 'matched' END AS bank,
       r.match_note,
       CASE WHEN r.parse_ok = 0 THEN 'check'
            WHEN r.items_sold IS NOT NULL AND r.items_sold <> COUNT(i.id) THEN 'item count differs'
            ELSE 'clean' END                    AS health,
       r.parse_note
  FROM receipt r
  LEFT JOIN receipt_item i ON i.receipt_id = r.id
 GROUP BY r.id;

-- ---------- VERBOSE: every line, decoded ----------------------------------
CREATE OR REPLACE VIEW v_receipt_detail AS
SELECT r.id                AS receipt_id,
       r.purchased_at,
       r.merchant,
       i.line_no,
       i.item_code,
       i.description_raw   AS as_printed,
       i.description       AS decoded,
       i.category,
       i.qty,
       i.line_total,
       i.unit_price,
       i.tax_flag,
       CASE WHEN i.tax_flag IS NULL THEN NULL
            WHEN i.tax_flag = 'A' THEN 'taxable'
            WHEN i.tax_flag = 'E' THEN 'tax exempt (usually food)'
            ELSE CONCAT('unrecognised flag ', i.tax_flag) END AS tax_meaning,
       i.price_ending,
       i.price_signal,
       i.is_discount,
       i.raw_line
  FROM receipt_item i
  JOIN receipt r ON r.id = i.receipt_id;

-- ---------- STANDARD: what you buy, by category, both ways ----------------
CREATE OR REPLACE VIEW v_receipt_category_month AS
SELECT DATE_FORMAT(r.purchased_at, '%Y-%m')        AS ym,
       COALESCE(i.category, 'uncategorised')       AS category,
       ROUND(SUM(i.line_total), 2)                 AS spent,
       ROUND(SUM(i.line_total) * 12, 2)            AS spent_annual,
       COUNT(*)                                    AS lines_bought,
       COUNT(DISTINCT r.id)                        AS receipts
  FROM receipt_item i
  JOIN receipt r ON r.id = i.receipt_id
 WHERE r.purchased_at IS NOT NULL
 GROUP BY ym, category;

-- ---------- The honest answer to "could I have got it cheaper?" -----------
-- There is no free, reliable feed of grocery prices, so this does not pretend
-- to know what the shop down the road charges. What it does know exactly is
-- what YOU have paid for the same item over time, which is checkable.
CREATE OR REPLACE VIEW v_item_price_history AS
SELECT i.item_code,
       MAX(i.description)                 AS item,
       MAX(i.category)                    AS category,
       COUNT(*)                           AS times_bought,
       MIN(r.purchased_at)                AS first_bought,
       MAX(r.purchased_at)                AS last_bought,
       MIN(i.unit_price)                  AS cheapest_paid,
       MAX(i.unit_price)                  AS dearest_paid,
       ROUND(AVG(i.unit_price), 4)        AS avg_paid,
       ROUND(MAX(i.unit_price) - MIN(i.unit_price), 2) AS spread,
       ROUND(SUM(i.line_total), 2)        AS spent_total,
       CASE WHEN COUNT(*) < 2 THEN 'only bought once, no comparison possible'
            WHEN MAX(i.unit_price) = MIN(i.unit_price) THEN 'same price every time'
            ELSE CONCAT('price moved ',
                 ROUND(100 * (MAX(i.unit_price) - MIN(i.unit_price)) / MIN(i.unit_price), 1),
                 '% between visits') END   AS verdict
  FROM receipt_item i
  JOIN receipt r ON r.id = i.receipt_id
 WHERE i.is_discount = 0
   AND i.item_code IS NOT NULL
 GROUP BY i.item_code;

-- Did you pay more than your own best price, and by how much?
CREATE OR REPLACE VIEW v_overpaid AS
SELECT r.purchased_at, i.item_code, i.description AS item,
       i.unit_price                              AS paid,
       h.cheapest_paid                           AS your_best_price,
       ROUND(i.unit_price - h.cheapest_paid, 2)  AS over_by,
       ROUND(100 * (i.unit_price - h.cheapest_paid) / h.cheapest_paid, 1) AS over_pct,
       h.times_bought
  FROM receipt_item i
  JOIN receipt r            ON r.id = i.receipt_id
  JOIN v_item_price_history h ON h.item_code = i.item_code
 WHERE i.is_discount = 0
   AND h.times_bought > 1
   AND i.unit_price > h.cheapest_paid;

-- ---------- Deals, from the price-ending convention -----------------------
-- These are shopper conventions, not Costco policy, so the confidence of each
-- signal travels with it.
CREATE OR REPLACE VIEW v_receipt_deals AS
SELECT r.purchased_at, r.merchant, i.item_code,
       i.description AS item, i.line_total, i.price_ending,
       p.meaning, p.detail, p.confidence
  FROM receipt_item i
  JOIN receipt r      ON r.id = i.receipt_id
  JOIN price_signal p ON p.ending = i.price_ending
 WHERE i.is_discount = 0
   AND p.ending <> '99';

-- ---------- Reconciliation: receipts against the bank ---------------------
CREATE OR REPLACE VIEW v_receipt_vs_bank AS
SELECT 'receipt with no bank transaction' AS issue,
       r.id AS receipt_id, r.purchased_at AS dt, r.merchant AS who,
       r.total AS amount, r.match_note AS detail
  FROM receipt r WHERE r.txn_id IS NULL
UNION ALL
SELECT 'bank charge with no receipt uploaded',
       NULL, t.txn_date, t.description, t.abs_amount,
       CONCAT('on ', COALESCE(t.acct_id, '?'))
  FROM claudia_usaa_import t
 WHERE t.is_transfer = 0 AND t.direction = 'out'
   AND UPPER(t.description) REGEXP 'COSTCO|SAM.S CLUB|BJ.S WHOLESALE'
   AND NOT EXISTS (SELECT 1 FROM receipt r WHERE r.txn_id = t.id);

-- ---------- What you actually buy most ------------------------------------
CREATE OR REPLACE VIEW v_receipt_top_items AS
SELECT i.description AS item, i.category,
       COUNT(*) AS times, ROUND(SUM(i.line_total), 2) AS spent,
       ROUND(AVG(i.line_total), 2) AS avg_line,
       ROUND(SUM(i.line_total) / NULLIF(COUNT(DISTINCT DATE_FORMAT(r.purchased_at,'%Y-%m')), 0), 2)
         AS monthly_equivalent,
       ROUND(SUM(i.line_total) / NULLIF(COUNT(DISTINCT DATE_FORMAT(r.purchased_at,'%Y-%m')), 0) * 12, 2)
         AS annual_equivalent
  FROM receipt_item i
  JOIN receipt r ON r.id = i.receipt_id
 WHERE i.is_discount = 0
 GROUP BY i.description, i.category;
