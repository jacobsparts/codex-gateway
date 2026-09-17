import io
import json
import pytest
from unittest.mock import MagicMock, patch

import server
from codex_transport import codex as transport

def test_is_rate_limit_error():
    assert server._is_rate_limit_error(transport.CodexRateLimitError("Quota reached"))
    assert server._is_rate_limit_error(transport.CodexError("No Codex credential has positive quota"))
    assert server._is_rate_limit_error(transport.CodexError("Codex request failed: HTTP 429: Too Many Requests"))
    assert not server._is_rate_limit_error(transport.CodexError("Malformed Codex JSON response"))

def test_post_responses_rate_limit_non_streaming():
    handler = object.__new__(server.Handler)
    handler.path = "/v1/responses"
    handler.headers = {}
    sent = {}

    def fake_send(status, payload):
        sent["status"] = status
        sent["payload"] = payload

    handler._send = fake_send
    handler._authorized = lambda: True
    handler._read_body = lambda: {"model": "gpt-5.6-luna", "input": [{"role": "user", "content": "hi"}]}

    with patch("codex_transport.codex.CodexAuth") as mock_auth, patch(
        "codex_transport.codex.responses",
        side_effect=transport.CodexRateLimitError("No Codex credential has positive quota"),
    ):
        handler.do_POST()

    assert sent["status"] == 429
    assert sent["payload"]["error"]["type"] == "rate_limit_exceeded"
    assert sent["payload"]["error"]["code"] == "rate_limit_exceeded"
    assert "positive quota" in sent["payload"]["error"]["message"]

def test_post_responses_rate_limit_streaming():
    handler = object.__new__(server.Handler)
    handler.path = "/v1/responses"
    handler.headers = {}
    sent = {}

    def fake_send(status, payload):
        sent["status"] = status
        sent["payload"] = payload

    handler._send = fake_send
    handler._authorized = lambda: True
    handler._read_body = lambda: {"model": "gpt-5.6-luna", "stream": True, "input": [{"role": "user", "content": "hi"}]}

    # Mock send_response to check if 200 headers were prematurely sent
    headers_called = []
    handler.send_response = lambda code: headers_called.append(code)
    handler.send_header = lambda *args: None
    handler.end_headers = lambda: None
    handler.wfile = io.BytesIO()

    with patch("codex_transport.codex.CodexAuth") as mock_auth, patch(
        "codex_transport.codex.responses",
        side_effect=transport.CodexRateLimitError("No Codex credential has positive quota"),
    ):
        handler.do_POST()

    assert sent["status"] == 429
    assert sent["payload"]["error"]["type"] == "rate_limit_exceeded"
    assert "positive quota" in sent["payload"]["error"]["message"]
    # Verify 200 was never sent
    assert 200 not in headers_called
