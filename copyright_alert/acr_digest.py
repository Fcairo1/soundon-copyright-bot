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
REGION_FLAG = {"BR": "🇧🇷", "US": "🇺🇸", "SPLA": "🌎"}
TOP_N_COMBINED = 5

DECISION_HEADER_ALIASES = ("Review Result", "Decision")
TITLE_HEADER = "SO Song Title"
SONG_ID_HEADER = "SO Song ID"
DO_NOT_CLAIM_TAB_NAME = "Do Not Claim"
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


def case_key(song_id: str, song_title: str, matched_isrc: str, matched_title: str) -> str:
    """Same key the dashboard uses for a (SoundOn song, matched track) pair —
    acrcloud-dashboard's caseKey()/data._case_key. Keep these in sync."""
    return f"{(song_id or song_title or '').strip()}|{(matched_isrc or matched_title or '').strip()}"


def read_do_not_claim_keys() -> set:
    """Keys the team marked "Do NOT claim" on the dashboard (an append-only
    mark/unmark event log in the "Do Not Claim" tab of the Takedown Stream
    Tracker workbook — see acrcloud-dashboard/no_claim_data.py). A key counts
    only if its LAST event is a mark. If the tab can't be read the digest
    still posts (just without the exclusion) rather than going silent."""
    try:
        tab = next((t for t in list_tabs(TAKEDOWN_STREAMS_SHEET_URL) if t["name"].strip() == DO_NOT_CLAIM_TAB_NAME), None)
        if not tab:
            return set()
        values = read_tab(TAKEDOWN_STREAMS_SHEET_URL, tab["sheet_id"])
    except Exception as exc:
        print(f"  ⚠ ACR digest: could not read the Do Not Claim tab ({exc!r}) — posting without that exclusion", flush=True)
        return set()
    if not values:
        return set()
    idx = _header_index(values[0])
    key_i, action_i = idx.get("key"), idx.get("action")
    if key_i is None or action_i is None:
        return set()
    marked = set()
    for row in values[1:]:
        key, action = _cell(row, key_i), _cell(row, action_i).lower()
        if not key:
            continue
        if action == "mark":
            marked.add(key)
        elif action == "unmark":
            marked.discard(key)
    return marked


STATUS_CHANGES_TAB_NAME = "Status Changes"


def _norm_status(value: str) -> str:
    v = (value or "").strip().lower()
    return v.capitalize() if v in ("online", "offline") else ""


def read_status_overrides() -> Dict[str, Dict[str, str]]:
    """Online/Offline corrections made on the dashboard (an append-only log in the
    "Status Changes" tab of the Takedown Stream Tracker workbook — see
    acrcloud-dashboard/status_data.py). Latest change per case key wins. Fails
    open: if the tab can't be read the sheet's own Status is used."""
    try:
        tab = next((t for t in list_tabs(TAKEDOWN_STREAMS_SHEET_URL) if t["name"].strip() == STATUS_CHANGES_TAB_NAME), None)
        if not tab:
            return {}
        values = read_tab(TAKEDOWN_STREAMS_SHEET_URL, tab["sheet_id"])
    except Exception as exc:
        print(f"  ⚠ ACR digest: could not read the Status Changes tab ({exc!r}) — using the sheet's Status as is", flush=True)
        return {}
    if not values:
        return {}
    idx = _header_index(values[0])
    key_i, new_i, old_i = idx.get("key"), idx.get("new_status"), idx.get("old_status")
    if key_i is None or new_i is None:
        return {}
    out: Dict[str, Dict[str, str]] = {}
    for row in values[1:]:
        key, status = _cell(row, key_i), _norm_status(_cell(row, new_i))
        if key and status:
            out[key] = {"status": status, "old_status": _norm_status(_cell(row, old_i))}
    return out


def effective_status(sheet_status: str, override) -> str:
    """Same rule as the dashboard (status_data.effective_status): the correction
    applies unless the cell was edited to a different Online/Offline value since."""
    if not override:
        return sheet_status
    sheet_norm = _norm_status(sheet_status)
    if sheet_norm and sheet_norm not in (override["status"], override.get("old_status", "")):
        return sheet_status
    return override["status"]


