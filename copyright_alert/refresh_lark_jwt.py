#!/usr/bin/env python3
"""Refresh the cached AIME JWT used by the copyright bot.

This script snapshots the live AIME JWT from the current process environment into
`copyright_alert/aime_env_refresh.json` so follow-up processes can reload it even
if their inherited environment is stale.

Exit codes:
    0  refresh succeeded
    1  refresh failed
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copyright_alert import lark_auth  # noqa: E402

JWT_KEYS = ("AIME_USER_CLOUD_JWT", "USER_CLOUD_JWT", "IRIS_USER_CLOUD_JWT")
SNAPSHOT_FILE = ROOT / "copyright_alert" / "aime_env_refresh.json"
LOG_FILE = ROOT / "copyright_alert" / "logs" / "jwt_refresh.log"
_MIN_VALIDITY_SECONDS = 60


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _load_snapshot(path: Path = SNAPSHOT_FILE) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _decode_jwt_claims(value: str) -> dict:
    if not (isinstance(value, str) and value.count(".") >= 2):
        return {}
    try:
        import base64

        payload = value.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        obj = json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _jwt_diag_line(token: str, now_ts: int | None = None) -> str:
    claims = _decode_jwt_claims(token)
    iat = int(claims.get("iat") or 0)
    exp = int(claims.get("exp") or 0)
    now_ts = int(now_ts if now_ts is not None else _utc_now().timestamp())
    if not exp:
        return "jwt_diag=unavailable"
    lifetime_min = (exp - iat) // 60 if iat else 0
    remaining_min = (exp - now_ts) // 60
    iat_iso = datetime.fromtimestamp(iat, tz=timezone.utc).isoformat() if iat else "n/a"
    exp_iso = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat()
    return (
        f"iat={iat_iso} exp={exp_iso} lifetime_min={lifetime_min} "
        f"remaining_min={remaining_min}"
    )


def _valid_live_tokens(env: Dict[str, str] | None = None, now_ts: int | None = None) -> Dict[str, Tuple[str, int]]:
    env = env or os.environ
    now_ts = int(now_ts if now_ts is not None else _utc_now().timestamp())
    tokens: Dict[str, Tuple[str, int]] = {}
    for key in JWT_KEYS:
        value = str(env.get(key, "") or "").strip()
        if not value:
            continue
        exp = int(lark_auth._jwt_expiry(value) or 0)
        if exp <= now_ts + _MIN_VALIDITY_SECONDS:
            continue
        tokens[key] = (value, exp)
    return tokens


def _env_state(env: Dict[str, str], now_ts: int) -> str:
    """Per-key description of the JWT env vars for the failure message. Never the token."""
    parts = []
    for key in JWT_KEYS:
        value = str(env.get(key, "") or "").strip()
        if not value:
            parts.append(f"{key}=not set")
            continue
        claims = _decode_jwt_claims(value)
        exp = int(claims.get("exp") or 0)
        iat = int(claims.get("iat") or 0)
        if not exp:
            parts.append(f"{key}=unreadable")
            continue
        iat_iso = datetime.fromtimestamp(iat, tz=timezone.utc).strftime("%H:%M:%SZ") if iat else "n/a"
        exp_iso = datetime.fromtimestamp(exp, tz=timezone.utc).strftime("%H:%M:%SZ")
        left_min = (exp - now_ts) // 60
        state = f"expired {-left_min} min ago" if left_min < 0 else f"{left_min} min left"
        parts.append(f"{key}={state} (iat {iat_iso}, exp {exp_iso})")
    return "; ".join(parts)


def _snapshot_best_exp(snapshot: dict) -> int:
    keys = snapshot.get("keys") if isinstance(snapshot.get("keys"), dict) else {}
    return max([int(lark_auth._jwt_expiry(v) or 0) for v in keys.values() if isinstance(v, str)], default=0)


def refresh_snapshot(
    env: Dict[str, str] | None = None,
    snapshot_file: Path = SNAPSHOT_FILE,
    log_file: Path = LOG_FILE,
) -> dict:
    env = env or os.environ
    now = _utc_now()
    now_ts = int(now.timestamp())
    snapshot = _load_snapshot(snapshot_file)
    live_tokens = _valid_live_tokens(env=env, now_ts=now_ts)
    if not live_tokens:
        raise RuntimeError("No non-expired live AIME JWT found in environment: " + _env_state(env, now_ts))

    best_key, (best_token, best_exp) = max(live_tokens.items(), key=lambda item: item[1][1])
    start_diag = _jwt_diag_line(best_token, now_ts=now_ts)

    # Never replace the snapshot with a token that expires EARLIER than the one it holds
    # (e.g. a process that still carries an older env token must not undo a fresher snapshot).
    existing_best_exp = _snapshot_best_exp(snapshot)
    if existing_best_exp > best_exp:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as fh:
            fh.write(f"{now.isoformat()} refresh_skip snapshot_already_newer existing_exp={existing_best_exp} "
                     f"env_exp={best_exp} {start_diag}\n")
        return {
            "updated_at": int(snapshot.get("updated_at") or 0),
            "source_key": "snapshot",
            "expires_at": existing_best_exp,
            "expires_at_iso": datetime.fromtimestamp(existing_best_exp, tz=timezone.utc).isoformat(),
            "snapshot_file": str(snapshot_file),
            "log_file": str(log_file),
            "kept_existing": True,
        }

    existing_keys = snapshot.get("keys") if isinstance(snapshot.get("keys"), dict) else {}
    new_keys = dict(existing_keys)
    for key in JWT_KEYS:
        new_keys[key] = live_tokens.get(key, (best_token, best_exp))[0]

    new_snapshot = {"keys": new_keys, "updated_at": now_ts}
    snapshot_file.parent.mkdir(parents=True, exist_ok=True)
    snapshot_file.write_text(json.dumps(new_snapshot, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    expiry_iso = datetime.fromtimestamp(best_exp, tz=timezone.utc).isoformat()
    end_diag = _jwt_diag_line(new_keys.get(best_key, best_token), now_ts=now_ts)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(f"{now.isoformat()} refresh_start source_key={best_key} {start_diag}\n")
        fh.write(
            f"{now.isoformat()} refresh_ok source_key={best_key} expires_at={best_exp} "
            f"expires_at_iso={expiry_iso} {end_diag}\n"
        )

    return {
        "updated_at": now_ts,
        "source_key": best_key,
        "expires_at": best_exp,
        "expires_at_iso": expiry_iso,
        "snapshot_file": str(snapshot_file),
        "log_file": str(log_file),
    }


def main() -> int:
    try:
        result = refresh_snapshot()
    except Exception as exc:
        print(f"JWT refresh failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
