import os, re, sys, traceback
import datetime as dt
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
import requests

# ---- Config ----
CENTRE_PAGE  = "https://www.placesleisure.org/centres/the-triangle/centre-activities/sports/"
LH_BOOKINGS  = "https://pfpleisure-pochub.org/LhWeb/en/Public/Bookings"
ACTIVITY_NAME = os.getenv("ACTIVITY_NAME", "Padel").strip()

EARLIEST_HOUR = int(os.getenv("EARLIEST_HOUR", "18"))
LATEST_HOUR   = int(os.getenv("LATEST_HOUR",   "22"))  # exclusive
DAYS_AHEAD    = int(os.getenv("DAYS_AHEAD",    "2"))
WEEKENDS_OK   = os.getenv("WEEKENDS_OK", "true").lower() == "true"
WEEKDAYS_OK   = os.getenv("WEEKDAYS_OK", "true").lower() == "true"

TG_TOKEN   = os.getenv("TG_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

DEBUG_DIR = Path("debug")
DEBUG_DIR.mkdir(exist_ok=True)

# ---- Helpers ----
def tg_send(msg: str):
    """Send a Telegram message to one or more chat IDs (comma/semicolon separated)."""
    if not TG_TOKEN or not TG_CHAT_ID:
        print("Telegram not configured (missing TG_TOKEN or TG_CHAT_ID).")
        return
    for cid in [c.strip() for c in re.split(r"[;,]", TG_CHAT_ID) if c.strip()]:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={
                    "chat_id": cid,
                    "text": msg,
                    "disable_web_page_preview": True,
                    "parse_mode": "Markdown"
                },
                timeout=20
            )
        except Exception as e:
            print(f"Telegram send failed for {cid}: {e}")

def slot_ok(day_str: str, time_str: str) -> bool:
    today = dt.date.today()
    valid_days = [(today + dt.timedelta(days=i)).strftime("%A") for i in range(DAYS_AHEAD + 1)]
    day_str = (day_str or "").strip()
    if day_str and day_str not in valid_days:
        return False
    check_day = day_str or today.strftime("%A")
    is_weekend = check_day in ("Saturday", "Sunday")
    if is_weekend and not WEEKENDS_OK:
        return False
    if (not is_weekend) and not WEEKDAYS_OK:
        return False
    try:
        hh = int(time_str.split(":")[0])
    except Exception:
        return False
    return EARLIEST_HOUR <= hh < LATEST_HOUR

def extract_from_cards(texts):
    """Pull (Day, HH:MM) from card text near 'Book' buttons."""
    slots = []
    day_re = re.compile(r"\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b", re.I)
    time_re = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")
    for t in texts:
        m_time = time_re.search(t)
        m_day  = day_re.search(t)
        if m_time:
            day = (m_day.group(0).capitalize() if m_day else "")
            tm  = m_time.group(0)
            if slot_ok(day, tm):
                slots.append((day, tm))
    # Deduplicate + sort by (day order, time)
    today = dt.date.today()
    day_to_ord = { (today + dt.timedelta(days=i)).strftime("%A"): i for i in range(8) }
    def key_fn(x):
        d, tm = x
        ordv = day_to_ord.get(d or today.strftime("%A"), 0)
        hh, mm = map(int, tm.split(":"))
        return (ordv, hh, mm)
    return sorted(set(slots), key=key_fn)

def _dismiss_cookies(page):
    for label in ("Accept", "I agree", "Allow all", "Accept all"):
        try:
            btn = page.get_by_role("button", name=re.compile(label, re.I))
            if btn.count():
                btn.first.click(timeout=2000)
                return
        except PWTimeoutError:
            pass
        except Exception:
            pass

def _safe_text(ne):
    try:
        return ne.inner_text(timeout=1000)
    except Exception:
        try:
            return ne.text_content(timeout=1000) or ""
        except Exception:
            return ""

def _take_debug(page, name="screenshot"):
    try:
        p = DEBUG_DIR / f"{name}.png"
        page.screenshot(path=str(p), full_page=True)
        print(f"Saved debug screenshot: {p}")
    except Exception:
        pass

def _select_activity(page, label_regex):
    """Try several patterns to select the activity by visible text."""
    # Try <select>
    try:
        selects = page.locator("select")
        for i in range(selects.count()):
            sel = selects.nth(i)
            opts = sel.locator("option")
            opt_labels = []
            for j in range(min(200, opts.count())):
                try:
                    opt_labels.append(opts.nth(j).inner_text().strip())
                except Exception:
                    pass
            if any(re.search(label_regex, x) for x in opt_labels):
                sel.select_option(label=re.compile(label_regex, re.I))
                return True
    except Exception:
        pass

    # Try clicking a “combobox” then the item
    try:
        combo = page.get_by_role("combobox").first
        combo.click(timeout=2000)
        page.get_by_text(re.compile(label_regex, re.I)).first.click(timeout=3000)
        return True
    except Exception:
        pass

    # Try clicking any element with the text directly (custom dropdowns)
    try:
        page.get_by_text(re.compile(label_regex, re.I)).first.click(timeout=3000)
        return True
    except Exception:
        pass
    return False