def apply_status_overrides(rows: List[List[str]], overrides) -> List[List[str]]:
    """Copy of the grid with the Status column corrected."""
    if not rows or not overrides:
        return rows
    idx = _header_index(rows[0])
    status_i = idx.get(STATUS_HEADER)
    if status_i is None:
        return rows
    song_id_i, title_i = idx.get(SONG_ID_HEADER), idx.get(TITLE_HEADER)
    m_isrc_i, m_title_i = idx.get(MATCHED_ISRC_HEADER), idx.get(MATCHED_TITLE_HEADER)
    out = [rows[0]]
    for row in rows[1:]:
        key = case_key(_cell(row, song_id_i), _cell(row, title_i), _cell(row, m_isrc_i), _cell(row, m_title_i))
        o = overrides.get(key)
        if o:
            row = list(row) + [""] * (status_i + 1 - len(row))
            row[status_i] = effective_status(_cell(row, status_i), o)
        out.append(row)
    return out


def read_cycle_tab(sheet_url: str, sheet_id: str) -> List[List[str]]:
    """read_tab + the dashboard's Online/Offline corrections."""
    return apply_status_overrides(read_tab(sheet_url, sheet_id), read_status_overrides())


def build_digest_data(rows: List[List[str]], excluded_keys=frozenset(), always_td_ids=frozenset()) -> Dict[str, object]:
    """rows[0] is the header. Returns the aggregates the card needs.

    excluded_keys: (song, matched) pair keys marked "Do NOT claim" — left out
    of the enforcement list (and its counts) but still counted as offline if
    they went offline, and reported separately as do_not_claim.
    always_td_ids: SoundOn user IDs whose Other Party cases are always taken
    down — those entries are flagged "always_td" (shown with ⚡) and counted
    in always_td_total."""
    header = rows[0]
    idx = _header_index(header)
    decision_i = _find(idx, *DECISION_HEADER_ALIASES)
    title_i, region_i = idx.get(TITLE_HEADER), idx.get(REGION_HEADER)
    m_title_i, m_artists_i = idx.get(MATCHED_TITLE_HEADER), idx.get(MATCHED_ARTISTS_HEADER)
    m_label_i, m_isrc_i = idx.get(MATCHED_LABEL_HEADER), idx.get(MATCHED_ISRC_HEADER)
    status_i, streams_i = idx.get(STATUS_HEADER), idx.get(STREAMS_HEADER)
    song_id_i = idx.get(SONG_ID_HEADER)
    user_i = idx.get("SO User ID")

    # "Reviewed" = every match ACRCloud surfaced in this tab, not just the
    # ones a human has classified — confirmed live on Aug3: only 299 of 2615
    # rows had any Decision value at all (the rest are backlog), and the
    # approved mockup's "reviewed" figure was the full 2,615.
    total_reviewed = len(rows) - 1
    escalated = 0
    offline_rows: List[Dict[str, object]] = []
    by_isrc: Dict[str, Dict[str, object]] = {}   # dedupe: same infringing track can appear under several SO rows
    do_not_claim_keys = set()

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
            "always_td": bool(always_td_ids) and re.sub(r"\D", "", _cell(row, user_i)) in always_td_ids,
        }
        pair_key = case_key(_cell(row, song_id_i), _cell(row, title_i), entry["isrc"], entry["title"])
        if pair_key in excluded_keys:
            do_not_claim_keys.add(pair_key)
        else:
            key = entry["isrc"] or (entry["title"], entry["artist"])
            if key in by_isrc and by_isrc[key]["always_td"]:
                entry["always_td"] = True
            if key not in by_isrc or streams > by_isrc[key]["streams"]:
                by_isrc[key] = entry
            elif entry["always_td"]:
                by_isrc[key]["always_td"] = True

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
        "do_not_claim": len(do_not_claim_keys),
        "always_td_total": sum(1 for e in by_isrc.values() if e["always_td"]),
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


