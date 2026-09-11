#!/usr/bin/env python3
"""Build the sample dataset: one fictional person, eighteen months of money.

    python3 demo/generate_demo.py [--schema flightdeck_demo]

Jordan Reyes is invented. The numbers around Jordan are not: rent, utilities,
fuel and take-home pay are set from published figures for Huntington Beach and
for a single filer on $120,000 in California, so the demo behaves like a real
budget rather than a toy. Sources are listed in the README.

Everything here is deterministic - the same seed every run - so two people who
clone the repository see exactly the same demo and can compare notes.
"""
import argparse, datetime, decimal, hashlib, json, os, random, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "db"))
D = decimal.Decimal

random.seed(20260101)

TODAY = datetime.date(2026, 9, 9)
START = datetime.date(2025, 3, 17)          # ~18 months, enough to see a yearly bill twice

ACCOUNTS = [
    ("1 everyday-checking", "4417", "Chase Total Checking",   "checking"),
    ("2 bills-checking",    "2298", "Chase Bills Account",    "checking"),
    ("3 emergency-savings", "7731", "Ally Online Savings",    "savings"),
    ("4 travel-savings",    "6650", "Ally Travel Fund",       "savings"),
    ("5 rewards-card",      "9042", "Chase Freedom Unlimited","credit"),
]

# ---------------------------------------------------------------- the person --
# Which day of the month each bill lands. Without this every bill stacks on the
# 1st, and the app would read them all as already paid by mid-month.
BILL_DAYS = {"Rent, 1 bedroom off Beach Blvd":1, "Renters insurance":28, "Electric (SCE)":17,
             "Gas (SoCalGas)":20, "Water, sewer, trash":9, "Internet (Spectrum)":22,
             "Mobile (Verizon)":18, "Car payment":15, "Car insurance":24,
             "Gym (LA Fitness)":2, "Streaming":4, "Student loan":12, "Savings transfer":5}

PROFILE = {
    "version": 1,
    "name": "Sample — Jordan Reyes, Huntington Beach",
    "created": "2026-09-09",
    "currency": "USD",
    "source": "sample",
    "person": {
        "label": "Jordan Reyes",
        "location": "Huntington Beach, California",
        "employer": "Acme Corp",
        "note": "Fictional. Invented person, real local costs.",
    },
    "income": [{
        "label": "Acme Corp salary",
        "detail": "Senior Operations Analyst, paid every two weeks",
        "gross_annual": 120000,
        "net_monthly": 6634.00,
        "notes": "After federal, California state, FICA, the 1.2% SDI, and 6% into the 401(k).",
    }],
    "expenses": [
        {"name": "Rent, 1 bedroom off Beach Blvd", "category": "housing",   "monthly": 2495.00,
         "notes": "Mid-market for a Huntington Beach one bedroom in 2026."},
        {"name": "Renters insurance",     "category": "housing",   "monthly": 18.00,   "notes": ""},
        {"name": "Electric (SCE)",        "category": "utilities", "monthly": 112.00,
         "notes": "Southern California Edison. Higher Jul-Sep, it is a beach apartment with no AC upstairs."},
        {"name": "Gas (SoCalGas)",        "category": "utilities", "monthly": 38.00,   "notes": ""},
        {"name": "Water, sewer, trash",   "category": "utilities", "monthly": 62.00,
         "notes": "City of Huntington Beach, billed every two months."},
        {"name": "Internet (Spectrum)",   "category": "utilities", "monthly": 70.00,   "notes": ""},
        {"name": "Mobile (Verizon)",      "category": "utilities", "monthly": 85.00,   "notes": ""},
        {"name": "Car payment",           "category": "transport", "monthly": 421.00,
         "notes": "2023 Civic, 38 payments left."},
        {"name": "Car insurance",         "category": "transport", "monthly": 186.00,
         "notes": "California rates. Paid every six months, shown monthly."},
        {"name": "Fuel",                  "category": "transport", "monthly": 145.00,
         "notes": "About 10,000 miles a year at California pump prices."},
        {"name": "Groceries",             "category": "food",      "monthly": 480.00,  "notes": ""},
        {"name": "Eating out",            "category": "food",      "monthly": 320.00,
         "notes": "The number Jordan most wants to see honestly."},
        {"name": "Gym (LA Fitness)",      "category": "health",    "monthly": 39.00,   "notes": ""},
        {"name": "Streaming",             "category": "lifestyle", "monthly": 40.00,
         "notes": "Netflix, Spotify, Disney+."},
        {"name": "Student loan",          "category": "debt",      "monthly": 310.00,  "notes": ""},
        {"name": "Savings transfer",      "category": "savings",   "monthly": 900.00,
         "notes": "Automatic on payday. Emergency fund first, then the Japan trip."},
    ],
    "accounts": [{"acct_id": a, "code": c, "label": l, "kind": k} for a, c, l, k in ACCOUNTS],
    "goals": [
        {"name": "Emergency fund", "target": 20000, "saved": 11400, "by": "2027-06-30",
         "notes": "Three months of everything. The one that has to come first."},
        {"name": "Japan, two weeks", "target": 6000, "saved": 2150, "by": "2027-04-01",
         "notes": "Flights in April, cherry blossom season."},
        {"name": "House deposit", "target": 90000, "saved": 8300, "by": "2031-01-01",
         "notes": "Orange County. A long way off and Jordan knows it."},
    ],
    "debts": [
        {"name": "Civic auto loan", "balance": 15980, "apr": 6.4, "minimum": 421,
         "notes": "38 payments left."},
        {"name": "Student loan",    "balance": 21400, "apr": 5.5, "minimum": 310, "notes": ""},
        {"name": "Chase Freedom",   "balance": 1840,  "apr": 24.99, "minimum": 55,
         "notes": "Carried since the move. The expensive one."},
    ],
    "settings": {"warehouse": True},
}

