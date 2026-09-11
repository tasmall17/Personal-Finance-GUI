#!/usr/bin/env python3
"""Pull transactions from every linked Plaid item into the flightdeck warehouse.

    plaid_sync.py [--env sandbox|production] [--dry-run] [--only NAME]

This is the same warehouse (account/merchant/category/txn) your CSV imports
land in and the one Overview, Bills, Spending, Debt and Budget all read - a
linked card shows up everywhere those already do, with real merchant names
and categories, not a separate database nothing else looks at.

Runs every item in plaid.env (PLAID_ITEM_<n>_*, see plaid_link.py) in turn,
using /transactions/sync per item so a repeat run is cheap and only pulls what
changed. Each item's own cursor is kept in its own slot, so linking a new card
never disturbs another one's sync position.

New accounts (a card's first sync) are created automatically, matched by the
last-4 mask the same way a CSV upload names its account. Money in is negative
in Plaid's own sign convention flipped to ours: negative is money out.

After writing, runs the same classify step CSV imports run (merchant/category
matching, and - the reason this matters for a credit card - marking a payment
to your own linked card as an internal transfer once its real purchases exist
to explain the spend instead. Skipping this step is why a payment stayed
double-counted until that step existed."""
import decimal, hashlib, json, os, sys, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fdb
import load_csv          # for cmd_classify's merchant/category + transfer-pairing pass
import plaid_link as link  # existing_items(), load_env() - one place that knows the item format

ENV_FILE = os.path.expanduser("~/.config/flightdeck/plaid.env")
HOSTS = {"sandbox": "https://sandbox.plaid.com", "production": "https://production.plaid.com"}

