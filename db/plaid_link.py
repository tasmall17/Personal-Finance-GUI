#!/usr/bin/env python3
"""Link a bank or card through Plaid and save the access token.

    plaid_link.py [--env sandbox|production] [--port 8735] [--name "American Express"]

--name labels the item being linked (shown in Settings and in npm run sync's
output); without it, Plaid's own name for the institution is used.

Every account you link this way is its own Plaid "item" with its own access
token - one for USAA, a separate one for Amex, another for Discover. They are
stored as PLAID_ITEM_<n>_NAME / _ACCESS_TOKEN / _ID / _CURSOR, numbered in the
order you linked them, so linking a second or third card never disturbs the
first. A pre-existing single-item install (PLAID_ACCESS_TOKEN etc., from
before multiple cards were supported) is migrated into slot 1 automatically
the first time this runs, and the old fields are left in place rather than
deleted, in case anything else still reads them.

USAA is an OAuth institution, so Plaid sends the browser to USAA and back to a
redirect URI that must be https and must be registered in the Plaid dashboard.
This serves that URI locally with a self-signed certificate.

Every access token is written to ~/.config/flightdeck/plaid.env at mode 600
before anything else happens, and is never printed or returned to the browser.
"""
import http.server, json, os, re, ssl, subprocess, sys, threading, urllib.request, urllib.error, webbrowser

ENV_FILE = os.path.expanduser("~/.config/flightdeck/plaid.env")
CERT_DIR = os.path.expanduser("~/.config/flightdeck")
CERT, KEY = os.path.join(CERT_DIR, "localhost.crt"), os.path.join(CERT_DIR, "localhost.key")
HOSTS = {"sandbox": "https://sandbox.plaid.com", "production": "https://production.plaid.com"}

# 730 days is Plaid's maximum. It can only be set when transactions is first
# added to an item; raising it later means removing the item and linking again.
# Plaid's maximum is 730 days. This is set once, when transactions are first
# added to an item; raising it later means removing the item and linking again.
# The setup screen asks the user up front for exactly that reason.
DAYS_REQUESTED = min(730, int(os.environ.get("FLIGHTDECK_PLAID_MONTHS", "24")) * 30.4) 

def load_env():
    env = {}
    if not os.path.exists(ENV_FILE):
        sys.exit("no %s — create it with your Plaid keys first" % ENV_FILE)
    for line in open(ENV_FILE):
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.strip().split("=", 1)
            env[k] = v
    return env

def save_token(name, value):
    """Disk first, mode 600, before the token is used for anything."""
    lines, seen = [], False
    for line in open(ENV_FILE):
        if line.startswith(name + "="):
            lines.append("%s=%s\n" % (name, value)); seen = True
        else:
            lines.append(line)
    if not seen:
        lines.append("%s=%s\n" % (name, value))
    fd = os.open(ENV_FILE + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.writelines(lines)
    os.replace(ENV_FILE + ".tmp", ENV_FILE)
    os.chmod(ENV_FILE, 0o600)

def api(host, path, body):
    req = urllib.request.Request(host + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:400]
        sys.exit("Plaid answered HTTP %d for %s\n%s" % (e.code, path, detail))

def soft_api(host, path, body):
    """Same as api(), but a failure returns None instead of exiting - for
    best-effort lookups (an institution's display name) that should never be
    allowed to abort a link that otherwise succeeded."""
    try:
        return api(host, path, body)
    except SystemExit:
        return None

def existing_items(env):
    """Every item already linked, in the order it was linked: list of dicts
    with slot/name/access_token/item_id/cursor. A legacy single-item install
    (no PLAID_ITEM_n_ fields yet, just the old flat PLAID_ACCESS_TOKEN) is
    reported as slot 1 without being written anywhere - see migrate_legacy."""
    items = []
    slots = sorted(set(int(m.group(1)) for k in env
                        for m in [re.match(r"PLAID_ITEM_(\d+)_ACCESS_TOKEN$", k)] if m))
    for n in slots:
        tok = env.get("PLAID_ITEM_%d_ACCESS_TOKEN" % n)
        if not tok:
            continue
        items.append({"slot": n, "name": env.get("PLAID_ITEM_%d_NAME" % n) or ("Item %d" % n),
                      "access_token": tok, "item_id": env.get("PLAID_ITEM_%d_ID" % n),
                      "cursor": env.get("PLAID_ITEM_%d_CURSOR" % n) or None})
    if not items and env.get("PLAID_ACCESS_TOKEN"):
        items.append({"slot": 1, "name": env.get("PLAID_ITEM_NAME") or "Item 1",
                      "access_token": env["PLAID_ACCESS_TOKEN"],
                      "item_id": env.get("PLAID_ITEM_ID"), "cursor": env.get("PLAID_CURSOR") or None})
    return items

def migrate_legacy(env, host, secret):
    """Write the legacy single-item fields into slot 1 for real, once, the
    first time a second item is about to be linked - so the numbering scheme
    has a real slot 1 on disk rather than an implied one, and a genuinely new
    slot 2 never collides with it."""
    # Checked on the access token specifically, not any PLAID_ITEM_ key -
    # plaid_sync.py writes PLAID_ITEM_1_CURSOR through the legacy fallback
    # path before this ever runs, and that alone must not look like slot 1
    # already exists for real.
    if env.get("PLAID_ITEM_1_ACCESS_TOKEN") or not env.get("PLAID_ACCESS_TOKEN"):
        return
    tok = env["PLAID_ACCESS_TOKEN"]
    name = institution_name(host, env["PLAID_CLIENT_ID"], secret, tok) or "Item 1"
    save_token("PLAID_ITEM_1_NAME", name)
    save_token("PLAID_ITEM_1_ACCESS_TOKEN", tok)
    save_token("PLAID_ITEM_1_ID", env.get("PLAID_ITEM_ID") or "")
    save_token("PLAID_ITEM_1_CURSOR", env.get("PLAID_CURSOR") or "")
    print("migrated the existing link (%s) into slot 1" % name)

def institution_name(host, client_id, secret, access_token):
    item = soft_api(host, "/item/get", {"client_id": client_id, "secret": secret,
                                        "access_token": access_token})
    inst_id = (item or {}).get("item", {}).get("institution_id")
    if not inst_id:
        return None
    inst = soft_api(host, "/institutions/get_by_id", {"client_id": client_id, "secret": secret,
                    "institution_id": inst_id, "country_codes": ["US"]})
    return (inst or {}).get("institution", {}).get("name")

def ensure_cert():
    if os.path.exists(CERT) and os.path.exists(KEY):
        return
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", KEY, "-out", CERT, "-days", "825",
                    "-subj", "/CN=localhost",
                    "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
                   check=True, capture_output=True)
    os.chmod(KEY, 0o600)
    print("made a self-signed certificate for localhost")

