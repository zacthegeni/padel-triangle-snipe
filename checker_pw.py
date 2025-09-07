#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, sys, sqlite3, traceback
import datetime as dt
from pathlib import Path
from typing import Dict, Tuple, Set, List

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
import requests

# ---------- Config ----------
GLAD_BASE   = "https://placesleisure.gladstonego.cloud/book/calendar"
ACTIVITY_ID = os.getenv("ACTIVITY_ID", "149A001015").strip()
START_HOUR_Z = int(os.getenv("START_HOUR_Z", "5"))

EARLIEST_HOUR = int(os.getenv("EARLIEST_HOUR", "0"))
LATEST_HOUR   = int(os.getenv("LATEST_HOUR", "24"))
WEEKENDS_OK   = os.getenv("WEEKENDS_OK", "true").lower() == "true"
WEEKDAYS_OK   = os.getenv("WEEKDAYS_OK", "true").lower() == "true"
SCAN_DAYS     = int(os.getenv("SCAN_DAYS", "45"))

TG_TOKEN    = os.getenv("TG_TOKEN")
TG_CHAT_ID  = os.getenv("TG_CHAT_ID", "")
BOT_USERNAME = os.getenv("BOT_USERNAME", "").lower().lstrip("@")

# Throttling per SLOT (date+time)
NOTIFY_LIMIT_PER_SLOT = int(os.getenv("NOTIFY_LIMIT_PER_SLOT", "2"))
MAX_SLOTS_PER_DATE_SHOWN = int(os.getenv("MAX_SLOTS_PER_DATE_SHOWN", "8"))
MAX_MSG_CHARS = int(os.getenv("MAX_MSG_CHARS", "3500"))
FAST_FORWARD_UPDATES = os.getenv("FAST_FORWARD_UPDATES", "true").lower() == "true"

# ---------- State (SQLite) ----------
STATE_DIR = Path("state"); STATE_DIR.mkdir(exist_ok=True)
DB_FP = STATE_DIR / "slots.sqlite"
TARGETS_FP = STATE_DIR / "targets.txt"

def _db():
    con = sqlite3.connect(DB_FP)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("""
        CREATE TABLE IF NOT EXISTS slot_counts(
          slot_key TEXT PRIMARY KEY,
          count INTEGER NOT NULL DEFAULT 0,
          last_sent_utc TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS kv (
          k TEXT PRIMARY KEY,
          v TEXT
        )
    """)
    con.commit()
    return con

def db_get_count(con, key:str) -> int:
    row = con.execute("SELECT count FROM slot_counts WHERE slot_key=?", (key,)).fetchone()
    return int(row[0]) if row else 0

def db_inc_count(con, key:str):
    now = dt.datetime.utcnow().isoformat(timespec="seconds")+"Z"
    con.execute("""
        INSERT INTO slot_counts(slot_key,count,last_sent_utc)
        VALUES(?,1,?)
        ON CONFLICT(slot_key) DO UPDATE SET count=count+1,last_sent_utc=excluded.last_sent_utc
    """, (key, now))
    con.commit()

def kv_get(con, k:str, default:str|None=None) -> str|None:
    row = con.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return (row[0] if row else default)

def kv_set(con, k:str, v:str):
    con.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
    con.commit()

def load_targets() -> Set[str]:
    if TARGETS_FP.exists():
        return {ln.strip() for ln in TARGETS_FP.read_text(encoding="utf-8").splitlines() if ln.strip()}
    return set()

def save_targets(dates: Set[str]):
    TARGETS_FP.write_text("\n".join(sorted(dates))+"\n", encoding="utf-8")

# ---------- Telegram ----------
def _post_telegram(payload: dict) -> bool:
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json=payload, timeout=20)
        if r.status_code != 200:
            print(f"Telegram error {r.status_code}: {r.text[:300]}")
            return False
        return True
    except Exception as e:
        print(f"Telegram send exception: {e}")
        return False

def tg_send(msg: str, chat_ids: str | None = None) -> bool:
    if not TG_TOKEN: return False
    ids = (chat_ids or TG_CHAT_ID or "").strip()
    if not ids: return False
    ok_any = False
    for cid in [c.strip() for c in re.split(r"[;,]", ids) if c.strip()]:
        if _post_telegram({"chat_id": cid, "text": msg, "disable_web_page_preview": True}):
            ok_any = True
    return ok_any

def tg_get_updates(offset: int | None):
    if not TG_TOKEN: return {"ok": False, "result": []}
    params = {"limit": 100, "allowed_updates": ["message"]}
    if offset is not None: params["offset"] = offset
    try:
        r = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates", params=params, timeout=20)
        return r.json()
    except Exception:
        return {"ok": False, "result": []}

def tg_ack_until(offset_after: int):
    try:
        requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                     params={"offset": offset_after, "limit": 1, "allowed_updates": ["message"]}, timeout=10)
    except Exception:
        pass