def api(host, path, body):
    req = urllib.request.Request(host + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        sys.exit("Plaid answered HTTP %d for %s\n%s" % (e.code, path, e.read().decode()[:400]))

def account_kind(acct):
    t, st = (acct.get("type") or "").lower(), (acct.get("subtype") or "").lower()
    if t == "credit":
        return "credit"
    if "mortgage" in st:
        return "mortgage"
    if t == "loan":
        return "loan"
    if st in ("savings", "money market", "cd", "hsa"):
        return "savings"
    if st == "checking":
        return "checking"
    return "other"

def ensure_accounts(cur, plaid_accounts):
    """Every account this item reports gets a row here if it does not have one
    already, matched by its last-4 mask - the same key a CSV upload names an
    account by. Returns {plaid_account_id: our account_id}."""
    cur.execute("SELECT id, code FROM account")
    by_code = {r["code"]: r["id"] for r in cur.fetchall()}
    out = {}
    for a in plaid_accounts.values():
        code = a.get("mask")
        if not code:
            continue
        if code not in by_code:
            name = a.get("official_name") or a.get("name") or ("Account " + code)
            cur.execute("INSERT INTO account (code, name, kind) VALUES (%s,%s,%s)",
                        (code, name[:64], account_kind(a)))
            by_code[code] = cur.lastrowid
            print("  new account: %s (%s), code %s" % (name, account_kind(a), code))
        out[a["account_id"]] = by_code[code]
    return out

def sync_item(item, which, dry):
    host = HOSTS[which]
    env = link.load_env()
    secret = env["PLAID_SECRET_PRODUCTION" if which == "production" else "PLAID_SECRET_SANDBOX"]
    base = {"client_id": env["PLAID_CLIENT_ID"], "secret": secret, "access_token": item["access_token"]}

    print("\n== %s (item %d) ==" % (item["name"], item["slot"]))
    plaid_accounts = {a["account_id"]: a for a in api(host, "/accounts/get", dict(base))["accounts"]}
    for a in plaid_accounts.values():
        print("  %-34s %-10s mask %s" % (a.get("name", "")[:34], a.get("subtype", ""), a.get("mask")))

    conn = fdb.connect(autocommit=False)
    cur = conn.cursor()
    acct_map = ensure_accounts(cur, plaid_accounts)
    conn.commit()

    cursor = item["cursor"]
    added, modified, removed, pages = [], [], [], 0
    while True:
        body = dict(base, count=500)
        if cursor:
            body["cursor"] = cursor
        d = api(host, "/transactions/sync", body)
        added += d.get("added", []); modified += d.get("modified", [])
        removed += [r["transaction_id"] for r in d.get("removed", [])]
        cursor = d.get("next_cursor"); pages += 1
        print("  page %d: +%d ~%d -%d" % (pages, len(d.get("added", [])),
              len(d.get("modified", [])), len(d.get("removed", []))))
        if not d.get("has_more"):
            break

    rows = added + modified
    print("  %d transactions to write, %d removed upstream" % (len(rows), len(removed)))
    if rows:
        ds = sorted(r["date"] for r in rows)
        print("  spanning %s to %s" % (ds[0], ds[-1]))
    if dry:
        print("  dry run, nothing written")
        return 0

    sql = ("INSERT INTO txn "
           "(account_id, posted_on, amount, description, description_raw, "
           " bank_category, is_pending, dedupe_hash) "
           "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
           "ON DUPLICATE KEY UPDATE "
           " posted_on=VALUES(posted_on), amount=VALUES(amount), "
           " description=VALUES(description), description_raw=VALUES(description_raw), "
           " bank_category=VALUES(bank_category), is_pending=VALUES(is_pending)")
    n, skipped = 0, 0
    for t in rows:
        account_id = acct_map.get(t["account_id"])
        if not account_id:
            skipped += 1  # an account Plaid reports that ensure_accounts could not place
            continue
        amount = -decimal.Decimal(str(t["amount"]))       # Plaid: money out is positive; ours: negative
        name = (t.get("name") or "").strip()
        desc = (t.get("merchant_name") or name or "Plaid transaction")[:255]
        cats = t.get("personal_finance_category") or {}
        bank_cat = (cats.get("primary") or ",".join(t.get("category") or []) or "")[:128]
        # No file_id/raw_row_id: Plaid itself is the re-fetchable layer 1 for
        # this data (re-running this script from a blank cursor pulls the
        # same history again), so there is nothing a local raw copy would add
        # that re-syncing would not already give back.
        cur.execute(sql, (account_id, t["date"], amount, desc, name[:255] or desc,
                          bank_cat, 1 if t.get("pending") else 0,
                          hashlib.sha256(("plaid:" + t["transaction_id"]).encode()).hexdigest()))
        n += 1
    for rid in removed:
        cur.execute("DELETE FROM txn WHERE dedupe_hash=%s",
                   (hashlib.sha256(("plaid:" + rid).encode()).hexdigest(),))
    conn.commit()
    link.save_token("PLAID_ITEM_%d_CURSOR" % item["slot"], cursor or "")
    uniq = len({t["transaction_id"] for t in rows})
    print("  %d writes covering %d distinct transactions (%d repeats from Plaid), "
          "%d skipped (unmapped account), deleted %d"
          % (n, uniq, n - uniq, skipped, len(removed)))
    return n

def main():
    env = link.load_env()
    which = env.get("PLAID_ENV", "sandbox")
    if "--env" in sys.argv:
        which = sys.argv[sys.argv.index("--env") + 1]
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
    dry = "--dry-run" in sys.argv

    items = link.existing_items(env)
    if not items:
        sys.exit("nothing linked yet - run plaid_link.py first")
    if only:
        items = [i for i in items if only.lower() in i["name"].lower()]
        if not items:
            sys.exit("no linked item matches %r" % only)

    total = 0
    for item in items:
        total += sync_item(item, which, dry)

    if not dry and total:
        print("\nreconciling merchants, categories and card-payment transfers...")
        load_csv.cmd_classify(None)

    conn = fdb.connect()
    cur = conn.cursor()
    cur.execute("SELECT a.code, a.name, COUNT(t.id) n, MIN(t.posted_on) mn, MAX(t.posted_on) mx "
                "FROM account a LEFT JOIN txn t ON t.account_id = a.id "
                "GROUP BY a.code, a.name ORDER BY n DESC")
    print("\nin the warehouse now:")
    for r in cur.fetchall():
        print("  %-28s %5d  %s -> %s" % (r["name"][:28], r["n"], r["mn"] or "-", r["mx"] or "-"))

if __name__ == "__main__":
    main()
