#!/usr/bin/env python3
"""The profile document: everything the app knows about one person's money.

A profile is a single JSON file in profiles/. Drop one in and the app works;
take it out and the app asks you to build one. Nothing about a person is
compiled into the page, so the repository never carries anyone's finances.

    profiles/demo.json      ships with the repo, fictional
    profiles/<yours>.json   yours, ignored by git

The shape is deliberately flat and readable. You are meant to be able to open
it in a text editor and understand every line.
"""
import json, os, re, datetime

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILE_DIR = os.path.join(HERE, "profiles")
ACTIVE = os.path.join(PROFILE_DIR, ".active")

VERSION = 1

# What the onboarding wizard puts in front of a new user. Not a budget, just a
# memory jogger: most people forget three or four of these until they see them.
COMMON_EXPENSES = [
    {"name": "Rent or mortgage",        "category": "housing",   "monthly": None},
    {"name": "Renters or home insurance","category": "housing",  "monthly": None},
    {"name": "Electric",                "category": "utilities", "monthly": None},
    {"name": "Gas",                     "category": "utilities", "monthly": None},
    {"name": "Water, sewer and trash",  "category": "utilities", "monthly": None},
    {"name": "Internet",                "category": "utilities", "monthly": None},
    {"name": "Mobile phone",            "category": "utilities", "monthly": None},
    {"name": "Car payment",             "category": "transport", "monthly": None},
    {"name": "Car insurance",           "category": "transport", "monthly": None},
    {"name": "Fuel",                    "category": "transport", "monthly": None},
    {"name": "Parking or transit",      "category": "transport", "monthly": None},
    {"name": "Groceries",               "category": "food",      "monthly": None},
    {"name": "Eating out",              "category": "food",      "monthly": None},
    {"name": "Health insurance",        "category": "health",    "monthly": None},
    {"name": "Prescriptions",           "category": "health",    "monthly": None},
    {"name": "Gym or fitness",          "category": "health",    "monthly": None},
    {"name": "Student loan",            "category": "debt",      "monthly": None},
    {"name": "Credit card payment",     "category": "debt",      "monthly": None},
    {"name": "Streaming services",      "category": "lifestyle", "monthly": None},
    {"name": "Childcare",               "category": "family",    "monthly": None},
    {"name": "Pet care",                "category": "family",    "monthly": None},
    {"name": "Savings transfer",        "category": "savings",   "monthly": None},
]

def blank(name="My money"):
    return {
        "version": VERSION,
        "name": name,
        "created": datetime.date.today().isoformat(),
        "currency": "USD",
        "source": "wizard",          # wizard | sample | plaid | csv
        "person": {"label": "", "location": "", "employer": ""},
        "income": [],                # {label, gross_annual, net_monthly, notes}
        "expenses": [],              # {name, category, monthly, annual, notes}
        "accounts": [],              # {acct_id, code, label, kind}
        "goals": [],                 # {name, target, saved, by, notes}
        "debts": [],                 # {name, balance, apr, minimum, notes}
        "settings": {"warehouse": False},   # true once real transactions are loaded
    }

def safe_name(s):
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", (s or "profile").strip()).strip("-.")
    return (s or "profile")[:60]

def path_for(name):
    return os.path.join(PROFILE_DIR, safe_name(name) + ".json")

def listing():
    os.makedirs(PROFILE_DIR, exist_ok=True)
    out = []
    for f in sorted(os.listdir(PROFILE_DIR)):
        if not f.endswith(".json"):
            continue
        p = os.path.join(PROFILE_DIR, f)
        try:
            d = json.load(open(p))
        except Exception:
            continue
        out.append({"file": f, "name": d.get("name") or f[:-5],
                    "source": d.get("source"), "created": d.get("created"),
                    "incomes": len(d.get("income") or []),
                    "expenses": len(d.get("expenses") or []),
                    "is_sample": f == "demo.json"})
    return out

def active_name():
    # An instance can be pinned to one profile with an environment variable, so
    # the demo and your own data can run side by side on different ports without
    # fighting over which profile is "current".
    pinned = os.environ.get("FLIGHTDECK_PROFILE")
    if pinned:
        return pinned if os.path.exists(os.path.join(PROFILE_DIR, pinned)) else None
    if os.path.exists(ACTIVE):
        n = open(ACTIVE).read().strip()
        if n and os.path.exists(os.path.join(PROFILE_DIR, n)):
            return n
    return None

def set_active(filename):
    os.makedirs(PROFILE_DIR, exist_ok=True)
    if not os.path.exists(os.path.join(PROFILE_DIR, filename)):
        raise ValueError("no such profile: %s" % filename)
    with open(ACTIVE, "w") as f:
        f.write(filename)

def load(filename=None):
    filename = filename or active_name()
    if not filename:
        return None
    p = os.path.join(PROFILE_DIR, filename)
    if not os.path.exists(p):
        return None
    d = json.load(open(p))
    return fill(d)

def fill(d):
    """Derive what can be derived, so the page never has to. Annual is always
       twelve times monthly and monthly is always a twelfth of annual: the same
       number said two ways, never two numbers that can disagree."""
    for e in d.get("expenses", []):
        m, a = e.get("monthly"), e.get("annual")
        if m is not None and a is None:
            e["annual"] = round(float(m) * 12, 2)
        elif a is not None and m is None:
            e["monthly"] = round(float(a) / 12, 2)
    for i in d.get("income", []):
        if i.get("net_monthly") is not None and i.get("net_annual") is None:
            i["net_annual"] = round(float(i["net_monthly"]) * 12, 2)
    d["totals"] = totals(d)
    return d

def totals(d):
    inc = sum(float(i.get("net_monthly") or 0) for i in d.get("income", []))
    exp = sum(float(e.get("monthly") or 0) for e in d.get("expenses", []))
    return {"income_monthly": round(inc, 2), "income_annual": round(inc * 12, 2),
            "expense_monthly": round(exp, 2), "expense_annual": round(exp * 12, 2),
            "left_monthly": round(inc - exp, 2), "left_annual": round((inc - exp) * 12, 2)}

def save(d, filename=None):
    os.makedirs(PROFILE_DIR, exist_ok=True)
    d["version"] = VERSION
    filename = filename or safe_name(d.get("name")) + ".json"
    if filename == "demo.json":
        raise ValueError("the sample profile is read-only; save under another name")
    p = os.path.join(PROFILE_DIR, filename)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, p)
    return filename
