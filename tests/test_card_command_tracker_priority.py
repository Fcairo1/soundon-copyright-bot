from copyright_alert import persistent_callback as pc

TARGET_UPC = "5064079652602"


def test_card_command_prefers_metadata_notice_over_stale_claims_row(monkeypatch):
    # Real bug reported 2026-09-29: this exact UPC has BOTH a stale row in
    # the main claims tracker (misclassified before specialized routing
    # existed) and a real metadata-notice record — /card kept resending the
    # generic claim card because the claims tracker was checked first.
    monkeypatch.setattr(pc, "REGION_CONFIGS", {"BR": {}})
    monkeypatch.setattr(pc.metadata_notice, "find_notice_by_upc", lambda upc: {
        "fields": {"upc": upc, "title": "T"}, "region": "BR", "resolved": False,
    })
    monkeypatch.setattr(pc.rights_confirmation_notice, "find_notice_by_upc",
                         lambda upc: (_ for _ in ()).throw(AssertionError("must not even check rights-confirmation")))
    monkeypatch.setattr(pc, "_read_tracker_fresh",
                         lambda region: (_ for _ in ()).throw(AssertionError("must not read the claims tracker at all")))
    posted = {}
    monkeypatch.setattr(pc, "_post_card_to_destination", lambda card, **k: posted.setdefault("card", card) or {"ok": True})
    replies = []
    monkeypatch.setattr(pc, "reply_post", lambda mid, title, lines: replies.append((title, lines)))

    pc._handle_card_command(f"/card {TARGET_UPC}", "msg1")

    assert "metadata" in replies[0][1][0].lower()
    assert posted["card"]["header"]["title"]["tag"] == "plain_text"


def test_card_command_prefers_rights_confirmation_over_stale_claims_row(monkeypatch):
    monkeypatch.setattr(pc, "REGION_CONFIGS", {"BR": {}})
    monkeypatch.setattr(pc.metadata_notice, "find_notice_by_upc", lambda upc: {})
    monkeypatch.setattr(pc.rights_confirmation_notice, "find_notice_by_upc", lambda upc: {
        "fields": {"upc": upc, "title": "T", "ref_id": "ref:1"}, "region": "BR",
    })
    monkeypatch.setattr(pc, "_read_tracker_fresh",
                         lambda region: (_ for _ in ()).throw(AssertionError("must not read the claims tracker at all")))
    posted = {}
    monkeypatch.setattr(pc, "_post_card_to_destination", lambda card, **k: posted.setdefault("card", card) or {"ok": True})
    replies = []
    monkeypatch.setattr(pc, "reply_post", lambda mid, title, lines: replies.append((title, lines)))

    pc._handle_card_command(f"/card {TARGET_UPC}", "msg1")

    assert "rights-confirmation" in replies[0][1][0].lower()


def test_card_command_falls_back_to_claims_tracker_when_no_specialized_match(monkeypatch):
    monkeypatch.setattr(pc, "REGION_CONFIGS", {"BR": {}})
    monkeypatch.setattr(pc.metadata_notice, "find_notice_by_upc", lambda upc: {})
    monkeypatch.setattr(pc.rights_confirmation_notice, "find_notice_by_upc", lambda upc: {})
    called = []
    monkeypatch.setattr(pc, "_read_tracker_fresh", lambda region: called.append(region) or ([], []))
    replies = []
    monkeypatch.setattr(pc, "reply_post", lambda mid, title, lines: replies.append((title, lines)))

    pc._handle_card_command(f"/card {TARGET_UPC}", "msg1")

    assert called == ["BR"]
    assert "not found in any tracker" in replies[0][1][0]
