#!/usr/bin/env python3
"""
copyright_alert/run_alert.py

Scans the copyright inbox (newest-first), extracts infringement claim fields,
cross-checks the Aeolus Song Dimension dashboard, and posts a Lark card via
the dedicated bot if the content qualifies (user_region=BR AND
(source_tag IN [AP, A&R] OR user_tier=High Quality)).

Stops after the first qualifying alert is successfully posted.
"""

import subprocess
import json
import re
import sys
import os
import ast
import csv
import io
import tempfile
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from copyright_alert.lark_auth import request_json_with_auth_retry
from copyright_alert.manager_exclusions import is_manager_excluded
from copyright_alert.upc_exclusions import is_upc_excluded
from copyright_alert.region_guard import assert_region_allowed

# ── Config ──────────────────────────────────────────────────────────────────

def _load_local_lark_secret():
    for key in ("BOT_SECRET", "LARK_APP_SECRET", "APP_SECRET", "app_secret"):
        value = os.getenv(key, "").strip()
        if value:
            return value

    here = Path(__file__).resolve().parent
    for candidate in (here / ".env", here / "secrets.json"):
        if not candidate.exists():
            continue
        try:
            if candidate.suffix == ".json":
                payload = json.loads(candidate.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    for key in ("BOT_SECRET", "LARK_APP_SECRET", "APP_SECRET", "app_secret"):
                        value = str(payload.get(key, "")).strip()
                        if value:
                            os.environ.setdefault("BOT_SECRET", value)
                            return value
            else:
                for raw_line in candidate.read_text(encoding="utf-8").splitlines():
                    line = raw_line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key in {"BOT_SECRET", "LARK_APP_SECRET", "APP_SECRET", "app_secret"} and value:
                        os.environ.setdefault("BOT_SECRET", value)
                        return value
        except Exception:
            continue
    return ""

MAILBOX        = "soundon-copyright@bytedance.com"
BOT_APP_ID     = "cli_aa94690b12b81cde"
BOT_SECRET     = _load_local_lark_secret()
TARGET_CHAT_ID = "oc_6e157309d8d7145ba5ce7f0ba67354cb"
TRACKER_SHEET_URL = "https://bytedance.sg.larkoffice.com/sheets/HMQLsGgymhdIQ3tSbNNlk3m1gKd"
TRACKER_SHEET_ID = "c02dad"
# Region the scan is currently configured for, and the Aeolus user_region codes
# that qualify for it. Overridden by bot_runtime.configure_region(region).
CURRENT_REGION = "BR"
QUALIFY_COUNTRIES = {"BR"}
AEOLUS_DATASET = "374690"
AEOLUS_BASE    = "https://aeolus-va.tiktok-row.net"
AEOLUS_SCRIPT  = "inner_skills/aeolus-platform-analysis/scripts/dataset_sql_query.py"
# Engagement dataset: [AOP] dm_distribution_song_country_df (sid=1576005)
# Source: https://aeolus-va.tiktok-row.net/pages/dataQuery?appId=1301&id=2414694441&isDefault=1&rid=5377337&sid=1576005
ENGAGEMENT_DATASET = "1576005"
ENGAGEMENT_PARTITION = "2026-06-14"
POSTED_CLAIMS_FILE = "copyright_alert/posted_claims.json"
TRIAGE_QUERY = "Infringement Claim"
TRIAGE_MAX = 50
EXCLUDED_MENTIONS = {
    "filipe.cairo", "esteban.mora", "diego.meleiro", "gabriel.borsatto",
    "marina.braum", "fellipe.perini", "rafael.lopes", "duane.gigliotti",
    "zhaoyaqing.devon",
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def run(cmd, timeout=120):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip()

def parse_lark_json(raw):
    """Strip lark-cli noise lines and parse the first JSON object found."""
    # Find the first '{' that starts a JSON object
    idx = raw.find('{')
    if idx == -1:
        return None
    try:
        return json.loads(raw[idx:])
    except Exception:
        # Try line-by-line
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith('{'):
                try:
                    return json.loads(line)
                except Exception:
                    continue
    return None


def parse_lark_annotated_csv(raw):
    """Parse lark-cli +csv-get output into JSON payload, row matrix and row numbers."""
    parsed = parse_lark_json(raw)
    if not parsed:
        return None, [], []
    data = parsed.get("data") or {}
    annotated_csv = data.get("annotated_csv") or ""
    cleaned_csv = re.sub(r"(?m)^\[row=\d+\]\s?", "", annotated_csv)
    rows = list(csv.reader(io.StringIO(cleaned_csv))) if cleaned_csv else []
    row_numbers = list(data.get("row_indices") or [])
    if rows and not row_numbers:
        row_numbers = list(range(1, len(rows) + 1))
    return parsed, rows, row_numbers


def _atomic_write_json(path, data, *, ensure_ascii=False, indent=2):
    """Write JSON to `path` atomically (temp file in same dir + os.replace).

    Prevents truncated/corrupted state files if the process crashes mid-write
    or if two writers race — readers always see either the old or the new file,
    never a partial one. os.replace is atomic on the same filesystem.
    """
    path = os.fspath(path)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=ensure_ascii, indent=indent)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _send_interactive_via_lark_cli(*, receive_id_type: str, receive_id: str, content: str, timeout: int = 60):
    """Send an interactive message with AIME's injected bot identity via lark-cli.

    This is the fallback path when the repo-local BOT_SECRET is unavailable in the
    current runtime (common in cron / isolated contexts).
    """
    cmd = [
        "lark-cli", "im", "+messages-send",
        "--as", "bot",
        "--format", "json",
        "--msg-type", "interactive",
        "--content", content,
    ]
    if receive_id_type == "chat_id":
        cmd.extend(["--chat-id", receive_id])
    elif receive_id_type == "open_id":
        cmd.extend(["--user-id", receive_id])
    else:
        raise ValueError(f"Unsupported receive_id_type for lark-cli send: {receive_id_type}")

    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    combined = (res.stdout or "") + (res.stderr or "")
    parsed = parse_lark_json(combined) or {}
    if res.returncode != 0:
        raise RuntimeError((combined or f"lark-cli send failed rc={res.returncode}").strip()[:1000])
    return parsed


def fetch_recent_candidates():
    """Fetch recent inbox messages dynamically via lark-cli triage."""
    cmd = [
        "lark-cli", "mail", "+triage",
        "--mailbox", MAILBOX,
        "--query", TRIAGE_QUERY,
        "--max", str(TRIAGE_MAX),
        "--format", "json",
    ]
    print(f"Fetching latest mailbox candidates: query={TRIAGE_QUERY!r}, max={TRIAGE_MAX}, mailbox={MAILBOX}")
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if res.returncode != 0:
        print(f"  ✗ lark-cli triage failed rc={res.returncode}: {(res.stdout + res.stderr)[:1000]}")
        return []

    parsed = parse_lark_json(res.stdout)
    if not parsed:
        print(f"  ✗ Failed to parse triage output. Raw (first 500): {res.stdout[:500]}")
        return []

    messages = parsed.get("messages") or (parsed.get("data") or {}).get("messages") or []
    candidates = []
    seen_threads = set()
    print(f"Fetched {len(messages)} raw emails from live inbox search:")
    for m in messages:
        subject = m.get("subject", "")
        msg_id = m.get("message_id", "")
        date = m.get("date", "")
        thread_id = m.get("thread_id") or msg_id
        print(f"  FETCHED: {date} | {msg_id} | {subject}")

        if not msg_id:
            print("  SKIP FETCHED EMAIL: missing message_id")
            continue
        if re.match(r"(?i)^(re:|fw:)", subject):
            print("  SKIP FETCHED EMAIL: reply/forward subject")
            continue
        if "claim release" in subject.lower():
            print("  SKIP FETCHED EMAIL: claim release subject")
            continue
        if thread_id in seen_threads:
            print(f"  SKIP FETCHED EMAIL: duplicate thread {thread_id}")
            continue
        seen_threads.add(thread_id)
        candidates.append((msg_id, subject))

    print(f"Built {len(candidates)} candidate emails after inbox pre-filtering")
    return candidates

def first(text, *patterns, default="N/A"):
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE | re.DOTALL | re.MULTILINE)
        if m:
            v = m.group(1).strip()
            if v:
                return v
    return default

