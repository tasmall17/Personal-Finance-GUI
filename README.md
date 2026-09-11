# Flightdeck

A budgeting app that reads your own bank data and keeps it on your machine.

Most budgeting apps ask you to guess what you spend. This one reads what you
actually spent, from your bank exports or a bank connection, and shows you where
the guess and the reality disagree. Nothing is uploaded anywhere. There is no
account to create and no server but the one on your laptop.

---

## Install

**macOS, the short way.** Double-click **`setup.command`** in Finder. It checks
what you already have, installs what you do not, sets up the database, and tells
you what it is doing at each step. It is safe to run again; it never deletes data.

**Or from a terminal:**

```bash
git clone <your-repo-url> flightdeck
cd flightdeck
./setup.sh
```

**Or with npm**, if that is more familiar:

```bash
npm run setup
npm start
```

The npm scripts are wrappers around the shell scripts. This is a Python and
MySQL app with a vanilla JavaScript front end; there are no JavaScript
dependencies and `npm install` installs nothing.

### What the installer needs

| | Why | If it is missing |
|---|---|---|
| Homebrew | installs everything else | the installer prints the one-line command and stops |
| Python 3.9+ | the server and the loaders | installed for you |
| MySQL 8+ | where your money lives | installed and started for you |
| poppler | reading PDF receipts | installed for you |

Everything Python goes into `./.venv`, so your system Python is untouched.

**Prefer Docker?**

```bash
docker compose up -d --wait   # --wait: returns only once MySQL is ready
./setup.sh                    # finds the container by name and uses it
```

Your data lives in a named volume, so `docker compose down` keeps it. Only
`down -v` deletes it.

Two things to know before choosing this route:

- **Do not run both.** A Homebrew MySQL and the container both want port 3306.
  If both are up, `./setup.sh` stops and asks which one you meant rather than
  set up one and leave the app reading the other.
- **Receipts need Homebrew.** The Receipts tab opens its connection on `::1`,
  which a container published on `127.0.0.1` does not answer. Every other tab
  works against the container.

---

## Running it

```bash
./run.sh              # your data, http://127.0.0.1:8730
./run.sh --demo       # the sample, http://127.0.0.1:8740
./run.sh --port=8899  # somewhere else
./run.sh --help       # all of the above
```

The demo runs on its own port against its own database, so you can leave it
running beside your real one and click through everything without touching
anything of yours.

### The npm scripts

Wrappers, nothing more. Each one is the shell or Python command beside it.

| | |
|---|---|
| `npm run setup` | `./setup.sh` |
| `npm start` | `./run.sh` |
| `npm run demo` | `./run.sh --demo` |
| `npm run link` | connect a bank through Plaid from the terminal |
| `npm run sync` | pull new transactions from a connected bank |
| `npm run receipt` | read a receipt file from the terminal instead of the Receipts tab |
| `npm run audit` | check the figures on screen against the raw transactions |
| `npm run rebuild-demo` | regenerate `demo/demo_data.sql` |
| `npm run docker:up` / `docker:down` | start and stop the containerised MySQL |

### Settings you can override

Set these in front of the command, for example
`FLIGHTDECK_PORT=9000 ./run.sh`.

| | |
|---|---|
| `FLIGHTDECK_PORT` | which port to serve on. Default 8730, or 8740 for `--demo`. |
| `FLIGHTDECK_SCHEMA` | which MySQL database to read. Default `flightdeck`. |
| `FLIGHTDECK_MYSQL_HOST` | which MySQL server setup should use, when you have more than one. `::1` is the Homebrew one, `127.0.0.1` the container. |
| `FLIGHTDECK_NO_DEMO=1` | skip the sample database during setup. |

---

## First run: four ways to get your money in

The first time you open it, there is no profile and it asks how you want to start.

**Type it in.** Your income, then a sheet of 22 common expenses — rent,
insurance, fuel, phone, internet, subscriptions — to jog your memory. Fill in
what applies, add your own rows, leave the rest blank. The yearly column fills
itself as you type, because a $12 subscription is $144 you did not decide to
spend. Ten minutes, no bank connection, works offline.

**Upload bank exports.** CSV files downloaded from your bank, one per account.
Most banks put this under statements or account activity. However far back the
files go is how far back your history goes, so there is nothing to choose.

