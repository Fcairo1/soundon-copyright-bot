#!/usr/bin/env python3
import csv
import io
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)

from copyright_alert import run_alert as ra  # noqa: E402

CUTOFF = datetime(2026, 6, 3, 0, 0, 0, tzinfo=timezone.utc)
TARGET_CHAT_NAME = "AP Direitos BR"
TARGET_CHAT_ID = "oc_fd2e43d6451f8d87adb4cd4ceefa7816"
TARGET_POSTED_CLAIMS_FILE = "copyright_alert/posted_claims_ap_direitos_br.json"
TRACKER_URL = ra.TRACKER_SHEET_URL
TRACKER_SHEET_ID = ra.TRACKER_SHEET_ID
PAGE_SIZE = 100
MAX_PAGES = 30
AEOLUS_CHUNK_SIZE = 80
SEQUENTIAL_BENCHMARK_SAMPLE = 60
EXPLICIT_SKIP_UPCS = {"044317902701", "047752352704"}

ra.EXCLUDED_MENTIONS = {"esteban.mora", "filipe.cairo"}
ra.TARGET_CHAT_ID = TARGET_CHAT_ID
ra.POSTED_CLAIMS_FILE = TARGET_POSTED_CLAIMS_FILE


def log(msg=""):
    print(msg, flush=True)


def parse_json_output(raw: str):
    idx = raw.find("{")
    if idx == -1:
        return None
    return json.loads(raw[idx:])