# ---------- Helpers ----------
TIME_RANGE_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\s*-\s*([01]?\d|2[0-3]):([0-5]\d)\b")
TIME_HHMM_RE  = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
DATE_TOKEN_RE = re.compile(r"\b(\d{4})[-/](\d{2})[-/](\d{2})\b")
UNAVAILABLE_RE= re.compile(r"(available to book from|fully booked|unavailable)", re.I)
BOOK_NAME_RE  = re.compile(r"\b(book now|book|add to basket)\b", re.I)

def normalise_dates(text: str) -> Set[str]:
    out: Set[str] = set()
    for y,m,d in DATE_TOKEN_RE.findall(text or ""):
        try: out.add(dt.date(int(y),int(m),int(d)).isoformat())
        except ValueError: pass
    return out

def _zstamp_for_date(d: dt.date) -> str:
    return f"{d.isoformat()}T{START_HOUR_Z:02d}:00:00.000Z"

def _within_filters(day_dt: dt.date, start_hhmm: str) -> bool:
    try: hh = int(start_hhmm.split(":")[0])
    except Exception: return False
    if not (EARLIEST_HOUR <= hh < LATEST_HOUR): return False
    is_weekend = day_dt.weekday() >= 5
    if is_weekend and not WEEKENDS_OK: return False
    if (not is_weekend) and not WEEKDAYS_OK: return False
    return True

def _robust_wait(page):
    try: page.wait_for_load_state("networkidle", timeout=20000)
    except PWTimeoutError: pass
    deadline = dt.datetime.now() + dt.timedelta(seconds=12)
    while dt.datetime.now() < deadline:
        try:
            body_txt = page.locator("body").inner_text(timeout=1000)
            if TIME_RANGE_RE.search(body_txt): return True
        except Exception: pass
        if page.get_by_role("button", name=BOOK_NAME_RE).count(): return True
        if page.get_by_role("link",   name=BOOK_NAME_RE).count(): return True
        page.wait_for_timeout(400)
    return False

def _button_scoped_time(button):
    for depth in range(1,6):
        try:
            cont = button.locator(f"xpath=ancestor::*[{depth}]")
            txt = (cont.inner_text(timeout=600) or "").strip()
        except Exception:
            continue
        if not txt or UNAVAILABLE_RE.search(txt): continue
        m = TIME_RANGE_RE.search(txt) or TIME_HHMM_RE.search(txt)
        if m:
            hh = m.group(1).zfill(2); mm = m.group(2)
            return f"{hh}:{mm}"
    return None

def _iter_bookable_slots(page, d: dt.date):
    slots = []
    ctas = []
    for loc in (page.get_by_role("button", name=BOOK_NAME_RE),
                page.get_by_role("link",   name=BOOK_NAME_RE)):
        for i in range(min(loc.count(), 400)):
            el = loc.nth(i)
            try:
                if not (el.is_visible() and el.is_enabled()): continue
                if el.get_attribute("disabled") is not None: continue
                if (el.get_attribute("aria-disabled") or "").lower() in ("true","1"): continue
            except Exception:
                continue
            ctas.append(el)

    seen = set()
    for el in ctas:
        start = _button_scoped_time(el)
        if not start: continue
        if not _within_filters(d, start): continue
        if start in seen: continue
        qs = _zstamp_for_date(d)
        url = f"{GLAD_BASE}/{ACTIVITY_ID}?activityDate={qs}&previousActivityDate={qs}"
        slots.append((d.isoformat(), d.strftime("%A"), start, "Padel Tennis", url))
        seen.add(start)

    slots.sort(key=lambda x:(x[0],x[2]))
    return slots

def collect_slots():
    today = dt.date.today()
    all_slots = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(); page = ctx.new_page()
        for offset in range(SCAN_DAYS+1):
            d = today + dt.timedelta(days=offset)
            url = f"{GLAD_BASE}/{ACTIVITY_ID}?activityDate={_zstamp_for_date(d)}&previousActivityDate={_zstamp_for_date(d)}"
            page.goto(url, timeout=60000)
            if not _robust_wait(page): continue
            all_slots.extend(_iter_bookable_slots(page, d))
        ctx.close(); browser.close()
    # global de-dupe by (date,time)
    seen, uniq = set(), []
    for s in all_slots:
        k = (s[0],s[2])
        if k not in seen:
            uniq.append(s); seen.add(k)
    uniq.sort(key=lambda x:(x[0],x[2]))
    return uniq

# ---------- Telegram commands ----------
def _normalise_command(text: str) -> Tuple[str, str]:
    t = (text or "").strip()
    if not t: return "", ""
    if t.startswith("@"):
        parts = t.split(maxsplit=1); t = parts[1] if len(parts)>1 else ""
    if not t.startswith("/"): return "", ""
    first,*rest = t.split(maxsplit=1)
    if "@" in first:
        cmd,sfx = first.split("@",1)
        if BOT_USERNAME and sfx.lower()!=BOT_USERNAME: return "",""
        first = cmd
    return first.lower(), (rest[0] if rest else "")

