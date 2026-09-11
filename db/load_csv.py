#!/usr/bin/env python3
"""Land USAA CSV exports in the Flightdeck warehouse.

    load_csv.py load  <file-or-directory>...  [--account 0626] [--dry-run]
    load_csv.py promote                       # raw_row -> txn
    load_csv.py classify                      # apply merchant patterns to txn

Every file is hashed, so re-running over the same folder is safe: a file that has
already landed is skipped, and a transaction that already exists is left alone.
The account is read from the file name when it contains one of the account codes
(usaa_0626_2025-01.csv), otherwise pass --account.
"""
import csv, hashlib, io, os, re, sys, glob
from datetime import datetime
import fdb

# --- header shapes seen in USAA exports, normalised to our column names -------
ALIASES = {
    "date": "date", "transaction date": "date", "posting date": "date", "post date": "date",
    "posted date": "posted_date", "effective date": "posted_date",
    "description": "description", "payee": "description", "name": "description",
    "original description": "original_desc", "extended description": "original_desc",
    "memo": "original_desc", "notes": "original_desc",
    "category": "category", "usaa category": "category",
    "amount": "amount", "debit": "debit", "credit": "credit",
    "balance": "balance", "running balance": "balance",
    "status": "status", "transaction type": "status", "type": "status",
}
DATE_FORMATS = ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%d/%m/%Y", "%b %d, %Y", "%m-%d-%Y")

def norm_header(h):
    return ALIASES.get(re.sub(r"\s+", " ", (h or "").strip().lower().lstrip("﻿")))

def parse_date(s):
    s = (s or "").strip()
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            pass
    return None

def parse_amount(s):
    """USAA writes debits as -12.34, (12.34) or 12.34 in a Debit column."""
    if s is None:
        return None
    s = str(s).strip().replace("$", "").replace(",", "").replace("−", "-")
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v

def clean_desc(s):
    """Collapse the noise USAA pads descriptions with, keep the merchant."""
    s = re.sub(r"\s+", " ", (s or "").strip())
    s = re.sub(r"\s+\*{2,}\d+$", "", s)          # trailing masked account
    s = re.sub(r"\b\d{6,}\b", "", s).strip()      # long reference numbers
    return s[:255]

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def account_from_name(path, codes):
    base = os.path.basename(path)
    for code in codes:
        if re.search(r"(?<!\d)" + re.escape(code) + r"(?!\d)", base):
            return code
    return None

def sniff(rows):
    """Return (header_map, first_data_index, layout_name)."""
    for i, row in enumerate(rows[:5]):
        mapped = [norm_header(c) for c in row]
        if mapped.count("date") == 1 and ("amount" in mapped or "debit" in mapped):
            return ({name: idx for idx, name in enumerate(mapped) if name}, i + 1,
                    "header:" + ",".join([m or "?" for m in mapped]))
    # headerless classic USAA: status,,date,,description,category,amount
    for row in rows[:3]:
        if len(row) >= 7 and parse_date(row[2]) and parse_amount(row[6]) is not None:
            return ({"status": 0, "date": 2, "description": 4, "category": 5, "amount": 6},
                    0, "headerless-7col")
    return (None, 0, "unknown")