def run_cmd(cmd, timeout=180):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def parse_iso(ts: str):
    if not ts:
        return None
    ts = ts.strip()
    try:
        if ts.endswith("Z"):
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def fetch_chat_id_by_name(name: str):
    token = ra._get_bot_access_token()
    if not token:
        raise RuntimeError("Could not get bot token for chat lookup")
    import urllib.request

    payload = json.dumps({"query": name, "page_size": 20}).encode("utf-8")
    req = urllib.request.Request(
        "https://open.larksuite.com/open-apis/im/v2/chats/search",
        data=payload,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("code") != 0:
        raise RuntimeError(f"Chat search failed: {data}")
    items = ((data.get("data") or {}).get("items") or [])
    for item in items:
        meta = item.get("meta_data") or {}
        if meta.get("name") == name:
            return meta.get("chat_id") or item.get("id")
    raise RuntimeError(f"Chat not found by exact name: {name}")


def iter_triage_messages(query: str):
    page_token = None
    page = 0
    while page < MAX_PAGES:
        page += 1
        cmd = [
            "lark-cli", "mail", "+triage",
            "--mailbox", ra.MAILBOX,
            "--query", query,
            "--max", str(PAGE_SIZE),
            "--format", "json",
        ]
        if page_token:
            cmd.extend(["--page-token", page_token])
        res = run_cmd(cmd, timeout=240)
        if res.returncode != 0:
            raise RuntimeError(f"triage failed rc={res.returncode}: {(res.stdout + res.stderr)[:1000]}")
        parsed = parse_json_output(res.stdout)
        if not parsed:
            raise RuntimeError(f"Failed to parse triage output: {res.stdout[:500]}")
        messages = parsed.get("messages") or (parsed.get("data") or {}).get("messages") or []
        if not messages:
            break
        for m in messages:
            yield m
        has_more = bool(parsed.get("has_more"))
        page_token = parsed.get("page_token")
        if not has_more or not page_token:
            break


def prefilter_skip_reason(subject, thread_id, seen_threads):
    subject = subject or ""
    if re.match(r"(?i)^(re:|fw:)", subject):
        return "reply/forward subject"
    if "claim release" in subject.lower():
        return "claim release subject"
    if thread_id and thread_id in seen_threads:
        return f"duplicate thread {thread_id}"
    return None


def strip_row_prefix(line: str):
    return re.sub(r"^\[row=\d+\]\s*", "", line)


def fetch_tracker_rows():
    cmd = [
        "lark-cli", "sheets", "+csv-get",
        "--url", TRACKER_URL,
        "--sheet-id", TRACKER_SHEET_ID,
        "--range", "A1:Q400",
    ]
    res = run_cmd(cmd, timeout=180)
    if res.returncode != 0:
        raise RuntimeError(f"tracker read failed rc={res.returncode}: {(res.stdout + res.stderr)[:1000]}")
    parsed = parse_json_output(res.stdout)
    data = (parsed or {}).get("data") or {}
    annotated = data.get("annotated_csv") or ""
    lines = [strip_row_prefix(line) for line in annotated.splitlines() if line.strip()]
    if not lines:
        return []
    reader = csv.reader(io.StringIO("\n".join(lines)))
    return list(reader)


def norm(v):
    return str(v if v is not None else "").strip()


def tracker_record_key_from_row(row):
    def cell(i):
        return norm(row[i]) if len(row) > i else ""

    return (
        cell(0),  # upc
        cell(1),  # isrc
        cell(2),  # title
        cell(8),  # claimant
        cell(13), # date received
    )


def tracker_record_key(ef, ar):
    return (
        norm(ef.get("upc", "N/A")),
        norm(ef.get("isrc", "N/A")),
        norm(ef.get("title") if ef.get("title") != "N/A" else ar.get("album_title", "N/A")),
        norm(ef.get("claimant_name", "N/A")),
        norm(ef.get("date_received", "N/A")),
    )


def load_posted_records(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def existing_claim_keys(*paths):
    keys = set()
    for path in paths:
        keys.update(k for k in load_posted_records(path).keys() if k)
    return keys


def posted_upcs_for_target_chat(target_chat_id, *paths):
    upcs = set(EXPLICIT_SKIP_UPCS)
    for path in paths:
        data = load_posted_records(path)
        for record in data.values():
            if not isinstance(record, dict):
                continue
            upc = norm(record.get("upc"))
            if not upc:
                continue
            if path == TARGET_POSTED_CLAIMS_FILE:
                upcs.add(upc)
                continue
            chat_id = norm(record.get("chat_id"))
            if chat_id and chat_id == target_chat_id:
                upcs.add(upc)
    return upcs


def benchmark_lookup_methods(unique_upcs):
    benchmark_upcs = list(unique_upcs)[:SEQUENTIAL_BENCHMARK_SAMPLE]
    if not benchmark_upcs:
        return {
            "sample_size": 0,
            "exact": True,
            "sequential_seconds": 0.0,
            "batch_seconds": 0.0,
            "estimated_full_sequential_seconds": 0.0,
        }

    start = time.perf_counter()
    for upc in benchmark_upcs:
        ra.query_aeolus(upc, "upc")
    sequential_seconds = time.perf_counter() - start

    start = time.perf_counter()
    ra.batch_query_aeolus_by_upc(benchmark_upcs, chunk_size=AEOLUS_CHUNK_SIZE)
    batch_seconds = time.perf_counter() - start

    exact = len(benchmark_upcs) == len(unique_upcs)
    estimated_full_sequential_seconds = sequential_seconds
    if benchmark_upcs and not exact:
        estimated_full_sequential_seconds = sequential_seconds * (len(unique_upcs) / len(benchmark_upcs))

    return {
        "sample_size": len(benchmark_upcs),
        "exact": exact,
        "sequential_seconds": sequential_seconds,
        "batch_seconds": batch_seconds,
        "estimated_full_sequential_seconds": estimated_full_sequential_seconds,
    }


def main():
    started_at = datetime.now(timezone.utc)
    log(f"Resolving chat: {TARGET_CHAT_NAME}")
    resolved_chat_id = fetch_chat_id_by_name(TARGET_CHAT_NAME)
    if resolved_chat_id != TARGET_CHAT_ID:
        raise RuntimeError(f"Resolved chat_id {resolved_chat_id} does not match expected {TARGET_CHAT_ID}")
    log(f"Target chat_id: {resolved_chat_id}")

    tracker_rows = fetch_tracker_rows()
    tracker_data_rows = [row for row in tracker_rows[1:] if any(norm(c) for c in row)]
    tracker_keys = {tracker_record_key_from_row(row) for row in tracker_data_rows}
    log(f"Loaded tracker rows: {len(tracker_data_rows)}")

    prior_claim_keys = existing_claim_keys("copyright_alert/posted_claims.json", TARGET_POSTED_CLAIMS_FILE)
    prior_posted_upcs = posted_upcs_for_target_chat(TARGET_CHAT_ID, "copyright_alert/posted_claims.json", TARGET_POSTED_CLAIMS_FILE)
    log(f"Loaded existing posted-claim keys: {len(prior_claim_keys)}")
    log(f"Loaded UPCs already posted for target chat (including explicit skips): {len(prior_posted_upcs)}")

    summary = {
        "emails_scanned": 0,
        "parsed_candidates": 0,
        "unique_upcs_extracted": 0,
        "passed_br_quality_filter": 0,
        "cards_posted": 0,
        "tracker_rows_logged": 0,
        "skipped_tracker_logged": 0,
        "skipped_prefilter": 0,
        "skipped_no_upc": 0,
        "skipped_no_aeolus": 0,
        "skipped_not_qualifying": 0,
        "skipped_explicit_or_prior_upc": 0,
        "skipped_duplicate_claim_key": 0,
    }

    seen_threads = set()
    parsed_candidates = []
    reached_cutoff = False

    for m in iter_triage_messages(ra.TRIAGE_QUERY):
        msg_id = m.get("message_id", "")
        subject = m.get("subject", "")
        date_raw = m.get("date", "")
        thread_id = m.get("thread_id") or msg_id
        msg_dt = parse_iso(date_raw)

        if msg_dt and msg_dt < CUTOFF:
            reached_cutoff = True
            break

        reason = prefilter_skip_reason(subject, thread_id, seen_threads)
        if reason:
            summary["skipped_prefilter"] += 1
            continue
        seen_threads.add(thread_id)
        summary["emails_scanned"] += 1

        body, meta = ra.fetch_email(msg_id)
        if not body:
            continue

        ef = ra.extract_fields(body, subject, meta)
        upc = norm(ef.get("upc"))
        if not upc or upc == "N/A":
            summary["skipped_no_upc"] += 1
            continue

        parsed_candidates.append({
            "message_id": msg_id,
            "subject": subject,
            "date_raw": date_raw,
            "ef": ef,
            "upc": upc,
        })

    summary["parsed_candidates"] = len(parsed_candidates)
    unique_upcs = []
    seen_upcs = set()
    for candidate in parsed_candidates:
        upc = candidate["upc"]
        if upc not in seen_upcs:
            seen_upcs.add(upc)
            unique_upcs.append(upc)
    summary["unique_upcs_extracted"] = len(unique_upcs)

    benchmark = benchmark_lookup_methods(unique_upcs)

    batch_start = time.perf_counter()
    aeolus_by_upc = ra.batch_query_aeolus_by_upc(unique_upcs, chunk_size=AEOLUS_CHUNK_SIZE)
    batch_lookup_seconds = time.perf_counter() - batch_start

    posted_cases = []
    filtered_cases = []

    for candidate in parsed_candidates:
        ef = candidate["ef"]
        upc = candidate["upc"]
        ar = aeolus_by_upc.get(upc) or {}

        if not ar:
            summary["skipped_no_aeolus"] += 1
            continue

        if (not ef.get("isrc") or ef.get("isrc") == "N/A") and ar.get("isrc"):
            ef["isrc"] = str(ar.get("isrc")).strip() or "N/A"

        if not ra.qualifies(ar):
            summary["skipped_not_qualifying"] += 1
            continue

        summary["passed_br_quality_filter"] += 1
        case_title = ef.get("title") if ef.get("title") != "N/A" else ar.get("album_title", "N/A")
        case_artist = ra._format_artist_names(ar.get("display_artist"))
        case_date = ef.get("date_received", candidate.get("date_raw") or "N/A")
        filtered_cases.append({
            "upc": upc,
            "artist": case_artist,
            "title": case_title,
            "date": case_date,
            "subject": candidate["subject"],
        })

        if upc in prior_posted_upcs:
            summary["skipped_explicit_or_prior_upc"] += 1
            continue

        dup_key = ra.claim_key(ef, ar, candidate["subject"])
        if dup_key in prior_claim_keys or ra.is_claim_already_posted(dup_key):
            summary["skipped_duplicate_claim_key"] += 1
            continue

        card = ra.build_card(ef, ar)
        success, posted_message_id = ra.post_card(card, ar, upc=upc, context="BR backfill group post")
        if not (success and posted_message_id):
            log(f"Failed to post card for UPC {upc} | {case_title}")
            continue

        card = ra.build_card(ef, ar, lark_message_id=posted_message_id)
        ra.patch_card_message(posted_message_id, card)
        summary["cards_posted"] += 1

        tracker_key = tracker_record_key(ef, ar)
        if tracker_key not in tracker_keys:
            if ra.append_tracker_row(ef, ar, posted_message_id, status=""):
                summary["tracker_rows_logged"] += 1
                tracker_keys.add(tracker_key)
            else:
                log(f"Tracker append failed for UPC {upc} | {case_title}")
        else:
            summary["skipped_tracker_logged"] += 1

        record = {
            "message_id": posted_message_id,
            "source_email_message_id": candidate["message_id"],
            "subject": candidate["subject"],
            "upc": ef.get("upc", "N/A"),
            "isrc": ef.get("isrc", "N/A"),
            "title": case_title,
            "artist": case_artist,
            "ref_id": ef.get("ref_id", "N/A"),
            "date": candidate.get("date_raw"),
            "region": (ar.get("user_region") or "").strip().upper(),
            "source": ar.get("source_type_name", "N/A"),
            "tier": ar.get("User Tier", "N/A"),
            "chat_id": TARGET_CHAT_ID,
        }
        ra._save_posted_claim(dup_key, record)
        prior_claim_keys.add(dup_key)
        prior_posted_upcs.add(upc)
        posted_cases.append({
            "upc": upc,
            "artist": case_artist,
            "title": case_title,
            "date": case_date,
            "message_id": posted_message_id,
        })

    finished_at = datetime.now(timezone.utc)
    total_seconds = (finished_at - started_at).total_seconds()
    output = {
        "chat_name": TARGET_CHAT_NAME,
        "chat_id": TARGET_CHAT_ID,
        "cutoff": CUTOFF.isoformat(),
        "reached_cutoff": reached_cutoff,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "total_seconds": total_seconds,
        "batch_lookup_seconds": batch_lookup_seconds,
        "benchmark": benchmark,
        "summary": summary,
        "filtered_cases": filtered_cases,
        "posted_cases": posted_cases,
    }
    print("__RESULT__")
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
