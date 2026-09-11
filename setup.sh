#!/usr/bin/env bash
# Flightdeck installer. Safe to run more than once: it never drops a database
# and never overwrites credentials that already exist.
set -euo pipefail
cd "$(dirname "$0")"

BOLD=$'\033[1m'; DIM=$'\033[2m'; GRN=$'\033[32m'; YLW=$'\033[33m'; RED=$'\033[31m'; OFF=$'\033[0m'
say()  { printf "%s\n" "$*"; }
step() { printf "\n${BOLD}%s${OFF}\n" "$*"; }
ok()   { printf "  ${GRN}ok${OFF}  %s\n" "$*"; }
warn() { printf "  ${YLW}!${OFF}   %s\n" "$*"; }
die()  { printf "\n${RED}stopped${OFF}  %s\n\n" "$*" >&2; exit 1; }

CONF="$HOME/.config/flightdeck"
ENVF="$CONF/mysql.env"
SCHEMA="${FLIGHTDECK_SCHEMA:-flightdeck}"
PORT="${FLIGHTDECK_PORT:-8730}"

cat <<BANNER

${BOLD}Flightdeck${OFF}
A budgeting app that reads your own bank data and keeps it on your machine.
Nothing is uploaded anywhere. This installer will tell you before it changes
anything on your computer.

BANNER

# ---------------------------------------------------------------- platform --
step "1. Checking what you already have"
OS="$(uname -s)"
[ "$OS" = "Darwin" ] || warn "This installer is written for macOS. On Linux, install python3 and mysql-server with your package manager, then run ./run.sh."

need_brew=0
if ! command -v brew >/dev/null 2>&1; then
  need_brew=1
  warn "Homebrew is not installed"
else
  ok "Homebrew $(brew --version | head -1 | awk '{print $2}')"
fi
if [ "$need_brew" = "1" ]; then
  cat <<'MSG'

  Homebrew installs the things this app needs. It is the standard package
  manager for macOS. Install it by pasting this into Terminal, then run this
  setup again:

    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

MSG
  exit 1
fi

command -v python3 >/dev/null 2>&1 && ok "python3 $(python3 -V 2>&1 | awk '{print $2}')" || {
  say "  installing python3…"; brew install python@3.12 >/dev/null; ok "python3 installed"; }

if command -v mysql >/dev/null 2>&1; then
  ok "mysql $(mysql --version | sed -E 's/.*Ver ([0-9.]+).*/\1/')"
else
  say "  installing mysql… (a few minutes)"
  brew install mysql >/dev/null
  ok "mysql installed"
fi

if command -v pdftotext >/dev/null 2>&1; then ok "pdftotext (for PDF receipts)"
else say "  installing poppler for PDF receipts…"; brew install poppler >/dev/null; ok "poppler installed"; fi

# ------------------------------------------------------------------ mysql ---
step "2. Finding the database server"

# macOS can end up with two MySQL servers both claiming port 3306: one from
# Homebrew, which answers on the unix socket and on ::1, and one in a Docker
# container, which publishes only on 127.0.0.1. They are different servers
# holding different data. Picking the wrong one loads every table into a
# database the app will never read, and setup would still say it worked — so
# look before choosing, and refuse to guess when both are up.
DBPORT="${FLIGHTDECK_MYSQL_PORT:-3306}"
DOCKER_MYSQL=0
if command -v docker >/dev/null 2>&1 &&
   docker ps --format '{{.Names}}' 2>/dev/null | grep -qx 'flightdeck-mysql'; then
  DOCKER_MYSQL=1
fi
BREW_MYSQL=0
mysqladmin --protocol=SOCKET ping --silent >/dev/null 2>&1 && BREW_MYSQL=1

if [ -n "${FLIGHTDECK_MYSQL_HOST:-}" ]; then
  DBHOST="$FLIGHTDECK_MYSQL_HOST"
  ok "using MySQL on $DBHOST:$DBPORT (FLIGHTDECK_MYSQL_HOST is set)"
elif [ "$DOCKER_MYSQL" = "1" ] && [ "$BREW_MYSQL" = "1" ]; then
  cat <<'MSG'

  Two different MySQL servers are running on port 3306 on this machine:

    - a Homebrew one, reachable on ::1 and the unix socket
    - the Docker container 'flightdeck-mysql', reachable on 127.0.0.1

  They hold different data, and this installer will not guess which one you
  meant. Pick one and run setup again:

    FLIGHTDECK_MYSQL_HOST=127.0.0.1 ./setup.sh    # the Docker container
    FLIGHTDECK_MYSQL_HOST=::1       ./setup.sh    # the Homebrew server

  Or stop the one you do not want:  docker compose down
                                    brew services stop mysql

MSG
  die "Two MySQL servers on port 3306; tell me which one to use."
elif [ "$DOCKER_MYSQL" = "1" ]; then
  DBHOST=127.0.0.1
  ok "found the Docker container 'flightdeck-mysql' on 127.0.0.1:$DBPORT"
