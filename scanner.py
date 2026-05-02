"""
scanner.py — Amul stock checker with MongoDB integration
---------------------------------------------------------
- Reads all products from MongoDB
- Gets all unique pincodes from users collection  
- For each pincode, checks stock of all products using Playwright
- Writes results to the stock collection

Run on a schedule (GitHub Actions / cron).

Config (environment variables or .env):
  MONGODB_URI — MongoDB Atlas connection string

Requirements:
  pip install playwright pymongo python-dotenv
  playwright install chromium
"""

import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from dotenv import load_dotenv
from pymongo import MongoClient
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ── Config ────────────────────────────────────────────────────────────────────
load_dotenv()
MONGODB_URI = os.environ["MONGODB_URI"]
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("scanner")
logging.getLogger("pymongo").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

# ── MongoDB ───────────────────────────────────────────────────────────────────
client    = MongoClient(MONGODB_URI)
db        = client["product_bot"]
users_col = db["users"]
prods_col = db["products"]
stock_col = db["stock"]
# ─────────────────────────────────────────────────────────────────────────────

STOCK_SIGNALS = [
    {"signal": "out of stock",          "in_stock": False},
    {"signal": "sold out",              "in_stock": False},
    {"signal": "currently unavailable", "in_stock": False},
    {"signal": "notify me",             "in_stock": False},
    {"signal": "not available",         "in_stock": False},
    {"signal": "coming soon",           "in_stock": False},
    {"signal": "add to cart",           "in_stock": True},
    {"signal": "add to bag",            "in_stock": True},
    {"signal": "buy now",               "in_stock": True},
]

PRODUCT_SECTION_SELECTORS = [
    ".product-detail",
    ".ms-product-detail",
    "#product-detail",
    ".product-description",
    "[itemtype*='schema.org/Product']",
    "main",
]

PAGE_LOAD_TIMEOUT_MS    = 60_000
NETWORK_IDLE_TIMEOUT_MS = 15_000
POST_PINCODE_WAIT_MS    = 15_000
POST_PINCODE_SETTLE_S   = 2


@dataclass
class StockResult:
    product_id: str
    name: str
    url: str
    pincode: str
    in_stock: bool | None
    matched_signal: str | None
    section_used: str | None = None
    error: str | None = None


# ── Pincode handling ──────────────────────────────────────────────────────────

def _pincode_already_set(page, pincode: str) -> bool:
    """
    Check if pincode is already set by looking at the header element.
    Much faster than waiting for the modal to appear or not.
    """
    header = page.evaluate("""() => {
        const el = document.querySelector('.loc_area, .pincode_wrap, .location_pin_wrap, .selected-store');
        return el ? el.innerText.trim() : null;
    }""")
    if header and pincode in header:
        log.debug(f"[PINCODE] Already set — header shows: '{header}'")
        return True
    return False


def _submit_pincode(page, pincode: str) -> bool:
    """
    Set the pincode via the autocomplete modal.
    Returns True if submitted, False if already set.
    """
    # Fast path: check header first before waiting for modal
    if _pincode_already_set(page, pincode):
        return False

    PINCODE_INPUT = "#search"
    log.debug(f"[PINCODE] Checking for modal input '{PINCODE_INPUT}' ...")
    try:
        page.wait_for_selector(PINCODE_INPUT, timeout=5_000)
        log.debug("[PINCODE] Modal found.")
    except PWTimeout:
        log.debug("[PINCODE] No modal — pincode may already be set.")
        return False

    log.debug(f"[PINCODE] Typing '{pincode}' ...")
    page.locator(PINCODE_INPUT).first.click()
    time.sleep(0.4)
    page.locator(PINCODE_INPUT).first.type(pincode, delay=100)

    # Try to click a matching autocomplete suggestion
    # All selectors tried with a short timeout since dropdown may not always appear
    SUGGESTION_SELECTORS = [
        ".input-auto-substore .dropdown-menu li",
        ".input-auto-substore ul li",
        ".substore-list li",
        "[class*='suggestion'] li",
        "[class*='autocomplete'] li",
    ]

    clicked = False
    for sel in SUGGESTION_SELECTORS:
        try:
            page.wait_for_selector(sel, timeout=3_000)
            items = page.locator(sel).all()
            log.debug(f"[PINCODE] Dropdown appeared via '{sel}' ({len(items)} items).")
            for item in items:
                txt = (item.text_content() or "").strip()
                log.debug(f"[PINCODE]   Option: '{txt}'")
                if pincode in txt:
                    log.debug(f"[PINCODE]   → Clicking match: '{txt}'")
                    item.click()
                    clicked = True
                    break
            if not clicked and items:
                first = (items[0].text_content() or "").strip()
                log.debug(f"[PINCODE]   → No exact match, clicking first: '{first}'")
                items[0].click()
                clicked = True
            break
        except PWTimeout:
            continue

    if not clicked:
        log.debug("[PINCODE] No dropdown appeared — pressing Enter.")
        page.locator(PINCODE_INPUT).first.press("Enter")

    log.debug("[PINCODE] Waiting for page to settle ...")
    try:
        page.wait_for_load_state("networkidle", timeout=POST_PINCODE_WAIT_MS)
    except PWTimeout:
        log.warning("[PINCODE] Network did not go idle after pincode — continuing.")

    time.sleep(POST_PINCODE_SETTLE_S)

    # Verify
    header = page.evaluate("""() => {
        const el = document.querySelector('.loc_area, .pincode_wrap, .location_pin_wrap, .selected-store');
        return el ? el.innerText.trim() : null;
    }""")
    if header and pincode in header:
        log.debug(f"[PINCODE] Confirmed: '{header}'")
    else:
        log.warning(f"[PINCODE] Could not confirm pincode in header (shows: '{header}'). "
                    "Results may be for wrong location.")

    return True


