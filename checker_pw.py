#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, sys, json, traceback
import datetime as dt
from pathlib import Path
from typing import Dict, Tuple, Set, List

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

SCAN_DAYS     = int(os.getenv("SCAN_DAYS", "45"))  # scan further ahead to catch cancellations

TG_TOKEN      = os.getenv("TG_TOKEN")
TG_CHAT_ID    = os.getenv("TG_CHAT_ID", "")
BOT_USERNAME  = os.getenv("BOT_USERNAME", "").lower().lstrip("@")

# --- Notification throttling ---
# Per SLOT (date+time), how many times can we notify in total?
NOTIFY_LIMIT_PER_SLOT       = int(os.getenv("NOTIFY_LIMIT_PER_SLOT", "2"))
# For readability in a single message
MAX_SLOTS_PER_DATE_SHOWN    = int(os.getenv("MAX_SLOTS_PER_DATE_SHOWN", "10"))

# --- Telegram safety: keep under 4096 chars (headroom) ---
MAX_MSG_CHARS               = int(os.getenv("MAX_MSG_CHARS", "3500"))

# Skip historic Telegram backlog on first ever run
FAST_FORWARD_UPDATES = os.getenv("FAST_FORWARD_UPDATES", "true").lower() == "true"

# ----------------- State -----------------
STATE_DIR   = Path("state"); STATE_DIR.mkdir(exist_ok=True)
TARGETS_FP  = STATE_DIR / "targets.json"             # user /want dates
TGSTATE_FP  = STATE_DIR / "tg_last_update.json"      # last telegram update id
SLOTS_FP    = STATE_DIR / "notified_slots.json"      # per-slot (date|time) counts

DEBUG_DIR = Path("debug"); DEBUG_DIR.mkdir(exist_ok=True)

# ----------------- Telegram -----------------
def _post_telegram(payload: dict) -> bool:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json=payload,
            timeout=20,
        )
        if r.status_code != 200:
            txt = ""
            try: txt = r.text
            except Exception: txt = "<no body>"
            print(f"Telegram error {r.status_code}: {txt[:300]}")
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
        payload = {"chat_id": cid, "text": msg, "disable_web_page_preview": True}
        if _post_telegram(payload): ok_any = True
    return ok_any

def tg_send_to_all(msg: str, extra_ids: Set[str]) -> bool:
    ok = tg_send(msg)
    for cid in extra_ids:
        tg_send(msg, cid); ok = True or ok
    return ok

def tg_get_updates(offset: int | None) -> Dict:
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
        requests.get(
            f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
            params={"offset": offset_after, "limit": 1, "allowed_updates": ["message"]},
            timeout=10,
        )
    except Exception:
        pass

# ----------------- JSON helpers -----------------
def _read_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default

def _write_json(path: Path, data) -> bool:
    try:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8"); return True
    except Exception:
        return False

# targets (dates user wants)
def load_targets() -> Set[str]:
    data = _read_json(TARGETS_FP, {"dates": []})
    return set(data.get("dates", []))

def save_targets(dates: Set[str]) -> bool:
    return _write_json(TARGETS_FP, {"dates": sorted(dates)})

# telegram last update id
def load_last_update_id() -> int | None:
    data = _read_json(TGSTATE_FP, {"last_update_id": None})
    return data.get("last_update_id")

def save_last_update_id(val: int | None) -> bool:
    return _write_json(TGSTATE_FP, {"last_update_id": val})

# per-slot notified counts { "YYYY-MM-DD|HH:MM": int }
def load_notified_slots() -> Dict[str, int]:
    data = _read_json(SLOTS_FP, {"counts": {}})
    raw = data.get("counts", {})
    out = {}
    for k, v in raw.items():
        if isinstance(k, str) and re.match(r"^\d{4}-\d{2}-\d{2}\|\d{2}:\d{2}$", k):
            try: out[k] = int(v)
            except Exception: pass
    return out

def save_notified_slots(counts: Dict[str, int]) -> bool:
    return _write_json(SLOTS_FP, {"counts": counts})

