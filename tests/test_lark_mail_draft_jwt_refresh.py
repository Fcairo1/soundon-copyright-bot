import subprocess
import types

from copyright_alert import lark_auth, lark_mail_draft as lmd

EXPIRED = '{"ok": false, "error": {"message": "failed to validate jwt token: token is expired by 70h38m"}}'


def _proc(rc, out="", err=""):
    return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)


def test_preflight_runs_before_every_lark_cli_mail_call(monkeypatch):
    calls = []
    monkeypatch.setattr(lark_auth, "ensure_jwt_fresh", lambda ctx, *a, **k: calls.append(("preflight", ctx)) or True)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(("run",)) or _proc(0, '{"ok": true}'))
    assert lmd._lark_cli_json(["+triage"]) == {"ok": True}
    assert calls == [("preflight", "lark_mail_draft"), ("run",)]


def test_expired_jwt_triggers_one_reload_and_retry(monkeypatch):
    monkeypatch.setattr(lark_auth, "ensure_jwt_fresh", lambda *a, **k: True)
    monkeypatch.setattr(lark_auth, "_refresh_aime_credentials", lambda: 2)
    results = iter([_proc(4, EXPIRED), _proc(0, '{"draft_id": "d1"}')])
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: seen.append(1) or next(results))
    assert lmd._lark_cli_json(["+reply"]) == {"draft_id": "d1"}
    assert len(seen) == 2


def test_no_retry_when_nothing_was_reloaded(monkeypatch):
    monkeypatch.setattr(lark_auth, "ensure_jwt_fresh", lambda *a, **k: True)
    monkeypatch.setattr(lark_auth, "_refresh_aime_credentials", lambda: 0)
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: seen.append(1) or _proc(4, EXPIRED))
    lmd._lark_cli_json(["+reply"])
    assert len(seen) == 1


def test_other_failures_are_not_retried(monkeypatch):
    monkeypatch.setattr(lark_auth, "ensure_jwt_fresh", lambda *a, **k: True)
    monkeypatch.setattr(lark_auth, "_refresh_aime_credentials", lambda: (_ for _ in ()).throw(AssertionError("no reload")))
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: seen.append(1) or _proc(1, '{"ok": false, "error": {"message": "other"}}'))
    lmd._lark_cli_json(["+reply"])
    assert len(seen) == 1
