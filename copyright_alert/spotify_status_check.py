#!/usr/bin/env python3
"""
copyright_alert/spotify_status_check.py

Weekly check of the ACR sheet's Online/Offline column against Spotify.

The Status column in the ACR cycle tabs is typed by hand and never refreshed, so
a matched track that Spotify already removed keeps showing "Online" (and keeps
inflating "claims to file"). Once a week this looks at every "Other Party" case
still shown as Online, asks the Spotify Web API about the matched track, and
records the ones that are gone:

  * 404                                   -> removed -> Offline
  * not playable in the SoundOn track's
    market AND in a second market         -> Offline
  * playable                              -> left alone

The market is the case's "SO Region" (the SoundOn track's country), as Spotify
needs one; a track unplayable only there is re-checked in US (BR if the region
is US) before it is called offline, so a regional gap alone never flips it.

It only ever moves Online -> Offline. A track that looks "back online" is not
touched here.

Results go to the same places a manual change on the dashboard does: a row in
the "Status Changes" tab of the Takedown Stream Tracker workbook (source "auto";
the dashboard and the digest read it) and the Status cell of the ACR sheet rows
(best effort). Nothing changes in dry-run.

Credentials: SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET in the environment
(client-credentials flow, no user login). Spotify's batch track endpoint is not
available to this app (403), so it is one request per distinct track.

Run by hand:  python3 -m copyright_alert.spotify_status_check --dry-run
New scan tab with an empty Status column (one-off, every row):
              python3 -m copyright_alert.spotify_status_check --fill-blank-tab "Oct 15" [--dry-run]
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

from copyright_alert import acr_digest as ad
from copyright_alert import claim_window as cw
from copyright_alert.metadata_notice import _run_lark_sheets
from pathlib import Path

SPOTIFY_HEADER = "Spotify"
STATUS_CHANGES_COLUMNS = [
    "key", "so_song_id", "matched_isrc", "song", "matched_title", "region",
    "old_status", "new_status", "source", "changed_by", "ts", "note",
]
CHECKER_NAME = "Spotify check"
MIN_DAYS_BETWEEN_RUNS = 7
CALL_PAUSE_SECONDS = 0.2
MAX_RETRIES = 3
STATE_FILE = Path(__file__).resolve().parent / "spotify_status_state.json"
TOKEN_URL = "https://accounts.spotify.com/api/token"
TRACK_URL = "https://api.spotify.com/v1/tracks/{id}?market={market}"
_TRACK_ID_RE = re.compile(r"track[/:]([A-Za-z0-9]{22})")

HttpFn = Callable[[str, Optional[bytes], Dict[str, str]], Tuple[int, Dict[str, str], str]]


def _http(url: str, data: Optional[bytes], headers: Dict[str, str]) -> Tuple[int, Dict[str, str], str]:
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read().decode("utf-8", "ignore")


def track_id_from_link(link: str) -> str:
    m = _TRACK_ID_RE.search(link or "")
    return m.group(1) if m else ""


def market_for(region: str) -> str:
    """The case's SO Region as a Spotify market (ISO 3166-1 alpha-2)."""
    r = (region or "").strip().upper()
    return r if re.fullmatch(r"[A-Z]{2}", r) else "US"


