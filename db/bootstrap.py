#!/usr/bin/env python3
"""Turn a profile document plus the warehouse into what the page needs at boot.

index.html in the repository contains no data about anybody. When the server
hands the page out it substitutes one placeholder with this structure, so the
file on disk stays empty and whoever's profile is active is what you see.

That is the whole "insert a CD" idea: the player is generic, the disc is yours.
"""
import datetime, decimal, json, os, re, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import profile as prof

# the page's own category vocabulary
CAT = {"housing": "housing", "utilities": "utilsub", "transport": "transport",
       "food": "food", "health": "utilsub", "lifestyle": "utilsub",
       "debt": "debt", "savings": "savings", "family": "shopping", "": "shopping"}
# Groceries and eating out share a profile category ("food") but the warehouse
# tells them apart (category.cat_key 'groceries' vs 'dining'), and the gap
# between them is exactly the kind of thing this app exists to surface - a
# combined "Food" line once hid that dining was running 5x over plan while
# groceries sat right on target. Matched by name, the same way LIVING is.
FOOD_SPLIT = (("groceries", "groceries"), ("eating out", "dining"), ("dining", "dining"))
# spending that varies week to week belongs on Cash flow as "living", not as a bill
LIVING = ("groceries", "eating out", "fuel", "household", "entertainment", "dining")

def slug(s, n=0):
    keep = "".join(ch for ch in (s or "").lower() if ch.isalnum())[:6] or "row"
    return keep + (str(n) if n else "")

STOP = {"THE","A","AN","OF","AND","FEE","FEES","PMT","PAYMENT","BILL","INC","LLC",
        "CO","CORP","SERVICE","SERVICES","MONTHLY","ACCT","ACCOUNT","CARD","INS"}

def words(s):
    return {w for w in re.split(r"[^A-Z0-9]+", (s or "").upper())
            if len(w) > 2 and w not in STOP}

def best_match(name, payees, min_score=2):
    """Which real payee string best names this bill, by shared meaningful words.
       Names rarely match exactly - "PA Housing mortgage" against
       "PA HOUSING MTG PMT" - so compare the words that carry meaning and
       take the best overlap. Used both to find a trustworthy due-day (kept
       strict, min_score=2, since a wrong day would be asserted as fact) and,
       more loosely (min_score=1), to link a bill to the charges that pay it -
       a weak link only ever surfaces a real payee string for the user to see
       and judge for themselves, so a single strong word like "housing" is
       worth taking even though it would not be enough to trust a due-day."""
    want = words(name)
    if not want:
        return None
    best, score = None, 0
    for payee in payees:
        hit = len(want & words(payee))
        if hit > score:
            best, score = payee, hit
    return best if score >= min_score or (score >= 1 and len(want) == 1) else None

def match_day(name, due_days):
    payee = best_match(name, due_days.keys())
    return due_days.get(payee) if payee else None

