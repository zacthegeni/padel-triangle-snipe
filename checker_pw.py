import os, re, sys, json, traceback
import datetime as dt
from pathlib import Path
from typing import List, Tuple, Dict

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
import requests

# ----------------- Config -----------------
GLAD_BASE     = "https://placesleisure.gladstonego.cloud/book/calendar"
ACTIVITY_ID   = os.getenv("ACTIVITY_ID", "149A001015").strip()  # Padel @ Triangle
START_HOUR_Z  = int(os.getenv("START_HOUR_Z", "5"))

EARLIEST_HOUR = int(os.getenv("EARLIEST_HOUR", "0"))
LATEST_HOUR   = int(os.getenv("LATEST_HOUR",   "24"))
WEEKENDS_OK   = os.getenv("WEEKENDS_OK", "true").lower() == "true"
WEEKDAYS_OK   = os.getenv("WEEKDAYS_OK", "true").lower() == "true"

MAX_DAYS                = int(os.getenv("MAX_DAYS", "60"))
STOP_AFTER_EMPTY_STREAK = int(os.getenv("STOP_AFTER_EMPTY_STREAK", "7"))

TG_TOKEN   = os.getenv("TG_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

STATE_DIR   = Path("state")
STATE_DIR.mkdir(exist_ok=True)
TARGETS_FP  = STATE_DIR / "targets.json"
TGSTATE_FP  = STATE_DIR / "tg_last_update.json"

DEBUG_DIR = Path("debug")
DEBUG_DIR.mkdir(exist_ok=True)

# ----------------- Telegram -----------------
def tg_send(msg: str, chat_ids: str | None = None):
    if not TG_TOKEN:
        return
    ids = (chat_ids or TG_CHAT_ID or "").strip()
    if not ids:
        return
    for cid in [c.strip() for c in re.split(r"[;,]", ids) if c.strip()]:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={
                    "chat_id": cid,
                    "text": msg,
                    "disable_web_page_preview": False,
                    "parse_mode": "Markdown",
                },
                timeout=20,
            )
        except Exception as e:
            print(f"Telegram send failed for {cid}: {e}")

def tg_get_updates(offset: int | None) -> Dict:
    if not TG_TOKEN:
        return {"ok": False, "result": []}
    params = {}
    if offset is not None:
        params["offset"] = offset
    try:
        r = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates", params=params, timeout=20)
        return r.json()
    except Exception:
        return {"ok": False, "result": []}

# ----------------- State -----------------
def _read_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default

def _write_json(path: Path, data):
    try:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False

def load_targets() -> set[str]:
    data = _read_json(TARGETS_FP, {"dates": []})
    return set(data.get("dates", []))

def save_targets(dates: set[str]):
    _write_json(TARGETS_FP, {"dates": sorted(dates)})

def load_last_update_id() -> int | None:
    data = _read_json(TGSTATE_FP, {"last_update_id": None})
    return data.get("last_update_id")

def save_last_update_id(val: int | None):
    _write_json(TGSTATE_FP, {"last_update_id": val})

# ----------------- Helpers -----------------
TIME_RANGE_RE = re.compile(r"\b([01]\d|2[0-3]):([0-5]\d)\s*-\s*([01]\d|2[0-3]):([0-5]\d)\b")

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

def _robust_wait(page):
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except PWTimeoutError:
        pass
    deadline = dt.datetime.now() + dt.timedelta(seconds=12)
    while dt.datetime.now() < deadline:
        if page.get_by_role("button", name=re.compile(r"(book|add to basket)", re.I)).count():
            return True
        if page.get_by_text(re.compile(r"this slot is unavailable", re.I)).count():
            return True
        try:
            txt = page.locator("body").inner_text(timeout=1000)
            if TIME_RANGE_RE.search(txt):
                return True
        except Exception:
            pass
        page.wait_for_timeout(400)
    return False

