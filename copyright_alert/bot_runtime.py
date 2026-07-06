#!/usr/bin/env python3
"""Shared runtime helpers for copyright alert bot commands and watchdog flows."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from copyright_alert.lark_auth import request_json_with_auth_retry

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copyright_alert import daily_workflow as dw  # noqa: E402
from copyright_alert import run_alert as ra  # noqa: E402
from copyright_alert.manager_exclusions import (
    add_exclusion,
    load_exclusions,
    manager_uids,
    remove_exclusion,
    total_exclusion_count,
)

BOT_SCRIPT = ROOT / "copyright_alert" / "persistent_callback.py"
PID_FILE = ROOT / "runtime" / "persistent_callback.pid"
DAEMON_LOG = ROOT / "logs" / "callback_daemon.log"
LAST_CARD_FILE = ROOT / "runtime" / "last_card.json"
COMMAND_STATE_FILE = ROOT / "runtime" / "command_state.json"

US_REGION_CODES = {"US", "CA", "AU", "NZ"}
DEFAULT_WATCHDOG_REGION = "BR"

REGION_CONFIGS = {
    "BR": {
        "chat_id": "oc_6e157309d8d7145ba5ce7f0ba67354cb",
        "chat_name": "AP Direitos BR",
        "tracker_url": "https://bytedance.sg.larkoffice.com/sheets/HMQLsGgymhdIQ3tSbNNlk3m1gKd",
        "sheet_id": "c02dad",
        "ignored_mentions": set(),
        "quality_label": "BR",
        # Ops owner who receives the private action-card DMs and ops reminders.
        "ops_dm_email": "filipe.cairo@bytedance.com",  # filipe.cairo
        "ops_dm_open_id": "",  # resolved from email at send time
        "ops_dm_chat_id": "",  # optional confirmed DM chat_id
        # Aeolus user_region codes that route to this region.
        "countries": {"BR"},
        # Daily scan schedule. The cron is expressed in Asia/Shanghai (the
        # scheduler runtime tz, UTC+8) but is *intended* to fire at the local
        # business hour shown by `scan_local_label`.
        # BR ops works in São Paulo (BRT, UTC-3); 09:00 BRT = 20:00 Shanghai.
        "scan_cron": "0 0 20 * * 1-5",
        "scan_local_tz": "America/Sao_Paulo",
        "scan_local_label": "09:00 BRT",
    },
    "US": {
        "chat_id": "oc_e85373716ee746e3dc1bf999929cf1c4",
        "chat_name": "US Infringement Clam Notification",
        "tracker_url": "https://bytedance.sg.larkoffice.com/sheets/FKqxsTu0bhl3ATt3n7YlIGvfgne",
        "sheet_id": "66eefc",
        "ignored_mentions": {"huang.zeyuan"},
        "quality_label": "US",
        "ops_dm_email": "ben.gordon-pound@bytedance.com",
        "ops_dm_open_id": "ou_9cd2b961d55ed59e1b0e79e0b52a677c",
        "ops_dm_chat_id": "oc_842a762dacdea52dd8cd4017da3a94d5",
        "countries": set(US_REGION_CODES),
        "scan_cron": "0 0 0 * * 1-5",
        "scan_local_tz": "America/Los_Angeles",
        "scan_local_label": "09:00 PDT",
    },
    "SPLA": {
        # Spanish-speaking Latin America (Latam minus Brazil).
        "chat_id": "oc_04c1d1182d5795c182ca34dd152c5f91",
        "chat_name": "SPLA Infringement Claim Alert",
        # Canonical tracker is the wiki node below; the sheets URL is the same
        # workbook resolved for the lark-cli sheets read/append/append APIs.
        "tracker_url": "https://bytedance.larkoffice.com/sheets/FKCTs8go0hsbWQtFtvGlg63Hgji",
        "tracker_wiki_url": "https://bytedance.larkoffice.com/wiki/Ig1XwJc85iWmsGkEzujcy7sln9d",
        "sheet_id": "66eefc",
        "ignored_mentions": set(),
        "quality_label": "SPLA",
        # SPLA Ops owner — Bernardo Sanchez (bernardo.sanchez). Routed by open_id
        # because the address is the SPLA ops mailbox, not filipe.cairo.
        "ops_dm_email": "bernardo.sanchez@bytedance.com",  # bernardo.sanchez
        "ops_dm_open_id": "ou_b0be769000b08971e717c7c01323dabe",  # bernardo.sanchez
        "ops_dm_chat_id": "oc_48de5eacf06bffee6cd4aa422e1e9855",  # bernardo.sanchez confirmed DM
        "countries": {"MX", "CL", "CO", "AR", "ES", "PR", "PE"},
        # Daily scan schedule. SPLA ops works in Mexico City (CDMX, UTC-6);
        # 09:00 CDMX = 23:00 Shanghai (UTC+8).
        "scan_cron": "0 0 23 * * 1-5",
        "scan_local_tz": "America/Mexico_City",
        "scan_local_label": "09:00 CDMX",
    },
}
CHAT_TO_REGION = {cfg["chat_id"]: region for region, cfg in REGION_CONFIGS.items()}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json_file(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def save_json_file(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_text_message(content: str) -> str:
    try:
        parsed = json.loads(content or "{}")
        if isinstance(parsed, dict):
            return str(parsed.get("text") or "").strip()
    except Exception:
        pass
    return str(content or "").strip()


def configure_region(region: str) -> Dict[str, str]:
    region = (region or "BR").upper()
    cfg = REGION_CONFIGS[region]
    ra.TARGET_CHAT_ID = cfg["chat_id"]
    ra.TRACKER_SHEET_URL = cfg["tracker_url"]
    ra.TRACKER_SHEET_ID = cfg["sheet_id"]
    ra.EXCLUDED_MENTIONS.update({u.lower() for u in cfg.get("ignored_mentions", set())})
    # Drive the region-aware scan filter in run_alert.qualifies().
    ra.CURRENT_REGION = region
    ra.QUALIFY_COUNTRIES = set(cfg.get("countries") or set())
    return cfg


def qualifies_region(row: Dict[str, str], region: str) -> bool:
    region = (region or "BR").upper()
    cfg = REGION_CONFIGS.get(region)
    if not cfg:
        return False
    data_region = (row.get("user_region") or "").strip().upper()
    tier = (row.get("User Tier") or "").strip()
    source = (row.get("source_type_name") or "").strip()
    countries = cfg.get("countries") or set()
    if data_region not in countries:
        return False
    return source in ("AP", "A&R") or tier == "High Quality"


def _get_bot_access_token() -> str:
    return ra._get_bot_access_token()


def _post_api(path: str, body: dict, method: str = "POST") -> dict:
    url = f"https://open.larksuite.com/open-apis{path}"

    def make_request():
        token = _get_bot_access_token()
        if not token:
            raise RuntimeError("Could not get bot access token")
        return urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method=method,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {token}",
            },
        )

    try:
        return request_json_with_auth_retry(make_request, timeout=60, context=f"bot_runtime._post_api:{path}")
    except Exception:
        receive_id = body.get("receive_id")
        msg_type = body.get("msg_type")
        content = body.get("content")
        if method != "POST" or not path.startswith("/im/v1/messages?receive_id_type="):
            raise
        receive_id_type = path.split("receive_id_type=", 1)[1]
        if msg_type != "interactive" or not receive_id or not content:
            raise
        return ra._send_interactive_via_lark_cli(
            receive_id_type=receive_id_type,
            receive_id=receive_id,
            content=content,
            timeout=60,
        )


def _post_content_payload(title: str, lines: List) -> dict:
    content = []
    for line in lines:
        if line == "__HR__":
            content.append([{"tag": "hr"}])
        else:
            content.append([{"tag": "text", "text": str(line)}])
    return {"zh_cn": {"title": title, "content": content}}


def reply_post(message_id: str, title: str, lines: List) -> dict:
    payload = _post_content_payload(title, lines)
    return _post_api(
        f"/im/v1/messages/{message_id}/reply",
        {"msg_type": "post", "content": json.dumps(payload, ensure_ascii=False)},
    )


def reply_text(message_id: str, text: str) -> dict:
    return _post_api(
        f"/im/v1/messages/{message_id}/reply",
        {"msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)},
    )


def send_chat_card(chat_id: str, card: dict) -> dict:
    return _post_api(
        "/im/v1/messages?receive_id_type=chat_id",
        {"receive_id": chat_id, "msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)},
    )


def build_watchdog_card(region: Optional[str] = None, detected_via: str = "watchdog") -> dict:
    region = (region or DEFAULT_WATCHDOG_REGION).upper()
    cfg = REGION_CONFIGS.get(region) or REGION_CONFIGS[DEFAULT_WATCHDOG_REGION]
    scope_label = cfg["chat_name"]
    if detected_via == "button_click":
        detail = f"Detection source: button click failure in **{scope_label}**"
    else:
        detail = f"Detection source: cron watchdog fallback → notifying default group **{scope_label}**"
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": "⚠️ Callback daemon auto-heal"},
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "**Callback daemon was detected as offline and has been restarted automatically ✅**",
                },
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        f"Restart time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                        f"Notification scope: **{scope_label}**\n"
                        f"{detail}\n"
                        "No mentions were sent."
                    ),
                },
            },
        ],
    }


def read_tracker_rows(region: str) -> Tuple[List[str], List[Dict[str, str]]]:
    cfg = configure_region(region)
    cmd = [
        "lark-cli",
        "sheets",
        "+csv-get",
        "--url",
        cfg["tracker_url"],
        "--sheet-id",
        cfg["sheet_id"],
        "--range",
        "A1:Q200",
    ]
    res = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=90)
    if res.returncode != 0:
        raise RuntimeError((res.stdout + res.stderr)[:800])
    parsed, rows, row_numbers = ra.parse_lark_annotated_csv(res.stdout)
    if not parsed:
        raise RuntimeError("Failed to parse tracker sheet output")
    if not rows:
        return [], []
    headers = [str(value or "").strip() for value in rows[0]]
    records = []
    for idx, row in enumerate(rows[1:], start=1):
        padded = list(row) + [""] * max(0, len(headers) - len(row))
        record = {
            headers[col_idx]: str(padded[col_idx] or "").strip()
            for col_idx in range(len(headers))
            if headers[col_idx]
        }
        if any(v for v in record.values()) and qualifies_region(record, region):
            record["_row_number"] = row_numbers[idx] if idx < len(row_numbers) else idx + 1
            records.append(record)
    return headers, records


def _is_open_status(status: str) -> bool:
    normalized = (status or "").strip().lower()
    if not normalized:
        return False
    if "resolved" in normalized:
        return False
    return any(token in normalized for token in ["pending", "investigating", "confirm takedown", "disputing", "open", "🔍", "🔴", "⚖️"])


def get_open_cases(region: str) -> List[Dict[str, str]]:
    _, rows = read_tracker_rows(region)
    return [row for row in rows if _is_open_status(row.get("Status", ""))]


# Tokens that mark a case as resolved / closed and therefore NOT eligible for
# the /unassigned listing. Empty status counts as "still open".
_RESOLVED_TOKENS = (
    "resolved",
    "closed",
    "taken down",
    "takedown",
    "removed",
    "withdrawn",
    "rejected",
    "duplicate",
    "no action",
)


def _is_unresolved_status(status: str) -> bool:
    """True for cases that are still open (empty / pending / investigating /
    disputing / etc.). False only when the status clearly indicates the case
    has been resolved or closed."""
    normalized = (status or "").strip().lower()
    if not normalized:
        return True
    return not any(tok in normalized for tok in _RESOLVED_TOKENS)


def _is_blank_assignment(value: str) -> bool:
    """True when a Label Manager / BD cell should be treated as 'unassigned'."""
    normalized = (value or "").strip().lower()
    if not normalized:
        return True
    return normalized in {"n/a", "na", "none", "-", "--", "tbd", "unassigned"}


def get_unassigned_cases(region: str) -> List[Dict[str, str]]:
    """Return open/unresolved tracker rows that have no Label Manager AND no BD."""
    _, rows = read_tracker_rows(region)
    out = []
    for row in rows:
        if not _is_unresolved_status(row.get("Status", "")):
            continue
        if not _is_blank_assignment(row.get("Label Manager", "")):
            continue
        if not _is_blank_assignment(row.get("BD", "")):
            continue
        out.append(row)
    return out


def unassigned_lines(region: str) -> Tuple[str, List]:
    """Build the (title, post-content lines) reply for the /unassigned command."""
    cases = get_unassigned_cases(region)
    title = f"/unassigned — {region}"
    if not cases:
        return title, ["All open cases have an assigned manager or BD ✅"]
    lines: List = [
        f"Open {region} cases with no Label Manager AND no BD: ({len(cases)})",
        "__HR__",
    ]
    for row in cases:
        upc = row.get("UPC") or "N/A"
        artist = row.get("Artist(s)") or row.get("Artist") or "N/A"
        track_title = row.get("Title") or "N/A"
        date_received = row.get("Date Received") or "N/A"
        status = row.get("Status") or "(empty)"
        lines.append(
            f"• UPC {upc} — Artist: {artist} — Title: {track_title} "
            f"— Received: {date_received} — Status: {status}"
        )
    return title, lines


def _am_name(row: Dict[str, str]) -> str:
    return row.get("BD") or "Unassigned"


def _case_bullet(row: Dict[str, str]) -> str:
    return (
        f"• UPC {row.get('UPC') or 'N/A'} — Artist: {row.get('Artist(s)') or 'N/A'} "
        f"— Title: {row.get('Title') or 'N/A'} — Status: {row.get('Status') or 'N/A'}"
    )


def grouped_claim_lines(region: str, am_filter: Optional[str] = None) -> Tuple[str, List]:
    _, cases = read_tracker_rows(region)
    groups: Dict[str, List[Dict[str, str]]] = {}
    for row in cases:
        am = _am_name(row)
        if am_filter and am_filter.lower() not in am.lower():
            continue
        groups.setdefault(am, []).append(row)

    title = f"/{'claims'} — {region} cases grouped by AM"
    if not groups:
        target = f" matching '{am_filter}'" if am_filter else ""
        return title, [f"No open cases found{target}.", "__HR__", "AM grouping currently uses the tracker BD column as the closest available proxy."]

    lines: List = ["AM grouping currently uses the tracker BD column as the closest available proxy.", "__HR__"]
    for am in sorted(groups.keys(), key=lambda x: x.lower()):
        lines.append(f"👤 {am}")
        for row in groups[am]:
            lines.append(_case_bullet(row))
        lines.append("__HR__")
    if lines and lines[-1] == "__HR__":
        lines.pop()
    return title, lines


def load_command_state() -> dict:
    return load_json_file(COMMAND_STATE_FILE, {"regions": {}})


def save_command_state(state: dict) -> None:
    save_json_file(COMMAND_STATE_FILE, state)


def update_last_scan(region: str, summary: dict) -> None:
    state = load_command_state()
    state.setdefault("regions", {})[region] = {
        "last_scan_at": utc_now_iso(),
        "summary": summary,
    }
    save_command_state(state)


def region_last_scan(region: str) -> str:
    state = load_command_state()
    region_state = (state.get("regions") or {}).get(region) or {}
    if region_state.get("last_scan_at"):
        return region_state["last_scan_at"]
    if region == "BR":
        checkpoint = load_json_file(ROOT / "runtime" / "scan_checkpoint.json", {})
        return checkpoint.get("updated_at") or "N/A"
    return "N/A"


def manual_scan_region(region: str, max_messages: int = 80) -> dict:
    cfg = configure_region(region)
    dw.TRIAGE_MAX = max_messages
    messages = dw.fetch_messages_raw()
    summary = {
        "region": region,
        "chat": cfg["chat_id"],
        "fetched": len(messages),
        "examined": 0,
        "parsed_candidates": 0,
        "unique_upcs": 0,
        "posted": 0,
        "skipped_duplicate": 0,
        "skipped_not_qualifying": 0,
        "skipped_no_aeolus": 0,
        "skipped_no_identifier": 0,
        "skipped_prefilter": 0,
        "posted_items": [],
    }
    seen_threads = set()
    candidates = []
    for m in messages:
        msg_id = m.get("message_id", "")
        subject = m.get("subject", "")
        date = m.get("date", "")
        thread_id = m.get("thread_id") or msg_id
        reason = dw._prefilter_skip_reason(subject, thread_id, seen_threads)
        if reason:
            summary["skipped_prefilter"] += 1
            continue
        seen_threads.add(thread_id)
        if not msg_id:
            continue
        summary["examined"] += 1
        body, meta = ra.fetch_email(msg_id)
        if not body:
            continue
        ef = ra.extract_fields(body, subject, meta)
        upc = str(ef.get("upc", "") or "").strip()
        if not upc or upc == "N/A":
            summary["skipped_no_identifier"] += 1
            continue
        candidates.append({"message_id": msg_id, "subject": subject, "date": date, "ef": ef})
    summary["parsed_candidates"] = len(candidates)
    aeolus_by_upc = ra.batch_query_aeolus_by_upc([c["ef"].get("upc") for c in candidates])
    summary["unique_upcs"] = len(aeolus_by_upc)

    for candidate in candidates:
        msg_id = candidate["message_id"]
        subject = candidate["subject"]
        ef = candidate["ef"]
        upc = str(ef.get("upc", "") or "").strip()
        ar = aeolus_by_upc.get(upc) or {}
        if not ar:
            summary["skipped_no_aeolus"] += 1
            continue
        if (not ef.get("isrc") or ef.get("isrc") == "N/A") and ar.get("isrc"):
            ef["isrc"] = str(ar.get("isrc")).strip() or "N/A"
        if not qualifies_region(ar, region):
            summary["skipped_not_qualifying"] += 1
            continue
        dup_key = ra.claim_key(ef, ar, subject)
        if ra.is_claim_already_posted(dup_key):
            summary["skipped_duplicate"] += 1
            continue

        card = ra.build_card(ef, ar)
        LAST_CARD_FILE.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
        success, posted_message_id = ra.post_card(card, ar, upc=upc, context=f"{region} manual scan group post")
        if not (success and posted_message_id):
            continue

        patched_card = ra.build_card(ef, ar, lark_message_id=posted_message_id)
        LAST_CARD_FILE.write_text(json.dumps(patched_card, ensure_ascii=False, indent=2), encoding="utf-8")
        ra.patch_card_message(posted_message_id, patched_card)
        tracker_row = ra.append_tracker_row(ef, ar, posted_message_id, status="")
        ra._save_posted_claim(
            dup_key,
            {
                "message_id": posted_message_id,
                "source_email_message_id": msg_id,
                "subject": subject,
                "upc": ef.get("upc", "N/A"),
                "isrc": ef.get("isrc", "N/A"),
                "title": ef.get("title") if ef.get("title") != "N/A" else ar.get("album_title", "N/A"),
                "artist": ra._format_artist_names(ar.get("display_artist")),
                "ref_id": ef.get("ref_id", "N/A"),
                "region": ar.get("user_region", "N/A"),
                "tracker_row": tracker_row,
                "source": ar.get("source_type_name", "N/A"),
                "tier": ar.get("User Tier", "N/A"),
                "date": candidate.get("date", ""),
                "claimant_name": ef.get("claimant_name", "N/A"),
                "claimant_email": ef.get("claimant_email", "N/A"),
                "chat_id": ra.TARGET_CHAT_ID,
            },
        )
        summary["posted"] += 1
        summary["posted_items"].append({
            "upc": ef.get("upc", "N/A"),
            "title": ef.get("title") if ef.get("title") != "N/A" else ar.get("album_title", "N/A"),
            "artist": ra._format_artist_names(ar.get("display_artist")),
            "message_id": posted_message_id,
        })
    update_last_scan(region, summary)
    return summary


def daemon_processes() -> List[int]:
    pattern = str(BOT_SCRIPT)
    res = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=20)
    if res.returncode not in (0, 1):
        return []
    pids = []
    for line in res.stdout.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def daemon_status() -> dict:
    pid = None
    pid_file_ok = False
    pid_data = load_json_file(PID_FILE, {})
    if isinstance(pid_data, dict):
        raw_pid = pid_data.get("pid")
        if isinstance(raw_pid, int) and is_pid_alive(raw_pid):
            pid = raw_pid
            pid_file_ok = True
    if pid is None:
        pids = daemon_processes()
        if pids:
            pid = pids[0]
    return {"running": bool(pid), "pid": pid, "pid_file_ok": pid_file_ok}


def write_pid_file(pid: int) -> None:
    save_json_file(PID_FILE, {"pid": pid, "updated_at": utc_now_iso(), "script": str(BOT_SCRIPT)})


def start_daemon() -> int:
    DAEMON_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(DAEMON_LOG, "a", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(BOT_SCRIPT)],
        cwd=str(ROOT),
        stdout=log_fh,
        stderr=log_fh,
        start_new_session=True,
    )
    write_pid_file(proc.pid)
    return proc.pid


def restart_daemon(current_pid: Optional[int] = None, wait_seconds: float = 5.0) -> int:
    status = daemon_status()
    old_pid = status.get("pid")
    if old_pid and current_pid and old_pid == current_pid:
        new_pid = start_daemon()
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import os, signal, time; "
                    f"time.sleep(2); "
                    f"os.kill({old_pid}, signal.SIGTERM)"
                ),
            ],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return new_pid
    if old_pid:
        stop_daemon(old_pid)
        deadline = datetime.now().timestamp() + max(wait_seconds, 0)
        while datetime.now().timestamp() < deadline:
            if not is_pid_alive(old_pid):
                break
            threading.Event().wait(0.2)
    return start_daemon()


def stop_daemon(pid: Optional[int] = None) -> bool:
    status = daemon_status()
    target_pid = pid or status.get("pid")
    if not target_pid:
        return False
    try:
        os.kill(target_pid, signal.SIGTERM)
        return True
    except OSError:
        return False


def ensure_daemon_alive(
    notify_chat_id: Optional[str] = None,
    detected_via: str = "button_click",
    send_notifications: bool = True,
    force_restart: bool = False,
    current_pid: Optional[int] = None,
) -> dict:
    """Reusable helper: verify the callback daemon is alive, restart it if not
    (or if ``force_restart`` is True), and optionally post a recovery card to a
    single target chat. Returns a structured result dict.

    - ``notify_chat_id``: chat to receive the recovery notification. If provided
      and known, the corresponding region's card is posted only there. If
      omitted, falls back to the default watchdog region's chat.
    - ``force_restart``: when True, restart even if the current process appears
      alive (used by the event-driven path where a callback failure already
      indicates the daemon is unhealthy).
    """
    before = daemon_status()
    region = None
    if notify_chat_id:
        region = CHAT_TO_REGION.get(notify_chat_id)
    region = (region or DEFAULT_WATCHDOG_REGION).upper()
    if region not in REGION_CONFIGS:
        region = DEFAULT_WATCHDOG_REGION
    target_chat_id = notify_chat_id or REGION_CONFIGS[region]["chat_id"]

    result = {
        "before": before,
        "restarted": False,
        "new_pid": before.get("pid"),
        "notifications": [],
        "notify_region": region,
        "notify_chat_id": target_chat_id,
        "detected_via": detected_via,
        "force_restart": force_restart,
    }

    if before.get("running") and not force_restart:
        if before.get("pid"):
            write_pid_file(before["pid"])
        return result

    if before.get("running") and force_restart:
        new_pid = restart_daemon(current_pid=current_pid)
    else:
        new_pid = start_daemon()
    result["restarted"] = True
    result["new_pid"] = new_pid

    if send_notifications:
        card = build_watchdog_card(region=region, detected_via=detected_via)
        try:
            resp = send_chat_card(target_chat_id, card)
            result["notifications"].append({"region": region, "chat_id": target_chat_id, "response": resp})
        except Exception as exc:
            result["notifications"].append({"region": region, "chat_id": target_chat_id, "error": repr(exc)})
    return result


def watchdog_check_once(
    send_notifications: bool = True,
    notify_region: Optional[str] = None,
    detected_via: str = "watchdog",
) -> dict:
    """Backwards-compatible wrapper around :func:`ensure_daemon_alive` for the
    legacy watchdog CLI. Resolves a region into its default chat_id and
    delegates the real work."""
    region = (notify_region or DEFAULT_WATCHDOG_REGION).upper()
    if region not in REGION_CONFIGS:
        region = DEFAULT_WATCHDOG_REGION
    chat_id = REGION_CONFIGS[region]["chat_id"]
    return ensure_daemon_alive(
        notify_chat_id=chat_id,
        detected_via=detected_via,
        send_notifications=send_notifications,
        force_restart=False,
    )


def command_help_lines() -> List:
    return [
        "Available commands:",
        "__HR__",
        "/status — callback daemon status, last scan time, and open-case count",
        "/scan — trigger an immediate scan for the current region",
        "/pending — list open / investigating cases from the current region tracker",
        "/pending @AccountManager — list open cases filtered to a specific AM (matches the tracker BD / Label Manager columns; works in DM too, defaults to BR)",
        "/claims [am_name] — list cases grouped by AM (using tracker BD column as AM proxy)",
        "/restart — manually restart the callback daemon for all groups",
        "/exclude @ManagerName from [label_uid] — stop tagging that manager for the label UID",
        "/include @ManagerName for [label_uid] — re-enable tagging that manager for the label UID",
        "/exceptions — show the full exclusion list",
        "/exceptions [manager_name] — show which label UIDs a manager is excluded from",
        "/unassigned [region] — list open cases with no Label Manager AND no BD (defaults to BR; works in DM too)",
        "/health or /healthcheck — run a full health check on all bot components (daemon, JWT, lark-cli, tracker, email flow, scheduled jobs)",
        "/fix — attempt to auto-resolve issues found by /health (refresh JWT, restart daemon, etc.) and report what still needs manual attention",
        "/help — show this command list",
    ]


# ── Health check & self-heal ─────────────────────────────────────────────────

AIME_ENV_REFRESH_FILE = ROOT / "runtime" / "aime_env_refresh.json"


def _decode_jwt_exp(token: str) -> Optional[int]:
    """Best-effort decode of a JWT to extract its `exp` claim (unix seconds)."""
    import base64
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode("utf-8"))
        exp = payload.get("exp")
        return int(exp) if exp is not None else None
    except Exception:
        return None


def _check_jwt_credentials() -> Dict:
    """Inspect aime_env_refresh.json + os.environ for the AIME user JWT and
    return {status: ok|warn|err, detail, expires_in_seconds}."""
    candidates = []
    snapshot = load_json_file(AIME_ENV_REFRESH_FILE, {}) or {}
    for key in ("AIME_USER_CLOUD_JWT", "USER_CLOUD_JWT", "IRIS_USER_CLOUD_JWT"):
        tok = snapshot.get(key) or os.environ.get(key)
        if tok:
            candidates.append((key, tok))
    if not candidates:
        return {"status": "err", "detail": "No AIME JWT found in env or aime_env_refresh.json"}
    now = int(datetime.now(timezone.utc).timestamp())
    soonest = None
    soonest_key = None
    for key, tok in candidates:
        exp = _decode_jwt_exp(tok)
        if exp is None:
            continue
        if soonest is None or exp < soonest:
            soonest = exp
            soonest_key = key
    if soonest is None:
        return {"status": "warn", "detail": "JWT(s) present but exp claim could not be decoded"}
    remaining = soonest - now
    if remaining <= 0:
        return {"status": "err", "detail": f"{soonest_key} expired {-remaining}s ago", "expires_in_seconds": remaining}
    if remaining < 300:
        return {"status": "warn", "detail": f"{soonest_key} expires in {remaining}s (<5min)", "expires_in_seconds": remaining}
    return {"status": "ok", "detail": f"{soonest_key} expires in {remaining}s", "expires_in_seconds": remaining}


def _check_lark_cli() -> Dict:
    """Verify lark-cli can connect by issuing a minimal authenticated call."""
    try:
        res = subprocess.run(
            ["lark-cli", "mail", "user_mailboxes", "profile",
             "--params", '{"user_mailbox_id":"me"}'],
            capture_output=True, text=True, timeout=30,
        )
        if res.returncode != 0:
            return {"status": "err", "detail": (res.stderr or res.stdout or "")[:300].strip()}
        return {"status": "ok", "detail": "mail.user_mailboxes.profile OK"}
    except FileNotFoundError:
        return {"status": "err", "detail": "lark-cli binary not on PATH"}
    except subprocess.TimeoutExpired:
        return {"status": "err", "detail": "lark-cli profile call timed out"}
    except Exception as exc:
        return {"status": "err", "detail": repr(exc)}


def _check_br_tracker() -> Dict:
    try:
        cfg = REGION_CONFIGS["BR"]
        cmd = [
            "lark-cli", "sheets", "+csv-get",
            "--url", cfg["tracker_url"],
            "--sheet-id", cfg["sheet_id"],
            "--range", "A1:A2",
        ]
        res = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=45)
        if res.returncode != 0:
            return {"status": "err", "detail": (res.stderr or res.stdout or "")[:300].strip()}
        return {"status": "ok", "detail": "BR tracker reachable"}
    except subprocess.TimeoutExpired:
        return {"status": "err", "detail": "BR tracker fetch timed out"}
    except Exception as exc:
        return {"status": "err", "detail": repr(exc)}


def _check_email_draft_dryrun() -> Dict:
    """Dry-run: verify lark-cli mail draft surface is reachable WITHOUT
    creating a draft. We invoke the help screen for `+draft-create`, which
    requires the binary + auth scaffolding to load but does not hit the
    server, then confirm a JWT is available."""
    try:
        res = subprocess.run(
            ["lark-cli", "mail", "+draft-create", "-h"],
            capture_output=True, text=True, timeout=15,
        )
        if res.returncode != 0:
            return {"status": "err", "detail": "+draft-create help failed: " + (res.stderr or "")[:200]}
        jwt = _check_jwt_credentials()
        if jwt.get("status") == "err":
            return {"status": "err", "detail": "draft surface OK but JWT invalid: " + jwt.get("detail", "")}
        return {"status": "ok", "detail": "+draft-create reachable (dry-run, no draft created)"}
    except Exception as exc:
        return {"status": "err", "detail": repr(exc)}


def _check_scheduled_jobs() -> Dict:
    """Heuristic: a scheduled scan job is considered active if either
    (a) command_state has a last_scan within the last 36 hours for any
    region, or (b) a daily_*.log under logs/ is newer than 36 hours."""
    threshold = 36 * 3600
    now = datetime.now(timezone.utc).timestamp()
    state = load_command_state() or {}
    most_recent = None
    for region, payload in (state.get("regions") or {}).items():
        ts = payload.get("last_scan_at")
        if not ts:
            continue
        try:
            dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            age = now - dt.timestamp()
            if most_recent is None or age < most_recent:
                most_recent = age
        except Exception:
            continue
    log_dir = ROOT / "logs"
    if log_dir.exists():
        for p in log_dir.glob("daily_*.log"):
            try:
                age = now - p.stat().st_mtime
                if most_recent is None or age < most_recent:
                    most_recent = age
            except Exception:
                continue
    if most_recent is None:
        return {"status": "warn", "detail": "No recent scan record found (no command_state or daily_*.log)"}
    if most_recent > threshold:
        return {"status": "warn", "detail": f"Last scheduled run was {int(most_recent/3600)}h ago (>36h)"}
    return {"status": "ok", "detail": f"Last scheduled run {int(most_recent/3600)}h ago"}


def run_health_check() -> Dict:
    """Run all health checks and return a structured report."""
    daemon = daemon_status()
    daemon_check = (
        {"status": "ok", "detail": f"PID {daemon['pid']} alive"}
        if daemon.get("running") else
        {"status": "err", "detail": "Daemon process not found"}
    )
    return {
        "daemon":          daemon_check,
        "jwt":             _check_jwt_credentials(),
        "lark_cli":        _check_lark_cli(),
        "br_tracker":      _check_br_tracker(),
        "email_dryrun":    _check_email_draft_dryrun(),
        "scheduled_jobs":  _check_scheduled_jobs(),
    }


_HEALTH_LABELS = {
    "daemon":         "Daemon process",
    "jwt":            "JWT / credentials",
    "lark_cli":       "lark-cli connectivity",
    "br_tracker":     "BR tracker sheet",
    "email_dryrun":   "Email draft flow (dry-run)",
    "scheduled_jobs": "Scheduled jobs",
}

_STATUS_ICON = {"ok": "✅", "warn": "⚠️", "err": "❌"}


def health_lines() -> Tuple[str, List]:
    report = run_health_check()
    title = "/health — bot component status"
    lines: List = []
    overall = "ok"
    for key, label in _HEALTH_LABELS.items():
        item = report.get(key) or {"status": "err", "detail": "no result"}
        status = item.get("status", "err")
        icon = _STATUS_ICON.get(status, "❓")
        lines.append(f"{icon} {label}: {item.get('detail', '')}")
        if status == "err":
            overall = "err"
        elif status == "warn" and overall != "err":
            overall = "warn"
    lines.append("__HR__")
    lines.append(f"Overall: {_STATUS_ICON.get(overall, '❓')} {overall.upper()}")
    return title, lines


def attempt_self_heal(current_pid: Optional[int] = None) -> Tuple[str, List]:
    """Run health check, then attempt to fix any failing/warn components.
    Returns (title, lines) ready for reply_post."""
    fixes_applied: List[str] = []
    still_broken: List[str] = []

    report = run_health_check()

    # Fix 1: refresh JWT — re-load aime_env_refresh.json into os.environ so
    # subsequent checks see the freshest token AIME has dropped to disk.
    jwt = report.get("jwt") or {}
    if jwt.get("status") in ("warn", "err"):
        try:
            snapshot = load_json_file(AIME_ENV_REFRESH_FILE, {}) or {}
            updated = 0
            for k, v in snapshot.items():
                if isinstance(v, str) and v and os.environ.get(k) != v:
                    os.environ[k] = v
                    updated += 1
            new_jwt = _check_jwt_credentials()
            if new_jwt.get("status") == "ok":
                fixes_applied.append(f"🔑 Refreshed JWT from aime_env_refresh.json ({updated} key(s) updated)")
                report["jwt"] = new_jwt
            else:
                still_broken.append(
                    f"🔑 JWT still {new_jwt.get('status')}: {new_jwt.get('detail', '')} "
                    f"— AIME runtime auto-refreshes; re-run /fix later or wait for next refresh cycle"
                )
        except Exception as exc:
            still_broken.append(f"🔑 JWT refresh attempt failed: {exc!r}")

    # Fix 2: restart daemon if down.
    daemon = report.get("daemon") or {}
    if daemon.get("status") == "err":
        try:
            new_pid = restart_daemon(current_pid=current_pid)
            fixes_applied.append(f"🔄 Restarted callback daemon — new PID {new_pid}")
        except Exception as exc:
            still_broken.append(f"🔄 Daemon restart failed: {exc!r}")

    # Re-run health check after fixes for a fresh snapshot (skip restart-only
    # reload to avoid racing the new daemon).
    final = run_health_check()

    title = "/fix — self-heal report"
    lines: List = []
    if fixes_applied:
        lines.append("Fixes applied:")
        for f in fixes_applied:
            lines.append(f"  • {f}")
    else:
        lines.append("No automatic fixes were necessary or possible.")
    lines.append("__HR__")

    needs_manual: List[str] = list(still_broken)
    for key, label in _HEALTH_LABELS.items():
        item = final.get(key) or {}
        if item.get("status") in ("warn", "err"):
            needs_manual.append(f"{_STATUS_ICON.get(item.get('status'),'❓')} {label}: {item.get('detail','')}")
    if needs_manual:
        lines.append("Still needs manual attention:")
        for n in needs_manual:
            lines.append(f"  • {n}")
    else:
        lines.append("✅ All components healthy after self-heal.")
    return title, lines


def _clean_manager_input(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^@+", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def parse_exclude_command(arg: str) -> Tuple[str, str]:
    match = re.match(r"(.+?)\s+from\s+([^\s]+)$", str(arg or "").strip(), flags=re.IGNORECASE)
    if not match:
        return "", ""
    manager = _clean_manager_input(match.group(1))
    label_uid = str(match.group(2) or "").strip()
    return manager, label_uid


def parse_include_command(arg: str) -> Tuple[str, str]:
    match = re.match(r"(.+?)\s+for\s+([^\s]+)$", str(arg or "").strip(), flags=re.IGNORECASE)
    if not match:
        return "", ""
    manager = _clean_manager_input(match.group(1))
    label_uid = str(match.group(2) or "").strip()
    return manager, label_uid


def exclude_manager_lines(manager: str, label_uid: str) -> Tuple[str, List]:
    ok, added = add_exclusion(label_uid, [manager])
    if not ok or not label_uid or not manager:
        return "/exclude", ["Usage: /exclude @ManagerName from [label_uid]"]
    return (
        "/exclude",
        [f"✅ {manager} will no longer be tagged for alerts related to label UID {label_uid} from now on."]
    )


def include_manager_lines(manager: str, label_uid: str) -> Tuple[str, List]:
    ok, removed = remove_exclusion(label_uid, [manager])
    if not ok or not label_uid or not manager:
        return "/include", ["Usage: /include @ManagerName for [label_uid]"]
    if removed:
        manager_label = removed[0]
    else:
        manager_label = manager
    return (
        "/include",
        [f"✅ {manager_label} has been re-added and will be tagged again for alerts related to label UID {label_uid}."]
    )


def exception_lines(manager: str = "") -> Tuple[str, List]:
    data = load_exclusions()
    manager = _clean_manager_input(manager)
    if manager:
        uids = manager_uids(manager, data)
        title = f"/exceptions — {manager}"
        if not uids:
            return title, [f"No exclusions found for {manager}."]
        lines: List = [f"{manager} is excluded from {len(uids)} label UID(s):", "__HR__"]
        for uid in uids:
            lines.append(f"• {uid}")
        return title, lines

    total = total_exclusion_count(data)
    title = "/exceptions — current manager exclusion list"
    if not data:
        return title, ["No manager exclusions are currently configured."]
    lines: List = [f"Total exclusions: {total}", "__HR__"]
    for uid in sorted(data.keys()):
        managers = data.get(uid) or []
        lines.append(f"🏷️ {uid} ({len(managers)})")
        for item in managers:
            lines.append(f"• {item}")
        lines.append("__HR__")
    if lines and lines[-1] == "__HR__":
        lines.pop()
    return title, lines


def status_lines(region: str) -> List:
    status = daemon_status()
    open_cases = get_open_cases(region)
    daemon_text = f"✅ Online (PID {status['pid']})" if status.get("running") else "❌ Offline"
    return [
        f"Region: {region}",
        f"Callback daemon: {daemon_text}",
        f"Last scan time: {region_last_scan(region)}",
        f"Pending/open cases: {len(open_cases)}",
    ]


def pending_lines(region: str, am_filter: Optional[str] = None) -> List:
    open_cases = get_open_cases(region)
    am_filter_clean = _clean_manager_input(am_filter or "")
    if am_filter_clean:
        needle = am_filter_clean.lower()
        filtered = []
        for row in open_cases:
            # Match against the BD column (the same proxy used by /claims),
            # and also the Label Manager column for flexibility.
            bd = (row.get("BD") or "").lower()
            lm = (row.get("Label Manager") or "").lower()
            if needle in bd or needle in lm:
                filtered.append(row)
        open_cases = filtered
        if not open_cases:
            return [f"No open cases found for {region} matching AM '{am_filter_clean}'."]
        header = f"Open {region} cases assigned to '{am_filter_clean}': ({len(open_cases)})"
    else:
        if not open_cases:
            return [f"No open cases found for {region}."]
        header = f"Open cases for {region}: ({len(open_cases)})"
    lines: List = [header, "__HR__"]
    for row in open_cases:
        bullet = _case_bullet(row)
        if not am_filter_clean:
            am = _am_name(row)
            bullet = f"{bullet} — AM: {am}"
        lines.append(bullet)
    return lines


def start_scan_in_background(region: str, message_id: str) -> None:
    def _worker():
        try:
            summary = manual_scan_region(region)
            lines: List = [
                f"Region: {region}",
                f"Fetched: {summary['fetched']}",
                f"Parsed candidates: {summary['parsed_candidates']}",
                f"Posted: {summary['posted']}",
                f"Skipped duplicate: {summary['skipped_duplicate']}",
                f"Skipped not qualifying: {summary['skipped_not_qualifying']}",
            ]
            if summary.get("posted_items"):
                lines.append("__HR__")
                lines.append("Posted items:")
                for item in summary["posted_items"]:
                    lines.append(
                        f"• UPC {item.get('upc')} — Artist: {item.get('artist')} — Title: {item.get('title')}"
                    )
            reply_post(message_id, f"/scan — {region} scan complete", lines)
        except Exception as exc:
            reply_post(message_id, f"/scan — {region} scan failed", [repr(exc)])

    threading.Thread(target=_worker, daemon=True).start()
