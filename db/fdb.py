"""Shared MySQL access for the Flightdeck warehouse. Credentials live in
~/.config/flightdeck/mysql.env (chmod 600) and never in the repo."""
import os, pymysql

ENV = os.path.expanduser("~/.config/flightdeck/mysql.env")

def env():
    cfg = {}
    with open(ENV) as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    return cfg

def connect(autocommit=True):
    c = env()
    # run.sh --demo exports FLIGHTDECK_SCHEMA so the demo runs against its own
    # database. Without this override every fdb.connect() caller — status,
    # sync, review — ignores it and reads and writes the real schema instead.
    schema = os.environ.get("FLIGHTDECK_SCHEMA") or c.get("MYSQL_DB", "flightdeck")
    return pymysql.connect(
        host=c.get("MYSQL_HOST", "127.0.0.1"), port=int(c.get("MYSQL_PORT", 3306)),
        user=c.get("MYSQL_USER", "flightdeck"), password=c.get("MYSQL_PASSWORD", ""),
        database=schema, charset="utf8mb4",
        autocommit=autocommit, cursorclass=pymysql.cursors.DictCursor)
