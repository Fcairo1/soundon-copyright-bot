import pytest

from copyright_alert import dm_upc_lookup as dul
from copyright_alert import handle_callback as hc


@pytest.fixture(autouse=True)
def _no_real_tracker_or_jwt(monkeypatch):
    """Unit tests must never hit the real Lark trackers / refresh real tokens."""
    monkeypatch.setattr(dul, "_tracker_lookup", lambda upc: [])
    monkeypatch.setattr(dul.lark_auth, "ensure_jwt_fresh", lambda *a, **k: True)


TARGET_UPC = "5063962570191"


def test_lookup_upc_falls_back_to_broad_claim_search_when_exact_upc_triage_misses(monkeypatch):
    triage_calls = []

    def fake_triage_search(query, *, max_results):
        triage_calls.append((query, max_results))
        if query == TARGET_UPC:
            return []
        if query == dul.FALLBACK_CLAIM_QUERIES[0]:
            return [{
                "message_id": "msg_1",
                "subject": "Possibly Infringing - Notification Warning No 1",
                "date": "2026-08-31T18:33:49Z",
            }]
        return []

    def fake_fetch_email(message_id):
        assert message_id == "msg_1"
        return (
            "Claimant Name: M M Fusaro\n"
            "Email: mfusaro@onerpm.com\n"
            "Release Title: ELA QUER EXCLUSIVIDADE\n"
            "Claimant's Description: unauthorized version\n"
            f"UPC(s): {TARGET_UPC}\n",
            {"date_formatted": "2026-08-31T18:33:49Z"},
        )

    def fake_extract_fields(body, subject, meta):
        return {
            "upc": TARGET_UPC,
            "title": "ELA QUER EXCLUSIVIDADE",
            "claimant_name": "M M Fusaro",
            "claimant_company": "ONErpm",
            "claimant_email": "mfusaro@onerpm.com",
            "claimant_message": "unauthorized version",
        }

    monkeypatch.setattr(dul, "_triage_search", fake_triage_search)
    monkeypatch.setattr(dul.ra, "fetch_email", fake_fetch_email)
    monkeypatch.setattr(dul.ra, "extract_fields", fake_extract_fields)

    result = dul.lookup_upc(TARGET_UPC)

    assert "Found 1 matching claim email(s)." in result
    assert "mfusaro@onerpm.com" in result
    assert "unauthorized version" in result
    assert triage_calls[0][0] == TARGET_UPC
    assert triage_calls[1][0] == dul.FALLBACK_CLAIM_QUERIES[0]


def test_lookup_upc_matches_when_body_contains_upc_with_whitespace(monkeypatch):
    monkeypatch.setattr(
        dul,
        "_candidate_messages",
        lambda upc: [{
            "message_id": "msg_2",
            "subject": "Possibly Infringing - Notification Warning No 1",
            "date": "2026-08-31T18:33:49Z",
        }],
    )

    monkeypatch.setattr(
        dul.ra,
        "fetch_email",
        lambda message_id: (
            "Claimant Name: M M Fusaro\n"
            "Email: mfusaro@onerpm.com\n"
            "Claimant's Description: unauthorized version\n"
            "UPC(s): 5063 9625 70191\n",
            {"date_formatted": "2026-08-31T18:33:49Z"},
        ),
    )
    monkeypatch.setattr(
        dul.ra,
        "extract_fields",
        lambda body, subject, meta: {
            "upc": "N/A",
            "title": "ELA QUER EXCLUSIVIDADE",
            "claimant_name": "M M Fusaro",
            "claimant_company": "ONErpm",
            "claimant_email": "mfusaro@onerpm.com",
            "claimant_message": "unauthorized version",
        },
    )

    result = dul.lookup_upc(TARGET_UPC)

    assert "Found 1 matching claim email(s)." in result
    assert "mfusaro@onerpm.com" in result
    assert "unauthorized version" in result


# --- regression: the bot said "No infringement claims found" for real claims ------

MULTI_UPC_BODY = """Hello,

We have received a complaint that your content infringes the sound recording rights of a third party.

Claimant

Name: Liliam Marques
Email: claim@example.com
Company:
Claim Type: composition
Claimed Territory: WORLDWIDE
Claimant's Description:
Initial Claim Description: content_description:unauthorized

Content

Title: First Album
Artist: DJ A
UPC: 021865991890
Label Name: Só Hits Records
Content Type: ALBUM
URI: spotify:album:aaa

Title: Second Album
Artist: DJ B
UPC: 698308096452
Label Name: Só Hits Records
Content Type: ALBUM
URI: spotify:album:bbb

If you, the content provider, wish to contest this claim...
Best Regards,
Spotify Content Protection
"""


