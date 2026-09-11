#!/usr/bin/env python3
"""Pull everything Firefly III holds into the Flightdeck warehouse.

    sync_firefly.py            # export -> load -> promote -> classify
    sync_firefly.py --keep     # leave the intermediate CSVs on disk

Firefly stays the source of truth: SimpleFIN feeds it, this reads it. Re-running is
safe — files are hashed and transactions are deduped, so only new rows land.
"""
import os, subprocess, sys, datetime
import fdb

HERE = os.path.dirname(os.path.abspath(__file__))
OUT  = os.path.join(os.path.dirname(HERE), "data", "firefly")
PG   = "github-db-1"          # the Firefly postgres container

QUERY = """
COPY (
  SELECT to_char(tj.date,'MM/DD/YYYY') AS "Date",
         tj.description                AS "Description",
         opp.name                      AS "Original Description",
         COALESCE(c.name,'')           AS "Category",
         to_char(t.amount,'FM9999999990.00') AS "Amount",
         'Posted'                      AS "Status"
  FROM transactions t
  JOIN transaction_journals tj ON tj.id = t.transaction_journal_id AND tj.deleted_at IS NULL
  JOIN accounts a  ON a.id = t.account_id
  JOIN account_types at ON at.id = a.account_type_id
  JOIN transactions t2 ON t2.transaction_journal_id = tj.id AND t2.id <> t.id
  JOIN accounts opp ON opp.id = t2.account_id
  LEFT JOIN category_transaction_journal ctj ON ctj.transaction_journal_id = tj.id
  LEFT JOIN categories c ON c.id = ctj.category_id
  WHERE t.deleted_at IS NULL
    AND at.type IN ('Asset account','Debt')
    AND a.name LIKE '%{code}%'
  ORDER BY tj.date, tj.id
) TO STDOUT WITH (FORMAT csv, HEADER true);
"""

def psql(sql):
    p = subprocess.run(["docker", "exec", "-i", PG, "sh", "-c",
                        'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -q'],
                       input=sql.encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode:
        sys.exit("postgres failed: " + p.stderr.decode()[:300])
    return p.stdout.decode()

def main(keep):
    conn = fdb.connect()
    cur = conn.cursor()
    cur.execute("SELECT code FROM account ORDER BY code")
    codes = [r["code"] for r in cur.fetchall()]

    span = psql("SELECT COALESCE(to_char(MIN(date),'YYYY-MM-DD'),'-') || ' ' || "
                "COALESCE(to_char(MAX(date),'YYYY-MM-DD'),'-') || ' ' || COUNT(*) "
                "FROM transaction_journals WHERE deleted_at IS NULL;").strip().split()
    print("Firefly holds %s journals, %s to %s" % (span[2], span[0], span[1]))

    os.makedirs(OUT, exist_ok=True)
    stamp = datetime.date.today().isoformat()
    written = []
    for code in codes:
        rows = psql(QUERY.format(code=code))
        n = max(0, len(rows.strip().splitlines()) - 1)
        if n == 0:
            continue
        path = os.path.join(OUT, "firefly_%s_%s.csv" % (code, stamp))
        with open(path, "w") as fh:
            fh.write(rows)
        written.append(path)
        print("  %s  %5d rows" % (code, n))
    if not written:
        sys.exit("nothing to export — is Firefly empty?")

    loader = os.path.join(HERE, "load_csv.py")
    py = sys.executable
    for stage in (["load", OUT], ["promote"], ["classify"]):
        print("\n$ load_csv.py " + " ".join(stage))
        subprocess.run([py, loader] + stage, check=False)

    if not keep:
        for p in written:
            os.remove(p)

    cur.execute("SELECT COUNT(*) n, MIN(posted_on) a, MAX(posted_on) b,"
                " SUM(merchant_id IS NULL) orphans FROM txn")
    r = cur.fetchone()
    print("\nwarehouse: %d transactions, %s to %s, %s still unmatched"
          % (r["n"], r["a"], r["b"], r["orphans"]))

if __name__ == "__main__":
    main("--keep" in sys.argv)