**Connect a bank with Plaid.** Pulls transactions automatically. Needs a free
Plaid account and one setting in their dashboard, described below. You are asked
how far back to go **before** connecting, because Plaid honours that only when
the connection is first made and it cannot be raised afterwards without
disconnecting and starting over.

**Try the sample.** A fictional person in Huntington Beach on $120,000, with
eighteen months of invented transactions. It copies the sample rather than using
it directly, so you can edit your copy freely.

Whichever you choose, the result is one file in `profiles/`. That file is the
app. Move it to another machine and your setup moves with it. `profiles/` is
git-ignored apart from the sample, so your finances never reach a repository.

---

## The tabs

| Tab | What it is for |
|---|---|
| **Overview** | The four numbers that matter: what comes in, what goes out, what is left, and what is safe to spend today. |
| **Cash flow** | Where each paycheck goes, and whether the accounts you pay bills from are actually funded. |
| **Bills** | Everything that recurs, when it is due, and what it costs a year. |
| **Spending** | Categories, biggest payees, and what changed since last month. |
| **Transactions** | Every row, searchable. The raw truth behind every other tab. |
| **Overhaul** | Your budget beside what your transactions actually show, line by line, and how your accounts get funded, read from your own transfers rather than assumed. |
| **Debt** | Balances, rates, minimums, and what paying extra actually buys you. |
| **Goals** | What you are saving for and whether the date is realistic. |
| **Simulate** | Change one thing, save more, spend less, overpay a card, and see the effect. |
| **Sheet** | A spreadsheet for anything the other tabs do not cover. |
| **Receipts** | Upload a photo or PDF of a receipt and see what was in the bag, not just the total. |
| **Inbox** | Merchants the classifier could not place. It only asks about what it genuinely does not know. |
| **Settings** | Connections, sync, and what is in the database. |

---

## Receipts

A bank charge says `COSTCO #1067 — $737.92`. A receipt says which eleven things
that was.

Drop a PDF or a photo on the Receipts tab. Text-layer PDFs are read exactly;
photos and scans go through Apple's Vision framework, which ships with macOS, so
no image leaves your machine and nothing extra is installed. HEIC straight from
an iPhone works.

What it does with them:

- Expands abbreviations. `KS BACON 4CT` becomes `Kirkland Signature BACON 4CT`.
- Decodes tax flags. `A` is taxable, `E` is usually food.
- Reads price endings. `.97` is a manager markdown, `.00` is last-chance
  clearance, `.99` is full price.
- Checks every line against the printed subtotal, so you know when a scan went
  wrong instead of trusting a bad number.
- Matches the receipt to the bank charge that paid for it.
- Tracks what you have paid for the same item over time.

**On whether you could have got it cheaper elsewhere:** it does not know, and
does not pretend to. There is no reliable free source of competitor grocery
prices. What it does know exactly is what *you* have paid for the same item
across visits, and it will tell you when this trip cost more than your own best
price.

Costco publishes no official legend for any of this. The abbreviations and price
endings are widely reported shopper conventions, they vary by state, and every
one carries a confidence grade in the database so a verdict never looks more
certain than it is. Both live in editable tables, `receipt_abbrev` and
`price_signal`, so you can correct them without touching code.

Originals are kept in `data/receipts/` so a parsing fix can be re-run against the
source. That folder is git-ignored.

---

## Connecting a bank with Plaid

Plaid connects apps to banks without the app ever seeing your banking password.
The free tier covers ten connections, which is more than enough.