def test_lookup_finds_a_claim_when_the_upc_is_not_the_first_one_in_the_email(monkeypatch):
    monkeypatch.setattr(dul, "_triage_search", lambda q, *, max_results: [
        {"message_id": "m1", "subject": "Possibly Infringing - Notification Warning No 1", "date": "2026-10-10T00:54:00Z"}])
    monkeypatch.setattr(dul.ra, "fetch_email", lambda mid: (MULTI_UPC_BODY, {"date_formatted": "2026-10-10"}))

    result = dul.lookup_upc("698308096452")

    assert "No infringement" not in result
    assert "Found 1 matching claim email(s)." in result
    assert "Second Album" in result            # the entry for THIS upc, not the first one


def test_failed_inbox_search_is_reported_not_treated_as_no_claims(monkeypatch):
    class Res:
        returncode, stdout, stderr = 1, "", "token is expired by 2h42m"
    monkeypatch.setattr(dul.subprocess, "run", lambda *a, **k: Res())

    result = dul.lookup_upc(TARGET_UPC)

    assert "No infringement claims found" not in result
    assert "couldn't confirm" in result and "token is expired" in result


def test_tracker_hit_answers_even_when_the_inbox_has_nothing(monkeypatch):
    monkeypatch.setattr(dul, "_triage_search", lambda q, *, max_results: [])
    monkeypatch.setattr(dul, "_tracker_lookup", lambda upc: [
        {"region": "BR", "status": "🔍 Investigating", "date": "2026-10-10 00:54", "title": "Some Title",
         "claimant": "Spotify Content Protection", "source": "Spotify", "dsp": "Spotify"}])

    result = dul.lookup_upc(TARGET_UPC)

    assert "No infringement" not in result
    assert "A claim exists for this UPC" in result
    assert "BR · status: 🔍 Investigating" in result and "Some Title" in result


def test_clean_negative_says_what_was_checked(monkeypatch):
    monkeypatch.setattr(dul, "_triage_search", lambda q, *, max_results: [])
    result = dul.lookup_upc(TARGET_UPC)
    assert result.startswith(f"No infringement claims found for UPC {TARGET_UPC}")
    assert "trackers" in result


def test_fallback_sweep_runs_when_exact_hits_do_not_verify(monkeypatch):
    calls = []

    def triage(query, *, max_results):
        calls.append(query)
        if query == TARGET_UPC:
            return [{"message_id": "loose", "subject": "unrelated", "date": ""}]      # loose hit, no UPC inside
        if query == dul.FALLBACK_CLAIM_QUERIES[0]:
            return [{"message_id": "real", "subject": "Possibly Infringing", "date": ""}]
        return []

    bodies = {"loose": "nothing here", "real": f"Title: X\nUPC: {TARGET_UPC}\nClaimant Name: A\nEmail: a@b.com\n"}
    monkeypatch.setattr(dul, "_triage_search", triage)
    monkeypatch.setattr(dul.ra, "fetch_email", lambda mid: (bodies[mid], {"date_formatted": "2026-10-01"}))

    result = dul.lookup_upc(TARGET_UPC)

    assert "Found 1 matching claim email(s)." in result
    assert dul.FALLBACK_CLAIM_QUERIES[0] in calls


def test_tracker_lookup_matches_across_regions_and_ignores_stripped_leading_zeros(monkeypatch):
    header = ["UPC", "Title", "Status", "Date Received", "Claimant", "Email Source", "DSP"]
    sheets = {
        "BR": [header, ["009392835979", "A", "", "2026-10-10", "Spotify CP", "Spotify", "Spotify"]],
        "US": [header, ["5063962570191", "B", "✅ Resolved", "2026-09-01", "X", "AudioSalad", "YouTube"]],
        "SPLA": [header],
    }
    monkeypatch.undo()   # use the real _tracker_lookup for this test
    monkeypatch.setattr(hc, "read_sheet_values", lambda region=None: sheets[region])
    # a user pasting the UPC from Excel often loses the leading zeros
    hits = dul._tracker_lookup("9392835979") + dul._tracker_lookup("5063962570191")
    assert [(h["region"], h["title"]) for h in hits] == [("BR", "A"), ("US", "B")]


def test_unreadable_tracker_is_reported(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(dul.lark_auth, "ensure_jwt_fresh", lambda *a, **k: True)
    monkeypatch.setattr(dul, "_triage_search", lambda q, *, max_results: [])
    monkeypatch.setattr(hc, "read_sheet_values", lambda region=None: [])
    result = dul.lookup_upc(TARGET_UPC)
    assert "No infringement claims found" not in result and "tracker unreadable" in result
