from copyright_alert import bot_runtime as br
from copyright_alert import daily_workflow as dw
from copyright_alert import run_alert as ra


def test_retraction_detection_requires_exact_sender_and_phrase():
    meta = {
        "head_from": {"mail_address": "infringement-claim-response@spotify.com"},
        "date_formatted": "2026-09-10T12:30:00Z",
    }
    body = "Hello,\n\nThe claimant has retracted the claim.\n\nUPC: 123456789012"
    assert ra.is_retraction_email(body, meta=meta) is True

    wrong_sender = {
        "head_from": {"mail_address": "rights@fuga.com"},
        "date_formatted": "2026-09-10T12:30:00Z",
    }
    assert ra.is_retraction_email(body, meta=wrong_sender) is False

    instructional_body = "Please work towards the notice being retracted as soon as possible."
    assert ra.is_retraction_email(instructional_body, meta=meta) is False


def test_extract_retraction_fields_uses_claim_labels():
    meta = {
        "head_from": {"mail_address": "infringement-claim-response@spotify.com"},
        "date_formatted": "2026-09-10T12:30:00Z",
    }
    body = """
Hello,

The claimant has retracted the claim.
Content Title: Midnight Echo
Artist: Example Artist
UPC: 123456789012
Spotify URI: spotify:track:abc123
ref:_00DA1ABC._5005xYz123:ref
"""
    fields = ra.extract_retraction_fields(body, meta=meta)

    assert fields["title"] == "Midnight Echo"
    assert fields["upc"] == "123456789012"
    assert fields["spotify_uri"] == "spotify:track:abc123"
    assert fields["ref_id"] == "ref:_00DA1ABC._5005xYz123:ref"
    assert fields["retracted_at"] == "2026-09-10T12:30:00Z"


def test_match_tracker_row_for_retraction_prefers_ref_code_and_open_row():
    values = [
        ["UPC", "Title", "Artist", "Status", "ref_code", "Retracted"],
        ["123456789012", "Older Match", "Artist A", "✅ Resolved", "ref:_old:ref", "Yes"],
        ["123456789012", "Current Match", "Artist A", "🔍 Investigating", "ref:_target:ref", ""],
        ["123456789012", "Same UPC Different Claim", "Artist A", "", "ref:_other:ref", ""],
    ]

    match = dw.match_tracker_row_for_retraction(
        values,
        {"upc": "123456789012", "ref_id": "ref:_target:ref"},
    )

    assert match is not None
    assert match["row_num"] == 3
    assert match["title"] == "Current Match"
    assert match["ref_match"] is True


def test_open_status_excludes_retracted_rows():
    row = {
        "Status": "🔍 Investigating",
        "Admin Action Taken": "",
        "Retracted": "Yes",
    }
    assert br._is_open_status(row) is False