# ----------------- Helpers -----------------
TIME_RANGE_RE   = re.compile(r"\b([01]\d|2[0-3]):([0-5]\d)\s*-\s*([01]\d|2[0-3]):([0-5]\d)\b")
DATE_TOKEN_RE   = re.compile(r"\b(\d{4})[-/](\d{2})[-/](\d{2})\b")  # YYYY-MM-DD or YYYY/MM/DD
UNAVAILABLE_RE  = re.compile(r"(available to book from|fully booked|unavailable)", re.I)
BOOK_NAME_RE    = re.compile(r"\b(book now|book|add to basket)\b", re.I)

def normalise_dates(text: str) -> Set[str]:
    out: Set[str] = set()
    for y, m, d in DATE_TOKEN_RE.findall(text or ""):
        try:
            out.add(dt.date(int(y), int(m), int(d)).isoformat())
        except ValueError:
            pass
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
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except PWTimeoutError:
        pass
    deadline = dt.datetime.now() + dt.timedelta(seconds=12)
    while dt.datetime.now() < deadline:
        try:
            body_txt = page.locator("body").inner_text(timeout=1000)
            if TIME_RANGE_RE.search(body_txt): return True
        except Exception:
            pass
        # fast check for any CTA presence
        if page.get_by_role("button", name=BOOK_NAME_RE).count(): return True
        if page.get_by_role("link",   name=BOOK_NAME_RE).count(): return True
        page.wait_for_timeout(400)
    return False

def _button_scoped_time(button) -> str | None:
    """
    Given a 'Book'/'Add to basket' button, find the nearest ancestor container
    that also contains a single time range and return that start time.
    """
    # Try a few ancestor depths to find an element that includes a time range.
    for depth in range(1, 6):
        try:
            cont = button.locator(f"xpath=ancestor::*[{depth}]")
            txt = (cont.inner_text(timeout=600) or "").strip()
        except Exception:
            continue
        if not txt: 
            continue
        # exclude non-bookable states
        if UNAVAILABLE_RE.search(txt):
            return None
        m = TIME_RANGE_RE.search(txt)
        if m:
            return f"{m.group(1)}:{m.group(2)}"
    return None

def _iter_bookable_slots(page, d: dt.date) -> List[Tuple[str, str, str, str, str]]:
    """
    Build slots by iterating over each visible, enabled CTA and extracting the time
    from its own container. This prevents mixing a button from one slot with the time of another.
    """
    slots: List[Tuple[str, str, str, str, str]] = []
    ctas = []
    btns = page.get_by_role("button", name=BOOK_NAME_RE)
    lks  = page.get_by_role("link",   name=BOOK_NAME_RE)
    for loc in (btns, lks):
        cnt = loc.count()
        for i in range(min(cnt, 400)):
            el = loc.nth(i)
            try:
                if not (el.is_visible() and el.is_enabled()):
                    continue
                disabled_attr = el.get_attribute("disabled")
                aria_disabled = el.get_attribute("aria-disabled")
                if disabled_attr is not None or (aria_disabled in ("true", "1")):
                    continue
            except Exception:
                continue
            ctas.append(el)

    # For each CTA, extract the start time from its own ancestor container
    for el in ctas:
        start = _button_scoped_time(el)
        if not start:
            continue
        if not _within_filters(d, start):
            continue
        qs  = _zstamp_for_date(d)
        url = f"{GLAD_BASE}/{ACTIVITY_ID}?activityDate={qs}&previousActivityDate={qs}"
        slots.append((d.isoformat(), d.strftime("%A"), start, "Padel Tennis", url))

    # de-dupe by (date, start)
    uniq, seen = [], set()
    for iso, day, start, act, url in slots:
        k = (iso, start)
        if k in seen: 
            continue
        seen.add(k); uniq.append((iso, day, start, act, url))
    uniq.sort(key=lambda x: (x[0], x[2]))
    return uniq

