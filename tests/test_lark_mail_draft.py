from copyright_alert import lark_auth, lark_mail_draft, oauth_setup


def test_oauth_scopes_include_mail_readonly():
    assert "mail:user_mailbox.message:readonly" in oauth_setup.SCOPE_KEYS
    assert "mail:user_mailbox.message:readonly" in lark_auth._OAUTH_REQUIRED_SCOPE_KEYS


def test_create_reply_draft_falls_back_to_standalone_when_original_metadata_unavailable(monkeypatch):
    monkeypatch.setattr(lark_mail_draft, "refresh_user_access_token", lambda force=False: "token")
    monkeypatch.setattr(lark_mail_draft, "_find_original_message_id", lambda mailbox, upc="", ref_id="": "")
    monkeypatch.setattr(lark_mail_draft, "_fetch_original_message", lambda mailbox, message_id, headers=None: {})

    calls = []

    def fake_post(url, payload, headers=None, timeout=30):
        calls.append({"url": url, "payload": payload, "headers": headers})
        return {"data": {"draft_id": "draft_123", "draft_link": "https://mail.example/draft_123"}}

    monkeypatch.setattr(lark_mail_draft, "_post_json", fake_post)

    result = lark_mail_draft.create_reply_draft(
        mailbox="soundon-copyright@bytedance.com",
        thread_message_id="",
        to="claimant@example.com",
        subject="Spotify infringement claim response - agree",
        body_html="<p>Thanks</p>",
        upc="5063965419770",
        ref_id="ref:_00D0992XChO._500QvfyHui:ref",
    )

    assert result["draft_id"] == "draft_123"
    assert result["threaded"] is False
    assert result["warning"] == "original message metadata was unavailable"
    assert calls and "message_id" not in calls[0]["payload"]
    assert "raw" in calls[0]["payload"]


def test_create_reply_draft_uses_native_lark_reply_when_original_exists(monkeypatch):
    monkeypatch.setattr(lark_mail_draft, "refresh_user_access_token", lambda force=False: "token")
    monkeypatch.setattr(lark_mail_draft, "_find_original_message_id", lambda mailbox, upc="", ref_id="": "msg_123")

    fetch_calls = []

    def fake_fetch(mailbox, message_id, headers=None):
        fetch_calls.append(message_id)
        if message_id == "draft_456":
            return {"thread_id": "thread_123"}
        return {
            "smtp_message_id": "<original@example.com>",
            "thread_id": "thread_123",
            "head_from": {"mail_address": "claimant@example.com", "name": "Claimant"},
            "subject": "Original subject",
            "body_plain_text": "body ref:_00D0992XChO._500QvfyHui:ref",
            "references": ["<older@example.com>"],
        }

    cli_calls = []

    def fake_lark_cli(args, timeout=90, cwd=None):
        cli_calls.append(args)
        assert args[0] == "+reply"
        assert "--message-id" in args
        assert args[args.index("--message-id") + 1] == "msg_123"
        return {"draft_id": "draft_456", "reference": "https://mail.example/draft_456"}

    monkeypatch.setattr(lark_mail_draft, "_fetch_original_message", fake_fetch)
    monkeypatch.setattr(lark_mail_draft, "_lark_cli_json", fake_lark_cli)

    result = lark_mail_draft.create_reply_draft(
        mailbox="soundon-copyright@bytedance.com",
        thread_message_id="legacy_or_missing",
        to="fallback@example.com",
        subject="Fallback subject",
        body_html="<p>Thanks</p>",
        upc="5063965419770",
        ref_id="ref:_00D0992XChO._500QvfyHui:ref",
    )

    assert result["draft_id"] == "draft_456"
    assert result["threaded"] is True
    assert result["thread_id"] == "thread_123"
    assert fetch_calls == ["msg_123", "draft_456"]
    assert cli_calls



def test_fetch_original_message_merges_oauth_ids_with_cli_subject_and_sender(monkeypatch):
    def fake_get(url, headers=None):
        return {
            "data": {
                "message": {
                    "message_id": "msg_123",
                    "smtp_message_id": "original@example.com",
                    "thread_id": "thread_123",
                }
            }
        }

    def fake_lark_cli(args, timeout=90, cwd=None):
        return {
            "data": {
                "message_id": "msg_123",
                "subject": "Original subject",
                "head_from": {"mail_address": "claimant@example.com"},
                "body_plain_text": "original body",
            }
        }

    monkeypatch.setattr(lark_mail_draft, "_get_json", fake_get)
    monkeypatch.setattr(lark_mail_draft, "_lark_cli_json", fake_lark_cli)

    original = lark_mail_draft._fetch_original_message(
        "soundon-copyright@bytedance.com",
        "msg_123",
        headers={"Authorization": "Bearer token"},
    )

    assert original["smtp_message_id"] == "original@example.com"
    assert original["thread_id"] == "thread_123"
    assert original["subject"] == "Original subject"
    assert original["head_from"]["mail_address"] == "claimant@example.com"
    assert original["body_plain_text"] == "original body"


def test_find_original_message_id_skips_reply_hits(monkeypatch):
    def fake_cli(args, timeout=90):
        assert args[0] == "+triage"
        return {
            "messages": [
                {"message_id": "reply_msg", "subject": "Re: Spotify infringement claim response - UPC 5063965051437"},
                {"message_id": "original_msg", "subject": "Spotify Notice of Possibly Infringing Content"},
            ]
        }

    def fake_fetch(mailbox, message_id, headers=None):
        if message_id == "reply_msg":
            return {
                "subject": "Re: Spotify infringement claim response - UPC 5063965051437",
                "head_from": {"mail_address": "soundon-copyright@bytedance.com"},
                "smtp_message_id": "<reply@example.com>",
            }
        return {
            "subject": "Spotify Notice of Possibly Infringing Content",
            "head_from": {"mail_address": "infringement-claim-response@spotify.com"},
            "smtp_message_id": "<original@example.com>",
        }

    monkeypatch.setattr(lark_mail_draft, "_lark_cli_json", fake_cli)
    monkeypatch.setattr(lark_mail_draft, "_fetch_original_message", fake_fetch)

    assert lark_mail_draft._find_original_message_id(
        "soundon-copyright@bytedance.com",
        upc="5063965051437",
        ref_id="ref:_00D0992XChO._500QvibkYt:ref",
    ) == "original_msg"
