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
