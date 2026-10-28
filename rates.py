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
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup

# No minimum deposit filter. Every product is listed whatever it requires,
# with the minimum shown in the Detail tab so it can be judged per product.
#
# Raisin still needs a figure typed into its amount box, and no single figure
# reaches everything: a low amount misses products with high minimums, a high
# one misses products with low maximums. So it runs twice and the results are
# pooled, with dedupe removing the overlap.
RAISIN_AMOUNTS = [1000, 50000]
DEPOSIT_LABEL = "all deposit sizes"

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

SOURCES = ["HL", "Meteor", "Flagstone", "Raisin"]

PLATFORM_NOTES = {
    "HL": ("HL's own Active Savings range. Its site also republishes Moneyfacts "
           "market-leading rates for providers not on its platform; those are not "
           "served to this collector and are not included here."),
}

# Present as a browser in the UK. HL serves this collector a reduced page,
# and locale is the cheapest of the possible reasons to rule out.
UK_CONTEXT = {
    "user_agent": None,
    "locale": "en-GB",
    "timezone_id": "Europe/London",
    "geolocation": {"latitude": 51.5074, "longitude": -0.1278},
    "permissions": ["geolocation"],
}

HEALTH: dict[str, object] = {}

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)

UK_CONTEXT["user_agent"] = UA


@dataclass
class Product:
    source: str
    bank: str
    rate: float
    term_raw: str
    bucket: str
    isa: bool
    min_deposit: float | None = None    # None means the source does not publish one
    view: str = ""                      # which listing it came from, e.g. "joint"


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


def parse_money(txt: str) -> float | None:
    m = re.search(r"£\s*([\d,]+(?:\.\d+)?)", txt)
    return float(m.group(1).replace(",", "")) if m else None


