#!/usr/bin/env python3
"""Group transactions into merchants and propose a category for each.

    propose_merchants.py [--reset] [--dry-run]

--reset clears the merchant table and every assignment on txn first, which is what
you want after changing the naming rules. Nothing is final: proposals land with
reviewed = 0 and the checklist is built from that.
"""
import sys, collections
import fdb
from merchants import canonical, category_for, merge_key

def main(reset, dry):
    conn = fdb.connect(autocommit=False)
    cur = conn.cursor()
    if reset and not dry:
        cur.execute("UPDATE txn SET merchant_id = NULL, category_id = NULL")
        cur.execute("DELETE FROM merchant_pattern")
        cur.execute("DELETE FROM recurring")
        cur.execute("DELETE FROM merchant")
        conn.commit()
        print("cleared every merchant and assignment\n")

    cur.execute("SELECT cat_key, id FROM category")
    cats = {r["cat_key"]: r["id"] for r in cur.fetchall()}
    cur.execute("SELECT id, description, description_raw, amount FROM txn")
    rows = cur.fetchall()

    # first pass: name every transaction, then fold the near-duplicate spellings together
    named = []
    for t in rows:
        name, key, how = canonical(t["description"], t["description_raw"])
        if name:
            named.append((name, key, float(t["amount"]), how))
    best = {}                                   # merge key -> the name to keep
    counts = collections.Counter()
    for name, key, _, _how in named:
        counts[(merge_key(key), name)] += 1
    for (mk, name), n in counts.most_common():
        if mk not in best:
            best[mk] = name                     # the most common spelling wins

    groups = {}
    for name, key, amt, how in named:
        keep = best.get(merge_key(key), name)
        g = groups.setdefault(keep, {"keys": set(), "n": 0, "out": 0.0, "how": "guess"})
        g["keys"].add(key)
        g["n"] += 1
        if how == "rule":
            g["how"] = "rule"
        if amt < 0:
            g["out"] += -amt

    print("%-28s %5s %12s  %s" % ("MERCHANT", "N", "TOTAL OUT", "CATEGORY"))
    unknown = 0
    for name in sorted(groups, key=lambda k: -groups[k]["out"]):
        g = groups[name]
        ck = category_for(name)
        if not ck:
            unknown += 1
        print("%-28s %5d %12.2f  %-13s %s"
              % (name[:28], g["n"], g["out"], ck or "-- needs you --", g["how"]))
        if dry:
            continue
        # a rule match is knowledge; it does not go in the inbox asking to be confirmed
        auto = 1 if (g["how"] == "rule" and ck) else 0
        cur.execute("INSERT INTO merchant (name, category_id, is_recurring, reviewed, confidence)"
                    " VALUES (%s,%s,0,%s,%s) ON DUPLICATE KEY UPDATE"
                    " category_id = COALESCE(VALUES(category_id), merchant.category_id),"
                    " confidence = VALUES(confidence),"
                    " reviewed = GREATEST(merchant.reviewed, VALUES(reviewed))",
                    (name[:128], cats.get(ck), auto, g["how"]))
        cur.execute("SELECT id FROM merchant WHERE name=%s", (name[:128],))
        mid = cur.fetchone()["id"]
        for key in g["keys"]:                   # every spelling points at the one merchant
            cur.execute("INSERT IGNORE INTO merchant_pattern (merchant_id, match_kind, pattern,"
                        " priority) VALUES (%s,'contains',%s,%s)",
                        (mid, key[:255], 50 if len(key) > 6 else 90))
    if not dry:
        conn.commit()
        print("\n%d merchants, %d still without a category" % (len(groups), unknown))
    return 0

if __name__ == "__main__":
    sys.exit(main("--reset" in sys.argv, "--dry-run" in sys.argv))
