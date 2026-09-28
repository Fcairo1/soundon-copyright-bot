from copyright_alert import dm_action_card
from copyright_alert import persistent_callback as pc
from copyright_alert import run_alert as ra
from copyright_alert import spotify_reply


def test_enrich_case_for_reply_loads_source_email_from_posted_claim_message(monkeypatch):
    monkeypatch.setattr(
        pc,
        "_posted_claim_record_for_upc",
        lambda upc: {
            "upc": upc,
            "source_email_message_id": "msg_123",
            "claimant_email": "beats@goreocean.info",
            "ref_id": "ref:spotify:123:ref",
        },
    )
    monkeypatch.setattr(ra, "fetch_email", lambda mid: ("body", {"subject": "Claim", "head_from": {"mail_address": "infringement-claim-response@spotify.com"}}))
    monkeypatch.setattr(
        ra,
        "extract_fields",
        lambda body, subject, meta: {"claimant_email": "beats@goreocean.info", "ref_id": "ref:spotify:123:ref"},
    )
    monkeypatch.setattr(ra, "_sender_email_from_meta", lambda meta: "infringement-claim-response@spotify.com")

    case = pc._enrich_case_for_reply({"upc": "5063965051437"})

    assert case["source_email_message_id"] == "msg_123"
    assert case["source_email"] == "infringement-claim-response@spotify.com"
    assert case["reply_to_email"] == "infringement-claim-response@spotify.com"
    payload = dm_action_card.build_dm_action_card(case)
    payload_text = str(payload)
    assert "infringement-claim-response@spotify.com" in payload_text


def test_send_reply_uses_source_email_as_fallback_recipient(monkeypatch):
    captured = {}

    def fake_create_reply_draft(**kwargs):
        captured.update(kwargs)
        return {"draft_id": "draft_123", "draft_link": "https://mail.example/draft_123"}

    monkeypatch.setattr(spotify_reply, "create_reply_draft", fake_create_reply_draft)

    result = spotify_reply.send_reply(
        reply_type="agree",
        source_email_message_id="legacy_or_missing",
        claimant_email="beats@goreocean.info",
        source_email="infringement-claim-response@spotify.com",
        upc="5063965051437",
        title="Example Title",
        ref_id="ref:spotify:123:ref",
    )

    assert result["ok"] is True
    assert captured["to"] == "infringement-claim-response@spotify.com"
    assert captured["thread_message_id"] == "legacy_or_missing"


def test_send_reply_flags_successful_threading(monkeypatch):
    monkeypatch.setattr(spotify_reply, "create_reply_draft", lambda **k: {
        "draft_id": "d1", "draft_link": "https://mail.example/d1", "threaded": True,
    })
    result = spotify_reply.send_reply(
        reply_type="agree", source_email_message_id="msg1", claimant_email="a@b.com",
        upc="123", title="T", ref_id="ref:1",
    )
    assert result["threaded"] is True
    assert result["thread_warning"] == ""


def test_send_reply_surfaces_threading_failure_loudly(monkeypatch):
    monkeypatch.setattr(spotify_reply, "create_reply_draft", lambda **k: {
        "draft_id": "d1", "draft_link": "https://mail.example/d1",
        "threaded": False, "warning": "original message was missing smtp_message_id",
    })
    result = spotify_reply.send_reply(
        reply_type="agree", source_email_message_id="msg1", claimant_email="a@b.com",
        upc="123", title="T", ref_id="ref:1",
    )
    assert result["ok"] is True  # a draft WAS created — this isn't a failure
    assert result["threaded"] is False
    assert "could not" in result["thread_warning"].lower()
    assert "missing smtp_message_id" in result["thread_warning"]
