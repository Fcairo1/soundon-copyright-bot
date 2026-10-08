#!/usr/bin/env python3
"""
copyright_alert/claim_window.py

The claim workflow behind the ACR dashboard's "Waiting on managers" and
"Always take down" areas (acrcloud-dashboard: claim_window_data.py,
always_td_data.py, no_claim_data.py).

  1. Claim window. When the ACR digest posts for a scan cycle, register that
     cycle in the "Claim Windows" tab of the Takedown Stream Tracker workbook.
     Managers then have 5 WORKDAYS (Mon-Fri, Brazil time, same counter the
     claims reminders use) to tick "Do NOT claim" on the dashboard.
  2. Day-5 alarm. The first daily run after the deadline sends each region's
     product ops owner one message: what is still open to claim, per label
     manager, plus any always-take-down cases not requested yet. Once per
     cycle and region (retries only the regions that failed).
  3. Monday reminder. While a window is open, every Monday the digest card is
     re-posted in the group with fresh numbers, tagging only managers who
     still have unmarked cases.
  4. Always-take-down accounts. The source is a plain Lark sheet of SoundOn
     user IDs; this mirrors it daily into the "Always TD Accounts" tab, which
     the dashboard reads (its Lark app already has access to that workbook).

A case is always-take-down only when its SO User ID is on the list AND an
analyst marked it "Other Party" — an account's own releases also show up in
the scan and must never be taken down.

Run by hand:  python3 -m copyright_alert.claim_window --dry-run
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Set

from copyright_alert import acr_digest as ad
from copyright_alert.metadata_notice import _run_lark_sheets
from copyright_alert.tag_managers import _add_workdays, _net_workdays

ALWAYS_TD_SOURCE_URL = "https://bytedance.sg.larkoffice.com/sheets/OVWtsBkishOl3PtZ2tulJVkggVe"
ALWAYS_TD_SOURCE_SHEET_ID = "EWBmcG"
WORKBOOK_URL = ad.TAKEDOWN_STREAMS_SHEET_URL
WINDOWS_TAB = "Claim Windows"
ACCOUNTS_TAB = "Always TD Accounts"
WINDOW_COLUMNS = ["cycle", "posted_date", "deadline_date", "alarmed_at", "registered_at"]
WINDOW_WORKDAYS = 5
REGIONS = ad.REGION_ORDER
USER_ID_HEADER = "SO User ID"
OPERATOR_HEADER = "operation_manager_list"
BRT = timezone(timedelta(hours=-3))


def today_brt() -> date:
    return datetime.now(timezone.utc).astimezone(BRT).date()


def _now_brt_str() -> str:
    return datetime.now(timezone.utc).astimezone(BRT).strftime("%Y-%m-%d %H:%M")


# ── Lark sheet helpers (lark-cli, same path metadata_notice.py uses) ────────

def _tab_id(workbook_url: str, name: str) -> Optional[str]:
    for t in ad.list_tabs(workbook_url):
        if t["name"].strip() == name:
            return t["sheet_id"]
    return None


def _read_grid(workbook_url: str, sheet_id: str, a1_range: Optional[str] = None) -> List[List[str]]:
    args = ["+csv-get", "--url", workbook_url, "--sheet-id", sheet_id, "--include-row-prefix=false"]
    if a1_range:
        args += ["--range", a1_range]
    text = ((_run_lark_sheets(args).get("data") or {}).get("annotated_csv")) or ""
    return [[c.strip() for c in row] for row in csv.reader(io.StringIO(text))]


def _digits(value) -> str:
    return re.sub(r"\D", "", str(value or ""))


# ── Claim windows ───────────────────────────────────────────────────────────

def read_windows() -> List[Dict[str, object]]:
    sheet_id = _tab_id(WORKBOOK_URL, WINDOWS_TAB)
    if not sheet_id:
        return []
    grid = _read_grid(WORKBOOK_URL, sheet_id)
    out = []
    for offset, row in enumerate(grid[1:], start=2):
        if not row or not row[0]:
            continue
        w = {c: (row[i] if i < len(row) else "") for i, c in enumerate(WINDOW_COLUMNS)}
        w["_row"] = offset
        out.append(w)
    return out


def register_window(cycle: str, posted: Optional[date] = None, *, dry_run: bool = False) -> Dict[str, object]:
    """Start the 5-workday window for a scan cycle. Idempotent per cycle name."""
    cycle = str(cycle or "").strip()
    if not cycle:
        return {"registered": False, "reason": "no cycle name"}
    posted = posted or today_brt()
    deadline = _add_workdays(posted, WINDOW_WORKDAYS)
    if any(str(w["cycle"]).strip().lower() == cycle.lower() for w in read_windows()):
        return {"registered": False, "reason": "already registered", "cycle": cycle}
    result = {"registered": not dry_run, "cycle": cycle, "posted": posted.isoformat(), "deadline": deadline.isoformat()}
    if dry_run:
        result["would_register"] = True
        return result
    row = [cycle, posted.isoformat(), deadline.isoformat(), "", _now_brt_str()]
    payload = {"sheets": [{
        "name": WINDOWS_TAB, "mode": "append", "header": False, "columns": WINDOW_COLUMNS,
        "data": [row], "dtypes": {c: "object" for c in WINDOW_COLUMNS},
    }]}
    _run_lark_sheets(["+table-put", "--url", WORKBOOK_URL, "--sheets", "-"], input_text=json.dumps(payload))
    return result


def _set_alarmed(window: Dict[str, object], text: str) -> None:
    sheet_id = _tab_id(WORKBOOK_URL, WINDOWS_TAB)
    _run_lark_sheets(["+csv-put", "--url", WORKBOOK_URL, "--sheet-id", sheet_id,
                      "--start-cell", f"D{window['_row']}", "--csv", text + "\n"])


def _alarmed_regions(text: str) -> Set[str]:
    """alarmed_at holds "DONE <ts> <regions>" or "PARTIAL <regions>"."""
    parts = str(text or "").replace(",", " ").split()
    return {p for p in parts if p in REGIONS}


# ── Always-take-down accounts (source sheet -> dashboard mirror) ────────────

def parse_account_ids(grid: Iterable[List[str]]) -> List[str]:
    """User IDs are 19 digits. Anything in scientific notation (a numeric cell
    can turn 7110648495548269570 into 7.11E+18) or too short to be an ID is
    dropped rather than guessed at."""
    ids: List[str] = []
    for row in grid:
        cell = row[0] if row else ""
        if not cell or "E" in cell.upper():
            continue
        d = _digits(cell)
        if len(d) >= 15 and d not in ids:
            ids.append(d)
    return ids


def sync_always_td_accounts(*, dry_run: bool = False) -> Dict[str, object]:
    source = parse_account_ids(_read_grid(ALWAYS_TD_SOURCE_URL, ALWAYS_TD_SOURCE_SHEET_ID, "A1:A500"))
    if not source:
        return {"synced": False, "reason": "source list empty or unreadable — mirror left as is"}
    sheet_id = _tab_id(WORKBOOK_URL, ACCOUNTS_TAB)
    if not sheet_id:
        return {"synced": False, "reason": f'"{ACCOUNTS_TAB}" tab missing'}
    current = parse_account_ids(_read_grid(WORKBOOK_URL, sheet_id, "A2:A500"))
    if set(current) == set(source):
        return {"synced": False, "reason": "already up to date", "accounts": len(source)}
    summary = {"synced": not dry_run, "accounts": len(source), "added": len(set(source) - set(current)),
               "removed": len(set(current) - set(source))}
    if dry_run:
        return summary
    # Pad with quoted-empty rows so IDs that left the list don't linger below.
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\n")
    for i in source:
        writer.writerow([i])
    for _ in range(max(0, len(current) - len(source))):
        writer.writerow([""])
    _run_lark_sheets(["+csv-put", "--url", WORKBOOK_URL, "--sheet-id", sheet_id, "--start-cell", "A2", "--csv", buf.getvalue()])
    return summary


# ── The day-5 alarm ─────────────────────────────────────────────────────────

def _key_sets():
    """(do-not-claim keys, requested keys) — requested = already has a row in
    the Takedown Stream Tracker, keyed so_song_id|matched_isrc like the dashboard."""
    no_claim = ad.read_do_not_claim_keys()
    requested: Set[str] = set()
    try:
        grid = ad.read_tab(WORKBOOK_URL, ad.TAKEDOWN_STREAMS_SHEET_ID)
        if grid:
            idx = ad._header_index(grid[0])
            s_i, m_i = idx.get("so_song_id"), idx.get("matched_isrc")
            for row in grid[1:]:
                sid, mi = ad._cell(row, s_i), ad._cell(row, m_i)
                if sid or mi:
                    requested.add(f"{sid}|{mi}")
    except Exception as exc:
        print(f"  ⚠ claim alarm: could not read the Takedown Stream Tracker ({exc!r}) — requested cases may be over-counted", flush=True)
    return no_claim, requested


def read_inputs() -> Dict[str, Set[str]]:
    """Everything summarize_cycle needs besides the scan rows."""
    no_claim, requested = _key_sets()
    acct_tab = _tab_id(WORKBOOK_URL, ACCOUNTS_TAB)
    accounts = set(parse_account_ids(_read_grid(WORKBOOK_URL, acct_tab, "A2:A500"))) if acct_tab else set()
    return {"no_claim": no_claim, "requested": requested, "accounts": accounts}


def waiting_summary(rows: List[List[str]], inputs: Dict[str, Set[str]], *, posted: date,
                    today: Optional[date] = None, weekly: bool = False) -> Dict[str, object]:
    """What the digest's "Waiting on managers" block shows: label managers with
    cases still unmarked (most first), the day the window closes, workdays left,
    and how many always-take-down cases ops still has to request."""
    today = today or today_brt()
    closes = _add_workdays(posted, WINDOW_WORKDAYS)
    per_region = summarize_cycle(rows, no_claim_keys=inputs["no_claim"], requested_keys=inputs["requested"],
                                 always_td_ids=inputs["accounts"])
    managers: Counter = Counter()
    for region in per_region.values():
        managers.update(region["by_manager"])
    return {
        "managers": managers.most_common(),
        "closes": closes,
        "days_left": _net_workdays(today, closes),
        "weekly": weekly,
        "always_td_open": sum(r["always_td_left"] for r in per_region.values()),
    }


def summarize_cycle(rows: List[List[str]], *, no_claim_keys: Set[str], requested_keys: Set[str],
                    always_td_ids: Set[str]) -> Dict[str, Dict[str, object]]:
    """Per region: cases still to claim, per label manager, and always-take-down
    cases not requested yet. Only Other Party cases that are still Online count."""
    header = rows[0]
    idx = ad._header_index(header)
    decision_i = ad._find(idx, *ad.DECISION_HEADER_ALIASES)
    song_id_i, title_i, region_i = idx.get(ad.SONG_ID_HEADER), idx.get(ad.TITLE_HEADER), idx.get(ad.REGION_HEADER)
    m_isrc_i, m_title_i, status_i = idx.get(ad.MATCHED_ISRC_HEADER), idx.get(ad.MATCHED_TITLE_HEADER), idx.get(ad.STATUS_HEADER)
    user_i, op_i = idx.get(USER_ID_HEADER), idx.get(OPERATOR_HEADER)

    out = {r: {"to_claim": 0, "always_td_left": 0, "by_manager": Counter()} for r in REGIONS}
    seen: Set[str] = set()
    for row in rows[1:]:
        if not ad._is_other_party(ad._cell(row, decision_i)) or ad._cell(row, status_i).lower() != "online":
            continue
        key = ad.case_key(ad._cell(row, song_id_i), ad._cell(row, title_i), ad._cell(row, m_isrc_i), ad._cell(row, m_title_i))
        if key in seen or key in requested_keys:
            continue
        seen.add(key)
        country = ad._cell(row, region_i)
        region = ad.COUNTRY_TO_REGION.get(country, "BR")  # unmapped countries go to the default (BR) ops owner
        if _digits(ad._cell(row, user_i)) in always_td_ids:
            out[region]["always_td_left"] += 1
            continue
        if key in no_claim_keys:
            continue
        out[region]["to_claim"] += 1
        raw = ad._cell(row, op_i)
        names = [n for n in ad._clean_names(raw).replace(" and ", ",").split(",") if n.strip()] or ["No manager on file"]
        for n in names:
            out[region]["by_manager"][n.strip()] += 1
    return out


def build_alarm_card(region: str, cycle: str, summary: Dict[str, object]) -> dict:
    mgr = summary["by_manager"].most_common(6)
    mgr_line = " · ".join(f"**{n}** {c}" for n, c in mgr) or "—"
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "red", "title": {"tag": "plain_text", "content": f"⏰ Claim window closed · {cycle} cycle"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content":
                f"The {WINDOW_WORKDAYS} workdays managers had to opt out are over. Time to file everything still open for **{region}**."}},
            {"tag": "column_set", "flex_mode": "none", "columns": [
                ad._stat_tile(str(summary["to_claim"]), "Claims to file"),
                ad._stat_tile(str(summary["always_td_left"]), "Always-TD still to request"),
            ]},
            {"tag": "div", "text": {"tag": "lark_md", "content": f"By label manager: {mgr_line}"}},
            {"tag": "action", "actions": [{
                "tag": "button", "text": {"tag": "plain_text", "content": "Open the claim queue"},
                "type": "primary", "url": ad.DASHBOARD_URL,
            }]},
            {"tag": "note", "elements": [{"tag": "plain_text", "content":
                "Sent once per cycle. Tick 🔔 Takedown on the dashboard after filing so the case leaves the list."}]},
        ],
    }


def run_daily_alarm(*, today: Optional[date] = None, dry_run: bool = False) -> Dict[str, object]:
    from copyright_alert.rights_confirmation_notice import _send_to_ops  # reused DM routing

    today = today or today_brt()
    result: Dict[str, object] = {"checked": 0, "sent": [], "skipped": []}
    due = []
    for w in read_windows():
        deadline = ad_parse_date(w["deadline_date"])
        if deadline and _net_workdays(deadline, today) > 0 and not str(w["alarmed_at"]).startswith("DONE"):
            due.append(w)
    result["checked"] = len(due)
    if not due:
        return result

    no_claim, requested = _key_sets()
    accounts = set(parse_account_ids(_read_grid(WORKBOOK_URL, _tab_id(WORKBOOK_URL, ACCOUNTS_TAB), "A2:A500"))) if _tab_id(WORKBOOK_URL, ACCOUNTS_TAB) else set()
    tabs = {t["name"].strip().lower(): t for t in ad.list_tabs(ad.ACR_SHEET_URL)}

    for w in due:
        cycle = str(w["cycle"]).strip()
        tab = tabs.get(cycle.lower())
        if not tab:
            result["skipped"].append({"cycle": cycle, "reason": "scan tab not found"})
            continue
        rows = ad.read_tab(ad.ACR_SHEET_URL, tab["sheet_id"])
        if not rows or not ad.is_valid_cycle_tab(ad._header_index(rows[0])):
            result["skipped"].append({"cycle": cycle, "reason": "scan tab unreadable"})
            continue
        per_region = summarize_cycle(rows, no_claim_keys=no_claim, requested_keys=requested, always_td_ids=accounts)
        done = _alarmed_regions(w["alarmed_at"])
        failed = False
        for region in REGIONS:
            s = per_region[region]
            if region in done:
                continue
            if not (s["to_claim"] or s["always_td_left"]):
                done.add(region)
                continue
            if dry_run:
                result["sent"].append({"cycle": cycle, "region": region, "to_claim": s["to_claim"],
                                       "always_td_left": s["always_td_left"], "dry_run": True})
                continue
            send = _send_to_ops(region, build_alarm_card(region, cycle, s), log_context=f"claim window {cycle}")
            if send.get("ok"):
                done.add(region)
                result["sent"].append({"cycle": cycle, "region": region, "to_claim": s["to_claim"], "always_td_left": s["always_td_left"]})
            else:
                failed = True
                result["skipped"].append({"cycle": cycle, "region": region, "reason": "DM failed — will retry next run"})
        if not dry_run:
            regions = ",".join(r for r in REGIONS if r in done)
            _set_alarmed(w, f"PARTIAL {regions}" if failed else f"DONE {_now_brt_str()} {regions}".strip())
    return result


# ── Monday reminder ─────────────────────────────────────────────────────────

WEEKLY_STATE_FILE = ad.ROOT / "runtime" / "claim_weekly_state.json"


def _load_weekly_state() -> Dict[str, str]:
    try:
        return json.loads(WEEKLY_STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def run_weekly_reminders(*, today: Optional[date] = None, dry_run: bool = False) -> Dict[str, object]:
    """Every Monday while a window is still open, re-post the digest card with
    fresh numbers in the Content Safety group. Only managers who still have
    unmarked cases are tagged; with nobody pending there is nothing to remind
    and nothing is posted. At most once per cycle per day."""
    today = today or today_brt()
    result: Dict[str, object] = {"posted": [], "skipped": []}
    if today.weekday() != 0:
        return result
    state = _load_weekly_state()
    tabs = None
    inputs = None
    for w in read_windows():
        cycle = str(w["cycle"]).strip()
        posted, deadline = ad_parse_date(w["posted_date"]), ad_parse_date(w["deadline_date"])
        if not posted or not deadline or posted >= today or _net_workdays(today, deadline) < 1:
            continue
        if state.get(cycle) == today.isoformat():
            result["skipped"].append({"cycle": cycle, "reason": "already posted today"})
            continue
        tabs = tabs if tabs is not None else {t["name"].strip().lower(): t for t in ad.list_tabs(ad.ACR_SHEET_URL)}
        tab = tabs.get(cycle.lower())
        rows = ad.read_tab(ad.ACR_SHEET_URL, tab["sheet_id"]) if tab else None
        if not rows or not ad.is_valid_cycle_tab(ad._header_index(rows[0])):
            result["skipped"].append({"cycle": cycle, "reason": "scan tab not found or unreadable"})
            continue
        inputs = inputs or read_inputs()
        waiting = waiting_summary(rows, inputs, posted=posted, today=today, weekly=True)
        if not waiting["managers"]:
            result["skipped"].append({"cycle": cycle, "reason": "no manager has cases pending"})
            continue
        data = ad.build_digest_data(rows, excluded_keys=inputs["no_claim"], always_td_ids=inputs["accounts"])
        card = ad.build_card(cycle, data, [], waiting=waiting)
        if dry_run:
            result["posted"].append({"cycle": cycle, "managers": len(waiting["managers"]), "dry_run": True})
            continue
        ad.ra.post_card(card, chat_id=ad.CONTENT_SAFETY_CHAT_ID, context=f"acr_digest_weekly:{cycle}")
        state[cycle] = today.isoformat()
        WEEKLY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        WEEKLY_STATE_FILE.write_text(json.dumps(state))
        result["posted"].append({"cycle": cycle, "managers": len(waiting["managers"])})
    return result


def ad_parse_date(text) -> Optional[date]:
    try:
        return date.fromisoformat(str(text or "").strip()[:10])
    except ValueError:
        return None


# ── Daily entry point ───────────────────────────────────────────────────────

def run_daily_jobs(*, dry_run: bool = False) -> Dict[str, object]:
    out: Dict[str, object] = {}
    for name, fn in (("always_td_sync", sync_always_td_accounts), ("alarm", run_daily_alarm), ("weekly", run_weekly_reminders)):
        try:
            out[name] = fn(dry_run=dry_run)
        except Exception as exc:  # one failing job must not stop the other, or the daily workflow
            print(f"  ✗ claim window {name} failed (non-fatal): {exc!r}", flush=True)
            out[name] = {"error": repr(exc)}
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Claim window: register, sync always-TD accounts, send the day-5 alarm")
    parser.add_argument("--dry-run", action="store_true", help="show what would happen, change nothing")
    parser.add_argument("--register", metavar="CYCLE", help="register a window for this scan tab now (e.g. for a cycle already announced)")
    parser.add_argument("--posted", metavar="YYYY-MM-DD", help="with --register: the day the digest posted (default today, BRT)")
    args = parser.parse_args()
    if args.register:
        print(json.dumps(register_window(args.register, ad_parse_date(args.posted), dry_run=args.dry_run), indent=2))
        return 0
    print(json.dumps(run_daily_jobs(dry_run=args.dry_run), ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