def collect_slots() -> List[Tuple[str, str, str, str, str]]:
    today = dt.date.today()
    all_slots: List[Tuple[str, str, str, str, str]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context()
        page = ctx.new_page()
        for offset in range(SCAN_DAYS + 1):
            d = today + dt.timedelta(days=offset)
            qs = _zstamp_for_date(d)
            url = f"{GLAD_BASE}/{ACTIVITY_ID}?activityDate={qs}&previousActivityDate={qs}"
            page.goto(url, timeout=60000)
            if not _robust_wait(page):
                continue
            all_slots.extend(_iter_bookable_slots(page, d))
        ctx.close(); browser.close()

    # Already unique by (date, time) inside each day; ensure global uniqueness anyway
    seen, uniq = set(), []
    for s in all_slots:
        k = (s[0], s[2])
        if k not in seen:
            uniq.append(s); seen.add(k)
    uniq.sort(key=lambda x: (x[0], x[2]))
    return uniq

# ----------------- Telegram commands -----------------
def _normalise_command(text: str) -> Tuple[str, str]:
    t = (text or "").strip()
    if not t: return "", ""
    if t.startswith("@"):
        parts = t.split(maxsplit=1); t = parts[1] if len(parts) > 1 else ""
    if not t.startswith("/"): return "", ""
    first, *rest = t.split(maxsplit=1)
    if "@" in first:
        cmd, suffix = first.split("@", 1)
        if BOT_USERNAME and suffix.lower() != BOT_USERNAME:
            return "", ""
        first = cmd
    return first.lower(), (rest[0] if rest else "")

def handle_commands() -> Tuple[Set[str], Set[str]]:
    targets = load_targets()
    last_id = load_last_update_id()

    # First-run fast-forward: skip backlog
    if last_id is None and FAST_FORWARD_UPDATES:
        res0 = tg_get_updates(None)
        if res0.get("ok") and res0.get("result"):
            max_seen = max(u.get("update_id", 0) for u in res0["result"])
            save_last_update_id(max_seen); tg_ack_until(max_seen + 1)
        return targets, set()

    res = tg_get_updates((last_id or 0) + 1)
    updates = res.get("result", []) if res.get("ok") else []
    notify_ids: Set[str] = set()
    if not updates:
        return targets, notify_ids

    max_id = last_id or 0
    DATE_RE = DATE_TOKEN_RE

    def save_and_reply(ok: bool, msg_ok: str, msg_fail: str, cid: str):
        tg_send(msg_ok if ok else msg_fail, cid)

    for upd in updates:
        max_id = max(max_id, upd.get("update_id", 0))
        msg = upd.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        if not text or not chat_id:
            continue
        cmd, tail = _normalise_command(text)
        if not cmd:
            continue
        notify_ids.add(chat_id)

        if cmd in ("/want", "/add"):
            parsed = normalise_dates(tail)
            if parsed:
                targets |= parsed
                ok = save_targets(targets)
                save_and_reply(ok,
                    "Saved target date(s):\n" + "\n".join(sorted(parsed)) +
                    "\n\nWatching:\n" + ("\n".join(sorted(targets)) if targets else "None"),
                    "Could not persist targets to disk.", chat_id)
            else:
                tg_send("I couldn’t read any dates. Use YYYY-MM-DD or YYYY/MM/DD.\nExample: /want 2025-08-29 2025/09/01", chat_id)

        elif cmd == "/clear":
            targets.clear()
            ok = save_targets(targets)
            save_and_reply(ok, "Cleared targets.", "Failed to persist clear to disk.", chat_id)

        elif cmd == "/list":
            tg_send("Watching:\n" + ("\n".join(sorted(targets)) if targets else "No targets set."), chat_id)

        elif cmd in ("/help", "/start"):
            tg_send(
                "Commands:\n"
                "/want YYYY-MM-DD …  (also accepts YYYY/MM/DD)\n"
                "/add YYYY-MM-DD …\n"
                "/list\n"
                "/clear\n"
                "/notified  (show per-slot notification counts)\n"
                "/forget_all  (reset all counts)\n",
                chat_id
            )

        elif cmd in ("/notified", "/seen"):
            counts = load_notified_slots()
            if counts:
                rows = [f"{k.replace('|',' ')} : {v}/{NOTIFY_LIMIT_PER_SLOT}" for k, v in sorted(counts.items())]
                tg_send("Notified counts per slot:\n" + "\n".join(rows[:50]), chat_id)
            else:
                tg_send("No notification counts yet.", chat_id)

        elif cmd in ("/forget_all", "/unnotify_all"):
            ok = save_notified_slots({})
            save_and_reply(ok, "Cleared all per-slot notification counts.", "Failed to reset counts.", chat_id)

    save_last_update_id(max_id); tg_ack_until(max_id + 1); save_targets(targets)
    return targets, notify_ids

# ----------------- Auto-prune past target dates -----------------
def prune_expired_targets(targets: Set[str]) -> Tuple[Set[str], Set[str]]:
    today_iso = dt.date.today().isoformat()
    removed = {d for d in targets if d < today_iso}
    if removed:
        targets = targets - removed; save_targets(targets)
    return targets, removed

# ----------------- Message building (per-date + safe length) -----------------
def build_date_messages(date_iso: str, entries: List[Tuple[str, str, str, str, str]]) -> List[str]:
    day_name = entries[0][1]; total = len(entries)
    header = f"Padel availability — {date_iso} ({day_name})\n"
    lines = []
    shown = 0
    for _, _, time_s, act, url in entries:
        if shown >= MAX_SLOTS_PER_DATE_SHOWN: break
        lines.append(f"• {time_s} — {act}\n  {url}"); shown += 1
    if total > MAX_SLOTS_PER_DATE_SHOWN:
        lines.append(f"…and {total - MAX_SLOTS_PER_DATE_SHOWN} more")

    msgs, cur = [], header
    for line in lines:
        if len(cur) + len(line) + 1 > MAX_MSG_CHARS:
            msgs.append(cur.rstrip()); cur = header + "(continued)\n" + line + "\n"
        else:
            cur += line + "\n"
    if cur.strip(): msgs.append(cur.rstrip())
    return msgs

# ----------------- Main -----------------
def main():
    try:
        targets, notify_ids = handle_commands()
        targets, removed = prune_expired_targets(targets)
        if removed:
            msg = "Removed past target dates:\n" + "\n".join(sorted(removed))
            print(msg); tg_send_to_all(msg, notify_ids)

        slots = collect_slots()
        if targets:
            slots = [s for s in slots if s[0] in targets]

        if not slots:
            print("No matching slots."); return 0

        # Group by date
        by_date: Dict[str, List[Tuple[str, str, str, str, str]]] = {}
        for iso, day, time_s, act, url in slots:
            by_date.setdefault(iso, []).append((iso, day, time_s, act, url))
        for iso in list(by_date.keys()):
            by_date[iso].sort(key=lambda x: x[2])

        # Load per-slot counts and choose eligible slots (< limit)
        counts = load_notified_slots()

        # Build and send per-date messages only for eligible slots
        sent_any = False
        for d in sorted(by_date.keys()):
            entries = by_date[d]
            eligible = []
            for iso, day, time_s, act, url in entries:
                key = f"{iso}|{time_s}"
                if counts.get(key, 0) < NOTIFY_LIMIT_PER_SLOT:
                    eligible.append((iso, day, time_s, act, url))
            if not eligible:
                continue

            # Send (may chunk if long)
            messages = build_date_messages(d, eligible)
            ok = False
            for piece in messages:
                if tg_send_to_all(piece, notify_ids): ok = True

            # Increment counts for the specific slots we just alerted on
            if ok:
                sent_any = True
                for iso, _, time_s, _, _ in eligible:
                    key = f"{iso}|{time_s}"
                    counts[key] = counts.get(key, 0) + 1

        if sent_any:
            save_notified_slots(counts)
            print("Sent notifications.")
        else:
            print("Slots exist but all are beyond the per-slot notification limit; no messages sent.")

        return 0

    except Exception:
        print("Error: unexpected exception in checker_pw.py")
        traceback.print_exc(limit=2)
        return 1

if __name__ == "__main__":
    sys.exit(main())
