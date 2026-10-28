"""send_stale_token_alert must reach ops without the AIME JWT and without the
feishu-im-send helper script (absent in the AIME sandbox)."""
import subprocess

import pytest

from copyright_alert import lark_auth


@pytest.fixture(autouse=True)
def _isolated_alert_state(monkeypatch, tmp_path):
    state = {}
    monkeypatch.setattr(lark_auth, "_load_alert_state", lambda: dict(state))
    monkeypatch.setattr(lark_auth, "_save_alert_state", lambda s: state.update(s))
    monkeypatch.setattr(lark_auth, "_AUTH_ALERT_FINGERPRINTS", set())
    monkeypatch.setattr(lark_auth, "_FEISHU_IM_DIR", tmp_path / "missing-helper")  # helper absent
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no helper script run")))
    return state


def test_alert_goes_through_the_bot_when_the_helper_script_is_missing(monkeypatch, _isolated_alert_state):
    sent = []
    monkeypatch.setattr(lark_auth, "_send_alert_via_bot",
                        lambda rid, typ, msg: sent.append((typ, rid)) or True)
    assert lark_auth.send_stale_token_alert("ctx", "detail") is True
    assert sent == [("email", lark_auth._ALERT_EMAIL)]      # first success wins
    assert "ctx" in _isolated_alert_state                    # throttle recorded


def test_falls_back_to_the_group_chat_when_the_dm_fails(monkeypatch):
    sent = []

    def fake(rid, typ, msg):
        sent.append(typ)
        return typ == "chat_id"
    monkeypatch.setattr(lark_auth, "_send_alert_via_bot", fake)
    assert lark_auth.send_stale_token_alert("ctx", "detail") is True
    assert sent == ["email", "chat_id"]


def test_returns_false_and_does_not_record_when_nothing_could_be_sent(monkeypatch, _isolated_alert_state):
    monkeypatch.setattr(lark_auth, "_send_alert_via_bot", lambda *a: False)
    assert lark_auth.send_stale_token_alert("ctx", "detail") is False
    assert "ctx" not in _isolated_alert_state               # a later attempt may retry


def test_bot_send_posts_a_post_message_with_the_bot_token(monkeypatch):
    import io
    import json
    import urllib.request
    from copyright_alert import run_alert as ra

    monkeypatch.setattr(ra, "_get_bot_access_token", lambda: "tenant-token")
    seen = {}

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=0):
        seen["url"] = req.full_url
        seen["auth"] = req.headers.get("Authorization")
        seen["body"] = json.loads(req.data.decode())
        return Resp(b'{"code": 0}')
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert lark_auth._send_alert_via_bot("oc_x", "chat_id", '{"zh_cn": {}}') is True
    assert seen["url"].endswith("/im/v1/messages?receive_id_type=chat_id")
    assert seen["auth"] == "Bearer tenant-token"
    assert seen["body"]["receive_id"] == "oc_x" and seen["body"]["msg_type"] == "post"


def test_bot_send_never_raises(monkeypatch):
    from copyright_alert import run_alert as ra
    monkeypatch.setattr(ra, "_get_bot_access_token", lambda: (_ for _ in ()).throw(RuntimeError("no secret")))
    assert lark_auth._send_alert_via_bot("oc_x", "chat_id", "{}") is False
