"""run_lark_cli: try -> reload JWT -> try again (once), for the long-lived daemon."""
import subprocess
import types

from copyright_alert import lark_auth

EXPIRED = "failed to validate jwt token: token is expired by 70h38m"


def _proc(rc, out="", err=""):
    return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)


def _setup(monkeypatch, results, *, reloaded=1, expiry=None):
    calls, alerts = [], []
    it = iter(results)
    monkeypatch.setattr(lark_auth, "ensure_jwt_fresh", lambda *a, **k: True)
    monkeypatch.setattr(lark_auth, "_refresh_aime_credentials", lambda: reloaded)
    monkeypatch.setattr(lark_auth, "_best_env_jwt_expiry", expiry or (lambda: 100))
    monkeypatch.setattr(lark_auth, "send_stale_token_alert", lambda ctx, detail: alerts.append(ctx))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(1) or next(it))
    return calls, alerts


def test_success_runs_once_without_reload(monkeypatch):
    calls, alerts = _setup(monkeypatch, [_proc(0, "{}")])
    monkeypatch.setattr(lark_auth, "_refresh_aime_credentials", lambda: (_ for _ in ()).throw(AssertionError("no reload")))
    assert lark_auth.run_lark_cli(["lark-cli", "x"], context="t", timeout=5).returncode == 0
    assert len(calls) == 1 and not alerts


def test_expired_then_reload_then_retry_succeeds(monkeypatch):
    calls, alerts = _setup(monkeypatch, [_proc(4, "", EXPIRED), _proc(0, '{"ok": true}')])
    res = lark_auth.run_lark_cli(["lark-cli", "x"], context="t", timeout=5)
    assert res.returncode == 0 and len(calls) == 2 and not alerts


def test_expired_and_no_fresher_jwt_returns_failure_and_alerts_without_retry(monkeypatch):
    calls, alerts = _setup(monkeypatch, [_proc(4, "", EXPIRED)], reloaded=0)
    res = lark_auth.run_lark_cli(["lark-cli", "x"], context="t", timeout=5)
    assert res.returncode == 4 and len(calls) == 1 and alerts == ["t"]


def test_retry_still_expired_gives_up_after_one_retry_and_alerts(monkeypatch):
    calls, alerts = _setup(monkeypatch, [_proc(4, "", EXPIRED), _proc(4, "", EXPIRED)])
    res = lark_auth.run_lark_cli(["lark-cli", "x"], context="t", timeout=5)
    assert res.returncode == 4 and len(calls) == 2 and alerts == ["t"]


def test_other_failures_are_never_retried(monkeypatch):
    calls, alerts = _setup(monkeypatch, [_proc(1, "", "permission denied")])
    monkeypatch.setattr(lark_auth, "_refresh_aime_credentials", lambda: (_ for _ in ()).throw(AssertionError("no reload")))
    assert lark_auth.run_lark_cli(["lark-cli", "x"], context="t", timeout=5).returncode == 1
    assert len(calls) == 1 and not alerts


def test_retry_when_another_thread_already_reloaded(monkeypatch):
    """This thread's reload finds nothing new, but the env got newer while the call
    ran (another thread reloaded first) -> still retry."""
    seq = iter([100, 200])   # expiry before the call, then after the (no-op) reload
    calls, alerts = _setup(monkeypatch, [_proc(4, "", EXPIRED), _proc(0, "{}")],
                           reloaded=0, expiry=lambda: next(seq))
    res = lark_auth.run_lark_cli(["lark-cli", "x"], context="t", timeout=5)
    assert res.returncode == 0 and len(calls) == 2


def test_looks_like_expired_jwt():
    assert lark_auth.looks_like_expired_jwt(EXPIRED)
    assert not lark_auth.looks_like_expired_jwt("tip: run lark-cli mail user_mailboxes profile")