def build(p, txns=None, accounts=None, due_days=None, all_payees=None):
    """p is a loaded profile dict. txns/accounts come from the warehouse when a
       dataset is attached, and are simply absent when it is not. due_days is
       payees the bank charges on a steady enough day to trust; all_payees is
       every real payee out there, looser, just to link a bill to its charges."""
    accounts = accounts or p.get("accounts") or []
    codes = [a.get("code") or a.get("account_code") for a in accounts]
    codes = [c for c in codes if c]

    income = []
    for i, inc in enumerate(p.get("income") or [], 1):
        income.append({"id": "inc%d" % i, "name": inc.get("label") or "Income",
                       "sub": inc.get("detail") or inc.get("notes") or "",
                       "acct": codes[0] if codes else "",
                       "amount": float(inc.get("net_monthly") or 0)})

    bills, living, seen = [], [], set()
    for i, e in enumerate(p.get("expenses") or [], 1):
        name = e.get("name") or "Expense"
        lname = name.lower()
        rid = slug(name, i)
        amount = float(e.get("monthly") or 0)
        cat = CAT.get((e.get("category") or "").lower(), "shopping")
        # Groceries and eating out split by name even though the profile files
        # both under "food" - see FOOD_SPLIT above.
        for needle, split_cat in FOOD_SPLIT:
            if needle in lname:
                cat = split_cat
                break
        row = {"id": rid, "name": name, "sub": e.get("notes") or "",
               "cat": cat, "amount": amount}
        if any(k in lname for k in LIVING):
            living.append(row)
        elif cat == "savings":
            continue                      # savings shows up as a sweep, below
        else:
            grp = {"housing": "Housing", "transport": "Transportation",
                   "utilsub": "Utilities", "debt": "Debt"}.get(cat, "Other")
            # The day of the month a bill lands. A profile can say; otherwise
            # spread them out rather than stacking every bill on the 1st, which
            # would make them all read as already paid.
            # In order of trust: what the profile says, then what the bank
            # actually did, then an even spread so bills do not all stack on
            # the 1st and read as already paid. Only the first two are ever
            # asserted as a fact - the spread is flagged as a guess so the
            # page can say so rather than showing it exactly like a real one.
            due, due_estimated = e.get("day"), False
            tx_link = best_match(name, due_days.keys()) if due_days else None
            if not due and tx_link:
                due = due_days.get(tx_link)
            if not due:
                due = 1 + (i * 7) % 28
                due_estimated = True
            # A day-confident match is also a charge-history match. If none was
            # found (the mortgage's own spread is too wide to trust the day,
            # say), still look for something to link "click to see every
            # charge" to - the day and the link do not have to come from the
            # same evidence.
            if not tx_link and all_payees:
                tx_link = best_match(name, all_payees, min_score=1)
            bills.append(dict(row, grp=grp, acct=codes[0] if codes else "",
                              due=int(due), dueEstimated=due_estimated,
                              tx=tx_link))

    sweeps = []
    for i, e in enumerate(p.get("expenses") or [], 1):
        if (e.get("category") or "").lower() == "savings":
            sweeps.append({"id": "sw%d" % i, "to": codes[-1] if codes else "",
                           "name": e.get("name") or "Savings transfer",
                           "sub": e.get("notes") or "", "amount": float(e.get("monthly") or 0)})

    # A profile may record what is actually in each account. Without it the
    # safe-to-spend figure cannot be computed, and the page says so rather than
    # guessing.
    assets = [{"id": "as%d" % i, "name": a.get("label") or a.get("acct_id") or "Account",
               "amount": float(a.get("balance") or 0), "kind": (a.get("kind") or "")}
              for i, a in enumerate(accounts, 1)] or \
             [{"id": "as1", "name": "Everyday account", "amount": 0, "kind": ""}]

    debts = []
    for i, d in enumerate(p.get("debts") or [], 1):
        debts.append({"id": "dbt%d" % i, "name": d.get("name") or "Debt",
                      "kind": d.get("kind") or "card", "amount": float(d.get("balance") or 0),
                      "apr": float(d.get("apr") or 0), "min": float(d.get("minimum") or 0),
                      "sub": d.get("notes") or ""})

    goals = []
    for i, g in enumerate(p.get("goals") or [], 1):
        goals.append({"id": "gol%d" % i, "name": g.get("name") or "Goal",
                      "note": g.get("notes") or "", "target": float(g.get("target") or 0),
                      "saved": float(g.get("saved") or 0), "monthly": float(g.get("monthly") or 0)})

    t = p.get("totals") or {}
    defaults = {
        "paycheck": round(float(t.get("income_monthly") or 0) / 2.1667, 2),
        "income": income, "bills": bills, "living": living, "sweeps": sweeps,
        "assets": assets, "debts": debts, "goals": goals,
        "sheet": [{"id": "r%d" % n, "name": "", "kind": k, "freq": "monthly",
                   "acct": codes[0] if codes else "", "amount": 0}
                  for n, k in enumerate(["bill", "bill", "spending", "income"], 1)],
        "efMonths": 3, "done": {}, "sim": {"save": 0, "trim": 0, "extra": 0, "apy": 0},
        "house": {"address": "", "value": 0, "purchase": 0, "boughtOn": "",
                  "loanId": "", "updated": "", "source": "manual"},
    }

    accts, txacct = {}, {}
    for n, a in enumerate(accounts, 1):
        code = a.get("code") or a.get("account_code") or str(n)
        accts[code] = a.get("label") or a.get("acct_id") or code
        txacct[str(n)] = code
    # Index "0" is the account-not-found bucket (see tx_from_warehouse below).
    # A warehouse row whose account code the profile does not recognise should
    # say so, not silently read as whichever account happens to be first.
    accts.setdefault("?", "Account not in your profile")
    txacct["0"] = "?"

    return {"profile": {"name": p.get("name"), "person": p.get("person") or {},
                        "source": p.get("source"), "totals": t},
            "defaults": defaults, "accts": accts, "txacct": txacct,
            "tx": txns or [], "hasWarehouse": bool(txns)}

