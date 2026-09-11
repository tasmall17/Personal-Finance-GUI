-- claudia_usaa_import — a standalone raw landing table for USAA CSV exports.
--
-- Deliberately self-contained: no foreign keys, no dependency on the flightdeck
-- schema. It holds what the bank actually exported, verbatim, so that anything
-- derived from it can always be rebuilt without going back to the bank.
--
-- Run it against whichever schema is selected in Workbench. It does not create
-- or choose a database of its own.
--
-- Money is DECIMAL throughout. Nothing here ever passes through a binary float.

CREATE TABLE IF NOT EXISTS claudia_usaa_import (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,

  -- ---- provenance: which file, which line, which account ------------------
  source_file    VARCHAR(255)  NOT NULL,          -- basename as exported
  source_sha256  CHAR(64)      NOT NULL,          -- hash of the whole file
  line_no        INT UNSIGNED  NOT NULL,          -- 1-based line within that file
  external_id    VARCHAR(64)       NULL,          -- the feed's own stable id, when it has one
  acct_id        VARCHAR(40)       NULL,          -- readable key: '1 day-to-day-spending'
  account_code   VARCHAR(8)        NULL,          -- last four digits, e.g. 4417
  layout         VARCHAR(48)       NULL,          -- which export shape was detected

  -- ---- verbatim, exactly as the bank wrote it -----------------------------
  col_status         VARCHAR(64)   NULL,
  col_date           VARCHAR(32)   NULL,
  col_posted_date    VARCHAR(32)   NULL,
  col_description    TEXT          NULL,
  col_original_desc  TEXT          NULL,
  col_category       VARCHAR(128)  NULL,          -- USAA's own guess, kept for reference
  col_amount         VARCHAR(32)   NULL,
  col_balance        VARCHAR(32)   NULL,
  raw_line           TEXT      NOT NULL,          -- the entire original line

  -- ---- parsed, and exact ---------------------------------------------------
  txn_date       DATE              NULL,
  posted_date    DATE              NULL,
  amount         DECIMAL(7,2)      NULL,          -- signed; negative is money out. caps at 99,999.99
  balance        DECIMAL(12,2)     NULL,          -- stays wide: a mortgage balance is six figures
  description    VARCHAR(255)      NULL,          -- cleaned for reading

  -- ---- generated, so month and direction rollups are indexable -------------
  ym             CHAR(7)  GENERATED ALWAYS AS (DATE_FORMAT(txn_date,'%Y-%m')) STORED,
  direction      ENUM('in','out') GENERATED ALWAYS AS (IF(amount < 0,'out','in')) STORED,
  abs_amount     DECIMAL(7,2) GENERATED ALWAYS AS (ABS(amount)) STORED,

  -- ---- the one curated field, and a rule below that actually writes it -----
  is_transfer    TINYINT(1)    NOT NULL DEFAULT 0,   -- money between your own accounts

  -- ---- did the loader understand this line? -------------------------------
  parse_ok       TINYINT(1)    NOT NULL DEFAULT 1,
  parse_note     VARCHAR(255)      NULL,          -- why not, when parse_ok = 0
  imported_at    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,

  PRIMARY KEY (id),

  -- re-running the loader over the same folder is safe: a line that already
  -- landed collides here and is skipped rather than duplicated
  UNIQUE KEY uq_claudia_line (source_sha256, line_no),

  -- a feed that issues its own ids (Plaid does) dedupes on that instead, so a
  -- re-sync updates the row it already has rather than making a second one
  UNIQUE KEY uq_claudia_external (external_id),

  KEY idx_claudia_acct_date  (acct_id, txn_date),        -- one account's statement, in order
  KEY idx_claudia_code_date  (account_code, txn_date),
  KEY idx_claudia_date       (txn_date),
  KEY idx_claudia_ym_dir     (ym, direction, amount),
  KEY idx_claudia_transfer   (is_transfer, ym),
  KEY idx_claudia_big        (abs_amount, txn_date),
  KEY idx_claudia_unparsed   (parse_ok, source_file),
  KEY idx_claudia_file       (source_sha256),
  FULLTEXT KEY ft_claudia_desc (description, col_original_desc)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- After loading, this is the first thing worth running:
--
--   SELECT account_code, MIN(txn_date), MAX(txn_date), COUNT(*),
--          SUM(parse_ok = 0) AS unparsed
--     FROM claudia_usaa_import
--    GROUP BY acct_id ORDER BY acct_id;
