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

TIME_RANGE_RE = re.compile(r"\b([01]\d|2[0-3]):([0-5]\d)\s*-\s*([01]\d|2[0-3]):([0-5]\d)\b", re.I)
TIME_HHMM_RE  = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

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
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except PWTimeoutError:
        pass

    _dismiss_cookies(page)

    try:
        head = page.get_by_role("heading")
        head.first.wait_for(timeout=8000)
    except Exception:
        pass

    deadline = dt.datetime.now() + dt.timedelta(seconds=12)
    while dt.datetime.now() < deadline:
        try:
            if page.get_by_role("button", name=re.compile(r"(book|add to basket|reserve|add)", re.I)).count():
                return True
        except Exception:
            pass
        try:
            if page.get_by_text(re.compile(r"this slot is unavailable", re.I)).count():
                return True
        except Exception:
            pass
        try:
            body_txt = page.locator("body").inner_text(timeout=1000)
            if TIME_RANGE_RE.search(body_txt):
                return True
        except Exception:
            pass

        try:
            page.evaluate("window.scrollBy(0, 600)")
        except Exception:
            pass
        page.wait_for_timeout(400)

    return False

def _parse_day_slots(page, day_dt: dt.date):
    """
    From a Gladstone day view, extract available slots with a real button,
    ignore 'This slot is unavailable'. Returns [(weekday, 'HH:MM', 'Padel Tennis'), ...]
    """
    cards = page.locator("div").filter(has_text=re.compile(r"\b\d{2}:\d{2}\s*-\s*\d{2}:\d{2}\b"))
    out = []
    n = min(cards.count(), 400)

    for i in range(n):
        c = cards.nth(i)
        txt = _safe_text(c)
        if not txt:
            continue
        if re.search(r"\bThis slot is unavailable\b", txt, re.I):
            continue

        m = TIME_RANGE_RE.search(txt)
        if not m:
            continue
        start_hm = f"{m.group(1)}:{m.group(2)}"
        if not TIME_HHMM_RE.match(start_hm):
            continue
        if not _within_filters(day_dt, start_hm):
            continue

        has_button = False
        try:
            btns = c.get_by_role("button")
            if btns.count():
                if btns.filter(has_text=re.compile(r"(book|add to basket|reserve|add)", re.I)).count():
                    has_button = True
                else:
                    has_button = True  # icon-only buttons
        except Exception:
            pass

        if not has_button:
            continue

        out.append((day_dt.strftime("%A"), start_hm, "Padel Tennis"))

    return out

def _visit_and_collect_for_date(page, d: dt.date):
    """
    Navigate to the Gladstone calendar URL for date d.
    Return parsed slots; if page didn't load content, retry once.
    """
    qs = _zstamp_for_date(d)
    url = f"{GLAD_BASE}/{ACTIVITY_ID}?activityDate={qs}&previousActivityDate={qs}"

    def _load_once():
        page.goto(url, timeout=60000, wait_until="domcontentloaded")
        ok = _robust_wait_gladstone(page)
        if not ok:
            return []
        return _parse_day_slots(page, d)

    slots = _load_once()
    if not slots:
        try:
            page.reload(timeout=30000, wait_until="domcontentloaded")
        except Exception:
            pass
        ok = _robust_wait_gladstone(page)
        if ok:
            slots = _parse_day_slots(page, d)
    print(f"[debug] {d.isoformat()} -> {len(slots)} slots parsed")
    return slots

def _safe_sort_key(day_order, tup):
    """Return a sort key; if time is malformed, send it to the end safely."""
    dname, hm, act = tup
    try:
        hh = int(hm[:2])
        mm = int(hm[3:5])
    except Exception:
        return (day_order.get(dname, 99), 99, 99, act or "")
    return (day_order.get(dname, 99), hh, mm, act or "")

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
                try:
                    day_slots = _visit_and_collect_for_date(page, d)
                    all_slots.extend(day_slots)
                except Exception as e:
                    print(f"Day {d.isoformat()} fetch failed: {e}")
            if not all_slots:
                _take_debug(page, "no_slots_page")
        finally:
            ctx.close()
            browser.close()

    # Deduplicate and sort (guard against any bad time strings)
    seen = set()
    unique = []
    day_order = { (today + dt.timedelta(days=i)).strftime("%A"): i for i in range(8) }
    for dname, hm, act in all_slots:
        if not (isinstance(hm, str) and TIME_HHMM_RE.match(hm)):
            # Skip malformed times entirely
            continue
        key = (dname, hm, (act or "").lower())
        if key in seen:
            continue
        seen.add(key)
        unique.append((dname, hm, act or ""))

    unique.sort(key=lambda x: _safe_sort_key(day_order, x))
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
