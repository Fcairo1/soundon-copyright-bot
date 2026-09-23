#!/usr/bin/env python3
"""ACR scan digest — posts a summary card to the Content Safety + Protection
group once per ACRCloud scan cycle.

Each cycle lands as a new TAB in the ACR results sheet (human-named, e.g.
"Aug3", "Aug19" — no fixed naming convention, so a tab is recognized by its
HEADERS, not its name: it must have "SO Song Title" and a decision column
under one of a few known names ("Review Result" or "Decision" — this drifted
between real cycles, confirmed live). A tab lacking those is either a utility
tab (Flags, Sheet4) or a broken/in-progress cycle (confirmed live: "Sep22" was
a bare "#REF!" with no real rows) and is skipped without being marked seen,
so a later run retries it once it's actually populated.

"Needs enforcement" is grouped BR / US / SPLA (the same region routing table
used everywhere else in this bot) and capped to the top 5 by the INFRINGING
track's streams in each region — always exactly 5 (or fewer if a region has
fewer flagged cases), never threshold-filtered (confirmed with the user: a
streams threshold does not reliably control volume — a real cycle had every
single flagged case clear 10,000 streams).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copyright_alert import run_alert as ra  # noqa: E402
from copyright_alert.lark_auth import extract_sheet_values, sheet_values_api  # noqa: E402

ACR_SHEET_URL = "https://bytedance.sg.larkoffice.com/sheets/SFgusRyethYRyJtuZlPln2Y4gfd"
TAKEDOWN_STREAMS_SHEET_URL = "https://bytedance.sg.larkoffice.com/sheets/AbuUsJeSZhTkKQtpHBUlLyNhgNc"
TAKEDOWN_STREAMS_SHEET_ID = "75bc80"
CONTENT_SAFETY_CHAT_ID = "oc_b9df680c89797c8b35398e302a054c24"
DASHBOARD_URL = "https://acrcloud-dashboard.onrender.com/content-protection"

DIGEST_STATE_FILE = ROOT / "runtime" / "acr_digest_state.json"
TOP_N_PER_REGION = 5
READ_RANGE = "A1:BZ5000"

COUNTRY_TO_REGION = {
    "BR": "BR",
    "US": "US", "CA": "US", "AU": "US", "NZ": "US",
    "MX": "SPLA", "CL": "SPLA", "CO": "SPLA", "AR": "SPLA", "ES": "SPLA", "PR": "SPLA", "PE": "SPLA",
}
REGION_ORDER = ("BR", "US", "SPLA")

DECISION_HEADER_ALIASES = ("Review Result", "Decision")
TITLE_HEADER = "SO Song Title"
REGION_HEADER = "SO Region"
MATCHED_TITLE_HEADER = "Matched Title"
MATCHED_ARTISTS_HEADER = "Matched Artists"
MATCHED_LABEL_HEADER = "Matched Label"
MATCHED_ISRC_HEADER = "Matched ISRC"
STATUS_HEADER = "Status"
STREAMS_HEADER = "Streamings"   # NOTE: distinct from "SO Streamings" (SoundOn's own track)


def _norm(value) -> str:
    return str(value or "").strip()


def _header_index(header: Sequence[str]) -> Dict[str, int]:
    return {_norm(h): i for i, h in enumerate(header) if _norm(h)}


def _find(idx: Dict[str, int], *names: str) -> Optional[int]:
    for name in names:
        if name in idx:
            return idx[name]
    return None


def _cell(row: Sequence[object], i: Optional[int]) -> str:
    if i is None or i >= len(row):
        return ""
    return _norm(row[i])


def _clean_names(raw: str) -> str:
    """The sheet's artist-list cells arrive as a JSON array already broken by
    naive CSV splitting (e.g. '["Detagaz","MC Guh SR"]' -> two ragged pieces).
    Strip stray brackets/quotes and re-join distinct tokens."""
    parts = re.split(r'["\[\],]+', raw)
    parts = [p.strip() for p in parts if p.strip()]
    seen = []
    for p in parts:
        if p not in seen:
            seen.append(p)
    return " and ".join(seen) if seen else raw


def _parse_streams(raw: str) -> int:
    text = raw.replace(",", "").replace('"', "").strip()
    try:
        return int(float(text)) if text else 0
    except ValueError:
        return 0


def list_tabs(sheet_url: str) -> List[Dict[str, str]]:
    """[{"sheet_id": ..., "name": ...}, ...] via lark-cli (same pattern as
    account_release_counts.resolve_first_sheet_id — this bot's lark_auth has
    no tab-listing primitive of its own)."""
    result = subprocess.run(
        ["lark-cli", "sheets", "+workbook-info", "--url", sheet_url],
        cwd=str(ROOT), capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stdout + result.stderr)[:1000])
    payload = json.loads(result.stdout[result.stdout.find("{"):])
    sheets = ((payload.get("data") or {}).get("sheets")) or []
    return [{"sheet_id": str(s.get("sheet_id") or s.get("sheetId") or ""), "name": str(s.get("sheet_name") or s.get("title") or "")} for s in sheets]


def read_tab(sheet_url: str, sheet_id: str) -> List[List[str]]:
    values = extract_sheet_values(sheet_values_api("GET", sheet_url, sheet_id, READ_RANGE))
    return [[_norm(c) for c in row] for row in values]


def _load_state() -> Dict[str, object]:
    try:
        return json.loads(DIGEST_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"digested_tabs": []}


def _save_state(state: Dict[str, object]) -> None:
    DIGEST_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    DIGEST_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def is_valid_cycle_tab(header_idx: Dict[str, int]) -> bool:
    return TITLE_HEADER in header_idx and _find(header_idx, *DECISION_HEADER_ALIASES) is not None


def _is_other_party(decision: str) -> bool:
    return "other party" in decision.lower()


def _is_additional_review(decision: str) -> bool:
    return "additional review" in decision.lower()


def _is_offline(status: str) -> bool:
    return status.strip().lower() == "offline"


def build_digest_data(rows: List[List[str]]) -> Dict[str, object]:
    """rows[0] is the header. Returns the aggregates the card needs."""
    header = rows[0]
    idx = _header_index(header)
    decision_i = _find(idx, *DECISION_HEADER_ALIASES)
    title_i, region_i = idx.get(TITLE_HEADER), idx.get(REGION_HEADER)
    m_title_i, m_artists_i = idx.get(MATCHED_TITLE_HEADER), idx.get(MATCHED_ARTISTS_HEADER)
    m_label_i, m_isrc_i = idx.get(MATCHED_LABEL_HEADER), idx.get(MATCHED_ISRC_HEADER)
    status_i, streams_i = idx.get(STATUS_HEADER), idx.get(STREAMS_HEADER)

    # "Reviewed" = every match ACRCloud surfaced in this tab, not just the
    # ones a human has classified — confirmed live on Aug3: only 299 of 2615
    # rows had any Decision value at all (the rest are backlog), and the
    # approved mockup's "reviewed" figure was the full 2,615.
    total_reviewed = len(rows) - 1
    escalated = 0
    offline_rows: List[Dict[str, object]] = []
    by_isrc: Dict[str, Dict[str, object]] = {}   # dedupe: same infringing track can appear under several SO rows

    for row in rows[1:]:
        decision = _cell(row, decision_i)
        if not decision:
            continue
        if _is_additional_review(decision):
            escalated += 1
        if not _is_other_party(decision):
            continue

        streams = _parse_streams(_cell(row, streams_i))
        country = _cell(row, region_i)
        region = COUNTRY_TO_REGION.get(country, country or "Other")
        entry = {
            "title": _cell(row, m_title_i), "artist": _clean_names(_cell(row, m_artists_i)),
            "label": _cell(row, m_label_i), "isrc": _cell(row, m_isrc_i),
            "streams": streams, "region": region,
        }
        key = entry["isrc"] or (entry["title"], entry["artist"])
        if key not in by_isrc or streams > by_isrc[key]["streams"]:
            by_isrc[key] = entry

        if _is_offline(_cell(row, status_i)):
            offline_rows.append({
                "title": _cell(row, title_i), "matched_title": entry["title"],
                "matched_artist": entry["artist"], "streams": streams,
            })

    by_region: Dict[str, List[Dict[str, object]]] = {}
    for entry in by_isrc.values():
        by_region.setdefault(entry["region"], []).append(entry)
    needs_enforcement = {
        region: sorted(items, key=lambda e: -e["streams"])[:TOP_N_PER_REGION]
        for region, items in by_region.items()
    }
    enforcement_counts = {region: len(items) for region, items in by_region.items()}

    offline_rows.sort(key=lambda r: -r["streams"])
    seen_offline = set()
    deduped_offline = []
    for r in offline_rows:
        key = (r["title"], r["matched_title"])
        if key in seen_offline:
            continue
        seen_offline.add(key)
        deduped_offline.append(r)

    return {
        "total_reviewed": total_reviewed,
        "flagged_total": len(by_isrc),
        "escalated": escalated,
        "needs_enforcement": needs_enforcement,
        "enforcement_counts": enforcement_counts,
        "offline_total": len(deduped_offline),
        "offline_top": deduped_offline[0] if deduped_offline else None,
    }


def build_recovery_lines() -> List[str]:
    """Top original-track recovery entries from the Takedown Stream Tracker —
    only rows soundon-copyright-bot's takedown_stream_tracker.py has actually
    checked at least once (last_checked set)."""
    try:
        values = read_tab(TAKEDOWN_STREAMS_SHEET_URL, TAKEDOWN_STREAMS_SHEET_ID)
    except Exception:
        return []
    if not values:
        return []
    idx = _header_index(values[0])
    song_i, delta_abs_i, delta_pct_i, checked_i = idx.get("song"), idx.get("streams_delta_abs"), idx.get("streams_delta_pct"), idx.get("last_checked")
    entries = []
    for row in values[1:]:
        if not _cell(row, checked_i):
            continue
        song = _cell(row, song_i)
        try:
            delta_abs = float(_cell(row, delta_abs_i) or 0)
            delta_pct = float(_cell(row, delta_pct_i) or 0)
        except ValueError:
            continue
        if song:
            entries.append((song, delta_abs, delta_pct))
    entries.sort(key=lambda e: -abs(e[2]))
    lines = []
    for song, delta_abs, delta_pct in entries[:3]:
        sign = "+" if delta_abs >= 0 else ""
        lines.append(f"{song}: {sign}{delta_abs:,.0f} streams ({sign}{delta_pct:.1f}%) since takedown")
    return lines


def _enforcement_section_md(data: Dict[str, object]) -> str:
    lines = ["**🚩 Needs enforcement — top 5 by streams, per region**"]
    any_region = False
    for region in REGION_ORDER:
        items = data["needs_enforcement"].get(region, [])
        if not items:
            continue
        any_region = True
        total = data["enforcement_counts"].get(region, len(items))
        lines.append(f"\n**{region} — {total} flagged**")
        for e in items:
            label = f" [{e['label']}]" if e["label"] else ""
            lines.append(f"{e['title']} — {e['artist']}{label} · {e['streams']:,} streams")
    if not any_region:
        lines.append("None flagged this cycle.")
    return "\n".join(lines)


def build_card(tab_name: str, data: Dict[str, object], recovery_lines: List[str]) -> dict:
    offline = data["offline_top"]
    offline_md = (
        f"**✅ Taken offline this cycle — {data['offline_total']}**\n"
        + (f"{offline['title']} ↔ {offline['matched_title']}, {offline['matched_artist']} · "
           f"{offline['streams']:,} streams · now offline"
           + (f"\n+ {data['offline_total'] - 1} more taken offline" if data["offline_total"] > 1 else "")
           if offline else "None taken offline this cycle.")
    )
    recovery_md = "**📈 Original-track recovery**\n" + (
        "\n".join(recovery_lines) if recovery_lines
        else "No takedowns tracked long enough yet to show a streams change."
    )
    escalated_md = (
        f"**⚠️ Escalated to account managers**\n{data['escalated']} additional-review case(s), "
        "direction unclear · age not yet tracked"
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "red",
            "title": {"tag": "plain_text", "content": f"🛡️ ACRCloud scan digest — {tab_name} cycle"},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": (
                f"**Scan overview**\n{data['total_reviewed']:,} reviewed · "
                f"{data['flagged_total']} flagged (other party) · {data['escalated']} escalated"
            )}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": _enforcement_section_md(data)}},
            {"tag": "action", "actions": [{
                "tag": "button", "text": {"tag": "plain_text", "content": "Review and file takedowns in the dashboard"},
                "type": "primary", "url": DASHBOARD_URL,
            }]},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": offline_md}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": recovery_md}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": escalated_md}},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": "Posts automatically when a new scan cycle appears"}]},
        ],
    }


def run_daily_check(*, dry_run: bool = False) -> Dict[str, object]:
    state = _load_state()
    digested = set(state.get("digested_tabs") or [])
    tabs = list_tabs(ACR_SHEET_URL)
    result = {"tabs_seen": [t["name"] for t in tabs], "posted": [], "skipped_invalid": [], "already_digested": []}

    for tab in tabs:
        name = tab["name"]
        if name in digested:
            result["already_digested"].append(name)
            continue
        try:
            rows = read_tab(ACR_SHEET_URL, tab["sheet_id"])
        except Exception as exc:
            result["skipped_invalid"].append({"name": name, "error": repr(exc)})
            continue
        if not rows or not is_valid_cycle_tab(_header_index(rows[0])):
            result["skipped_invalid"].append({"name": name, "error": "not a valid cycle tab (missing headers or empty)"})
            continue

        data = build_digest_data(rows)
        recovery_lines = build_recovery_lines()
        card = build_card(name, data, recovery_lines)
        if not dry_run:
            ra.post_card(card, chat_id=CONTENT_SAFETY_CHAT_ID, context=f"acr_digest:{name}")
        digested.add(name)
        result["posted"].append({"name": name, "flagged_total": data["flagged_total"], "escalated": data["escalated"]})

    if not dry_run:
        _save_state({"digested_tabs": sorted(digested)})
    return result


def run_daily_check_safe() -> Dict[str, object]:
    try:
        result = run_daily_check()
        print(f"  ✓ ACR digest check: {json.dumps(result, ensure_ascii=False)}", flush=True)
        return result
    except Exception as exc:
        print(f"  ✗ ACR digest check failed (non-fatal): {exc!r}", flush=True)
        return {"error": repr(exc)}


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Check for a new ACR scan cycle and post the digest")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run_daily_check(dry_run=args.dry_run), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