def _combined_top_n(data: Dict[str, object], n: int = TOP_N_COMBINED) -> List[Dict[str, object]]:
    """Flatten needs_enforcement (already capped top-N per region) across all
    regions and take the overall top N by streams. Safe: an item in the global
    top N must already be in its own region's top-N-per-region list, so no
    region's real top pick can be missed by this flattening."""
    all_items = [e for items in data["needs_enforcement"].values() for e in items]
    all_items.sort(key=lambda e: -e["streams"])
    return all_items[:n]


def _enforcement_section_md(data: Dict[str, object]) -> str:
    """Combined (not per-region) top-N ranked list — the design the user
    approved 2026-09-28 after live-testing both a per-region-with-collapsibles
    layout (collapsed panels render with no visible expand affordance in real
    Lark — looks broken, not "tap to open") and this simpler flat ranking."""
    top = _combined_top_n(data)
    if not top:
        return "**🚩 Top enforcement targets**\nNone flagged this cycle."
    lines = ["**🚩 Top enforcement targets**"]
    for i, e in enumerate(top, start=1):
        flag = REGION_FLAG.get(e["region"], "")
        label = f" [{e['label']}]" if e["label"] else ""
        bolt = "⚡ " if e.get("always_td") else ""
        lines.append(f"**{i}.** {bolt}{e['title']} — {e['artist']}{label} {flag} · {e['streams']:,} streams")
    if any(e.get("always_td") for e in top):
        lines.append("⚡ = always take down (ops requests it, no manager decision)")
    return "\n".join(lines)


def _stat_tile(value: str, label: str) -> dict:
    return {"tag": "column", "width": "weighted", "weight": 1, "background_style": "grey", "elements": [
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**{value}**\n{label}"}}
    ]}


def _fmt_day(d) -> str:
    return f"{d:%a} {d.day} {d:%b}"


def _mention(name: str) -> str:
    """Lark @mention for a label manager's display name ('Mariana Vieira' ->
    mariana.vieira@bytedance.com, same rule tag_managers uses). Tags only
    notify members of the chat; others still see their name in bold."""
    from copyright_alert.tag_managers import MENTION_DOMAIN, _username_from_display
    user = _username_from_display(name)
    if not user or name == "No manager on file":
        return f"**{name}**"
    return f'<at email="{user}@{MENTION_DOMAIN}">{name}</at>'


def _waiting_elements(waiting: Dict[str, object]) -> List[dict]:
    managers = waiting.get("managers") or []
    closes = waiting["closes"]
    elements: List[dict] = [{"tag": "hr"}]
    head = f"**🕒 Waiting on managers** · window closes **{_fmt_day(closes)}**"
    if managers:
        tags = "  ·  ".join(f"{_mention(n)} **{c}**" for n, c in managers)
        body = f"{head}\n{tags}"
    else:
        body = f"{head}\nNo manager has cases pending."
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": body}})
    rule = f"All content not marked 🚫 **Do NOT claim** by **{_fmt_day(closes)}** will be claimed."
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": rule}})
    if managers:
        elements.append({"tag": "action", "actions": [{
            "tag": "button", "text": {"tag": "plain_text", "content": "Review & mark my cases"},
            "type": "default", "url": DASHBOARD_URL,
        }]})
    return elements


