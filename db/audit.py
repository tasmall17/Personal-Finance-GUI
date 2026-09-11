#!/usr/bin/env python3
"""Fact-check every figure the app asserts against what the bank actually did.

    audit.py

The page carries a set of seeded amounts. This holds each one up against the
transactions in the warehouse and says whether it still holds, has drifted, or
was never seen at all. Nothing is changed — this only reports.
"""
import io, os, re, sys, collections
import fdb

PAGE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "index.html")
FULL_MONTHS = None          # complete months in the data, worked out below

def seeded(kind):
    """Pull the seeded rows straight out of the page so the audit cannot drift from it."""
    s = io.open(PAGE, encoding="utf-8").read()
    block = re.search(kind + r":\s*\[(.*?)\n  \]", s, re.S)
    if not block:
        return []
    out = []
    for m in re.finditer(r"\{([^{}]*)\}", block.group(1)):
        row, body = {}, m.group(1)
        for k, v in re.findall(r'(\w+)\s*:\s*"([^"]*)"', body):
            row[k] = v
        for k, v in re.findall(r'(\w+)\s*:\s*(-?[\d.]+)', body):
            row[k] = float(v)
        if row.get("name"):
            out.append(row)
    return out

def actuals(cur, matcher):
    keys = matcher if isinstance(matcher, list) else [matcher]
    where = " OR ".join(["t.description_raw LIKE %s OR t.description LIKE %s"] * len(keys))
    args = []
    for k in keys:
        args += ["%" + k + "%", "%" + k + "%"]
    cur.execute("""SELECT t.posted_on, t.amount, t.ym FROM txn t
                    WHERE t.amount < 0 AND (%s) ORDER BY t.posted_on""" % where, args)
    rows = cur.fetchall()
    if not rows:
        return None
    by_month = collections.defaultdict(float)
    for r in rows:
        by_month[r["ym"]] += -float(r["amount"])
    whole = [by_month.get(m, 0.0) for m in FULL_MONTHS]     # a quiet month is a zero
    amts = sorted(-float(r["amount"]) for r in rows)
    typical = amts[len(amts) // 2]                          # the charge it usually is
    return {"n": len(rows), "months": sorted(by_month), "typical": typical,
            "spread": (min(amts), max(amts)),
            "avg_full": sum(whole) / len(whole) if whole else None,
            "last": -float(rows[-1]["amount"]), "last_on": rows[-1]["posted_on"],
            "by_month": dict(by_month)}

def main():
    global FULL_MONTHS
    cur = fdb.connect().cursor()
    cur.execute("SELECT ym, COUNT(*) n, MIN(posted_on) a, MAX(posted_on) b FROM txn"
                " GROUP BY ym ORDER BY ym")
    months = cur.fetchall()
    # a month is complete only if the data covers its first and last days
    FULL_MONTHS = []
    for m in months:
        first, last = m["a"], m["b"]
        if first.day <= 2 and last.day >= 27:
            FULL_MONTHS.append(m["ym"])
    print("warehouse months: " + ", ".join("%s(%d)" % (m["ym"], m["n"]) for m in months))
    print("complete months used for averages: " + ", ".join(FULL_MONTHS) + "\n")

    verdicts = collections.Counter()
    def check(label, planned, act, note="", cadence=""):
        if cadence and act is not None:
            # three months of data cannot confirm a yearly or quarterly amount
            total = sum(act["by_month"].values())
            verdicts["short window"] += 1
            print("  %-28s %10s  %s charge of $%.2f seen — %d months of data cannot confirm a "
                  "%s amount" % (label[:28], "$%.2f" % planned, cadence, total,
                                 len(FULL_MONTHS), cadence))
            return
        if act is None:
            verdicts["unseen"] += 1
            print("  %-28s %10s  %-10s  NEVER SEEN in the data %s"
                  % (label[:28], "$%.2f" % planned, "", note))
            return
        # the amount it bills at, versus what it costs across every month
        typ, avg = act["typical"], act["avg_full"] or 0
        gap = typ - planned
        pct = (gap / planned * 100) if planned else 0
        ok = abs(pct) <= 5
        tag = "ok" if ok else ("bills $%.2f, not $%.2f" % (typ, planned))
        verdicts["ok" if ok else "wrong amount"] += 1
        lo, hi = act["spread"]
        spread = "" if abs(hi - lo) < 0.01 else "  ranged $%.2f-$%.2f" % (lo, hi)
        print("  %-26s planned $%8.2f  bills $%8.2f  costs $%8.2f/mo  %-24s %d charges%s"
              % (label[:26], planned, typ, avg, tag, act["n"], spread))

    print("INCOME")
    cur.execute("""SELECT t.posted_on, t.amount, t.ym, t.description_raw, t.description
                     FROM txn t WHERE t.amount > 0 ORDER BY t.posted_on""")
    deposits = cur.fetchall()
    # Map each income source's name (as typed on the Overview tab) to a
    # substring that actually appears in your bank's own description for that
    # deposit - there's no generic answer, every bank and employer words a
    # direct deposit differently. Empty by default; add your own pairs here
    # the same way you'd have to for any income source, e.g.
    #   {"My salary": "ACME CORP PAYROLL", "Side gig": "STRIPE TRANSFER"}
    for row in seeded("income"):
        pats = {}
        pat = pats.get(row["name"])
        if not pat:
            continue
        hits = [d for d in deposits
                if pat.lower() in ((d["description_raw"] or "") + " " +
                                   (d["description"] or "")).lower()]
        by_month = collections.defaultdict(float)
        for h in hits:
            by_month[h["ym"]] += float(h["amount"])
        whole = [by_month.get(m, 0.0) for m in FULL_MONTHS]   # a month with no deposit is a zero
        avg = sum(whole) / len(whole) if whole else 0
        amounts = sorted({round(float(h["amount"]), 2) for h in hits})
        gap = avg - row["amount"]
        tag = "ok" if abs(gap) <= max(5, row["amount"] * 0.05) else "OFF by $%.2f" % gap
        verdicts["ok" if tag == "ok" else "drift"] += 1
        print("  %-28s planned $%9.2f   actual $%9.2f a month  %-16s  %d deposits %s"
              % (row["name"][:28], row["amount"], avg, tag, len(hits),
                 ("of " + ", ".join("$%.2f" % a for a in amounts[:4])) if len(amounts) > 1 else ""))

    print("\nBILLS")
    for row in seeded("bills"):
        if not row.get("tx"):
            print("  %-28s %10s  no matcher — cannot be checked" % (row["name"][:28],
                  "$%.2f" % row["amount"]))
            verdicts["nomatcher"] += 1
            continue
        sub = (row.get("sub") or "").lower()
        cadence = ("yearly" if "a year" in sub or "annual" in sub else
                   "quarterly" if "quarter" in sub else "")
        check(row["name"], row["amount"], actuals(cur, row["tx"]), cadence=cadence)

    print("\nSWEEPS AND LIVING (no per-merchant matcher, shown for context)")
    for row in seeded("living"):
        print("  %-28s %10s  budgeted only" % (row["name"][:28], "$%.2f" % row["amount"]))

    print("\n" + ", ".join("%s: %d" % kv for kv in sorted(verdicts.items())))

if __name__ == "__main__":
    sys.exit(main())
