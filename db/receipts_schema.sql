-- Receipts: what you actually bought, as opposed to what the bank saw.
--
-- A bank transaction says "COSTCO WHSE #1067  737.92". A receipt says which
-- eleven things that was. This is the layer that answers "what did I spend on
-- food" rather than "what did I spend at the grocery store".
--
-- Run against the same schema as claudia_usaa_import.

CREATE TABLE IF NOT EXISTS receipt (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,

  -- provenance: the uploaded file itself
  source_file    VARCHAR(255)  NOT NULL,
  source_sha256  CHAR(64)      NOT NULL,          -- re-uploading the same file is a no-op
  mime           VARCHAR(40)       NULL,
  ocr_engine     VARCHAR(24)       NULL,          -- pdftotext | vision | manual

  -- what the receipt says
  merchant       VARCHAR(80)       NULL,
  store_no       VARCHAR(16)       NULL,
  purchased_at   DATETIME          NULL,
  subtotal       DECIMAL(9,2)      NULL,
  tax            DECIMAL(9,2)      NULL,
  total          DECIMAL(9,2)      NULL,
  items_sold     SMALLINT UNSIGNED NULL,          -- the receipt's own count, to check ours against
  payment_last4  VARCHAR(4)        NULL,

  -- the bank transaction this receipt explains, once matched
  txn_id         BIGINT UNSIGNED   NULL,
  match_note     VARCHAR(120)      NULL,

  -- OCR is lossy. Keep everything, and say so when it went wrong.
  parse_ok       TINYINT(1)    NOT NULL DEFAULT 1,
  parse_note     VARCHAR(255)      NULL,
  raw_text       MEDIUMTEXT        NULL,
  uploaded_at    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,

  PRIMARY KEY (id),
  UNIQUE KEY uq_receipt_sha (source_sha256),
  KEY idx_receipt_date     (purchased_at),
  KEY idx_receipt_merchant (merchant, purchased_at),
  KEY idx_receipt_txn      (txn_id),
  KEY idx_receipt_unparsed (parse_ok, uploaded_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS receipt_item (
  id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  receipt_id      BIGINT UNSIGNED NOT NULL,
  line_no         INT UNSIGNED    NOT NULL,

  item_code       VARCHAR(20)         NULL,       -- Costco's own item number
  description_raw VARCHAR(160)        NULL,       -- exactly as printed
  description     VARCHAR(160)        NULL,       -- abbreviations expanded
  qty             DECIMAL(9,3)    NOT NULL DEFAULT 1.000,
  line_total      DECIMAL(9,2)        NULL,       -- negative for a discount line
  unit_price      DECIMAL(11,4)       NULL,       -- line_total / qty, for comparing sizes
  tax_flag        VARCHAR(2)          NULL,       -- A taxable, E exempt, per state

  -- the price-ending convention. Shopper lore, not official Costco policy,
  -- so it is recorded as a signal to weigh, never as a fact.
  price_ending    CHAR(2)             NULL,
  price_signal    VARCHAR(28)         NULL,

  is_discount     TINYINT(1)      NOT NULL DEFAULT 0,
  discount_for    VARCHAR(20)         NULL,       -- item_code the discount applies to
  category        VARCHAR(32)         NULL,       -- food, household, electronics, ...
  raw_line        VARCHAR(255)        NULL,

  PRIMARY KEY (id),
  UNIQUE KEY uq_receipt_line (receipt_id, line_no),
  KEY idx_item_code   (item_code, receipt_id),    -- one product's price history
  KEY idx_item_desc   (description),
  KEY idx_item_cat    (category, receipt_id),
  KEY idx_item_disc   (is_discount),
  CONSTRAINT fk_item_receipt FOREIGN KEY (receipt_id) REFERENCES receipt(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Abbreviations live in a table, not in code, so you can correct them without
-- touching the parser. Costco publishes no official legend; these are the
-- widely reported ones plus whatever you add.
CREATE TABLE IF NOT EXISTS receipt_abbrev (
  abbrev     VARCHAR(24)  NOT NULL,
  expansion  VARCHAR(64)  NOT NULL,
  merchant   VARCHAR(40)  NOT NULL DEFAULT 'COSTCO',
  category   VARCHAR(32)      NULL,               -- what this usually implies
  confidence ENUM('documented','widely-reported','guess') NOT NULL DEFAULT 'widely-reported',
  PRIMARY KEY (abbrev, merchant)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

INSERT IGNORE INTO receipt_abbrev (abbrev, expansion, category, confidence) VALUES
  ('KS','Kirkland Signature','','widely-reported'),
  ('KIRK','Kirkland Signature','','widely-reported'),
  ('ORG','Organic','food','widely-reported'),
  ('ORGN','Organic','food','widely-reported'),
  ('CHKN','Chicken','food','widely-reported'),
  ('ROTISS','Rotisserie','food','widely-reported'),
  ('BF','Beef','food','widely-reported'),
  ('BNLS','Boneless','food','widely-reported'),
  ('SKNLS','Skinless','food','widely-reported'),
  ('BRST','Breast','food','widely-reported'),
  ('GRND','Ground','food','widely-reported'),
  ('CHZ','Cheese','food','widely-reported'),
  ('YOG','Yogurt','food','widely-reported'),
  ('VEG','Vegetable','food','widely-reported'),
  ('FRZ','Frozen','food','widely-reported'),
  ('FRSH','Fresh','food','widely-reported'),
  ('WTR','Water','food','widely-reported'),
  ('BTL','Bottle','','widely-reported'),
  ('CT','Count','','documented'),
  ('PK','Pack','','documented'),
  ('OZ','Ounce','','documented'),
  ('LB','Pound','','documented'),
  ('TP','Toilet Paper','household','widely-reported'),
  ('PPR','Paper','household','widely-reported'),
  ('TWL','Towel','household','widely-reported'),
  ('DTRGNT','Detergent','household','widely-reported'),
  ('BATT','Battery','household','widely-reported'),
  ('SM','Small','','widely-reported'),
  ('LG','Large','','widely-reported'),
  ('XL','Extra Large','','widely-reported');

-- Store-agnostic abbreviations, not specific to Costco's own printing. Kept as a
-- separate merchant so a generic guess never overwrites a Costco-verified one --
-- receipt_ingest.py loads a receipt's own merchant plus 'GENERIC', in that order,
-- so a merchant-specific row always wins when both exist for the same token.
--
-- Sourced two ways: the legend line and item-category text bundled in the
-- RecieptsParse/OCR_TO_JSON capture under fi-professor/ocr ("ORG - Organic
-- VG - Vegetable PK - Pack GF - gluten free SF - sugar free", plus the grocery
-- and beverage category blobs it trains its classifier on), and a few pulled
-- from this database's own first uploaded receipt (KS FR 2DZ, PAM SPRY 2PK,
-- B/S THIGH) where the split was unambiguous. None of it is Costco's own
-- documentation, so nothing here is graded 'documented'.
INSERT IGNORE INTO receipt_abbrev (abbrev, expansion, merchant, category, confidence) VALUES
  ('VG','Vegetable','GENERIC','food','widely-reported'),
  ('GF','Gluten Free','GENERIC','food','widely-reported'),
  ('SF','Sugar Free','GENERIC','food','widely-reported'),
  ('OJ','Orange Juice','GENERIC','food','widely-reported'),
  ('DZ','Dozen','GENERIC','food','widely-reported'),
  ('GRK','Greek','GENERIC','food','guess'),
  ('YGRT','Yogurt','GENERIC','food','guess'),
  ('SNCK','Snack','GENERIC','food','guess'),
  ('RSTD','Roasted','GENERIC','food','guess'),
  ('PNUT','Peanut','GENERIC','food','guess'),
  ('BTTR','Butter','GENERIC','food','guess'),
  ('SR','Sour','GENERIC','food','guess'),
  ('CRM','Cream','GENERIC','food','guess'),
  ('CCNUT','Coconut','GENERIC','food','guess'),
  ('SLD','Salad','GENERIC','food','guess'),
  ('SHRT','Short','GENERIC','food','guess'),
  ('MSH','Mashed','GENERIC','food','guess'),
  ('POT','Potato','GENERIC','food','guess'),
  ('BURG','Burger','GENERIC','food','guess'),
  ('STK','Steak','GENERIC','food','guess'),
  ('FRT','Fruit','GENERIC','food','guess'),
  ('CP','Cup','GENERIC','food','guess'),
  ('SPRKL','Sparkling','GENERIC','food','guess'),
  ('ALM','Almond','GENERIC','food','guess'),
  ('MLK','Milk','GENERIC','food','guess'),
  ('ESP','Espresso','GENERIC','food','guess'),
  ('TORT','Tortilla','GENERIC','food','guess'),
  ('BS','Boneless Skinless','GENERIC','food','guess'),
  ('SPRY','Spray','GENERIC','','guess'),
  ('INF','Infant','GENERIC','baby','guess'),
  ('BBY','Baby','GENERIC','baby','guess'),
  ('HLMRK','Hallmark','GENERIC','','guess');

-- The price-ending convention, same treatment: data, with its confidence stated.
CREATE TABLE IF NOT EXISTS price_signal (
  ending     CHAR(2)      NOT NULL,
  meaning    VARCHAR(28)  NOT NULL,
  detail     VARCHAR(160) NOT NULL,
  confidence ENUM('documented','widely-reported','guess') NOT NULL DEFAULT 'widely-reported',
  PRIMARY KEY (ending)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

INSERT IGNORE INTO price_signal (ending, meaning, detail, confidence) VALUES
  ('99','full price','Standard Costco price. No markdown.','widely-reported'),
  ('97','manager markdown','Marked down by the warehouse. Often the best deal on the floor.','widely-reported'),
  ('49','manufacturer deal','Reported to indicate a manufacturer discount rather than a store one.','widely-reported'),
  ('59','manufacturer deal','Reported to indicate a manufacturer discount rather than a store one.','widely-reported'),
  ('79','manufacturer deal','Reported to indicate a manufacturer discount rather than a store one.','widely-reported'),
  ('89','manufacturer deal','Reported to indicate a manufacturer discount rather than a store one.','widely-reported'),
  ('00','last chance','Deep markdown. Usually the final clearance before the item goes.','widely-reported');
