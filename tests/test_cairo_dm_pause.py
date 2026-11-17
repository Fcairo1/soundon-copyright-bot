from datetime import datetime
from zoneinfo import ZoneInfo

from copyright_alert import cairo_dm_pause as cdp


class _FrozenDateTime(datetime):
    frozen_now = datetime(2026, 11, 14, 12, 0, tzinfo=ZoneInfo("America/Sao_Paulo"))

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls.frozen_now.replace(tzinfo=None)
        return cls.frozen_now.astimezone(tz)


def _freeze_brt(monkeypatch, year, month, day):
    class Frozen(_FrozenDateTime):
        frozen_now = datetime(year, month, day, 12, 0, tzinfo=ZoneInfo("America/Sao_Paulo"))

    monkeypatch.setattr(cdp, "datetime", Frozen)


def _reset_caches(monkeypatch):
    monkeypatch.setattr(cdp, "_CAIRO_OPEN_ID_CACHE", "")
    monkeypatch.setattr(cdp, "_CAIRO_CHAT_IDS_CACHE", ())


def test_is_cairo_dm_paused_false_before_window(monkeypatch):
    _reset_caches(monkeypatch)
    _freeze_brt(monkeypatch, 2026, 11, 13)
    assert cdp.is_cairo_dm_paused(recipient_email=cdp.CAIRO_EMAIL) is False


def test_is_cairo_dm_paused_true_on_window_start(monkeypatch):
    _reset_caches(monkeypatch)
    _freeze_brt(monkeypatch, 2026, 11, 14)
    assert cdp.is_cairo_dm_paused(recipient_email=cdp.CAIRO_EMAIL) is True


def test_is_cairo_dm_paused_true_on_window_end(monkeypatch):
    _reset_caches(monkeypatch)
    _freeze_brt(monkeypatch, 2026, 11, 30)
    assert cdp.is_cairo_dm_paused(recipient_email=cdp.CAIRO_EMAIL) is True


def test_is_cairo_dm_paused_false_after_window(monkeypatch):
    _reset_caches(monkeypatch)
    _freeze_brt(monkeypatch, 2026, 12, 1)
    assert cdp.is_cairo_dm_paused(recipient_email=cdp.CAIRO_EMAIL) is False


def test_is_cairo_dm_paused_false_for_non_cairo_recipient(monkeypatch):
    _reset_caches(monkeypatch)
    _freeze_brt(monkeypatch, 2026, 11, 14)
    monkeypatch.setattr(cdp, "_resolve_cairo_open_id", lambda: "ou_cairo")
    monkeypatch.setattr(cdp, "_cached_cairo_chat_ids", lambda: ("oc_cairo",))
    assert cdp.is_cairo_dm_paused(recipient_email="someone@example.com") is False
    assert cdp.is_cairo_dm_paused(recipient_open_id="ou_other") is False
    assert cdp.is_cairo_dm_paused(recipient_chat_id="oc_other") is False


def test_resolve_cairo_open_id_does_not_cache_empty_result(monkeypatch):
    _reset_caches(monkeypatch)
    calls = []

    def fake_resolve(_email):
        calls.append(1)
        return "" if len(calls) == 1 else "ou_cairo"

    monkeypatch.setattr("copyright_alert.dm_action_card.resolve_open_id", fake_resolve)

    assert cdp._resolve_cairo_open_id() == ""
    assert cdp._resolve_cairo_open_id() == "ou_cairo"
    assert cdp._resolve_cairo_open_id() == "ou_cairo"
    assert len(calls) == 2


def test_stale_token_alert_paused_without_throttle_state_write(monkeypatch, capsys):
    from copyright_alert import lark_auth

    _freeze_brt(monkeypatch, 2026, 11, 14)
    monkeypatch.setattr(lark_auth, "_ALERT_EMAIL", cdp.CAIRO_EMAIL)
    fingerprints = {"existing-alert"}
    monkeypatch.setattr(lark_auth, "_AUTH_ALERT_FINGERPRINTS", fingerprints)

    def forbidden(*args, **kwargs):
        raise AssertionError("Paused auth alert must not send or touch throttle state")

    monkeypatch.setattr(lark_auth, "_load_alert_state", forbidden)
    monkeypatch.setattr(lark_auth, "_save_alert_state", forbidden)
    monkeypatch.setattr(lark_auth, "_send_alert_via_bot", forbidden)
    monkeypatch.setattr(lark_auth.subprocess, "run", forbidden)

    assert lark_auth.send_stale_token_alert("test-context", "test-detail") is False
    assert fingerprints == {"existing-alert"}
    assert capsys.readouterr().out.strip() == (
        "[CAIRO_DM_PAUSED] skipped DM: stale-token auth alert"
    )


def test_stale_token_alert_resumes_on_december_first(monkeypatch):
    from copyright_alert import lark_auth

    _freeze_brt(monkeypatch, 2026, 12, 1)
    monkeypatch.setattr(lark_auth, "_ALERT_EMAIL", cdp.CAIRO_EMAIL)
    monkeypatch.setattr(lark_auth, "_AUTH_ALERT_FINGERPRINTS", set())
    monkeypatch.setattr(lark_auth, "_load_alert_state", lambda: {})
    writes = []
    sends = []
    monkeypatch.setattr(lark_auth, "_save_alert_state", lambda state: writes.append(state))
    monkeypatch.setattr(
        lark_auth, "_send_alert_via_bot",
        lambda *args: sends.append(True) or True,
    )

    assert lark_auth.send_stale_token_alert("test-context", "test-detail") is True
    assert len(sends) == 1
    assert len(writes) == 1
    assert "test-context" in writes[0]
