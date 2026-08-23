"""
Savings rate collector.

Pulls best-buy rates from HL, Meteor, Raisin and Flagstone, normalises them
into fixed term buckets, and writes a top 10 per bucket to a Google Sheet.

Run locally:      python rates.py --local
Run in Actions:   python rates.py
"""

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, asdict

import requests
from bs4 import BeautifulSoup

# Amount held constant so week-on-week comparisons are like for like.
AMOUNT = 50000

# Anna's report order. Keys are internal, values are the labels in the output.
BUCKETS = [
    ("easy_access", "Easy access"),
    ("6m", "6m"),
    ("9m", "9m"),
    ("12m", "12m"),
    ("24m", "24m"),
    ("36m", "36m"),
    ("60m", "60m"),
    ("isa_easy_access", "Easy access ISA"),
    ("isa_12m", "12m ISA"),
    ("isa_24m", "24m ISA"),
]

HEALTH: dict[str, object] = {}

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


@dataclass
class Product:
    source: str
    bank: str
    rate: float
    term_raw: str
    bucket: str
    isa: bool


# ---------------------------------------------------------------------------
# Term normalisation
# ---------------------------------------------------------------------------

def normalise_term(raw: str, isa: bool) -> str | None:
    """Map a source's term label onto one of Anna's buckets. None means drop it."""
    t = raw.lower().strip()

    if any(k in t for k in ("easy access", "instant access", "easy-access")):
        return "isa_easy_access" if isa else "easy_access"

    # Notice accounts are neither easy access nor fixed. Drop them.
    if "notice" in t:
        return None

    months = None
    m = re.search(r"(\d+(?:\.\d+)?)\s*(month|year|yr|m\b|y\b)", t)
    if m:
        n = float(m.group(1))
        unit = m.group(2)
        months = n * 12 if unit.startswith(("year", "yr", "y")) else n

    if months is None:
        return None

    months = int(round(months))
    if isa:
        return {12: "isa_12m", 24: "isa_24m"}.get(months)
    return {6: "6m", 9: "9m", 12: "12m", 24: "24m", 36: "36m", 60: "60m"}.get(months)