# ------------------------------------------------------------ the ledger -----
# (payee, category, typical amount, jitter, account)
GROCERY = [("Ralphs", 78, 34), ("Trader Joe's", 61, 26), ("Costco Wholesale", 148, 62),
           ("Sprouts Farmers Market", 44, 19), ("Albertsons", 52, 22)]
DINING  = [("Sancho's Tacos", 22, 9), ("Bear Flag Fish Co", 31, 12), ("Sessions West Coast Deli", 18, 7),
           ("Chipotle", 15, 5), ("Starbucks", 7, 3), ("Blk Dot Coffee", 6, 2),
           ("Pacific City Food Hall", 34, 14), ("Sugar Shack Cafe", 19, 8),
           ("In-N-Out Burger", 12, 4), ("Duke's Huntington Beach", 68, 24)]
SHOPS   = [("Amazon", 38, 30), ("Target", 54, 28), ("Home Depot", 47, 33),
           ("REI Huntington Beach", 82, 45), ("Nordstrom Rack", 63, 30), ("Apple", 29, 22)]
FUEL    = [("Chevron", 52, 12), ("Costco Gas", 46, 10), ("Arco", 44, 11)]
FUN     = [("AMC Bella Terra", 21, 8), ("Huntington Beach Pier Parking", 5, 2),
           ("Surf City Beach Cruisers", 35, 15), ("Barnes & Noble", 24, 12)]

SUBS = [   # payee, amount, day of month, cadence in months
    ("Netflix",            17.99, 4,  1), ("Spotify",            11.99, 9,  1),
    ("Disney Plus",         9.99, 14, 1), ("LA Fitness",         39.00, 2,  1),
    ("Verizon Wireless",   85.00, 18, 1), ("Spectrum Internet",  70.00, 22, 1),
    ("iCloud+",             2.99, 26, 1), ("Adobe Creative Cloud",22.99, 11, 1),
    ("Amazon Prime",      139.00, 7, 12), ("AAA Membership",      69.00, 3, 12),
    ("NYTimes",            25.00, 16, 1),
]

def jitter(base, spread):
    return round(random.uniform(base - spread, base + spread), 2)

