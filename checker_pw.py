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

SCAN_DAYS     = int(os.getenv("SCAN_DAYS", "45"))  # today + N days (wider to catch cancellations)

TG_TOKEN      = os.getenv("TG_TOKEN")
TG_CHAT_ID    = os.getenv("TG_CHAT_ID", "")
BOT_USERNAME  = os.getenv("BOT_USERNAME", "").lower().lstrip("@")

# --- Notification throttling ---
NOTIFY_LIMIT_PER_DATE       = int(os.getenv("NOTIFY_LIMIT_PER_DATE", "2"))   # send at most N messages per ISO date
MAX_SLOTS_PER_DATE_SHOWN    = int(os.getenv("MAX_SLOTS_PER_DATE_SHOWN", "8"))  # cap lines shown per date (message length hygiene)

# Anti-dup behaviour: on first ever run, fast-forward to the latest update id silently
FAST_FORWARD_UPDATES = os.getenv("FAST_FORWARD_UPDATES", "true").lower() == "true"

# ----------------- State -----------------
STATE_DIR   = Path("state"); STATE_DIR.mkdir(exist_ok=True)
TARGETS_FP  = STATE_DIR / "targets.json"           # user /want dates
TGSTATE_FP  = STATE_DIR / "tg_last_update.json"    # last telegram update id
COUNTS_FP   = STATE_DIR / "notified_counts.json"   # per-date notification counts

DEBUG_DIR = Path("debug"); DEBUG_DIR.mkdir(exist_ok=True)

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
                json={"chat_id": cid, "text": msg, "disable_web_page_preview": False, "parse_mode": "Markdown"},
                timeout=20,
            )
        except Exception as e:
            print(f"Telegram send failed for {cid}: {e}")

def tg_get_updates(offset: int | None) -> Dict:
    if not TG_TOKEN:
        return {"ok": False, "result": []}
    params = {"limit": 100, "allowed_updates": ["message"]}
    if offset is not None:
        params["offset"] = offset
    try:
        r = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates", params=params, timeout=20)
        return r.json()
    except Exception:
        return {"ok": False, "result": []}

def tg_ack_until(offset_after: int):
    """Tell Telegram we've consumed up to update_id == offset_after-1, even if our state file didn't persist."""
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

def _write_json(path: Path, data):
    try:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False

# targets (dates user wants)
def load_targets() -> Set[str]:
    data = _read_json(TARGETS_FP, {"dates": []})
    return set(data.get("dates", []))

def save_targets(dates: Set[str]):
    _write_json(TARGETS_FP, {"dates": sorted(dates)})

# telegram last update id
def load_last_update_id() -> int | None:
    data = _read_json(TGSTATE_FP, {"last_update_id": None})
    return data.get("last_update_id")

def save_last_update_id(val: int | None):
    _write_json(TGSTATE_FP, {"last_update_id": val})

# notified counts per date
def load_notified_counts() -> Dict[str, int]:
    data = _read_json(COUNTS_FP, {"counts": {}})
    raw = data.get("counts", {})
    return {str(k): int(v) for k, v in raw.items() if re.match(r"\d{4}-\d{2}-\d{2}", str(k))}

def save_notified_counts(counts: Dict[str, int]):
    _write_json(COUNTS_FP, {"counts": counts})

# ----------------- Helpers -----------------
TIME_RANGE_RE = re.compile(r"\b([01]\d|2[0-3]):([0-5]\d)\s*-\s*([01]\d|2[0-3]):([0-5]\d)\b")
DATE_RE       = re.compile(r"\d{4}-\d{2}-\d{2}")

def _zstamp_for_date(d: dt.date) -> str:
    return f"{d.isoformat()}T{START_HOUR_Z:02d}:00:00.000Z"

