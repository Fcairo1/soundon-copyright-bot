from copyright_alert import persistent_callback as pc


TARGET_UPC = "5063962570191"


def test_action_card_trigger_phrases_route_to_action_card_intent():
    examples = [
        f"resend an action card for UPC {TARGET_UPC}",
        f"please post the claim card for {TARGET_UPC}",
        f"resend card {TARGET_UPC}",
        f"new card for {TARGET_UPC}",
        f"generate card {TARGET_UPC}",
    ]

    for text in examples:
        assert pc._extract_upcs(text) == [TARGET_UPC]
        assert pc._dm_upc_intent(text) == "action_card"


def test_inbox_search_trigger_phrases_route_to_inbox_search_intent():
    examples = [
        f"check claims for UPC {TARGET_UPC}",
        f"find claims {TARGET_UPC}",
        f"scan inbox for {TARGET_UPC}",
        f"search claims {TARGET_UPC}",
        f"show claims {TARGET_UPC}",
        TARGET_UPC,
    ]

    for text in examples:
        assert pc._extract_upcs(text) == [TARGET_UPC]
        assert pc._dm_upc_intent(text) == "inbox_search"
