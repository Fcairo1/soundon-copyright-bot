#!/usr/bin/env python3
"""
copyright_alert/claim_followup.py

Spotify follow-up on claims the team filed. Ticking ✅ Claim filed on the ACR
dashboard writes a row (with the filing date) to the Takedown Stream Tracker.
Spotify usually removes a track within a couple of days, and gives the uploader
5 days to reply to a claim, so each filed case is checked twice:

  * 2 days after filing  -> if Spotify already removed it, mark it Offline
  * 7 days after filing  -> same, and if it is STILL online, DM the region's
                            ops owner once so they can follow up with Spotify

Results go in the tracker's check_2d / check_7d columns ("Online 2026-10-29",
"Offline 2026-10-29", ...), which also make each check run only once. A track
found offline is recorded the same way the weekly check does it (Status Changes
row + the ACR sheet's Status cell). An unclear Spotify answer writes nothing,
so the case is retried on the next daily run. A case already 7+ days old when
first seen skips the day-2 check.

Runs every day from claim_window.run_daily_jobs (PART L). Needs
SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET, like the weekly check.

Run by hand:  python3 -m copyright_alert.claim_followup --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from typing import Dict, List, Optional, Tuple

from copyright_alert import acr_digest as ad
from copyright_alert import claim_window as cw
from copyright_alert import spotify_status_check as sp

CHECKS = (("check_7d", 7), ("check_2d", 2))   # latest due stage wins
COL_LETTER = {c: sp._col_letter(ad.TRACKER_COLUMNS.index(c)) for c in ("filed_by", "case_key", "check_2d", "check_7d")}


def due_stage(row: Dict[str, object], today: date) -> Optional[str]:
    """The check this filed case needs today, if any."""
    filed = cw.ad_parse_date(row.get("takedown_date"))
    if not filed:
        return None
    age = (today - filed).days
    for col, days in CHECKS:
        if age >= days:
            return None if row.get(col) else col
    return None


def index_cases(tabs: Dict[str, Dict[str, object]]) -> Dict[str, Dict[str, object]]:
    """{case key: {case info for the Status Changes log, status, Spotify link, cells}}
    over every scan tab. A case listed in several tabs keeps all its Status cells."""
    out: Dict[str, Dict[str, object]] = {}
    for tab in tabs.values():
        rows = tab["rows"]
        if not rows:
            continue
        idx = ad._header_index(rows[0])
        status_i, sp_i = idx.get(ad.STATUS_HEADER), idx.get(sp.SPOTIFY_HEADER)
        if status_i is None:
            continue
        song_id_i, title_i = idx.get(ad.SONG_ID_HEADER), idx.get(ad.TITLE_HEADER)
        m_isrc_i, m_title_i, region_i = idx.get(ad.MATCHED_ISRC_HEADER), idx.get(ad.MATCHED_TITLE_HEADER), idx.get(ad.REGION_HEADER)
        for n, row in enumerate(rows[1:], start=2):
            key = ad.case_key(ad._cell(row, song_id_i), ad._cell(row, title_i), ad._cell(row, m_isrc_i), ad._cell(row, m_title_i))
            entry = out.setdefault(key, {
                "case": {"key": key, "so_song_id": ad._cell(row, song_id_i), "matched_isrc": ad._cell(row, m_isrc_i),
                         "song": ad._cell(row, title_i), "matched_title": ad._cell(row, m_title_i), "region": ad._cell(row, region_i)},
                "status": "", "link": "", "cells": [],
            })
            status = ad._cell(row, status_i)
            if status.lower() == "online" or not entry["status"]:
                entry["status"] = status
            entry["link"] = entry["link"] or ad._cell(row, sp_i)
            entry["cells"].append((str(tab["sheet_id"]), sp._col_letter(status_i), n))
    return out


def ops_region(country: str) -> str:
    c = (country or "").strip().upper()
    return c if c in cw.REGIONS else ad.COUNTRY_TO_REGION.get(c, "BR")


def build_still_online_card(region: str, cases: List[Dict[str, object]]) -> dict:
    lines = []
    for c in cases[:15]:
        who = f" by {c['filed_by']}" if c.get("filed_by") else ""
        link = f" · [Spotify]({c['link']})" if c.get("link") else ""
        lines.append(f"• **{c['song']}** → {c['matched_title']} · filed {c['takedown_date']}{who}{link}")
    if len(cases) > 15:
        lines.append(f"…and {len(cases) - 15} more (Takedown Stream Tracker, check_7d = Online).")
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "orange", "title": {"tag": "plain_text",
                   "content": f"⚠️ Still on Spotify 7 days after the claim · {region}"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content":
                f"{len(cases)} claim{'s' if len(cases) != 1 else ''} filed a week ago "
                f"{'are' if len(cases) != 1 else 'is'} still online. Worth following up with Spotify."}},
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
            {"tag": "action", "actions": [{
                "tag": "button", "text": {"tag": "plain_text", "content": "Open the dashboard"},
                "type": "primary", "url": ad.DASHBOARD_URL,
            }]},
            {"tag": "note", "elements": [{"tag": "plain_text", "content":
                "Sent once per case, after the day-7 Spotify check."}]},
        ],
    }


def write_check(row_no: int, col: str, value: str) -> None:
    from copyright_alert.lark_auth import sheet_values_api
    sheet_values_api("PUT", ad.TAKEDOWN_STREAMS_SHEET_URL, ad.TAKEDOWN_STREAMS_SHEET_ID,
                     f"{COL_LETTER[col]}{row_no}", [[value]])


def run_followups(*, today: Optional[date] = None, dry_run: bool = False,
                  client: Optional[sp.SpotifyClient] = None, filed: Optional[List[Dict[str, object]]] = None,
                  tabs: Optional[Dict[str, Dict[str, object]]] = None, send=None) -> Dict[str, object]:
    today = today or cw.today_brt()
    result: Dict[str, object] = {"due": 0, "offline": [], "online": [], "unclear": 0, "skipped": [], "alerts": [], "dry_run": dry_run}
    filed = filed if filed is not None else ad.read_filed_claims()
    due: List[Tuple[Dict[str, object], str]] = [(r, s) for r in filed for s in [due_stage(r, today)] if s]
    result["due"] = len(due)
    if not due:
        return result
    client = client or sp.SpotifyClient.from_env()
    if client is None:
        return {**result, "skipped": "SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET not set"}
    cases = index_cases(tabs if tabs is not None else sp.load_tabs())
    stamp = today.isoformat()
    writes: List[Tuple[int, str, str]] = []
    still_online: Dict[str, List[Dict[str, object]]] = {}

    for row, stage in due:
        label = f"{row['song']} → {row['matched_title']}"
        entry = cases.get(row["key"])
        if entry is None:
            writes.append((row["_row"], stage, f"Not in ACR sheet {stamp}"))
            result["skipped"].append({"case": label, "reason": "not in the ACR sheet"})
            continue
        if entry["status"].lower() == "offline":
            writes.append((row["_row"], stage, f"Offline {stamp}"))
            result["offline"].append({"case": label, "stage": stage, "detail": "already Offline in the sheet"})
            continue
        track_id = sp.track_id_from_link(entry["link"])
        if not track_id:
            writes.append((row["_row"], stage, f"No Spotify link {stamp}"))
            result["skipped"].append({"case": label, "reason": "no Spotify link"})
            continue
        market = sp.market_for(entry["case"]["region"] or row["region"])
        verdict = client.check(track_id, market)
        if verdict["state"] == "unknown":
            result["unclear"] += 1
            print(f"  ⚠ claim follow-up: {label} unclear ({verdict['detail']}) — retrying next run", flush=True)
            continue
        if verdict["state"] == "offline":
            writes.append((row["_row"], stage, f"Offline {stamp}"))
            result["offline"].append({"case": label, "stage": stage, "detail": verdict["detail"]})
            if not dry_run:
                sp.record_offline([entry["case"]], f"{verdict['detail']} (market {market}, {stage[-2:]} after claim)")
                sp.write_cells(entry["cells"])
            continue
        result["online"].append({"case": label, "stage": stage})
        if stage == "check_7d":
            still_online.setdefault(ops_region(row["region"]), []).append({**row, "link": entry["link"]})
        else:
            writes.append((row["_row"], stage, f"Online {stamp}"))

    if send is None:
        from copyright_alert.rights_confirmation_notice import _send_to_ops as send
    for region, rows in still_online.items():
        sent = True
        if not dry_run:
            sent = bool(send(region, build_still_online_card(region, rows), log_context="claim follow-up").get("ok"))
        result["alerts"].append({"region": region, "cases": len(rows), "sent": sent})
        if sent:   # a failed DM leaves check_7d blank, so it is retried tomorrow
            writes += [(r["_row"], "check_7d", f"Online {stamp}") for r in rows]

    if not dry_run:
        for row_no, col, value in writes:
            try:
                write_check(row_no, col, value)
            except Exception as exc:
                print(f"  ⚠ claim follow-up: could not write {col} on tracker row {row_no} ({exc!r})", flush=True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Spotify check 2 and 7 days after a claim was filed")
    parser.add_argument("--dry-run", action="store_true", help="check Spotify and report, change nothing, send nothing")
    args = parser.parse_args()
    print(json.dumps(run_followups(dry_run=args.dry_run), ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
