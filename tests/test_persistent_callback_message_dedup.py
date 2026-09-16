import json
from types import SimpleNamespace

from copyright_alert import persistent_callback as pc


def _message_event(message_id):
    content = json.dumps({"text": "5063962570191"})
    return SimpleNamespace(
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_type="user",
                sender_id=SimpleNamespace(open_id="ou_test", user_id="u_test"),
            ),
            message=SimpleNamespace(
                message_type="text",
                chat_id="oc_test",
                chat_type="p2p",
                message_id=message_id,
                content=content,
            ),
        )
    )


def test_message_receive_ignores_duplicate_message_id(monkeypatch):
    pc.MESSAGE_LOCKS.clear()
    replies = []
    workers = []

    monkeypatch.setattr(pc, "reply_text", lambda message_id, text: replies.append((message_id, text)))

    class ImmediateThread:
        def __init__(self, target, args=(), daemon=None, kwargs=None):
            self.target = target
            self.args = args
            self.kwargs = kwargs or {}
            workers.append(self)

        def start(self):
            return None

    monkeypatch.setattr(pc.threading, "Thread", ImmediateThread)

    event = _message_event("om_duplicate")
    pc.handle_message_receive(event)
    pc.handle_message_receive(event)

    assert len(replies) == 1
    assert len(workers) == 1
