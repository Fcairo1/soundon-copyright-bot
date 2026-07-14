#!/usr/bin/env python3
"""Shared Lark auth-refresh helpers for copyright_alert."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
_APP_ID_EXPECTED = "cli_aa94690b12b81cde"
_ENV_REFRESH_FILE = ROOT / "copyright_alert" / "aime_env_refresh.json"
_LEGACY_OAUTH_FILE = ROOT / "copyright_alert" / "lark_mail_oauth.json"
_OAUTH_SECRET_FILE = ROOT / "runtime" / "lark_oauth_secret.json"
_REFRESH_URL = "https://open.larksuite.com/open-apis/authen/v1/refresh_access_token"
_OAUTH_AUTHORIZE_URL = "https://accounts.larksuite.com/open-apis/authen/v1/authorize"
_OAUTH_REDIRECT_URI = "http://localhost:9876/oauth/callback"
# OAuth scopes must match the published/live Lark app version exactly.
# The SoundOn app currently has these granular Sheets scopes published:
# sheets:spreadsheet:read for values GET, and sheets:spreadsheet:write_only
# for values PUT updates.
_OAUTH_REQUIRED_SCOPE_KEYS = (
    "sheets:spreadsheet:read",
    "sheets:spreadsheet:write_only",
    "mail:user_mailbox.message:modify",
)
_OAUTH_REQUIRED_SCOPES = " ".join(_OAUTH_REQUIRED_SCOPE_KEYS)
_TOKEN_REFRESH_SKEW_SECONDS = 120
_OAUTH_LOCK = threading.Lock()
_FEISHU_IM_DIR = ROOT / "inner_skills" / "feishu-im-send"
_ALERT_EMAIL = "filipe.cairo@bytedance.com"
_ALERT_CHAT_ID = "oc_6e157309d8d7145ba5ce7f0ba67354cb"
_AUTH_ERROR_CODES = {99991663, 99991664}
_AUTH_ALERT_LOCK = threading.Lock()
_AUTH_ALERT_FINGERPRINTS: set[str] = set()

# I4: persistent, cross-process throttle for operator alerts. The proactive JWT
# healthcheck runs as a FRESH process on every cron tick, so the in-memory
# _AUTH_ALERT_FINGERPRINTS set can never dedupe across runs — and because the
# detail string embeds a changing "expires in Xs", the fingerprint changes every
# run anyway. That means a stale token spams the operator with one DM per cron
# run (every 2h, or every 10 min if scheduled that way). We persist the last
# time we alerted per-context to disk and re-alert at most once per interval.
_ALERT_STATE_FILE = ROOT / "runtime" / "auth_alert_last_sent.json"
_ALERT_MIN_INTERVAL_SEC = 6 * 3600  # re-alert at most once per 6 hours per context


def _load_alert_state() -> dict:
    try:
        if _ALERT_STATE_FILE.exists():
            data = json.loads(_ALERT_STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _save_alert_state(state: dict) -> None:
    try:
        _ALERT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _ALERT_STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        print(f"⚠ could not persist auth alert state: {exc!r}", flush=True)


def _safe_json_loads(raw: str):
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        idx = text.find("{")
        if idx >= 0:
            try:
                return json.loads(text[idx:])
            except Exception:
                return None
    return None


def _response_code(payload) -> Optional[int]:
    if not isinstance(payload, dict):
        return None
    code = payload.get("code")
    if isinstance(code, int):
        return code
    if isinstance(code, str) and code.isdigit():
        try:
            return int(code)
        except Exception:
            return None
    return None


def _looks_like_auth_failure(http_status: Optional[int] = None, payload=None, raw_text: str = "") -> bool:
    code = _response_code(payload)
    if http_status == 401:
        return True
    if code in _AUTH_ERROR_CODES:
        return True
    if isinstance(code, int) and code == 0:
        return False
    text_parts = []
    if raw_text:
        text_parts.append(str(raw_text))
    if isinstance(payload, dict):
        error_obj = payload.get("error")
        if error_obj:
            text_parts.append(json.dumps(error_obj, ensure_ascii=False))
        msg = payload.get("msg") or payload.get("message")
        if msg:
            text_parts.append(str(msg))
    blob = "\n".join(text_parts).lower()
    return any(marker in blob for marker in ("99991663", "99991664", "401 unauthorized", "unauthorized", "invalid access token", "token expired", "jwt expired"))


def _auth_failure_detail(http_status: Optional[int] = None, payload=None, raw_text: str = "") -> str:
    if isinstance(payload, dict):
        return json.dumps(
            {
                "http_status": http_status,
                "code": payload.get("code"),
                "msg": payload.get("msg"),
                "error": payload.get("error"),
            },
            ensure_ascii=False,
        )
    text = (raw_text or "").strip()
    if len(text) > 400:
        text = text[:400] + "…"
    return json.dumps({"http_status": http_status, "detail": text}, ensure_ascii=False)


def _jwt_expiry(value: str) -> int:
    if not (isinstance(value, str) and value.count(".") >= 2):
        return 0
    try:
        import base64
        payload = value.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        obj = json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")))
        exp = obj.get("exp")
        return int(exp) if exp else 0
    except Exception:
        return 0


def _prefer_candidate_credential(current: str, candidate: str) -> bool:
    if not isinstance(candidate, str) or not candidate:
        return False
    if "JWT" not in candidate and not (candidate.count(".") >= 2):
        return bool(candidate != current)
    candidate_exp = _jwt_expiry(candidate)
    current_exp = _jwt_expiry(current or "")
    now = int(__import__("time").time())
    if candidate_exp and candidate_exp <= now + 60:
        return False
    if current_exp and candidate_exp and candidate_exp <= current_exp:
        return False
    return candidate != current


def _int_or_none(value: Any) -> Optional[int]:
    if value in (None, "", False):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _load_app_credentials() -> Tuple[str, str]:
    app_id = os.getenv("LARK_APP_ID") or os.getenv("BOT_APP_ID") or os.getenv("APP_ID") or _APP_ID_EXPECTED
    app_secret = ""
    for key in ("BOT_SECRET", "LARK_APP_SECRET", "APP_SECRET", "app_secret"):
        value = os.getenv(key, "").strip()
        if value:
            app_secret = value
            break
    if not app_secret:
        for candidate in (ROOT / "copyright_alert" / ".env", ROOT / "copyright_alert" / "secrets.json"):
            if not candidate.exists():
                continue
            try:
                if candidate.suffix == ".json":
                    payload = json.loads(candidate.read_text(encoding="utf-8"))
                    if isinstance(payload, dict):
                        app_id = str(payload.get("LARK_APP_ID") or payload.get("BOT_APP_ID") or payload.get("APP_ID") or app_id).strip()
                        for key in ("BOT_SECRET", "LARK_APP_SECRET", "APP_SECRET", "app_secret"):
                            value = str(payload.get(key, "")).strip()
                            if value:
                                app_secret = value
                                break
                else:
                    for raw_line in candidate.read_text(encoding="utf-8").splitlines():
                        line = raw_line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        key, value = line.split("=", 1)
                        key = key.strip()
                        value = value.strip().strip('"').strip("'")
                        if key in {"LARK_APP_ID", "BOT_APP_ID", "APP_ID"} and value:
                            app_id = value
                        if key in {"BOT_SECRET", "LARK_APP_SECRET", "APP_SECRET", "app_secret"} and value:
                            app_secret = value
                if app_secret:
                    break
            except Exception:
                continue
    if app_id != _APP_ID_EXPECTED:
        raise RuntimeError(f"Loaded app_id {app_id!r}, expected {_APP_ID_EXPECTED!r}")
    if not app_secret:
        raise RuntimeError("Lark app secret is missing; set BOT_SECRET/LARK_APP_SECRET or copyright_alert/secrets.json")
    return app_id, app_secret


def _load_oauth_record() -> dict:
    if not _OAUTH_SECRET_FILE.exists() and _LEGACY_OAUTH_FILE.exists():
        _OAUTH_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_LEGACY_OAUTH_FILE, _OAUTH_SECRET_FILE)
        print(f"↻ migrated legacy OAuth token file to {_OAUTH_SECRET_FILE}", flush=True)
    if not _OAUTH_SECRET_FILE.exists():
        raise RuntimeError(_oauth_setup_instructions())
    try:
        record = json.loads(_OAUTH_SECRET_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Could not read OAuth secret file {_OAUTH_SECRET_FILE}: {exc!r}") from exc
    if not isinstance(record, dict) or not record.get("refresh_token"):
        raise RuntimeError(f"OAuth secret file is missing refresh_token: {_OAUTH_SECRET_FILE}")
    return record


def _save_oauth_record(record: dict) -> None:
    _OAUTH_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    _OAUTH_SECRET_FILE.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")


def _oauth_setup_instructions() -> str:
    params = {
        "app_id": _APP_ID_EXPECTED,
        "redirect_uri": _OAUTH_REDIRECT_URI,
        "scope": _OAUTH_REQUIRED_SCOPES,
        "state": "COPYRIGHT_BOT_SETUP",
    }
    auth_url = _OAUTH_AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)
    return (
        "No Lark OAuth refresh_token is available for the copyright bot. "
        f"Authorize Filipe Cairo with this URL: {auth_url}. "
        "After approval, exchange the returned code with copyright_alert/oauth_setup.py "
        f"and store the resulting refresh_token in {_OAUTH_SECRET_FILE}."
    )


def _oauth_token_expired(record: dict) -> bool:
    token = record.get("user_access_token") or record.get("access_token")
    expires_at = _int_or_none(record.get("user_access_token_expires_at") or record.get("access_token_expires_at"))
    return not token or expires_at is None or expires_at <= int(time.time()) + _TOKEN_REFRESH_SKEW_SECONDS


def _oauth_refresh_token_expired(record: dict) -> bool:
    expires_at = _int_or_none(record.get("refresh_token_expires_at"))
    return expires_at is not None and expires_at <= int(time.time()) + _TOKEN_REFRESH_SKEW_SECONDS


def refresh_oauth_user_access_token(force: bool = False) -> str:
    """Return a valid session-independent Lark user_access_token."""
    with _OAUTH_LOCK:
        record = _load_oauth_record()
        if not force and not _oauth_token_expired(record):
            return str(record.get("user_access_token") or record.get("access_token"))
        if _oauth_refresh_token_expired(record):
            raise RuntimeError("Stored Lark OAuth refresh_token is expired. Re-run copyright_alert/oauth_setup.py.")
        app_id, app_secret = _load_app_credentials()
        payload = {
            "grant_type": "refresh_token",
            "app_id": app_id,
            "app_secret": app_secret,
            "refresh_token": record["refresh_token"],
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            _REFRESH_URL,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                response = json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Lark OAuth refresh HTTP {exc.code}: {text[:1000]}") from exc
        if response.get("code") not in (0, "0", None):
            raise RuntimeError(f"Lark OAuth refresh failed: {json.dumps(response, ensure_ascii=False)[:1000]}")
        data = response.get("data") if isinstance(response.get("data"), dict) else response
        user_access_token = data.get("access_token") or data.get("user_access_token")
        if not user_access_token:
            raise RuntimeError(f"Lark OAuth refresh response missing access token: {json.dumps(response, ensure_ascii=False)[:1000]}")
        now = int(time.time())
        expires_in = _int_or_none(data.get("expires_in") or data.get("access_token_expires_in"))
        refresh_expires_in = _int_or_none(data.get("refresh_token_expires_in") or data.get("refresh_expires_in"))
        record.update({
            "app_id": app_id,
            "refresh_token": data.get("refresh_token") or record.get("refresh_token"),
            "refresh_token_expires_in": refresh_expires_in,
            "refresh_token_expires_at": now + refresh_expires_in if refresh_expires_in is not None else record.get("refresh_token_expires_at"),
            "user_access_token": user_access_token,
            "access_token": user_access_token,
            "user_access_token_expires_in": expires_in,
            "access_token_expires_at": now + expires_in if expires_in is not None else None,
            "user_access_token_expires_at": now + expires_in if expires_in is not None else None,
            "token_type": data.get("token_type") or record.get("token_type"),
            "scope": data.get("scope") or record.get("scope"),
            "updated_at": now,
        })
        _save_oauth_record(record)
        return str(user_access_token)


def get_user_access_token(force_refresh: bool = False) -> str:
    return refresh_oauth_user_access_token(force=force_refresh)


def _spreadsheet_token(sheet_url: str) -> str:
    parsed = urllib.parse.urlparse(sheet_url)
    parts = [part for part in parsed.path.split("/") if part]
    for marker in ("sheets", "spreadsheets"):
        if marker in parts:
            idx = parts.index(marker)
            if idx + 1 < len(parts):
                return parts[idx + 1]
    return sheet_url.strip()


def sheet_values_api(method: str, sheet_url: str, sheet_id: str, cell_range: str, values=None, timeout: int = 60) -> dict:
    spreadsheet_token = _spreadsheet_token(sheet_url)
    # Bug 3: the Sheets v2 API rejects a single-cell reference (e.g. "N3") with
    # code 90202 "wrong range" — it must be a full range ("N3:N3"). Callers that
    # write one cell (status / email-status write-backs) pass a bare cell, so
    # normalize it here for both reads and writes.
    if ":" not in cell_range:
        cell_range = f"{cell_range}:{cell_range}"
    a1_range = f"{sheet_id}!{cell_range}"
    base = f"https://open.larksuite.com/open-apis/sheets/v2/spreadsheets/{spreadsheet_token}/values"
    body = None
    if values is not None:
        # Bug 3 (root cause): the single-range WRITE endpoint is `.../values`
        # (PUT) with the range carried inside the body's valueRange — NOT
        # `.../values/{range}`. Reusing the read-style path for writes returned
        # HTTP 404, so EVERY status write-back (group-card buttons and the new
        # DM-card write-back alike) silently failed and column N stayed blank.
        body = json.dumps({"valueRange": {"range": a1_range, "values": values}}, ensure_ascii=False).encode("utf-8")
        url = base
    else:
        encoded_range = urllib.parse.quote(a1_range, safe="")
        url = f"{base}/{encoded_range}"

    force_refresh = False

    def make_request():
        token = get_user_access_token(force_refresh=force_refresh)
        return urllib.request.Request(
            url,
            data=body,
            method=method.upper(),
            headers={"Content-Type": "application/json; charset=utf-8", "Authorization": f"Bearer {token}"},
        )

    try:
        payload = request_json_with_auth_retry(make_request, timeout=timeout, context=f"sheet_values_api:{method}:{a1_range}")
    except RuntimeError:
        force_refresh = True
        payload = request_json_with_auth_retry(make_request, timeout=timeout, context=f"sheet_values_api:{method}:{a1_range}:forced")
    if payload.get("code") not in (0, "0", None):
        raise RuntimeError(f"Sheet API {method} {a1_range} failed: {json.dumps(payload, ensure_ascii=False)[:1000]}")
    return payload


def extract_sheet_values(payload: dict) -> list:
    data_obj = payload.get("data") or {}
    value_range = data_obj.get("valueRange") or data_obj.get("value_range") or {}
    if value_range.get("values") is not None:
        return value_range.get("values") or []
    ranges = data_obj.get("ranges") or []
    if ranges and ranges[0].get("cells") is not None:
        return [[(cell or {}).get("value") for cell in row] for row in (ranges[0].get("cells") or [])]
    return []


def _refresh_aime_credentials() -> int:
    """Legacy fallback: re-load non-expired AIME JWT credentials when present."""
    updated = 0
    try:
        if not _ENV_REFRESH_FILE.exists():
            print("⚠ credential refresh: aime_env_refresh.json missing", flush=True)
            return 0
        with _ENV_REFRESH_FILE.open("r", encoding="utf-8") as f:
            snapshot = json.load(f) or {}
        if isinstance(snapshot.get("keys"), dict):
            snapshot = snapshot.get("keys") or {}
        for k, v in snapshot.items():
            if _prefer_candidate_credential(os.environ.get(k, ""), v):
                os.environ[k] = v
                updated += 1
            elif "JWT" in k and _jwt_expiry(v) and _jwt_expiry(v) <= int(time.time()) + 60:
                print(f"⚠ credential refresh skipped expired {k} from aime_env_refresh.json", flush=True)
    except Exception as exc:  # pragma: no cover
        print(f"⚠ credential refresh failed: {exc!r}", flush=True)
    return updated


def send_stale_token_alert(context: str, detail: str) -> bool:
    """Best-effort operator alert when auth still fails after one refresh retry."""
    # I4: persistent, cross-process throttle FIRST. Keyed by context only (not the
    # detail, which embeds a changing "expires in Xs" and would defeat dedupe).
    # A stale token seen on every cron run only alerts once per interval.
    now = time.time()
    with _AUTH_ALERT_LOCK:
        state = _load_alert_state()
        last = state.get(context)
        if isinstance(last, (int, float)) and (now - last) < _ALERT_MIN_INTERVAL_SEC:
            mins_left = int((_ALERT_MIN_INTERVAL_SEC - (now - last)) // 60)
            print(
                f"⚠ auth alert throttled for {context}: last alerted "
                f"{int((now - last) // 60)} min ago, ~{mins_left} min until re-alert",
                flush=True,
            )
            return False

    fingerprint = f"{context}|{detail}"
    with _AUTH_ALERT_LOCK:
        if fingerprint in _AUTH_ALERT_FINGERPRINTS:
            print(f"⚠ auth alert already sent for {context}", flush=True)
            return False
        _AUTH_ALERT_FINGERPRINTS.add(fingerprint)

    title = "⚠️ copyright_alert auth refresh failed"
    lines = [
        f"A Lark auth error still failed after an on-demand credential refresh.",
        f"Context: {context}",
        f"Detail: {detail}",
        "Manual refresh of copyright_alert/aime_env_refresh.json may be required.",
    ]
    payload = {
        "zh_cn": {
            "title": title,
            "content": [[{"tag": "text", "text": line}] for line in lines],
        }
    }
    msg_json = json.dumps(payload, ensure_ascii=False)

    attempts = [
        ["python3", "scripts/im_send.py", "send", _ALERT_EMAIL, "post", msg_json],
        ["python3", "scripts/im_send.py", "send", _ALERT_CHAT_ID, "post", msg_json, "--id-type=chat_id"],
    ]
    for cmd in attempts:
        try:
            res = subprocess.run(
                cmd,
                cwd=str(_FEISHU_IM_DIR),
                capture_output=True,
                text=True,
                timeout=90,
            )
            if res.returncode == 0:
                print(f"⚠ stale-token alert sent via {' '.join(cmd[3:5])}", flush=True)
                # I4: record the successful alert time so we do not re-alert for
                # this context until _ALERT_MIN_INTERVAL_SEC has elapsed. Only
                # recorded on success so a transient send failure still retries.
                with _AUTH_ALERT_LOCK:
                    state = _load_alert_state()
                    state[context] = now
                    _save_alert_state(state)
                return True
            print(f"⚠ stale-token alert failed rc={res.returncode}: {(res.stdout + res.stderr)[:400]}", flush=True)
        except Exception as exc:
            print(f"⚠ stale-token alert exception: {exc!r}", flush=True)
    return False


def request_json_with_auth_retry(
    request_factory: Callable[[], urllib.request.Request],
    *,
    timeout: int = 60,
    context: str,
):
    """Execute a Lark HTTP request, refresh creds on auth failure, then retry once."""

    def _perform_once():
        req = request_factory()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8")
                payload = _safe_json_loads(text)
                status = getattr(resp, "status", 200)
                auth_error = _looks_like_auth_failure(status, payload, text)
                return {
                    "ok": not auth_error,
                    "auth_error": auth_error,
                    "status": status,
                    "payload": payload,
                    "text": text,
                }
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace")
            payload = _safe_json_loads(text)
            auth_error = _looks_like_auth_failure(exc.code, payload, text)
            if auth_error:
                return {
                    "ok": False,
                    "auth_error": True,
                    "status": exc.code,
                    "payload": payload,
                    "text": text,
                }
            raise RuntimeError(f"HTTP {exc.code}: {text[:800]}") from exc

    first = _perform_once()
    if first["ok"]:
        return first["payload"] if isinstance(first["payload"], dict) else {}

    if not first["auth_error"]:
        raise RuntimeError(f"Unexpected request failure in {context}: {first['text'][:800]}")

    detail = _auth_failure_detail(first["status"], first["payload"], first["text"])
    oauth_refreshed = False
    try:
        refresh_oauth_user_access_token(force=True)
        oauth_refreshed = True
    except Exception as exc:
        print(f"⚠ {context}: OAuth refresh unavailable, trying legacy AIME JWT fallback: {exc!r}", flush=True)
    updated = 0 if oauth_refreshed else _refresh_aime_credentials()
    refreshed_detail = "OAuth user_access_token" if oauth_refreshed else f"{updated} legacy AIME key(s)"
    print(f"↻ {context}: auth failure detected; refreshed {refreshed_detail} and retrying once. {detail}", flush=True)

    second = _perform_once()
    if second["ok"]:
        return second["payload"] if isinstance(second["payload"], dict) else {}

    if second["auth_error"]:
        retry_detail = _auth_failure_detail(second["status"], second["payload"], second["text"])
        print(f"✗ {context}: auth failure persisted after refresh retry. {retry_detail}", flush=True)
        send_stale_token_alert(context, retry_detail)
        raise RuntimeError(f"Auth failure persisted after one refresh retry: {retry_detail}")

    raise RuntimeError(f"Request failed after refresh retry in {context}: {second['text'][:800]}")