class SpotifyClient:
    def __init__(self, client_id: str, client_secret: str, http: HttpFn = _http, sleep=time.sleep):
        self._id, self._secret, self._http, self._sleep = client_id, client_secret, http, sleep
        self._token = ""

    @classmethod
    def from_env(cls) -> Optional["SpotifyClient"]:
        cid, sec = os.environ.get("SPOTIFY_CLIENT_ID", "").strip(), os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip()
        return cls(cid, sec) if cid and sec else None

    def _refresh(self) -> None:
        basic = base64.b64encode(f"{self._id}:{self._secret}".encode()).decode()
        status, _, body = self._http(
            TOKEN_URL, urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
            {"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"})
        if status != 200:
            raise RuntimeError(f"Spotify token request failed: HTTP {status}")
        self._token = json.loads(body)["access_token"]

    def _get(self, track_id: str, market: str) -> Tuple[int, str]:
        """GET with token refresh on 401 and Retry-After handling on 429."""
        if not self._token:
            self._refresh()
        refreshed = False
        for attempt in range(MAX_RETRIES + 1):
            status, headers, body = self._http(TRACK_URL.format(id=track_id, market=market), None,
                                               {"Authorization": f"Bearer {self._token}"})
            if status == 401 and not refreshed:
                self._refresh()
                refreshed = True
                continue
            if status == 429 and attempt < MAX_RETRIES:
                wait = next((v for k, v in headers.items() if k.lower() == "retry-after"), "2")
                self._sleep(min(float(wait) if str(wait).isdigit() else 2.0, 60.0) + 0.5)
                continue
            return status, body
        return 429, ""

    def check(self, track_id: str, market: str) -> Dict[str, str]:
        """{"state": "online" | "offline" | "unknown", "detail": ...}"""
        status, body = self._get(track_id, market)
        if status == 400:                         # market Spotify doesn't know
            market = "US"
            status, body = self._get(track_id, market)
        if status == 404:
            return {"state": "offline", "detail": "removed from Spotify (404)"}
        if status != 200:
            return {"state": "unknown", "detail": f"HTTP {status}"}
        if _playable(body):
            return {"state": "online", "detail": ""}
        other = "BR" if market == "US" else "US"
        status2, body2 = self._get(track_id, other)
        if status2 == 404:
            return {"state": "offline", "detail": "removed from Spotify (404)"}
        if status2 == 200 and _playable(body2):
            return {"state": "online", "detail": f"playable in {other}"}
        if status2 == 200:
            return {"state": "offline", "detail": f"not playable in {market} or {other}"}
        return {"state": "unknown", "detail": f"HTTP {status2} on recheck"}


def _playable(body: str) -> bool:
    try:
        t = json.loads(body)
    except ValueError:
        return True                               # unreadable body: don't call it offline
    return t.get("is_playable", True) is not False


# ── what to check ───────────────────────────────────────────────────────────

def _col_letter(i: int) -> str:
    n, out = i + 1, ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def plan_checks(tabs: Dict[str, Dict[str, object]]) -> Dict[Tuple[str, str], Dict[str, object]]:
    """tabs: {tab_name: {"sheet_id": ..., "rows": grid with Status corrections applied}}.
    Returns {(track_id, market): {"cases": {key: case_info}, "cells": [(sheet_id, col, row_no)]}}
    for every Other Party case still shown Online that has a Spotify track link."""
    plan: Dict[Tuple[str, str], Dict[str, object]] = {}
    for tab in tabs.values():
        rows = tab["rows"]
        if not rows:
            continue
        idx = ad._header_index(rows[0])
        decision_i = ad._find(idx, *ad.DECISION_HEADER_ALIASES)
        status_i, sp_i = idx.get(ad.STATUS_HEADER), idx.get(SPOTIFY_HEADER)
        if decision_i is None or status_i is None or sp_i is None:
            continue
        song_id_i, title_i = idx.get(ad.SONG_ID_HEADER), idx.get(ad.TITLE_HEADER)
        m_isrc_i, m_title_i, region_i = idx.get(ad.MATCHED_ISRC_HEADER), idx.get(ad.MATCHED_TITLE_HEADER), idx.get(ad.REGION_HEADER)
        for n, row in enumerate(rows[1:], start=2):
            if not ad._is_other_party(ad._cell(row, decision_i)) or ad._cell(row, status_i).lower() != "online":
                continue
            track_id = track_id_from_link(ad._cell(row, sp_i))
            if not track_id:
                continue
            market = market_for(ad._cell(row, region_i))
            entry = plan.setdefault((track_id, market), {"cases": {}, "cells": []})
            key = ad.case_key(ad._cell(row, song_id_i), ad._cell(row, title_i), ad._cell(row, m_isrc_i), ad._cell(row, m_title_i))
            entry["cases"].setdefault(key, {
                "key": key, "so_song_id": ad._cell(row, song_id_i), "matched_isrc": ad._cell(row, m_isrc_i),
                "song": ad._cell(row, title_i), "matched_title": ad._cell(row, m_title_i), "region": ad._cell(row, region_i),
            })
            entry["cells"].append((str(tab["sheet_id"]), _col_letter(status_i), n))
    return plan


def load_tabs() -> Dict[str, Dict[str, object]]:
    tabs: Dict[str, Dict[str, object]] = {}
    for t in ad.list_tabs(ad.ACR_SHEET_URL):
        try:
            rows = ad.read_cycle_tab(ad.ACR_SHEET_URL, t["sheet_id"])
        except Exception as exc:
            print(f"  ⚠ Spotify check: could not read tab {t['name']!r}: {exc!r}", flush=True)
            continue
        if rows and ad.is_valid_cycle_tab(ad._header_index(rows[0])):
            tabs[t["name"]] = {"sheet_id": t["sheet_id"], "rows": rows}
    return tabs


