#!/usr/bin/env python3
"""Read the checklist back and write your answers into the warehouse.

    apply_review.py [file] [--dry-run]

A ticked line becomes the merchant's category and marks it reviewed, so every past
and future charge from that merchant lands there. A line with `cancel` written on it
is flagged for the overhaul checklist. Unticked lines are left alone.
"""
import io, os, re, sys
import fdb

OBSIDIAN = os.path.expanduser("~/Documents/CentralVault/General/Finance/Finance.md")
LINE = re.compile(r"- \[([ xX])\] \*\*(.+?)\*\* — .*?→ `([^`]*)`(.*)$")

def main(path, dry):
    if not os.path.exists(path):
        sys.exit("no such file: " + path)
    conn = fdb.connect(autocommit=False)
    cur = conn.cursor()
    cur.execute("SELECT cat_key, id FROM category")
    cats = {r["cat_key"]: r["id"] for r in cur.fetchall()}
    cur.execute("SELECT name, id, category_id FROM merchant")
    merch = {r["name"]: r for r in cur.fetchall()}

    applied, cancels, unknown_cat, missing = 0, [], set(), []
    for line in io.open(path, encoding="utf-8"):
        m = LINE.match(line.strip())
        if not m:
            continue
        ticked, name, cat, note = m.group(1).lower() == "x", m.group(2), m.group(3).strip(), m.group(4)
        if "cancel" in note.lower():
            cancels.append(name)
        if not ticked or cat in ("", "?????", "keep?"):
            continue
        if name not in merch:
            missing.append(name); continue
        if cat not in cats:
            unknown_cat.add(cat); continue
        applied += 1
        if not dry:
            cur.execute("UPDATE merchant SET category_id=%s, reviewed=1 WHERE id=%s",
                        (cats[cat], merch[name]["id"]))
            cur.execute("UPDATE txn SET category_id=%s WHERE merchant_id=%s",
                        (cats[cat], merch[name]["id"]))
    if not dry:
        conn.commit()
    print("%s %d merchants%s" % ("would apply" if dry else "applied", applied,
                                 " (dry run)" if dry else ""))
    if cancels:
        print("marked to cancel: " + ", ".join(cancels))
    if unknown_cat:
        print("categories I do not know yet, tell me and I will add them: "
              + ", ".join(sorted(unknown_cat)))
    if missing:
        print("names not in the database (renamed?): " + ", ".join(missing[:8]))
    return 0

if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    sys.exit(main(args[0] if args else OBSIDIAN, "--dry-run" in sys.argv))
