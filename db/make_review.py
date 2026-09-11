#!/usr/bin/env python3
"""Write the merchant review checklist as plain lines you edit in place.

    make_review.py [outfile]     default: review/merchants-review.md

One line per merchant. Fix the category at the end of the line, tick the box, hand
it back. apply_review.py reads the same file and writes your answers into the DB.
"""
import io, os, re, sys, collections
import fdb

OBSIDIAN = os.path.expanduser("~/Documents/CentralVault/General/Finance/Finance.md")

def existing(path):
    """Keep the frontmatter and every answer already in the file. Regenerating must
       never cost you work you have already done."""
    fm, answers = "", {}
    if not os.path.exists(path):
        return fm, answers
    txt = io.open(path, encoding="utf-8").read()
    if txt.startswith("---"):
        end = txt.find("\n---", 3)
        if end != -1:
            fm = txt[:end + 4].rstrip() + "\n\n"
    for line in txt.splitlines():
        m = re.match(r"- \[([ xX])\] \*\*(.+?)\*\* — .*?→ `([^`]*)`(.*)$", line)
        if m:
            answers[m.group(2)] = {"checked": m.group(1).lower() == "x",
                                   "cat": m.group(3).strip(),
                                   "note": m.group(4).strip()}
    return fm, answers

CATS = ["income", "mortgage", "hoa", "housing", "utilities", "phone", "subscription",
        "groceries", "dining", "fuel", "carloan", "carins", "creditcard", "debt",
        "shopping", "health", "savings", "transfer", "fees"]

def ordinal(n):
    n = int(n or 0)
    return "%d%s" % (n, "th" if 11 <= n % 100 <= 13 else
                     {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th"))

def money(v):
    return "$%s" % format(float(v or 0), ",.2f")

def span(r):
    if not r["first_seen"]:
        return "no charges"
    a, b = r["first_seen"], r["last_seen"]
    if a == b:
        return a.strftime("%-d %b")
    return "%s–%s" % (a.strftime("%-d %b"), b.strftime("%-d %b"))

ANSWERS = {}
def line(r):
    n = r["charges"] or 0
    prev = ANSWERS.get(r["name"], {})
    # a ticked line, or one you left a note on, is your answer and it stands.
    # an untouched line takes whatever the current rules say, so improving a rule
    # is not silently overridden by an old guess.
    mine = prev.get("checked") or bool(prev.get("note"))
    cat = (prev.get("cat") if mine and prev.get("cat") not in (None, "", "?????")
           else r["cat_key"] or "?????")
    tick = "x" if (prev.get("checked") or r["reviewed"]) else " "
    note = (" " + prev["note"]) if prev.get("note") else ""
    return "- [%s] **%s** — %d charge%s, %s, %s → `%s`%s" % (
        tick, r["name"], n, "" if n == 1 else "s",
        money(r["total_out"]), span(r), cat, note)

def main(out_path):
    global ANSWERS
    fm, ANSWERS = existing(out_path)
    cur = fdb.connect().cursor()
    cur.execute("SELECT * FROM v_merchant_summary ORDER BY total_out DESC")
    merchants = cur.fetchall()
    cur.execute("SELECT * FROM v_recurring_candidates ORDER BY avg_charge DESC")
    recurring = cur.fetchall()
    cur.execute("SELECT posted_on, a.code, t.amount, t.description FROM txn t"
                " JOIN account a ON a.id=t.account_id WHERE t.merchant_id IS NULL"
                " ORDER BY ABS(t.amount) DESC")
    orphans = cur.fetchall()
    cur.execute("SELECT MIN(posted_on) a, MAX(posted_on) b, COUNT(*) n FROM txn")
    sp = cur.fetchone()

    cat_of = {m["name"]: m["cat_key"] for m in merchants}
    unknown = [m for m in merchants if not m["cat_key"]]
    known = [m for m in merchants if m["cat_key"]]
    by_cat = {}
    for m in known:
        by_cat.setdefault(m["cat_key"], []).append(m)

    d = []
    d.append("# Merchant review\n")
    d.append("## What I need you to do\n")
    d.append("Every line ends with a category in backticks. Two jobs, nothing else:\n")
    d.append("1. **Wrong category?** Type the right one over it. The list is at the bottom.")
    d.append("2. **Right category?** Change `- [ ]` to `- [x]` so I know you checked it.\n")
    d.append("Ticking a line locks your answer in — I will never overwrite it. Lines you leave "
             "unticked stay open to my best guess, which changes as the rules improve.\n")
    d.append("Anything you leave as `- [ ]` I treat as unreviewed and will ask about again. "
             "Add a note on the line if you want to tell me something — I read those.\n")
    d.append("> Obsidian renders this as you read it — click a checkbox to tick it, and click "
             "into a line to edit the category. Everything you write here survives a "
             "regeneration; I merge your answers back in.\n")
    d.append("Start with section 1 — those are the ones I could not guess. If you only do that "
             "section, it is still worth it.\n")
    d.append("_%d transactions, %s to %s, %d merchants._\n" % (sp["n"], sp["a"], sp["b"], len(merchants)))
    d.append("---\n")

    d.append("## 1. I could not guess these — %d lines\n" % len(unknown))
    if unknown:
        d += [line(m) for m in unknown]
    else:
        d.append("_Nothing here._")

    d.append("\n## 2. Charges that matched no merchant — %d\n" % len(orphans))
    if not orphans:
        d.append("None. Every charge on file belongs to a merchant above.\n")
    else:
        d.append("Tell me what each one is and I will build a rule for it.\n")
    for o in orphans[:60]:
        d.append("- [ ] %s · %s · %s — `%s` → **is this?** " % (
            o["posted_on"], o["code"], money(o["amount"]), (o["description"] or "")[:64]))

    FIXED = {"mortgage", "hoa", "carloan", "carins", "creditcard", "utilities", "phone",
             "income", "transfer", "fees", "debt"}
    cancellable = [r for r in recurring
                   if (cat_of.get(r["name"]) or "") not in FIXED]
    d.append("\n## 3. Recurring things you could actually cancel — %d\n" % len(cancellable))
    d.append("Tick to keep it. Write `cancel` on the line and I will put it on the overhaul "
             "checklist. Your mortgage, insurance and card payments recur too, but they are not "
             "choices, so they are not here.\n")
    for r in cancellable:
        d.append("- [ ] **%s** — %s a month, %d charge%s over %d months, usually the %s → `keep?`"
                 % (r["name"], money(r["avg_charge"]), r["charges"],
                    "" if r["charges"] == 1 else "s", r["months_seen"],
                    ordinal(r["typical_day"])))

    d.append("\n## 4. My guesses — skim and fix what is wrong\n")
    for ck in sorted(by_cat, key=lambda k: -sum(float(x["total_out"] or 0) for x in by_cat[k])):
        rows = by_cat[ck]
        tot = sum(float(x["total_out"] or 0) for x in rows)
        d.append("\n### `%s` — %d merchants, %s\n" % (ck, len(rows), money(tot)))
        d += [line(m) for m in rows]

    d.append("\n---\n")
    d.append("## The categories you can use\n")
    d.append(" ".join("`%s`" % c for c in CATS) + "\n")
    d.append("If none of them fit, invent one — write it in and I will add it.\n")

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with io.open(out_path, "w", encoding="utf-8") as fh:
        fh.write(fm + "\n".join(d) + "\n")
    print("wrote %s — %d merchants, %d unknown, %d unmatched charges, %d answers carried over"
          % (out_path, len(merchants), len(unknown), len(orphans), len(ANSWERS)))

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else OBSIDIAN)
