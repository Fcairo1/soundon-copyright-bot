from copyright_alert.run_alert import (
    NON_CLAIM_RELAY_NOTICE_WARNING,
    _extract_copyright_infringement_submission,
    build_card,
    detect_email_source,
    extract_fields,
    is_possible_non_claim_relay_notice,
)


# --- FUGA relay claims (Apple/Amazon DMCA notices forwarded via FUGA) -------

FUGA_APPLE_CLAIM_BODY = """
Dear TikTok Technology Limited,

Please be informed that Apple Music has notified us in relation to a DMCA
claim against the following recording(s):
UPC: 054853875117

Please review the claim details and contact the claimant at: rights@example.com
to resolve the issue as soon as possible.
"""

FUGA_STREAMING_REPORT_BODY = """
Dear TikTok Technology Limited,

In an effort to mitigate illegitimate activity and copyright infringement,
FUGA regularly monitors royalty and analytics data and checks outgoing
content for artificial streaming activity.
"""


def test_fuga_sender_is_classified_as_fuga_not_other():
    meta = {"from": "claims@fuga.com"}
    source = detect_email_source(FUGA_APPLE_CLAIM_BODY, "Apple Music DMCA Claim Notification - UPC: 054853875117", meta)
    assert source == "FUGA"


def test_lowercase_fuga_in_body_does_not_false_positive():
    # "fuga" (lowercase) is an ordinary PT/ES/IT word ("flight/escape") that
    # can legitimately show up in a claim description body — must not be
    # mistaken for the FUGA relay.
    body = "Additional info: o artista fugiu da fuga de direitos autorais alegada."
    meta = {"from": "infringement-claim-response@spotify.com"}
    source = detect_email_source(body, "Possibly Infringing - Notification Warning No 1", meta)
    assert source != "FUGA"


def test_audiosalad_detected_from_infringement_address_not_just_support():
    meta = {"from": "infringement@audiosalad.com"}
    source = detect_email_source("We have received an infringement claim for your content.", "Infringement Claim: SoundCloud - UPC 5063965208763 - SoundOn", meta)
    assert source == "AudioSalad"


def test_audiosalad_domain_check_does_not_collide_with_lookalike_domain():
    # "audiosalad.company.net" shares the "audiosalad.com" prefix but is a
    # different domain entirely — must not be mistaken for the real relay.
    meta = {"from": "someone@audiosalad.company.net"}
    body = "This is an unrelated email with no relay signature."
    source = detect_email_source(body, "Some other subject", meta)
    assert source != "AudioSalad"


def test_fuga_bare_word_ignores_body_text():
    # A claim body/title could legitimately contain "FUGA" in caps (common in
    # Brazilian funk track/artist naming); the bare-word fallback must not
    # scan body text, only sender/subject.
    meta = {"from": "infringement-claim-response@spotify.com"}
    body = "Additional info: TRACK TITLE FUGA DO GUETO by MC EXAMPLE."
    source = detect_email_source(body, "Possibly Infringing - Notification Warning No 1", meta)
    assert source != "FUGA"


# --- Non-claim relay notices (e.g. FUGA's streaming report) -----------------

def test_streaming_report_subject_flagged_as_non_claim_notice():
    subject = "TikTok Technology Limited - Apple Music Artificial Streaming Report [04-08-2026 - 08-08-2026]"
    assert is_possible_non_claim_relay_notice(subject, FUGA_STREAMING_REPORT_BODY) is True


def test_streaming_report_body_mentioning_infringement_does_not_confuse_the_check():
    # The report body uses the word "infringement" in passing; the subject is
    # still the reliable signal and must win.
    subject = "Apple Music Artificial Streaming Report [04-08-2026 - 08-08-2026]"
    assert "infringement" in FUGA_STREAMING_REPORT_BODY.lower()
    assert is_possible_non_claim_relay_notice(subject, FUGA_STREAMING_REPORT_BODY) is True


