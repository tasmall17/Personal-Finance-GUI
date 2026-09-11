#!/usr/bin/env bash
# Launch Flightdeck. --demo runs the sample dataset on its own port and its own
# database, so it can sit beside your real one without touching it.
set -euo pipefail
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { echo "Run ./setup.sh first."; exit 1; }
# serve.py reads this file on its first database query. Without it the app
# starts and then throws a traceback at the browser instead of saying why.
[ -f "$HOME/.config/flightdeck/mysql.env" ] || {
  echo "No database credentials at ~/.config/flightdeck/mysql.env."
  echo "Run ./setup.sh to create them."
  exit 1
}

SCHEMA="${FLIGHTDECK_SCHEMA:-flightdeck}"
PORT="${FLIGHTDECK_PORT:-8730}"
PROFILE=""

for a in "$@"; do
  case "$a" in
    --demo)   SCHEMA="${SCHEMA%_demo}_demo"; PORT=8740; PROFILE="demo.json" ;;
    --port=*) PORT="${a#--port=}" ;;
    --schema=*) SCHEMA="${a#--schema=}" ;;
    -h|--help)
      cat <<'USAGE'
./run.sh                 your own data, on http://127.0.0.1:8730
./run.sh --demo          the sample person, on http://127.0.0.1:8740
./run.sh --port=8899     somewhere else
./run.sh --schema=NAME   a different database

Press ctrl-c to stop. Set up first with ./setup.sh.
USAGE
      exit 0 ;;
    *) echo "unknown option: $a"; echo "try: ./run.sh --help"; exit 2 ;;
  esac
done

export FLIGHTDECK_SCHEMA="$SCHEMA"
[ -n "$PROFILE" ] && export FLIGHTDECK_PROFILE="$PROFILE"

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Something is already listening on port $PORT."
  echo "Stop it, or pick another with: ./run.sh --port=8731"
  exit 1
fi

echo "Flightdeck on http://127.0.0.1:$PORT   (database: $SCHEMA)"
echo "Press ctrl-c to stop."
exec ./.venv/bin/python serve.py "$PORT"
