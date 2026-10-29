from copyright_alert import daily_workflow as dw


def test_no_marker_when_no_cards_were_posted(monkeypatch):
    monkeypatch.setattr(dw, "post_card", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not post")))
    assert dw.post_cards_finished_marker(0) is False


def test_marker_posts_to_the_regions_group_once(monkeypatch):
    seen = []
    monkeypatch.setattr(dw, "post_card", lambda card, **k: seen.append((card, k)) or (True, "om_1"))
    monkeypatch.setattr(dw, "TARGET_CHAT_ID", "oc_group")
    monkeypatch.setattr(dw, "ACTIVE_REGION", "SPLA")
    assert dw.post_cards_finished_marker(3) is True
    card, kw = seen[0]
    assert kw["chat_id"] == "oc_group" and kw["expected_region"] == "SPLA"
    assert "SPLA" in card["header"]["title"]["content"] and "finished" in card["header"]["title"]["content"]
    assert "3 new claim cards" in card["elements"][0]["content"]


def test_singular_wording(monkeypatch):
    assert "1 new claim card**" in dw.build_cards_finished_card(1, "BR")["elements"][0]["content"]


def test_marker_failure_never_breaks_the_run(monkeypatch):
    monkeypatch.setattr(dw, "post_card", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert dw.post_cards_finished_marker(2) is False
    monkeypatch.setattr(dw, "post_card", lambda *a, **k: (False, ""))
    assert dw.post_cards_finished_marker(2) is False
