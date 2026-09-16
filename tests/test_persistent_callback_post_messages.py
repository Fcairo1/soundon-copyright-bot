import json

from copyright_alert import persistent_callback as pc


def test_extract_post_text_flattens_text_tags_and_ignores_mentions_and_links():
    content = json.dumps(
        {
            "title": "",
            "content": [
                [
                    {"tag": "at", "user_id": "ou_bot", "user_name": "Copyright Bot"},
                    {"tag": "text", "text": "Generate a new action card for UPC "},
                    {"tag": "a", "text": "ignored link text", "href": "https://example.com"},
                    {"tag": "text", "text": "5063965051437"},
                ]
            ],
        },
        ensure_ascii=False,
    )

    assert pc._extract_post_text(content) == "Generate a new action card for UPC 5063965051437"
    assert pc._extract_upcs(pc._extract_message_text("post", content)) == ["5063965051437"]
    assert pc._dm_upc_intent(pc._extract_message_text("post", content)) == "action_card"


def test_extract_post_text_returns_empty_for_mentions_links_only_or_bad_content():
    mentions_and_links_only = json.dumps(
        {
            "title": "",
            "content": [[{"tag": "at", "user_name": "Someone"}, {"tag": "a", "text": "link", "href": "https://example.com"}]],
        }
    )

    assert pc._extract_post_text(mentions_and_links_only) == ""
    assert pc._extract_post_text("not json") == ""
    assert pc._extract_message_text("image", mentions_and_links_only) == ""