# ── Product section extraction ────────────────────────────────────────────────

def _get_product_section(page) -> tuple[str, str]:
    """Extract HTML from the main product section only (not related carousels)."""
    for sel in PRODUCT_SECTION_SELECTORS:
        try:
            el = page.locator(sel).first
            if el.count() and el.is_visible():
                html = el.inner_html()
                log.debug(f"[SECTION] Matched '{sel}' ({len(html):,} chars).")
                return html, sel
        except Exception as e:
            log.debug(f"[SECTION] '{sel}' failed: {e}")

    # JS fallback: schema.org Product microdata
    schema_html = page.evaluate("""() => {
        const el = document.querySelector('[itemtype*="schema.org/Product"]');
        return el ? el.outerHTML : null;
    }""")
    if schema_html:
        log.debug(f"[SECTION] schema.org/Product matched ({len(schema_html):,} chars).")
        return schema_html, "schema.org/Product"

    log.warning("[SECTION] No selector matched — falling back to full page. May cause false positives.")
    html = page.content()
    return html, "full page (fallback)"


# ── Stock check ───────────────────────────────────────────────────────────────

def check_stock(page, product: dict, pincode: str) -> StockResult:
    name = product["name"]
    url  = product["url"]
    pid  = str(product["_id"])

    log.info(f"{'─' * 60}")
    log.info(f"[CHECK] {name}")
    log.info(f"[CHECK] {url}")

    # ── Navigate ──────────────────────────────────────────────────────────────
    log.debug("[NAV] Loading page ...")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
        page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT_MS)
        log.debug(f"[NAV] Loaded. Title: '{page.title()}' | URL: {page.url}")
    except PWTimeout:
        log.warning("[NAV] Network idle timed out — continuing.")
    except Exception as exc:
        log.error(f"[NAV] Navigation failed: {exc}")
        return StockResult(product_id=pid, name=name, url=url, pincode=pincode,
                           in_stock=None, matched_signal=None, error=str(exc))

    # ── Set pincode ───────────────────────────────────────────────────────────
    _submit_pincode(page, pincode)

    # ── Wait for product page to render ───────────────────────────────────────
    log.debug("[RENDER] Waiting for <h1> ...")
    try:
        page.wait_for_selector("h1", timeout=15_000)
        h1 = page.locator("h1").first.text_content()
        log.debug(f"[RENDER] h1: '{h1}'")
    except PWTimeout:
        log.error("[RENDER] <h1> not found — page did not render.")
        return StockResult(product_id=pid, name=name, url=url, pincode=pincode,
                           in_stock=None, matched_signal=None, error="Page did not render (no h1)")

    # ── Validate page loaded correctly (after pincode + render) ───────────────
    # Only check for errors now that the modal is dismissed and page has rendered
    try:
        body_text = page.locator("body").inner_text()
    except Exception:
        body_text = ""

    ERROR_PHRASES = [
        "we are sorry",
        "not a functioning page",
        "page not found",
        "looking for something",
    ]
    for phrase in ERROR_PHRASES:
        if phrase in body_text.lower():
            log.error(f"[NAV] Page error detected ('{phrase}'). Bad URL: {url}")
            return StockResult(product_id=pid, name=name, url=url, pincode=pincode,
                               in_stock=None, matched_signal=None,
                               error="Page not found — verify URL in MongoDB")

    # ── Log buttons (most useful diagnostic) ─────────────────────────────────
    try:
        buttons = [b.strip() for b in page.locator("button").all_text_contents() if b.strip()]
        log.debug(f"[BUTTONS] {buttons}")
    except Exception:
        pass

    # ── Extract product section & scan signals ────────────────────────────────
    section_html, section_label = _get_product_section(page)
    lower = section_html.lower()

    matched_in_stock = None
    matched_signal   = None
    for rule in STOCK_SIGNALS:
        signal = rule["signal"].lower()
        if signal in lower:
            idx = lower.find(signal)
            snippet = section_html[max(0, idx - 80):idx + 150].replace("\n", " ").strip()
            log.debug(f"[SIGNAL] Matched '{rule['signal']}' → in_stock={rule['in_stock']}")
            log.debug(f"[SIGNAL] Context: ...{snippet}...")
            matched_in_stock = rule["in_stock"]
            matched_signal   = rule["signal"]
            break

    # ── UNKNOWN: dump diagnostics ─────────────────────────────────────────────
    if matched_in_stock is None:
        log.warning("[UNKNOWN] No stock signal matched. Diagnostic dump:")
        log.debug(f"[UNKNOWN] Buttons: {buttons if 'buttons' in dir() else 'n/a'}")
        log.debug(f"[UNKNOWN] Body text (3000 chars):\n{body_text[:3000]}")

    return StockResult(
        product_id=pid, name=name, url=url, pincode=pincode,
        in_stock=matched_in_stock, matched_signal=matched_signal,
        section_used=section_label,
    )


