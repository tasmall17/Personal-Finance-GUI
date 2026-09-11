"""Turning a bank description into a merchant name and a category.

Both the proposer and the classifier import from here, so the text a pattern was
built from is normalised exactly the same way as the text it is matched against.
That symmetry is the whole point: a pattern built from "TST*CORNER DINER 717-555-0142"
has to still match that same string later.
"""
import re

# payment processors and terminal noise that sit in front of the real merchant
PREFIX = r"(sq|tst|sp|signup|pos|ach|paypal|pp|dd|ext|chk|deb|web|int)\s*\*+\s*"
NOISE = [
    (r"\b\d{3}[- ]?\d{3}[- ]?\d{4}\b", " "),          # phone numbers
    (r"\*+\d+", " "), (r"\bx{2,}\d+\b", " "),          # card and account masks
    (r"#\s*\d+", " "), (r"\b\d{4,}\b", " "),           # store and reference numbers
    (r"\b(recurring|debit card|purchase|payment|withdrawal|deposit|card pur)\b", " "),
    (r"\b(llc|inc|corp|co)\b", " "),
    (r"\.(com|net|org|io|co)\b", " "),                 # keep the brand, drop the tld
    (r"\b(pa|md|va|de|nj|ny|oh|wv|ca|wa|tx|il|nc|sc|ga|fl|az|wi|gb|us)\b\s*$", " "),
    (r"[^a-z0-9&'+ -]", " "),
]
# A city name at the tail of a bank description is location, not identity, and
# is worth stripping the same way phone numbers and store numbers are above.
# Empty by default: which cities show up is specific to where you actually
# shop, so there is nothing generic to seed here. Add your own as you notice
# them in the Inbox - "STARBUCKS #4021 SPRINGFIELD IL" and "STARBUCKS #4021"
# should resolve to the same merchant, and this is what makes that happen.
# (?!) never matches anything - a real placeholder, not a bare "" that would
# match (and insert a space) at every single position in the string.
CITIES = r"(?!)"

def normalise(s):
    s = (s or "").lower()
    s = re.sub(PREFIX, " ", s)
    for pat, rep in NOISE:
        s = re.sub(pat, rep, s)
    s = re.sub(CITIES, " ", s)
    return re.sub(r"\s+", " ", s).strip(" -")

# a normalised string containing the key becomes the merchant on the right
#
# This starter set is deliberately generic - national chains and brands
# common enough that almost any US bank feed will hit a few of them - rather
# than tuned to one person's actual spending. It exists to take the edge off
# a brand-new install, not to be a complete answer: whatever it misses lands
# in the Inbox for you to confirm once, same as everything else. Add your own
# local grocery store, landlord, gym, or insurer here the same way, or just
# answer them in the Inbox - either works, this file is only a head start.
CANON = [
    # groceries
    ("walmart", "Walmart"), ("target", "Target"), ("costco", "Costco"),
    ("kroger", "Kroger"), ("safeway", "Safeway"), ("whole foods", "Whole Foods"),
    ("trader joe", "Trader Joe's"), ("aldi", "Aldi"), ("publix", "Publix"),
    # dining
    ("starbucks", "Starbucks"), ("mcdonald", "McDonald's"), ("chipotle", "Chipotle"),
    ("taco bell", "Taco Bell"), ("subway", "Subway"), ("dunkin", "Dunkin'"),
    ("uber eats", "Uber Eats"), ("doordash", "DoorDash"), ("grubhub", "Grubhub"),
    # shopping
    ("amazon", "Amazon"), ("amzn", "Amazon"), ("ebay", "eBay"), ("etsy", "Etsy"),
    ("ross dress", "Ross"), ("tj maxx", "T.J. Maxx"), ("home depot", "Home Depot"),
    ("lowes", "Lowe's"), ("best buy", "Best Buy"),
    # fuel and transport
    ("shell oil", "Shell"), ("exxon", "Exxon"), ("chevron", "Chevron"),
    ("phillips 66", "Phillips 66"), ("uber", "Uber"), ("lyft", "Lyft"),
    # subscriptions
    ("netflix", "Netflix"), ("spotify", "Spotify"), ("hulu", "Hulu"),
    ("disney plus", "Disney+"), ("apple", "Apple"), ("adobe", "Adobe"),
    ("amazon prime", "Amazon Prime"), ("youtube premium", "YouTube Premium"),
    # health and fitness
    ("cvs", "CVS Pharmacy"), ("walgreens", "Walgreens"), ("rite aid", "Rite Aid"),
    ("planet fitness", "Planet Fitness"), ("ymca", "YMCA"),
    # phone
    ("at&t", "AT&T"), ("verizon", "Verizon"), ("t-mobile", "T-Mobile"),
    # cards and fees
    ("american express", "American Express"), ("discover", "Discover"),
    ("capital one", "Capital One"), ("chase credit", "Chase Card"),
    ("overdraft fee", "Overdraft Fee"), ("interest paid", "Interest Paid"),
    ("interest on purchases", "Card Interest"), ("dispute", "Card Dispute"),
    ("initial balance", "Opening Balance"),
    # transfers and income
    ("cash app", "Cash App"), ("venmo", "Venmo"), ("zelle", "Zelle"),
    ("payroll", "Payroll"), ("irs treas", "IRS Refund"),
]
CATEGORY = [
    (r"walmart|target|amazon|amzn|ebay|etsy|ross dress|tj maxx|home depot|lowes|best buy", "shopping"),
    (r"starbucks|mcdonald|chipotle|taco bell|subway|dunkin|uber eats|doordash|grubhub", "dining"),
    (r"kroger|safeway|whole foods|trader joe|aldi|publix|costco", "groceries"),
    (r"shell oil|exxon|chevron|phillips 66", "fuel"),
    (r"^uber$|lyft", "transport"),
    (r"netflix|spotify|hulu|disney plus|apple|adobe|amazon prime|youtube premium", "subscription"),
    (r"cvs|walgreens|rite aid|planet fitness|ymca", "health"),
    (r"at&t|verizon|t-mobile", "phone"),
    (r"american express|discover|capital one|chase credit", "creditcard"),
    (r"overdraft fee|interest paid|card interest|card dispute", "fees"),
    (r"cash app|venmo|zelle|initial balance", "transfer"),
    (r"payroll|irs treas", "income"),
]

def canonical(desc, payee):
    """Return (display name, pattern key, how we got there).

    'rule'  — it matched a merchant named explicitly in CANON. That is knowledge,
              not a guess, and nobody should have to confirm it.
    'guess' — nothing matched, so the name is just the first few words tidied up.
              These are the only ones worth a human's attention.
    """
    norm = normalise(desc) or normalise(payee)
    hay = (norm + " " + normalise(payee)).strip()
    for key, name in CANON:
        if key in hay:
            return name, key, "rule"
    if not norm:
        return None, None, None
    words = [w for w in norm.split() if len(w) > 1][:4]
    return " ".join(w.capitalize() for w in words), " ".join(words), "guess"

def category_for(name):
    for pat, key in CATEGORY:
        if re.search(pat, name, re.I):
            return key
    return None

def merge_key(norm_key):
    """Two keys sharing their first three words are the same shop in two spellings."""
    return " ".join(norm_key.split()[:3])