def load_file(cur, path, account_code, dry=False):
    digest = sha256_file(path)
    cur.execute("SELECT id, filename FROM import_file WHERE sha256=%s", (digest,))
    seen = cur.fetchone()
    if seen:
        return ("skipped", 0, "already loaded as #%s %s" % (seen["id"], seen["filename"]))

    with io.open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        rows = [r for r in csv.reader(fh) if any((c or "").strip() for c in r)]
    if not rows:
        return ("empty", 0, "no rows")

    hmap, start, layout = sniff(rows)
    if not hmap:
        return ("failed", 0, "layout not recognised — first line: " + ",".join(rows[0][:8]))

    def cell(row, key):
        i = hmap.get(key)
        return row[i].strip() if i is not None and i < len(row) else None

    parsed, dates = [], []
    for n, row in enumerate(rows[start:], start=start + 1):
        d = parse_date(cell(row, "date"))
        amt = parse_amount(cell(row, "amount"))
        if amt is None:                                  # split debit/credit columns
            deb, cred = parse_amount(cell(row, "debit")), parse_amount(cell(row, "credit"))
            if deb is not None:
                amt = -abs(deb)
            elif cred is not None:
                amt = abs(cred)
        if d is None or amt is None:
            continue
        parsed.append((n, row, d, amt))
        dates.append(d)
    if not parsed:
        return ("failed", 0, "no parsable rows (layout %s)" % layout)
    if dry:
        return ("dry-run", len(parsed), "%s .. %s as %s" % (min(dates), max(dates), account_code))

    cur.execute(
        "INSERT INTO import_file (path, filename, sha256, account_code, layout, row_count,"
        " first_date, last_date, source) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (path[:512], os.path.basename(path)[:255], digest, account_code, layout[:48],
         len(parsed), min(dates), max(dates), "usaa-csv"))
    file_id = cur.lastrowid
    cur.executemany(
        "INSERT INTO raw_row (file_id, line_no, col_status, col_date, col_posted_date,"
        " col_description, col_original_desc, col_category, col_amount, col_balance, raw_line)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        [(file_id, n, cell(row, "status"), cell(row, "date"), cell(row, "posted_date"),
          cell(row, "description"), cell(row, "original_desc"), cell(row, "category"),
          cell(row, "amount"), cell(row, "balance"), ",".join(row)[:65535])
         for n, row, d, amt in parsed])
    return ("loaded", len(parsed), "%s .. %s as %s" % (min(dates), max(dates), account_code))

def cmd_load(args):
    dry = "--dry-run" in args
    forced = None
    if "--account" in args:
        forced = args[args.index("--account") + 1]
        args = [a for i, a in enumerate(args)
                if a != "--account" and args[i - 1] != "--account"]
    targets = [a for a in args if not a.startswith("--")]

    conn = fdb.connect()
    cur = conn.cursor()
    cur.execute("SELECT code FROM account ORDER BY code")
    codes = [r["code"] for r in cur.fetchall()]

    files = []
    for t in targets:
        if os.path.isdir(t):
            files += sorted(glob.glob(os.path.join(t, "**", "*.csv"), recursive=True))
        else:
            files += sorted(glob.glob(t))
    if not files:
        print("no CSV files found in: " + ", ".join(targets))
        return 1

    totals = {}
    for path in files:                      # the loop over every month, every account
        code = forced or account_from_name(path, codes)
        if not code:
            print("  ?  %-46s no account in the file name — pass --account"
                  % os.path.basename(path)[:46])
            totals["needs account"] = totals.get("needs account", 0) + 1
            continue
        status, n, msg = load_file(cur, path, code, dry)
        totals[status] = totals.get(status, 0) + 1
        print("  %-8s %-46s %5d rows  %s" % (status, os.path.basename(path)[:46], n, msg))
    print("\n" + ", ".join("%s: %d" % kv for kv in sorted(totals.items())))
    return 0

def cmd_promote(_args):
    """raw_row -> txn, with a dedupe hash that still allows genuine same-day repeats."""
    conn = fdb.connect(autocommit=False)
    cur = conn.cursor()
    cur.execute("SELECT id, code FROM account")
    acct = {r["code"]: r["id"] for r in cur.fetchall()}
    cur.execute(
        "SELECT r.id, r.file_id, f.account_code, r.col_date, r.col_amount, r.col_description,"
        "       r.col_original_desc, r.col_category, r.col_status"
        "  FROM raw_row r JOIN import_file f ON f.id = r.file_id"
        " ORDER BY r.file_id, r.line_no")
    rows, seen, batch = cur.fetchall(), {}, []
    current_file = None
    for r in rows:
        d, amt = parse_date(r["col_date"]), parse_amount(r["col_amount"])
        if d is None or amt is None or r["account_code"] not in acct:
            continue
        raw = (r["col_original_desc"] or r["col_description"] or "").strip()
        desc = clean_desc(r["col_description"] or raw)
        # The identity of a charge is the account, the day, the amount and which
        # repeat it is within its own file — never the description, because two
        # feeds word the same charge differently and we would import it twice.
        # Counting resets per file so three real Turkey Hill charges stay three,
        # whether they arrive from one source or from both.
        if r["file_id"] != current_file:
            current_file, seen = r["file_id"], {}
        key = "%s|%s|%.2f" % (r["account_code"], d, amt)
        seen[key] = seen.get(key, 0) + 1                 # nth identical charge that day
        h = hashlib.sha256(("%s|%d" % (key, seen[key])).encode()).hexdigest()
        pending = (r["col_status"] or "").strip().lower() in ("pending", "p")
        batch.append((acct[r["account_code"]], d, amt, desc, raw[:65535],
                      (r["col_category"] or None), 1 if pending else 0,
                      r["file_id"], r["id"], h))
    cur.executemany(
        "INSERT IGNORE INTO txn (account_id, posted_on, amount, description, description_raw,"
        " bank_category, is_pending, file_id, raw_row_id, dedupe_hash)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", batch)
    added = cur.rowcount
    conn.commit()
    print("promoted %d raw rows, %d new transactions" % (len(batch), added))
    return 0

