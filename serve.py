#!/usr/bin/env python3
"""Flightdeck local server: the page, plus the handful of endpoints it needs.

Binds to 127.0.0.1 only. The SimpleFIN token is claimed server side and the
resulting access URL is written to ~/.config/flightdeck/simplefin.env with mode
600 — it is never sent back to the page, never stored in the browser, and never
printed. The page only ever sees "configured: true".

    ./serve.py [port]        default 8730
"""
import json, os, sys, threading, subprocess, datetime, urllib.request, urllib.error, base64
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "db"))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
CONF = os.path.expanduser("~/.config/flightdeck")
SF_ENV = os.path.join(CONF, "simplefin.env")
PY = os.path.join(HERE, ".venv", "bin", "python")
JOB = {"state": "idle", "log": [], "started": None, "finished": None}

def sf_access_url():
    if not os.path.exists(SF_ENV):
        return None
    for line in open(SF_ENV):
        if line.startswith("SIMPLEFIN_ACCESS_URL="):
            return line.split("=", 1)[1].strip()
    return None

def save_access(access):
    os.makedirs(CONF, mode=0o700, exist_ok=True)
    fd = os.open(SF_ENV, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("# saved %s — treat this file like a password\n"
                 % datetime.datetime.now().isoformat(timespec="seconds"))
        fh.write("SIMPLEFIN_ACCESS_URL=%s\n" % access)

def claim(pasted):
    """Accept either a setup token (base64 of a one-use claim URL) or an access URL
       already claimed elsewhere. Errors say what to do about them."""
    token = "".join(pasted.split())

    if token.startswith("http"):                       # already an access URL
        if "@" not in token:
            raise ValueError("that looks like a URL but carries no credentials — "
                             "an access URL has the form https://user:pass@bridge…/simplefin")
        save_access(token)
        return True

    try:
        url = base64.b64decode(token + "=" * (-len(token) % 4)).decode().strip()
    except Exception:
        raise ValueError("that is not a SimpleFIN setup token — it should be one long "
                         "base64 string, no spaces or line breaks (%d characters pasted)"
                         % len(token))
    if not url.startswith("https://"):
        raise ValueError("the token decodes to %r, which is not an https claim URL. "
                         "Copy the setup token again from bridge.simplefin.org/simplefin/create"
                         % url[:60])
    # a browser user agent, or Cloudflare answers 403 "error code: 1010" for us
    req = urllib.request.Request(url, data=b"", method="POST", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=45) as res:
            access = res.read().decode().strip()
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()[:120]
        except Exception:
            pass
        if "1010" in body:
            raise ValueError("Cloudflare blocked the request before it reached the bridge "
                             "(code 1010). Your token is untouched — this is my problem to fix, "
                             "not a spent token.")
        if e.code in (403, 409):
            raise ValueError("the bridge rejected this token (HTTP %d). If it says already "
                             "claimed, it is spent — generate a fresh one at "
                             "bridge.simplefin.org/simplefin/create. %s" % (e.code, body))
        raise ValueError("the bridge said HTTP %d. %s" % (e.code, body))
    except Exception as e:
        raise ValueError("could not reach the bridge: %s" % str(e)[:120])
    if not access.startswith("http"):
        raise ValueError("the bridge returned something that is not an access URL: %r"
                         % access[:60])
    save_access(access)
    return True

def db_status():
    try:
        import fdb
        cur = fdb.connect().cursor()
        cur.execute("SELECT COUNT(*) n, MIN(posted_on) a, MAX(posted_on) b,"
                    " SUM(merchant_id IS NULL) orphans FROM txn")
        t = cur.fetchone()
        cur.execute("SELECT COUNT(*) n FROM merchant")
        m = cur.fetchone()
        cur.execute("SELECT COUNT(*) n, MAX(imported_at) last FROM import_file")
        f = cur.fetchone()
        cur.execute("SELECT a.code, COUNT(t.id) n FROM account a LEFT JOIN txn t"
                    " ON t.account_id = a.id GROUP BY a.code ORDER BY a.code")
        accts = [{"code": r["code"], "n": r["n"]} for r in cur.fetchall()]
        return {"ok": True, "transactions": t["n"], "first": str(t["a"] or ""),
                "last": str(t["b"] or ""), "unmatched": int(t["orphans"] or 0),
                "merchants": m["n"], "files": f["n"],
                "lastImport": str(f["last"] or ""), "accounts": accts}
    except Exception as e:
        return {"ok": False, "error": type(e).__name__ + ": " + str(e)[:160]}

def review_rows():
    """Everything waiting on a human decision, worst first: no category, then the
       biggest unreviewed spend. One row per merchant, with a real charge to look at."""
    import fdb
    cur = fdb.connect().cursor()
    cur.execute("SELECT cat_key, label, kind FROM category ORDER BY sort_order, label")
    cats = [{"key": r["cat_key"], "label": r["label"], "kind": r["kind"]} for r in cur.fetchall()]
    # what actually repeats is a fact about the charges, not a flag someone remembered to set
    cur.execute("SELECT merchant_id FROM v_recurring_candidates")
    repeats = {r["merchant_id"] for r in cur.fetchall()}
    cur.execute("""
        SELECT m.id, m.name, m.reviewed, m.is_recurring, m.notes, m.confidence,
               c.cat_key AS category,
               COUNT(t.id)          AS charges,
               COUNT(DISTINCT t.ym) AS months,
               COALESCE(SUM(-t.amount),0) AS total,
               MIN(t.posted_on)     AS first_seen,
               MAX(t.posted_on)     AS last_seen,
               SUBSTRING_INDEX(GROUP_CONCAT(t.description ORDER BY t.posted_on DESC
                                            SEPARATOR '\n'), '\n', 1) AS sample
          FROM merchant m
          LEFT JOIN txn t ON t.merchant_id = m.id AND t.amount < 0
          LEFT JOIN category c ON c.id = m.category_id
         GROUP BY m.id, m.name, m.reviewed, m.is_recurring, m.notes, m.confidence, c.cat_key
         ORDER BY (c.cat_key IS NULL) DESC, (m.confidence='guess') DESC, m.reviewed ASC,
                  total DESC""")
    rows = []
    for r in cur.fetchall():
        rows.append({"id": r["id"], "name": r["name"], "category": r["category"] or "",
                     "note": r["notes"] or "", "reviewed": bool(r["reviewed"]),
                     "recurring": bool(r["is_recurring"]) or r["id"] in repeats,
                     "confidence": r["confidence"],
                     "charges": int(r["charges"] or 0), "months": int(r["months"] or 0),
                     "total": float(r["total"] or 0),
                     "first": str(r["first_seen"] or ""), "last": str(r["last_seen"] or ""),
                     "sample": (r["sample"] or "")[:90]})
    return {"categories": cats, "rows": rows}

def review_save(rows):
    import fdb
    conn = fdb.connect(autocommit=False)
    cur = conn.cursor()
    cur.execute("SELECT cat_key, id FROM category")
    cats = {r["cat_key"]: r["id"] for r in cur.fetchall()}
    saved, bad = 0, []
    for r in rows:
        try:
            mid = int(r.get("id"))
        except Exception:
            continue
        cat = (r.get("category") or "").strip()
        note = (r.get("note") or "").strip()[:255]
        reviewed = 1 if r.get("reviewed") else 0
        if cat and cat not in cats:
            bad.append(cat); continue
        if cat:
            cur.execute("UPDATE merchant SET category_id=%s, notes=%s, reviewed=%s WHERE id=%s",
                        (cats[cat], note or None, reviewed, mid))
            cur.execute("UPDATE txn SET category_id=%s WHERE merchant_id=%s", (cats[cat], mid))
        else:
            cur.execute("UPDATE merchant SET notes=%s, reviewed=%s WHERE id=%s",
                        (note or None, reviewed, mid))
        saved += 1
    conn.commit()
    # leave a copy on disk too, so the answers are readable without the database
    try:
        os.makedirs(os.path.join(HERE, "review"), exist_ok=True)
        with open(os.path.join(HERE, "review", "answers.json"), "w") as fh:
            json.dump({"saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
                       "rows": rows}, fh, indent=1)
    except Exception:
        pass
    return {"saved": saved, "unknownCategories": sorted(set(bad))}

def _current_ym():
    """The month to call 'this month' is the latest one your data actually
       reaches, not today's calendar date -- a feed that stops on the 8th has
       nothing to say about the 9th, and pretending otherwise would show a
       grocery total that can only ever look artificially low."""
    import fdb
    cur = fdb.connect().cursor()
    cur.execute("SELECT MAX(posted_on) mx FROM txn WHERE is_transfer = 0")
    last = cur.fetchone()["mx"]
    return (last.strftime("%Y-%m"), last) if last else (None, None)

def budget_status():
    """Every discretionary category: what you have spent this month, the target
       you have set (if any), and what the three months before this one average
       out to, so a target you have not set yet still has a starting point."""
    import fdb
    ym, last = _current_ym()
    if not ym:
        return {"categories": [], "month": None, "asOf": None}
    month_start = ym + "-01"
    cur = fdb.connect().cursor()
    cur.execute("""
        SELECT c.cat_key, c.label,
               COALESCE(m.spent, 0)  AS spent_this_month,
               COALESCE(h.avg3, 0)   AS suggested,
               b.amount              AS budgeted,
               b.note
          FROM category c
          LEFT JOIN (SELECT category_id, SUM(-amount) AS spent
                       FROM txn
                      WHERE amount < 0 AND is_transfer = 0
                        AND DATE_FORMAT(posted_on, '%%Y-%%m') = %s
                      GROUP BY category_id) m ON m.category_id = c.id
          LEFT JOIN (SELECT category_id,
                            ROUND(SUM(-amount) / 3, 2) AS avg3
                       FROM txn
                      WHERE amount < 0 AND is_transfer = 0
                        AND posted_on >= DATE_SUB(%s, INTERVAL 3 MONTH)
                        AND posted_on <  %s
                      GROUP BY category_id) h ON h.category_id = c.id
          LEFT JOIN budget_line b ON b.category_id = c.id AND b.active = 1
         WHERE c.kind = 'variable'
           -- a category that is itself a parent (Food, over Groceries/Dining)
           -- never gets a transaction posted directly to it, so it would sit
           -- in this list forever at $0 spent with no way to ever track it
           AND NOT EXISTS (SELECT 1 FROM category ch WHERE ch.parent_id = c.id)
         ORDER BY c.sort_order""",
        (ym, month_start, month_start))
    cats = [{"key": r["cat_key"], "label": r["label"],
             "spent": float(r["spent_this_month"] or 0),
             "suggested": float(r["suggested"] or 0),
             "budgeted": float(r["budgeted"]) if r["budgeted"] is not None else None,
             "note": r["note"] or ""} for r in cur.fetchall()]
    return {"categories": cats, "month": ym, "asOf": str(last)[:10]}

def budget_build():
    """Fill in a starting target for every discretionary category that does not
       already have one, from the average of the three months before this one.
       A category you have already set is left alone -- edit that one directly
       rather than rebuilding over it."""
    import fdb
    ym, last = _current_ym()
    if not ym:
        return {"built": 0, "note": "no transactions loaded yet"}
    month_start = ym + "-01"
    conn = fdb.connect(autocommit=False)
    cur = conn.cursor()
    cur.execute("""
        SELECT c.id, c.cat_key, c.label,
               ROUND(SUM(-t.amount) / 3, 2) AS avg3
          FROM category c
          JOIN txn t ON t.category_id = c.id AND t.amount < 0 AND t.is_transfer = 0
         WHERE c.kind = 'variable'
           AND NOT EXISTS (SELECT 1 FROM category ch WHERE ch.parent_id = c.id)
           AND t.posted_on >= DATE_SUB(%s, INTERVAL 3 MONTH)
           AND t.posted_on <  %s
           -- ANY existing row means you already decided, even one you later
           -- cleared (active=0) - rebuilding must never resurrect a target
           -- you deliberately turned off.
           AND NOT EXISTS (SELECT 1 FROM budget_line b WHERE b.category_id = c.id)
         GROUP BY c.id, c.cat_key, c.label""",
        (month_start, month_start))
    rows = cur.fetchall()
    for r in rows:
        cur.execute("""INSERT INTO budget_line
                            (code, label, grp, kind, category_id, amount, note)
                        VALUES (%s,%s,'Everyday spending','spending',%s,%s,%s)
                        ON DUPLICATE KEY UPDATE
                            amount = VALUES(amount), note = VALUES(note), active = 1""",
                     (r["cat_key"], r["label"], r["id"], r["avg3"],
                      "built from your last 3 months, " + datetime.date.today().isoformat()))
    conn.commit()
    return {"built": len(rows), "categories": [r["cat_key"] for r in rows]}

def budget_set(category, amount):
    """Save or clear a manual target for one category. Clearing it (amount is
       null) removes the target rather than setting it to zero, so 'nothing set'
       and 'budgeted at $0' stay two different things."""
    import fdb
    conn = fdb.connect(autocommit=False)
    cur = conn.cursor()
    cur.execute("SELECT id, label FROM category WHERE cat_key = %s", (category or "",))
    c = cur.fetchone()
    if not c:
        raise ValueError("unknown category: %r" % category)
    if amount is None:
        cur.execute("UPDATE budget_line SET active = 0 WHERE category_id = %s", (c["id"],))
    else:
        amt = round(float(amount), 2)
        if amt < 0:
            raise ValueError("a budget cannot be negative")
        cur.execute("""INSERT INTO budget_line (code, label, grp, kind, category_id, amount, note)
                        VALUES (%s,%s,'Everyday spending','spending',%s,%s,'set by you')
                        ON DUPLICATE KEY UPDATE
                            amount = VALUES(amount), note = VALUES(note), active = 1""",
                     (category, c["label"], c["id"], amt))
    conn.commit()
    return {"ok": True}

def rewards_status():
    """For every linked card with reward rules on file: real spend over the
       trailing 12 months, what it likely earned against those rules, and
       whether a different card already in the wallet would have earned more
       for the same purchase. This is a simplification, stated up front
       rather than hidden: once a capped category is used up for the period,
       the WHOLE purchase that crossed the cap falls back to the base rate,
       rather than splitting one transaction across two rates. A rule marked
       'approximate' (an online-retailer guess, a drug-store merchant list, a
       travel-booking-channel assumption) is never presented with the same
       certainty as a rate that is simply true of every purchase in a
       category - see each rule's own note for why."""
    import fdb, datetime as dt
    from collections import defaultdict
    cur = fdb.connect().cursor()
    cur.execute("SELECT id, code, name FROM account WHERE kind = 'credit'")
    cards = {r["code"]: {"id": r["id"], "name": r["name"]} for r in cur.fetchall()}
    if not cards:
        return {"cards": [], "asOf": None}

    cur.execute("""SELECT account_code, label, match_kind, match_value, rate_pct,
                          reward_kind, point_value_cents, annual_cap, quarter_cap,
                          active_from, active_to, confidence, notes
                     FROM card_reward_rule
                    ORDER BY account_code, FIELD(match_kind, 'category', 'merchant_contains', 'all')""")
    rules_by_card = defaultdict(list)
    for r in cur.fetchall():
        rules_by_card[r["account_code"]].append(r)
    cards_with_rules = {c: cards[c] for c in cards if c in rules_by_card}
    if not cards_with_rules:
        return {"cards": [], "asOf": None,
                "note": "%d card%s linked, no reward rates on file yet for any of them."
                        % (len(cards), "" if len(cards) == 1 else "s")}

    cur.execute("SELECT MAX(posted_on) mx FROM txn WHERE is_transfer = 0")
    last = cur.fetchone()["mx"]
    if not last:
        return {"cards": [], "asOf": None}
    start = last - dt.timedelta(days=365)

    codes = tuple(cards_with_rules.keys())
    fmt = ",".join(["%s"] * len(codes))
    cur.execute("""SELECT a.code AS account_code, t.posted_on, t.amount, t.description, c.cat_key
                     FROM txn t JOIN account a ON a.id = t.account_id
                     LEFT JOIN category c ON c.id = t.category_id
                    WHERE t.direction = 'out' AND t.is_transfer = 0
                      AND a.code IN (%s) AND t.posted_on >= %%s""" % fmt,
                codes + (start,))
    rows = cur.fetchall()

    def rule_value(r):
        return (float(r["rate_pct"]) / 100
                * (float(r["point_value_cents"]) / 100 if r["reward_kind"] == "points" else 1.0))

    base_rule = {code: next((r for r in rs if r["match_kind"] == "all"), None)
                 for code, rs in rules_by_card.items()}

    def pick_rule(code, cat_key, desc, d):
        for r in rules_by_card[code]:
            if r["match_kind"] == "all":
                continue
            if r["active_from"] and d < r["active_from"]:
                continue
            if r["active_to"] and d > r["active_to"]:
                continue
            if r["match_kind"] == "category" and cat_key == r["match_value"]:
                return r
            if r["match_kind"] == "merchant_contains" and r["match_value"] \
               and r["match_value"].upper() in (desc or "").upper():
                return r
        return base_rule.get(code)

    # the best rate any linked card offers for a category, for the
    # "leaving money on the table" comparison
    best_for_cat = {}
    for code, rs in rules_by_card.items():
        for r in rs:
            if r["match_kind"] != "category":
                continue
            if r["active_from"] and last < r["active_from"]:
                continue
            if r["active_to"] and last > r["active_to"]:
                continue
            v = rule_value(r)
            if r["match_value"] not in best_for_cat or v > best_for_cat[r["match_value"]]["value"]:
                best_for_cat[r["match_value"]] = {"value": v, "card": cards[code]["name"],
                                                  "rate_pct": float(r["rate_pct"]), "label": r["label"]}

    per_card = {code: {"name": cards_with_rules[code]["name"], "total_spend": 0.0,
                        "reward_value": 0.0, "reward_kind": None,
                        "spend_by_cat": defaultdict(float), "rule_hits": defaultdict(float)}
                for code in codes}
    running = defaultdict(float)   # (code, rule_label, period_key) -> $ counted toward a cap so far

    def period_key(rule, d):
        if rule["quarter_cap"] is not None:
            return "%d-Q%d" % (d.year, (d.month - 1) // 3 + 1)
        if rule["annual_cap"] is not None:
            return str(d.year)
        return None

    for t in rows:
        code = t["account_code"]
        amt = float(-t["amount"])
        cat = t["cat_key"] or "uncategorised"
        d = t["posted_on"]
        pc = per_card[code]
        pc["total_spend"] += amt
        pc["spend_by_cat"][cat] += amt

        rule = pick_rule(code, cat, t["description"], d)
        if rule and rule["match_kind"] != "all":
            cap = rule["quarter_cap"] or rule["annual_cap"]
            if cap is not None:
                key = (code, rule["label"], period_key(rule, d))
                if running[key] + amt > float(cap):
                    rule = base_rule.get(code)   # this purchase crossed the cap; falls back whole
                else:
                    running[key] += amt
        if not rule:
            continue
        pc["reward_value"] += amt * rule_value(rule)
        pc["reward_kind"] = rule["reward_kind"]
        pc["rule_hits"][rule["label"]] += amt

    cards_out = []
    for code in codes:
        pc = per_card[code]
        leaving = []
        for cat, spent in pc["spend_by_cat"].items():
            best = best_for_cat.get(cat)
            if not best or spent <= 0.005:
                continue
            here_rule = next((r for r in rules_by_card[code]
                              if r["match_kind"] == "category" and r["match_value"] == cat), None)
            here_value = rule_value(here_rule) if here_rule else rule_value(base_rule[code])
            if best["value"] > here_value + 1e-9:
                leaving.append({"category": cat, "spent": round(spent, 2),
                                "here_pct": round(here_value * 100, 2), "best_pct": best["rate_pct"],
                                "best_card": best["card"],
                                "delta": round(spent * (best["value"] - here_value), 2)})
        leaving.sort(key=lambda x: -x["delta"])
        cards_out.append({
            "code": code, "name": pc["name"], "totalSpend": round(pc["total_spend"], 2),
            "rewardValue": round(pc["reward_value"], 2), "rewardKind": pc["reward_kind"],
            "effectiveRate": round(pc["reward_value"] / pc["total_spend"] * 100, 2) if pc["total_spend"] > 0.005 else 0,
            "byCategory": [{"category": c, "spent": round(v, 2)} for c, v in
                          sorted(pc["spend_by_cat"].items(), key=lambda x: -x[1])],
            "ruleHits": [{"label": k, "spent": round(v, 2)} for k, v in
                        sorted(pc["rule_hits"].items(), key=lambda x: -x[1])],
            "leavingOnTable": leaving,
            "rules": [{"label": r["label"], "ratePct": float(r["rate_pct"]), "matchKind": r["match_kind"],
                      "matchValue": r["match_value"], "confidence": r["confidence"], "notes": r["notes"],
                      "activeFrom": str(r["active_from"]) if r["active_from"] else None,
                      "activeTo": str(r["active_to"]) if r["active_to"] else None}
                     for r in rules_by_card[code]],
        })
    return {"cards": cards_out, "asOf": str(last)[:10], "windowDays": 365}

def run_sync(months):
    JOB.update(state="running", log=[], started=datetime.datetime.now().isoformat(timespec="seconds"),
               finished=None)
    script = os.path.join(HERE, "db", "simplefin_sync.py")
    try:
        p = subprocess.Popen([PY, script, "--months", str(months)],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in p.stdout:
            JOB["log"].append(line.rstrip()[:300])
            del JOB["log"][:-200]
        p.wait()
        JOB["state"] = "done" if p.returncode == 0 else "failed"
    except Exception as e:
        JOB["log"].append("crashed: %s" % e)
        JOB["state"] = "failed"
    JOB["finished"] = datetime.datetime.now().isoformat(timespec="seconds")

# --------------------------------------------------------------- profiles --
sys.path.insert(0, os.path.join(HERE, "db"))
import profile as prof
import bootstrap as boot

def boot_payload():
    """What gets injected into index.html. No profile means a first-run install,
       which is a working app with nothing in it, not a broken one."""
    p = prof.load()
    if not p:
        b = boot.empty()
        b["needsSetup"] = True
        b["profiles"] = prof.listing()
        b["commonExpenses"] = prof.COMMON_EXPENSES
        return b
    txns, accounts, due_days = [], p.get("accounts") or [], {}
    if p.get("settings", {}).get("warehouse"):
        try:
            import fdb
            conn = fdb.connect(); cur = conn.cursor()
            # The profile is the source of truth for which account is which -
            # it is what maps a bank's account code to a role like "primary" -
            # so accounts come from there. The warehouse only supplies the
            # transactions themselves.
            txns = boot.tx_from_warehouse(cur, accounts)
            # When did each recurring charge actually land? The warehouse knows,
            # so the Bills tab can show the real day instead of a guess. Only
            # steady payees qualify: three or more charges, all on a similar day.
            cur.execute("""SELECT COALESCE(m.name, t.description) AS payee,
                                  ROUND(AVG(DAY(t.posted_on))) AS day,
                                  STDDEV_POP(DAY(t.posted_on))  AS spread,
                                  COUNT(*) AS n
                             FROM txn t
                             LEFT JOIN merchant m ON m.id = t.merchant_id
                            WHERE t.is_transfer = 0 AND t.direction = 'out'
                              AND t.posted_on IS NOT NULL
                            GROUP BY payee
                           HAVING n >= 3 AND spread <= 4""")
            due_days = {r["payee"].upper(): int(r["day"]) for r in cur.fetchall()
                        if r["day"]}
            # Every real payee ever charged, no steadiness required - due_days
            # above only holds ones steady enough to trust for a day-of-month,
            # which is too strict a bar for "does a bill have any charges to
            # show at all" (the mortgage's own spread misses that bar).
            cur.execute("""SELECT DISTINCT COALESCE(m.name, t.description) AS payee
                             FROM txn t LEFT JOIN merchant m ON m.id = t.merchant_id
                            WHERE t.is_transfer = 0 AND t.direction = 'out'
                              AND t.posted_on IS NOT NULL""")
            all_payees = [r["payee"].upper() for r in cur.fetchall() if r["payee"]]
            conn.close()
        except Exception as e:
            print("[boot] warehouse unavailable: %s" % str(e)[:120], flush=True)
            all_payees = []
    else:
        all_payees = []
    b = boot.build(p, txns, accounts,
                   due_days if p.get('settings', {}).get('warehouse') else None,
                   all_payees)
    b["needsSetup"] = False
    b["profiles"] = prof.listing()
    b["commonExpenses"] = prof.COMMON_EXPENSES
    b["active"] = prof.active_name()
    roles = (p.get("settings") or {}).get("roles") or {}
    codes = [a.get("code") for a in accounts if a.get("code")]
    b["roles"] = {"primary": roles.get("primary") or (codes[0] if codes else ""),
                  "bills":   roles.get("bills")   or (codes[1] if len(codes) > 1 else ""),
                  "mortgage":roles.get("mortgage")or (codes[2] if len(codes) > 2 else "")}
    return b


# ------------------------------------------------------------------ setup --
# Four ways in. Each one ends with a profile on disk and, where there is data to
# load, a warehouse behind it. Nothing is written until the user picks a path.

def setup_status():
    return {"profiles": prof.listing(), "active": prof.active_name(),
            "commonExpenses": prof.COMMON_EXPENSES,
            "plaidConfigured": os.path.exists(os.path.expanduser(
                "~/.config/flightdeck/plaid.env")),
            "schema": GUI_SCHEMA}

def setup_sample():
    """Copy the sample rather than activating it in place, so the user can edit
       their own copy and the shipped one stays pristine."""
    d = json.load(open(os.path.join(prof.PROFILE_DIR, "demo.json")))
    d["name"] = "My copy of the sample"
    d["source"] = "sample-copy"
    name = prof.save(d, "sample-copy.json")
    prof.set_active(name)
    return {"ok": True, "file": name,
            "note": "Loaded the sample. Everything is editable and nothing here is real."}

def setup_wizard(payload):
    d = prof.blank(payload.get("name") or "My money")
    d["source"] = "wizard"
    d["income"] = payload.get("income") or []
    d["expenses"] = [e for e in (payload.get("expenses") or [])
                     if (e.get("name") or "").strip() and (e.get("monthly") or 0)]
    d["settings"]["warehouse"] = False
    name = prof.save(d, prof.safe_name(d["name"]) + ".json")
    prof.set_active(name)
    return {"ok": True, "file": name, "expenses": len(d["expenses"])}

def setup_csv(files, months_hint=None):
    """Load bank CSV exports. The range is whatever the files contain - there is
       nothing to ask the user, the answer is in the data."""
    import base64, glob, re as _re
    d = os.path.join(HERE, "data", "imports")
    os.makedirs(d, exist_ok=True)
    saved = []
    for f in files or []:
        safe = _re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(f.get("filename") or "import.csv"))[:120]
        raw = base64.b64decode(f.get("data") or "", validate=True)
        if len(raw) < 16:
            continue
        dest = os.path.join(d, safe)
        open(dest, "wb").write(raw)
        saved.append(dest)
    if not saved:
        raise ValueError("no usable CSV files were sent")
    p = subprocess.run([PY, os.path.join(HERE, "db", "load_csv.py"), "load"] + saved,
                       capture_output=True, text=True, timeout=600)
    log = p.stdout + p.stderr
    for step in ("promote", "classify"):
        q = subprocess.run([PY, os.path.join(HERE, "db", "load_csv.py"), step],
                           capture_output=True, text=True, timeout=600)
        log += q.stdout + q.stderr
    return {"ok": p.returncode == 0, "files": len(saved), "log": log[-6000:],
            "span": warehouse_span()}

def warehouse_span():
    try:
        conn = gui_connect(); cur = conn.cursor()
        cur.execute("""SELECT COUNT(*) n, MIN(txn_date) mn, MAX(txn_date) mx
                         FROM claudia_usaa_import""")
        r = cur.fetchone(); conn.close()
        return {"transactions": r["n"], "from": str(r["mn"] or ""), "to": str(r["mx"] or "")}
    except Exception as e:
        return {"error": str(e)[:160]}

def setup_plaid(months):
    """Start the Plaid link. The history window is asked for here because Plaid
       only honours it when the item is first created - it cannot be raised
       afterwards without unlinking and starting again."""
    months = max(1, min(24, int(months or 24)))
    script = os.path.join(HERE, "db", "plaid_link.py")
    env = dict(os.environ, FLIGHTDECK_PLAID_MONTHS=str(months))
    p = subprocess.Popen([PY, script, "--port", "8735"], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    JOB.update(state="linking", log=[], started=datetime.datetime.now().isoformat(timespec="seconds"))
    def pump():
        for line in p.stdout:
            JOB["log"].append(line.rstrip()[:300]); del JOB["log"][:-200]
        p.wait()
        JOB["state"] = "linked" if p.returncode == 0 else "failed"
    threading.Thread(target=pump, daemon=True).start()
    return {"ok": True, "months": months, "open": "https://localhost:8735/",
            "note": "Accept the certificate warning, then sign in to your bank in Plaid's window."}

# ---------------------------------------------------------------- receipts --
# The receipt tables live in `personal-finance-gui` on the Homebrew MySQL, which
# answers on ::1. The warehouse this file otherwise talks to is a container on
# 127.0.0.1. Same port, different servers; do not merge these two connections -
# FLIGHTDECK_SCHEMA picks the warehouse database on the OTHER server and must
# never leak in here, or run.sh's default of "flightdeck" makes this file look
# for a database of that name on the Homebrew server, where it does not exist.
GUI_SCHEMA = os.environ.get("FLIGHTDECK_GUI_SCHEMA", "personal-finance-gui")
RECEIPT_DIR = os.path.join(HERE, "data", "receipts")

def gui_connect():
    import pymysql
    env = {}
    path = os.path.expanduser("~/.config/flightdeck/mysql.env")
    for line in open(path):
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.strip().split("=", 1)
            env[k] = v
    return pymysql.connect(host="::1", port=int(env.get("MYSQL_PORT", 3306)),
                           user=env["MYSQL_USER"], password=env["MYSQL_PASSWORD"],
                           database=GUI_SCHEMA, charset="utf8mb4",
                           cursorclass=pymysql.cursors.DictCursor, autocommit=True)

def receipt_list():
    conn = gui_connect(); cur = conn.cursor()
    cur.execute("""SELECT id, purchased_at, merchant, store_no, total, items_parsed,
                          discounts, bank, health, parse_note, ocr_engine
                     FROM v_receipt_summary ORDER BY purchased_at DESC, id DESC""")
    rows = cur.fetchall()
    cur.execute("""SELECT dt, who, amount FROM v_receipt_vs_bank
                    WHERE issue = 'bank charge with no receipt uploaded'
                    ORDER BY dt DESC""")
    waiting = cur.fetchall()
    # Averaged per month actually on file, not a lifetime total mislabeled "/
    # mo" and then multiplied by 12 on top of that - the more receipts pile
    # up, the worse that would get, and it is already wrong with just one.
    cur.execute("""SELECT category,
                          ROUND(AVG(month_spent), 2) AS spent,
                          ROUND(AVG(month_spent) * 12, 2) AS spent_annual,
                          SUM(line_count) AS line_count
                     FROM (SELECT DATE_FORMAT(r.purchased_at, '%%Y-%%m') AS ym,
                                  COALESCE(i.category, 'uncategorised') AS category,
                                  SUM(i.line_total) AS month_spent, COUNT(*) AS line_count
                             FROM receipt_item i JOIN receipt r ON r.id = i.receipt_id
                            WHERE i.is_discount = 0
                            GROUP BY ym, category) monthly
                    GROUP BY category ORDER BY spent DESC""")
    cats = cur.fetchall()
    conn.close()
    return {"receipts": rows, "waiting": waiting, "categories": cats}

def receipt_detail(rid):
    conn = gui_connect(); cur = conn.cursor()
    # avg_paid/times_bought ride along per line so the page can show how this
    # price compares to what this item has cost across every other receipt,
    # without a second round trip.
    cur.execute("""SELECT d.line_no, d.item_code, d.as_printed, d.decoded, d.category, d.qty,
                          d.line_total, d.unit_price, d.tax_flag, d.tax_meaning,
                          d.price_ending, d.price_signal, d.is_discount,
                          h.times_bought, h.avg_paid, h.cheapest_paid, h.dearest_paid
                     FROM v_receipt_detail d
                     LEFT JOIN v_item_price_history h ON h.item_code = d.item_code
                    WHERE d.receipt_id = %s ORDER BY d.line_no""", (rid,))
    items = cur.fetchall()
    cur.execute("""SELECT o.item, o.paid, o.your_best_price, o.over_by, o.over_pct
                     FROM v_overpaid o JOIN receipt_item i
                       ON i.item_code = o.item_code AND i.receipt_id = %s
                    GROUP BY o.item, o.paid, o.your_best_price, o.over_by, o.over_pct""", (rid,))
    over = cur.fetchall()
    cur.execute("SELECT * FROM v_receipt_summary WHERE id = %s", (rid,))
    head = cur.fetchone()
    cur.execute("SELECT raw_text, mime, source_file, payment_last4, ocr_engine FROM receipt WHERE id = %s", (rid,))
    raw = cur.fetchone() or {}
    conn.close()
    return {"head": head, "items": items, "overpaid": over,
            "rawText": raw.get("raw_text"), "paymentLast4": raw.get("payment_last4"),
            "sourceFile": raw.get("source_file")}

def receipt_prices():
    """Every item bought more than once, cheapest-to-dearest history and all -
       the Price history tab's whole reason to exist."""
    conn = gui_connect(); cur = conn.cursor()
    cur.execute("""SELECT item_code, item, category, times_bought, first_bought, last_bought,
                          cheapest_paid, dearest_paid, avg_paid, spread, spent_total, verdict
                     FROM v_item_price_history
                    ORDER BY times_bought DESC, spent_total DESC""")
    rows = cur.fetchall()
    conn.close()
    return {"items": rows}

def receipt_price_series(code):
    conn = gui_connect(); cur = conn.cursor()
    cur.execute("""SELECT r.purchased_at, r.merchant, i.unit_price, i.description
                     FROM receipt_item i JOIN receipt r ON r.id = i.receipt_id
                    WHERE i.item_code = %s AND i.is_discount = 0
                    ORDER BY r.purchased_at""", (code,))
    rows = cur.fetchall()
    conn.close()
    return {"series": rows}

def receipt_eating():
    """Grocery category as a share of spend, month by month - only as real as
       the category column on each line, which today is mostly unset."""
    conn = gui_connect(); cur = conn.cursor()
    cur.execute("""SELECT ym, category, spent, spent_annual, lines_bought, receipts
                     FROM v_receipt_category_month ORDER BY ym, spent DESC""")
    rows = cur.fetchall()
    conn.close()
    return {"months": rows}

def receipt_save(filename, b64):
    """Write the upload to disk, then hand it to the ingest script. The file is
       kept so a parsing fix can be re-run against the original."""
    import base64, re as _re, subprocess
    safe = _re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(filename or "receipt"))[:120]
    if not safe or safe.startswith("."):
        safe = "receipt_" + datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    if not b64:
        raise ValueError("no file content was sent")
    raw = base64.b64decode(b64, validate=True)
    if len(raw) < 512:
        raise ValueError("that file is too small to be a receipt (%d bytes)" % len(raw))
    if len(raw) > 25 * 1024 * 1024:
        raise ValueError("file is larger than 25MB")
    os.makedirs(RECEIPT_DIR, exist_ok=True)
    dest = os.path.join(RECEIPT_DIR, safe)
    stem, ext = os.path.splitext(dest)
    n = 1
    while os.path.exists(dest):
        dest = "%s_%d%s" % (stem, n, ext); n += 1
    with open(dest, "wb") as f:
        f.write(raw)
    p = subprocess.run([PY, os.path.join(HERE, "db", "receipt_ingest.py"), dest],
                       capture_output=True, text=True, timeout=180)
    return {"ok": p.returncode == 0, "file": os.path.basename(dest),
            "log": (p.stdout + p.stderr)[-4000:]}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=HERE, **kw)

    def _send_index(self):
        """index.html on disk has a null placeholder where the data goes. It is
           filled in here, per request, so the file never holds anyone's money."""
        try:
            html = open(os.path.join(HERE, "index.html"), encoding="utf-8").read()
            payload = json.dumps(boot_payload(), default=Handler._jsonable)
            a = html.index("/*__FLIGHTDECK_BOOT__*/")
            b = html.index("/*__END_BOOT__*/") + len("/*__END_BOOT__*/")
            html = html[:a] + payload + html[b:]
        except Exception as e:
            return self._json({"error": "could not build the page: %s" % str(e)[:200]}, 500)
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass                                    # keep the console quiet

    def end_headers(self):
        # never let the browser hold an old copy of the app — a stale page talking to
        # a newer server is the most confusing failure there is
        if self.path.endswith((".html", "/")) or self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    @staticmethod
    def _jsonable(v):
        """MySQL hands back Decimal and date objects. Money stays exact by going
           out as a string; the page parses it. Never float a money value."""
        import decimal as _d
        if isinstance(v, _d.Decimal):
            return str(v)
        if isinstance(v, (datetime.datetime, datetime.date)):
            return v.isoformat(sep=" ")
        if isinstance(v, (bytes, bytearray)):
            return v.decode("utf-8", "replace")
        raise TypeError("not JSON serialisable: %r" % type(v))

    def _json(self, obj, code=200):
        body = json.dumps(obj, default=self._jsonable).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _local(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _same_origin(self):
        """A socket-address check alone lets any page in any tab drive this API:
           the browser connects from localhost on the attacker's behalf. Browsers
           always send Origin on a cross-site POST, so require it to be ours."""
        origin = self.headers.get("Origin")
        if origin is None:
            return True                      # curl, or a same-origin form post
        host = self.headers.get("Host") or ""
        return origin.split("://", 1)[-1] == host

    def do_GET(self):
        if self.path in ("/", "/index.html") or self.path.startswith("/index.html?"):
            return self._send_index()
        if self.path.startswith("/api/"):
            if not self._local():
                return self._json({"error": "local only"}, 403)
            if self.path.startswith("/api/review"):
                try:
                    return self._json(review_rows())
                except Exception as e:
                    return self._json({"error": type(e).__name__ + ": " + str(e)[:160]}, 500)
            if self.path.startswith("/api/setup"):
                return self._json(setup_status())
            if self.path.startswith("/api/span"):
                return self._json(warehouse_span())
            if self.path.startswith("/api/profiles"):
                return self._json({"profiles": prof.listing(),
                                   "active": prof.active_name(),
                                   "commonExpenses": prof.COMMON_EXPENSES})
            if self.path.startswith("/api/receipts/prices"):
                try:
                    return self._json(receipt_prices())
                except Exception as e:
                    return self._json({"error": type(e).__name__ + ": " + str(e)[:160]}, 500)
            if self.path.startswith("/api/receipts/price?"):
                try:
                    code = self.path.split("code=", 1)[1].split("&")[0]
                    return self._json(receipt_price_series(urllib.parse.unquote(code)))
                except Exception as e:
                    return self._json({"error": type(e).__name__ + ": " + str(e)[:160]}, 500)
            if self.path.startswith("/api/receipts/eating"):
                try:
                    return self._json(receipt_eating())
                except Exception as e:
                    return self._json({"error": type(e).__name__ + ": " + str(e)[:160]}, 500)
            if self.path.startswith("/api/receipts"):
                try:
                    return self._json(receipt_list())
                except Exception as e:
                    return self._json({"error": type(e).__name__ + ": " + str(e)[:160]}, 500)
            if self.path.startswith("/api/receipt?"):
                try:
                    rid = int(self.path.split("id=", 1)[1].split("&")[0])
                    return self._json(receipt_detail(rid))
                except Exception as e:
                    return self._json({"error": type(e).__name__ + ": " + str(e)[:160]}, 500)
            if self.path.startswith("/api/status"):
                return self._json({"db": db_status(),
                                   "simplefin": {"configured": bool(sf_access_url())},
                                   "job": JOB})
            if self.path.startswith("/api/budget"):
                try:
                    return self._json(budget_status())
                except Exception as e:
                    return self._json({"error": type(e).__name__ + ": " + str(e)[:160]}, 500)
            if self.path.startswith("/api/rewards"):
                try:
                    return self._json(rewards_status())
                except Exception as e:
                    return self._json({"error": type(e).__name__ + ": " + str(e)[:160]}, 500)
            return self._json({"error": "unknown endpoint"}, 404)
        return super().do_GET()

    def do_POST(self):
        if not self.path.startswith("/api/") or not self._local():
            return self._json({"error": "not found"}, 404)
        if not self._same_origin():
            return self._json({"error": "cross-origin write refused"}, 403)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._json({"error": "bad json"}, 400)

        if self.path.startswith("/api/setup/sample"):
            try: return self._json(setup_sample())
            except Exception as e: return self._json({"error": str(e)[:200]}, 400)

        if self.path.startswith("/api/setup/wizard"):
            try: return self._json(setup_wizard(body))
            except Exception as e: return self._json({"error": str(e)[:200]}, 400)

        if self.path.startswith("/api/setup/csv"):
            try: return self._json(setup_csv(body.get("files"), body.get("months")))
            except Exception as e: return self._json({"error": str(e)[:200]}, 400)

        if self.path.startswith("/api/setup/plaid"):
            try: return self._json(setup_plaid(body.get("months")))
            except Exception as e: return self._json({"error": str(e)[:200]}, 400)

        if self.path.startswith("/api/profile/activate"):
            try:
                prof.set_active(body.get("file") or "")
                return self._json({"ok": True, "active": prof.active_name()})
            except Exception as e:
                return self._json({"error": str(e)[:200]}, 400)

        if self.path.startswith("/api/profile/save"):
            try:
                d = body.get("profile") or {}
                name = prof.save(d, body.get("file"))
                prof.set_active(name)
                return self._json({"ok": True, "file": name})
            except Exception as e:
                return self._json({"error": str(e)[:200]}, 400)

        if self.path.startswith("/api/receipt"):
            try:
                return self._json(receipt_save(body.get("filename"), body.get("data") or ""))
            except Exception as e:
                return self._json({"error": type(e).__name__ + ": " + str(e)[:200]}, 400)

        if self.path.startswith("/api/budget/build"):
            try:
                return self._json(budget_build())
            except Exception as e:
                return self._json({"error": type(e).__name__ + ": " + str(e)[:200]}, 400)

        if self.path.startswith("/api/budget/set"):
            try:
                return self._json(budget_set(body.get("category"), body.get("amount")))
            except Exception as e:
                return self._json({"error": type(e).__name__ + ": " + str(e)[:200]}, 400)

        if self.path.startswith("/api/simplefin"):
            token = (body.get("token") or "").strip()
            if not token:
                return self._json({"error": "no token given"}, 400)
            try:
                claim(token)
            except Exception as e:
                # the token itself never goes in the log, only what went wrong with it
                print("[%s] simplefin claim failed: %s"
                      % (datetime.datetime.now().isoformat(timespec="seconds"), str(e)[:200]),
                      flush=True)
                return self._json({"error": str(e)[:200]}, 400)
            print("[%s] simplefin token claimed"
                  % datetime.datetime.now().isoformat(timespec="seconds"), flush=True)
            return self._json({"ok": True, "configured": True})

        if self.path.startswith("/api/review"):
            rows = body.get("rows")
            if not isinstance(rows, list):
                return self._json({"error": "expected a list of rows"}, 400)
            try:
                return self._json(review_save(rows[:2000]))
            except Exception as e:
                return self._json({"error": type(e).__name__ + ": " + str(e)[:160]}, 500)

        if self.path.startswith("/api/sync"):
            if not sf_access_url():
                return self._json({"error": "no SimpleFIN token saved yet"}, 400)
            if JOB["state"] == "running":
                return self._json({"error": "a sync is already running"}, 409)
            months = int(body.get("months") or 24)
            threading.Thread(target=run_sync, args=(max(1, min(60, months)),),
                             daemon=True).start()
            return self._json({"ok": True, "state": "running"})

        return self._json({"error": "unknown endpoint"}, 404)

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8730
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("Flightdeck on http://127.0.0.1:%d  (ctrl-c to stop)" % port)
    srv.serve_forever()
