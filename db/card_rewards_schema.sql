-- What each linked card actually pays back, so a card can be judged against
-- your real spending instead of a gut feeling. Rules live in a table, not
-- code, the same as receipt_abbrev and price_signal: a rate that changes
-- (Discover rotates every quarter) is an UPDATE here, not a code change.
--
-- match_kind decides how a transaction qualifies for a row's rate:
--   category          - the transaction's category.cat_key equals match_value
--   merchant_contains - the transaction's description contains match_value
--                       (case-insensitive) - used where there is no clean
--                       category for the real reward rule (Amex Blue Cash
--                       Everyday's "online retail", Discover's "drug stores")
--   all               - the card's base/everything-else rate, tried last
--
-- Rows are tried most-specific first for a given transaction; the first
-- match wins. confidence='approximate' marks a rule this app cannot verify
-- from bank data alone (Amex Platinum's 5x depends on whether a flight was
-- booked directly or through a third party, which a bank description does
-- not say) so it is never presented with the same certainty as a rate that
-- is simply true of every purchase in a category.

CREATE TABLE IF NOT EXISTS card_reward_rule (
  id                INT UNSIGNED NOT NULL AUTO_INCREMENT,
  account_code      VARCHAR(8)   NOT NULL,              -- matches account.code
  label             VARCHAR(80)  NOT NULL,              -- "US supermarkets", "Gas stations", ...
  match_kind        ENUM('category','merchant_contains','all') NOT NULL DEFAULT 'category',
  match_value       VARCHAR(120)     NULL,              -- a category.cat_key, or a description substring
  rate_pct          DECIMAL(5,2) NOT NULL,              -- 3.00 = 3%
  reward_kind       ENUM('cashback','points') NOT NULL DEFAULT 'cashback',
  point_value_cents DECIMAL(5,2) NOT NULL DEFAULT 1.00, -- assumed cents per point - only matters for points cards
  annual_cap        DECIMAL(10,2)    NULL,              -- $ of spend the elevated rate applies to per calendar year
  quarter_cap       DECIMAL(10,2)    NULL,              -- $ of spend per calendar quarter (Discover-style rotation)
  active_from       DATE             NULL,              -- for a rotating category; NULL = always on
  active_to         DATE             NULL,
  confidence        ENUM('published','approximate') NOT NULL DEFAULT 'published',
  source            VARCHAR(255)     NULL,              -- where the rate came from
  notes             VARCHAR(255)     NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_reward_account_label (account_code, label),
  KEY idx_reward_account (account_code, match_kind)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- No rows ship pre-loaded: a reward rate only means something tied to an
-- account_code that actually exists in your own `account` table, which is
-- different for every install. Add a row per real rate on your own linked
-- cards, using whichever account code Settings shows for that card. For
-- example, if you had a card with account_code 'XXXX' offering 3% cash back
-- at US supermarkets and 1% on everything else, capped at $6,000/year of
-- elevated spend:
--
-- INSERT INTO card_reward_rule
--   (account_code, label, match_kind, match_value, rate_pct, reward_kind, annual_cap, confidence, source) VALUES
--   ('XXXX', 'US supermarkets', 'category', 'groceries', 3.00, 'cashback', 6000.00, 'published', 'your card issuer''s own terms page'),
--   ('XXXX', 'Everything else', 'all', NULL, 1.00, 'cashback', NULL, 'published', 'your card issuer''s own terms page');
--
-- Look up your own card's real published rates before adding rows - a rate
-- typed in here is presented as fact on the Card Fit tab, so it should be one.