def _parse_day(page, d: dt.date):
    slots = []
    cards = page.locator("div").filter(has_text=re.compile(r"\d{2}:\d{2}\s*-\s*\d{2}:\d{2}"))
    for i in range(min(cards.count(), 400)):
        c = cards.nth(i)
        txt = (c.inner_text(timeout=500) or "").strip()
        if not txt or "unavailable" in txt.lower():
            continue
        m = TIME_RANGE_RE.search(txt)
        if not m:
            continue
        start = f"{m.group(1)}:{m.group(2)}"
        if not _within_filters(d, start):
            continue
        if not c.get_by_role("button").count():
            continue
        qs = _zstamp_for_date(d)
        url = f"{GLAD_BASE}/{ACTIVITY_ID}?activityDate={qs}&previousActivityDate={qs}"
        slots.append((d.isoformat(), d.strftime("%A"), start, "Padel Tennis", url))
    return slots

def collect_slots():
    today = dt.date.today()
    all_slots = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context()
        page = ctx.new_page()
        empty = 0
        for offset in range(MAX_DAYS):
            d = today + dt.timedelta(days=offset)
            qs = _zstamp_for_date(d)
            url = f"{GLAD_BASE}/{ACTIVITY_ID}?activityDate={qs}&previousActivityDate={qs}"
            page.goto(url, timeout=60000)
            if not _robust_wait(page):
                empty += 1
                if empty >= STOP_AFTER_EMPTY_STREAK:
                    break
                continue
            day_slots = _parse_day(page, d)
            if day_slots:
                all_slots.extend(day_slots)
                empty = 0
            else:
                empty += 1
                if empty >= STOP_AFTER_EMPTY_STREAK:
                    break
        ctx.close()
        browser.close()
    # dedupe
    seen, uniq = set(), []
    for s in all_slots:
        k = (s[0], s[2])
        if k not in seen:
            uniq.append(s)
            seen.add(k)
    return uniq

# ----------------- Telegram commands -----------------
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

def handle_commands():
    targets = load_targets()
    last_id = load_last_update_id()
    res = tg_get_updates(last_id + 1 if last_id else None)
    notify_ids = set()
    if not res.get("ok"):
        return targets, notify_ids
    max_id = last_id or 0
    for upd in res.get("result", []):
        max_id = max(max_id, upd.get("update_id", 0))
        msg = upd.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if not text:
            continue
        notify_ids.add(chat_id)
        cmd = text.split()[0].lower()
        if cmd in ("/want", "/add"):
            dates = set(DATE_RE.findall(text))
            if dates:
                targets |= dates
                tg_send("✅ Added:\n" + "\n".join(sorted(dates)), chat_id)
            else:
                tg_send("Usage: /want YYYY-MM-DD [YYYY-MM-DD ...]", chat_id)
        elif cmd == "/clear":
            targets.clear()
            tg_send("🧹 Cleared targets.", chat_id)
        elif cmd == "/list":
            if targets:
                tg_send("🎯 Watching:\n" + "\n".join(sorted(targets)), chat_id)
            else:
                tg_send("No targets set. Use /want YYYY-MM-DD.", chat_id)
        elif cmd in ("/help", "/start"):
            tg_send("Commands:\n/want YYYY-MM-DD …\n/add YYYY-MM-DD …\n/list\n/clear", chat_id)
    save_last_update_id(max_id)
    save_targets(targets)
    return targets, notify_ids

# ----------------- Main -----------------
def main():
    targets, notify_ids = handle_commands()
    slots = collect_slots()
    if targets:
        slots = [s for s in slots if s[0] in targets]
    if not slots:
        print("No matching slots.")
        return 0
    lines = []
    for iso, day, time, act, url in slots:
        lines.append(f"{iso} ({day}) {time} — {act}\n🔗 {url}")
    msg = "🎾 *Padel slots found:*\n\n" + "\n\n".join(lines)
    print(msg)
    tg_send(msg)
    for cid in notify_ids:
        tg_send(msg, cid)
    return 0

if __name__ == "__main__":
    sys.exit(main())