def first_line(text, *patterns, default="N/A"):
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE | re.MULTILINE)
        if m:
            v = m.group(1).strip()
            if v:
                return v
    return default


_FIELD_LABELS = [
    "Claimant", "Claimant Name", "Email", "Company", "Claim Type", "Claimed Territory",
    "Claimant's Description", "Initial Claim Description", "content_description", "Content Title",
    "Release Title", "Artist", "UPC", "UPC(s)", "ISRC", "Label Name", "Label",
    "Content Type", "URI", "DSP", "DSP(s)", "If you", "Best Regards", "ref",
]


def _normalized_body(text):
    """Normalize copied/forwarded email text while keeping field boundaries detectable."""
    text = text or ""
    text = re.sub(r"=\r?\n", "", text)
    text = re.sub(r"\r\n?", "\n", text)
    # Some forwarded emails arrive as one long line. Put known labels back on their own line.
    for label in sorted(_FIELD_LABELS, key=len, reverse=True):
        text = re.sub(rf"\s+({re.escape(label)}\s*:)", r"\n\1", text, flags=re.IGNORECASE)
    return text.strip()


def labeled_value(text, *labels, default="N/A"):
    """Extract a single labeled field without letting it bleed into later fields."""
    text = _normalized_body(text)
    label_alt = "|".join(re.escape(l) for l in labels)
    next_alt = "|".join(re.escape(l) for l in _FIELD_LABELS)
    pat = rf"(?:^|\n)\s*(?:{label_alt})\s*:[ \t]*(.*?)(?=\n\s*(?:{next_alt})\s*:|\n\s*If you\b|\n\s*Best Regards\b|\n\s*ref:_|\Z)"
    m = re.search(pat, text, re.IGNORECASE | re.DOTALL)
    if not m:
        return default
    value = _clean_email_value(m.group(1))
    return value if value else default

