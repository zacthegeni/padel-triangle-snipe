import os, re, sys, traceback
import datetime as dt
from pathlib import Path

from playwright.sync_api import sync_playwright
import requests

# ----------------- Config (env-driven) -----------------
GLAD_BASE     = "https://placesleisure.gladstonego.cloud/book/calendar"
# Padel Tennis @ The Triangle Activity ID (stable as of now)
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
    # Build YYYY-MM-DDT{START_HOUR_Z}:00:00.000Z
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
        return (el.inner_text(timeout=800) or "").strip()
    except Exception:
        try:
            return (el.text_content(timeout=800) or "").strip()
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

def _parse_day_slots(page, day_dt: dt.date):
    """
    We are on a Gladstone 'calendar/{ACTIVITY_ID}?activityDate=...&previousActivityDate=...' page.
    Extract visible cards with a real booking button and without 'This slot is unavailable'.
    Returns list of (weekday, HH:MM, 'Padel Tennis').
    """
    # Cards typically contain a time range plus a button if available.
    cards = page.locator("div").filter(has_text=re.compile(r"\b\d{2}:\d{2}\s*-\s*\d{2}:\d{2}\b"))
    out = []

    n = min(cards.count(), 300)
    for i in range(n):
        c = cards.nth(i)
        txt = _safe_text(c)
        if not txt:
            continue

        # Skip explicit unavailable
        if re.search(r"\bThis slot is unavailable\b", txt, re.I):
            continue

        m = TIME_RANGE_RE.search(txt)
        if not m:
            continue
        start_hm = m.group(1)

        if not _within_filters(day_dt, start_hm):
            continue

        # Must have a button (book/add etc.)
        has_button = False
        try:
            btns = c.get_by_role("button")
            if btns.count():
                if btns.filter(has_text=re.compile(r"(book|add to basket|reserve|add)", re.I)).count():
                    has_button = True
                else:
                    # Some skins use icon-only buttons
                    has_button = True
        except Exception:
            pass

        if not has_button:
            continue

        out.append((day_dt.strftime("%A"), start_hm, "Padel Tennis"))

    return out

def _collect_slots():
    today = dt.date.today()
    all_slots = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 2200})
        page = ctx.new_page()
        try:
            for offset in range(0, DAYS_AHEAD + 1):
                d = today + dt.timedelta(days=offset)
                qs = _zstamp_for_date(d)
                url = f"{GLAD_BASE}/{ACTIVITY_ID}?activityDate={qs}&previousActivityDate={qs}"
                page.goto(url, timeout=60000, wait_until="domcontentloaded")

                # Try to dismiss any cookie banners that might block clicks
                for label in ("Accept", "I agree", "Allow all", "Accept all", "OK"):
                    try:
                        b = page.get_by_role("button", name=re.compile(label, re.I))
                        if b.count():
                            b.first.click(timeout=1500)
                            break
                    except Exception:
                        pass

                slots = _parse_day_slots(page, d)
                all_slots.extend(slots)

            if not all_slots:
                _take_debug(page, "no_slots_page")

        finally:
            ctx.close()
            browser.close()

    # Deduplicate and sort
    seen = set()
    unique = []
    day_order = { (today + dt.timedelta(days=i)).strftime("%A"): i for i in range(8) }
    for dname, hm, act in all_slots:
        key = (dname, hm, act.lower())
        if key in seen:
            continue
        seen.add(key)
        unique.append((dname, hm, act))
    unique.sort(key=lambda x: (day_order.get(x[0], 99), int(x[1][:2]), int(x[1][3:5]), x[2]))
    return unique

# ----------------- Main -----------------
def main():
    try:
        slots = _collect_slots()
        if not slots:
            print("No matching slots.")
            return 0

        lines = [f"{d} {t} — Padel at The Triangle" for (d, t, _) in slots]
        msg = "🎾 *Padel — slots found:*\n\n" + "\n".join(lines)
        print(msg)
        tg_send(msg)
        return 0
    except Exception:
        print("Error: unexpected exception in checker_pw.py")
        traceback.print_exc(limit=2)
        return 1

if __name__ == "__main__":
    sys.exit(main())
