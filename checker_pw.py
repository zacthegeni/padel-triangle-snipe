import os, re, sys, time, datetime as dt
from playwright.sync_api import sync_playwright
import requests

CENTRE_PAGE = "https://www.placesleisure.org/centres/the-triangle/centre-activities/sports/"
LH_BOOKINGS = "https://pfpleisure-pochub.org/LhWeb/en/Public/Bookings"
ACTIVITY_NAME = os.getenv("ACTIVITY_NAME", "Padel")

EARLIEST_HOUR = int(os.getenv("EARLIEST_HOUR", "18"))
LATEST_HOUR   = int(os.getenv("LATEST_HOUR",   "22"))  # exclusive
DAYS_AHEAD    = int(os.getenv("DAYS_AHEAD",    "2"))
WEEKENDS_OK   = os.getenv("WEEKENDS_OK", "true").lower() == "true"
WEEKDAYS_OK   = os.getenv("WEEKDAYS_OK", "true").lower() == "true"

TG_TOKEN   = os.getenv("TG_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

def tg_send(msg: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        print("Telegram not configured")
        return
    for cid in [c.strip() for c in re.split(r"[;,]", TG_CHAT_ID) if c.strip()]:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": cid, "text": msg, "disable_web_page_preview": True},
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
    if is_weekend and not WEEKENDS_OK: return False
    if (not is_weekend) and not WEEKDAYS_OK: return False
    try:
        hh = int(time_str.split(":")[0])
    except Exception:
        return False
    return EARLIEST_HOUR <= hh < LATEST_HOUR

def extract_from_cards(texts):
    slots = []
    for t in texts:
        m_time = re.search(r"\b([01]?\d|2[0-3]):[0-5]\d\b", t)
        m_day  = re.search(r"\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b", t, re.I)
        if m_time:
            day = (m_day.group(0).capitalize() if m_day else "")
            tm  = m_time.group(0)
            if slot_ok(day, tm):
                slots.append((day, tm))
    return sorted(set(slots))


def collect_texts(book_btns, require_padel=False, limit=50):
    """Extract relevant card text for each booking button.

    Args:
        book_btns: A Playwright Locator of the booking buttons.
        require_padel: If True, discard cards that don't mention Padel.
        limit: Max number of booking buttons to inspect.
    """
    texts = []
    for i in range(min(book_btns.count(), limit)):
        btn = book_btns.nth(i)
        parent = btn.locator("xpath=ancestor::*[self::div or self::li][1]")
        try:
            t = parent.inner_text(timeout=1000)
        except Exception:
            t = btn.inner_text()
        if not require_padel or re.search(r"\bpadel\b", t, re.I):
            texts.append(t)
    return texts

def scrape_with_playwright():
    found = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        # Attempt 1: Triangle sports page
        try:
            page.goto(CENTRE_PAGE, timeout=60000)
            for label in ("Accept", "I agree", "Allow all"):
                btn = page.get_by_role("button", name=re.compile(label, re.I))
                if btn.count():
                    btn.first.click(timeout=2000)
                    break
            try:
                page.locator('a[href="#timetable"]').first.click(timeout=2000)
            except Exception:
                pass

            # Try native select
            selected = False
            selects = page.locator("select")
            for i in range(selects.count()):
                sel = selects.nth(i)
                opts = sel.locator("option")
                labels = [opts.nth(j).inner_text().strip() for j in range(opts.count())]
                if any(re.search(r"\bpadel\b", x, re.I) for x in labels):
                    sel.select_option(label=re.compile(r"padel", re.I))
                    selected = True
                    break
            # Fallback custom dropdown
            if not selected:
                try:
                    page.get_by_text(re.compile(r"\bpadel\b", re.I)).first.click(timeout=4000)
                    selected = True
                except Exception:
                    try:
                        page.get_by_role("combobox").first.click(timeout=2000)
                        page.get_by_text(re.compile(r"\bpadel\b", re.I)).first.click(timeout=4000)
                        selected = True
                    except Exception:
                        pass

            page.wait_for_timeout(4000)
            book_btns = page.get_by_role("link", name=re.compile(r"\bbook\b", re.I))
            if book_btns.count() == 0:
                book_btns = page.get_by_role("button", name=re.compile(r"\bbook\b", re.I))

            texts = collect_texts(book_btns)
            found = extract_from_cards(texts)
        except Exception as e:
            print(f"Sports page scrape failed: {e}")

        # Attempt 2: Leisure Hub fallback
        if not found:
            try:
                page.goto(LH_BOOKINGS, timeout=60000)
                for label in ("Accept", "I agree", "Allow all"):
                    btn = page.get_by_role("button", name=re.compile(label, re.I))
                    if btn.count():
                        btn.first.click(timeout=2000)
                        break

                # Try choosing 'The Triangle' site
                combos = page.get_by_role("combobox")
                for i in range(combos.count()):
                    try:
                        combos.nth(i).select_option(label=re.compile(r"\btriangle\b", re.I))
                        break
                    except Exception:
                        pass

                # Search for Padel
                try:
                    tb = page.get_by_role("textbox").first
                    tb.fill("Padel")
                    page.keyboard.press("Enter")
                except Exception:
                    pass

                page.wait_for_timeout(4000)
                book_btns = page.get_by_role("link", name=re.compile(r"\bbook\b", re.I))
                if book_btns.count() == 0:
                    book_btns = page.get_by_role("button", name=re.compile(r"\bbook\b", re.I))

                texts = collect_texts(book_btns, require_padel=True)
                found = extract_from_cards(texts)
            except Exception as e:
                print(f"LhWeb scrape failed: {e}")

        ctx.close()
        browser.close()
    return found

def main():
    slots = scrape_with_playwright()
    if not slots:
        print("No matching slots.")
        return 0
    lines = [f"{d or '(day tbc)'} {t} — Padel at The Triangle" for (d, t) in slots]
    msg = "🎾 Padel — slots found:\n\n" + "\n".join(lines)
    print(msg)
    tg_send(msg)
    return 0

if __name__ == "__main__":
    sys.exit(main())