def fetch_email(msg_id):
    cmd = [
        "lark-cli", "mail", "+message",
        "--mailbox", MAILBOX,
        "--message-id", str(msg_id),
        "--html=false",
        "--format", "json",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    out = (proc.stdout or "").strip()
    parsed = parse_lark_json(out)
    if not parsed:
        print(f"  ✗ Failed to parse lark-cli output. Raw (first 300): {out[:300]}")
        return "", {}
    # lark-cli wraps response in {"ok": true, "data": {...}}
    inner = parsed.get("data", parsed)
    body = inner.get("body_plain_text", "")
    return body, inner

def _clean_email_value(value):
    """Normalize simple values extracted from quoted-printable forwarded emails."""
    if not value or value == "N/A":
        return "N/A"
    value = re.sub(r"[*`_]+", "", value)
    value = re.sub(r"=\r?\n", "", value)
    return value.strip(" \t\r\n<>")


def _claimant_company_from_email(email):
    if not email or email == "N/A" or "@" not in email:
        return "N/A"
    domain = email.split("@", 1)[1].lower().strip()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain or "N/A"


def _parse_people_list(value):
    """Parse Aeolus people-list fields and remove alert-routing exclusions."""
    if not value or value == "N/A":
        return []
    if isinstance(value, list):
        raw_items = value
    else:
        text = str(value).strip()
        try:
            parsed = json.loads(text)
        except Exception:
            try:
                parsed = ast.literal_eval(text)
            except Exception:
                parsed = re.split(r"[,;\n]+", text)
        raw_items = parsed if isinstance(parsed, list) else [parsed]

    people = []
    seen = set()
    for item in raw_items:
        user = str(item).strip().strip('"\'')
        if not user or user == "N/A":
            continue
        key = user.lower()
        if key in EXCLUDED_MENTIONS or key in seen:
            continue
        seen.add(key)
        people.append(user)
    return people


def _display_name_from_username(username):
    parts = [p for p in str(username).replace("_", ".").split(".") if p]
    return " ".join(p.capitalize() for p in parts) or str(username)


def _mention_people(value, include_test_user=False, label_uid=""):
    people = _parse_people_list(value)
    if label_uid:
        people = [
            p for p in people
            if not is_manager_excluded(label_uid, p, _display_name_from_username(p), f"{p}@bytedance.com")
        ]
    if include_test_user and "filipe.cairo" not in [p.lower() for p in people]:
        people.append("filipe.cairo")
    if not people:
        return "N/A"
    # Lark card markdown resolves mentions via <at email="...">Display Name</at>.
    # The username is the account name; @bytedance.com is the corporate email domain.
    return " ".join(
        f'<at email="{p}@bytedance.com">{_display_name_from_username(p)}</at>'
        for p in people
    )


def _format_artist_names(value):
    if not value or value == "N/A":
        return "N/A"
    names = _parse_people_list(value)
    if not names:
        return str(value).strip()
    return ", ".join(names)


def _message_preview(message, limit=140):
    message = _clean_email_value(message)
    if message == "N/A":
        return "N/A"
    first_line = next((line.strip() for line in message.splitlines() if line.strip()), message.strip())
    return first_line[:limit].rstrip() + ("…" if len(first_line) > limit else "")


def detect_email_source(body, subject="", meta=None):
    """Classify the source/category of the infringement claim email."""
    meta = meta or {}
    haystack = "\n".join([
        subject or "",
        meta.get("from", "") or meta.get("sender", ""),
        body or "",
    ])
    if re.search(r"AudioSalad\s+Support|support@audiosalad\.com|AudioSalad", haystack, re.IGNORECASE):
        return "AudioSalad"
    if re.search(r"Infringement Claim Response", haystack, re.IGNORECASE):
        return "Infringement Claim Response"
    if re.search(r"Content Takedown", haystack, re.IGNORECASE):
        return "Content Takedown"
    return "Other"


_DSP_RULES = [
    ("Spotify", [r"spotify\.com"], [r"\bspotify\b"]),
    ("Apple Music", [r"apple\.com", r"itunes"], [r"\bapple\s+music\b", r"\bitunes\b", r"\bapple\b"]),
    ("YouTube / Google", [r"youtube\.com", r"google\.com"], [r"\byoutube\b", r"\bgoogle\b", r"content\s+id"]),
    ("TikTok", [r"tiktok\.com", r"bytedance"], [r"\btiktok\b", r"\bbyte\s*dance\b"]),
    ("Meta", [r"facebook\.com", r"instagram\.com"], [r"\bmeta\b", r"\bfacebook\b", r"\binstagram\b", r"rights\s+manager"]),
    ("Amazon Music", [r"amazon\.com", r"amazonmusic"], [r"\bamazon\s+music\b", r"\bamazonmusic\b", r"\bamazon\b"]),
    ("Deezer", [r"deezer\.com"], [r"\bdeezer\b"]),
    ("SoundCloud", [r"soundcloud\.com"], [r"\bsoundcloud\b"]),
    ("Pandora", [r"pandora\.com"], [r"\bpandora\b"]),
    ("TIDAL", [r"tidal\.com"], [r"\btidal\b"]),
]


def _meta_sender_text(meta):
    if not isinstance(meta, dict):
        return ""
    sender_bits = []
    for key in ("from", "sender", "from_email", "sender_email", "reply_to", "reply_to_email"):
        value = meta.get(key)
        if isinstance(value, (dict, list)):
            sender_bits.append(json.dumps(value, ensure_ascii=False))
        elif value:
            sender_bits.append(str(value))
    return "\n".join(sender_bits)


def detect_dsp(email):
    """Detect the DSP/platform for an inbound claim email.

    Returns {"dsp": <name>, "confidence": "high"|"medium"|"low"}.
    Sender domain/address rules are checked first; subject/body keywords are the fallback.
    """
    email = email or {}
    if isinstance(email, dict):
        subject = str(email.get("subject") or "")
        body = str(email.get("body") or email.get("body_plain_text") or email.get("text") or "")
        meta = email.get("meta") if isinstance(email.get("meta"), dict) else email
        sender_text = _meta_sender_text(meta).lower()
    else:
        subject = ""
        body = str(email)
        sender_text = ""

    for dsp, sender_patterns, _keyword_patterns in _DSP_RULES:
        if any(re.search(pattern, sender_text, re.IGNORECASE) for pattern in sender_patterns):
            return {"dsp": dsp, "confidence": "high"}

    subject_text = subject.lower()
    body_text = body.lower()
    for dsp, _sender_patterns, keyword_patterns in _DSP_RULES:
        if any(re.search(pattern, subject_text, re.IGNORECASE) for pattern in keyword_patterns):
            return {"dsp": dsp, "confidence": "medium"}
    for dsp, _sender_patterns, keyword_patterns in _DSP_RULES:
        if any(re.search(pattern, body_text, re.IGNORECASE) for pattern in keyword_patterns):
            return {"dsp": dsp, "confidence": "low"}

    explicit_subject = first(subject, r"Infringement Claim:\s*([\w\s/,]+?)\s+-")
    if explicit_subject != "N/A":
        explicit_subject = re.sub(r"\s*,\s*all\b", "", explicit_subject, flags=re.IGNORECASE).strip()
        if explicit_subject:
            return {"dsp": explicit_subject, "confidence": "low"}

    explicit_body = labeled_value(body, "DSP(s)", "DSP")
    if explicit_body != "N/A":
        for stop in ("UPC", "Additional info", "\n", "\r"):
            idx = explicit_body.find(stop)
            if idx > 0:
                explicit_body = explicit_body[:idx].strip()
        if explicit_body:
            return {"dsp": explicit_body, "confidence": "low"}

    print(f"  ⚠ DSP detection unknown: sender={sender_text[:200]!r}, subject={subject[:200]!r}")
    return {"dsp": "Unknown", "confidence": "low"}


def extract_fields(body, subject, meta):
    """Extract all needed fields from email body + subject."""
    fields = {}
    fields["email_source"] = detect_email_source(body, subject, meta)

    # UPC — from subject first, then body
    fields["upc"] = first(subject, r"UPC\s*[:\-]?\s*(\d{10,13})")
    if fields["upc"] == "N/A":
        fields["upc"] = first(body,
            r"UPC\(s\)[:\s*]+(\d{10,13})",
            r"UPC[:\s*]+(\d{10,13})",
        )

    # ISRC — prefer explicit email field; if absent, main() falls back to Aeolus by UPC.
    fields["isrc"] = labeled_value(body, "ISRC")
    if fields["isrc"] != "N/A":
        m = re.search(r"[A-Z0-9]{12}", fields["isrc"], re.IGNORECASE)
        fields["isrc"] = m.group(0).upper() if m else "N/A"

    # Title is not present in the AudioSalad template; keep N/A so build_card can
    # fall back to the Aeolus album_title.
    fields["title"] = labeled_value(body, "Content Title", "Release Title", "Title")

    # Claimant fields can arrive as either a normal multi-line form or a single
    # collapsed forwarded paragraph. Always stop at the next known label.
    claimant_line = labeled_value(body, "Claimant")
    claimant_name = labeled_value(body, "Claimant Name")
    claimant_email = labeled_value(body, "Email")

    if claimant_line != "N/A":
        email_match = re.search(r"([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})", claimant_line)
        if email_match and claimant_email == "N/A":
            claimant_email = email_match.group(1)
        # AudioSalad style: "Firstname Lastname - email@domain.com".
        claimant_line = re.sub(r"\s+-\s*[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}.*$", "", claimant_line).strip()
        if claimant_line:
            claimant_name = claimant_line

    if claimant_email == "N/A":
        claimant_email = first(body, r"([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})")

    fields["claimant_name"] = _clean_email_value(claimant_name)
    fields["claimant_email"] = _clean_email_value(claimant_email)

    # Company must be only the explicit Company value. If no company was supplied,
    # show N/A rather than inferring unrelated claim text or an email domain.
    fields["claimant_company"] = labeled_value(body, "Company")

    # Claimant message: the long/free-text claim belongs here only.
    msg = labeled_value(body, "content_description")
    if msg == "N/A":
        msg = labeled_value(body, "Initial Claim Description")
    if msg == "N/A":
        msg = labeled_value(body, "Claimant's Description")
    if msg == "N/A":
        msg = first(body,
            r"Additional info:\s*(.+?)(?:\n\s*\*--\*|\n\s*If you accept the claim|\n\s*-{2,}\s*Original Message|\Z)",
        )
    fields["claimant_message"] = _clean_email_value(msg) if msg != "N/A" else "no message"

    # Ref ID (ref:_ ... :ref)
    ref_match = re.search(r'(ref:_[A-Za-z0-9._]+:ref)', body + " " + subject)
    fields["ref_id"] = ref_match.group(1) if ref_match else "N/A"

    # Optional fields. Keep missing fields as N/A unless explicitly labeled.
    fields["claim_type"]       = labeled_value(body, "Claim Type", "type")
    fields["label_name"]       = labeled_value(body, "Label Name", "Label", "indie_label")
    fields["content_type"]     = labeled_value(body, "Content Type")
    fields["content"]          = labeled_value(body, "Content Title", "Release Title")

    detected_dsp = detect_dsp({"subject": subject, "body": body, "meta": meta})
    fields["dsp"] = detected_dsp["dsp"]
    fields["dsp_confidence"] = detected_dsp["confidence"]
    if fields["dsp"] == "Unknown":
        explicit_dsp = labeled_value(body, "DSP(s)", "DSP")
        if explicit_dsp == "N/A":
            explicit_dsp = first(subject, r"Infringement Claim:\s*([\w\s/,]+?) -")
        if explicit_dsp and explicit_dsp != "N/A":
            for stop in ("UPC", "Additional info", "\n", "\r"):
                idx = explicit_dsp.find(stop)
                if idx > 0:
                    explicit_dsp = explicit_dsp[:idx].strip()
            fields["dsp"] = explicit_dsp.strip() or "Unknown"
            fields["dsp_confidence"] = "low"
    fields["date_received"] = meta.get("date_formatted", "N/A")

    return fields

def _aeolus_rows_to_dict(parsed):
    """
    Convert an Aeolus queryData response (or wrapper) into a list of dicts.

    The script can return two shapes:
      A) Large result (>4KB): wrapper dict with keys:
           {"file": "...", "totalRows": N, "sampleRows": [...], "columns": [...]}
         where sampleRows is already a list of dicts OR list of lists.
         The full data is in the file as the raw API response.
      B) Small result: raw API response:
           {"data": {"columns": ["col1","col2",...], "data": [[v1,v2,...], ...]}}
         OR {"data": [[col1,...], [row1...], [row2...], ...]}  (array-of-arrays)

    Returns a list of dicts (may be empty).
    """
    if not isinstance(parsed, dict):
        return []

    # Shape A — wrapper with file + sampleRows
    total = parsed.get("totalRows")
    result_file = parsed.get("file", "")
    columns_hint = parsed.get("columns", [])

    def _arrays_to_dicts(columns, rows):
        out = []
        for row in rows:
            if isinstance(row, list):
                out.append(dict(zip(columns, row)))
            elif isinstance(row, dict):
                out.append(row)
        return out

    if total is not None:
        if total == 0:
            return []
        # Always prefer the full result file when present. sampleRows is only a
        # preview for truncated responses and can silently drop valid rows.
        if result_file and os.path.exists(result_file):
            with open(result_file) as fh:
                raw = json.load(fh)
            rows = _aeolus_rows_to_dict(raw)  # recurse into shape B/full payload
            if rows:
                return rows
        # Fallback only when no result file is available.
        sample = parsed.get("sampleRows", [])
        if isinstance(sample, list) and sample:
            if isinstance(sample[0], dict):
                return sample
            if isinstance(sample[0], list) and columns_hint:
                return _arrays_to_dicts(columns_hint, sample)
        return []

    # Shape B — raw API response: {"data": {...}} or {"data": [[...],...]}
    data_any = parsed.get("data")
    if data_any is None:
        return []

    if isinstance(data_any, list) and data_any:
        # array-of-arrays: first element is column names
        first = data_any[0]
        if isinstance(first, list):
            columns = [str(c) for c in first]
            return _arrays_to_dicts(columns, data_any[1:])
        # list of dicts
        if isinstance(first, dict):
            return data_any

    if isinstance(data_any, dict):
        cols_raw = data_any.get("columns", [])
        columns = [str(c) for c in cols_raw] if isinstance(cols_raw, list) else []
        rows_raw = data_any.get("data", [])
        if isinstance(rows_raw, list):
            if rows_raw and isinstance(rows_raw[0], dict):
                return rows_raw
            return _arrays_to_dicts(columns, rows_raw)

    return []


def _aeolus_sql_quote(value):
    return str(value).replace("'", "''")


AEOLUS_FRIENDLY_FIELDS = [
    "upc",
    "isrc",
    "album_title",
    "user_region",
    "source_type_name",
    "User Tier",
    "display_artist",
    "bd_manager_list",
    "operation_manager_list",
    "p_date",
]


def _run_aeolus_sql(sql, timeout=180, dataset_id=None):
    result = subprocess.run(
        [
            "python3", AEOLUS_SCRIPT,
            "--dataset-id", dataset_id or AEOLUS_DATASET,
            "--base-url", AEOLUS_BASE,
            "--sql", sql,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=os.path.dirname(os.path.abspath(__file__)) + "/..",
    )
    out = result.stdout.strip()
    print(f"  Aeolus raw (first 500): {out[:500]}")
    if result.returncode != 0 and not out:
        print(f"  Aeolus stderr: {result.stderr.strip()[:300]}")
        return None
    parsed = parse_lark_json(out)
    if not parsed:
        return None
    return parsed


def _friendly_aeolus_rows(parsed):
    rows = _aeolus_rows_to_dict(parsed)
    friendly_rows = []
    for row in rows:
        old_keys = list(row.keys())
        friendly_row = {}
        for i, friendly in enumerate(AEOLUS_FRIENDLY_FIELDS):
            # Prefer matching by column name (the SQL SELECT already aliases
            # columns to these friendly names). Positional mapping is only a
            # fallback so a change in column order can't silently map values to
            # the wrong fields (e.g. region ending up in album_title).
            if friendly in row:
                friendly_row[friendly] = row[friendly]
            elif i < len(old_keys):
                friendly_row[friendly] = row[old_keys[i]]
        for k, v in row.items():
            if k not in friendly_row and k not in AEOLUS_FRIENDLY_FIELDS:
                friendly_row[k] = v
        friendly_rows.append(friendly_row)
    return friendly_rows


def batch_query_aeolus_by_upc(upcs, chunk_size=40):
    """Batch query Aeolus Song Dimension by UPC and keep the latest row per UPC."""
    unique_upcs = []
    seen = set()
    for upc in upcs or []:
        value = str(upc or "").strip()
        if not value or value == "N/A" or value in seen:
            continue
        seen.add(value)
        unique_upcs.append(value)

    if not unique_upcs:
        return {}

    results = {}
    total_chunks = (len(unique_upcs) + chunk_size - 1) // chunk_size
    # Limit to recent daily partitions so each UPC returns only a few rows
    # (the dataset keeps ~600 historical snapshots per UPC otherwise).
    recent_cutoff = (datetime.utcnow() - timedelta(days=21)).strftime("%Y-%m-%d")
    for idx in range(0, len(unique_upcs), chunk_size):
        chunk = unique_upcs[idx:idx + chunk_size]
        chunk_no = idx // chunk_size + 1
        in_list = ", ".join(f"'{_aeolus_sql_quote(v)}'" for v in chunk)
        sql = (
            "SELECT `[upc]`, `[isrc]`, `[album_title]`, `[user_region]`, "
            "`[source_type_name]`, `[User Tier]`, `[display_artist]`, "
            "`[bd_manager_list]`, `[operation_manager_list]`, `[p_date]` "
            "FROM `[[AOP] Song Dimension]` "
            f"WHERE `[upc]` IN ({in_list}) AND `[p_date]` >= '{recent_cutoff}' "
            "ORDER BY `[p_date]` DESC"
        )
        print(f"  Aeolus batch chunk {chunk_no}/{total_chunks}: querying {len(chunk)} UPC(s)")
        parsed = _run_aeolus_sql(sql, timeout=240)
        if not parsed:
            print(f"  ✗ Could not parse Aeolus JSON for chunk {chunk_no}")
            continue
        rows = _friendly_aeolus_rows(parsed)
        if not rows:
            print(f"  ✗ Aeolus returned 0 rows for chunk {chunk_no}")
            continue
        for row in rows:
            upc = str(row.get("upc") or "").strip()
            if upc and upc not in results:
                results[upc] = row

    print(f"  ✓ Aeolus batch rows kept: {len(results)}/{len(unique_upcs)} unique UPC(s)")
    return results


def batch_query_engagement_by_upc(upcs, chunk_size=40):
    """Batch query Aeolus engagement dataset by UPC.

    Returns a dict keyed by UPC: {upc: {"uid", "tt_30d_vv", "sptf_30d_str"}}.
    Backward-compatible: any failure / timeout / parse error yields an empty
    mapping so callers fall back to "N/A" without breaking the workflow.
    """
    results = {}
    try:
        unique_upcs = []
        seen = set()
        for upc in upcs or []:
            value = str(upc or "").strip()
            if not value or value == "N/A" or value in seen:
                continue
            seen.add(value)
            unique_upcs.append(value)

        if not unique_upcs:
            return {}

        total_chunks = (len(unique_upcs) + chunk_size - 1) // chunk_size
        for idx in range(0, len(unique_upcs), chunk_size):
            chunk = unique_upcs[idx:idx + chunk_size]
            chunk_no = idx // chunk_size + 1
            in_list = ", ".join(f"'{_aeolus_sql_quote(v)}'" for v in chunk)
            sql = (
                "SELECT `[album_upc]` AS upc, `[user_id]`, "
                "SUM(`[tt_nonspam_item_vv_30d]`) AS tt_30d_vv, "
                "SUM(`[api_sptf_play_cnt_30d]`) AS sptf_30d_str "
                "FROM `[[AOP] dm_distribution_song_country_df]` "
                f"WHERE `[album_upc]` IN ({in_list}) "
                f"AND `[p_date]` = '{ENGAGEMENT_PARTITION}' "
                "GROUP BY `[album_upc]`, `[user_id]`"
            )
            print(f"  Engagement batch chunk {chunk_no}/{total_chunks}: querying {len(chunk)} UPC(s)")
            try:
                parsed = _run_aeolus_sql(sql, timeout=240, dataset_id=ENGAGEMENT_DATASET)
            except Exception as e:
                print(f"  ✗ Engagement query failed for chunk {chunk_no}: {e}")
                continue
            if not parsed:
                print(f"  ✗ Could not parse engagement JSON for chunk {chunk_no}")
                continue
            rows = _aeolus_rows_to_dict(parsed)
            if not rows:
                print(f"  ✗ Engagement returned 0 rows for chunk {chunk_no}")
                continue
            for row in rows:
                upc = str(row.get("upc") or "").strip()
                if not upc or upc in results:
                    continue
                results[upc] = {
                    "uid": row.get("user_id") if row.get("user_id") not in (None, "") else "N/A",
                    "tt_30d_vv": row.get("tt_30d_vv") if row.get("tt_30d_vv") is not None else "N/A",
                    "sptf_30d_str": row.get("sptf_30d_str") if row.get("sptf_30d_str") is not None else "N/A",
                }

        print(f"  ✓ Engagement batch rows kept: {len(results)}/{len(unique_upcs)} unique UPC(s)")
    except Exception as e:
        print(f"  ✗ Engagement batch lookup failed entirely: {e}")
        return {}
    return results


def _has_engagement_value(value):
    """Return True when a tracker engagement cell already has a real value."""
    text = str(value if value is not None else "").strip()
    return bool(text) and text.upper() != "N/A"


def enrich_with_engagement_once(ar):
    """Fetch engagement metrics only when they are absent from the row payload.

    Engagement metrics are intended to be a first-ingestion snapshot in the
    tracker. Existing tracker rows must not be refreshed/overwritten by later
    daily jobs; callers should only use this helper immediately before appending
    a brand-new tracker row.
    """
    if not isinstance(ar, dict):
        return ar
    if _has_engagement_value(ar.get("tt_30d_vv")) and _has_engagement_value(ar.get("sptf_30d_str")):
        return ar
    upc = str(ar.get("upc") or "").strip()
    if not upc or upc == "N/A":
        return ar
    try:
        eng = batch_query_engagement_by_upc([upc]).get(upc) or {}
    except Exception as e:
        print(f"  ✗ Engagement lookup raised for UPC {upc}, falling back to N/A: {e}")
        eng = {}
    return {**ar, **eng} if eng else ar


def query_aeolus(identifier, id_type="upc"):
    """Query Aeolus Song Dimension dataset for a given ISRC or UPC."""
    identifier = str(identifier or "").strip()
    if not identifier or identifier == "N/A":
        return {}

    if str(id_type).lower() == "upc":
        row = batch_query_aeolus_by_upc([identifier]).get(identifier, {})
        if not row:
            print(f"  ✗ Aeolus returned 0 rows for UPC {identifier}")
            return {}
        print(
            f"  ✓ Aeolus data: upc={row.get('upc')}, region={row.get('user_region')}, "
            f"source={row.get('source_type_name')}, tier={row.get('User Tier')}"
        )
        return row

    # Keep the single-ISRC path aligned with the UPC batch lookup: Aeolus / ClickHouse
    # enforces date-index pruning on this large daily snapshot dataset. Without a
    # p_date lower bound, the query can scan historical snapshots and fail with
    # force_index_by_date timeouts during manager alert resolution.
    recent_cutoff = (datetime.utcnow() - timedelta(days=21)).strftime("%Y-%m-%d")
    sql = (
        "SELECT `[upc]`, `[isrc]`, `[album_title]`, `[user_region]`, "
        "`[source_type_name]`, `[User Tier]`, `[display_artist]`, "
        "`[bd_manager_list]`, `[operation_manager_list]`, `[p_date]` "
        "FROM `[[AOP] Song Dimension]` "
        f"WHERE `[isrc]` = '{_aeolus_sql_quote(identifier)}' "
        f"AND `[p_date]` >= '{recent_cutoff}' "
        "ORDER BY `[p_date]` DESC LIMIT 1"
    )
    parsed = _run_aeolus_sql(sql)
    if not parsed:
        print(f"  ✗ Could not parse Aeolus JSON for {id_type.upper()} {identifier}")
        return {}

    rows = _friendly_aeolus_rows(parsed)
    if not rows:
        print(f"  ✗ Aeolus returned 0 rows for {id_type.upper()} {identifier}")
        return {}

    row = rows[0]
    print(
        f"  ✓ Aeolus data: upc={row.get('upc')}, region={row.get('user_region')}, "
        f"source={row.get('source_type_name')}, tier={row.get('User Tier')}"
    )
    return row

def _load_posted_claims():
    if not os.path.exists(POSTED_CLAIMS_FILE):
        return {}
    try:
        with open(POSTED_CLAIMS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_posted_claim(claim_key, record):
    if not claim_key:
        return
    data = _load_posted_claims()
    if isinstance(record, dict):
        if "chat_id" not in record:
            record = {**record, "chat_id": TARGET_CHAT_ID}
        if "region" not in record:
            record = {**record, "region": CURRENT_REGION}
        if "tracker_row" not in record:
            record = {**record, "tracker_row": None}
    data[claim_key] = record
    _atomic_write_json(POSTED_CLAIMS_FILE, data, ensure_ascii=False, indent=2)


def claim_key(ef, ar=None, subject=""):
    """Build a duplicate-prevention key. Same release can post again only for a different claim."""
    ar = ar or {}
    ref_id = ef.get("ref_id")
    if ref_id and ref_id != "N/A":
        return f"ref:{ref_id}"
    claim_id = first(subject or "", r"Claim\s+(\d{6,})")
    if claim_id != "N/A":
        return f"claim:{claim_id}"
    upc = ef.get("upc") or ar.get("upc") or "N/A"
    isrc = ef.get("isrc") or ar.get("isrc") or "N/A"
    claimant = (ef.get("claimant_email") or ef.get("claimant_name") or "N/A").lower()
    message = _message_preview(ef.get("claimant_message", "N/A"), limit=80).lower()
    return "|".join(["release_claim", str(upc), str(isrc), claimant, message])


def is_claim_already_posted(claim_key_value):
    return bool(claim_key_value and claim_key_value in _load_posted_claims())


def qualifies(row):
    """Check if the Aeolus row meets the alert criteria for the active region."""
    region = (row.get("user_region") or "").strip().upper()
    tier   = (row.get("User Tier") or "").strip()
    source = (row.get("source_type_name") or "").strip()
    if region not in QUALIFY_COUNTRIES:
        print(f"  ✗ Filtered: user_region={region!r} (need one of {sorted(QUALIFY_COUNTRIES)} for {CURRENT_REGION})")
        return False
    if source in ("AP", "A&R") or tier == "High Quality":
        print(f"  ✓ Qualifies: source={source!r}, tier={tier!r}")
        return True
    print(f"  ✗ Filtered: source={source!r}, tier={tier!r} (need AP/A&R/High Quality)")
    return False

def _ops_context_for_region(region: str):
    region = (region or CURRENT_REGION or "BR").upper()
    configs = {
        "BR": {
            "ops_dm_email": "filipe.cairo@bytedance.com",
            "ops_dm_open_id": "",
            "ops_dm_chat_id": "",
        },
        "US": {
            "ops_dm_email": "ben.gordon-pound@bytedance.com",
            "ops_dm_open_id": "ou_9cd2b961d55ed59e1b0e79e0b52a677c",
            "ops_dm_chat_id": "oc_842a762dacdea52dd8cd4017da3a94d5",
        },
        "SPLA": {
            "ops_dm_email": "bernardo.sanchez@bytedance.com",
            "ops_dm_open_id": "ou_b0be769000b08971e717c7c01323dabe",
            "ops_dm_chat_id": "oc_48de5eacf06bffee6cd4aa422e1e9855",
        },
    }
    return configs.get(region, configs["BR"])



def build_card(
    ef,
    ar,
    current_status="",
    lark_message_id="",
    source_email_message_id="",
    tracker_row="",
    region="",
    ops_dm_email="",
    ops_dm_open_id="",
    ops_dm_chat_id="",
):
    """Build the base Lark interactive card JSON.

    Note: the Spotify reply countdown badge is injected later for posted group
    cards via `build_posted_group_card()`, not directly inside this base card
    builder.
    """
    def v(val):
        return val if val and val != "N/A" else "N/A"

    display_title = ef.get("title") if ef.get("title") and ef.get("title") != "N/A" else ar.get("album_title")
    label_uid = ar.get("uid", "")
    bd_mentions = _mention_people(ar.get("bd_manager_list"), label_uid=label_uid)
    label_manager_mentions = _mention_people(ar.get("operation_manager_list"), label_uid=label_uid)
    artist_names = _format_artist_names(ar.get("display_artist"))
    claimant_message = _clean_email_value(ef.get("claimant_message", "N/A"))
    claimant_preview = _message_preview(claimant_message)
    upc_value = v(ef.get("upc"))
    upc_display = f"[{upc_value}](https://sg-musician-admin.bytedance.net/avenue/content/album/new?currentPage=1&pageSize=10&showFields=upc&upc={upc_value})" if upc_value != "N/A" else "N/A"
    region_value = (region or CURRENT_REGION or "BR").upper()
    ops_ctx = _ops_context_for_region(region_value)
    ref_id_value = ef.get("ref_id", "")
    source_email_value = source_email_message_id or ef.get("source_email_message_id", "")
    tracker_row_value = tracker_row if tracker_row not in (None, "") else ef.get("tracker_row", "")
    ops_dm_email_value = ops_dm_email or ef.get("ops_dm_email", "") or ops_ctx.get("ops_dm_email", "")
    ops_dm_open_id_value = ops_dm_open_id or ef.get("ops_dm_open_id", "") or ops_ctx.get("ops_dm_open_id", "")
    ops_dm_chat_id_value = ops_dm_chat_id or ef.get("ops_dm_chat_id", "") or ops_ctx.get("ops_dm_chat_id", "")

    status_buttons = []
    for status in ["🔴 Confirm Takedown", "🔍 Investigating", "⚖️ Disputing", "✅ Resolved"]:
        status_buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": status},
            "type": ("danger" if status.startswith("🔴") else "primary") if status == current_status else "default",
            "value": {
                "action": "copyright_alert_status_update",
                "status": status,
                "current_status": current_status,
                "message_id": lark_message_id,
                "upc": ef.get("upc", "N/A"),
                "isrc": ef.get("isrc", "N/A"),
                "tracker_row": tracker_row_value,
                "region": region_value,
            }
        })

    card = {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": "red",
            "title": {"tag": "plain_text", "content": "🚨 Copyright Infringement Alert"}
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**{v(display_title)}**\nArtist(s): {v(artist_names)}\n**Status: —**"}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": "**📧 Claim Details**"}},
            {
                "tag": "column_set",
                "flex_mode": "none",
                "background_style": "grey",
                "columns": [
                    {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                        {"tag": "div", "text": {"tag": "lark_md", "content": f"**UPC**\n{upc_display}"}}
                    ]},
                    {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                        {"tag": "div", "text": {"tag": "lark_md", "content": f"**ISRC**\n`{v(ef.get('isrc'))}`"}}
                    ]},
                    {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                        {"tag": "div", "text": {"tag": "lark_md", "content": f"**Email Source**\n{v(ef.get('email_source'))}"}}
                    ]},
                ]
            },
            {
                "tag": "column_set",
                "flex_mode": "none",
                "background_style": "grey",
                "columns": [
                    {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                        {"tag": "div", "text": {"tag": "lark_md", "content": f"**Claimant**\n{v(ef.get('claimant_name'))}"}}
                    ]},
                    {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                        {"tag": "div", "text": {"tag": "lark_md", "content": f"**DSP**\n{v(ef.get('dsp'))}"}}
                    ]},
                    {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                        {"tag": "div", "text": {"tag": "lark_md", "content": f"**Company**\n{v(ef.get('claimant_company'))}"}}
                    ]},
                    {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                        {"tag": "div", "text": {"tag": "lark_md", "content": f"**Email**\n{v(ef.get('claimant_email'))}"}}
                    ]},
                ]
            },
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": "**👤 Point of Contact**"}},
            {
                "tag": "column_set",
                "flex_mode": "none",
                "background_style": "grey",
                "columns": [
                    {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                        {"tag": "div", "text": {"tag": "lark_md", "content": f"**BD**\n{v(bd_mentions)}"}}
                    ]},
                    {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                        {"tag": "div", "text": {"tag": "lark_md", "content": f"**Label Manager**\n{v(label_manager_mentions)}"}}
                    ]},
                ]
            },
            {
                "tag": "action",
                "actions": status_buttons
            },
            {"tag": "hr"},
            {
                "tag": "collapsible_panel",
                "expanded": False,
                "header": {
                    "title": {"tag": "plain_text", "content": "Claimant Message"},
                    "subtitle": {"tag": "plain_text", "content": v(claimant_preview)}
                },
                "elements": [
                    {"tag": "div", "text": {"tag": "lark_md", "content": claimant_message}}
                ]
            },
            {"tag": "hr"},
            {"tag": "note", "elements": [
                {"tag": "plain_text", "content": f"{v(ef.get('email_source'))} · {v(ef.get('dsp'))} · Received {v(ef.get('date_received'))}"}
            ]}
        ]
    }
    return card