# ── MongoDB write ─────────────────────────────────────────────────────────────

def save_stock_result(result: StockResult) -> None:
    now = datetime.now(timezone.utc)
    existing = stock_col.find_one({"product_id": result.product_id, "pincode": result.pincode})

    prev = existing.get("available") if existing else "never scanned"
    availability_changed = (existing is None or existing.get("available") != result.in_stock)

    update_fields = {
        "product_id":   result.product_id,
        "pincode":      result.pincode,
        "available":    result.in_stock,
        "last_checked": now,
        "section_used": result.section_used,
    }

    if result.error:
        update_fields["last_error"] = result.error

    if availability_changed:
        update_fields["last_changed"] = now
        if result.in_stock is True:
            update_fields["notified_available"]   = False
            update_fields["notified_unavailable"] = True
        elif result.in_stock is False:
            update_fields["notified_available"]   = True
            update_fields["notified_unavailable"] = False
        # None (unknown/error): don't change notification flags

    stock_col.update_one(
        {"product_id": result.product_id, "pincode": result.pincode},
        {"$set": update_fields},
        upsert=True,
    )

    if result.error:
        status_str = f"❌ ERROR — {result.error}"
    elif result.in_stock is True:
        status_str = "🟢 IN STOCK"
    elif result.in_stock is False:
        status_str = "🔴 OUT OF STOCK"
    else:
        status_str = "⚠️  UNKNOWN"

    changed_str = " ← CHANGED" if availability_changed else ""
    signal_str  = f" ['{result.matched_signal}']" if result.matched_signal else ""
    prev_str    = f" (was: {prev})" if availability_changed else ""
    log.info(f"{status_str}{changed_str}{signal_str} — {result.name}{prev_str}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info("Amul Stock Scanner")
    log.info("=" * 60)

    products = list(prods_col.find())
    if not products:
        log.warning("No products in MongoDB. Add some to the 'products' collection.")
        return
    log.info(f"Products ({len(products)}):")
    for p in products:
        log.info(f"  • {p['name']}")
        log.info(f"    {p['url']}")

    pincodes = list(set(
        u["pincode"] for u in users_col.find({"pincode": {"$exists": True}})
    ))
    if not pincodes:
        log.warning("No users with pincodes. Register via the bot first.")
        return
    log.info(f"\nPincodes to scan: {pincodes}")
    log.info(f"Total checks: {len(products) * len(pincodes)}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        for pincode in pincodes:
            log.info(f"\n{'=' * 60}")
            log.info(f"Pincode: {pincode}")
            log.info(f"{'=' * 60}")

            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
            )
            page = context.new_page()
            page.on("pageerror", lambda err: log.warning(f"[JS ERROR] {err}"))

            for i, product in enumerate(products, 1):
                log.info(f"\n[{i}/{len(products)}]")
                result = check_stock(page, product, pincode)
                save_stock_result(result)

            context.close()

        browser.close()

    log.info("\n" + "=" * 60)
    log.info("Scan complete.")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
