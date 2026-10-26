#!/usr/bin/env python3
"""
copyright_alert/run_log.py

Append-only run log in a private Lark sheet ("Bot run log" > tab "Reports") so a
daily run's outcome can be read by someone who was not watching the chat.

A run writes TWO rows that share one Report ID:
  * STARTED  — the moment the run begins (so a run that is killed mid-way is visible
               as a STARTED row with no final row);
  * OK / WARNING / FAILED — in the end, including clean aborts (e.g. JWT start gate).

Strictly best-effort: a failed write is logged and ignored, it must never break a run.
Disabled unless RUN_LOG_SHEET_URL (and RUN_LOG_SHEET_ID) are set in the environment, so
nothing about the sheet lives in this public repo. RUN_LOG_ENABLED=0 switches it off.

Columns (A-Q, same as the sheet header): Report ID | Logged at (BRT) | Project / bot |
Automation | Region / scope | Status | Problem? | Summary | Problem details |
Key numbers | Run started (BRT) | Run ended (BRT) | Log / reference | Chat message link |
Suggested action | Reviewed by Claude (BRT) | Claude notes.
Never put tokens, secrets or personal data in a row (counts, UPCs and times only).
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

PROJECT = "SoundOn Copyright Bot"
HEADERS = [
    "Report ID", "Logged at (BRT)", "Project / bot", "Automation", "Region / scope", "Status",
    "Problem?", "Summary", "Problem details", "Key numbers", "Run started (BRT)",
    "Run ended (BRT)", "Log / reference", "Chat message link", "Suggested action",
    "Reviewed by Claude (BRT)", "Claude notes",
]
_BRT = ZoneInfo("America/Sao_Paulo")
_MAX_CELL = 1500


def _now() -> datetime:
    return datetime.now(_BRT)


def _fmt(dt: Optional[datetime], with_brt: bool = False) -> str:
    if dt is None:
        return ""
    text = dt.astimezone(_BRT).strftime("%Y-%m-%d %H:%M")
    return f"{text} (BRT)" if with_brt else text


def _cell(value) -> str:
    text = str(value if value is not None else "").replace("\r", " ").replace("\n", " ").strip()
    return text[:_MAX_CELL]


def enabled() -> bool:
    if os.environ.get("RUN_LOG_ENABLED", "1").strip().lower() in ("0", "false", "no", "off"):
        return False
    return bool(os.environ.get("RUN_LOG_SHEET_URL", "").strip())


def append_row(row: List[str]) -> bool:
    """Append one row (best-effort). Never raises."""
    if not enabled():
        return False
    row = [_cell(c) for c in row]
    row += [""] * (len(HEADERS) - len(row))
    cmd = ["lark-cli", "sheets", "+append", "--url", os.environ["RUN_LOG_SHEET_URL"].strip()]
    sheet_id = os.environ.get("RUN_LOG_SHEET_ID", "").strip()
    if sheet_id:
        cmd += ["--sheet-id", sheet_id]
    cmd += ["--values", json.dumps([row], ensure_ascii=False)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            print(f"  ⚠ run_log append failed rc={proc.returncode}: {(proc.stderr or proc.stdout)[:300]}", flush=True)
            return False
        return True
    except Exception as exc:  # noqa: BLE001 - never break the run
        print(f"  ⚠ run_log append error: {exc!r}", flush=True)
        return False


class RunLog:
    """One daily-workflow run. Create it, call start(), then finish() exactly once."""

    def __init__(self, automation: str, region: str, *, project: str = PROJECT, log_file: str = ""):
        self.automation = automation
        self.region = region or "-"
        self.project = project
        self.log_file = os.path.basename(str(log_file or ""))
        self.started_at = _now()
        slug = "".join(ch if ch.isalnum() else "-" for ch in automation.lower()).strip("-")
        self.report_id = f"soundon-{self.region.lower()}-{slug}-{self.started_at.strftime('%Y%m%d-%H%M')}"
        self._finished = False

    def _row(self, status: str, summary: str, *, problem: str = "", numbers: Optional[Dict] = None,
             ended: Optional[datetime] = None, action: str = "") -> List[str]:
        key_numbers = "; ".join(f"{k}={v}" for k, v in (numbers or {}).items())
        return [
            self.report_id, _fmt(_now(), True), self.project, self.automation, self.region, status,
            "Yes" if status in ("WARNING", "FAILED") else "No", summary, problem, key_numbers,
            _fmt(self.started_at), _fmt(ended), self.log_file, "", action, "", "",
        ]

    def start(self, *, token_minutes: Optional[int] = None, mode: str = "") -> bool:
        numbers = {}
        if token_minutes is not None:
            numbers["token_min_at_start"] = token_minutes
        if mode:
            numbers["mode"] = mode
        return append_row(self._row("STARTED", f"{self.automation} started.", numbers=numbers))

    def finish(self, status: str, summary: str, *, problem: str = "", numbers: Optional[Dict] = None,
               action: str = "") -> bool:
        if self._finished:
            return False
        self._finished = True
        ended = _now()
        numbers = dict(numbers or {})
        numbers["duration_min"] = round((ended - self.started_at).total_seconds() / 60, 1)
        return append_row(self._row(status, summary, problem=problem, numbers=numbers, ended=ended, action=action))


def summarize_results(results: dict, parts_completed: List[str]):
    """Turn daily_workflow's results dict into (status, summary, problem, numbers)."""
    results = results or {}
    scan = results.get("scan") or {}
    errors = {name: sec.get("error") for name, sec in results.items()
              if isinstance(sec, dict) and sec.get("error")}
    numbers = {
        "claims_examined": scan.get("examined", 0),
        "candidates": scan.get("parsed_candidates", 0),
        "cards_posted": scan.get("posted", 0),
        "section_errors": len(errors),
        "parts_completed": ",".join(parts_completed),
    }
    if "scan" in errors:
        status = "FAILED"
    elif errors:
        status = "WARNING"
    else:
        status = "OK"
    if status == "OK":
        summary = f"Completed: {numbers['cards_posted']} card(s) posted from {numbers['candidates']} candidate(s)."
        problem = ""
    else:
        summary = (f"Completed with {len(errors)} section error(s): {', '.join(errors)}."
                   if status == "WARNING" else "The inbox scan failed.")
        problem = "; ".join(f"{k}: {v}" for k, v in errors.items())
    return status, summary, problem, numbers
