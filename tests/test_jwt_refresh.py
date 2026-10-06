import base64
import json
from datetime import datetime, timezone

import pytest

from copyright_alert import refresh_lark_jwt


def _make_jwt(exp: int) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode()).decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"{header}.{payload}.signature"


def test_refresh_snapshot_updates_snapshot_and_log(tmp_path):
    snapshot_file = tmp_path / "aime_env_refresh.json"
    log_file = tmp_path / "logs" / "jwt_refresh.log"
    existing = {
        "keys": {
            "AIME_USER_CLOUD_JWT": "old-aime",
            "USER_CLOUD_JWT": "old-user",
            "IRIS_USER_CLOUD_JWT": "old-iris",
        },
        "updated_at": 1,
    }
    snapshot_file.write_text(json.dumps(existing), encoding="utf-8")

    future_exp = 1893456000
    live_token = _make_jwt(future_exp)
    env = {"AIME_USER_CLOUD_JWT": live_token}

    result = refresh_lark_jwt.refresh_snapshot(env=env, snapshot_file=snapshot_file, log_file=log_file)

    saved = json.loads(snapshot_file.read_text(encoding="utf-8"))
    assert result["source_key"] == "AIME_USER_CLOUD_JWT"
    assert result["expires_at"] == future_exp
    assert saved["updated_at"] == result["updated_at"]
    assert saved["keys"]["AIME_USER_CLOUD_JWT"] == live_token
    assert saved["keys"]["USER_CLOUD_JWT"] == live_token
    assert saved["keys"]["IRIS_USER_CLOUD_JWT"] == live_token

    log_line = log_file.read_text(encoding="utf-8").strip()
    assert "refresh_ok" in log_line
    assert f"expires_at={future_exp}" in log_line
    datetime.fromisoformat(log_line.split(" refresh_ok", 1)[0])


def test_refresh_snapshot_fails_without_valid_live_token(tmp_path):
    snapshot_file = tmp_path / "aime_env_refresh.json"
    log_file = tmp_path / "logs" / "jwt_refresh.log"
    expired = _make_jwt(1)

    with pytest.raises(RuntimeError, match="No non-expired live AIME JWT"):
        refresh_lark_jwt.refresh_snapshot(
            env={"AIME_USER_CLOUD_JWT": expired},
            snapshot_file=snapshot_file,
            log_file=log_file,
        )
