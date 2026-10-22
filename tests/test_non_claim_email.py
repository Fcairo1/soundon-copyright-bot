"""Spotify mails that mention existing claims must never become new claims.

Bodies mirror the real emails (Aug 10 claim, Oct 15 PPP report, Oct 20 reply)."""
import pytest

from copyright_alert import daily_workflow as dw
from copyright_alert import run_alert as ra
from copyright_alert.non_claim_email import is_non_claim_email, non_claim_reason

REAL_CLAIM = (
    "Hello, We have received a complaint that your content infringes the sound recording rights "
    "of a third party. While this claim is under investigation, the content has been taken down. "
    "Claimant Name: Sarah Claudia Regis Email: juridico@h7music.com Company: Claim Type: sound "
    "recording Claimed Territory: WORLDWIDE Claimant's Description: Initial Claim Description: "
    "content_description:The song was released without our permission; this is a pirated copy of "
    "our original content. Content Title: Relíquia 2024 Artist: DJ Henrique da Sul, Love Fluxos "
    "UPC: 751238693884 Label Name: Love Fluxos Content Type: TRACK URI: spotify:track:1HpgTauGEKpyHDOt5pTFNU"
)
REAL_SUBJECT = "Possibly Infringing - Notification Warning No 1 - Love Fluxos - Claim 24739502"

PPP_REPORT = (
    "Hello, We're following up on last month's Excessive Infringement Policy Requirement report. "
    "The latest report shows your catalog has exceeded the policy threshold, and your status "
    'remains "At Risk." Your monthly infringement claim removals exceed 1,000 URIs. Your Stat: 1640 '
    "Your deliveries of content subject to infringement claims are considered excessive. Your Stat: 0.58% "
    "You confirm rights to releases in Held in Review that are later removed due to an infringement claim. "
    "Month Licensor Name Album URI Case Number Entity URI External ID Aug 2026 SoundOn "
    "spotify:album:0fC99fEihwcee8HcYBzm8e 24739502 spotify:track:4jq2TCOtEdeZqMjGLSBIBR "
    "isrc=AEA3A24 upc=751238693884"
)

CONTEST_REPLY = (
    "Hello, Thank you for getting in touch. If you would like to contest a claim, please respond "
    "directly to the takedown notification you received from us with the subject: "
    '"Possibly Infringing - Notification Warning No 1 - Love Fluxos - Claim 24786925 - '
    'ref:_00D0992XChO._500QvguTx5:ref" Content Affected URI: spotify:track:7b8vUjIdQ4l7VfdBajSdoz '
    "UPC: 644401000933 Best regards, Spotify Content Protection"
)


def test_real_claim_is_not_flagged():
    assert non_claim_reason(REAL_CLAIM, REAL_SUBJECT) is None


def test_ppp_report_is_flagged():
    assert "PPP" in non_claim_reason(PPP_REPORT, "PPP Excessive Infringement")


def test_ppp_report_flagged_from_subject_alone_context():
    assert is_non_claim_email("Your status remains At Risk. upc=751238693884", "PPP Excessive Infringement")


def test_contest_reply_is_flagged_despite_quoting_the_notice_subject():
    assert "contest" in non_claim_reason(CONTEST_REPLY, "Infringement Claim Response")


def test_real_claim_wins_even_if_it_mentions_the_policy():
    body = REAL_CLAIM + " See our Excessive Infringement Policy; your status may be At Risk."
    assert non_claim_reason(body, REAL_SUBJECT) is None


def test_unrelated_email_is_not_flagged():
    assert non_claim_reason("Copyright Infringement Submission UPC: 123", "Infringement Claim: FUGA - x") is None


@pytest.mark.parametrize("body,subject", [(PPP_REPORT, "PPP Excessive Infringement"),
                                           (CONTEST_REPLY, "Infringement Claim Response")])
def test_extract_claim_entries_returns_nothing_for_non_claims(body, subject):
    assert ra.extract_claim_entries(body, subject, {}) == []


def test_extract_claim_entries_still_parses_real_claim():
    entries = ra.extract_claim_entries(REAL_CLAIM, REAL_SUBJECT, {})
    assert entries and entries[0]["upc"] == "751238693884"


@pytest.mark.parametrize("body,subject", [(PPP_REPORT, "PPP Excessive Infringement"),
                                           (CONTEST_REPLY, "Infringement Claim Response")])
def test_daily_workflow_skips_non_claims_before_any_handler(monkeypatch, body, subject):
    monkeypatch.setattr(dw, "fetch_email", lambda _id: (body, {}))
    called = []
    monkeypatch.setattr(dw.metadata_notice, "is_metadata_notice", lambda *a, **k: called.append("meta") or False)
    monkeypatch.setattr(dw.rights_confirmation_notice, "is_rights_confirmation_notice",
                        lambda *a, **k: called.append("rights") or True)
    summary = {"skipped_prefilter": 0, "examined": 0, "skipped_no_identifier": 0}
    kind, info = dw._parse_candidate("m1", subject, "2026-10-22", "t1", set(), summary)
    assert (kind, info) == ("skip", "non-claim email")
    assert called == []
    assert summary["skipped_non_claim"] == 1