else
  # Homebrew. serve.py's receipt connection is pinned to ::1, so use ::1 here
  # too and the two halves of the app agree about which server they mean.
  DBHOST="::1"
  if [ "$BREW_MYSQL" = "1" ]; then
    ok "MySQL is already running"
  else
    if ! brew services list 2>/dev/null | grep -q '^mysql '; then
      die "A 'mysql' command exists but it is not managed by Homebrew, and it is not running.
          Start your MySQL however you normally do, then run this setup again.
          Or, to let Homebrew install and manage one:  brew install mysql"
    fi
    say "  starting MySQL…"
    brew services start mysql >/dev/null 2>&1 || true
    for i in $(seq 1 30); do
      mysqladmin --protocol=SOCKET ping --silent >/dev/null 2>&1 && break
      printf "."; sleep 1
    done
    printf "\n"
    mysqladmin --protocol=SOCKET ping --silent >/dev/null 2>&1 ||
      die "MySQL would not start within 30 seconds. Try: brew services restart mysql
          and then look at:  brew services info mysql"
    ok "MySQL started"
  fi
fi

# Everything from here on talks to exactly that server, over TCP, so the
# credentials we write below describe the same server we are setting up.
# MYSQL_PWD rather than -p so the password never appears in `ps` output, and
# so mysql stops printing "Using a password on the command line is insecure"
# in front of every step.
ROOT_PW=""
mysql_root() { MYSQL_PWD="$ROOT_PW" mysql --protocol=TCP -h "$DBHOST" -P "$DBPORT" -u root "$@"; }

# Distinguish "wrong password" from "nothing is answering". Asking for a
# password when the server is simply not there sends people hunting for a
# password that was never the problem.
root_error() { mysql_root -e "SELECT 1" 2>&1 >/dev/null || true; }
ERR="$(root_error)"
if [ -n "$ERR" ]; then
  # The container ships with a known root password; try it before asking.
  if [ "$DOCKER_MYSQL" = "1" ]; then ROOT_PW="flightdeck-local-only"; ERR="$(root_error)"; fi
fi
if [ -n "$ERR" ]; then
  case "$ERR" in
    *"Access denied"*) : ;;   # a password problem; ask below
    *) die "Could not reach a MySQL server at $DBHOST:$DBPORT.
          MySQL said: $ERR
          Check it is running:  lsof -nP -iTCP:$DBPORT -sTCP:LISTEN" ;;
  esac
  ROOT_PW=""
  [ -t 0 ] || die "MySQL on $DBHOST:$DBPORT needs an administrator password, and there is no
          terminal here to ask on. Run ./setup.sh directly in Terminal."
  for try in 1 2 3; do
      say ""
      say "  MySQL on $DBHOST:$DBPORT needs its administrator (root) password."
      say "  A Homebrew MySQL usually has none — press return if you never set one."
      read -r -s -p "  root password: " ROOT_PW || ROOT_PW=""; say ""
      mysql_root -e "SELECT 1" >/dev/null 2>&1 && break
      warn "That was not accepted."
      [ "$try" = "3" ] && die "Could not sign in to MySQL on $DBHOST:$DBPORT as root.
          If you have forgotten it, Homebrew's server can be reset with:
            brew services stop mysql && mysqld_safe --skip-grant-tables"
  done
fi
ok "signed in to MySQL on $DBHOST:$DBPORT as root"

# ------------------------------------------------------------ credentials ---
step "3. Credentials"
mkdir -p "$CONF"; chmod 700 "$CONF"
NEW_CREDS=0
if [ -f "$ENVF" ]; then
  ok "keeping the credentials already in $ENVF"
  DBPASS="$(awk -F= '/^MYSQL_PASSWORD=/{print substr($0,index($0,"=")+1)}' "$ENVF")"
  DBUSER="$(awk -F= '/^MYSQL_USER=/{print $2}' "$ENVF")"
  EHOST="$(awk -F= '/^MYSQL_HOST=/{print $2}' "$ENVF")"
  if [ -n "$EHOST" ] && [ "$EHOST" != "$DBHOST" ]; then
    warn "$ENVF says MYSQL_HOST=$EHOST but this run is setting up $DBHOST."
    warn "Leaving the file alone. If the app cannot see its data, that is why."
  fi
else
  NEW_CREDS=1
  DBUSER="flightdeck"
  DBPASS="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 28)"
  umask 077
  cat > "$ENVF" <<ENV
MYSQL_HOST=$DBHOST
MYSQL_PORT=$DBPORT
MYSQL_DB=$SCHEMA
MYSQL_USER=$DBUSER
MYSQL_PASSWORD=$DBPASS
ENV
  chmod 600 "$ENVF"
  ok "wrote $ENVF (mode 600, never leaves your machine)"
fi

