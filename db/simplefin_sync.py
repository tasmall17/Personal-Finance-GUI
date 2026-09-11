#!/usr/bin/env python3
"""Pull every transaction SimpleFIN will give us and land it in the warehouse.

    simplefin_sync.py [--months 24] [--dry-run]

The access URL lives in ~/.config/flightdeck/simplefin.env (mode 600) and is never
printed. Rows land in the same base tables the CSV loader uses, so promote and
classify work exactly the same way afterwards.
"""
import datetime, hashlib, json, os, re, subprocess, sys, base64
import urllib.request, urllib.error, urllib.parse
import fdb

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
HERE = os.path.dirname(os.path.abspath(__file__))
SF_ENV = os.path.expanduser("~/.config/flightdeck/simplefin.env")

def access_url():
    if not os.path.exists(SF_ENV):
        sys.exit("no SimpleFIN token saved — add one on the Settings tab first")
    for line in open(SF_ENV):
        if line.startswith("SIMPLEFIN_ACCESS_URL="):
            return line.split("=", 1)[1].strip()
    sys.exit("simplefin.env has no SIMPLEFIN_ACCESS_URL line")

def fetch(url, start, end):
    """The access URL embeds credentials as https://user:pass@host, which urllib reads
       as a nonnumeric port. Split them out into a Basic auth header instead, and never
       let the URL reach an exception message."""
    parts = urllib.parse.urlsplit(url.rstrip("/"))
    netloc, auth = parts.netloc, None
    if "@" in netloc:
        creds, netloc = netloc.rsplit("@", 1)
        user, _, pw = creds.partition(":")
        auth = base64.b64encode(
            ("%s:%s" % (urllib.parse.unquote(user), urllib.parse.unquote(pw))).encode()).decode()
    q = urllib.parse.urlunsplit((parts.scheme, netloc, parts.path + "/accounts",
                                 "start-date=%d&end-date=%d&pending=1" % (start, end), ""))
    req = urllib.request.Request(q, headers={"Accept": "application/json", "User-Agent": UA})
    if auth:
        req.add_header("Authorization", "Basic " + auth)
    try:
        with urllib.request.urlopen(req, timeout=300) as res:
            return json.loads(res.read().decode())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()[:200]
        except Exception:
            pass
        sys.exit("the bridge answered HTTP %d for %s. %s" % (e.code, netloc, body))
    except Exception as e:
        sys.exit("could not reach %s: %s" % (netloc, type(e).__name__))

def code_for(name, codes):
    """SimpleFIN account names carry the last four, e.g. 'USAA CLASSIC CHECKING x0626'."""
    for c in codes:
        if re.search(r"(?<!\d)" + re.escape(c) + r"(?!\d)", name or ""):
            return c
    return None

