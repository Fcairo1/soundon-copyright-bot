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


def refresh_snapshot(
    env: Dict[str, str] | None = None,
    snapshot_file: Path = SNAPSHOT_FILE,
    log_file: Path = LOG_FILE,
) -> dict:
    now = _utc_now()
    snapshot = _load_snapshot(snapshot_file)
    live_tokens = _valid_live_tokens(env=env, now_ts=int(now.timestamp()))
    if not live_tokens:
        raise RuntimeError("No non-expired live AIME JWT found in environment")

    best_key, (best_token, best_exp) = max(live_tokens.items(), key=lambda item: item[1][1])

    existing_keys = snapshot.get("keys") if isinstance(snapshot.get("keys"), dict) else {}
    new_keys = dict(existing_keys)
    for key in JWT_KEYS:
        new_keys[key] = live_tokens.get(key, (best_token, best_exp))[0]

    new_snapshot = {"keys": new_keys, "updated_at": int(now.timestamp())}
    snapshot_file.parent.mkdir(parents=True, exist_ok=True)
    snapshot_file.write_text(json.dumps(new_snapshot, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    expiry_iso = datetime.fromtimestamp(best_exp, tz=timezone.utc).isoformat()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(
            f"{now.isoformat()} refresh_ok source_key={best_key} expires_at={best_exp} expires_at_iso={expiry_iso}\n"
        )

    return {
        "updated_at": int(now.timestamp()),
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
