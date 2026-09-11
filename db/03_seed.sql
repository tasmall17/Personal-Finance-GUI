USE flightdeck;

INSERT INTO account (code, name, kind) VALUES
  ('0626','Checking 0626','checking'),
  ('2745','Bill Acct 2745','checking'),
  ('1011','Mortgage Acct 1011','checking'),
  ('3259','College Acct 3259','savings'),
  ('5334','Car Saving 5334','savings'),
  ('8828','Signature Visa 8828','credit')
ON DUPLICATE KEY UPDATE name = VALUES(name), kind = VALUES(kind);

-- top level mirrors the six colours the Flightdeck page already uses
INSERT INTO category (cat_key, label, kind, sort_order) VALUES
  ('income',    'Income',            'income',   10),
  ('housing',   'Housing',           'fixed',    20),
  ('debt',      'Debt & loans',      'debt',     30),
  ('transport', 'Transportation',    'fixed',    40),
  ('utilsub',   'Utilities & subs',  'fixed',    50),
  ('food',      'Food',              'variable', 60),
  ('shopping',  'Shopping & household','variable',70),
  ('health',    'Health & fitness',  'variable', 80),
  ('savings',   'Savings',           'savings',  90),
  ('transfer',  'Transfers',         'transfer',100),
  ('fees',      'Fees & interest',   'fixed',   110)
ON DUPLICATE KEY UPDATE label = VALUES(label), kind = VALUES(kind);

-- children, so a charge can be precise without losing the roll-up
INSERT INTO category (cat_key, label, kind, sort_order, parent_id) VALUES
  ('mortgage',   'Mortgage & escrow',  'fixed',    21, (SELECT id FROM (SELECT id FROM category WHERE cat_key='housing') x)),
  ('hoa',        'HOA & property mgmt','fixed',    22, (SELECT id FROM (SELECT id FROM category WHERE cat_key='housing') x)),
  ('utilities',  'Utilities',          'fixed',    51, (SELECT id FROM (SELECT id FROM category WHERE cat_key='utilsub') x)),
  ('subscription','Subscriptions',     'fixed',    52, (SELECT id FROM (SELECT id FROM category WHERE cat_key='utilsub') x)),
  ('phone',      'Phone & internet',   'fixed',    53, (SELECT id FROM (SELECT id FROM category WHERE cat_key='utilsub') x)),
  ('groceries',  'Groceries',          'variable', 61, (SELECT id FROM (SELECT id FROM category WHERE cat_key='food') x)),
  ('dining',     'Meals out',          'variable', 62, (SELECT id FROM (SELECT id FROM category WHERE cat_key='food') x)),
  ('fuel',       'Fuel',               'variable', 41, (SELECT id FROM (SELECT id FROM category WHERE cat_key='transport') x)),
  ('carloan',    'Car loan',           'fixed',    42, (SELECT id FROM (SELECT id FROM category WHERE cat_key='transport') x)),
  ('carins',     'Car insurance',      'fixed',    43, (SELECT id FROM (SELECT id FROM category WHERE cat_key='transport') x)),
  ('creditcard', 'Credit card payments','debt',    31, (SELECT id FROM (SELECT id FROM category WHERE cat_key='debt') x))
ON DUPLICATE KEY UPDATE label = VALUES(label);
