import os, re, sys, traceback
import datetime as dt
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
import requests

# ----------------- Config (env-driven) -----------------
GLAD_BASE     = "https://placesleisure.gladstonego.cloud/book/calendar"
# Padel Tennis @ The Triangle Activity ID
ACTIVITY_ID   = os.getenv("ACTIVITY_ID", "149A001015").strip()

# The Gladstone URL wants a UTC timestamp like 05:00Z that maps to local morning.
START_HOUR_Z  = int(os.getenv("START_HOUR_Z", "5"))  # 0..23 (UTC hour placed in the query string)

# Filters
EARLIEST_HOUR = int(os.getenv("EARLIEST_HOUR", "0"))   # local hour, inclusive
LATEST_HOUR   = int(os.getenv("LATEST_HOUR",   "24"))  # local hour, exclusive
DAYS_AHEAD    = int(os.getenv("DAYS_AHEAD",    "7"))
WEEKENDS_OK   = os.getenv("WEEKENDS_OK", "true").lower() == "true"
WEEKDAYS_OK   = os.getenv("WEEKDAYS_OK", "true").lower() == "true"

# Messaging
TG_TOKEN      = os.getenv("TG_TOKEN")
TG_CHAT_ID    = os.getenv("TG_CHAT_ID", "")

# Debug
DEBUG_DIR = Path("debug")
DEBUG_DIR.mkdir(exist_ok=True)

# ----------------- Telegram -----------------
def tg_send(msg: str):
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
                    "parse_mode": "Markdown",
                },
                timeout=20,
            )
        except Exception as e:
            print(f"Telegram send failed for {cid}: {e}")

# ----------------- Helpers -----------------
def _zstamp_for_date(d: dt.date) -> str:
    return f"{d.isoformat()}T{START_HOUR_Z:02d}:00:00.000Z"

def _within_filters(day_dt: dt.date, start_hhmm: str) -> bool:
    try:
        hh = int(start_hhmm.split(":")[0])
    except Exception:
        return False
    if not (EARLIEST_HOUR <= hh < LATEST_HOUR):
        return False
    is_weekend = day_dt.weekday() >= 5
    if is_weekend and not WEEKENDS_OK:
        return False
    if (not is_weekend) and not WEEKDAYS_OK:
        return False
    return True

def _safe_text(el) -> str:
    try:
        return (el.inner_text(timeout=1000) or "").strip()
    except Exception:
        try:
            return (el.text_content(timeout=1000) or "").strip()
        except Exception:
            return ""

def _take_debug(page, name="shot"):
    try:
        p = DEBUG_DIR / f"{name}.png"
        page.screenshot(path=str(p), full_page=True)
        print(f"Saved debug screenshot: {p}")
    except Exception:
        pass

TIME_RANGE_RE = re.compile(r"\b([01]\d|2[0-3]):[0-5]\d\s*-\s*([01]\d|2[0-3]):[0-5]\d\b", re.I)

def _dismiss_cookies(page):
    for label in ("Accept", "I agree", "Allow all", "Accept all", "OK"):
        try:
            b = page.get_by_role("button", name=re.compile(label, re.I))
            if b.count():
                b.first.click(timeout=1500)
                break
        except Exception:
            pass

def _robust_wait_gladstone(page):
    """
    Wait for Gladstone SPA to finish loading something meaningful.
    Success = any of:
     - heading present (e.g., 'Padel Tennis')
     - a booking button appears
     - an 'unavailable' badge appears
     - any time-range text like '06:00 - 07:00' is in DOM
    """
    # Wait for network to settle
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except PWTimeoutError:
        pass

    _dismiss_cookies(page)

    # Try to wait for a heading (often 'Padel Tennis')
    try:
        head = page.get_by_role("heading")
        head.first.wait_for(timeout=8000)
    except Exception:
        pass

    # Poll for content signals up to ~12s
    deadline = dt.datetime.now() + dt.timedelta(seconds=12)
    while dt.datetime.now() < deadline:
        try:
            # any book-ish button?
            if page.get_by_role("button", name=re.compile(r"(book|add to basket|reserve|add)", re.I)).count():
                return True
        except Exception:
            pass
        try:
            # any 'unavailable' badge?
            if page.get_by_text(re.compile(r"this slot is unavailable", re.I)).count():
                return True
        except Exception:
            pass
        # any time range present in the whole page text?
        try:
            body_txt = page.locator("body").inner_text(timeout=1000)
            if TIME_RANGE_RE.search(body_txt):
                return True
        except Exception:
            pass

        # nudge: small scroll to trigger lazy load
        try:
            page.evaluate("window.scrollBy(0, 600)")
        except Exception:
            pass
        page.wait_for_timeout(400)

    return False

def _parse_day_slots(page, day_dt: dt.date):
    cards = page.locator("div").filter(has_text=re.compile(r"\b\d{2}:\d{2}_