def handle_commands(con) -> Tuple[Set[str], Set[str]]:
    targets = load_targets()
    last_id_s = kv_get(con, "tg_last_update_id", None)
    last_id = int(last_id_s) if (last_id_s and last_id_s.isdigit()) else None

    if last_id is None and FAST_FORWARD_UPDATES:
        res0 = tg_get_updates(None)
        if res0.get("ok") and res0.get("result"):
            max_seen = max(u.get("update_id",0) for u in res0["result"])
            kv_set(con,"tg_last_update_id",str(max_seen))
            tg_ack_until(max_seen+1)
        return targets,set()

    res = tg_get_updates((last_id or 0)+1)
    updates = res.get("result",[]) if res.get("ok") else []
    notify_ids:set[str]=set()
    if not updates: return targets,notify_ids

    max_id = last_id or 0
    for upd in updates:
        max_id = max(max_id, upd.get("update_id",0))
        msg = upd.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id",""))
        if not text or not chat_id: continue
        cmd, tail = _normalise_command(text)
        if not cmd: continue
        notify_ids.add(chat_id)

        if cmd in ("/want","/add"):
            parsed = normalise_dates(tail)
            if parsed:
                targets |= parsed
                save_targets(targets)
                tg_send("Saved target date(s):\n" + "\n".join(sorted(parsed)) +
                        "\n\nWatching:\n" + ("\n".join(sorted(targets)) if targets else "None"), chat_id)
            else:
                tg_send("I couldn’t read any dates. Use YYYY-MM-DD or YYYY/MM/DD.", chat_id)

        elif cmd=="/clear":
            targets.clear(); save_targets(targets); tg_send("Cleared targets.", chat_id)

        elif cmd=="/list":
            tg_send("Watching:\n" + ("\n".join(sorted(targets)) if targets else "No targets set."), chat_id)

        elif cmd in ("/help","/start"):
            tg_send("Commands:\n/want YYYY-MM-DD …\n/add YYYY-MM-DD …\n/list\n/clear\n(Notifications limited to 2 per slot.)", chat_id)

    kv_set(con,"tg_last_update_id",str(max_id))
    tg_ack_until(max_id+1)
    save_targets(targets)
    return targets, notify_ids

# ---------- Message building (display +1h) ----------
def build_date_messages(date_iso: str, entries: List[Tuple[str,str,str,str,str]]) -> List[str]:
    day = entries[0][1]; total = len(entries)
    header = f"Padel availability — {date_iso} ({day})\n"
    lines, shown = [], 0
    for _,_,time_s,act,url in entries:
        if shown >= MAX_SLOTS_PER_DATE_SHOWN: break
        # +1 hour for display only
        try:
            hh, mm = map(int, time_s.split(":"))
            hh = (hh + 1) % 24
            display_time = f"{hh:02d}:{mm:02d}"
        except Exception:
            display_time = time_s
        lines.append(f"• {display_time} — {act}\n  {url}")
        shown += 1
    if total > MAX_SLOTS_PER_DATE_SHOWN:
        lines.append(f"…and {total - MAX_SLOTS_PER_DATE_SHOWN} more")
    msgs, cur = [], header
    for line in lines:
        if len(cur)+len(line)+1 > MAX_MSG_CHARS:
            msgs.append(cur.rstrip()); cur = header+"(continued)\n"+line+"\n"
        else:
            cur += line+"\n"
    if cur.strip(): msgs.append(cur.rstrip())
    return msgs

# ---------- Main ----------
def main():
    try:
        con = _db()

        targets, notify_ids = handle_commands(con)

        # prune past targets
        today_iso = dt.date.today().isoformat()
        tset = {d for d in targets if d >= today_iso}
        if tset != targets:
            save_targets(tset)
            if targets - tset:
                tg_send("Removed past target dates:\n" + "\n".join(sorted(targets - tset)))

        slots = collect_slots()
        if tset:
            slots = [s for s in slots if s[0] in tset]
        if not slots:
            print("No matching slots."); return 0

        # group by date
        by_date: Dict[str, List[Tuple[str,str,str,str,str]]] = {}
        for iso,day,time_s,act,url in slots:
            by_date.setdefault(iso, []).append((iso,day,time_s,act,url))
        for iso in list(by_date.keys()):
            by_date[iso].sort(key=lambda x:x[2])

        # filter by per-slot cap via SQLite
        eligible_by_date: Dict[str, List[Tuple[str,str,str,str,str]]] = {}
        for d, items in by_date.items():
            elig=[]
            for iso,day,time_s,act,url in items:
                key = f"{iso}|{time_s}"
                if db_get_count(con, key) < NOTIFY_LIMIT_PER_SLOT:
                    elig.append((iso,day,time_s,act,url))
            if elig:
                eligible_by_date[d]=elig

        if not eligible_by_date:
            print("Slots exist but all are beyond the per-slot limit; no messages sent.")
            return 0

        # send & increment
        for d in sorted(eligible_by_date.keys()):
            for piece in build_date_messages(d, eligible_by_date[d]):
                tg_send(piece);  [tg_send(piece, cid) for cid in notify_ids]
            for iso,_,time_s,_,_ in eligible_by_date[d]:
                db_inc_count(con, f"{iso}|{time_s}")

        print("Sent notifications.")
        return 0

    except Exception:
        print("Error: unexpected exception in checker_pw.py")
        traceback.print_exc(limit=2)
        return 1

if __name__ == "__main__":
    sys.exit(main())