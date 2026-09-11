#!/usr/bin/env python3
"""Read a receipt (PDF or photo) and land it in the warehouse.

    receipt_ingest.py <file> [<file>...] [--merchant COSTCO] [--dry-run]

Text-layer PDFs go through pdftotext. Photos and scanned PDFs go through the
Vision framework, which ships with macOS, so nothing is installed and no image
ever leaves the machine.

OCR is lossy. Every receipt keeps its full extracted text, and any line the
parser could not read is recorded rather than dropped.
"""
import decimal, hashlib, os, re, subprocess, sys, tempfile
import pymysql

HERE = os.path.dirname(os.path.abspath(__file__))
MYSQL_ENV = os.path.expanduser("~/.config/flightdeck/mysql.env")
SCHEMA = "personal-finance-gui"
D = decimal.Decimal

# --- getting text out of the file ------------------------------------------

def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def ocr_image(path):
    r = subprocess.run(["swift", os.path.join(HERE, "ocr_image.swift"), path],
                       capture_output=True, text=True)
    return r.stdout

def extract(path):
    """Return (text, engine). Tries the cheap exact route before the lossy one."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        r = subprocess.run(["pdftotext", "-layout", path, "-"],
                           capture_output=True, text=True)
        if len(r.stdout.strip()) > 80:          # a real text layer
            return r.stdout, "pdftotext"
        # a scan: rasterise, then OCR
        with tempfile.TemporaryDirectory() as td:
            png = os.path.join(td, "page.png")
            subprocess.run(["sips", "-s", "format", "png", "--resampleWidth", "1600",
                            path, "--out", png], capture_output=True)
            return ocr_image(png), "vision"
    return ocr_image(path), "vision"

# --- parsing ----------------------------------------------------------------

MONEY = r"-?\$?[\d,]+\.\d{2}"
RE_ITEM     = re.compile(r"^\s*(\d{3,8})\s+(.+?)\s{1,}(%s)\s*([A-Z])?\s*$" % MONEY)
RE_DISCOUNT = re.compile(r"^\s*(\d{3,8})\s*/\s*([\d.]+)\s+(.+?)\s{1,}(-%s)\s*$"
                         % MONEY.lstrip("-?"))
RE_STORE    = re.compile(r"#\s*(\d{2,5})")
RE_DATE     = re.compile(r"(\d{2})/(\d{2})/(\d{4})(?:\s+(\d{1,2}):(\d{2}))?")
RE_LAST4    = re.compile(r"(?:VISA|MASTERCARD|MC|AMEX|DISCOVER|DEBIT)\D{0,12}(\d{4})\b", re.I)
RE_ITEMS    = re.compile(r"(\d{1,3})\s+ITEMS?\s+SOLD", re.I)
RE_QTY      = re.compile(r"\b(\d{1,3})\s*(?:CT|PK|PACK|COUNT)\b", re.I)

def money(s):
    return D(s.replace("$", "").replace(",", ""))

def find_total(lines, *words):
    """Last match wins: receipts print SUBTOTAL before TOTAL, and TOTAL again
       in the payment block. The later one is the authoritative figure."""
    hit = None
    for ln in lines:
        u = ln.upper()
        if any(w in u for w in words):
            m = re.findall(MONEY, ln)
            if m:
                hit = money(m[-1])
    return hit

def parse(text, merchant_hint=None):
    lines = [l.rstrip() for l in text.splitlines()]
    head = {"merchant": merchant_hint, "store_no": None, "purchased_at": None,
            "subtotal": None, "tax": None, "total": None,
            "items_sold": None, "payment_last4": None}
    items, unread = [], []

    joined = "\n".join(lines)
    if not head["merchant"]:
        for name in ("COSTCO", "WHOLESALE", "SAM'S CLUB", "BJ'S", "TARGET",
                     "WALMART", "GIANT", "WEGMANS", "ALDI", "TRADER JOE"):
            if name in joined.upper():
                head["merchant"] = "COSTCO" if name == "WHOLESALE" else name
                break
    m = RE_STORE.search(joined);  head["store_no"] = m.group(1) if m else None
    m = RE_LAST4.search(joined);  head["payment_last4"] = m.group(1) if m else None
    m = RE_ITEMS.search(joined);  head["items_sold"] = int(m.group(1)) if m else None
    m = RE_DATE.search(joined)
    if m:
        mo, da, yr, hh, mi = m.groups()
        head["purchased_at"] = "%s-%s-%s %s:%s:00" % (yr, mo, da, hh or "00", mi or "00")

    head["subtotal"] = find_total(lines, "SUBTOTAL", "SUB TOTAL")
    head["tax"]      = find_total(lines, "TAX")
    head["total"]    = find_total(lines, "TOTAL")
    if head["subtotal"] is not None and head["total"] == head["subtotal"]:
        head["total"] = find_total(lines, "****", "AMOUNT")  or head["total"]

    skip = re.compile(r"SUBTOTAL|SUB TOTAL|^\s*TAX\b|TOTAL|ITEMS? SOLD|CHANGE|"
                      r"MEMBER|AID:|APPROV|AUTH|SEQ|TERMINAL|THANK|RETURN", re.I)
    n = 0
    for raw in lines:
        if not raw.strip() or skip.search(raw):
            continue
        d = RE_DISCOUNT.match(raw)
        if d:
            n += 1
            items.append(dict(line_no=n, item_code=d.group(1), raw=raw,
                              desc=d.group(3).strip(), amount=money(d.group(4)),
                              flag=None, is_discount=1, discount_for=d.group(1)))
            continue
        i = RE_ITEM.match(raw)
        if i:
            n += 1
            amt = money(i.group(3))
            items.append(dict(line_no=n, item_code=i.group(1), raw=raw,
                              desc=i.group(2).strip(), amount=amt,
                              flag=i.group(4), is_discount=1 if amt < 0 else 0,
                              discount_for=None))
            continue
        if re.search(MONEY, raw):               # looks like a line, did not parse
            unread.append(raw.strip())
    return head, items, unread

# --- enrichment -------------------------------------------------------------

FOOD = re.compile(r"SPINACH|CHICKEN|BACON|BEEF|PORK|MILK|EGG|CHEESE|YOGURT|BREAD|"
                  r"RICE|PASTA|OIL|COFFEE|TEA|JUICE|WATER|FRUIT|BERR|APPLE|BANANA|"
                  r"SALAD|VEG|FROZEN|SNACK|CEREAL|SOUP|SAUCE|BUTTER|CHIP|NUT|"
                  r"SALMON|SHRIMP|TURKEY|ROTISS|PIZZA|TORTILLA|HUMMUS", re.I)
HOUSE = re.compile(r"TOWEL|TISSUE|TOILET|DETERG|SOAP|CLEAN|TRASH|BAG|FOIL|WRAP|"
                   r"BATTER|BULB|LAUNDRY|DISH|PODS|SANITIZ|PAPER", re.I)
HEALTH = re.compile(r"VITAMIN|IBUPROFEN|ADVIL|TYLENOL|SHAMPOO|TOOTH|RAZOR|"
                    r"LOTION|MEDICINE|SUPPLEMENT|CONTACT|PHARMAC", re.I)
TECH = re.compile(r"\bTV\b|LAPTOP|TABLET|PHONE|HDMI|CABLE|MONITOR|PRINTER|"
                  r"SAMSUNG|APPLE|SONY|HEADPHONE|SPEAKER", re.I)

def categorise(desc):
    for rx, name in ((FOOD, "food"), (HOUSE, "household"),
                     (HEALTH, "health"), (TECH, "electronics")):
        if rx.search(desc or ""):
            return name
    return None

def expand(desc, abbrev):
    out = []
    for word in (desc or "").split():
        key = re.sub(r"[^A-Z0-9]", "", word.upper())
        out.append(abbrev.get(key, word))
    return " ".join(out)[:160]

# --- database ---------------------------------------------------------------

def connect():
    env = {}
    for l in open(MYSQL_ENV):
        if "=" in l and not l.startswith("#"):
            k, v = l.strip().split("=", 1); env[k] = v
    return pymysql.connect(host="::1", port=int(env.get("MYSQL_PORT", 3306)),
                           user=env["MYSQL_USER"], password=env["MYSQL_PASSWORD"],
                           database=SCHEMA, charset="utf8mb4",
                           cursorclass=pymysql.cursors.DictCursor, autocommit=False)

def match_txn(cur, total, purchased_at, last4):
    """Find the bank transaction this receipt explains. Amount must match to the
       cent; the date can drift a few days because cards post late."""
    if total is None or not purchased_at:
        return None, "no total or no date on the receipt"
    cur.execute("""SELECT id, txn_date, acct_id, description, account_code
                     FROM claudia_usaa_import
                    WHERE abs_amount = %s AND direction = 'out'
                      AND txn_date BETWEEN DATE_SUB(%s, INTERVAL 4 DAY)
                                       AND DATE_ADD(%s, INTERVAL 4 DAY)
                    ORDER BY ABS(DATEDIFF(txn_date, %s))""",
                (total, purchased_at[:10], purchased_at[:10], purchased_at[:10]))
    rows = cur.fetchall()
    if not rows:
        return None, "no bank transaction for %s within 4 days" % total
    if last4:
        for r in rows:
            if r["account_code"] == last4:
                return r["id"], "matched on amount, date and card %s" % last4
    if len(rows) > 1:
        return rows[0]["id"], "%d candidates matched; took the closest date" % len(rows)
    return rows[0]["id"], "matched on amount and date"

def ingest(path, merchant_hint=None, dry=False):
    digest = sha256_file(path)
    text, engine = extract(path)
    head, items, unread = parse(text, merchant_hint)

    print("\n%s" % os.path.basename(path))
    print("  engine %s, %d lines of text" % (engine, len(text.splitlines())))
    print("  %s  store %s  %s" % (head["merchant"] or "?", head["store_no"] or "?",
                                  head["purchased_at"] or "no date found"))
    print("  subtotal %s  tax %s  total %s" % (head["subtotal"], head["tax"], head["total"]))
    print("  %d item lines parsed, %d unreadable" % (len(items), len(unread)))

    # does the receipt add up? this is the check that catches OCR damage
    note, ok = None, 1
    if items and head["total"] is not None:
        summed = sum(i["amount"] for i in items)
        expected = head["subtotal"] if head["subtotal"] is not None else head["total"]
        if abs(summed - expected) > D("0.02"):
            ok = 0
            note = "lines sum to %s, receipt says %s, off by %s" % (
                summed, expected, (summed - expected))
            print("  MISMATCH: %s" % note)
        else:
            print("  lines reconcile to the printed subtotal exactly")
    if unread:
        note = ((note + "; ") if note else "") + "%d unread lines" % len(unread)
        for u in unread[:3]:
            print("    unread: %s" % u[:90])
    if dry:
        return

    conn = connect(); cur = conn.cursor()
    # Only this receipt's own merchant, plus the store-agnostic GENERIC tier --
    # loading every merchant's rows unfiltered would let another chain's
    # abbreviation for the same letters silently win. GENERIC sorts first so a
    # merchant-specific row overwrites it below when both define the same token.
    cur.execute("SELECT abbrev, expansion, category FROM receipt_abbrev"
                " WHERE merchant = 'GENERIC' OR merchant = %s"
                " ORDER BY (merchant = 'GENERIC') DESC", (head["merchant"] or "",))
    rows = cur.fetchall()
    abbrev = {r["abbrev"]: r["expansion"] for r in rows}
    abbrev_cat = {r["abbrev"]: r["category"] for r in rows if r["category"]}
    cur.execute("SELECT ending, meaning FROM price_signal")
    signals = {r["ending"]: r["meaning"] for r in cur.fetchall()}

    txn_id, match_note = match_txn(cur, head["total"], head["purchased_at"],
                                   head["payment_last4"])
    print("  bank match: %s" % match_note)

    cur.execute("""INSERT INTO receipt
        (source_file, source_sha256, mime, ocr_engine, merchant, store_no,
         purchased_at, subtotal, tax, total, items_sold, payment_last4,
         txn_id, match_note, parse_ok, parse_note, raw_text)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id)""",
        (os.path.basename(path)[:255], digest, os.path.splitext(path)[1].lstrip("."),
         engine, head["merchant"], head["store_no"], head["purchased_at"],
         head["subtotal"], head["tax"], head["total"], head["items_sold"],
         head["payment_last4"], txn_id, match_note, ok, note,
         text[:16000000]))
    rid = cur.lastrowid
    cur.execute("DELETE FROM receipt_item WHERE receipt_id = %s", (rid,))

    for it in items:
        desc = expand(it["desc"], abbrev)
        cat = categorise(it["desc"]) or categorise(desc)
        if not cat:
            for w in it["desc"].upper().split():
                if abbrev_cat.get(re.sub(r"[^A-Z0-9]", "", w)):
                    cat = abbrev_cat[re.sub(r"[^A-Z0-9]", "", w)]; break
        q = RE_QTY.search(it["desc"])
        qty = D(q.group(1)) if q else D(1)
        cents = ("%.2f" % abs(it["amount"]))[-2:]
        cur.execute("""INSERT INTO receipt_item
            (receipt_id, line_no, item_code, description_raw, description, qty,
             line_total, unit_price, tax_flag, price_ending, price_signal,
             is_discount, discount_for, category, raw_line)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (rid, it["line_no"], it["item_code"], it["desc"][:160], desc, qty,
             it["amount"], (it["amount"] / qty) if qty else None, it["flag"],
             None if it["is_discount"] else cents,
             None if it["is_discount"] else signals.get(cents),
             it["is_discount"], it["discount_for"],
             cat, it["raw"][:255]))
    conn.commit()
    print("  stored as receipt #%d with %d items" % (rid, len(items)))

def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    hint = None
    if "--merchant" in sys.argv:
        hint = sys.argv[sys.argv.index("--merchant") + 1]
        args = [a for a in args if a != hint]
    if not args:
        sys.exit(__doc__)
    for p in args:
        if not os.path.exists(p):
            print("no such file: %s" % p); continue
        ingest(p, hint, dry)

if __name__ == "__main__":
    main()
