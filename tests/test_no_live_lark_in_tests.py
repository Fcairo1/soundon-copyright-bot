import subprocess

import pytest

from copyright_alert import bot_runtime
from copyright_alert import rights_confirmation_notice as rc


def test_live_lark_api_is_blocked():
    with pytest.raises(RuntimeError, match="blocked in tests"):
        bot_runtime._post_api("/im/v1/messages?receive_id_type=chat_id", {"receive_id": "x"})


def test_live_lark_cli_is_blocked():
    with pytest.raises(RuntimeError, match="blocked in tests"):
        subprocess.run(["lark-cli", "mail", "+triage"], capture_output=True)


def test_an_unstubbed_ops_card_never_leaves_the_machine():
    card = rc.build_draft_ready_card(title="T", upc="999", ref_id="ref:1", region="BR", outcome_label="We have rights")
    with pytest.raises(Exception):      # _send_to_ops tries every route; all are blocked
        result = rc._send_to_ops("BR", card)
        # if it swallowed the errors it must not have reported a delivery
        assert not (result or {}).get("ok")
        raise AssertionError("not delivered")