def main():
    months = 24
    if "--months" in sys.argv:
        months = int(sys.argv[sys.argv.index("--months") + 1])
    dry = "--dry-run" in sys.argv

    # the bridge caps a request at 90 days and asks for 45, so walk backwards in
    # 45-day windows and stop once the past stops answering. Whatever history it holds
    # is what we get: it accumulates from the day the bank was linked, it is not a
    # back catalogue. Overlaps are harmless, the dedupe hash sorts them out.
    url = access_url()
    now = datetime.datetime.now()
    windows, cursor = [], now
    for _ in range(int(months * 30.5 / 45) + 1):
        start = cursor - datetime.timedelta(days=45)
        windows.append((start, cursor))
        cursor = start
    print("asking SimpleFIN for %d months in %d windows of 45 days, newest first"
          % (months, len(windows)))

    merged, seen_err, empties = {}, set(), 0
    for wstart, wend in windows:
        data = fetch(url, int(wstart.timestamp()), int(wend.timestamp()))
        for err in (data.get("errors") or []):
            e = str(err)[:160]
            if e not in seen_err:
                seen_err.add(e)
                print("  bridge says: %s" % e)
        n = 0
        for acct in data.get("accounts", []):
            key = acct.get("id")
            slot = merged.setdefault(key, dict(acct, transactions=[]))
            byid = {t.get("id"): t for t in slot["transactions"]}
            for t in (acct.get("transactions") or []):
                if t.get("id") not in byid:
                    slot["transactions"].append(t)
                    n += 1
        print("  %s → %s  %4d new" % (wstart.date(), wend.date(), n))
        if n == 0:
            empties += 1
            if empties >= 2:
                print("  two empty windows — the bridge has nothing older, stopping")
                break
        else:
            empties = 0
    data = {"accounts": list(merged.values())}

    conn = fdb.connect(autocommit=False)
    cur = conn.cursor()
    cur.execute("SELECT code, id FROM account")
    known = {r["code"]: r["id"] for r in cur.fetchall()}
    codes = list(known)

    total_new, unmapped = 0, []
    for acct in data.get("accounts", []):
        name = "%s %s" % ((acct.get("org") or {}).get("name", ""), acct.get("name", ""))
        code = code_for(name, codes) or code_for(acct.get("id", ""), codes)
        txns = acct.get("transactions") or []
        if not code:
            unmapped.append((name.strip(), len(txns), acct.get("id", "")[:24]))
            continue
        if not txns:
            print("  %-34s %-6s no transactions in the window" % (name.strip()[:34], code))
            continue

        dates = []
        rows = []
        for t in txns:
            posted = t.get("posted") or t.get("transacted_at")
            if not posted:
                continue
            d = datetime.datetime.fromtimestamp(int(posted)).date()
            dates.append(d)
            desc = (t.get("description") or t.get("payee") or "").strip()
            rows.append({
                "sfid": str(t.get("id") or ""), "date": d,
                "amount": "%.2f" % float(t.get("amount") or 0),
                "desc": desc, "payee": (t.get("payee") or "").strip(),
                "memo": (t.get("memo") or "").strip(),
                "pending": bool(t.get("pending"))})
        if not rows:
            continue
        if dry:
            print("  %-34s %-6s %4d rows  %s .. %s"
                  % (name.strip()[:34], code, len(rows), min(dates), max(dates)))
            continue

        # one import_file per account per window, hashed on the account and span
        digest = hashlib.sha256(("simplefin|%s|%s|%s|%d" %
                    (acct.get("id"), min(dates), max(dates), len(rows))).encode()).hexdigest()
        cur.execute("SELECT id FROM import_file WHERE sha256=%s", (digest,))
        seen = cur.fetchone()
        if seen:
            file_id = seen["id"]
        else:
            cur.execute(
                "INSERT INTO import_file (path, filename, sha256, account_code, layout,"
                " row_count, first_date, last_date, source)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                ("simplefin://" + str(acct.get("id"))[:64], name.strip()[:255], digest, code,
                 "simplefin-json", len(rows), min(dates), max(dates), "simplefin"))
            file_id = cur.lastrowid
            cur.executemany(
                "INSERT IGNORE INTO raw_row (file_id, line_no, col_status, col_date,"
                " col_description, col_original_desc, col_category, col_amount, raw_line)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                [(file_id, i + 1, "pending" if r["pending"] else "posted",
                  r["date"].strftime("%m/%d/%Y"), r["desc"], r["payee"] or r["memo"],
                  None, r["amount"], json.dumps(r, default=str)[:65535])
                 for i, r in enumerate(rows)])
        total_new += len(rows)
        print("  %-34s %-6s %4d rows  %s .. %s"
              % (name.strip()[:34], code, len(rows), min(dates), max(dates)))

    conn.commit()
    for name, n, sfid in unmapped:
        print("  ?  %-40s %4d rows — no account code in the name (%s)" % (name[:40], n, sfid))
    if unmapped:
        print("     add the last four to the account table, or rename it in SimpleFIN, "
              "then run again")
    if dry:
        return 0

    print("\nlanded %d rows; promoting and classifying" % total_new)
    for stage in (["promote"], ["classify"]):
        subprocess.run([sys.executable, os.path.join(HERE, "load_csv.py")] + stage, check=False)
    subprocess.run([sys.executable, os.path.join(HERE, "make_review.py")], check=False)
    return 0

if __name__ == "__main__":
    sys.exit(main())
