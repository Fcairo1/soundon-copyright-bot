from copyright_alert.run_alert import (
    CONTENT_ID_DISPUTE_WARNING,
    build_card,
    extract_fields,
    is_possible_content_id_release_request,
)


EXAMPLE_BODY = """
Claimant: ONErpm
DSP: YouTube, all
UPC: 5063963471855
Additional info: Our client, PAVUNA, has informed us that they believe your claims on the following assets associated with our MCN are incorrect. Could you please review the claim and release it or provide additional information regarding your rights?
"""


def test_detects_content_id_release_request_example():
    assert is_possible_content_id_release_request(EXAMPLE_BODY) is True


def test_hard_guard_keeps_real_infringement_claims_unflagged():
    body = """
    Additional info: We are the rights holder and this release is infringing our work.
    Please remove the infringing content from the service.
    """

    assert is_possible_content_id_release_request(body) is False


def test_extract_fields_sets_warning_flag_from_body():
    fields = extract_fields(EXAMPLE_BODY, "Infringement Claim: YouTube - UPC 5063963471855", {})

    assert fields["upc"] == "5063963471855"
    assert fields["possible_content_id_release_request"] is True


def test_build_card_adds_visible_warning_without_changing_actions():
    ef = extract_fields(EXAMPLE_BODY, "Infringement Claim: YouTube - UPC 5063963471855", {})
    ef.update({"title": "Example Release", "date_received": "2026-07-24"})
    ar = {"album_title": "Example Release", "display_artist": "PAVUNA"}

    card = build_card(ef, ar)
    card_text = str(card)

    assert CONTENT_ID_DISPUTE_WARNING in card_text
    assert "copyright_alert_status_update" in card_text