def _within_filters(day_dt: dt.date, start_hhmm: str) -> bool:
    try:
        hh = int(start_hhmm.split(":")[0])
    except Exception:
        return False
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
        if page.get_by_role("button", name=re.compile(r"(book|add to basket)", re.I)).count(): return True
        if page.get_by_text(re.compile(r"this slot is unavailable", re.I)).count(): return True
        try:
            txt = page.locator("body").inner_text(timeout=1000)
            if TIME_RANGE_RE.search(txt): return True
        except Exception:
            pass
        page.wait_for_timeout(400)
    return False

def _parse_day(page, d: dt.date):
    slots = []
    cards = page.locator("div").filter(has_text=re.compile(r"\d{2}:\d{2}\s*-\s*\d{2}:\d{2}"))
    n = min(cards.count(), 500)
    for i in range(n):
        c = cards.nth(i)
        try:
            txt = (c.inner_text(timeout=700) or "").strip()
        except Exception:
            continue
        if not txt or "unavailable" in txt.lower():
            continue
        m = TIME_RANGE_RE.search(txt)
        if not m:
            continue
        start = f"{m.group(1)}:{m.group(2)}"
        if not _within_filters(d, start):
            continue
        # Require at least one button (book/add)
        if not c.get_by_role("button").count():
            continue
        qs = _zstamp_for_date(d)
        url = f"{GLAD_BASE}/{ACTIVITY_ID}?activityDate={qs}&previousActivityDate={qs}"
        slots.append((d.isoformat(), d.strftime("%A"), start, "Padel Tennis", url))
    return slots

