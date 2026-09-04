"""Minimal OpenAI Responses-compatible server over the Codex transport."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from codex_transport import codex as transport

DEFAULT_HOST = os.environ.get("CODEX_GATEWAY_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("CODEX_GATEWAY_PORT", "8932"))
# Leave unset to accept any (including absent) token; set it to require an exact match.
GATEWAY_TOKEN = os.environ.get("CODEX_GATEWAY_TOKEN")

_auth = transport.CodexAuth()

def _error(message: str, code: str = "invalid_request_error"):
    return {"error": {"message": message, "type": code, "code": code}}

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, status: int, payload: dict, extra: dict[str, str] | None = None) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for name, value in (extra or {}).items():
            self.send_header(name, value)
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
        if self.path in ("/health", "/healthz"):
            self._send(200, {"status": "ok", "models": len(_list_models_cached())})
            return
        if self.path == "/v1/usage":
            if not self._authorized():
                return
            try:
                snapshots = transport.usage(_auth)
            except Exception as exc:
                self._send(502, _error(f"codex transport failed: {exc}", "transport_error"))
                return
            self._send(200, {"object": "list", "data": snapshots})
            return
        if self.path == "/v1/models":
            if not self._authorized():
                return
            try:
                names = _list_models_cached()
            except Exception as exc:
                self._send(502, _error(f"codex transport failed: {exc}", "transport_error"))
                return
            now = int(time.time())
            data = [
                {"id": name, "object": "model", "created": now, "owned_by": "codex"}
                for name in names
            ]
            self._send(200, {"object": "list", "data": data})
            return
        self._send(404, _error(f"unknown path {self.path}", "not_found"))

    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        if self.path != "/v1/responses":
            self._send(404, _error(f"unknown path {self.path}", "not_found"))
            return
        if not self._authorized():
            return
        body = self._read_body()
        if body is None:
            self._send(400, _error("body must be a JSON object"))
            return
        if not isinstance(body, dict):
            self._send(400, _error("body must be a JSON object"))
            return
        if not body.get("model"):
            body = dict(body, model=transport.DEFAULT_MODEL)

        meta: dict = {}
        try:
            if body.get("stream"):
                self._stream_response(body, meta)
                return
            response = transport.responses(body, auth=_auth, meta=meta)
        except transport.CredentialInvalidError as exc:
            self._send(502, _error(f"codex credential rejected: {exc}", "transport_error"))
            return
        except transport.RateLimitedError as exc:
            extra = meta.get("rate_limits") or {}
            self._send(429, rate_limit_error(exc), extra)
            return
        except transport.UpstreamBadRequestError as exc:
            self._send(400, _error(str(exc)))
            return
        except transport.CodexStallError as exc:
            self._send(504, _error(f"codex stream stalled: {exc}", "transport_error"))
            return
        except transport.CodexError as exc:
            self._send(502, _error(f"codex transport failed: {exc}", "transport_error"))
            return
        except Exception as exc:  # noqa: BLE001 - transport raises plain exceptions
            self._send(502, _error(f"codex transport failed: {exc}", "transport_error"))
            return
        extra = dict(meta.get("rate_limits") or {})
        if meta.get("email"):
            extra["x-codex-account"] = meta["email"]
        self._send(200, response, extra)

    def _stream_response(self, body: dict, meta: dict) -> None:
        """Forward the upstream SSE stream, repopulating `response.output` at the end."""
        builder = transport._ResponseBuilder()
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

            for event in transport.iter_events(body, auth=_auth, meta=meta):
                builder.add(event)
                etype = event.get("type")
                if etype in ("response.completed", "response.incomplete", "response.failed"):
                    response = event.get("response")
                    if isinstance(response, dict) and not response.get("output"):
                        response["output"] = builder.output()
                    usage = response.get("usage") if isinstance(response, dict) else None
                    if isinstance(usage, dict) and "attribution" in usage:
                        response["usage"] = {k: v for k, v in usage.items() if k != "attribution"}
                write(event)
        except transport.CredentialInvalidError as exc:
            write({"type": "error", "message": f"codex credential rejected: {exc}"})
        except transport.RateLimitedError as exc:
            write({"type": "error", "message": f"codex rate limited: {exc}"})
        except transport.CodexStallError as exc:
            write({"type": "error", "message": f"codex stream stalled: {exc}"})
        except transport.CodexError as exc:
            write({"type": "error", "message": f"codex transport failed: {exc}"})
        except (urllib.error.URLError, OSError) as exc:
            write({"type": "error", "message": f"codex stream failed: {exc}"})
        self.close_connection = True

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[codex-gateway] %s - %s\n" % (self.address_string(), fmt % args))

def rate_limit_error(exc: Exception) -> dict:
    return {
        "error": {
            "message": str(exc),
            "type": "rate_limit_error",
            "code": "rate_limit_exceeded",
        }
    }

_MODEL_CACHE: tuple[float, list[str]] | None = None

def _list_models_cached(ttl: float = 300.0) -> list[str]:
    """Model list is a network call; cache it briefly so /health stays cheap."""
    global _MODEL_CACHE
    now = time.time()
    if _MODEL_CACHE and now - _MODEL_CACHE[0] < ttl:
        return _MODEL_CACHE[1]
    names = transport.list_models(_auth)
    _MODEL_CACHE = (now, names)
    return names

def main() -> None:
    server = ThreadingHTTPServer((DEFAULT_HOST, DEFAULT_PORT), Handler)
    print(
        "codex-gateway listening on http://%s:%d" % (DEFAULT_HOST, DEFAULT_PORT),
        flush=True,
    )
    server.serve_forever()

if __name__ == "__main__":
    main()
