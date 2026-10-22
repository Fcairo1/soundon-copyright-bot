import pytest

from copyright_alert import lark_auth

URL = "https://bytedance.sg.larkoffice.com/sheets/ABC123def456"
RANGES = [{"range": "sid!A2:A2", "values": [[1]]}, {"range": "sid!B2:B2", "values": [[2]]}]
SCOPE_BODY = ('{"code":99991679,"msg":"Unauthorized. You do not have permission to perform the requested '
              'operation on the resource. Please request user re-authorization and grant the scopes"}')


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(lark_auth, "_batch_scope_missing_since", 0.0)
    monkeypatch.setattr(lark_auth, "send_stale_token_alert",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("a scope error must not raise a stale-token alert")))


def _wire(monkeypatch):
    attempts, puts = [], []

    def fake_retry(make_request, timeout=0, context=""):
        attempts.append(context)
        raise RuntimeError(f"Unexpected request failure in {context}: {SCOPE_BODY}")

    monkeypatch.setattr(lark_auth, "request_json_with_auth_retry", fake_retry)
    monkeypatch.setattr(lark_auth, "sheet_values_api",
                        lambda method, url, sheet_id, rng, values=None, timeout=0: puts.append((sheet_id, rng, values)) or {"code": 0})
    return attempts, puts


def test_99991679_is_a_scope_problem_not_an_auth_failure():
    assert lark_auth._looks_like_auth_failure(200, {"code": 99991679, "msg": "Unauthorized ... grant the scopes"}) is False
    assert lark_auth._looks_like_auth_failure(400, {"code": 99991679}) is False


@pytest.mark.parametrize("code", [99991663, 99991664])
def test_expired_token_codes_are_still_auth_failures(code):
    assert lark_auth._looks_like_auth_failure(200, {"code": code, "msg": "invalid access token"}) is True


def test_batch_scope_error_goes_straight_to_single_puts_with_one_attempt_and_no_alert(monkeypatch):
    attempts, puts = _wire(monkeypatch)
    out = lark_auth.sheet_values_batch_update(URL, RANGES)
    assert attempts == ["sheet_values_batch_update"]            # no second "forced" attempt
    assert out["fallback"] == "single_range_put" and len(puts) == 2
    assert puts[0] == ("sid", "A2:A2", [[1]])


def test_later_calls_skip_the_batch_endpoint_entirely(monkeypatch):
    attempts, puts = _wire(monkeypatch)
    lark_auth.sheet_values_batch_update(URL, RANGES)
    lark_auth.sheet_values_batch_update(URL, RANGES)
    assert attempts == ["sheet_values_batch_update"]            # probed once, remembered
    assert len(puts) == 4


def test_the_batch_endpoint_is_probed_again_after_the_recheck_window(monkeypatch):
    attempts, _puts = _wire(monkeypatch)
    lark_auth.sheet_values_batch_update(URL, RANGES)
    monkeypatch.setattr(lark_auth, "_batch_scope_missing_since", lark_auth.time.time() - lark_auth._BATCH_SCOPE_RECHECK_SEC - 5)
    lark_auth.sheet_values_batch_update(URL, RANGES)
    assert len(attempts) == 2


def test_a_working_batch_endpoint_is_used_normally(monkeypatch):
    monkeypatch.setattr(lark_auth, "request_json_with_auth_retry", lambda *a, **k: {"code": 0, "data": {}})
    monkeypatch.setattr(lark_auth, "sheet_values_api", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no fallback")))
    assert lark_auth.sheet_values_batch_update(URL, RANGES)["code"] == 0


def test_other_failures_still_raise(monkeypatch):
    def boom(make_request, timeout=0, context=""):
        raise RuntimeError("Request failed: 500 internal error")
    monkeypatch.setattr(lark_auth, "request_json_with_auth_retry", boom)
    with pytest.raises(RuntimeError, match="500"):
        lark_auth.sheet_values_batch_update(URL, RANGES)
