"""Lark Mail reply-draft helper using a persisted OAuth refresh token.

Expected setup:
    python3 copyright_alert/oauth_setup.py

Public entry point:
    create_reply_draft(mailbox, thread_message_id, to, subject, body_html)
"""

from __future__ import annotations

import base64
import importlib
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

APP_ID_EXPECTED = "cli_aa94690b12b81cde"
TOKEN_FILE = ROOT / "runtime" / "lark_oauth_secret.json"
REFRESH_URL = "https://open.larksuite.com/open-apis/authen/v1/refresh_access_token"
MAIL_API_BASE = "https://open.larksuite.com/open-apis/mail/v1"
TOKEN_REFRESH_SKEW_SECONDS = 120
TAG_RE = re.compile(r"<[^>]+>")
EMAIL_RE = re.compile(r"^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$")
# Spotify (and AudioSalad) claim emails end with a tracking code like
# "ref:_00Dabc123!_00Nxyz456:ref". It MUST be preserved verbatim in the reply
# body so the claimant's ticketing system threads our response correctly.
REF_CODE_RE = re.compile(r"ref:[^\s:]+:ref")


class LarkMailDraftError(RuntimeError):
    """Raised when OAuth refresh or draft creation fails."""


def _first_attr(module: Any, names: tuple[str, ...]) -> Optional[str]:
    for name in names:
        value = getattr(module, name, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def load_app_credentials() -> Tuple[str, str, str]:
    """Load app_id/app_secret from project config modules.

    Preferred locations are copyright_alert.lark_auth and copyright_alert.config.
    The project currently keeps these credentials in copyright_alert.run_alert, so
    that module is used as a compatibility fallback.
    """
    candidates = (
        "copyright_alert.lark_auth",
        "copyright_alert.config",
        "copyright_alert.run_alert",
    )
    app_id_names = ("APP_ID", "APPID", "LARK_APP_ID", "BOT_APP_ID", "app_id")
    secret_names = ("APP_SECRET", "LARK_APP_SECRET", "BOT_SECRET", "app_secret")

    errors: list[str] = []
    for module_name in candidates:
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            errors.append(f"{module_name}: {exc!r}")
            continue

        app_id = _first_attr(module, app_id_names)
        app_secret = _first_attr(module, secret_names)
        if app_id and app_secret:
            if app_id != APP_ID_EXPECTED:
                raise LarkMailDraftError(
                    f"Loaded app_id {app_id!r} from {module_name}, expected {APP_ID_EXPECTED!r}."
                )
            return app_id, app_secret, module_name

        errors.append(f"{module_name}: app_id/app_secret attributes not found")

    raise LarkMailDraftError("Could not load Lark app credentials. Tried: " + "; ".join(errors))


def _load_oauth_record() -> Dict[str, Any]:
    if not TOKEN_FILE.exists():
        raise LarkMailDraftError(f"OAuth token file not found: {TOKEN_FILE}. Run copyright_alert/oauth_setup.py first.")
    try:
        record = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        raise LarkMailDraftError(f"Could not read OAuth token file {TOKEN_FILE}: {exc!r}") from exc
    if not isinstance(record, dict) or not record.get("refresh_token"):
        raise LarkMailDraftError(f"OAuth token file is missing refresh_token: {TOKEN_FILE}")
    return record


def _save_oauth_record(record: Dict[str, Any]) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")


def _request_json(
    method: str,
    url: str,
    payload: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 30,
) -> Dict[str, Any]:
    req_headers = {"Content-Type": "application/json; charset=utf-8"}
    if headers:
        req_headers.update(headers)
    data_bytes = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data_bytes, headers=req_headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        raise LarkMailDraftError(f"HTTP {exc.code} from {url}: {text[:1200]}") from exc
    except urllib.error.URLError as exc:
        raise LarkMailDraftError(f"Network error calling {url}: {exc!r}") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LarkMailDraftError(f"Non-JSON response from {url}: {text[:1200]}") from exc

    code = data.get("code", 0)
    if code not in (0, "0", None):
        raise LarkMailDraftError(f"Lark API error from {url}: {json.dumps(data, ensure_ascii=False)}")
    return data


def _post_json(url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None, timeout: int = 30) -> Dict[str, Any]:
    return _request_json("POST", url, payload=payload, headers=headers, timeout=timeout)


def _get_json(url: str, headers: Optional[Dict[str, str]] = None, timeout: int = 30) -> Dict[str, Any]:
    return _request_json("GET", url, payload=None, headers=headers, timeout=timeout)


def _int_or_none(value: Any) -> Optional[int]:
    if value in (None, "", False):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _token_expired(record: Dict[str, Any]) -> bool:
    token = record.get("user_access_token") or record.get("access_token")
    expires_at = _int_or_none(
        record.get("user_access_token_expires_at")
        or record.get("access_token_expires_at")
    )
    if not token:
        return True
    if expires_at is None:
        return True
    return expires_at <= int(time.time()) + TOKEN_REFRESH_SKEW_SECONDS


def _refresh_token_expired(record: Dict[str, Any]) -> bool:
    """Return True only when an explicit refresh-token expiry is present and stale.

    Some OAuth saves omit refresh_token_expires_at / refresh_token_expires_in.
    In that case we must not fail pre-emptively; we simply skip the expiry check
    and let the refresh endpoint be the source of truth.
    """
    expires_at = _int_or_none(record.get("refresh_token_expires_at"))
    if expires_at is None:
        return False
    return expires_at <= int(time.time()) + TOKEN_REFRESH_SKEW_SECONDS


def refresh_user_access_token(force: bool = False) -> str:
    """Return a valid user access token, refreshing with the stored refresh token."""
    record = _load_oauth_record()
    if not force and not _token_expired(record):
        token = record.get("user_access_token") or record.get("access_token")
        return str(token)

    if _refresh_token_expired(record):
        raise LarkMailDraftError(
            "Stored OAuth refresh token is expired. Re-run copyright_alert/oauth_setup.py to re-authorize."
        )

    app_id, app_secret, source_module = load_app_credentials()
    payload = {
        "grant_type": "refresh_token",
        "app_id": app_id,
        "app_secret": app_secret,
        "refresh_token": record["refresh_token"],
    }
    response = _post_json(REFRESH_URL, payload)
    data = response.get("data") if isinstance(response.get("data"), dict) else response

    user_access_token = data.get("access_token") or data.get("user_access_token")
    refresh_token = data.get("refresh_token") or record.get("refresh_token")
    if not user_access_token:
        raise LarkMailDraftError(f"Refresh response did not include a user access token: {json.dumps(response, ensure_ascii=False)}")

    now = int(time.time())
    expires_in = _int_or_none(data.get("expires_in") or data.get("access_token_expires_in"))
    refresh_expires_in = _int_or_none(
        data.get("refresh_token_expires_in")
        or data.get("refresh_expires_in")
    )
    record.update(
        {
            "app_id": app_id,
            "source_module": source_module,
            "refresh_token": refresh_token,
            "refresh_token_expires_in": refresh_expires_in,
            "refresh_token_expires_at": now + refresh_expires_in if refresh_expires_in is not None else record.get("refresh_token_expires_at"),
            "user_access_token": user_access_token,
            "access_token": user_access_token,
            "user_access_token_expires_in": expires_in,
            "expires_in": expires_in,
            "user_access_token_expires_at": now + expires_in if expires_in is not None else None,
            "access_token_expires_at": now + expires_in if expires_in is not None else None,
            "token_type": data.get("token_type") or record.get("token_type"),
            "scope": data.get("scope") or record.get("scope"),
            "raw_response": response,
            "updated_at": now,
        }
    )
    _save_oauth_record(record)
    return user_access_token


def _is_valid_email(value: Any) -> bool:
    email = str(value or "").strip()
    if not email or email.upper() == "N/A":
        return False
    return bool(EMAIL_RE.match(email))


def _recipient_list(value: Union[str, Dict[str, Any], Iterable[Union[str, Dict[str, Any]]]]) -> List[Dict[str, str]]:
    if isinstance(value, str):
        items: Iterable[Union[str, Dict[str, Any]]] = [value]
    elif isinstance(value, dict):
        items = [value]
    else:
        items = value

    recipients: List[Dict[str, str]] = []
    invalid_values: List[str] = []
    for item in items:
        if isinstance(item, str):
            email = item.strip()
            if not email:
                continue
            if not _is_valid_email(email):
                invalid_values.append(email)
                continue
            recipients.append({"mail_address": email})
        elif isinstance(item, dict):
            email = item.get("mail_address") or item.get("email") or item.get("address")
            name = item.get("name") or item.get("display_name")
            if not email:
                continue
            if not _is_valid_email(email):
                invalid_values.append(str(email))
                continue
            entry = {"mail_address": str(email).strip()}
            if name:
                entry["name"] = str(name)
            recipients.append(entry)
    if not recipients:
        detail = f" Invalid value(s): {', '.join(invalid_values[:3])}" if invalid_values else ""
        raise LarkMailDraftError(
            "At least one valid recipient email is required to create a reply draft." + detail
        )
    return recipients


def _html_to_plain_text(body_html: str) -> str:
    text = TAG_RE.sub(" ", body_html or "")
    text = text.replace("&nbsp;", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip() or " "


def _message_reference(value: str) -> str:
    ref = (value or "").strip()
    if not ref:
        return ""
    if ref.startswith("<") and ref.endswith(">"):
        return ref
    return f"<{ref}>"


def _lark_cli_json(args: List[str], timeout: int = 90) -> Dict[str, Any]:
    """Run a lark-cli mail command and return the parsed JSON payload.

    Reuses run_alert's tolerant JSON parser (lark-cli may emit proxy/warning
    lines to stdout before the JSON object). Returns {} on any failure.
    """
    try:
        proc = subprocess.run(
            ["lark-cli", "mail", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception as exc:  # pragma: no cover - defensive
        print(f"  ⚠ lark-cli invocation failed: {exc!r}", flush=True)
        return {}
    out = proc.stdout or ""
    try:
        from copyright_alert import run_alert as ra  # lazy: avoid import cycle
        parsed = ra.parse_lark_json(out)
    except Exception:
        # Fall back to a plain best-effort JSON extraction.
        parsed = None
        start = out.find("{")
        if start != -1:
            try:
                parsed = json.loads(out[start:])
            except Exception:
                parsed = None
    if not isinstance(parsed, dict):
        if proc.returncode != 0:
            print(f"  ⚠ lark-cli rc={proc.returncode}: {(out + proc.stderr)[:400]}", flush=True)
        return {}
    return parsed


def _first_nested_value(obj: Any, names: tuple[str, ...]) -> Any:
    """Return the first matching key value found in a nested dict/list payload."""
    if isinstance(obj, dict):
        for name in names:
            value = obj.get(name)
            if value not in (None, "", []):
                return value
        for value in obj.values():
            found = _first_nested_value(value, names)
            if found not in (None, "", []):
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _first_nested_value(item, names)
            if found not in (None, "", []):
                return found
    return None


def _normalize_original_message_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize CLI/OpenAPI message payloads to the fields this module needs."""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    message = data.get("message") if isinstance(data.get("message"), dict) else data
    if not isinstance(message, dict):
        message = {}
    smtp_message_id = _first_nested_value(
        message,
        (
            "smtp_message_id",
            "smtp_messageId",
            "rfc_message_id",
            "rfc_messageId",
            "internet_message_id",
            "internetMessageId",
            "message_id_header",
            "messageIdHeader",
            "message-id",
            "Message-ID",
        ),
    )
    headers = _first_nested_value(message, ("headers", "internet_headers", "internetHeaders"))
    if not smtp_message_id and isinstance(headers, dict):
        smtp_message_id = _first_nested_value(headers, ("Message-ID", "Message-Id", "message-id", "message_id"))
    if not smtp_message_id and isinstance(headers, list):
        for header in headers:
            if not isinstance(header, dict):
                continue
            name = str(header.get("name") or header.get("key") or "").lower()
            if name == "message-id":
                smtp_message_id = header.get("value")
                break
    normalized = dict(message)
    if smtp_message_id:
        normalized["smtp_message_id"] = str(smtp_message_id).strip()
    thread_id = _first_nested_value(message, ("thread_id", "threadId"))
    if thread_id:
        normalized["thread_id"] = str(thread_id).strip()
    references = _first_nested_value(message, ("references", "References"))
    if references:
        normalized["references"] = references
    sender = _first_nested_value(message, ("head_from", "from", "sender"))
    if isinstance(sender, dict):
        normalized["head_from"] = sender
    subject = _first_nested_value(message, ("subject", "Subject"))
    if subject:
        normalized["subject"] = str(subject)
    body_plain = _first_nested_value(message, ("body_plain_text", "bodyPlainText", "plain_text", "plainText", "body_text"))
    if body_plain:
        normalized["body_plain_text"] = str(body_plain)
    return normalized


def _fetch_original_message(mailbox: str, message_id: str, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Fetch the original inbound email so we can thread the reply correctly.

    ``message_id`` must be the Lark Mail message_id of the original email. It is
    not an IM card/open_message_id. We first call the official Mail message get
    endpoint, then fall back to the local lark-cli shortcut for older runtimes.
    """
    if not message_id:
        return {}
    encoded_mailbox = urllib.parse.quote(mailbox, safe="")
    encoded_message_id = urllib.parse.quote(message_id, safe="")
    url = f"{MAIL_API_BASE}/user_mailboxes/{encoded_mailbox}/messages/{encoded_message_id}?format=full"
    if headers:
        try:
            return _normalize_original_message_payload(_get_json(url, headers=headers))
        except Exception as exc:
            print(f"  ⚠ OpenAPI message lookup failed for {message_id!r}: {exc!r}", flush=True)
    parsed = _lark_cli_json(
        ["+message", "--mailbox", mailbox, "--message-id", message_id, "--html=false", "--format", "json"]
    )
    if not parsed:
        return {}
    return _normalize_original_message_payload(parsed)


def _looks_like_im_message_id(message_id: str) -> bool:
    """Return True when a value is clearly a Lark IM message/card ID.

    Lark Mail message IDs are opaque base64-ish strings. Lark IM group/card
    messages use prefixes such as ``om_`` / ``omt_``. Passing an IM ID into Mail
    APIs can never resolve the original RFC Message-ID, so treat it as a signal
    to search the mailbox by claim identifiers instead.
    """
    value = (message_id or "").strip()
    return value.startswith(("om_", "omt_"))


def _has_threading_metadata(original: Dict[str, Any]) -> bool:
    """Whether a fetched mail payload contains the minimum reply-thread data."""
    return bool((original or {}).get("smtp_message_id"))


def _find_original_message_id(mailbox: str, upc: str = "", ref_id: str = "") -> str:
    """Locate the original inbound claim email by ref code or UPC.

    The reliable source of truth for threaded reply drafts is the mailbox itself:
    first search the unique ``ref:...:ref`` code, then fall back to the UPC. This
    avoids relying on tracker/checkpoint fields that may contain a Lark IM card
    message ID instead of a Lark Mail inbox message ID.
    """
    for query in [q for q in (ref_id, upc) if q]:
        parsed = _lark_cli_json(
            ["+triage", "--mailbox", mailbox, "--query", str(query), "--max", "20", "--format", "json"]
        )
        messages = parsed.get("messages") or (parsed.get("data") or {}).get("messages") or []
        for m in messages:
            mid = (m or {}).get("message_id")
            if mid:
                return str(mid)
    return ""


def _ensure_re_subject(subject: str) -> str:
    """Prefix the subject with 'Re: ' unless it already carries one."""
    subject = (subject or "").strip()
    if not subject:
        return "Re:"
    if re.match(r"^\s*re\s*:", subject, re.IGNORECASE):
        return subject
    return f"Re: {subject}"


def _ensure_ref_preserved(body_html: str, original_body: str, ref_id: str = "") -> str:
    """Guarantee the claim's ref:...:ref tracking code survives into the reply.

    If the reply body already contains a ref code we leave it untouched.
    Otherwise we append the ref code taken from ``ref_id`` (if provided) or
    extracted from the original inbound email body.
    """
    body_html = body_html or ""
    if REF_CODE_RE.search(body_html):
        return body_html
    ref = (ref_id or "").strip()
    if not REF_CODE_RE.fullmatch(ref):
        match = REF_CODE_RE.search(original_body or "")
        ref = match.group(0) if match else ""
    if not ref:
        return body_html
    return f"{body_html}\n<p style=\"color:#888888\">{ref}</p>"


def _build_raw_reply_eml(
    mailbox: str,
    recipients: List[Dict[str, str]],
    subject: str,
    body_html: str,
    in_reply_to: str = "",
    references_list: Optional[List[str]] = None,
    cc_recipients: Optional[List[Dict[str, str]]] = None,
) -> str:
    message = EmailMessage()
    message["From"] = mailbox
    message["To"] = ", ".join(
        formataddr((str(item.get("name") or ""), str(item.get("mail_address") or "")))
        for item in recipients
    )
    if cc_recipients:
        message["Cc"] = ", ".join(
            formataddr((str(item.get("name") or ""), str(item.get("mail_address") or "")))
            for item in cc_recipients
        )
    message["Subject"] = subject
    # In-Reply-To / References MUST be the RFC 2822 Message-ID (smtp_message_id)
    # of the original inbound email — NOT Lark's internal message_id.
    reference = _message_reference(in_reply_to)
    if reference:
        message["In-Reply-To"] = reference
        refs = [_message_reference(r) for r in (references_list or []) if r]
        refs = [r for r in refs if r]
        if reference not in refs:
            refs.append(reference)
        message["References"] = " ".join(refs)
    message.set_content(_html_to_plain_text(body_html), subtype="plain", charset="utf-8")
    message.add_alternative(body_html, subtype="html", charset="utf-8")
    return base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8").rstrip("=")


def _extract_result(data: Dict[str, Any]) -> Dict[str, Any]:
    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    draft_obj = payload.get("draft") if isinstance(payload.get("draft"), dict) else {}
    draft_id = (
        payload.get("draft_id")
        or payload.get("message_id")
        or payload.get("id")
        or draft_obj.get("draft_id")
        or draft_obj.get("id")
    )
    link = (
        payload.get("draft_link")
        or payload.get("send_preview_url")
        or payload.get("url")
        or payload.get("link")
        or payload.get("web_url")
        or draft_obj.get("draft_link")
        or draft_obj.get("send_preview_url")
        or draft_obj.get("url")
        or draft_obj.get("link")
        or draft_obj.get("web_url")
    )
    return {"draft_id": draft_id or "", "draft_link": link or "", "raw": data}


def _create_draft(mailbox: str, payload: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
    encoded_mailbox = urllib.parse.quote(mailbox, safe="")
    drafts_url = f"{MAIL_API_BASE}/user_mailboxes/{encoded_mailbox}/drafts"
    response = _post_json(drafts_url, payload, headers=headers)
    result = _extract_result(response)
    if not result["draft_id"] and not result["draft_link"]:
        raise LarkMailDraftError(
            f"Draft created but response did not include a draft ID/link: {json.dumps(response, ensure_ascii=False)}"
        )
    if result["draft_id"] and not result["draft_link"]:
        result["draft_link"] = (
            "https://www.larkoffice.com/mail?draftId="
            f"{result['draft_id']}&scene=send-preview&mailbox={encoded_mailbox}"
        )
    return result


def _build_raw_new_eml(
    mailbox: str,
    recipients: List[Dict[str, str]],
    subject: str,
    body_html: str,
    cc_recipients: Optional[List[Dict[str, str]]] = None,
) -> str:
    message = EmailMessage()
    message["From"] = mailbox
    message["To"] = ", ".join(
        formataddr((str(item.get("name") or ""), str(item.get("mail_address") or "")))
        for item in recipients
    )
    if cc_recipients:
        message["Cc"] = ", ".join(
            formataddr((str(item.get("name") or ""), str(item.get("mail_address") or "")))
            for item in cc_recipients
        )
    message["Subject"] = subject
    message.set_content(_html_to_plain_text(body_html), subtype="plain", charset="utf-8")
    message.add_alternative(body_html, subtype="html", charset="utf-8")
    return base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8").rstrip("=")


def create_reply_draft(
    mailbox: str,
    thread_message_id: str,
    to: Union[str, Dict[str, Any], Iterable[Union[str, Dict[str, Any]]]],
    subject: str,
    body_html: str,
    cc: Union[str, Dict[str, Any], Iterable[Union[str, Dict[str, Any]]], None] = None,
    upc: str = "",
    ref_id: str = "",
    source_is_spotify: bool = True,
) -> Dict[str, Any]:
    """Create a Lark Mail reply draft threaded onto the original claim email.

    The draft is built as a genuine REPLY to the original inbound email:
      * We fetch that email (by its Lark message_id, or by searching the inbox
        for the UPC / ref code) to read its RFC Message-ID, thread_id, sender
        and subject.
      * ``In-Reply-To`` / ``References`` are set to the original email's
        ``smtp_message_id`` (the RFC 2822 Message-ID), so the claimant's mail
        client threads our reply under the original message.
      * ``To`` defaults to the ORIGINAL SENDER (the Spotify/claimant address),
        not a freshly composed address.
      * The subject is prefixed with ``Re: ``.
      * The ``ref:...:ref`` tracking code from the original body is preserved.

    Args:
        mailbox: User mailbox ID or address (e.g. "soundon-copyright@bytedance.com").
        thread_message_id: Lark message_id of the original inbound email. May be
            empty, in which case we locate it via ``upc`` / ``ref_id``.
        to: Fallback recipient(s) if the original sender cannot be resolved.
        subject: Fallback subject if the original subject cannot be resolved.
        body_html: HTML body for the reply.
        cc: Optional extra CC recipient(s).
        upc / ref_id: Used to locate the original email when ``thread_message_id``
            is missing, and to preserve the ref tracking code.
        source_is_spotify: When False (rare, non-Spotify source) we still reply to
            the original sender's email.

    Returns:
        A dict with draft_id, draft_link, and raw API response.

    Raises:
        LarkMailDraftError if the original thread cannot be located or draft
        creation fails.
    """
    mailbox = (mailbox or "").strip()
    message_id = (thread_message_id or "").strip()
    subject = (subject or "").strip()
    body_html = (body_html or "").strip()
    upc = str(upc or "").strip()
    ref_id = str(ref_id or "").strip()
    if not mailbox:
        raise LarkMailDraftError("mailbox is required.")
    if not body_html:
        raise LarkMailDraftError("body_html is required.")

    token = refresh_user_access_token()
    headers = {"Authorization": f"Bearer {token}"}

    # ── Locate the original inbound email so the draft threads as a reply ─────
    # Prefer mailbox search by the claim's unique ref code / UPC. Stored IDs in
    # older tracker rows can be Lark IM card IDs, and callback environments can
    # lack enough Mail read scope to fetch a stale/ambiguous ID directly.
    lookup_message_id = _find_original_message_id(mailbox, upc=upc, ref_id=ref_id) if (upc or ref_id) else ""
    if lookup_message_id:
        if message_id and message_id != lookup_message_id:
            print(
                "  ℹ Using mailbox-search mail message_id "
                f"{lookup_message_id!r} instead of stored id {message_id!r} ",
                "for threaded reply lookup.",
                flush=True,
            )
        message_id = lookup_message_id
    elif _looks_like_im_message_id(message_id):
        message_id = ""

    original: Dict[str, Any] = {}
    if message_id:
        original = _fetch_original_message(mailbox, message_id, headers=headers)
        if not _has_threading_metadata(original):
            fallback_message_id = _find_original_message_id(mailbox, upc=upc, ref_id=ref_id) if (upc or ref_id) else ""
            if fallback_message_id and fallback_message_id != message_id:
                message_id = fallback_message_id
                original = _fetch_original_message(mailbox, message_id, headers=headers)

    smtp_message_id = str(original.get("smtp_message_id") or "").strip()
    thread_id = str(original.get("thread_id") or "").strip()
    original_refs = original.get("references")
    if isinstance(original_refs, str):
        original_refs = [original_refs]
    elif not isinstance(original_refs, list):
        original_refs = []
    sender = (original.get("head_from") or {})
    sender_email = str(sender.get("mail_address") or "").strip()
    sender_name = str(sender.get("name") or "").strip()
    original_subject = str(original.get("subject") or "").strip()
    original_body = str(original.get("body_plain_text") or "")

    # ── Recipients: reply to the ORIGINAL SENDER (claimant), not a new address ─
    if sender_email and _is_valid_email(sender_email):
        recipients: List[Dict[str, str]] = [{"mail_address": sender_email}]
        if sender_name:
            recipients[0]["name"] = sender_name
    else:
        # Fall back to the caller-provided address only when we truly can't
        # read the original sender.
        recipients = _recipient_list(to)

    cc_recipients: List[Dict[str, str]] = []
    if cc:
        try:
            cc_recipients = _recipient_list(cc)
        except LarkMailDraftError:
            cc_recipients = []

    # ── Subject: Re: <original subject> ───────────────────────────────────────
    reply_subject = _ensure_re_subject(original_subject or subject)

    # ── Preserve the ref:...:ref tracking code from the original body ─────────
    body_html = _ensure_ref_preserved(body_html, original_body, ref_id=ref_id)

    if smtp_message_id:
        drafts_payload: Dict[str, Any] = {
            "raw": _build_raw_reply_eml(
                mailbox,
                recipients,
                reply_subject,
                body_html,
                in_reply_to=smtp_message_id,
                references_list=original_refs,
                cc_recipients=cc_recipients or None,
            ),
            # Threading anchors: message_id ties the draft to the inbound email;
            # thread_id keeps it in the same conversation.
            "message_id": message_id,
        }
        if thread_id:
            drafts_payload["thread_id"] = thread_id
        result = _create_draft(mailbox, drafts_payload, headers)
        result["threaded"] = True
        return result

    fallback_reason = (
        "original message metadata was unavailable"
        if not original
        else "original message was missing smtp_message_id"
    )
    print(
        "  ⚠ Falling back to standalone draft because "
        f"{fallback_reason} (message_id={message_id!r}, upc={upc!r}, ref_id={ref_id!r}).",
        flush=True,
    )
    result = _create_draft(
        mailbox,
        {
            "raw": _build_raw_new_eml(
                mailbox,
                recipients,
                reply_subject,
                body_html,
                cc_recipients=cc_recipients or None,
            )
        },
        headers,
    )
    result["threaded"] = False
    result["warning"] = fallback_reason
    return result


__all__ = ["LarkMailDraftError", "create_reply_draft", "refresh_user_access_token"]