def test_real_dmca_claim_subject_not_flagged_as_non_claim_notice():
    subject = "Apple Music DMCA Claim Notification - UPC: 054853875117"
    assert is_possible_non_claim_relay_notice(subject, FUGA_APPLE_CLAIM_BODY) is False


def test_extract_fields_sets_non_claim_notice_flag():
    fields = extract_fields(
        FUGA_STREAMING_REPORT_BODY,
        "Apple Music Artificial Streaming Report [04-08-2026 - 08-08-2026]",
        {"from": "trustandsafety@fuga.com"},
    )
    assert fields["possible_non_claim_notice"] is True


def test_build_card_shows_non_claim_notice_warning():
    ef = extract_fields(
        FUGA_STREAMING_REPORT_BODY,
        "Apple Music Artificial Streaming Report [04-08-2026 - 08-08-2026]",
        {"from": "trustandsafety@fuga.com"},
    )
    ef.update({"title": "Example Release", "date_received": "2026-08-12"})
    ar = {"album_title": "Example Release", "display_artist": "Example Artist"}

    card_text = str(build_card(ef, ar))

    assert NON_CLAIM_RELAY_NOTICE_WARNING in card_text


# --- Nested "Copyright Infringement Submission" sub-form (AudioSalad relay) -

AUDIOSALAD_NESTED_SUBMISSION_BODY = """
Hello,

We have received an infringement claim for your content. Please see the
details below and inform us of how you would like to respond to the claim.

Claimant: Alyssa DeFonte - alyssa.defonte@example.com
DSP(s): YouTube
UPC(s): 5063970532365
Additional info:

Copyright Infringement Submission
-----
From :Alyssa DeFonteEmail:alyssa.defonte@example.comCompany: Example Music Publishing

Name of track / video you would like us to take down: Example Track Name

Link to infringed content: https://youtube.com/watch?v=example

Name of copyright owner: Example Music Publishing

More detail: This takedown request is due to the unauthorized use of the
copyrighted musical work.

Google Ad Fields:
--
If you accept the claim, please deliver takedowns for the affected content ASAP.
"""


def test_nested_submission_fills_company_from_glued_label_line():
    fields = extract_fields(AUDIOSALAD_NESTED_SUBMISSION_BODY, "Infringement Claim: YouTube - 5063970532365 - SoundOn", {})
    assert fields["claimant_company"] == "Example Music Publishing"


def test_nested_submission_fills_title_from_unrecognized_label():
    fields = extract_fields(AUDIOSALAD_NESTED_SUBMISSION_BODY, "Infringement Claim: YouTube - 5063970532365 - SoundOn", {})
    assert fields["title"] == "Example Track Name"


def test_nested_submission_ignores_an_earlier_unrelated_from_email_pair():
    # A forwarded chain can carry an earlier, unrelated "From:"/"Email:" pair
    # (e.g. a quoted header block) before the actual sub-form. The parser
    # must scope to the sub-form marker, not grab the first pair in the body.
    body_with_earlier_header = (
        "From: no-reply@some-other-system.com\n"
        "Email: not-the-claimant@some-other-system.com\n"
        "\n" + AUDIOSALAD_NESTED_SUBMISSION_BODY
    )
    overrides = _extract_copyright_infringement_submission(body_with_earlier_header)
    assert overrides["claimant_email"] == "alyssa.defonte@example.com"
    assert overrides["claimant_name"] == "Alyssa DeFonte"


def test_nested_submission_does_not_overwrite_a_value_already_found():
    # claimant_email is already correctly picked up by the generic extractor
    # from the top-level "Claimant:" line; the sub-form parser must not
    # clobber it even though it also finds an "Email:" value.
    fields = extract_fields(AUDIOSALAD_NESTED_SUBMISSION_BODY, "Infringement Claim: YouTube - 5063970532365 - SoundOn", {})
    assert fields["claimant_email"] == "alyssa.defonte@example.com"
