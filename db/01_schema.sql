-- Flightdeck finance warehouse
-- Layer 1 (base)     : import_file, raw_row  — a faithful copy of every CSV line
-- Layer 2 (curated)  : account, category, merchant, txn, recurring, budget_line
-- Layer 3 (views)    : monthly rollups and the review queues
-- Rebuild rule: layer 2 is derived from layer 1 and can always be rebuilt from it.

CREATE DATABASE IF NOT EXISTS flightdeck
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE flightdeck;

-- ============================== layer 1: base ==============================

CREATE TABLE IF NOT EXISTS import_file (
  id            INT UNSIGNED NOT NULL AUTO_INCREMENT,
  path          VARCHAR(512)  NOT NULL,
  filename      VARCHAR(255)  NOT NULL,
  sha256        CHAR(64)      NOT NULL,
  account_code  VARCHAR(8)        NULL,   -- 0626, 2745, 1011, 3259, 5334
  layout        VARCHAR(48)   NOT NULL,   -- which export shape was detected
  row_count     INT UNSIGNED  NOT NULL DEFAULT 0,
  first_date    DATE              NULL,
  last_date     DATE              NULL,
  source        VARCHAR(32)   NOT NULL DEFAULT 'usaa-csv',
  imported_at   DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_import_sha (sha256),          -- same file never lands twice
  KEY idx_import_account_span (account_code, first_date, last_date)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS raw_row (
  id                 BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  file_id            INT UNSIGNED    NOT NULL,
  line_no            INT UNSIGNED    NOT NULL,
  col_status         VARCHAR(64)         NULL,
  col_date           VARCHAR(32)         NULL,
  col_posted_date    VARCHAR(32)         NULL,
  col_description    TEXT                NULL,
  col_original_desc  TEXT                NULL,
  col_category       VARCHAR(128)        NULL,
  col_amount         VARCHAR(32)         NULL,
  col_balance        VARCHAR(32)         NULL,
  raw_line           TEXT            NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_raw_row (file_id, line_no),
  CONSTRAINT fk_raw_file FOREIGN KEY (file_id) REFERENCES import_file(id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- ============================ layer 2: curated =============================

CREATE TABLE IF NOT EXISTS account (
  id           SMALLINT UNSIGNED NOT NULL AUTO_INCREMENT,
  code         VARCHAR(8)   NOT NULL,          -- last four, as Flightdeck names them
  name         VARCHAR(64)  NOT NULL,
  kind         ENUM('checking','savings','credit','loan','mortgage','other') NOT NULL,
  institution  VARCHAR(64)  NOT NULL DEFAULT 'USAA',
  is_own       TINYINT(1)   NOT NULL DEFAULT 1, -- own account => transfers, not spending
  opened_on    DATE             NULL,
  closed_on    DATE             NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_account_code (code)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS category (
  id         SMALLINT UNSIGNED NOT NULL AUTO_INCREMENT,
  cat_key    VARCHAR(32)  NOT NULL,
  label      VARCHAR(64)  NOT NULL,
  parent_id  SMALLINT UNSIGNED NULL,
  kind       ENUM('income','fixed','variable','debt','savings','transfer') NOT NULL,
  sort_order SMALLINT     NOT NULL DEFAULT 100,
  PRIMARY KEY (id),
  UNIQUE KEY uq_category_key (cat_key),
  KEY idx_category_parent (parent_id),
  CONSTRAINT fk_category_parent FOREIGN KEY (parent_id) REFERENCES category(id) ON DELETE SET NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS merchant (
  id           INT UNSIGNED NOT NULL AUTO_INCREMENT,
  name         VARCHAR(128) NOT NULL,           -- the display name you approve
  category_id  SMALLINT UNSIGNED NULL,
  is_recurring TINYINT(1)   NOT NULL DEFAULT 0,
  is_bill      TINYINT(1)   NOT NULL DEFAULT 0, -- shows on the Bills tab
  reviewed     TINYINT(1)   NOT NULL DEFAULT 0, -- flipped when you sign the row off
  notes        VARCHAR(255)     NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_merchant_name (name),
  KEY idx_merchant_category (category_id, name),
  KEY idx_merchant_review (reviewed, is_recurring),
  CONSTRAINT fk_merchant_category FOREIGN KEY (category_id) REFERENCES category(id) ON DELETE SET NULL
) ENGINE=InnoDB;

-- how a bank description resolves to a merchant; lowest priority number wins
CREATE TABLE IF NOT EXISTS merchant_pattern (
  id          INT UNSIGNED NOT NULL AUTO_INCREMENT,
  merchant_id INT UNSIGNED NOT NULL,
  match_kind  ENUM('exact','prefix','contains','regex') NOT NULL DEFAULT 'contains',
  pattern     VARCHAR(255) NOT NULL,
  priority    SMALLINT     NOT NULL DEFAULT 100,
  PRIMARY KEY (id),
  UNIQUE KEY uq_pattern (match_kind, pattern),
  KEY idx_pattern_merchant (merchant_id, priority),
  CONSTRAINT fk_pattern_merchant FOREIGN KEY (merchant_id) REFERENCES merchant(id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS txn (
  id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  account_id      SMALLINT UNSIGNED NOT NULL,
  posted_on       DATE            NOT NULL,
  amount          DECIMAL(12,2)   NOT NULL,        -- signed; negative is money out
  description     VARCHAR(255)    NOT NULL,        -- cleaned for reading
  description_raw TEXT            NOT NULL,        -- exactly what the bank sent
  merchant_id     INT UNSIGNED        NULL,
  category_id     SMALLINT UNSIGNED   NULL,
  bank_category   VARCHAR(128)        NULL,        -- USAA's own guess, kept for reference
  is_transfer     TINYINT(1)      NOT NULL DEFAULT 0,
  transfer_pair   BIGINT UNSIGNED     NULL,        -- the other leg, once matched
  is_pending      TINYINT(1)      NOT NULL DEFAULT 0,
  file_id         INT UNSIGNED        NULL,
  raw_row_id      BIGINT UNSIGNED     NULL,
  dedupe_hash     CHAR(64)        NOT NULL,        -- account+date+amount+desc+nth
  -- generated columns so month and direction rollups are indexable, not computed per query
  ym              CHAR(7)  GENERATED ALWAYS AS (DATE_FORMAT(posted_on,'%Y-%m')) STORED,
  direction       ENUM('in','out') GENERATED ALWAYS AS (IF(amount < 0,'out','in')) STORED,
  abs_amount      DECIMAL(12,2) GENERATED ALWAYS AS (ABS(amount)) STORED,
  PRIMARY KEY (id),
  UNIQUE KEY uq_txn_dedupe (dedupe_hash),
  KEY idx_txn_account_date  (account_id, posted_on),      -- account statement view
  KEY idx_txn_date          (posted_on),                  -- everything in a window
  KEY idx_txn_ym_category   (ym, category_id, amount),     -- monthly category rollup, covering
  KEY idx_txn_merchant_date (merchant_id, posted_on),      -- one merchant's history
  KEY idx_txn_category_date (category_id, posted_on),
  KEY idx_txn_direction_ym  (direction, ym),               -- income vs spend by month
  KEY idx_txn_big           (abs_amount, posted_on),       -- largest charges first
  KEY idx_txn_unassigned    (merchant_id, category_id, posted_on), -- the review queue
  KEY idx_txn_file          (file_id),
  FULLTEXT KEY ft_txn_description (description, description_raw),
  CONSTRAINT fk_txn_account  FOREIGN KEY (account_id)  REFERENCES account(id),
  CONSTRAINT fk_txn_merchant FOREIGN KEY (merchant_id) REFERENCES merchant(id) ON DELETE SET NULL,
  CONSTRAINT fk_txn_category FOREIGN KEY (category_id) REFERENCES category(id) ON DELETE SET NULL,
  CONSTRAINT fk_txn_file     FOREIGN KEY (file_id)     REFERENCES import_file(id) ON DELETE SET NULL
) ENGINE=InnoDB;

-- what actually repeats, derived from txn and confirmed by you
CREATE TABLE IF NOT EXISTS recurring (
  id              INT UNSIGNED NOT NULL AUTO_INCREMENT,
  merchant_id     INT UNSIGNED NOT NULL,
  account_id      SMALLINT UNSIGNED NULL,
  cadence         ENUM('weekly','biweekly','monthly','quarterly','semiannual','annual','irregular')
                  NOT NULL DEFAULT 'monthly',
  expected_amount DECIMAL(12,2)    NULL,
  amount_low      DECIMAL(12,2)    NULL,
  amount_high     DECIMAL(12,2)    NULL,
  day_of_month    TINYINT UNSIGNED NULL,
  occurrences     SMALLINT UNSIGNED NOT NULL DEFAULT 0,
  first_seen      DATE NULL,
  last_seen       DATE NULL,
  status          ENUM('active','lapsed','cancelled','candidate') NOT NULL DEFAULT 'candidate',
  PRIMARY KEY (id),
  UNIQUE KEY uq_recurring (merchant_id, account_id),
  KEY idx_recurring_status (status, last_seen),
  CONSTRAINT fk_recurring_merchant FOREIGN KEY (merchant_id) REFERENCES merchant(id) ON DELETE CASCADE,
  CONSTRAINT fk_recurring_account  FOREIGN KEY (account_id)  REFERENCES account(id)  ON DELETE SET NULL
) ENGINE=InnoDB;

-- the plan side: exactly what the Flightdeck page shows on Bills and Spending
CREATE TABLE IF NOT EXISTS budget_line (
  id          INT UNSIGNED NOT NULL AUTO_INCREMENT,
  code        VARCHAR(16)  NOT NULL,            -- matches the row ids in the page (mtg, hoa, net)
  label       VARCHAR(64)  NOT NULL,
  grp         VARCHAR(32)  NOT NULL,            -- Housing, Utilities, Subscriptions, Debt
  kind        ENUM('income','bill','spending','savings') NOT NULL DEFAULT 'bill',
  category_id SMALLINT UNSIGNED NULL,
  account_id  SMALLINT UNSIGNED NULL,
  merchant_id INT UNSIGNED      NULL,
  amount      DECIMAL(12,2) NOT NULL DEFAULT 0, -- monthly share
  cadence     ENUM('monthly','quarterly','semiannual','annual') NOT NULL DEFAULT 'monthly',
  due_day     TINYINT UNSIGNED NULL,
  note        VARCHAR(255) NULL,
  active      TINYINT(1)   NOT NULL DEFAULT 1,
  PRIMARY KEY (id),
  UNIQUE KEY uq_budget_code (code),
  KEY idx_budget_group (active, grp, amount),
  CONSTRAINT fk_budget_category FOREIGN KEY (category_id) REFERENCES category(id) ON DELETE SET NULL,
  CONSTRAINT fk_budget_account  FOREIGN KEY (account_id)  REFERENCES account(id)  ON DELETE SET NULL,
  CONSTRAINT fk_budget_merchant FOREIGN KEY (merchant_id) REFERENCES merchant(id) ON DELETE SET NULL
) ENGINE=InnoDB;
