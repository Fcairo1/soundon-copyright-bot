import base64
import json
import os
import time

import pytest

from copyright_alert import lark_auth

KEYS = ("AIME_USER_CLOUD_JWT", "USER_CLOUD_JWT", "IRIS_USER_CLOUD_JWT")


def _jwt(exp):
    b64 = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{b64({'alg': 'none'})}.{b64({'exp': int(exp)})}.sig"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(lark_auth, "_ENV_REFRESH_FILE", tmp_path / "aime_env_refresh.json")
    monkeypatch.setattr(lark_auth, "_jwt_preflight_last_attempt", 0.0)
    alerts = []
    monkeypatch.setattr(lark_auth, "send_stale_token_alert", lambda ctx, detail: alerts.append((ctx, detail)) or True)
    yield alerts
    for k in KEYS:                      # _refresh_aime_credentials writes os.environ directly
        os.environ.pop(k, None)


def _snapshot(path, token):
    path.write_text(json.dumps({"keys": {k: token for k in KEYS}}), encoding="utf-8")


def test_noop_when_no_jwt_configured(_isolate):
    assert lark_auth.ensure_jwt_fresh("t") is True and not _isolate


def test_noop_when_token_has_plenty_of_time(monkeypatch, _isolate):
    monkeypatch.setenv("AIME_USER_CLOUD_JWT", _jwt(time.time() + 7200))
    monkeypatch.setattr(lark_auth, "_refresh_aime_credentials", lambda: (_ for _ in ()).throw(AssertionError("no refresh")))
    assert lark_auth.ensure_jwt_fresh("t") is True


def test_reloads_newer_token_from_snapshot_when_near_expiry(monkeypatch, tmp_path, _isolate):
    monkeypatch.setenv("AIME_USER_CLOUD_JWT", _jwt(time.time() + 300))     # 5 min left (< 30 min margin)
    fresh = _jwt(time.time() + 6 * 86400)
    _snapshot(tmp_path / "aime_env_refresh.json", fresh)
    assert lark_auth.ensure_jwt_fresh("t") is True
    assert os.environ["AIME_USER_CLOUD_JWT"] == fresh      # children spawned after this inherit it
    assert not _isolate


def test_reloads_after_token_already_expired_mid_run(monkeypatch, tmp_path, _isolate):
    monkeypatch.setenv("AIME_USER_CLOUD_JWT", _jwt(time.time() - 600))
    _snapshot(tmp_path / "aime_env_refresh.json", _jwt(time.time() + 86400))
    assert lark_auth.ensure_jwt_fresh("t") is True


def test_alerts_and_returns_false_when_no_fresher_snapshot(monkeypatch, tmp_path, _isolate):
    monkeypatch.setenv("AIME_USER_CLOUD_JWT", _jwt(time.time() - 10))
    _snapshot(tmp_path / "aime_env_refresh.json", _jwt(time.time() - 5))      # snapshot is stale too
    assert lark_auth.ensure_jwt_fresh("ctx") is False
    assert _isolate and _isolate[-1][0] == "ctx"


def test_refresh_attempts_are_rate_limited(monkeypatch, tmp_path, _isolate):
    monkeypatch.setenv("AIME_USER_CLOUD_JWT", _jwt(time.time() - 10))
    calls = []
    monkeypatch.setattr(lark_auth, "_refresh_aime_credentials", lambda: calls.append(1) or 0)
    for _ in range(5):                       # e.g. one call per message in a 4h scan
        lark_auth.ensure_jwt_fresh("t")
    assert len(calls) == 1 and len(_isolate) == 1


def test_every_workflow_section_runs_the_preflight(monkeypatch):
    from copyright_alert import daily_workflow
    seen = []
    monkeypatch.setattr(lark_auth, "ensure_jwt_fresh", lambda ctx, *a, **k: seen.append(ctx) or True)
    daily_workflow.section("PART G — Spotify metadata notices (daily re-send)")
    assert seen and seen[0].startswith("daily_workflow PART G")


def test_reloads_fresher_token_from_dot_env_when_snapshot_is_stale(monkeypatch, tmp_path, _isolate):
    # Real incident: snapshot expired days ago, but `.env` held a newer JWT.
    monkeypatch.setenv("AIME_USER_CLOUD_JWT", _jwt(time.time() - 3600))
    _snapshot(tmp_path / "aime_env_refresh.json", _jwt(time.time() - 90000))
    dotenv = tmp_path / ".env"
    fresh = _jwt(time.time() + 6 * 3600)
    dotenv.write_text(f'BOT_SECRET=x\nexport AIME_USER_CLOUD_JWT="{fresh}"\n', encoding="utf-8")
    monkeypatch.setattr(lark_auth, "_ENV_FILE_CANDIDATES", (dotenv,))
    assert lark_auth.ensure_jwt_fresh("t") is True
    assert os.environ["AIME_USER_CLOUD_JWT"] == fresh


def test_dot_env_with_older_token_does_not_downgrade(monkeypatch, tmp_path, _isolate):
    current = _jwt(time.time() + 5 * 3600)
    monkeypatch.setenv("AIME_USER_CLOUD_JWT", current)
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"AIME_USER_CLOUD_JWT={_jwt(time.time() + 1000)}\n", encoding="utf-8")
    monkeypatch.setattr(lark_auth, "_ENV_FILE_CANDIDATES", (dotenv,))
    lark_auth._refresh_aime_credentials()
    assert os.environ["AIME_USER_CLOUD_JWT"] == current
