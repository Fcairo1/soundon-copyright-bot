import subprocess

import pytest

from copyright_alert import daily_workflow as dw
from copyright_alert import lark_auth, run_log


@pytest.fixture
def rows(monkeypatch):
    out = []
    monkeypatch.setattr(run_log, "append_row", lambda row: out.append(list(row)) or True)
    return out


def test_disabled_without_sheet_url(monkeypatch):
    monkeypatch.delenv("RUN_LOG_SHEET_URL", raising=False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call lark-cli")))
    assert run_log.enabled() is False and run_log.append_row(["x"]) is False


def test_kill_switch(monkeypatch):
    monkeypatch.setenv("RUN_LOG_SHEET_URL", "https://example/sheets/abc")
    monkeypatch.setenv("RUN_LOG_ENABLED", "0")
    assert run_log.enabled() is False


def test_append_row_pads_to_17_columns_and_calls_lark_cli(monkeypatch):
    monkeypatch.setenv("RUN_LOG_SHEET_URL", "https://example/sheets/abc")
    monkeypatch.setenv("RUN_LOG_SHEET_ID", "16b7cb")
    seen = {}

    def fake_run(cmd, **k):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "{}", "")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert run_log.append_row(["a", "b\nc"]) is True
    cmd = seen["cmd"]
    assert cmd[:4] == ["lark-cli", "sheets", "+append", "--url"] and "--sheet-id" in cmd and "16b7cb" in cmd
    import json
    values = json.loads(cmd[cmd.index("--values") + 1])
    assert len(values) == 1 and len(values[0]) == 17 and values[0][1] == "b c"


def test_append_failure_never_raises(monkeypatch):
    monkeypatch.setenv("RUN_LOG_SHEET_URL", "https://example/sheets/abc")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no lark-cli")))
    assert run_log.append_row(["a"]) is False
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, "", "boom"))
    assert run_log.append_row(["a"]) is False


def test_started_and_final_rows_share_report_id(rows):
    rl = run_log.RunLog("Daily Scan", "BR", log_file="/x/logs/daily_1.log")
    rl.start(token_minutes=110, mode="full")
    rl.finish("OK", "done", numbers={"cards_posted": 2})
    assert len(rows) == 2 and rows[0][0] == rows[1][0] and rows[0][0].startswith("soundon-br-daily-scan-")
    assert rows[0][5] == "STARTED" and rows[0][6] == "No" and "token_min_at_start=110" in rows[0][9]
    assert rows[1][5] == "OK" and "cards_posted=2" in rows[1][9] and "duration_min=" in rows[1][9]
    assert rows[1][12] == "daily_1.log" and len(rows[1]) == 17
    assert rows[1][15] == "" and rows[1][16] == ""          # Claude's columns stay empty


def test_finish_only_once(rows):
    rl = run_log.RunLog("Daily Scan", "US")
    rl.finish("OK", "a")
    rl.finish("FAILED", "b")
    assert len(rows) == 1


def test_summarize_results_statuses():
    ok = run_log.summarize_results({"scan": {"examined": 5, "parsed_candidates": 3, "posted": 3}}, ["1A", "M"])
    assert ok[0] == "OK" and "3 card(s)" in ok[1] and "parts_completed=" not in ok[1] and ok[3]["parts_completed"] == "1A,M"
    warn = run_log.summarize_results({"scan": {"posted": 1}, "retractions": {"error": "x"}}, [])
    assert warn[0] == "WARNING" and "retractions" in warn[2]
    failed = run_log.summarize_results({"scan": {"error": "boom"}}, [])
    assert failed[0] == "FAILED"


def _stub_main_env(monkeypatch):
    monkeypatch.setattr(dw, "_setup_logging", lambda: "/tmp/logs/daily_x.log")
    monkeypatch.setattr(dw, "log", lambda *a, **k: None)
    monkeypatch.setattr(dw, "_current_env_jwt_diag", lambda: "")


def test_main_writes_started_and_final_rows(monkeypatch, rows):
    _stub_main_env(monkeypatch)
    monkeypatch.setattr(lark_auth, "jwt_start_gate", lambda *a, **k: (True, 6000))
    def fake_main(only, skip, log_path, rl):
        rl.start(token_minutes=100)
        return {"scan": {"examined": 2, "parsed_candidates": 1, "posted": 1}}
    monkeypatch.setattr(dw, "_main", fake_main)
    dw.main()
    assert [r[5] for r in rows] == ["STARTED", "OK"]


def test_main_crash_writes_failed_row(monkeypatch, rows):
    _stub_main_env(monkeypatch)
    monkeypatch.setattr(dw, "_main", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        dw.main()
    assert rows[-1][5] == "FAILED" and "boom" in rows[-1][8]


def test_jwt_abort_writes_started_then_failed(monkeypatch, rows):
    _stub_main_env(monkeypatch)
    monkeypatch.setattr(lark_auth, "jwt_start_gate", lambda *a, **k: (False, 120))
    monkeypatch.setattr(dw, "run_scan", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not scan")))
    with pytest.raises(SystemExit) as exc:
        dw.main()
    assert exc.value.code == 3
    assert [r[5] for r in rows] == ["STARTED", "FAILED"] and "ABORT" in rows[1][8] and "JWT" in rows[1][7]
