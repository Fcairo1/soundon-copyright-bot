#!/usr/bin/env python3
"""One-off cleanup for the regional "Rights Confirmation" / "Metadata Corrections" tabs.

Two things went wrong in those tabs:

1. Foreign accounts. A notice whose account is outside BR/US/SPLA (e.g. Indonesia)
   was filed under whichever region's daily run scanned it (the old region
   fallback). Rows for such UPCs belong in NO tracker; rows for another AMS
   region belong in THAT region's tracker.
2. Duplicates. The append was blind, so a failure after it re-appended the same
   row on every daily run (one UPC reached 14+ identical rows).

``plan_cleanup`` is pure (rows + a UPC->region answer in, actions out) so it is
easy to test and to eyeball. ``run_cleanup`` is a dry run unless ``apply=True``;
with ``apply=True`` it first writes a JSON backup of every row of every tab it
will touch, then moves rows, then deletes rows (irreversible), then updates the
notice state files so the daily DM re-send follows the rows.

    python3 -m copyright_alert.notice_tab_cleanup rights            # dry run
    python3 -m copyright_alert.notice_tab_cleanup rights --apply
    python3 -m copyright_alert.notice_tab_cleanup metadata --apply
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from copyright_alert.run_alert import (
    AMS_ALERT_REGIONS,
    RUNTIME_DIR,
    _region_from_aeolus_row,
    batch_query_aeolus_rows_by_upc,
    choose_ams_row,
)

REGIONS = AMS_ALERT_REGIONS


# ── Pure planning ────────────────────────────────────────────────────────────
def _cell(row, i) -> str:
    return str(row[i]).strip() if i is not None and i < len(row) and row[i] is not None else ""


def _progress_score(header, row) -> int:
    """How far along a row is — the copy with real progress is the one to keep."""
    score = 0
    es, st, cm = (header.index(h) if h in header else None for h in ("Email Status", "Status", "Card Message ID"))
    if _cell(row, es):
        score += 4
    if _cell(row, st) not in ("", "Investigating", "🔍 Investigating", "New"):
        score += 2
    if _cell(row, cm) not in ("", "None"):
        score += 1
    return score


def plan_cleanup(tabs: Dict[str, dict], region_of: Callable[[str, str, str], Optional[str]],
                 protected: Optional[Dict[str, set]] = None) -> dict:
    """Decide what to do with every row.

    ``tabs``: {region: {"header": [...], "rows": [(sheet_row_number, [cells]), ...]}}
    ``region_of(tab_region, upc, user_id)``: the region the row belongs to — "BR"/"US"/
    "SPLA", None for an account outside AMS (belongs nowhere), or "?" when it can't
    be resolved (the row is left untouched).

    Actions: keep | keep_unknown | move (to another region's tab) | delete_foreign |
    delete_duplicate | protected (never touched).
    """
    protected = protected or {}
    entries = []   # one per row
    for tab_region, tab in tabs.items():
        header = tab["header"]
        upc_i = header.index("UPC") if "UPC" in header else None
        uid_i = header.index("User ID") if "User ID" in header else None
        date_i = header.index("Date Received") if "Date Received" in header else None
        for row_number, row in tab["rows"]:
            upc = _cell(row, upc_i)
            if not upc:
                continue
            entry = {"tab": tab_region, "row": row_number, "upc": upc, "date": _cell(row, date_i),
                     "score": _progress_score(header, row), "to": tab_region, "action": "keep"}
            if row_number in protected.get(tab_region, set()):
                entry["action"] = "protected"
            else:
                target = region_of(tab_region, upc, _cell(row, uid_i))
                if target == "?":
                    entry["action"] = "keep_unknown"
                elif target is None:
                    entry["action"] = "delete_foreign"
                else:
                    entry["to"] = target
                    entry["action"] = "keep" if target == tab_region else "move"
            entries.append(entry)

    # Duplicates: same destination + UPC + Date Received. Prefer a row already in the
    # right tab, then the most advanced one, then the earliest.
    groups: Dict[tuple, List[dict]] = {}
    for e in entries:
        if e["action"] in ("keep", "move"):
            groups.setdefault((e["to"], e["upc"], e["date"]), []).append(e)
    for members in groups.values():
        members.sort(key=lambda e: (e["tab"] != e["to"], -e["score"], e["row"]))
        for e in members[1:]:
            e["action"] = "delete_duplicate"
    # A winner that lives in the wrong tab must be moved; one already in place is kept.
    return {"entries": entries, "summary": _summarize(entries)}


def _summarize(entries) -> dict:
    out: Dict[str, int] = {}
    for e in entries:
        out[e["action"]] = out.get(e["action"], 0) + 1
    out["rows"] = len(entries)
    return out


# ── Sheet / state adapters ───────────────────────────────────────────────────
def _adapter(kind: str) -> dict:
    if kind == "rights":
        from copyright_alert import rights_confirmation_notice as m
        return {"module": m, "url": m._rights_tracker_url, "read": m._read_rights_tracker_rows,
                "state_file": m.STATE_FILE, "prefer_sources": None,
                "mark_foreign": lambda rec: rec.update({"answered": True, "closed_reason": "out_of_region"}),
                "protected": {}}
    if kind == "metadata":
        from copyright_alert import metadata_notice as m
        return {"module": m, "url": m._metadata_tracker_url, "read": m._read_metadata_tracker_rows,
                "state_file": m.STATE_FILE, "prefer_sources": None,
                "mark_foreign": lambda rec: rec.update({"resolved": True, "closed_reason": "out_of_region"}),
                "protected": m._METADATA_PROTECTED_ROWS}
    raise ValueError(f"unknown kind {kind!r} (use 'rights' or 'metadata')")


def _read_tabs(ad) -> Dict[str, dict]:
    tabs = {}
    for region in REGIONS:
        header, _data_rows, sheet_id = ad["read"](region)
        # The readers drop blank rows, which would shift row numbers; number the rows
        # from the raw CSV instead so deletes hit exactly the right sheet rows.
        tabs[region] = {"header": header, "sheet_id": sheet_id, "rows": _number_rows(ad, region, sheet_id)}
    return tabs


def _number_rows(ad, region, sheet_id):
    """[(sheet_row_number, cells)] using the raw CSV (blank rows kept) so numbers are exact."""
    import csv
    import io
    m = ad["module"]
    result = m._run_lark_sheets(["+csv-get", "--url", ad["url"](region), "--sheet-id", sheet_id,
                                 "--include-row-prefix=false"])
    rows = list(csv.reader(io.StringIO((result.get("data") or {}).get("annotated_csv") or "")))
    return [(i + 1, r) for i, r in enumerate(rows) if i > 0 and any(c.strip() for c in r)]


def _make_region_of(ad, tabs):
    upcs = sorted({_cell(r, tab["header"].index("UPC")) for tab in tabs.values()
                   for _n, r in tab["rows"] if "UPC" in tab["header"]} - {""})
    rows_by_upc = batch_query_aeolus_rows_by_upc(upcs) if upcs else {}

    def region_of(tab_region, upc, user_id):
        rows = rows_by_upc.get(upc) or []
        if not rows:
            return "?"
        if user_id:   # exact: the account this row was written for
            for r in rows:
                if str(r.get("uid") or r.get("user_id") or "").strip() == user_id:
                    return _region_from_aeolus_row(r) or None
        region, _row, status = choose_ams_row(rows, ad["prefer_sources"])
        return region if status == "ok" else (None if status == "out_of_region" else "?")

    return region_of


# ── Execution ────────────────────────────────────────────────────────────────
def run_cleanup(kind: str, *, apply: bool = False) -> dict:
    ad = _adapter(kind)
    m = ad["module"]
    tabs = _read_tabs(ad)
    plan = plan_cleanup(tabs, _make_region_of(ad, tabs), ad["protected"])
    entries = plan["entries"]
    result = {"kind": kind, "apply": apply, "summary": plan["summary"],
              "moves": [e for e in entries if e["action"] == "move"],
              "foreign": [e for e in entries if e["action"] == "delete_foreign"],
              "unknown": [e for e in entries if e["action"] == "keep_unknown"],
              "rows_leaving_each_tab": {r: sum(1 for e in entries if e["tab"] == r and
                                               (e["action"].startswith("delete") or e["action"] == "move"))
                                        for r in REGIONS}}
    if not apply:
        return result

    # 1) backup every tab, before anything is changed
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = RUNTIME_DIR / f"notice_tab_cleanup_{kind}_{stamp}.json"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(json.dumps({r: {"header": t["header"], "rows": t["rows"]} for r, t in tabs.items()},
                                 ensure_ascii=False, indent=1), encoding="utf-8")
    result["backup"] = str(backup)

    # 2) move: append to the destination tab, skipping a row that is already there
    moved_ok = set()
    for e in result["moves"]:
        src_tab = tabs[e["tab"]]
        src_row = dict(zip(src_tab["header"], next(r for n, r in src_tab["rows"] if n == e["row"])))
        dest = tabs[e["to"]]
        dest_key = (e["upc"], e["date"])
        d_upc, d_date = dest["header"].index("UPC"), dest["header"].index("Date Received")
        if not any((_cell(r, d_upc), _cell(r, d_date)) == dest_key for _n, r in dest["rows"]):
            values = [str(src_row.get(h, "")) for h in dest["header"]]
            payload = {"sheets": [{"name": getattr(m, "RIGHTS_CONFIRMATION_SHEET_NAME", None)
                                   or m.METADATA_CORRECTIONS_SHEET_NAME,
                                   "mode": "append", "header": False, "columns": dest["header"], "data": [values],
                                   "dtypes": {h: "object" for h in dest["header"]}}]}
            m._run_lark_sheets(["+table-put", "--url", ad["url"](e["to"]), "--sheets", "-"],
                               input_text=json.dumps(payload))
        moved_ok.add((e["tab"], e["row"]))

    # 3) delete (irreversible; the backup above is the way back). Only rows whose
    #    move succeeded, foreign rows and duplicates.
    deletions: Dict[str, List[int]] = {r: [] for r in REGIONS}
    for e in entries:
        if e["action"] in ("delete_foreign", "delete_duplicate") or (e["action"] == "move" and (e["tab"], e["row"]) in moved_ok):
            deletions[e["tab"]].append(e["row"])
    deleted = {}
    for region, rows in deletions.items():
        if rows:
            ranges = json.dumps([f"{n}:{n}" for n in sorted(set(rows))])
            m._run_lark_sheets(["+dim-delete", "--url", ad["url"](region), "--sheet-id", tabs[region]["sheet_id"],
                                "--ranges", ranges, "--yes"])
            deleted[region] = len(set(rows))
    result["deleted"] = deleted

    # 4) state: the daily DM re-send must follow the rows
    from copyright_alert.state_io import update_json_state
    moved_to = {e["upc"]: e["to"] for e in result["moves"]}
    foreign = {e["upc"] for e in result["foreign"]}

    def _update(state):
        for rec in (state.get("notices") or {}).values():
            upc = str(rec.get("upc") or (rec.get("fields") or {}).get("upc") or "").strip()
            if upc in moved_to:
                rec["region"] = moved_to[upc]
            elif upc in foreign:
                ad["mark_foreign"](rec)
    update_json_state(ad["state_file"], _update, default=lambda: {"notices": {}})
    return result


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("rights", "metadata"):
        print(__doc__)
        return 2
    out = run_cleanup(argv[0], apply="--apply" in argv)
    brief = {k: v for k, v in out.items() if k not in ("moves", "foreign", "unknown")}
    brief["moves"] = [(e["upc"], e["tab"], "->", e["to"]) for e in out["moves"]]
    brief["foreign"] = sorted({e["upc"] for e in out["foreign"]})
    brief["unknown"] = sorted({e["upc"] for e in out["unknown"]})
    print(json.dumps(brief, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
