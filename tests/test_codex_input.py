from unittest.mock import patch

import pytest

from codex_transport import codex as transport


def _capture():
    captured = {}

    def fake_request(auth, body, timeouts, on_event=None):
        captured["body"] = body
        return {"output": []}

    return captured, fake_request


def test_string_input_is_normalized_to_user_message():
    captured, fake_request = _capture()
    with patch.object(transport, "_request", fake_request):
        transport.responses({"model": "gpt-6.1-sol", "input": "Reply with exactly: pong"})

    assert captured["body"]["input"] == [{"role": "user", "content": "Reply with exactly: pong"}]


def test_list_input_passes_through():
    captured, fake_request = _capture()
    items = [{"role": "user", "content": "hi"}]
    with patch.object(transport, "_request", fake_request):
        transport.responses({"model": "gpt-6.1-sol", "input": items})

    assert captured["body"]["input"] == items


@pytest.mark.parametrize("value", [[], "", None])
def test_empty_input_still_rejected(value):
    with pytest.raises(ValueError):
        transport.responses({"model": "gpt-6.1-sol", "input": value})