PAGE = """<!doctype html><meta charset=utf-8><title>Link a bank</title>
<style>body{font:15px system-ui;margin:60px auto;max-width:640px;padding:0 20px}
code{background:#eee;padding:2px 5px;border-radius:3px}</style>
<h2>Flightdeck &rarr; Plaid</h2><p id=s>Opening Plaid Link&hellip;</p>
<script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
<script>
const TOKEN = "%TOKEN%", S = document.getElementById("s");
const cfg = {
  token: TOKEN,
  onSuccess: (public_token) => {
    S.textContent = "Linked. Saving the access token\\u2026";
    fetch("/exchange", {method:"POST", headers:{"Content-Type":"application/json"},
                        body: JSON.stringify({public_token})})
      .then(r => r.json())
      .then(d => { S.textContent = d.ok
          ? "Saved as \\"" + d.name + "\\". " + d.accounts + " accounts linked. You can close this tab."
          : "Failed: " + d.error; });
  },
  onExit: (err) => { S.textContent = err ? ("Exited: " + (err.display_message || err.error_code)) : "Cancelled."; }
};
if (window.location.search.includes("oauth_state_id")) {
  cfg.receivedRedirectUri = window.location.href;   // coming back from the bank
}
Plaid.create(cfg).open();
</script>"""

def main():
    env = load_env()
    which = env.get("PLAID_ENV", "sandbox")
    if "--env" in sys.argv:
        which = sys.argv[sys.argv.index("--env") + 1]
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8735
    override_name = sys.argv[sys.argv.index("--name") + 1] if "--name" in sys.argv else None
    host = HOSTS[which]
    secret = env["PLAID_SECRET_PRODUCTION" if which == "production" else "PLAID_SECRET_SANDBOX"]
    redirect = "https://localhost:%d/" % port

    migrate_legacy(env, host, secret)
    env = load_env()                            # re-read in case migrate_legacy just wrote slot 1
    next_slot = max([i["slot"] for i in existing_items(env)] + [0]) + 1

    ensure_cert()
    tok = api(host, "/link/token/create", {
        "client_id": env["PLAID_CLIENT_ID"], "secret": secret,
        "client_name": "Flightdeck", "language": "en", "country_codes": ["US"],
        "user": {"client_user_id": "flightdeck-local"},
        "products": ["transactions"],
        "transactions": {"days_requested": int(DAYS_REQUESTED)},
        "redirect_uri": redirect,
    })
    link_token = tok["link_token"]
    print("link token created for %s, asking for %d days of history" % (which, int(DAYS_REQUESTED)))

    done = threading.Event()

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            html = PAGE.replace("%TOKEN%", link_token).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            pub = json.loads(self.rfile.read(n) or b"{}").get("public_token")
            out = {"ok": False, "error": "no public_token"}
            if pub:
                r = api(host, "/item/public_token/exchange", {
                    "client_id": env["PLAID_CLIENT_ID"], "secret": secret, "public_token": pub})
                name = (override_name or institution_name(host, env["PLAID_CLIENT_ID"], secret, r["access_token"])
                        or ("Item %d" % next_slot))
                # a sync cursor belongs to one item; a new item starts fresh -
                # saved under its own numbered slot, so linking this never
                # touches any card or bank already linked in another slot.
                save_token("PLAID_ITEM_%d_NAME" % next_slot, name)
                save_token("PLAID_ITEM_%d_ACCESS_TOKEN" % next_slot, r["access_token"])
                save_token("PLAID_ITEM_%d_ID" % next_slot, r["item_id"])
                save_token("PLAID_ITEM_%d_CURSOR" % next_slot, "")
                acc = api(host, "/accounts/get", {
                    "client_id": env["PLAID_CLIENT_ID"], "secret": secret,
                    "access_token": r["access_token"]})
                out = {"ok": True, "accounts": len(acc.get("accounts", [])), "name": name}
                print("saved as item %d (%s), %d accounts:" % (next_slot, name, len(acc.get("accounts", []))))
                for a in acc.get("accounts", []):
                    print("  %-34s %-10s mask %s" % (a.get("name", "")[:34],
                          a.get("subtype", ""), a.get("mask")))
            body = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            if out["ok"]:
                threading.Timer(1.0, done.set).start()

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(CERT, KEY)
    srv = http.server.HTTPServer(("127.0.0.1", port), H)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    print("\nopen this, and accept the certificate warning:\n\n    %s\n" % redirect)
    try:
        webbrowser.open(redirect)
    except Exception:
        pass
    print("waiting for the link to finish (ctrl-c to give up)...")
    try:
        done.wait()
    except KeyboardInterrupt:
        print("\ngave up")
    srv.shutdown()

if __name__ == "__main__":
    main()