def build_card(tab_name: str, data: Dict[str, object], recovery_lines: List[str],
               waiting: Optional[Dict[str, object]] = None) -> dict:
    """waiting (optional, from claim_window.waiting_summary) adds the "Waiting
    on managers" block and, with waiting["weekly"], the Monday re-post variant.
    Without it the card is the plain digest (e.g. a cycle with no window)."""
    weekly = bool(waiting and waiting.get("weekly"))
    do_not_claim = int(data.get("do_not_claim") or 0)
    always_td = int(data.get("always_td_total") or 0)

    bottom = []
    if always_td:
        open_ = waiting.get("always_td_open") if waiting else None
        if open_ is None:
            bottom.append(f"⚡ **{always_td} always take down** to request (ops)")
        else:
            bottom.append(f"⚡ **{always_td} always take down**: {always_td - open_} requested, {open_} to request")
    bottom.append(
        f"✅ **{data['offline_total']} taken offline** this cycle" if data["offline_total"]
        else "✅ **None taken offline** this cycle"
    )
    if not weekly:
        bottom.append(f"⚠️ **{data['escalated']} escalated** to account managers")
    if do_not_claim:
        bottom.append(f"🚫 **{do_not_claim} marked do not claim**")

    elements: List[dict] = []
    if weekly:
        left = waiting.get("days_left")
        left_txt = f"{left} workday{'s' if left != 1 else ''} left" if isinstance(left, int) and left >= 0 else "window open"
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": f"**Weekly update** · {left_txt}"}})
    elements += [
        {"tag": "column_set", "flex_mode": "none", "columns": [
            _stat_tile(f"{data['total_reviewed']:,}", "Reviewed"),
            _stat_tile(f"{data['flagged_total']} 🚩", "To claim"),
            _stat_tile(f"{data['escalated']} ⚠️", "Escalated"),
        ]},
        {"tag": "column_set", "flex_mode": "none", "columns": [
            _stat_tile(f"{REGION_FLAG.get(r, '')} {r}".strip(), f"{data['enforcement_counts'].get(r, 0)} to claim")
            for r in REGION_ORDER
        ]},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": _enforcement_section_md(data)}},
        {"tag": "action", "actions": [{
            "tag": "button", "text": {"tag": "plain_text", "content": "Review and file takedowns"},
            "type": "primary", "url": DASHBOARD_URL,
        }]},
    ]
    if waiting:
        elements += _waiting_elements(waiting)
    elements += [
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": "  ·  ".join(bottom)}},
    ]
    # Recovery tracking is real but usually empty (nothing checked long enough
    # yet) — only take up card space once there's something to show.
    if recovery_lines:
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content":
            "**📈 Original-track recovery**\n" + "\n".join(recovery_lines)}})
    footer = ("Posts when a new scan cycle appears, then every Monday until the window closes · full breakdown in the dashboard"
              if waiting else "Posts automatically when a new scan cycle appears · full per-region breakdown in the dashboard")
    elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": footer}]})

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "red",
            "title": {"tag": "plain_text", "content": f"🛡️ ACRCloud scan digest — {tab_name} cycle"},
        },
        "elements": elements,
    }


def _register_claim_window(cycle: str) -> None:
    """Start the managers' 5-workday window for this cycle. Never lets a
    registry hiccup undo a digest that already posted."""
    try:
        from copyright_alert import claim_window
        res = claim_window.register_window(cycle)
        print(f"  ✓ Claim window for {cycle}: {json.dumps(res, ensure_ascii=False)}", flush=True)
    except Exception as exc:
        print(f"  ⚠ Could not register the claim window for {cycle} ({exc!r}) — "
              f"run `python3 -m copyright_alert.claim_window --register \"{cycle}\"`", flush=True)


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
            rows = read_cycle_tab(ACR_SHEET_URL, tab["sheet_id"])
        except Exception as exc:
            result["skipped_invalid"].append({"name": name, "error": repr(exc)})
            continue
        if not rows or not is_valid_cycle_tab(_header_index(rows[0])):
            result["skipped_invalid"].append({"name": name, "error": "not a valid cycle tab (missing headers or empty)"})
            continue

        always_td_ids, waiting = frozenset(), None
        try:
            from copyright_alert import claim_window
            inputs = claim_window.read_inputs()
            always_td_ids = frozenset(inputs["accounts"])
            waiting = claim_window.waiting_summary(rows, inputs, posted=claim_window.today_brt())
        except Exception as exc:  # the claim extras must never stop the digest
            print(f"  ⚠ ACR digest: claim-window extras unavailable ({exc!r}) — posting the plain digest", flush=True)
        data = build_digest_data(rows, excluded_keys=read_do_not_claim_keys(), always_td_ids=always_td_ids)
        recovery_lines = build_recovery_lines()
        card = build_card(name, data, recovery_lines, waiting=waiting)
        if not dry_run:
            ra.post_card(card, chat_id=CONTENT_SAFETY_CHAT_ID, context=f"acr_digest:{name}")
        digested.add(name)
        result["posted"].append({"name": name, "flagged_total": data["flagged_total"], "escalated": data["escalated"]})
        if not dry_run:
            _register_claim_window(name)

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