def build_posted_group_card(
    ef,
    ar,
    posted_message_id,
    *,
    current_status="",
    source_email_message_id="",
    tracker_row="",
    region="",
    ops_dm_email="",
    ops_dm_open_id="",
    ops_dm_chat_id="",
):
    """Build the canonical patchable group card for an already-posted message.

    This centralizes the second-pass card build used after `post_card()` so all
    manual/backfill/recovery flows consistently include the Spotify reply
    countdown badge when the detected date is available.
    """
    card = build_card(
        ef,
        ar,
        current_status=current_status,
        lark_message_id=posted_message_id,
        source_email_message_id=source_email_message_id,
        tracker_row=tracker_row,
        region=region,
        ops_dm_email=ops_dm_email,
        ops_dm_open_id=ops_dm_open_id,
        ops_dm_chat_id=ops_dm_chat_id,
    )

    try:
        from datetime import date as _date
        from copyright_alert.daily_workflow import (
            build_card_with_countdown,
            REPLY_DEADLINE_CALENDAR_DAYS,
        )
        from copyright_alert.dm_action_card import parse_detected_date

        detected = parse_detected_date(ef.get("date_received", ""))
        if detected is not None:
            days_remaining = REPLY_DEADLINE_CALENDAR_DAYS - (_date.today() - detected).days
            card = build_card_with_countdown(card, days_remaining)
    except Exception as exc:
        print(f"  ⚠ Could not add initial countdown badge: {exc!r}")

    return card


