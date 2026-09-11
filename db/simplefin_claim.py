#!/usr/bin/env python3
"""Claim a SimpleFIN setup token from a file, without the web server involved.

    simplefin_claim.py [file]     default: ~/Downloads/flightdeck-gui-simpleFIN.txt

A setup token is single use. This is a short-lived process with no restarts to race
it, and the access URL is written to disk the instant the bridge returns it — before
anything else can fail. Nothing here prints the token or the access URL.
"""
import base64, datetime, os, sys, urllib.request, urllib.error

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

CONF = os.path.expanduser("~/.config/flightdeck")
SF_ENV = os.path.join(CONF, "simplefin.env")
DEFAULT = os.path.expanduser("~/Downloads/flightdeck-gui-simpleFIN.txt")

def write(access):
    os.makedirs(CONF, mode=0o700, exist_ok=True)
    fd = os.open(SF_ENV, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("# saved %s — treat this file like a password\n"
                 % datetime.datetime.now().isoformat(timespec="seconds"))
        fh.write("SIMPLEFIN_ACCESS_URL=%s\n" % access)
    os.chmod(SF_ENV, 0o600)

def main(path):
    if not os.path.exists(path):
        sys.exit("no token file at " + path)
    token = "".join(open(path).read().split())
    if not token:
        sys.exit("that file is empty")

    if token.startswith("http"):
        if "@" not in token:
            sys.exit("that URL carries no credentials — an access URL looks like "
                     "https://user:pass@bridge…/simplefin")
        write(token)
        print("saved the access URL you pasted (no claim needed)")
        return 0

    try:
        url = base64.b64decode(token + "=" * (-len(token) % 4)).decode().strip()
    except Exception:
        sys.exit("that is not a setup token — expected one long base64 string, got %d characters"
                 % len(token))
    if not url.startswith("https://"):
        sys.exit("the token decodes to something that is not an https claim URL")
    print("claiming against %s …" % url.split("/claim/")[0])

    # Cloudflare sits in front of the bridge and 403s Python's default user agent
    # with its own "error code: 1010". That is not the bridge and not a spent token.
    req = urllib.request.Request(url, data=b"", method="POST", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            access = res.read().decode().strip()
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()[:160]
        except Exception:
            pass
        if "1010" in body:
            sys.exit("Cloudflare blocked this request (code 1010) before it reached the bridge. "
                     "The token is untouched — this is a client problem, not a spent token.")
        if e.code in (403, 409):
            sys.exit("HTTP %d — the bridge refused it. If it says 'already claimed', the token "
                     "is spent; generate a fresh one at "
                     "https://bridge.simplefin.org/simplefin/create\n%s" % (e.code, body))
        sys.exit("the bridge said HTTP %d\n%s" % (e.code, body))
    except Exception as e:
        sys.exit("could not reach the bridge (the token may or may not be spent — check the "
                 "bridge before generating another): %s" % str(e)[:160])

    if access.startswith("http"):
        write(access)          # DISK FIRST. never print, slice or buffer a single-use secret.
    else:
        sys.exit("the bridge returned %d characters that are not a URL: %r"
                 % (len(access), access[:60]))
    print("claimed and saved to %s (mode 600)" % SF_ENV)
    print("now run:  .venv/bin/python db/simplefin_sync.py --months 24")
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT))
