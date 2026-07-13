#!/usr/bin/env python3
"""Shared Lark auth-refresh helpers for copyright_alert."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
_ENV_REFRESH_FILE = ROOT / "copyright_alert" / "aime_env_refresh.json"
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


def _refresh_aime_credentials() -> int:
    """Re-load only non-expired, newer aime_env_refresh.json credentials."""
    updated = 0
    try:
        if not _ENV_REFRESH_FILE.exists():
            print("⚠ credential refresh: aime_env_refresh.json missing", flush=True)
            return 0
        with _ENV_REFRESH_FILE.open("r", encoding="utf-8") as f:
            snapshot = json.load(f) or {}
        for k, v in snapshot.items():
            if _prefer_candidate_credential(os.environ.get(k, ""), v):
                os.environ[k] = v
                updated += 1
            elif "JWT" in k and _jwt_expiry(v) and _jwt_expiry(v) <= int(__import__("time").time()) + 60:
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
    updated = _refresh_aime_credentials()
    print(f"↻ {context}: auth failure detected; refreshed {updated} key(s) and retrying once. {detail}", flush=True)

    second = _perform_once()
    if second["ok"]:
        return second["payload"] if isinstance(second["payload"], dict) else {}

    if second["auth_error"]:
        retry_detail = _auth_failure_detail(second["status"], second["payload"], second["text"])
        print(f"✗ {context}: auth failure persisted after refresh retry. {retry_detail}", flush=True)
        send_stale_token_alert(context, retry_detail)
        raise RuntimeError(f"Auth failure persisted after one refresh retry: {retry_detail}")

    raise RuntimeError(f"Request failed after refresh retry in {context}: {second['text'][:800]}")