def update_tracker_message_ids(row_num, message_id):
    """Update the tracker row's message-id columns (P/S) in place."""
    try:
        row_num = int(row_num)
    except (TypeError, ValueError):
        print(f"  ⚠ Invalid tracker row for message-id update: {row_num!r}")
        return False

    msg_id_str = str(message_id or "").strip()
    if not msg_id_str:
        print("  ⚠ Missing message_id for tracker update")
        return False

    updates = {
        f"P{row_num}": _tracker_cell(msg_id_str, text=True),
        f"S{row_num}": _tracker_cell(msg_id_str, text=True),
    }
    ok = True
    for cell_addr, cell in updates.items():
        cmd = [
            "lark-cli", "sheets", "+cells-set", "--url", TRACKER_SHEET_URL,
            "--sheet-id", TRACKER_SHEET_ID, "--range", cell_addr,
            "--cells", json.dumps([[cell]], ensure_ascii=False),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        print(f"  Tracker message-id update {cell_addr} rc:", res.returncode)
        print((res.stdout + res.stderr)[:500])
        ok = ok and res.returncode == 0
    return ok


def _tracker_row_has_case_identity(values):
    """Return True when a tracker row represents a real case row.

    The Email Status column can be written independently by callback cards. A
    stale or incorrect callback row number must not make a far-away, otherwise
    blank row look like the end of the tracker. Treat a row as a case only when
    it has at least one stable case identifier / core field, not merely a status
    note in column T.
    """
    if not values:
        return False
    identity_cols = ("A", "B", "C", "D", "O", "P", "S")
    return any(str(values.get(col) or "").strip() for col in identity_cols)


def _tracker_physical_row_count(default=2000):
    """Return the sheet's physical row count so append scans can cover the full tab."""
    cmd = [
        "lark-cli", "sheets", "+workbook-info", "--url", TRACKER_SHEET_URL,
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        parsed = parse_lark_json(res.stdout)
        sheets = ((parsed or {}).get("data") or {}).get("sheets") or []
        for sheet in sheets:
            if str(sheet.get("sheet_id") or "").strip() == str(TRACKER_SHEET_ID).strip():
                return int(sheet.get("row_count") or default)
    except Exception as exc:
        print(f"  ⚠ Could not inspect tracker row count: {exc!r}")
    return default



def _tracker_next_row():
    """Return the next writable row after the last real tracker case row.

    This intentionally ignores rows that only contain follow-up metadata such as
    Email Status (column T). Those can appear if an old callback carried a stale
    tracker_row value, and counting them would create a large phantom gap before
    the next append.

    The scan covers the full physical sheet in chunks instead of assuming the
    first 500 rows are enough. That guarantees new rows are appended to the true
    bottom of the tracker, never written back near the top because of a partial
    pre-read.
    """
    physical_rows = max(_tracker_physical_row_count(), 2)
    case_rows = []
    nonempty_rows = []
    chunk_size = 500

    for start_row in range(1, physical_rows + 1, chunk_size):
        end_row = min(start_row + chunk_size - 1, physical_rows)
        cmd = [
            "lark-cli", "sheets", "+csv-get", "--url", TRACKER_SHEET_URL,
            "--sheet-id", TRACKER_SHEET_ID, "--range", f"A{start_row}:T{end_row}",
            "--max-chars", "200000",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        parsed, rows, row_numbers = parse_lark_annotated_csv(res.stdout)
        if res.returncode != 0 or not parsed:
            print("  ⚠ Could not inspect tracker before append:", (res.stdout + res.stderr)[:500])
            return None

        for idx, values in enumerate(rows):
            row_number = row_numbers[idx] if idx < len(row_numbers) else (start_row + idx)
            try:
                row_number = int(row_number or 0)
            except (TypeError, ValueError):
                continue
            if any(str(value or "").strip() for value in values):
                nonempty_rows.append(row_number)
            row_map = {chr(ord("A") + col_idx): value for col_idx, value in enumerate(values)}
            if _tracker_row_has_case_identity(row_map):
                case_rows.append(row_number)

    # Append after the last existing/non-empty row, not just after the last row
    # that looks like a case. This avoids writing near the top when the sheet has
    # formulas, notes, or manual content below the last case row.
    occupied_rows = case_rows + nonempty_rows
    if occupied_rows:
        return max(occupied_rows) + 1
    return 2


def _tracker_cell(value, *, text=False):
    # Double-ensure casting to string before writing to the cell value
    value_str = str(value if value is not None else "N/A")
    cell = {"value": value_str}
    if text:
        cell["cell_styles"] = {"number_format": "@"}
    return cell


def append_tracker_row(ef, ar, message_id, status=""):
    """Append the posted alert to the tracker sheet so later callbacks can update it.

    Engagement columns (tt_30d_vv / sptf_30d_str) are populated as a one-time
    snapshot only when a brand-new tracker row is appended. Existing rows are
    never refreshed or overwritten by daily jobs.

    Columns A:T (20):
      A UPC, B ISRC, C Title, D UID, E Source (Aeolus source_type_name, e.g.
      "AP", "A&R", "UG-Paid ads"), F tt_30d_vv, G sptf_30d_str, H Artist(s),
      I DSP, J Claimant, K Email Source, L BD, M Label Manager, N Status,
      O Date Received (reused as the "detected at" timestamp for the Spotify
      reply countdown), P Lark Message ID, Q Notes, R Admin Action Taken,
      S Card Message ID (alias of P — same group-card message_id, written to
      both so the daily countdown refresh can find it easily),
      T Email Status (filled in later once a Spotify reply is sent).

    Returns the appended tracker row number on success, else None.
    """
    ar = enrich_with_engagement_once(ar or {})

    # Explicitly cast identifiers to strings to harden against spreadsheet formatting issues
    upc_str = str(ef.get("upc") or "N/A").strip()
    isrc_str = str(ef.get("isrc") or "N/A").strip()
    uid_str = str(ar.get("uid") or "N/A").strip()
    msg_id_str = str(message_id or "N/A").strip()

    row = [[
        _tracker_cell(upc_str, text=True),
        _tracker_cell(isrc_str, text=True),
        _tracker_cell(ef.get("title") if ef.get("title") != "N/A" else ar.get("album_title", "N/A")),
        _tracker_cell(uid_str, text=True),
        _tracker_cell(ar.get("source_type_name", "N/A")),  # E Source
        _tracker_cell(ar.get("tt_30d_vv", "N/A")),
        _tracker_cell(ar.get("sptf_30d_str", "N/A")),
        _tracker_cell(_format_artist_names(ar.get("display_artist"))),
        _tracker_cell(ef.get("dsp", "N/A")),
        _tracker_cell(ef.get("claimant_name", "N/A")),
        _tracker_cell(ef.get("email_source", "N/A")),
        _tracker_cell(", ".join(_display_name_from_username(p) for p in _parse_people_list(ar.get("bd_manager_list"))) or "N/A"),
        _tracker_cell(", ".join(_display_name_from_username(p) for p in _parse_people_list(ar.get("operation_manager_list"))) or "N/A"),
        _tracker_cell(status),
        _tracker_cell(ef.get("date_received", "N/A")),
        _tracker_cell(msg_id_str, text=True),
        _tracker_cell("Initial alert posted. Card contains reversible status buttons; sheet row logged at post time."),
        _tracker_cell(""),          # R Admin Action Taken (filled later by daily workflow)
        _tracker_cell(msg_id_str, text=True),  # S Card Message ID (alias of P / Lark Message ID)
        _tracker_cell(""),          # T Email Status (filled after a Spotify reply is sent)
    ]]
    assert len(row[0]) == 20, f"Tracker row schema drift: expected 20 cells, got {len(row[0])}"

    next_row = _tracker_next_row()
    if not next_row:
        return None
    target_range = f"A{next_row}:T{next_row}"
    cmd = [
        "lark-cli", "sheets", "+cells-set", "--url", TRACKER_SHEET_URL,
        "--sheet-id", TRACKER_SHEET_ID, "--range", target_range,
        "--allow-overwrite=false",
        "--cells", json.dumps(row, ensure_ascii=False),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    print("  Tracker write rc:", res.returncode)
    print("  Tracker write range:", target_range)
    print("  Tracker write output:", (res.stdout + res.stderr)[:500])
    return next_row if res.returncode == 0 else None


def post_card(card, aeolus_row=None, *, upc=None, context="group post"):
    """Post the card to Lark via direct REST API using BOT_APP_ID as sender.

    When claim metadata is available, enforce the chat/region guard inside the
    final posting primitive so callers cannot accidentally route an out-of-scope
    UPC to the currently configured group.
    """
    if aeolus_row is not None:
        assert_region_allowed(TARGET_CHAT_ID, aeolus_row, upc=upc, context=context)

    def make_request():
        token = _get_bot_access_token()
        if not token:
            raise RuntimeError("Could not get bot access token")
        url = "https://open.larksuite.com/open-apis/im/v1/messages?receive_id_type=chat_id"
        body = json.dumps({
            "receive_id": TARGET_CHAT_ID,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        }).encode("utf-8")
        return urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {token}",
            },
        )

    try:
        parsed = request_json_with_auth_retry(make_request, timeout=60, context=f"run_alert.post_card:{TARGET_CHAT_ID}")
        print(f"Post result: code={parsed.get('code')} msg={parsed.get('msg')}")
        return parsed.get("code") == 0, ((parsed.get("data") or {}).get("message_id") or "")
    except Exception as exc:
        print(f"Post failed via REST: {exc}")
        try:
            parsed = _send_interactive_via_lark_cli(
                receive_id_type="chat_id",
                receive_id=TARGET_CHAT_ID,
                content=json.dumps(card, ensure_ascii=False),
                timeout=60,
            )
            print(f"Post fallback result: code={parsed.get('code')} msg={parsed.get('msg')}")
            return parsed.get("code") == 0, ((parsed.get("data") or {}).get("message_id") or "")
        except Exception as fallback_exc:
            print(f"Post fallback failed: {fallback_exc}")
            return False, ""

def _get_bot_access_token():
    """Obtain a tenant_access_token for the bot via Lark REST API."""
    url = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
    payload = json.dumps({"app_id": BOT_APP_ID, "app_secret": BOT_SECRET}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return data.get("tenant_access_token", "")
    except Exception as e:
        print(f"  ⚠ Could not get bot token: {e}")
        return ""


# ── Posted-card persistence ──────────────────────────────────────────────────
# The Lark GET /im/v1/messages/{id} API does NOT return the interactive card in
# its original schema ({config, header, elements:[{tag:div,...}]}). It returns a
# *rendered post format* ({title, elements:[[{tag:text,...}]]}) that cannot be
# fed back into PATCH (doing so → HTTP 400 Bad Request). To refresh an existing
# card we therefore keep a copy of the exact card JSON we sent, keyed by the
# message_id, and patch THAT (with the countdown badge merged in) instead.
POSTED_CARDS_FILE = "copyright_alert/posted_cards.json"


def _load_posted_cards():
    try:
        with open(POSTED_CARDS_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_posted_card(message_id, card):
    """Persist the exact interactive-card JSON keyed by message_id."""
    if not message_id or not isinstance(card, dict):
        return
    try:
        store = _load_posted_cards()
        store[message_id] = card
        _atomic_write_json(POSTED_CARDS_FILE, store, ensure_ascii=False, indent=None)
    except Exception as e:
        print(f"  ⚠ Could not persist posted card {message_id}: {e}")


def load_posted_card(message_id):
    """Return the previously-persisted card JSON for a message_id, or None."""
    if not message_id:
        return None
    return _load_posted_cards().get(message_id)


def patch_card_message(message_id, card):
    """Update an already-sent interactive card via Lark REST API (PATCH /im/v1/messages/{id}).

    The PATCH body must be {"content": "<json-stringified card>"} where the card
    is the interactive-card object itself ({config, header, elements}). We pass
    ensure_ascii=False so emoji/non-ASCII glyphs are preserved in the same way
    they were originally posted.
    """
    if not message_id:
        return False
    if not isinstance(card, dict) or "elements" not in card:
        # Guard against the rendered post-format ({title, elements:[[...]]}) or
        # any non-card object slipping through — those always 400.
        print(f"  ⚠ Patch skipped: card for {message_id} is not a valid "
              f"interactive-card object (got keys: "
              f"{list(card.keys()) if isinstance(card, dict) else type(card).__name__}).")
        return False

    def make_request():
        token = _get_bot_access_token()
        if not token:
            raise RuntimeError("Could not get bot access token")
        url = f"https://open.larksuite.com/open-apis/im/v1/messages/{message_id}"
        body = json.dumps({
            "content": json.dumps(card, ensure_ascii=False)
        }, ensure_ascii=False).encode("utf-8")
        return urllib.request.Request(
            url, data=body, method="PATCH",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {token}",
            }
        )

    try:
        result = request_json_with_auth_retry(make_request, timeout=15, context=f"run_alert.patch_card_message:{message_id}")
        ok = result.get("code") == 0
        print(f"  Patch result: code={result.get('code')} msg={result.get('msg')}")
        if ok:
            # Keep the persisted copy in sync so the next refresh patches the
            # latest card shape rather than the GET rendered format.
            save_posted_card(message_id, card)
        return ok
    except Exception as e:
        print(f"  ⚠ Patch failed: {e}")
        return False

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    candidates = fetch_recent_candidates()
    if not candidates:
        print("\n⚠️  No candidate emails fetched from the live inbox search.")
        sys.exit(1)

    for msg_id, subject in candidates:
        print(f"\n{'='*60}")
        print(f"Processing: {subject[:80]}")
        print(f"Message ID: {msg_id}")

        body, meta = fetch_email(msg_id)
        if not body:
            print("  ✗ Could not fetch email body, skipping")
            continue

        ef = extract_fields(body, subject, meta)
        upc = ef.get("upc", "")
        isrc = ef.get("isrc", "")
        print(f"  UPC extracted: {upc}")
        print(f"  ISRC extracted: {isrc}")

        lookup_id = isrc if isrc and isrc != "N/A" else upc
        lookup_type = "isrc" if isrc and isrc != "N/A" else "upc"
        if upc and upc != "N/A" and is_upc_excluded(upc):
            print(f"  ✗ Skipping excluded UPC {upc}")
            continue
        if not lookup_id or lookup_id == "N/A":
            print("  ✗ No ISRC or UPC found, skipping")
            continue

        print(f"  Querying Aeolus for {lookup_type.upper()} {lookup_id}...")
        ar = query_aeolus(lookup_id, lookup_type)
        if not ar:
            print("  ✗ No Aeolus data found, skipping")
            continue

        print(f"  Aeolus row: {ar}")
        if (not ef.get("isrc") or ef.get("isrc") == "N/A") and ar.get("isrc"):
            ef["isrc"] = str(ar.get("isrc")).strip() or "N/A"
            print(f"  ISRC filled from Aeolus: {ef['isrc']}")

        if not qualifies(ar):
            print("  ✗ Skipping not qualifying after Aeolus filters")
            continue

        duplicate_key = claim_key(ef, ar, subject)
        if is_claim_already_posted(duplicate_key):
            print(f"  ✗ Skipping duplicate claim already posted: {duplicate_key}")
            continue

        print("  ✓ Building and posting Lark card...")
        card = build_card(ef, ar)

        # Save card for inspection
        with open("copyright_alert/last_card.json", "w") as f:
            json.dump(card, f, indent=2)
        print("  Card saved to copyright_alert/last_card.json")

        success, posted_message_id = post_card(card, ar, upc=ef.get("upc"), context=f"{CURRENT_REGION} run_alert group post")
        if success and posted_message_id:
            card = build_posted_group_card(
                ef,
                ar,
                posted_message_id,
                source_email_message_id=msg_id,
                region=CURRENT_REGION,
            )
            with open("copyright_alert/last_card.json", "w") as f:
                json.dump(card, f, indent=2)
            patch_card_message(posted_message_id, card)
            tracker_row = append_tracker_row(ef, ar, posted_message_id, status="")
            _save_posted_claim(duplicate_key, {
                "message_id": posted_message_id,
                "source_email_message_id": msg_id,
                "subject": subject,
                "upc": ef.get("upc", "N/A"),
                "isrc": ef.get("isrc", "N/A"),
                "title": ef.get("title") if ef.get("title") != "N/A" else ar.get("album_title", "N/A"),
                "artist": _format_artist_names(ar.get("display_artist")),
                "ref_id": ef.get("ref_id", "N/A"),
                "dsp": ef.get("dsp", "Unknown"),
                "dsp_confidence": ef.get("dsp_confidence", "low"),
                "claimant_name": ef.get("claimant_name", "N/A"),
                "claimant_email": ef.get("claimant_email", "N/A"),
                "region": CURRENT_REGION,
                "tracker_row": tracker_row,
                "chat_id": TARGET_CHAT_ID,
            })
        if success:
            print(f"\n✅ Alert posted successfully for UPC {upc}!")
            print(f"   Title:  {ef.get('title')}")
            print(f"   Artist: {ar.get('display_artist')}")
            print(f"   Source: {ar.get('source_type_name')}, Tier: {ar.get('User Tier')}")
            sys.exit(0)
        else:
            print("  ✗ Card posting failed, trying next candidate")

    print("\n⚠️  No qualifying email found in the candidate list.")
    sys.exit(1)

if __name__ == "__main__":
    main()