def _collect_book_card_text(page, include_only_activity_regex=None, limit=100):
    """Find 'Book' buttons/links and return parent card texts (lightly normalised)."""
    book_btns = page.get_by_role("link", name=re.compile(r"\bbook\b", re.I))
    if book_btns.count() == 0:
        book_btns = page.get_by_role("button", name=re.compile(r"\bbook\b", re.I))

    texts = []
    n = min(book_btns.count(), limit)
    for i in range(n):
        btn = book_btns.nth(i)
        # get nearest reasonable ancestor
        parent = btn.locator("xpath=ancestor::*[self::article or self::li or self::div][1]")
        t = _safe_text(parent) or _safe_text(btn)
        t = re.sub(r"\s+", " ", t).strip()
        if include_only_activity_regex and not re.search(include_only_activity_regex, t, re.I):
            continue
        texts.append(t)
    return texts

# ---- Scrapers ----
def scrape_triangle_sports(page) -> list[tuple[str, str]]:
    """Attempt 1: The Triangle “Sports” page."""
    page.goto(CENTRE_PAGE, timeout=60000, wait_until="domcontentloaded")
    _dismiss_cookies(page)

    # If a “Timetable” tab exists, click it. (Avoid building a new '#timetable' URL.)
    try:
        timetable = page.get_by_role("link", name=re.compile("timetable", re.I))
        if timetable.count():
            timetable.first.click(timeout=3000)
    except Exception:
        pass

    # Try to select the activity
    selected = _select_activity(page, label_regex=rf"\b{re.escape(ACTIVITY_NAME)}\b")

    # Give a moment for dynamic content to populate
    page.wait_for_timeout(4000)

    texts = _collect_book_card_text(page, include_only_activity_regex=rf"\b{re.escape(ACTIVITY_NAME)}\b")
    if not texts and selected:
        # One more small wait if selection seemed to work
        page.wait_for_timeout(2000)
        texts = _collect_book_card_text(page, include_only_activity_regex=rf"\b{re.escape(ACTIVITY_NAME)}\b")

    return extract_from_cards(texts)

def scrape_leisurehub(page) -> list[tuple[str, str]]:
    """Attempt 2: Leisure Hub public bookings."""
    page.goto(LH_BOOKINGS, timeout=60000, wait_until="domcontentloaded")
    _dismiss_cookies(page)

    # Try choosing site “The Triangle”
    try:
        # Some LH instances put a site selector as a combobox or select
        if _select_activity(page, label_regex=r"\bThe\s+Triangle\b"):
            page.wait_for_timeout(1500)
    except Exception:
        pass

    # Search for the activity by typing in a search/text box if present
    try:
        tb = page.get_by_role("textbox").first
        tb.fill(ACTIVITY_NAME)
        page.keyboard.press("Enter")
        page.wait_for_timeout(3000)
    except Exception:
        # If no textbox, try clicking activity directly
        _select_activity(page, label_regex=rf"\b{re.escape(ACTIVITY_NAME)}\b")

    texts = _collect_book_card_text(page, include_only_activity_regex=rf"\b{re.escape(ACTIVITY_NAME)}\b")
    return extract_from_cards(texts)

def scrape_with_playwright():
    found = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1280, "height": 2000})
        page = ctx.new_page()
        try:
            # Attempt 1
            try:
                found = scrape_triangle_sports(page)
            except Exception as e:
                print(f"Sports page scrape failed: {e}")
                _take_debug(page, "sports_error")

            # Attempt 2 (fallback)
            if not found:
                try:
                    found = scrape_leisurehub(page)
                except Exception as e:
                    print(f"LH scrape failed: {e}")
                    _take_debug(page, "lh_error")

            if not found:
                _take_debug(page, "no_slots_page")

        finally:
            ctx.close()
            browser.close()
    return found

# ---- Entry point ----
def main():
    try:
        slots = scrape_with_playwright()
        if not slots:
            print("No matching slots.")
            return 0

        lines = [f"{(d or '(day tbc)')} {t} — {ACTIVITY_NAME} at The Triangle" for (d, t) in slots]
        msg = "🎾 *Padel — slots found:*\n\n" + "\n".join(lines)
        print(msg)
        tg_send(msg)
        return 0
    except Exception:
        # Hard failure (keep secrets out of logs)
        print("Error: unexpected exception in checker_pw.py")
        traceback.print_exc(limit=2)
        return 1

if __name__ == "__main__":
    sys.exit(main())