def parse_rate(txt) -> float | None:
    """
    A rate from text.

    Flagstone's schema.org blocks used to carry "4.45%" and now carry a bare
    "4.65", so a percent sign can no longer be required: demanding one returned
    None for every product and the platform silently reported zero. The bare
    form is accepted only when the whole string is a number, so a stray figure
    inside a longer sentence is still ignored.
    """
    txt = str(txt)
    m = re.search(r"(\d+\.\d+)\s*%", txt)
    if m:
        return float(m.group(1))
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*", txt)
    return float(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Source: Hargreaves Lansdown  (needs a browser: Next.js hydrates the tables)
# ---------------------------------------------------------------------------

# The explicit tab parameter, as it appears in a real visitor's address bar.
# The bare URL renders the same tables but has never been compared against
# this, and it is the one difference we have not tested.
HL_GENERAL = "https://www.hl.co.uk/savings/latest-savings-rates-and-products?filter=active-savings"
HL_ISA = "https://www.hl.co.uk/savings/latest-savings-rates-and-products?filter=cash-isa"


def parse_hl(html: str, isa: bool, quiet: bool = False) -> list[Product]:
    """See _parse_hl. This wrapper logs what was skipped, so a silent shortfall
    in a term table shows up in the run log instead of only in the report."""
    out, skipped = _parse_hl(html, isa)
    from collections import Counter

    # HL prints when it last refreshed against Moneyfacts. If that date is not
    # today's, we are being served a stale page and no parser fix will help.
    if quiet:
        return out

    stamp = re.search(
        r"last checked against Moneyfacts on\s+([\d]{1,2}\s+\w+\s+\d{4}[^.<]*)",
        re.sub(r"<[^>]+>", " ", html),
    )
    if stamp:
        when = re.sub(r"\s+", " ", stamp.group(1)).strip()
        print(f"    HL page says rates last checked: {when}", file=sys.stderr)
        HEALTH["HL rates last checked"] = when
    per = Counter(p.bucket for p in out)
    label = "HL ISA" if isa else "HL"
    print(f"    {label} by term: " + ", ".join(f"{k}={v}" for k, v in sorted(per.items())),
          file=sys.stderr)
    if skipped:
        top = Counter(skipped).most_common(6)
        print(f"    {label} skipped rows: " + ", ".join(f"{k}x{v}" for k, v in top),
              file=sys.stderr)
    return out


def _parse_hl(html: str, isa: bool) -> tuple[list[Product], list[str]]:
    """HL renders one <table> per term with a Bank / AER / Term column set."""
    soup = BeautifulSoup(html, "lxml")
    out, skipped = [], []
    for table in soup.find_all("table"):
        heads = [th.get_text(" ", strip=True).lower() for th in table.select("thead th")]
        if not heads:
            skipped.append("table:no-thead")
            continue
        if "bank" not in heads[0]:
            skipped.append(f"table:head0={heads[0][:18]}")
            continue
        try:
            i_aer = next(i for i, h in enumerate(heads) if h == "aer")
            i_term = next(i for i, h in enumerate(heads) if h == "term")
        except StopIteration:
            skipped.append(f"table:cols={'/'.join(heads)[:40]}")
            continue
        i_min = next((i for i, h in enumerate(heads) if "min opening" in h), None)

        for tr in table.select("tbody tr"):
            tds = tr.find_all("td")
            if len(tds) <= max(i_aer, i_term):
                continue
            bank = re.sub(r"\s+", " ", tds[0].get_text(" ", strip=True))
            bank = re.sub(r"^Market Leading Rate\s*", "", bank).strip()
            rate = parse_rate(tds[i_aer].get_text())
            term_raw = tds[i_term].get_text(" ", strip=True)
            bucket = normalise_term(term_raw, isa)
            min_dep = (parse_money(tds[i_min].get_text())
                       if i_min is not None and len(tds) > i_min else None)
            if rate and bucket:
                out.append(Product("HL", bank, rate, term_raw, bucket, isa, min_dep))
            elif not rate:
                skipped.append("row:no-rate")
            else:
                skipped.append(f"row:term={term_raw[:18]}")
    return out, skipped


# ---------------------------------------------------------------------------
# Source: Meteor  (server rendered, plain fetch is enough)
# ---------------------------------------------------------------------------

# All four URLs from the brief. The filter0 param is applied client-side, so
# these very likely return identical HTML, but fetching all four costs two
# extra plain requests and removes any doubt about ISA-only products being
# absent from the general pages. Dedupe in top_ten() handles the overlap.
METEOR = {
    "easy-access general": "https://savings.meteoram.com/savings/easy-access?filter0=general",
    "fixed-term general": "https://savings.meteoram.com/savings/fixed-term?filter0=general",
    "easy-access isa": "https://savings.meteoram.com/savings/easy-access?filter0=cash-isa",
    "fixed-term isa": "https://savings.meteoram.com/savings/fixed-term?filter0=cash-isa",
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


def meteor_pages(url: str) -> list[str]:
    """
    Fetch every page of a Meteor listing.

    Meteor paginates server-side with &page=N and links each page in a
    .pagination block, so page 1 alone gives about 10 products. The filter0
    param is also server-side, which is why all four brief URLs are fetched.
    """
    first = fetch_plain(url)
    soup = BeautifulSoup(first, "lxml")

    pages = {1}
    for a in soup.select(".pagination a[href]"):
        m = re.search(r"[?&]page=(\d+)", a["href"])
        if m:
            pages.add(int(m.group(1)))

    out = [first]
    for n in sorted(pages)[1:]:
        sep = "&" if "?" in url else "?"
        out.append(fetch_plain(f"{url}{sep}page={n}"))
    return out


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

        m_dep = re.search(r"Deposit amount\s*(£[\d,]+)", text)
        min_dep = parse_money(m_dep.group(1)) if m_dep else None

        for is_isa in (v for v, on in ((False, general), (True, isa)) if on):
            bucket = normalise_term(term_raw, is_isa)
            if bucket:
                out.append(Product("Meteor", bank, rate, term_raw, bucket, is_isa, min_dep))

    return out


# ---------------------------------------------------------------------------
# Source: Raisin  (needs a browser; sort by term to force all rows to render)
# ---------------------------------------------------------------------------

# Raisin's product cards carry a data-testid with the product code, e.g.
# dcms-product-table-block-offer-list-card-RCU403. That is stable across
# deploys; the styled-components class names next to it are build hashes that
# change every release, and keying on one of those is what broke this source.
RAISIN_CARD = '[data-testid^="dcms-product-table-block-offer-list-card"]'
RAISIN_READY = RAISIN_CARD
HL_READY = "table tbody tr"

RAISIN_EASY = "https://www.raisin.com/en-gb/savings-accounts/easy-access-savings-accounts/"
RAISIN_FIXED = "https://www.raisin.com/en-gb/savings-accounts/fixed-rate-bonds/"


def parse_raisin(html: str, page_kind: str) -> list[Product]:
    """
    Parse Raisin's product cards.

    A card reads, in one run of text:
        "RCI Bank 5.10 % AER 3 years \u00b7 £ 120,000.00 +£ 8,046.78 Details Save now"
    An easy access card is the same without the term. The bank name leads the
    card and is taken from the text rather than a logo's alt attribute, and the
    term is anchored on AER rather than the old trailing "Max.", which the
    redesign removed. Badges such as "Sharia account" trail the card and so sit
    outside both captures.
    """
    soup = BeautifulSoup(html, "lxml")
    out = []
    for card in soup.select(RAISIN_CARD):
        text = re.sub(r"\s+", " ", card.get_text(" ", strip=True))

        m = re.search(r"(\d+\.\d+)\s*%\s*AER", text)
        if not m:
            continue
        rate = float(m.group(1))

        bank = text[:m.start()].strip(" \u00b7|-") or "unknown"

        if page_kind == "easy":
            term_raw = "Easy Access"
        else:
            m_term = re.search(
                r"AER\s+(\d+(?:\.\d+)?\s*(?:month|months|year|years))", text, re.I
            )
            if not m_term:
                continue
            term_raw = m_term.group(1)

        bucket = normalise_term(term_raw, isa=False)
        if not bucket:
            continue

        out.append(Product("Raisin", bank, rate, term_raw, bucket, False))
    return out


# ---------------------------------------------------------------------------
# Source: Flagstone ISA page  (schema.org JSON in the HTML, plain fetch)
# ---------------------------------------------------------------------------

# Flagstone is the only platform that splits its listing by account type in
# the URL. HL, Meteor and Raisin show one personal table covering both, so
# individual and joint only need handling here.
_FS = "https://clients.direct.flagstoneim.com/build-your-sample-portfolio?accounttype="
FLAGSTONE_SAMPLE_TYPES = {
    "individual": _FS + "individual",
    "joint": _FS + "joint",
}
FLAGSTONE_SAMPLE_READY = "article"

def parse_flagstone_sample(html: str, view: str = "") -> list[Product]:
    """
    The sample portfolio renders one <article> per product:
    "Kent Reliance 4.18% | 4.18% Rate (AER | Gross) Fixed 12 months Term
     £1,000 - £1,000,000 Min - Max deposit Add"
    The first percentage is AER, the second Gross. Take the first.
    """
    soup = BeautifulSoup(html, "lxml")
    out = []
    for art in soup.find_all("article"):
        text = re.sub(r"\s+", " ", art.get_text(" ", strip=True))

        rates = re.findall(r"(\d+\.\d+)%", text)
        if not rates:
            continue
        rate = float(rates[0])

        m = re.search(
            r"(Fixed\s+\d+\s+(?:month|months|year|years)|Instant access)", text, re.I
        )
        if not m:
            continue
        term_raw = m.group(1)

        bank = re.split(r"\s*(?:Best Flagstone|\d+\.\d+%)", text)[0].strip()

        m_dep = re.search(r"(£[\d,]+)\s*[-\u2013]\s*£[\d,]+\s*Min\s*-\s*Max", text)
        min_dep = parse_money(m_dep.group(1)) if m_dep else None

        bucket = normalise_term(term_raw, isa=False)
        if bucket:
            out.append(Product("Flagstone", bank or "unknown", rate, term_raw,
                               bucket, False, min_dep, view))
    return out


# Flagstone sorts by rate descending across every term at once, so instant
# access products, which pay less than fixed term, sit at the bottom of a
# 17-page list. However many pages the paging reached that morning decided how
# many easy access products survived: 16 products one day, 10 easy access the
# next, 2 the day after. Filtering by term instead makes each view small and
# puts that term's best rates on page one, so a top ten needs no paging.
#
# The hooks are data-heap-id analytics attributes, not styled-components
# hashes, so they survive a restyle the way the class names do not.
FLAGSTONE_TERM_FILTERS = [
    ("easy access", "Instant"),
    ("6m", "M6"),
    ("9m", "M7To9"),
    ("12m", "M12"),
    ("24m", "Y2"),
    ("36m", "Y3"),
    ("60m", "Y5Plus"),
]


def _first_article(page) -> str:
    """Identity of the top card, used to tell a redraw from a stale view."""
    try:
        a = page.locator(FLAGSTONE_SAMPLE_READY).first
        return a.inner_text(timeout=3000)[:120] if a.count() else ""
    except Exception:
        return ""


def _flagstone_results(page) -> str:
    """The page's own count, e.g. "27" from "Results (27)". Diagnostic only."""
    try:
        m = re.search(r"Results \((\d+)\)", page.inner_text("body", timeout=3000))
        return m.group(1) if m else "?"
    except Exception:
        return "?"


def _expand_terms(page):
    """
    Open the collapsed block of less common terms if it is closed.

    Only the popular terms (Instant Access, 6 and 12 months, 90-95 days) show
    on load. 7-9 months, 2, 3 and 5+ years sit behind a "Show more" toggle,
    and a click on a label inside a collapsed block misses. Decided on the
    toggle's aria-expanded rather than the label's visibility, because a
    collapsed block can report its contents as visible while clipping them
    out of reach. Once open the button reads "Show less", so this is a no-op.
    Called before every filter in case clearing collapses it again.
    """
    try:
        more = page.get_by_role("button", name="Show more", exact=True)
        if more.count() and (more.first.get_attribute("aria-expanded") or "") != "true":
            more.first.click(timeout=4000)
            page.wait_for_timeout(500)
    except Exception:
        pass


def fetch_flagstone_filtered(url: str, view: str) -> list[Product]:
    """
    Read the sample portfolio once per term rather than paging the whole list.

    The filter only decides which products are on screen; the bucket still
    comes from each article's own text, so a filter that selects loosely (the
    7-9 month one, say) costs nothing. If the filters are missing, or any of
    them fails, the old paging pass runs as well and the two are pooled, so a
    failure here can never leave Flagstone worse off than paging alone.
    """
    from playwright.sync_api import sync_playwright

    products: list[Product] = []
    failed: list[str] = []
    filters_present = True

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(**UK_CONTEXT, viewport={"width": 1400, "height": 1200})
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        dismiss_cookies(page)
        page.wait_for_selector(FLAGSTONE_SAMPLE_READY, timeout=30000)

        if page.locator('[data-heap-id^="term-length-filter"]').count() == 0:
            filters_present = False
        else:
            for label, fid in FLAGSTONE_TERM_FILTERS:
                hook = f'[data-heap-id="term-length-filter-{fid}-checkbox"]'
                try:
                    # Reset first: the filters are additive. The button is
                    # disabled when nothing is ticked, so skip it then rather
                    # than wait out a click timeout.
                    clear = page.get_by_role("button", name="Clear all", exact=True)
                    if clear.count() and clear.first.is_enabled():
                        clear.first.click(timeout=3000)
                        page.wait_for_timeout(700)

                    if page.locator(hook).count() == 0:
                        print(f"    flagstone {label}: no {fid} filter", file=sys.stderr)
                        failed.append(label)
                        continue

                    _expand_terms(page)
                    before_click = _first_article(page)
                    lab = page.locator(f"label:has({hook})").first
                    lab.scroll_into_view_if_needed(timeout=4000)
                    lab.click(timeout=5000)

                    # Wait for the list to actually redraw rather than a fixed
                    # pause, so a slow response cannot leave the previous
                    # term's products on screen to be parsed as this one's.
                    for _ in range(15):
                        page.wait_for_timeout(400)
                        now = _first_article(page)
                        if now and now != before_click:
                            break
                    page.wait_for_selector(FLAGSTONE_SAMPLE_READY, timeout=15000)
                except Exception as e:
                    print(f"    flagstone {label}: filter failed, {type(e).__name__}",
                          file=sys.stderr)
                    failed.append(label)
                    continue

                got = parse_flagstone_sample(page.content(), view)
                products += got
                print(f"    flagstone {view} {label}: {len(got)} "
                      f"(page says {_flagstone_results(page)})", file=sys.stderr)

        browser.close()

    # Outside the Playwright context on purpose: fetch_flagstone_pages opens
    # its own, and Playwright's sync API refuses to nest inside a live one.
    if not filters_present or failed:
        why = "filters absent" if not filters_present else f"failed: {', '.join(failed)}"
        print(f"    flagstone {view}: {why}, adding paging pass", file=sys.stderr)
        products += [q for h in fetch_flagstone_pages(url)
                     for q in parse_flagstone_sample(h, view)]

    return products


FLAGSTONE_MAX_PAGES = 25    # safety stop only. The real end is the last page button.


def fetch_flagstone_pages(url: str, max_pages: int = FLAGSTONE_MAX_PAGES) -> list[str]:
    """
    Return the HTML of every page of the sample portfolio.

    The listing is paginated with numbered buttons, and changing page replaces
    the articles rather than appending to them, so each page has to be captured
    as it is shown. The total is read from the highest numbered button rather
    than assumed: Flagstone runs to 17 pages, and a fixed guess silently cut
    the platform off at a third of its products.
    """
    from playwright.sync_api import sync_playwright

    out = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(**UK_CONTEXT, viewport={"width": 1400, "height": 1200})
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        dismiss_cookies(page)
        page.wait_for_selector(FLAGSTONE_SAMPLE_READY, timeout=30000)
        out.append(page.content())

        # Highest digit on a pagination button is the last page.
        total = 1
        try:
            for txt in page.locator("button").all_inner_texts():
                txt = txt.strip()
                if txt.isdigit():
                    total = max(total, int(txt))
        except Exception:
            pass
        total = min(total, max_pages)

        # Advance one page at a time. Clicking numbers fails past page 3:
        # the visible window slides to 1 ... 3 4 5 ... 17, so there is no
        # button for the page you want. The next arrow is always present.
        for n in range(2, total + 1):
            clicked = False

            # Prefer the numbered button when it happens to be on screen.
            try:
                btn = page.get_by_role("button", name=str(n), exact=True)
                if btn.count() > 0:
                    btn.first.scroll_into_view_if_needed(timeout=4000)
                    btn.first.click(timeout=4000)
                    clicked = True
            except Exception:
                pass

            # Otherwise the next arrow, by whatever it exposes itself as.
            if not clicked:
                for sel in ('[aria-label*="next" i]', 'button:has-text("\u203a")',
                            'button:has-text("\u2192")', "li:last-child > button"):
                    try:
                        nxt = page.locator(sel).last
                        if nxt.count() == 0 or not nxt.is_enabled():
                            continue
                        nxt.scroll_into_view_if_needed(timeout=4000)
                        nxt.click(timeout=4000)
                        clicked = True
                        break
                    except Exception:
                        continue

            if not clicked:
                print(f"    flagstone: could not reach page {n}", file=sys.stderr)
                break

            try:
                page.wait_for_timeout(1400)
                page.wait_for_selector(FLAGSTONE_SAMPLE_READY, timeout=15000)
                out.append(page.content())
            except Exception:
                break

        browser.close()

    print(f"    flagstone pages captured: {len(out)} of {total}", file=sys.stderr)
    return out


FLAGSTONE = {
    # Public marketing pages, same Umbraco template, same schema.org blocks.
    # The client sample-portfolio tool is deliberately excluded: it needs a
    # login and it is Flagstone's client area.
    # Only the ISA page. The instant-access and fixed-rates pages publish no
    # minimum deposit in their markup, and the sample portfolio covers the same
    # non-ISA products with a verified minimum, so they are redundant here.
    "cash ISA": ("https://www.flagstoneim.com/personal/cash-isa", True),
}


def parse_flagstone(html: str, isa: bool) -> list[Product]:
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
        bucket = normalise_term(term_raw, isa)
        if rate and bucket:
            out.append(Product("Flagstone", brand, rate, term_raw, bucket, isa))
    return out


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def fetch_plain(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    return r.text


DEBUG_DIR = os.environ.get("DEBUG_DIR", "")


def save_debug(name: str, html: str):
    """Write the page the runner actually got, for inspection as an artifact."""
    if not DEBUG_DIR:
        return
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        path = os.path.join(DEBUG_DIR, f"{name}.html")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(html)
        print(f"    saved {path} ({len(html):,} bytes)", file=sys.stderr)
    except Exception as e:
        print(f"    debug save failed: {e}", file=sys.stderr)


def fetch_rendered(url: str, ready: str, prep=None) -> str:
    """
    Load with a real browser and wait for `ready` to appear.

    Never wait for networkidle on these sites. They run Datadog RUM, Exponea,
    Clarity and Optimizely, which beacon on a timer, so the network never goes
    quiet and the wait burns the full timeout on a page that loaded in seconds.
    Wait for the content instead.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(**UK_CONTEXT, viewport={"width": 1400, "height": 1200},
                                extra_http_headers={
                                    "Cache-Control": "no-cache",
                                    "Pragma": "no-cache",
                                    "Accept-Language": "en-GB,en;q=0.9",
                                })
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        dismiss_cookies(page)
        page.wait_for_selector(ready, timeout=30000)
        if prep:
            prep(page)
        html = page.content()
        browser.close()
    return html


def scroll_until_stable(page, selector: str, limit: int = 40) -> int:
    """
    Scroll until the number of matching elements stops growing.

    Four stalls rather than two: HL renders its term tables lazily and pauses
    between them, so an impatient loop stops halfway down the page and the
    later terms never appear.
    """
    last = -1
    stalls = 0
    for _ in range(limit):
        count = page.locator(selector).count()
        if count == last:
            stalls += 1
            if stalls >= 4:
                break
        else:
            stalls = 0
        last = count

        # Small steps, not a jump to the bottom. HL reveals each term table as
        # it scrolls into view, so an End keypress lands past the triggers and
        # the tables in between never render.
        page.evaluate("window.scrollBy(0, 700)")
        page.wait_for_timeout(450)
        page.evaluate("window.scrollBy(0, 700)")
        page.wait_for_timeout(450)
    return last


def click_load_more(page, button, selector: str, limit: int = 40) -> int:
    """
    Click a 'Load more' button until the listing stops growing.

    Driven by the item count, not the button's presence. Raisin's button is in
    the DOM from first paint and stays there after the last page, so "click
    until it disappears" either spins pointlessly or stops on the first
    hiccup. Growth is the only reliable evidence a click did anything, and the
    wait after each click is adaptive rather than a fixed pause.
    """
    count = page.locator(selector).count()
    start = count

    for _ in range(limit):
        try:
            btn = page.locator(button).first
            if btn.count() == 0 or not btn.is_visible():
                break
            btn.scroll_into_view_if_needed(timeout=5000)
            btn.click(timeout=5000)
        except Exception:
            break

        before = count
        for _ in range(12):                 # up to ~5s for new rows to land
            page.wait_for_timeout(400)
            count = page.locator(selector).count()
            if count > before:
                break
        if count == before:                 # click achieved nothing: end of list
            break

    if start and count > start:
        print(f"    load more: {start} -> {count}", file=sys.stderr)
    return count


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


# The button exposes aria-label="Load more offers"; its visible text sits in a
# nested div, so match the label first and fall back to text. It reads "Load
# more offers" now and "Load more" before, and both selectors are substring
# matches, so the shorter form covers either.
RAISIN_LOAD_MORE = ('button[aria-label*="Load more" i], '
                    'button:has-text("Load more")')


def raisin_prep(page, amount, warn_on_single_page=True):
    """
    Set the amount, then exhaust the Load more button.

    Raisin shows a first batch of cards and hides the rest behind Load more. Sorting only
    reorders those 10, which is why an earlier run returned five year bonds and
    nothing else. No sort is needed once every row is loaded, we rank ourselves.
    """
    seen_api = set()

    def on_response(resp):
        try:
            u = resp.url
            if "api" in u and resp.request.resource_type in ("xhr", "fetch"):
                if "json" in resp.headers.get("content-type", "").lower():
                    seen_api.add(u.split("?")[0])
        except Exception:
            pass

    page.on("response", on_response)

    # The deposit box names itself; the positional fallbacks are a safety net.
    for sel in ('input[name="depositAmount"]', "input[inputmode='numeric']",
                "input[type='text']"):
        try:
            box = page.locator(sel).first
            box.click(timeout=3000)
            box.fill(str(amount))
            page.keyboard.press("Enter")
            page.wait_for_timeout(2000)
            break
        except Exception:
            continue

    got = click_load_more(page, RAISIN_LOAD_MORE, RAISIN_READY)
    print(f"    raisin rows loaded: {got}", file=sys.stderr)

    # Raisin's page JSON carries full structured records for the first batch
    # only (id, termNormalized.months, interestRateEffective, and a minimum
    # balance it never prints in the HTML). Paging must therefore call an API.
    # Naming it here costs nothing and would let this source drop the browser
    # entirely, the way Meteor and Flagstone's ISA page already have.
    for u in sorted(seen_api)[:6]:
        print(f"    raisin api: {u[:120]}", file=sys.stderr)
    if warn_on_single_page and got <= 10:
        print("    WARNING: Load more did not fire, only page 1 captured", file=sys.stderr)



def fetch_hl_by_term(url: str, isa: bool) -> list[Product]:
    """
    Collect HL's Active Savings products.

    Note on scope: HL's public page also shows Moneyfacts "market-leading"
    rates for providers that are not on its platform, such as Kent Reliance
    and Charter Savings. Those are served to a UK browser but not to this
    runner: they appear nowhere in the HTML it receives, and the term filter
    is client-side, so no interaction can reveal them. What follows is HL's
    own Active Savings range, which is what its API returns.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(**UK_CONTEXT, viewport={"width": 1400, "height": 1600},
                                extra_http_headers={"Cache-Control": "no-cache",
                                                    "Accept-Language": "en-GB,en;q=0.9"})
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        dismiss_cookies(page)
        page.wait_for_selector(HL_READY, timeout=30000)
        hl_prep(page)
        html = page.content()
        save_debug("hl-all" + ("-isa" if isa else ""), html)

    return parse_hl(html, isa)


def hl_prep(page):
    got = scroll_until_stable(page, HL_READY)

    # HL keeps filling its tables after the row count first goes quiet: rows
    # are inserted at the top of each term table once a later call returns,
    # which is why the highest rates were the ones going missing. Settle and
    # re-check rather than trusting the first plateau.
    for _ in range(4):
        page.wait_for_timeout(3000)
        now = page.locator(HL_READY).count()
        if now == got:
            break
        print(f"    hl rows still arriving: {got} -> {now}", file=sys.stderr)
        got = scroll_until_stable(page, HL_READY)

    print(f"    hl rows after scroll: {got}", file=sys.stderr)


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
    attempt("HL general", lambda: fetch_hl_by_term(HL_GENERAL, False))
    attempt("HL ISA", lambda: fetch_hl_by_term(HL_ISA, True))

    for kind, url in METEOR.items():
        attempt(
            f"Meteor {kind}",
            lambda u=url: [p for h in meteor_pages(u) for p in parse_meteor(h)],
        )

    for acct, url in FLAGSTONE_SAMPLE_TYPES.items():
        attempt(f"Flagstone {acct}",
                lambda u=url, a=acct: fetch_flagstone_filtered(u, a))

    for kind, (url, isa) in FLAGSTONE.items():
        attempt(
            f"Flagstone {kind}",
            lambda u=url, i=isa: parse_flagstone(fetch_plain(u), i),
        )

    # Raisin twice per page, once at each amount, pooled. Products whose range
    # excludes one amount are picked up by the other; dedupe drops the overlap.
    for amount in RAISIN_AMOUNTS:
        for label, url, kind in (("easy", RAISIN_EASY, "easy"),
                                 ("fixed", RAISIN_FIXED, "fixed")):
            attempt(
                f"Raisin {label} £{amount:,}",
                lambda u=url, k=kind, a=amount: parse_raisin(
                    fetch_rendered(u, RAISIN_READY,
                                   lambda pg, amt=a: raisin_prep(pg, amt)), k),
            )

    return products


def top_ten(products: list[Product], source: str | None = None) -> dict[str, list[Product]]:
    """
    Rank within each bucket. Duplicates across platforms are kept, as requested,
    but the exact same listing scraped twice from one platform is not a duplicate,
    it's a double count. Those are dropped.

    The dedupe signature deliberately ignores `view`, so a product offered to
    both individual and joint holders takes one slot rather than two, while a
    product only available on one of them still gets in.
    """
    out = {}
    for key, _ in BUCKETS:
        rows, seen = [], set()
        for p in products:
            if p.bucket != key:
                continue
            if source and p.source != source:
                continue
            sig = (p.source, p.bank.lower().strip(), p.rate, p.term_raw.lower())
            if sig in seen:
                continue
            seen.add(sig)
            rows.append(p)
        rows.sort(key=lambda p: -p.rate)
        out[key] = rows[:10]
    return out


def report_rows(ranked: dict[str, list[Product]], title: str, note: str = "") -> list[list]:
    """The ten tables, rates only, in Anna's order. Used by sheet and xlsx."""
    rows = [[title]]
    if note:
        rows.append([note])
    rows.append([])
    for key, label in BUCKETS:
        rows.append([label])
        got = ranked[key]
        for i in range(10):
            rows.append([i + 1, f"{got[i].rate:.2f}" if i < len(got) else ""])
        rows.append([])
    return rows


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

    def tab(title, rows_needed=200):
        existing = [w.title for w in sh.worksheets()]
        return (sh.worksheet(title) if title in existing
                else sh.add_worksheet(title, rows=rows_needed, cols=6))

    # Pooled report across all four platforms.
    rows = report_rows(ranked, f"All platforms, {today}, {DEPOSIT_LABEL}")
    rows.append(["Source health"])
    for label, count in HEALTH.items():
        rows.append([label, count])

    report = tab("Report")
    report.clear()
    report.update(values=rows, range_name="A1")

    # One tab per platform, same ten tables, that platform only.
    for name in SOURCES:
        per = top_ten(products, source=name)
        ws = tab(name)
        ws.clear()
        ws.update(
            values=report_rows(per, f"{name} only, {today}, {DEPOSIT_LABEL}",
                               PLATFORM_NOTES.get(name, "")),
            range_name="A1",
        )

    detail_rows = [["date", "bucket", "rate", "bank", "source",
                    "term as listed", "min deposit", "listing"]]
    for key, label in BUCKETS:
        for p in ranked[key]:
            detail_rows.append([
                today, label, p.rate, p.bank, p.source, p.term_raw,
                p.min_deposit if p.min_deposit is not None else "not published",
                p.view or "personal",
            ])

    detail = sh.worksheet("Detail") if "Detail" in [w.title for w in sh.worksheets()] \
        else sh.add_worksheet("Detail", rows=2000, cols=8)
    detail.append_rows(detail_rows[1:], value_input_option="USER_ENTERED")

    print(f"Wrote {len(products)} products to sheet", file=sys.stderr)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def build_xlsx(ranked, products, path: str):
    """Two sheets: the report as Anna wants it, and the full audit trail."""
    from datetime import date
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    bold = Font(name="Arial", bold=True)
    plain = Font(name="Arial")

    def write_block(ws, ranked_set, title, note=""):
        for row in report_rows(ranked_set, title, note):
            ws.append(row if row else [None])
        ws.column_dimensions["A"].width = 28
        ws.column_dimensions["B"].width = 12
        ws["A1"].font = bold

    ws = wb.active
    ws.title = "Report"
    write_block(ws, ranked, f"All platforms, {date.today():%d %B %Y}, {DEPOSIT_LABEL}")
    ws.append(["Source health"])
    ws.cell(ws.max_row, 1).font = bold
    for label, count in HEALTH.items():
        ws.append([label, count])

    for name in SOURCES:
        per = wb.create_sheet(name)
        write_block(
            per,
            top_ten(products, source=name),
            f"{name} only, {date.today():%d %B %Y}, {DEPOSIT_LABEL}",
            PLATFORM_NOTES.get(name, ""),
        )

    det = wb.create_sheet("Detail")
    det.append(["bucket", "rank", "rate", "bank", "source", "term as listed",
                "min deposit", "listing"])
    for c in det[1]:
        c.font = bold
    for key, label in BUCKETS:
        for i, p in enumerate(ranked[key], 1):
            det.append([label, i, float(p.rate), p.bank, p.source, p.term_raw,
                        p.min_deposit if p.min_deposit is not None else "not published",
                        p.view or "personal"])
    for col, w in zip("ABCDEFGH", (18, 6, 8, 38, 12, 20, 14, 12)):
        det.column_dimensions[col].width = w
    det.freeze_panes = "A2"

    labels = {label for _, label in BUCKETS} | {"Source health"}
    for sheet in wb.worksheets:
        for row in sheet.iter_rows():
            for c in row:
                if c.row > 1 and c.column == 1 and c.value in labels:
                    c.font = bold
                elif c.font is not bold:
                    c.font = plain

    wb.save(path)


def send_email(ranked, products):
    """
    Send from Gmail over SMTP. Authenticating as a real Gmail account means the
    mail leaves Google's servers with valid SPF and DKIM, rather than
    originating from a GitHub runner IP, which matters for getting into a
    bank's inbox rather than its quarantine.
    """
    import smtplib
    import ssl
    from datetime import date
    from email.message import EmailMessage

    user = os.environ.get("GMAIL_USER")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    recipients = [a.strip() for a in os.environ.get("REPORT_TO", "").split(",") if a.strip()]

    if not (user and password and recipients):
        print("Email not configured, skipping", file=sys.stderr)
        return

    thin = [label for key, label in BUCKETS if len(ranked[key]) < 10]
    # Only source rows count. The health block also carries descriptive
    # strings and a count that is legitimately zero on a good day.
    failed = [k for k, v in HEALTH.items()
              if (isinstance(v, str) and v.startswith("FAILED"))
              or (v == 0 and k != "shown, minimum not published")]

    body = [
        f"Savings rates for {date.today():%d %B %Y}, {DEPOSIT_LABEL}.",
        "",
        render_text(ranked).rstrip(),
        "",
    ]
    if thin:
        body.append(f"Fewer than 10 products available: {', '.join(thin)}.")
    if failed:
        body.append(f"Sources that returned nothing: {', '.join(failed)}.")
        body.append("Those tables are drawn from the remaining sources only.")
    body += [
        "",
        f"{len(products)} products collected. Full detail in the attached file.",
        "Rates are AER. Compiled automatically, worth spot checking before use.",
    ]

    msg = EmailMessage()
    msg["Subject"] = f"Savings rates, {date.today():%d %b %Y}"
    msg["From"] = user
    msg["To"] = ", ".join(recipients)
    msg.set_content("\n".join(body))

    path = f"savings-rates-{date.today():%Y-%m-%d}.xlsx"
    build_xlsx(ranked, products, path)
    with open(path, "rb") as fh:
        msg.add_attachment(
            fh.read(),
            maintype="application",
            subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=path,
        )

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(user, password)
        s.send_message(msg)

    print(f"Emailed {len(recipients)} recipient(s)", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", action="store_true", help="print only, no sheet write, no email")
    args = ap.parse_args()

    products = collect()
    if not products:
        sys.exit("No products collected. Something upstream changed.")

    ranked = top_ten(products)
    print(render_text(ranked))

    thin = [label for key, label in BUCKETS if len(ranked[key]) < 10]
    if thin:
        print(f"\nUnder 10 results: {', '.join(thin)}", file=sys.stderr)

    shown = [p for v in ranked.values() for p in v]
    unverified = sum(1 for p in shown if p.min_deposit is None)
    mins = sorted({p.min_deposit for p in shown if p.min_deposit is not None})
    spread = f"£{mins[0]:,.0f} to £{mins[-1]:,.0f}" if mins else "none published"
    print(f"Minimum deposits in the report: {spread}", file=sys.stderr)
    print(f"Shown without a published minimum: {unverified}", file=sys.stderr)
    HEALTH["minimum deposit range"] = spread
    HEALTH["shown, minimum not published"] = unverified

    if not args.local:
        write_sheet(ranked, products)
        send_email(ranked, products)


if __name__ == "__main__":
    main()