def parse_rate(txt: str) -> float | None:
    m = re.search(r"(\d+\.\d+)\s*%", txt)
    return float(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Source: Hargreaves Lansdown  (needs a browser: Next.js hydrates the tables)
# ---------------------------------------------------------------------------

HL_GENERAL = "https://www.hl.co.uk/savings/latest-savings-rates-and-products"
HL_ISA = "https://www.hl.co.uk/savings/latest-savings-rates-and-products?filter=cash-isa"


def parse_hl(html: str, isa: bool) -> list[Product]:
    """HL renders one <table> per term with a Bank / AER / Term column set."""
    soup = BeautifulSoup(html, "lxml")
    out = []
    for table in soup.find_all("table"):
        heads = [th.get_text(" ", strip=True).lower() for th in table.select("thead th")]
        if not heads or "bank" not in heads[0]:
            continue
        try:
            i_aer = next(i for i, h in enumerate(heads) if h == "aer")
            i_term = next(i for i, h in enumerate(heads) if h == "term")
        except StopIteration:
            continue

        for tr in table.select("tbody tr"):
            tds = tr.find_all("td")
            if len(tds) <= max(i_aer, i_term):
                continue
            bank = re.sub(r"\s+", " ", tds[0].get_text(" ", strip=True))
            bank = re.sub(r"^Market Leading Rate\s*", "", bank).strip()
            rate = parse_rate(tds[i_aer].get_text())
            term_raw = tds[i_term].get_text(" ", strip=True)
            bucket = normalise_term(term_raw, isa)
            if rate and bucket:
                out.append(Product("HL", bank, rate, term_raw, bucket, isa))
    return out


# ---------------------------------------------------------------------------
# Source: Meteor  (server rendered, plain fetch is enough)
# ---------------------------------------------------------------------------

METEOR = {
    "easy-access": "https://savings.meteoram.com/savings/easy-access?filter0=general",
    "fixed-term": "https://savings.meteoram.com/savings/fixed-term?filter0=general",
}


def _meteor_card(node):
    """Walk up from a rate node to the product card (the bit with the bank name)."""
    card = node
    for _ in range(10):
        card = card.parent
        if card is None:
            return None
        if card.find(["h2", "h3", "h4"]):
            return card
    return None


def parse_meteor(html: str) -> list[Product]:
    """
    Every Meteor card states its own Type and Term, and carries tick/cross icons
    for General and Cash ISA eligibility. Trust the card, never the page: the
    easy access page carries promoted fixed term cards, and the ?filter0= query
    param is applied client-side so a plain fetch ignores it.
    """
    soup = BeautifulSoup(html, "lxml")
    out = []

    for node in soup.select(".rateInfoValue"):
        rate = parse_rate(re.sub(r"\s+", " ", node.get_text(" ")))
        if not rate:
            continue
        card = _meteor_card(node)
        if card is None:
            continue

        bank = card.find(["h2", "h3", "h4"]).get_text(" ", strip=True)
        text = re.sub(r"\s+", " ", card.get_text(" "))

        m_type = re.search(r"Type\s+(Easy Access|Fixed Term|Notice)", text)
        if not m_type:
            continue
        kind = m_type.group(1)
        if kind == "Notice":
            continue

        if kind == "Easy Access":
            term_raw = "Easy Access"
        else:
            m_term = re.search(
                r"Term\s+(\d+(?:\.\d+)?\s*(?:month|months|year|years))", text
            )
            if not m_term:
                continue
            term_raw = m_term.group(1)

        # Eligibility. Standard cards show both labels with a tick or a cross.
        # Promoted cards show a single label and no icons at all.
        general = isa = False
        icons = card.select("[data-name]")
        if icons:
            for icon in icons:
                label = icon.parent.get_text(" ", strip=True)
                if not label and icon.parent.parent:
                    label = icon.parent.parent.get_text(" ", strip=True)
                ok = icon.get("data-name") == "yes_tick"
                if "Cash ISA" in label:
                    isa = isa or ok
                elif "General" in label:
                    general = general or ok
        else:
            isa = "Cash ISA" in text
            general = "General" in text

        for is_isa in (v for v, on in ((False, general), (True, isa)) if on):
            bucket = normalise_term(term_raw, is_isa)
            if bucket:
                out.append(Product("Meteor", bank, rate, term_raw, bucket, is_isa))

    return out


# ---------------------------------------------------------------------------
# Source: Raisin  (needs a browser; sort by term to force all rows to render)
# ---------------------------------------------------------------------------

RAISIN_EASY = "https://www.raisin.com/en-gb/savings-accounts/easy-access-savings-accounts/"
RAISIN_FIXED = "https://www.raisin.com/en-gb/savings-accounts/fixed-rate-bonds/"


def parse_raisin(html: str, page_kind: str) -> list[Product]:
    soup = BeautifulSoup(html, "lxml")
    out = []
    for cell in soup.select('[class*="styles-module_rate__"]'):
        row = cell.parent
        text = re.sub(r"\s+", " ", row.get_text(" ")).strip()
        rate = parse_rate(text)
        if not rate:
            continue

        if page_kind == "easy":
            term_raw = "Easy Access"
        else:
            m = re.search(r"AER\s+(.+?)\s+Max", text)
            term_raw = m.group(1) if m else ""
            # Raisin appends badges: "1 Year Sharia account", "6 months Easy access"
            term_raw = re.split(r"\s+(?:Sharia|Easy)\b", term_raw)[0].strip()

        bucket = normalise_term(term_raw, isa=False)
        if not bucket:
            continue

        img = row.find("img", alt=True)
        bank = img["alt"] if img else "unknown"
        out.append(Product("Raisin", bank, rate, term_raw, bucket, False))
    return out


# ---------------------------------------------------------------------------
# Source: Flagstone ISA page  (schema.org JSON in the HTML, plain fetch)
# ---------------------------------------------------------------------------

FLAGSTONE_ISA = "https://www.flagstoneim.com/personal/cash-isa"


def parse_flagstone_isa(html: str) -> list[Product]:
    out = []
    for blob in re.findall(
        r'<script type="application/ld\+json">(\{.*?)</script>', html, re.S
    ):
        try:
            d = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if d.get("@type") != "FinancialProduct":
            continue
        rate = parse_rate(d.get("annualPercentageRate", ""))
        name = d.get("name", "")
        brand = d.get("brand", name)
        term_raw = name.replace(brand, "").strip()
        bucket = normalise_term(term_raw, isa=True)
        if rate and bucket:
            out.append(Product("Flagstone", brand, rate, term_raw, bucket, True))
    return out


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def fetch_plain(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    return r.text


def fetch_rendered(url: str, prep=None) -> str:
    """Load with a real browser. prep(page) runs before the HTML is grabbed."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=UA, viewport={"width": 1400, "height": 1200})
        page.goto(url, wait_until="networkidle", timeout=60000)
        dismiss_cookies(page)
        if prep:
            prep(page)
        html = page.content()
        browser.close()
    return html


def dismiss_cookies(page):
    for sel in [
        "#onetrust-accept-btn-handler",
        "button:has-text('Accept all')",
        "button:has-text('Accept All')",
        "[data-testid='uc-accept-all-button']",
    ]:
        try:
            page.click(sel, timeout=2500)
            page.wait_for_timeout(600)
            return
        except Exception:
            continue


def raisin_prep(page):
    """Set the amount, then sort by term so every row renders."""
    try:
        box = page.locator("input[value*='50'], input[name*='amount']").first
        box.fill(str(AMOUNT))
        page.keyboard.press("Enter")
        page.wait_for_timeout(1500)
    except Exception:
        pass
    try:
        page.click("text=Term", timeout=4000)
        page.wait_for_timeout(2000)
    except Exception:
        pass
    for _ in range(12):
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(400)


def hl_prep(page):
    for _ in range(10):
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(300)


# ---------------------------------------------------------------------------
# Collection and output
# ---------------------------------------------------------------------------

def collect() -> list[Product]:
    products: list[Product] = []

    def attempt(label, fn):
        try:
            got = fn()
            print(f"  {label}: {len(got)} products", file=sys.stderr)
            HEALTH[label] = len(got)
            products.extend(got)
        except Exception as e:
            print(f"  {label}: FAILED {type(e).__name__}: {e}", file=sys.stderr)
            HEALTH[label] = f"FAILED: {type(e).__name__}"

    print("Collecting...", file=sys.stderr)
    attempt("HL general", lambda: parse_hl(fetch_rendered(HL_GENERAL, hl_prep), False))
    attempt("HL ISA", lambda: parse_hl(fetch_rendered(HL_ISA, hl_prep), True))

    for kind, url in METEOR.items():
        attempt(f"Meteor {kind}", lambda u=url: parse_meteor(fetch_plain(u)))

    attempt("Raisin easy", lambda: parse_raisin(fetch_rendered(RAISIN_EASY, raisin_prep), "easy"))
    attempt("Raisin fixed", lambda: parse_raisin(fetch_rendered(RAISIN_FIXED, raisin_prep), "fixed"))
    attempt("Flagstone ISA", lambda: parse_flagstone_isa(fetch_plain(FLAGSTONE_ISA)))

    return products


def top_ten(products: list[Product]) -> dict[str, list[Product]]:
    """
    Rank within each bucket. Duplicates across platforms are kept, as requested,
    but the exact same listing scraped twice from one platform is not a duplicate,
    it's a double count. Those are dropped.
    """
    out = {}
    for key, _ in BUCKETS:
        rows, seen = [], set()
        for p in products:
            if p.bucket != key:
                continue
            sig = (p.source, p.bank.lower().strip(), p.rate, p.term_raw.lower())
            if sig in seen:
                continue
            seen.add(sig)
            rows.append(p)
        rows.sort(key=lambda p: -p.rate)
        out[key] = rows[:10]
    return out


def render_text(ranked: dict[str, list[Product]]) -> str:
    lines = []
    for key, label in BUCKETS:
        lines.append(label)
        rows = ranked[key]
        for i in range(10):
            if i < len(rows):
                lines.append(f"{i + 1}\t{rows[i].rate:.2f}")
            else:
                lines.append(f"{i + 1}\t")
        lines.append("")
    return "\n".join(lines)


def write_sheet(ranked: dict[str, list[Product]], products: list[Product]):
    """Tab 1: the report as Anna wants it. Tab 2: full audit trail."""
    import gspread
    from datetime import date
    from google.oauth2.service_account import Credentials

    creds_json = os.environ["GOOGLE_CREDENTIALS"]
    sheet_id = os.environ["SHEET_ID"]

    creds = Credentials.from_service_account_info(
        json.loads(creds_json),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)

    today = date.today().isoformat()

    rows = [[f"Savings rates, {today}, £{AMOUNT:,} deposit"], []]
    for key, label in BUCKETS:
        rows.append([label])
        got = ranked[key]
        for i in range(10):
            rows.append([i + 1, f"{got[i].rate:.2f}" if i < len(got) else ""])
        rows.append([])

    rows.append(["Source health"])
    for label, count in HEALTH.items():
        rows.append([label, count])

    report = sh.worksheet("Report") if "Report" in [w.title for w in sh.worksheets()] \
        else sh.add_worksheet("Report", rows=200, cols=6)
    report.clear()
    report.update(values=rows, range_name="A1")

    detail_rows = [["date", "bucket", "rate", "bank", "source", "term_raw"]]
    for key, label in BUCKETS:
        for p in ranked[key]:
            detail_rows.append([today, label, p.rate, p.bank, p.source, p.term_raw])

    detail = sh.worksheet("Detail") if "Detail" in [w.title for w in sh.worksheets()] \
        else sh.add_worksheet("Detail", rows=2000, cols=8)
    detail.append_rows(detail_rows[1:], value_input_option="USER_ENTERED")

    print(f"Wrote {len(products)} products to sheet", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", action="store_true", help="print only, no sheet write")
    args = ap.parse_args()

    products = collect()
    if not products:
        sys.exit("No products collected. Something upstream changed.")

    ranked = top_ten(products)
    print(render_text(ranked))

    thin = [label for key, label in BUCKETS if len(ranked[key]) < 10]
    if thin:
        print(f"\nUnder 10 results: {', '.join(thin)}", file=sys.stderr)

    if not args.local:
        write_sheet(ranked, products)


if __name__ == "__main__":
    main()