def cmd_classify(_args):
    """Attach merchant and category using merchant_pattern, lowest priority first."""
    conn = fdb.connect(autocommit=False)
    cur = conn.cursor()
    cur.execute(
        "SELECT p.pattern, p.match_kind, p.priority, m.id AS merchant_id, m.category_id"
        "  FROM merchant_pattern p JOIN merchant m ON m.id = p.merchant_id"
        " ORDER BY p.priority, CHAR_LENGTH(p.pattern) DESC")
    pats = cur.fetchall()
    if not pats:
        print("no merchant patterns yet — nothing to classify")
        return 0
    cur.execute("SELECT id, description, description_raw FROM txn")
    hits, total = 0, cur.rowcount
    updates = []
    from merchants import normalise
    for t in cur.fetchall():
        # normalise the haystack the same way the pattern was normalised, or a
        # pattern like "josies pub" can never match "TST*JOSIES PUB 717-826-9952"
        hay = (normalise(t["description"]) + " " + normalise(t["description_raw"])).strip()
        for p in pats:
            pat, kind = p["pattern"].lower(), p["match_kind"]
            ok = (hay.startswith(pat) if kind == "prefix" else
                  hay.strip() == pat if kind == "exact" else
                  bool(re.search(p["pattern"], hay, re.I)) if kind == "regex" else
                  pat in hay)
            if ok:
                updates.append((p["merchant_id"], p["category_id"], t["id"]))
                hits += 1
                break
    cur.executemany("UPDATE txn SET merchant_id=%s, category_id=COALESCE(%s, category_id)"
                    " WHERE id=%s", updates)
    conn.commit()
    print("classified %d transactions, %d matched no pattern" % (hits, total - hits))

    flagged = mark_card_payment_transfers(cur)
    conn.commit()
    print("flagged %d rows as internal credit-card payments" % flagged)
    return 0

def mark_card_payment_transfers(cur):
    """Paying your own credit card is never new spending, and the payment
    landing on the card is never new income - both legs are the same money,
    once. Found by pairing an exact equal-and-opposite amount, a few days
    apart, where at least one leg is an account you hold that is itself a
    credit card. This is safe to run unattended (unlike a general "any two
    equal and opposite amounts" wash-pair sweep, which is left for a human to
    judge) because a mirrored amount against your OWN credit card is not a
    coincidence to weigh - it is what a card payment is, by definition.

    This is what keeps a newly linked card from double-counting: the moment a
    card's own itemized purchases start arriving, the lump payment that used
    to be the only signal of its spending becomes provably a transfer, and
    stops being counted a second time on top of the real purchases."""
    cur.execute("""
        UPDATE txn t
          JOIN txn m ON m.amount = -t.amount AND m.id <> t.id AND m.account_id <> t.account_id
                    AND m.posted_on BETWEEN t.posted_on - INTERVAL 4 DAY AND t.posted_on + INTERVAL 4 DAY
          JOIN account ca ON ca.id = t.account_id
          JOIN account cm ON cm.id = m.account_id
           SET t.is_transfer = 1
         WHERE t.is_transfer = 0 AND ca.is_own = 1 AND cm.is_own = 1
           AND (ca.kind = 'credit' OR cm.kind = 'credit')""")
    return cur.rowcount

if __name__ == "__main__":
    cmds = {"load": cmd_load, "promote": cmd_promote, "classify": cmd_classify}
    if len(sys.argv) < 2 or sys.argv[1] not in cmds:
        print(__doc__)
        sys.exit(1)
    sys.exit(cmds[sys.argv[1]](sys.argv[2:]))
