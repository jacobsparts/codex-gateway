"""Minimal OpenAI Responses-compatible server over the Codex transport."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from codex_transport import codex as transport

DEFAULT_HOST = os.environ.get("CODEX_GATEWAY_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("CODEX_GATEWAY_PORT", "8932"))
# Leave unset to accept any (including absent) token; set it to require an exact match.
GATEWAY_TOKEN = os.environ.get("CODEX_GATEWAY_TOKEN")
RESET_CREDITS_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"
CONSUME_RESET_URL = RESET_CREDITS_URL + "/consume"
QUOTA_MAX_AGE = 5 * 60
RESET_MAX_AGE = 60 * 60


def _error(message: str, code: str = "invalid_request_error"):
    return {"error": {"message": message, "type": code, "code": code}}

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_body(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return None
        try:
            return json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            return None

    def _authorized(self) -> bool:
        if GATEWAY_TOKEN is None:
            return True
        auth = self.headers.get("Authorization", "")
        if not auth.lower().startswith("bearer "):
            self._send(401, _error("missing Bearer token in Authorization header", "auth_error"))
            return False
        if auth[7:].strip() != GATEWAY_TOKEN:
            self._send(401, _error("invalid Bearer token", "auth_error"))
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/health", "/healthz"):
            self._send(200, {"status": "ok", "models": 1})
            return
        if parsed.path == "/v1/usage":
            if not self._authorized():
                return
            query = urllib.parse.parse_qs(parsed.query)
            try:
                snapshots = _usage(
                    transport.CodexAuth(),
                    account=query.get("account", [None])[0],
                    include_resets=query.get("resets") == ["1"],
                )
            except transport.CodexError as exc:
                self._send(502, _error(f"codex transport failed: {exc}", "transport_error"))
                return
            self._send(200, {"object": "list", "data": snapshots})
            return
        if parsed.path == "/v1/models":
            if not self._authorized():
                return
            now = int(time.time())
            data = [{"id": transport.MODEL, "object": "model", "created": now, "owned_by": "codex"}]
            self._send(200, {"object": "list", "data": data})
            return
        self._send(404, _error(f"unknown path {self.path}", "not_found"))

    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        if self.path not in {"/v1/responses", "/v1/usage/reset"}:
            self._send(404, _error(f"unknown path {self.path}", "not_found"))
            return
        if not self._authorized():
            return
        if self.path == "/v1/usage/reset":
            body = self._read_body()
            if not isinstance(body, dict) or not isinstance(body.get("account"), str):
                self._send(400, _error("account is required"))
                return
            try:
                snapshots = _consume_reset(transport.CodexAuth(), body["account"])
            except transport.CodexError as exc:
                self._send(502, _error(f"codex transport failed: {exc}", "transport_error"))
                return
            self._send(200, {"object": "list", "data": snapshots})
            return
        body = self._read_body()
        if body is None:
            self._send(400, _error("body must be a JSON object"))
            return
        if not isinstance(body, dict):
            self._send(400, _error("body must be a JSON object"))
            return
        if not body.get("model"):
            body = dict(body, model=transport.MODEL)

        auth = transport.CodexAuth()
        try:
            if body.get("stream"):
                self._stream_response(body, auth)
                return
            response = transport.responses(body, auth=auth)
        except transport.CodexStallError as exc:
            self._send(504, _error(f"codex stream stalled: {exc}", "transport_error"))
            return
        except transport.CodexError as exc:
            self._send(502, _error(f"codex transport failed: {exc}", "transport_error"))
            return
        self._send(200, response)

    def _stream_response(self, body: dict, auth: transport.CodexAuth) -> None:
        """Forward the upstream SSE stream."""
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            def write(event: dict) -> None:
                self.wfile.write(f"event: {event.get('type')}\n".encode())
                self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                self.wfile.flush()

            transport.responses(body, auth=auth, on_event=write)
        except transport.CodexStallError as exc:
            write({"type": "error", "message": f"codex stream stalled: {exc}"})
        except transport.CodexError as exc:
            write({"type": "error", "message": f"codex transport failed: {exc}"})
        self.close_connection = True

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[codex-gateway] %s - %s\n" % (self.address_string(), fmt % args))

def _account_indices(root: dict, account: str | None) -> list[int]:
    if account is None:
        return list(range(len(root["credentials"])))
    for index, credential in enumerate(root["credentials"]):
        if account in {credential.get("account"), f"cred-{index}"}:
            return [index]
    raise transport.CodexError(f"Unknown Codex account: {account}")


def _reset_request(auth: transport.CodexAuth, url: str, data: bytes | None = None) -> dict:
    headers = {
        "Authorization": "Bearer " + auth.access_token,
        "Accept": "application/json",
        "OpenAI-Beta": "codex-1",
        "Originator": "Codex Desktop",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    if auth.account_id:
        headers["ChatGPT-Account-ID"] = auth.account_id
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise transport.CodexError(f"Reset request failed: HTTP {exc.code}: {detail}") from exc
    except (OSError, ValueError) as exc:
        raise transport.CodexError(f"Reset request failed: {exc}") from exc


def _refresh_resets(auth: transport.CodexAuth, credential: dict) -> None:
    payload = _reset_request(auth, RESET_CREDITS_URL)
    credential["reset_credits"] = {
        "fetched_at": int(time.time()),
        "available_count": payload.get("available_count", 0),
        "credits": payload.get("credits", []),
    }


def _usage(
    auth: transport.CodexAuth,
    account: str | None = None,
    include_resets: bool = False,
    force: bool = False,
) -> list[dict]:
    with auth._lock(transport.fcntl.LOCK_EX):
        root = auth._load_unlocked()
        indices = _account_indices(root, account)
        now = time.time()
        snapshots = []
        for index in indices:
            auth._select_loaded(root, index)
            credential = root["credentials"][index]
            quota = credential.get("rate_limits")
            quota_fetched = quota.get("fetched_at", 0) if isinstance(quota, dict) else 0
            if force or now - quota_fetched > QUOTA_MAX_AGE:
                auth._usage_request_unlocked(index)
            resets = credential.get("reset_credits")
            reset_fetched = resets.get("fetched_at", 0) if isinstance(resets, dict) else 0
            if include_resets and (force or now - reset_fetched > RESET_MAX_AGE):
                if transport._credential_needs_refresh(credential):
                    auth._refresh_credential_unlocked(index)
                _refresh_resets(auth, credential)
            snapshots.append({
                "account": credential.get("account") or f"cred-{index}",
                "email": credential.get("email"),
                "rate_limits": credential.get("rate_limits"),
                "reset_credits": credential.get("reset_credits"),
            })
        auth._save_unlocked()
        return snapshots


def _consume_reset(auth: transport.CodexAuth, account: str) -> list[dict]:
    with auth._lock(transport.fcntl.LOCK_EX):
        root = auth._load_unlocked()
        index = _account_indices(root, account)[0]
        auth._select_loaded(root, index)
        if transport._credential_needs_refresh(auth.data):
            auth._refresh_credential_unlocked(index)
        body = json.dumps({"redeem_request_id": str(uuid.uuid4())}).encode()
        _reset_request(auth, CONSUME_RESET_URL, body)
        auth._save_unlocked()
    return _usage(auth, account=account, include_resets=True, force=True)

def main() -> None:
    server = ThreadingHTTPServer((DEFAULT_HOST, DEFAULT_PORT), Handler)
    print(
        "codex-gateway listening on http://%s:%d" % (DEFAULT_HOST, DEFAULT_PORT),
        flush=True,
    )
    server.serve_forever()

if __name__ == "__main__":
    main()