def collect_slots() -> List[Tuple[str, str, str, str, str]]:
    today = dt.date.today()
    all_slots: List[Tuple[str, str, str, str, str]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context()
        page = ctx.new_page()
        for offset in range(SCAN_DAYS + 1):  # today + N days
            d = today + dt.timedelta(days=offset)
            qs = _zstamp_for_date(d)
            url = f"{GLAD_BASE}/{ACTIVITY_ID}?activityDate={qs}&previousActivityDate={qs}"
            page.goto(url, timeout=60000)
            if not _robust_wait(page):
                continue
            all_slots.extend(_parse_day(page, d))
        ctx.close()
        browser.close()
    # De-dupe by (date, start-time)
    seen, uniq = set(), []
    for s in all_slots:
        k = (s[0], s[2])
        if k not in seen:
            uniq.append(s)
            seen.add(k)
    uniq.sort(key=lambda x: (x[0], x[2]))
    return uniq

# ----------------- Telegram commands -----------------
def _normalise_command(text: str) -> Tuple[str, str]:
    t = (text or "").strip()
    if not t: return "", ""
    if t.startswith("@"):
        parts = t.split(maxsplit=1)
        t = parts[1] if len(parts) > 1 else ""
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

    # First-run fast-forward: skip historic backlog
    if last_id is None and FAST_FORWARD_UPDATES:
        res0 = tg_get_updates(None)
        if res0.get("ok") and res0.get("result"):
            max_seen = max(u.get("update_id", 0) for u in res0["result"])
            save_last_update_id(max_seen)
            tg_ack_until(max_seen + 1)
        return targets, set()

    res = tg_get_updates((last_id or 0) + 1)
    updates = res.get("result", []) if res.get("ok") else []
    notify_ids: Set[str] = set()
    if not updates:
        return targets, notify_ids

    max_id = last_id or 0
    for upd in updates:
        max_id = max(max_id, upd.get("update_id", 0))
        msg = upd.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if not text or not chat_id:
            continue
        cmd, tail = _normalise_command(text)
        if not cmd:
            continue
        notify_ids.add(chat_id)

        if cmd in ("/want", "/add"):
            dates = set(DATE_RE.findall(tail))
            if dates:
                targets |= dates
                save_targets(targets)
                tg_send("✅ Added:\n" + "\n".join(sorted(dates)), chat_id)
            else:
                tg_send("Usage: /want YYYY-MM-DD [YYYY-MM-DD ...]", chat_id)

        elif cmd == "/clear":
            targets.clear()
            save_targets(targets)
            tg_send("🧹 Cleared targets.", chat_id)

        elif cmd == "/list":
            tg_send("🎯 Watching:\n" + ("\n".join(sorted(targets)) if targets else "No targets set."), chat_id)

        elif cmd in ("/help", "/start"):
            tg_send(
                "Commands:\n"
                "/want YYYY-MM-DD …\n"
                "/add YYYY-MM-DD …\n"
                "/list\n"
                "/clear\n"
                "/notified  (show per-date notification counts)\n"
                "/forget_all  (reset all counts)\n",
                chat_id
            )

        elif cmd in ("/notified", "/seen"):
            nd = load_notified_counts()
            if nd:
                rows = [f"{d}: {nd.get(d,0)}/{NOTIFY_LIMIT_PER_DATE}" for d in sorted(nd)]
                tg_send("🛎️ Notified counts per date:\n" + "\n".join(rows), chat_id)
            else:
                tg_send("🛎️ No notification counts yet.", chat_id)

        elif cmd in ("/forget_all", "/unnotify_all"):
            save_notified_counts({})
            tg_send("🧹 Cleared all per-date notification counts.", chat_id)

    save_last_update_id(max_id)
    tg_ack_until(max_id + 1)
    save_targets(targets)
    return targets, notify_ids

# ----------------- Auto-prune past target dates -----------------
def prune_expired_targets(targets: Set[str]) -> Tuple[Set[str], Set[str]]:
    today_iso = dt.date.today().isoformat()
    removed = {d for d in targets if d < today_iso}
    if removed:
        targets = targets - removed
        save_targets(targets)
    return targets, removed

# ----------------- Main -----------------
def main():
    try:
        targets, notify_ids = handle_commands()
        targets, removed = prune_expired_targets(targets)
        if removed:
            msg = "🗑️ Removed past target dates:\n" + "\n".join(sorted(removed))
            print(msg)
            tg_send(msg)
            for cid in notify_ids:
                tg_send(msg, cid)

        slots = collect_slots()
        if targets:
            slots = [s for s in slots if s[0] in targets]

        if not slots:
            print("No matching slots.")
            return 0

        # Group slots by ISO date
        by_date: Dict[str, List[Tuple[str, str, str, str, str]]] = {}
        for iso, day, time_s, act, url in slots:
            by_date.setdefault(iso, []).append((iso, day, time_s, act, url))
        for iso in list(by_date.keys()):
            by_date[iso].sort(key=lambda x: x[2])  # sort by time

        # Load per-date notification counts and decide eligibility
        counts = load_notified_counts()
        eligible_dates = [d for d in sorted(by_date.keys())
                          if counts.get(d, 0) < NOTIFY_LIMIT_PER_DATE]

        if not eligible_dates:
            print("Slots found, but all dates have reached the notification limit. No new notifications.")
            return 0

        # Build one compact message covering all eligible dates (to prevent spamming)
        lines = []
        for d in eligible_dates:
            day_name = by_date[d][0][1]
            total = len(by_date[d])
            header = f"📅 {d} — {day_name} ({total} slot{'s' if total != 1 else ''})"
            lines.append(header)
            shown = 0
            for iso, day, time_s, act, url in by_date[d]:
                if shown >= MAX_SLOTS_PER_DATE_SHOWN:
                    break
                lines.append(f"• {time_s} — {act}\n  🔗 {url}")
                shown += 1
            if total > MAX_SLOTS_PER_DATE_SHOWN:
                lines.append(f"…and {total - MAX_SLOTS_PER_DATE_SHOWN} more")

        msg = "🎾 *Padel availability:*\n\n" + "\n".join(lines)
        print(msg)
        tg_send(msg)
        for cid in notify_ids:
            tg_send(msg, cid)

        # Increment per-date counts (we sent one message that covered these dates)
        for d in eligible_dates:
            counts[d] = counts.get(d, 0) + 1
        save_notified_counts(counts)

        return 0

    except Exception:
        print("Error: unexpected exception in checker_pw.py")
        traceback.print_exc(limit=2)
        return 1

if __name__ == "__main__":
    sys.exit(main())
