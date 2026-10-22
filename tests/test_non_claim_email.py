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


QUERY_REPLY = (
    "Hi, Please refer to our email sent to you with the subject line \"Re: Possibly Infringing - "
    "Notification Warning No 1 - H7 Music - Claim 24769868 - ref:_00D0992XChO._500QvgqzTz:ref\" regarding this. "
    "Best regards, Spotify Content Protection ref:_00D0992XChO._500QvkESX0:ref "
    "--------------- Original Message --------------- From: SoundOn [soundon-copyright@bytedance.com] "
    "Subject: [SoundOn] Request to Reinstate Content Following Review — 033932262024 "
    "Dear Spotify Content Operations Team, ... Album Title: Eu Falei pra Elas UPC: 033932262024"
)


def test_query_regarding_claim_reply_is_flagged():
    assert "Query regarding claim" in non_claim_reason(QUERY_REPLY, "Query regarding claim 25519890 - ref:_00D0992XChO._500QvkESX0:ref")
    assert is_non_claim_email(QUERY_REPLY, "Query regarding claim 25520081")
    assert ra.extract_claim_entries(QUERY_REPLY, "Query regarding claim 25519890", {}) == []


FORWARDED_TICKET = (
    "---------- Forwarded message ---------- From: \"Mariana Fusaro (Matter Collective)\" <support@matter.is> "
    "Date: Tuesday, October 20 2026 at 9:36 AM CDT Subject: [Matter Collective] Content Removal - Sam Business "
    "To: Jeff Rojas <jeff@matter.is>, Kyle Trafton <kyle@matter.is> "
    "A ticket (#5150) by Mariana Fusaro has been received. It is unassigned. "
    "Mariana Fusaro Oct 20, 2026, 09:36 CDT Hello Matter team, Hope everything is well! "
    "We've noticed that the following content is infringing the rights of one of our clients, Sam Business. "
    "According to our client, the songs below are unauthorized versions of our artist's song. "
    "Could you please check with your client and proceed with the removal from all platforms? "
    "UPC: 5064078954943 Link: https://open.spotify.com/album/5POoY0yg5IjdHJ2ezoXmkk "
    "According to DMCA procedure, I state that I believe in good faith there is a copyright infringement. "
    "Best regards, -- Mariana Fusaro Copyright São Paulo, BR onerpm.com You are an agent. Add a comment "
    "by replying to this email or view ticket in Zendesk Support. Ticket # 5150 Status New Requester "
    "Mariana Fusaro CCs Copyright Followers Delivered by Zendesk [7YL2Y7-GPGGX]Ticket-Id:5150"
)


def test_forwarded_matter_ticket_uses_requester_as_claimant():
    meta = {"from": "someone@bytedance.com"}
    entries = ra.extract_claim_entries(FORWARDED_TICKET, "Fwd: [Matter Collective] Content Removal - Sam Business", meta)
    ef = entries[0]
    assert ef["upc"] == "5064078954943"
    assert ef["claimant_name"] == "Mariana Fusaro"
    assert ef["claimant_email"] == "N/A"          # support@matter.is is the relay, not the claimant
    assert ef["claimant_company"] == "Sam Business (via Matter Collective)"
    assert ef["email_source"] == "Matter Collective"
    assert ef["claimant_message"].startswith("Hope everything is well")
    assert "Zendesk" not in ef["claimant_message"] and "onerpm" not in ef["claimant_message"]


def test_signature_fallback_no_longer_swallows_non_spotify_signatures():
    body = "UPC: 5064078954943 Best regards, -- Some Person Copyright São Paulo, BR example.com"
    ef = ra.extract_fields(body, "Fwd: claim", {})
    assert "São Paulo" not in ef["claimant_name"]


DISPUTE_ACK = (
    "Hi, Thank you for letting us know. The content will remain taken down from the service until the "
    "provider reaches a resolution with the claimant. Both parties should inform us once they reach a "
    "resolution. We will make the content live upon the receipt of instructions and any necessary updates "
    "from both parties. If they cannot reach a resolution, we reserve the right to act at our discretion. "
    "The email we have for the claimant is x@example.com. Title: T Artist: A UPC: 751238998965 "
    "Content Type: TRACK URI: spotify:track:abc"
)


def test_dispute_acknowledgement_is_flagged():
    assert "dispute" in non_claim_reason(DISPUTE_ACK, "Thank you for your inquiry SoundOn")
    assert ra.extract_claim_entries(DISPUTE_ACK, "Thank you for your inquiry SoundOn", {}) == []


def test_generic_inquiry_subject_alone_is_not_flagged():
    assert non_claim_reason(REAL_CLAIM, "Thank you for your inquiry SoundOn") is None