# ---------------------------------------------------------------- schema ----
step "4. Database and tables"
EXISTS=$(mysql_root -N -e "SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name='$SCHEMA'")
if [ "$EXISTS" = "1" ]; then
  N=$(mysql_root -N "$SCHEMA" -e "SELECT COUNT(*) FROM claudia_usaa_import" 2>/dev/null || echo 0)
  ok "database '$SCHEMA' already exists with $N transactions — leaving its data alone"
else
  mysql_root -e "CREATE DATABASE \`$SCHEMA\` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
  ok "created database '$SCHEMA'"
fi

# Which client hosts the app will appear to come from. A connection from your
# Mac into a Docker container arrives from the container network's gateway, not
# from 127.0.0.1, so the container needs '%' — harmless there, because the
# container publishes only on 127.0.0.1. A Homebrew server listens on every
# interface, so it deliberately does not get '%'.
GRANT_HOSTS=(localhost 127.0.0.1 ::1)
[ "$DOCKER_MYSQL" = "1" ] && GRANT_HOSTS+=('%')

grant_to() {  # grant_to <schema>
  local sch="$1" h
  for h in "${GRANT_HOSTS[@]}"; do
    mysql_root -e "CREATE USER IF NOT EXISTS '$DBUSER'@'$h' IDENTIFIED BY '$DBPASS';
                   GRANT ALL PRIVILEGES ON \`$sch\`.* TO '$DBUSER'@'$h';"
    # CREATE USER IF NOT EXISTS silently keeps an existing password. If we just
    # generated a fresh one, the old user would be left unopenable, so set it.
    if [ "$NEW_CREDS" = "1" ]; then
      mysql_root -e "ALTER USER '$DBUSER'@'$h' IDENTIFIED BY '$DBPASS';"
    fi
  done
}
grant_to "$SCHEMA"
ok "database user '$DBUSER' can reach '$SCHEMA' and nothing else"

for f in db/claudia_usaa_import.sql db/personal_finance_gui_views.sql \
         db/receipts_schema.sql db/receipts_views.sql; do
  mysql_root "$SCHEMA" < "$f"
done
V=$(mysql_root -N -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='$SCHEMA' AND table_type='VIEW'")
ok "tables and $V views are in place"

# ------------------------------------------------------------------ python --
step "5. Python packages"
# -x rather than -d: a .venv left behind by a Python that has since been
# upgraded or uninstalled is a directory with nothing runnable in it.
[ -x .venv/bin/python ] || { rm -rf .venv; python3 -m venv .venv; }
./.venv/bin/pip install --quiet --upgrade pip >/dev/null 2>&1 || true
./.venv/bin/pip install --quiet -r requirements.txt
ok "installed into ./.venv (nothing touches your system python)"

# -------------------------------------------------------------------- demo --
step "6. Sample data"
if [ "${FLIGHTDECK_NO_DEMO:-}" = "1" ]; then
  ok "skipped"
else
  DEMO_SCHEMA="${SCHEMA}_demo"
  DEMO_EXISTS=$(mysql_root -N -e "SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name='$DEMO_SCHEMA'")
  if [ "$DEMO_EXISTS" = "1" ]; then
    ok "sample database already there — leaving it alone"
  else
    mysql_root -e "CREATE DATABASE \`$DEMO_SCHEMA\` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
    for f in db/claudia_usaa_import.sql db/personal_finance_gui_views.sql \
             db/receipts_schema.sql db/receipts_views.sql; do
      mysql_root "$DEMO_SCHEMA" < "$f"
    done
    # demo_data.sql carries its own `USE`; point it at whatever the demo schema
    # is called here, so FLIGHTDECK_SCHEMA does not load the sample into the
    # wrong database.
    sed "s/^USE \`.*\`;/USE \`$DEMO_SCHEMA\`;/" demo/demo_data.sql | mysql_root "$DEMO_SCHEMA"
    N=$(mysql_root -N "$DEMO_SCHEMA" -e "SELECT COUNT(*) FROM claudia_usaa_import")
    ok "sample loaded: $N invented transactions in '$DEMO_SCHEMA'"
  fi
  # Outside the branch: a demo database that already existed still needs grants,
  # and re-granting one that has them is a no-op.
  grant_to "$DEMO_SCHEMA"
fi

if [ "$DOCKER_MYSQL" = "1" ]; then
  say ""
  warn "One known limit of the Docker route: the Receipts tab opens its own"
  warn "connection on ::1, which a container published on 127.0.0.1 does not"
  warn "answer. Every other tab works. For receipts, use a Homebrew MySQL."
fi

cat <<DONE

${GRN}${BOLD}Done.${OFF}    (database '$SCHEMA' on $DBHOST:$DBPORT)

  Start it:        ${BOLD}./run.sh${OFF}            then open http://127.0.0.1:$PORT
  Start the demo:  ${BOLD}./run.sh --demo${OFF}     then open http://127.0.0.1:8740

The first time you open it you will be asked how to get your money in: type it
in, upload bank CSV exports, connect a bank through Plaid, or look at the sample.

DONE
