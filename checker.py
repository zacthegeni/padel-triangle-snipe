import os, sys, re, time, datetime as dt
import requests
from bs4 import BeautifulSoup

# ====== CONFIG ======
CENTRE_URL = "https://www.placesleisure.org/centres/the-triangle/centre-activities/sports/"
KEYWORD = "Padel"  # what we search for on the timetable/results
# Filter examples: set to None to disable
EARLIEST_HOUR = int(os.getenv("EARLIEST_HOUR", "18"))  # 24h clock
LATEST_HOUR   = int(os.getenv("LATEST_HOUR", "22"))
DAYS_AHEAD    = int(os.getenv("DAYS_AHEAD", "2"))      # today + next N days
# Telegram
TG_TOKEN   = os.getenv("TG_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")

def tg_send(msg):
    if not TG_TOKEN or not TG_CHAT_ID:
        print("Telegram not configured")
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TG_CHAT_ID, "text": msg, "disable_web_page_preview": True})

def normalise_spaces(s): return re.sub(r"\s+", " ", s or "").strip()

def fetch_timetable_html():
    # Load the sports page; the “Book via timetable” link lives here.
    r = requests.get(CENTRE_URL, timeout=30)
    r.raise_for_status()
    return r.text

def extract_timetable_link(html):
    soup = BeautifulSoup(html, "html.parser")
    # Find the Padel section then its "Book via timetable" link
    padel_header = None
    for h in soup.find_all(["h2","h3","h4"]):
        if normalise_spaces(h.get_text()).lower() == "padel":
            padel_header = h
            break
    if not padel_header:
        return None
    link = None
    for a in padel_header.find_all_next("a", href=True, limit=10):
        if "timetable" in a.get_text(strip=True).lower() or "book" in a.get_text(strip=True).lower():
            link = a["href"]
            break
    return link

def list_slots(timetable_html):
    soup = BeautifulSoup(timetable_html, "html.parser")
    # The timetable renders sessions with text blocks and “Book” buttons.
    # Grab any visible “Book” buttons and read the surrounding time/day.
    slots = []
    for btn in soup.find_all("a", string=re.compile(r"book", re.I)):
        # Walk up to a card/row element to pull context (date/time/activity)
        parent_text = normalise_spaces(btn.find_parent().get_text(" "))
        # Try to find a HH:MM pattern and the day label
        m_time = re.search(r"\b([01]?\d|2[0-3]):[0-5]\d\b", parent_text)
        m_day  = re.search(r"\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b", parent_text, re.I)
        # Also sanity-check activity contains “Padel”
        is_padel = re.search(r"padel", parent_text, re.I) is not None
        if m_time and is_padel:
            slots.append({
                "time": m_time.group(0),
                "day":  m_day.group(0) if m_day else "",
                "text": parent_text[:300],
                "href": btn.get("href")
            })
    return slots

def slot_ok(slot):
    # Day filter: within today..(today + DAYS_AHEAD)
    today = dt.date.today()
    valid_days = [ (today + dt.timedelta(days=i)).strftime("%A") for i in range(DAYS_AHEAD+1) ]
    if slot["day"] and slot["day"] not in valid_days:
        return False
    # Time filter
    hh = int(slot["time"].split(":")[0])
    return EARLIEST_HOUR <= hh < LATEST_HOUR

def main():
    base_html = fetch_timetable_html()
    link = extract_timetable_link(base_html)
    if not link:
        print("Couldn’t find the Padel timetable link; site layout may have changed.")
        return 0
    # Follow the timetable link
    if link.startswith("/"):
        link = "https://www.placesleisure.org" + link
    r = requests.get(link, timeout=30)
    r.raise_for_status()
    slots = [s for s in list_slots(r.text) if slot_ok(s)]
    if not slots:
        print("No matching slots found.")
        return 0
    # De-dupe & notify
    lines = []
    for s in slots:
        url = s["href"]
        if url and url.startswith("/"):
            url = "https://www.placesleisure.org" + url
        lines.append(f"{s['day']} {s['time']} — Padel\n{url or '(open timetable and select Padel)'}")
    msg = "🎾 Padel at The Triangle — slots found:\n\n" + "\n\n".join(lines)
    print(msg)
    tg_send(msg)
    return 0

if __name__ == "__main__":
    sys.exit(main())
