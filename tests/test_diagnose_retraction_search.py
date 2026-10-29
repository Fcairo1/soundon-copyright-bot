import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "diagnose_retraction_search",
    Path(__file__).resolve().parents[1] / "scripts" / "diagnostics" / "diagnose_retraction_search.py")
diag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag)


def test_summarize_search_items_shape():
    parsed = {"items": [{"meta_data": {"create_time": "2026-10-28", "from": "a@spotify.com", "title": "Claim withdrawn"}}]}
    count, rows = diag.summarize(parsed)
    assert count == 1 and "a@spotify.com" in rows[0] and "Claim withdrawn" in rows[0]


def test_summarize_triage_shape_and_empty():
    count, rows = diag.summarize({"data": {"messages": [{"date": "d", "from": "x@y", "subject": "S"}]}})
    assert count == 1 and rows == ["d | x@y | S"]
    assert diag.summarize({}) == (0, [])
