import io
import urllib.error
import json
import pytest
from unittest.mock import MagicMock, patch

import server
from codex_transport import codex as transport

@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("detail", [b'{"error":{"message":"actual upstream error"}}', b''])
def test_upstream_error_has_no_wrapper(stream, detail):
    handler = object.__new__(server.Handler)
    handler.path = "/v1/responses"
    handler.headers = {}
    handler._authorized = lambda: True
    handler._read_body = lambda: {"model": "gpt-6.1-sol", "input": "hi", "stream": stream}
    handler._send = MagicMock()
    error = urllib.error.HTTPError("https://example.com", 400, "Bad Request", {}, io.BytesIO(detail))
    with patch.object(transport, "CodexAuth") as auth, patch.object(
        transport.urllib.request, "urlopen", side_effect=error
    ):
        auth.return_value.access_token = "test"
        auth.return_value.account_id = ""
        handler.do_POST()
    expected = detail.decode() or "HTTP 400 Bad Request"
    handler._send.assert_called_once_with(502, server._error(expected, "transport_error"))


@pytest.fixture(autouse=True)
def isolate_version_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "VERSION_CACHE_FILE", str(tmp_path / "cache" / "client-version"))


@pytest.mark.parametrize("current, offered, expected", [
    ("0.171.0", "0.160.0", "0.171.0"),
    ("0.171.0", "0.171.0", "0.171.0"),
    ("0.99.0", "0.100.0", "0.100.0"),
])
def test_version_refresh_only_moves_forward(monkeypatch, current, offered, expected):
    monkeypatch.delenv("CODEX_VERSION", raising=False)
    monkeypatch.setattr(server, "VERSION_LAST_CHECK", None)
    monkeypatch.setattr(transport, "CLIENT_VERSION", current)
    with patch.object(server.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps({"version": offered}).encode())):
        server.refresh_client_version()
    assert transport.CLIENT_VERSION == expected


def test_version_survives_restart_and_offline_check(monkeypatch):
    monkeypatch.delenv("CODEX_VERSION", raising=False)
    monkeypatch.setattr(server, "VERSION_LAST_CHECK", None)
    monkeypatch.setattr(transport, "CLIENT_VERSION", "0.171.0")
    server._save_client_version("0.200.0")
    server._load_client_version()
    assert transport.CLIENT_VERSION == "0.200.0"
    with patch.object(server.urllib.request, "urlopen", side_effect=OSError("offline")):
        server.refresh_client_version()
    assert transport.CLIENT_VERSION == "0.200.0"


@pytest.mark.parametrize("cached", ["0.160.0", "invalid"])
def test_cache_cannot_lower_version(monkeypatch, cached):
    monkeypatch.delenv("CODEX_VERSION", raising=False)
    monkeypatch.setattr(transport, "CLIENT_VERSION", "0.171.0")
    server._save_client_version(cached)
    server._load_client_version()
    assert transport.CLIENT_VERSION == "0.171.0"


def test_pin_ignores_cached_version(monkeypatch):
    monkeypatch.setenv("CODEX_VERSION", "0.160.0")
    monkeypatch.setattr(transport, "CLIENT_VERSION", "0.160.0")
    server._save_client_version("0.200.0")
    server._load_client_version()
    assert transport.CLIENT_VERSION == "0.160.0"


def test_client_version_refresh_daily(monkeypatch):
    monkeypatch.delenv("CODEX_VERSION", raising=False)
    monkeypatch.setattr(server, "VERSION_LAST_CHECK", None)
    monkeypatch.setattr(transport, "CLIENT_VERSION", "0.160.0")
    with patch.object(server.time, "monotonic", side_effect=[0, 86399, 86400]), patch.object(
        server.urllib.request, "urlopen", side_effect=lambda *a, **kw: io.BytesIO(b'{"version":"0.171.0"}')
    ) as fetch:
        server.refresh_client_version()
        assert transport.CLIENT_VERSION == "0.171.0"
        assert transport._headers(MagicMock(access_token="test", account_id=""))["Version"] == "0.171.0"
        server.refresh_client_version()
        assert fetch.call_count == 1
        server.refresh_client_version()
        assert fetch.call_count == 2


def test_client_version_pin_skips_npm(monkeypatch):
    monkeypatch.setenv("CODEX_VERSION", "0.160.0")
    with patch.object(server.urllib.request, "urlopen") as fetch:
        server.refresh_client_version()
        fetch.assert_not_called()


@pytest.mark.parametrize("payload", [b'{}', b'[]', b'{"version":"bad\\r\\nheader"}', b'{"version":"0.172.0-beta.1"}', b'not json'])
def test_client_version_invalid_response_keeps_current(monkeypatch, payload):
    monkeypatch.delenv("CODEX_VERSION", raising=False)
    monkeypatch.setattr(server, "VERSION_LAST_CHECK", None)
    monkeypatch.setattr(transport, "CLIENT_VERSION", "0.171.0")
    with patch.object(server.urllib.request, "urlopen", return_value=io.BytesIO(payload)):
        server.refresh_client_version()
    assert transport.CLIENT_VERSION == "0.171.0"


def test_client_version_network_failure_keeps_current(monkeypatch):
    monkeypatch.delenv("CODEX_VERSION", raising=False)
    monkeypatch.setattr(server, "VERSION_LAST_CHECK", None)
    monkeypatch.setattr(transport, "CLIENT_VERSION", "0.171.0")
    with patch.object(server.urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")) as fetch:
        server.refresh_client_version()
        server.refresh_client_version()
        assert fetch.call_count == 1
    assert transport.CLIENT_VERSION == "0.171.0"


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

def test_post_responses_empty_input_returns_400():
    handler = object.__new__(server.Handler)
    handler.path = "/v1/responses"
    handler.headers = {}
    sent = {}

    def fake_send(status, payload):
        sent["status"] = status
        sent["payload"] = payload

    handler._send = fake_send
    handler._authorized = lambda: True
    handler._read_body = lambda: {"model": "gpt-6.1-sol", "input": []}

    with patch("codex_transport.codex.CodexAuth"):
        handler.do_POST()

    assert sent["status"] == 400
    assert "input" in sent["payload"]["error"]["message"]


def test_post_responses_unexpected_error_returns_500():
    handler = object.__new__(server.Handler)
    handler.path = "/v1/responses"
    handler.headers = {}
    sent = {}

    def fake_send(status, payload):
        sent["status"] = status
        sent["payload"] = payload

    handler._send = fake_send
    handler._authorized = lambda: True
    handler._read_body = lambda: {"model": "gpt-6.1-sol", "input": [{"role": "user", "content": "hi"}]}

    with patch("codex_transport.codex.CodexAuth"), patch(
        "codex_transport.codex.responses",
        side_effect=RuntimeError("boom"),
    ):
        handler.do_POST()

    assert sent["status"] == 500
    assert sent["payload"]["error"]["code"] == "internal_error"