def rows():
    out = []
    def add(d, payee, amount, cat, acct, code):
        out.append({"date": d, "payee": payee, "amount": round(amount, 2),
                    "category": cat, "acct": acct, "code": code})

    CHK, CHKC = ACCOUNTS[0][0], ACCOUNTS[0][1]
    BIL, BILC = ACCOUNTS[1][0], ACCOUNTS[1][1]
    EMG, EMGC = ACCOUNTS[2][0], ACCOUNTS[2][1]
    TRV, TRVC = ACCOUNTS[3][0], ACCOUNTS[3][1]
    CRD, CRDC = ACCOUNTS[4][0], ACCOUNTS[4][1]

    # pay every second Friday
    d = START
    while d.weekday() != 4:
        d += datetime.timedelta(days=1)
    while d <= TODAY:
        add(d, "ACME CORP PAYROLL", 3063.00, "INCOME", CHK, CHKC)
        d += datetime.timedelta(days=14)

    m = datetime.date(START.year, START.month, 1)
    while m <= TODAY:
        def day(n):
            try: return datetime.date(m.year, m.month, n)
            except ValueError: return None

        # rent, and the transfer that funds the bills account
        for dt, payee, amt, cat, ac, cc in [
            (day(1),  "SEACLIFF PROPERTY MGMT RENT", -2495.00, "RENT_AND_UTILITIES", BIL, BILC),
            (day(1),  "TRANSFER TO BILLS 2298",      -2900.00, "TRANSFER_OUT",       CHK, CHKC),
            (day(1),  "TRANSFER FROM CHECKING 4417",  2900.00, "TRANSFER_IN",        BIL, BILC),
            (day(5),  "TRANSFER TO SAVINGS 7731",      -650.00,"TRANSFER_OUT",       CHK, CHKC),
            (day(5),  "TRANSFER FROM CHECKING 4417",    650.00,"TRANSFER_IN",        EMG, EMGC),
            (day(5),  "TRANSFER TO TRAVEL 6650",       -250.00,"TRANSFER_OUT",       CHK, CHKC),
            (day(5),  "TRANSFER FROM CHECKING 4417",    250.00,"TRANSFER_IN",        TRV, TRVC),
            (day(12), "NAVIENT STUDENT LOAN",          -310.00,"LOAN_PAYMENTS",      BIL, BILC),
            (day(15), "HONDA FINANCIAL SERVICES",      -421.00,"LOAN_PAYMENTS",      BIL, BILC),
            (day(20), "SOCALGAS",                       -jitter(38, 14), "RENT_AND_UTILITIES", BIL, BILC),
            (day(24), "STATE FARM AUTO",               -186.00,"GENERAL_SERVICES",   BIL, BILC),
            (day(28), "LEMONADE RENTERS INS",           -18.00,"GENERAL_SERVICES",   BIL, BILC),
        ]:
            if dt and START <= dt <= TODAY:
                add(dt, payee, amt, cat, ac, cc)

        # electric, higher in the warm months
        season = 1.45 if m.month in (7, 8, 9) else (0.85 if m.month in (1, 2, 12) else 1.0)
        if day(17) and START <= day(17) <= TODAY:
            add(day(17), "SO CAL EDISON", -round(112 * season * random.uniform(.9, 1.1), 2),
                "RENT_AND_UTILITIES", BIL, BILC)
        # water every other month
        if m.month % 2 == 1 and day(9) and START <= day(9) <= TODAY:
            add(day(9), "CITY OF HUNTINGTON BEACH UTIL", -jitter(124, 18),
                "RENT_AND_UTILITIES", BIL, BILC)
        # paying the card off
        if day(25) and START <= day(25) <= TODAY:
            add(day(25), "CHASE CREDIT CRD EPAY", -jitter(640, 210), "LOAN_PAYMENTS", CHK, CHKC)

        for payee, amt, dom, every in SUBS:
            dt = day(dom)
            if not dt or not (START <= dt <= TODAY):
                continue
            if every == 12 and m.month != (START.month + 1) % 12 + 1:
                continue
            add(dt, payee, -amt, "ENTERTAINMENT" if every == 1 else "GENERAL_SERVICES", CRD, CRDC)

        m = (m.replace(day=28) + datetime.timedelta(days=7)).replace(day=1)

    # day to day, on the card
    d = START
    while d <= TODAY:
        if random.random() < .34:
            p, b, s = random.choice(GROCERY); add(d, p, -jitter(b, s), "FOOD_AND_DRINK", CRD, CRDC)
        for _ in range(random.choice([0, 0, 0, 1, 1, 2])):
            p, b, s = random.choice(DINING);  add(d, p, -jitter(b, s), "FOOD_AND_DRINK", CRD, CRDC)
        if random.random() < .22:
            p, b, s = random.choice(SHOPS);   add(d, p, -jitter(b, s), "GENERAL_MERCHANDISE", CRD, CRDC)
        if random.random() < .17:
            p, b, s = random.choice(FUEL);    add(d, p, -jitter(b, s), "TRANSPORTATION", CRD, CRDC)
        if random.random() < .10:
            p, b, s = random.choice(FUN);     add(d, p, -jitter(b, s), "ENTERTAINMENT", CRD, CRDC)
        d += datetime.timedelta(days=1)

    # a few things that should stand out on the Unusual view
    add(datetime.date(2025, 12, 18), "SOUTHWEST AIRLINES", -486.40, "TRAVEL", CRD, CRDC)
    add(datetime.date(2026, 2, 14),  "PACIFIC DENTAL",     -742.00, "MEDICAL", CHK, CHKC)
    add(datetime.date(2026, 5, 2),   "MIDAS AUTO SERVICE", -1104.75,"TRANSPORTATION", CHK, CHKC)
    add(datetime.date(2026, 6, 27),  "ACME CORP BONUS",     2400.00,"INCOME", CHK, CHKC)
    add(datetime.date(2026, 8, 11),  "IRS TREAS 310 TAX REF",1187.00,"INCOME", CHK, CHKC)
    return sorted(out, key=lambda r: (r["date"], r["payee"]))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--schema", default="flightdeck_demo")
    ap.add_argument("--sql-only", action="store_true")
    a = ap.parse_args()

    os.makedirs(os.path.join(ROOT, "profiles"), exist_ok=True)
    for e in PROFILE["expenses"]:
        if e["name"] in BILL_DAYS:
            e["day"] = BILL_DAYS[e["name"]]
    for a in PROFILE["accounts"]:
        a.setdefault("balance", {"1 everyday-checking":2840.00, "2 bills-checking":1310.00,
                                 "3 emergency-savings":11400.00, "4 travel-savings":2150.00,
                                 "5 rewards-card":-1840.00}.get(a["acct_id"], 0))
    PROFILE["settings"]["roles"] = {"primary":"4417", "bills":"2298", "mortgage":"2298"}
    with open(os.path.join(ROOT, "profiles", "demo.json"), "w") as f:
        json.dump(PROFILE, f, indent=2)

    r = rows()
    out = [ "-- Generated by demo/generate_demo.py. Do not edit by hand.",
            "USE `%s`;" % a.schema, "DELETE FROM claudia_usaa_import;",
            "DELETE FROM account;" ]
    out.append("INSERT INTO account (acct_id, account_code, label, kind) VALUES\n" +
               ",\n".join("  ('%s','%s','%s','%s')" % t for t in ACCOUNTS) + ";")
    vals = []
    for i, x in enumerate(r, 1):
        tid = "demo-%s" % hashlib.sha256(
            ("%s|%s|%.2f|%d" % (x["date"], x["payee"], x["amount"], i)).encode()).hexdigest()[:24]
        vals.append("('demo.json','%s',0,'%s','%s','%s','',%s,'%s','%s',%.2f,1)" % (
            hashlib.sha256(tid.encode()).hexdigest(), tid, x["acct"], x["code"],
            "'%s'" % x["date"], x["payee"].replace("'", "''"),
            x["category"], x["amount"]))
    for i in range(0, len(vals), 400):
        out.append("INSERT INTO claudia_usaa_import (source_file, source_sha256, line_no,"
                   " external_id, acct_id, account_code, raw_line, txn_date, description,"
                   " col_category, amount, parse_ok) VALUES\n" + ",\n".join(vals[i:i+400]) + ";")
    sql = "\n".join(out) + "\n"
    p = os.path.join(HERE, "demo_data.sql")
    open(p, "w").write(sql)
    print("profiles/demo.json written")
    print("demo/demo_data.sql written: %d transactions, %s to %s"
          % (len(r), r[0]["date"], r[-1]["date"]))

if __name__ == "__main__":
    main()