# The warehouse's own category vocabulary (see category.cat_key) is finer than
# the page's six spending buckets, so charges are folded down onto those six -
# the same six the Spending tab and its charts already know how to color.
PAGE_CAT = {
    "housing": "housing", "mortgage": "housing", "hoa": "housing",
    "debt": "debt", "creditcard": "debt", "carloan": "debt",
    "transport": "transport", "carins": "transport", "fuel": "transport",
    "utilsub": "utilsub", "utilities": "utilsub", "subscription": "utilsub",
    "phone": "utilsub", "fees": "utilsub",
    # groceries and dining keep their own bucket rather than folding into
    # "food" - the profile-side split (FOOD_SPLIT, above) matches this so the
    # two sides of Plan-vs-Actual never merge them back together.
    "food": "food", "groceries": "groceries", "dining": "dining",
    # health matches CAT["health"]="utilsub" above - it used to land here as
    # "shopping" on the warehouse side while the profile side called it
    # "utilsub", so a gym membership counted against two different rows
    # depending only on which half of the app computed it.
    "shopping": "shopping", "health": "utilsub", "entertainment": "shopping",
    "education": "shopping",
    "savings": "savings", "income": "salary",
    # Money moving between the user's own accounts is neither income nor
    # spending. Giving it a bucket of its own (instead of falling through the
    # .get() default below into "shopping") is what lets the frontend keep it
    # out of every spend/income total instead of silently inflating both.
    "transfer": "transfer",
}

def tx_from_warehouse(cur, accounts, limit=6000):
    """The page wants [date, amount, in|out, category, accountIndex, description, isTransfer]."""
    idx = {}
    for n, a in enumerate(accounts, 1):
        idx[a.get("code") or a.get("account_code")] = str(n)
    cur.execute("""SELECT t.posted_on, t.amount, t.direction, t.is_transfer,
                          a.code AS account_code,
                          COALESCE(c.cat_key, mc.cat_key) AS cat_key,
                          COALESCE(m.name, t.description) AS description
                     FROM txn t
                     JOIN account a ON a.id = t.account_id
                     LEFT JOIN category c ON c.id = t.category_id
                     LEFT JOIN merchant m ON m.id = t.merchant_id
                     LEFT JOIN category mc ON mc.id = m.category_id
                    WHERE t.posted_on IS NOT NULL
                    ORDER BY t.posted_on, t.id LIMIT %s""", (limit,))
    out = []
    for r in cur.fetchall():
        page_cat = PAGE_CAT.get((r["cat_key"] or "").lower(), "shopping")
        # the seventh field is the transfer flag. Spending views must skip these
        # or money between your own accounts is counted as if you spent it.
        # "0" (not any real profile account) beats silently aliasing an
        # unrecognised warehouse account to account "1" - a code the profile
        # never learned about should read as unmatched, not as checking.
        out.append([r["posted_on"].isoformat(), float(r["amount"] or 0),
                    r["direction"], page_cat,
                    idx.get(r["account_code"], "0"), r["description"] or "",
                    1 if r["is_transfer"] else 0])
    return out

def empty():
    """What a brand-new install sees: a working app with nothing in it."""
    return build(prof.fill(prof.blank("Not set up yet")))