1. Sign up at [dashboard.plaid.com](https://dashboard.plaid.com). Most accounts
   are approved automatically for real bank data.
2. Copy your **Client ID** and **Production secret** from the Keys panel.
3. Put them in a file, using an editor rather than the shell so the secret never
   lands in your shell history:

   ```bash
   mkdir -p ~/.config/flightdeck && chmod 700 ~/.config/flightdeck
   install -m 600 /dev/null ~/.config/flightdeck/plaid.env
   nano ~/.config/flightdeck/plaid.env
   ```

   ```
   PLAID_CLIENT_ID=
   PLAID_SECRET_SANDBOX=
   PLAID_SECRET_PRODUCTION=
   PLAID_ENV=production
   ```

4. **Register the redirect URI.** In the Plaid dashboard go to
   **Developers, then API**, and add exactly this under *Allowed redirect URIs*:

   ```
   https://localhost:8735/
   ```

   Most US banks use OAuth, which means Plaid sends your browser to your bank and
   back again. Without this registered, the connection is refused. The trailing
   slash matters and query parameters are not allowed.

5. Choose Plaid on the setup screen, pick how far back to go, and sign in to your
   bank in Plaid's own window. Your browser will warn about the certificate on
   `localhost`. That is expected: the certificate is generated on your machine
   during setup. Click through it.

Afterwards, `npm run sync` pulls anything new.

### How far back you can actually go

Plaid's maximum is **730 days**, and many banks offer less. This is not a setting
anyone can raise. If you need older history, your bank's own CSV export or PDF
statement archive is the only route. Statements typically go back seven years
where transaction downloads go back one or two.

---

## Uploading bank CSV exports

The setup screen does this for you. From a terminal it is four steps:

```bash
./.venv/bin/python db/load_csv.py load ~/Downloads/exports/ --dry-run
./.venv/bin/python db/load_csv.py load ~/Downloads/exports/
./.venv/bin/python db/load_csv.py promote
./.venv/bin/python db/load_csv.py classify
```

Name each file with its account's last four digits, such as
`checking_4417_2025.csv`, and the loader assigns the account itself. Files are
hashed, so running it twice over the same folder is safe and overlapping date
ranges are fine.

---

## Where your data lives

| | |
|---|---|
| `profiles/<you>.json` | your income, bills, goals and debts. Git-ignored. |
| MySQL `flightdeck` | every transaction, receipt and rollup |
| `~/.config/flightdeck/*.env` | database and Plaid credentials, mode 600, outside the repo |
| `data/receipts/` | the receipt files you uploaded. Git-ignored. |

`index.html` contains no data about anybody. The server injects your profile when
it serves the page, so the file on disk stays generic and the repository never
carries anyone's finances.

The app is local-only. The server refuses connections that are not from your own
machine, and refuses writes from any other website open in your browser.

---

## The sample dataset

Jordan Reyes is invented. The numbers around Jordan are not. Take-home pay is
from published 2026 figures for a single filer on $120,000 in California; rent,
electricity and fuel are Huntington Beach and Southern California Edison
averages. It behaves like a real budget, which is the point: Jordan planned to
have $1,845 left over each month and actually ran short.

Rebuild or change it with `npm run rebuild-demo`.

Sources: [California take-home](https://www.taxsaveiq.com/take-home-pay/california/120000) ·
[Huntington Beach rent](https://www.rentcafe.com/average-rent-market-trends/us/ca/huntington-beach/) ·
[SCE electricity](https://energyfactbook.com/utilities/southern-california-edison-co-ca/) ·
[Costco receipt codes](https://pricematcher.app/resources/how-to-read-costco-receipt)

---

## When something goes wrong

**Something is already listening on port 8730.** An older copy is still running.
Run `pkill -f serve.py`, or use `./run.sh --port=8731`.

**The page loads but every number is empty.** No transactions are loaded yet.
Open Settings to see what the database actually holds.

**It says it could not reach the receipts database.** The running server is older
than the files on disk. Stop it and start it again.

**Plaid says the redirect URI is not configured.** Step 4 above. It must match
exactly, including the trailing slash.

**MySQL will not start.** Run `brew services restart mysql`, then
`brew services info mysql` to see why it stopped.

**Setup said it worked, but every screen is empty.** You probably have two MySQL
servers. macOS lets a Homebrew MySQL and a Docker container both sit on port
3306: the Homebrew one answers on `::1` and the unix socket, the container only
on `127.0.0.1`. They hold different data, so setup can load every table into one
while the app reads the other. Check with:

```bash
lsof -nP -iTCP:3306 -sTCP:LISTEN
```

Two lines means two servers. Stop one (`docker compose down`, or
`brew services stop mysql`) and run `./setup.sh` again, or name the one you want
with `FLIGHTDECK_MYSQL_HOST=::1 ./setup.sh`.

**The Receipts tab errors but the other tabs are fine.** The receipts connection
is pinned to `::1`, so it only reaches a Homebrew MySQL, never a container
published on `127.0.0.1`.

---

## License

MIT. Do what you like with it.
