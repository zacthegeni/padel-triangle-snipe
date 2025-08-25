import os, re, sys, datetime as dt
import requests
from bs4 import BeautifulSoup

# ====== CONFIG (set via GitHub "Secrets" and "Variables") ======
CENTRE_URL   = "https://www.placesleisure.org/centres/the-triangle/centre-activities/sports/"
KEYWORD      = os.getenv("KEYWORD", "Padel")
EARLIEST_HH  = int(os.getenv("EARLIEST_HOUR", "18"))  # 24h clock (inclusive)
LATEST_HH    = int(os.getenv("LATEST_HOUR",   "22"))  # 24h clock (exclusive)
DAYS_AHEAD   = int(os.getenv("DAYS_AHEAD",    "2"))   # today + next N days
WEEKENDS_OK  = os.getenv("WEEKENDS_OK", "true").lower() == "true"
WEEKDAYS_OK  = os.getenv("WEEKDAYS_OK", "true").lower() == "true"

TG_TOKEN     = os.getenv("TG_TOKEN")
TG_CHAT_IDS  = os.getenv("TG_CHAT_ID", "")  # one or many IDs, comma/semicolon separated

def normalise(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()

def fetch(url: str) -> str:
    r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.text

def extract_padel_timetable_link(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    headings = soup.find_all(["h2", "h3", "h4"])
    target = next((h for h in headings if normalise(h.get_text()).lower() == "padel"), None)
    if not target:
        return None
    for a in target.find_all_next("a", href=True, limit=20):
        txt = normalise(a.get_text()).lower()
        if "timetable" in txt or "book" in txt:
            href = a["href"]
            if href.startswith("/"):
                href = "https://www.placesleisure.org" + href
            return href
    return None

def parse_slots(timetable_html: str):
    soup = BeautifulSoup(timetable_html, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        if re.search(r"\bbook\b", a.get_text(strip=True), re.I):
            block = a.find_parent()
            text = normalise(block.get_text(" ")) if block else normalise(a.get_text(" "))
            if not re.search(KEYWORD, text, re.I):
                continue
            m_time = re.search(r"\b([01]?\d|2[0-3]):[0-5]\d\b", text)
            m_day  = re.search(r"\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b", text, re.I)
            if not m_time:
                continue
            href = a["href"]
            if href.startswith("/"):
                href = "https://www.placesleisure.org" + href
            out.append({
                "time": m_time.group(0),
                "day":  (m_day.group(0).capitalize() if m_day else ""),
                "href": href,
                "raw":  text[:300]
            })
    return out

def slot_allowed(slot) -> bool:
    today = dt.date.today()
    valid_days = [(today + dt.timedelta(days=i)).strftime("%A") for i in range(DAYS_AHEAD + 1)]
    if slot["day"] and slot["day"] not in valid_days:
        return False
    day = slot["day"] or today.strftime("%A")
    is_weekend = day in ("Saturday", "Sunday")
    if is_weekend and not WEEKENDS_OK: return False
    if (not is_weekend) and not WEEKDAYS_OK: return False
    hh = int(slot["time"].split(":")[0])
    return EARLIEST_HH <= hh < LATEST_HH

def tg_send(text: str):
    # Accept one or many chat IDs (comma or semicolon separated)
    raw = TG_CHAT_IDS or ""
    ids = []
    for chunk in raw.replace(";", ",").split(","):
        cid = chunk.strip()
        if cid:
            ids.append(cid)
    if not TG_TOKEN or not ids:
        print("Telegram not configured (missing TG_TOKEN or TG_CHAT_ID).")
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    for cid in ids:
        try:
            requests.post(url, json={
                "chat_id": cid,
                "text": text,
                "disable_web_page_preview": True
            }, timeout=20)
        except Exception as e:
            print(f"Telegram send failed for {cid}: {e}")

def main():
    try:
        base = fetch(CENTRE_URL)
    except Exception as e:
        print(f"Fetch centre page failed: {e}")
        return 0
    link = extract_padel_timetable_link(base)
    if not link:
        print("Couldn’t locate the Padel timetable link (layout may have changed).")
        return 0
    try:
        thtml = fetch(link)
    except Exception as e:
        print(f"Fetch timetable failed: {e}")
        return 0
    slots = [s for s in parse_slots(thtml) if slot_allowed(s)]
    if not slots:
        print("No matching slots.")
        return 0
    lines = []
    for s in slots:
        lines.append(f"{s['day'] or '(day tbc)'} {s['time']} — Padel\n{s['href']}")
    msg = "🎾 Padel at The Triangle — slots found:\n\n" + "\n\n".join(lines)
    print(msg)
    tg_send(msg)
    return 0

if __name__ == "__main__":
    sys.exit(main())
