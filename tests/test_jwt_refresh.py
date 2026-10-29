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

    # Two lines now: refresh_start (env JWT diag) then refresh_ok (snapshot JWT diag).
    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert "refresh_start" in lines[0] and "lifetime_min=" in lines[0]
    log_line = lines[-1]
    assert "refresh_ok" in log_line
    assert f"expires_at={future_exp}" in log_line
    datetime.fromisoformat(log_line.split(" refresh_ok", 1)[0])
    assert live_token not in "\n".join(lines)          # diagnostics must never log the token itself


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


def test_failure_message_says_which_key_is_expired_or_missing_without_leaking_tokens(tmp_path):
    expired = _make_jwt(1)
    with pytest.raises(RuntimeError) as exc:
        refresh_lark_jwt.refresh_snapshot(
            env={"AIME_USER_CLOUD_JWT": expired},
            snapshot_file=tmp_path / "s.json", log_file=tmp_path / "l.log",
        )
    msg = str(exc.value)
    assert "AIME_USER_CLOUD_JWT=expired" in msg and "USER_CLOUD_JWT=not set" in msg
    assert expired not in msg


def test_snapshot_is_never_replaced_by_an_older_token(tmp_path):
    newer = _make_jwt(1893456000 + 7200)
    older = _make_jwt(1893456000)
    snapshot_file = tmp_path / "s.json"
    snapshot_file.write_text(json.dumps({"keys": {"AIME_USER_CLOUD_JWT": newer}, "updated_at": 5}), encoding="utf-8")
    result = refresh_lark_jwt.refresh_snapshot(env={"AIME_USER_CLOUD_JWT": older},
                                               snapshot_file=snapshot_file, log_file=tmp_path / "l.log")
    assert result.get("kept_existing") is True
    assert json.loads(snapshot_file.read_text())["keys"]["AIME_USER_CLOUD_JWT"] == newer
    assert "refresh_skip" in (tmp_path / "l.log").read_text()


def test_daily_workflow_publishes_its_token_at_start(monkeypatch, tmp_path):
    from copyright_alert import daily_workflow as dw
    token = _make_jwt(1893456000)
    monkeypatch.setenv("AIME_USER_CLOUD_JWT", token)
    dw._publish_jwt_snapshot()
    saved = json.loads(refresh_lark_jwt.SNAPSHOT_FILE.read_text())
    assert saved["keys"]["AIME_USER_CLOUD_JWT"] == token


def test_publish_is_silent_when_there_is_no_live_token(monkeypatch):
    from copyright_alert import daily_workflow as dw
    for k in refresh_lark_jwt.JWT_KEYS:
        monkeypatch.delenv(k, raising=False)
    dw._publish_jwt_snapshot()   # must not raise