# ── recording what we found ─────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(cw.BRT).strftime("%Y-%m-%d %H:%M:%S")


def record_offline(cases: List[Dict[str, str]], detail: str) -> None:
    """One 'Status Changes' row per case (the dashboard and the digest read it)."""
    rows = [[c["key"], c["so_song_id"], c["matched_isrc"], c["song"], c["matched_title"], c["region"],
             "Online", "Offline", "auto", CHECKER_NAME, _now(), detail] for c in cases]
    payload = {"sheets": [{
        "name": "Status Changes", "mode": "append", "header": False, "columns": STATUS_CHANGES_COLUMNS,
        "data": rows, "dtypes": {c: "object" for c in STATUS_CHANGES_COLUMNS},
    }]}
    _run_lark_sheets(["+table-put", "--url", cw.WORKBOOK_URL, "--sheets", "-"], input_text=json.dumps(payload))


def write_cells(cells: List[Tuple[str, str, int]]) -> Dict[str, int]:
    """Best effort: put "Offline" into the ACR sheet's Status cells."""
    from copyright_alert.lark_auth import sheet_values_api
    ok = failed = 0
    for sheet_id, col, row_no in cells:
        try:
            sheet_values_api("PUT", ad.ACR_SHEET_URL, sheet_id, f"{col}{row_no}", [["Offline"]])
            ok += 1
        except Exception as exc:
            failed += 1
            if failed == 1:
                print(f"  ⚠ Spotify check: could not write the Status cell ({exc!r}); the Status Changes log still holds it", flush=True)
    return {"written": ok, "failed": failed}


# ── run ─────────────────────────────────────────────────────────────────────

def run_check(*, dry_run: bool = False, max_calls: int = 0, client: Optional[SpotifyClient] = None,
              tabs: Optional[Dict[str, Dict[str, object]]] = None, sleep=time.sleep) -> Dict[str, object]:
    result: Dict[str, object] = {"checked": 0, "online": 0, "offline": [], "unknown": 0, "no_link": 0, "dry_run": dry_run}
    client = client or SpotifyClient.from_env()
    if client is None:
        return {**result, "skipped": "SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET not set"}
    plan = plan_checks(tabs if tabs is not None else load_tabs())
    result["distinct_tracks"] = len(plan)
    for n, ((track_id, market), entry) in enumerate(plan.items()):
        if max_calls and n >= max_calls:
            result["stopped_at_max_calls"] = max_calls
            break
        verdict = client.check(track_id, market)
        result["checked"] += 1
        sleep(CALL_PAUSE_SECONDS)
        if verdict["state"] == "online":
            result["online"] += 1
        elif verdict["state"] == "unknown":
            result["unknown"] += 1
            print(f"  ⚠ Spotify check: {track_id} ({market}) unclear — {verdict['detail']}", flush=True)
        else:
            cases = list(entry["cases"].values())
            result["offline"].append({"track_id": track_id, "market": market, "detail": verdict["detail"],
                                      "cases": [f"{c['song']} → {c['matched_title']}" for c in cases]})
            if not dry_run:
                record_offline(cases, f"{verdict['detail']} (market {market})")
                write_cells(entry["cells"])
    return result


def _load_state() -> Dict[str, str]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def run_weekly(*, today: Optional[date] = None, force: bool = False, dry_run: bool = False) -> Dict[str, object]:
    """Self-throttled: does nothing unless 7+ days passed since the last real run."""
    today = today or cw.today_brt()
    last = cw.ad_parse_date(_load_state().get("last_run"))
    if not force and last and (today - last) < timedelta(days=MIN_DAYS_BETWEEN_RUNS):
        return {"skipped": f"ran {last.isoformat()}, next after {(last + timedelta(days=MIN_DAYS_BETWEEN_RUNS)).isoformat()}"}
    result = run_check(dry_run=dry_run)
    if not dry_run and "skipped" not in result and not result.get("stopped_at_max_calls"):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps({"last_run": today.isoformat()}), encoding="utf-8")
    return result


