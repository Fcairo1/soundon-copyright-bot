from copyright_alert import dm_upc_lookup as dul


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