def summary_lines(result: Dict[str, object]) -> List[str]:
    lines = [f"Checked {result['checked']} distinct tracks on Spotify: {len(result['offline'])} now offline, "
             f"{result['online']} still online, {result['unknown']} unclear."]
    for o in result["offline"][:15]:
        lines.append(f"• {o['cases'][0]} — {o['detail']} ({o['market']})")
    if len(result["offline"]) > 15:
        lines.append(f"…and {len(result['offline']) - 15} more (see the Status Changes tab).")
    return lines


def plan_blank_status(rows: List[List[str]]) -> Dict[Tuple[str, str], List[int]]:
    """{(track_id, market): [sheet row numbers]} for every row of a scan tab whose
    Status is empty and that has a Spotify track link, whatever its review result."""
    plan: Dict[Tuple[str, str], List[int]] = {}
    if not rows:
        return plan
    idx = ad._header_index(rows[0])
    status_i, sp_i, region_i = idx.get(ad.STATUS_HEADER), idx.get(SPOTIFY_HEADER), idx.get(ad.REGION_HEADER)
    if status_i is None or sp_i is None:
        raise RuntimeError("tab has no Status or Spotify column")
    for n, row in enumerate(rows[1:], start=2):
        if ad._cell(row, status_i):
            continue
        track_id = track_id_from_link(ad._cell(row, sp_i))
        if track_id:
            plan.setdefault((track_id, market_for(ad._cell(row, region_i))), []).append(n)
    return plan


def fill_blank_status(tab_name: str, *, dry_run: bool = False, client: Optional[SpotifyClient] = None,
                      rows: Optional[List[List[str]]] = None, sheet_id: str = "", write=None,
                      sleep=time.sleep) -> Dict[str, object]:
    """One-off for a new scan tab: look up every row with an empty Status on
    Spotify and write Online or Offline into it. Unclear answers stay empty.
    Unlike the weekly check this covers every row, reviewed or not."""
    client = client or SpotifyClient.from_env()
    if client is None:
        return {"skipped": "SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET not set"}
    if rows is None:
        tab = next((t for t in ad.list_tabs(ad.ACR_SHEET_URL) if t["name"].strip() == tab_name.strip()), None)
        if tab is None:
            return {"skipped": f"no tab named {tab_name!r}"}
        sheet_id, rows = tab["sheet_id"], ad.read_tab(ad.ACR_SHEET_URL, tab["sheet_id"])
    plan = plan_blank_status(rows)
    status_col = _col_letter(ad._header_index(rows[0])[ad.STATUS_HEADER])
    result: Dict[str, object] = {"tab": tab_name, "distinct_tracks": len(plan), "rows": sum(map(len, plan.values())),
                                 "online": 0, "offline": 0, "unclear": 0, "dry_run": dry_run}
    cells: List[Tuple[int, str]] = []
    for (track_id, market), row_nos in plan.items():
        verdict = client.check(track_id, market)
        sleep(CALL_PAUSE_SECONDS)
        if verdict["state"] == "unknown":
            result["unclear"] += len(row_nos)
            continue
        value = "Online" if verdict["state"] == "online" else "Offline"
        result[verdict["state"]] += len(row_nos)
        cells += [(n, value) for n in row_nos]
    if not dry_run and cells:
        if write is None:
            from copyright_alert.lark_auth import sheet_values_batch_update

            def write(batch):
                sheet_values_batch_update(ad.ACR_SHEET_URL, batch)
        for i in range(0, len(cells), 200):
            write([{"range": f"{sheet_id}!{status_col}{n}:{status_col}{n}", "values": [[v]]} for n, v in cells[i:i + 200]])
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Weekly Spotify Online/Offline check for the ACR sheet")
    parser.add_argument("--dry-run", action="store_true", help="check Spotify and report, change nothing")
    parser.add_argument("--force", action="store_true", help="run even if it ran in the last 7 days")
    parser.add_argument("--max-calls", type=int, default=0, help="stop after this many Spotify lookups (testing)")
    parser.add_argument("--fill-blank-tab", metavar="TAB",
                        help="one-off: fill every empty Status in this scan tab with Online/Offline from Spotify")
    args = parser.parse_args()
    if args.fill_blank_tab:
        print(json.dumps(fill_blank_status(args.fill_blank_tab, dry_run=args.dry_run), ensure_ascii=False, indent=2))
        return 0
    if args.max_calls:
        print(json.dumps(run_check(dry_run=args.dry_run, max_calls=args.max_calls), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(run_weekly(force=args.force or args.dry_run, dry_run=args.dry_run), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
