"""Minimal OpenAI Responses-compatible server over the Codex transport."""

from __future__ import annotations

from datetime import datetime
import json
import os
import sys
import threading
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
QUOTA_REFRESH_INTERVAL = 60 * 60
RESET_REFRESH_INTERVAL = 24 * 60 * 60
AUTO_RESET_BEFORE = 10 * 60
AUTO_RESET_MIN_QUOTA_RESET = 24 * 60 * 60
MAINTENANCE_INTERVAL = 60

MAINTENANCE_LOCK = threading.Lock()
REFRESH_TIMERS = {"quota": {}, "resets": {}}


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
                account = query.get("account", [None])[0]
                if query.get("refresh") == ["1"]:
                    refresh_quota(force=True, account=account)
                    refresh_reset_credits(force=True, account=account)
                snapshots = _usage(transport.CodexAuth(), account=account)
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
                snapshots = _consume_reset(body["account"])
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


def _backend_request(auth: transport.CodexAuth, url: str, data: bytes | None = None) -> dict:
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
        raise transport.CodexError(f"Codex backend request failed: HTTP {exc.code}: {detail}") from exc
    except (OSError, ValueError) as exc:
        raise transport.CodexError(f"Codex backend request failed: {exc}") from exc


def _auth_for_index(index: int) -> transport.CodexAuth:
    auth = transport.CodexAuth()
    auth._select_loaded(auth.root, index)
    if auth.needs_refresh():
        auth.refresh()
    return auth


def _persist_quota(index: int, snapshot: dict, reset_summary) -> None:
    auth = transport.CodexAuth()
    with auth._lock(transport.fcntl.LOCK_EX):
        root = auth._load_unlocked()
        credential = root["credentials"][index]
        credential["rate_limits"] = snapshot
        if isinstance(reset_summary, dict):
            resets = credential.get("reset_credits")
            if not isinstance(resets, dict):
                resets = {}
                credential["reset_credits"] = resets
            for name in ("available_count", "applicable_available_count"):
                if name in reset_summary:
                    resets[name] = reset_summary[name]
        auth._select_loaded(root, index)
        auth._save_unlocked()


def _persist_resets(index: int, payload: dict) -> None:
    auth = transport.CodexAuth()
    with auth._lock(transport.fcntl.LOCK_EX):
        root = auth._load_unlocked()
        credential = root["credentials"][index]
        previous = credential.get("reset_credits")
        resets = {
            "fetched_at": int(time.time()),
            "available_count": payload.get("available_count", 0),
            "credits": payload.get("credits", []),
        }
        if isinstance(previous, dict) and "applicable_available_count" in previous:
            resets["applicable_available_count"] = previous["applicable_available_count"]
        credential["reset_credits"] = resets
        auth._select_loaded(root, index)
        auth._save_unlocked()


def _refresh_quota_index(index: int) -> None:
    auth = _auth_for_index(index)
    payload = _backend_request(auth, transport.USAGE_URL)
    snapshot = transport._quota_snapshot_from_usage(payload)
    if not snapshot["limits"]:
        raise transport.CodexError("Quota refresh returned no usable rate limits")
    _persist_quota(index, snapshot, payload.get("rate_limit_reset_credits"))


def _refresh_reset_index(index: int) -> None:
    auth = _auth_for_index(index)
    _persist_resets(index, _backend_request(auth, RESET_CREDITS_URL))


def _refresh_indices(account: str | None) -> list[int]:
    auth = transport.CodexAuth()
    indices = _account_indices(auth.root, account)
    usable = [
        index
        for index in indices
        if auth.root["credentials"][index].get("invalid") is not True
    ]
    if account is not None and not usable:
        raise transport.CodexError(f"Codex account is marked invalid: {account}")
    return usable


def refresh_quota(force: bool = False, account: str | None = None) -> None:
    with MAINTENANCE_LOCK:
        auth = transport.CodexAuth()
        now = time.time()
        error = None
        for index in _refresh_indices(account):
            quota = auth.root["credentials"][index].get("rate_limits")
            fetched_at = quota.get("fetched_at", 0) if isinstance(quota, dict) else 0
            last_refresh = max(REFRESH_TIMERS["quota"].get(index, 0), fetched_at)
            if not force and now - last_refresh < QUOTA_REFRESH_INTERVAL:
                continue
            try:
                _refresh_quota_index(index)
            except transport.CodexError as exc:
                error = exc
                print(f"[codex-gateway] quota refresh failed for credential {index}: {exc}", file=sys.stderr)
                continue
            REFRESH_TIMERS["quota"][index] = time.time()
        if account is not None and error is not None:
            raise error


def _expiry_timestamp(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    return None


def _consume_reset_index(index: int) -> None:
    auth = _auth_for_index(index)
    body = json.dumps({"redeem_request_id": str(uuid.uuid4())}).encode()
    _backend_request(auth, CONSUME_RESET_URL, body)
    _refresh_quota_index(index)
    REFRESH_TIMERS["quota"][index] = time.time()
    _refresh_reset_index(index)
    REFRESH_TIMERS["resets"][index] = time.monotonic()


def _available_resets(credential: dict, now: float) -> list[float]:
    resets = credential.get("reset_credits")
    if not isinstance(resets, dict):
        return []
    expirations = []
    for credit in resets.get("credits", []):
        if not isinstance(credit, dict):
            continue
        expires_at = _expiry_timestamp(credit.get("expires_at"))
        if (
            credit.get("status") == "available"
            and credit.get("reset_type") == "codex_rate_limits"
            and expires_at is not None
            and expires_at > now
        ):
            expirations.append(expires_at)
    return expirations


def _longest_quota_window(credential: dict):
    quota = credential.get("rate_limits")
    limits = quota.get("limits") if isinstance(quota, dict) else None
    if not isinstance(limits, dict):
        return None
    windows = []
    for name, limit in limits.items():
        if not name.startswith("codex_") or not isinstance(limit, dict):
            continue
        duration = limit.get("limit_window_seconds")
        used_percent = limit.get("used_percent")
        if isinstance(duration, (int, float)) and isinstance(used_percent, (int, float)):
            windows.append((float(duration), 100.0 - float(used_percent)))
    return max(windows) if windows else None


def _depleted_pool_reset_candidate(
    root: dict,
    indices: list[int],
    now: float,
    require_applicable: bool = False,
) -> int | None:
    if not indices:
        return None
    credentials = root["credentials"]
    for index in indices:
        longest = _longest_quota_window(credentials[index])
        if longest is None or longest[1] > 5:
            return None

    candidates = []
    for index in indices:
        credential = credentials[index]
        resets = credential.get("reset_credits")
        if (
            require_applicable
            and (not isinstance(resets, dict) or resets.get("applicable_available_count", 0) < 1)
        ):
            continue
        effective = transport._effective_quota(credential)
        if effective is None or effective[0] <= now + AUTO_RESET_MIN_QUOTA_RESET:
            continue
        for expires_at in _available_resets(credential, now):
            candidates.append((expires_at, index))
    return min(candidates)[1] if candidates else None


def _consume_automatic_reset(indices: list[int]) -> None:
    auth = transport.CodexAuth()
    indices = [index for index in indices if auth.root["credentials"][index].get("invalid") is not True]
    now = time.time()
    expiring = sorted(
        (expires_at, index)
        for index in indices
        for expires_at in _available_resets(auth.root["credentials"][index], now)
        if expires_at <= now + AUTO_RESET_BEFORE
    )
    for _, index in expiring:
        try:
            _refresh_quota_index(index)
            REFRESH_TIMERS["quota"][index] = time.time()
            _refresh_reset_index(index)
            REFRESH_TIMERS["resets"][index] = time.monotonic()
        except transport.CodexError as exc:
            print(f"[codex-gateway] automatic reset refresh failed for credential {index}: {exc}", file=sys.stderr)
            continue
        credential = transport.CodexAuth().root["credentials"][index]
        resets = credential.get("reset_credits")
        if (
            isinstance(resets, dict)
            and resets.get("applicable_available_count", 0) > 0
            and any(
                expires_at <= time.time() + AUTO_RESET_BEFORE
                for expires_at in _available_resets(credential, time.time())
            )
        ):
            try:
                _consume_reset_index(index)
            except transport.CodexError as exc:
                print(f"[codex-gateway] automatic reset failed for credential {index}: {exc}", file=sys.stderr)
            return

    candidate = _depleted_pool_reset_candidate(auth.root, indices, now)
    if candidate is None:
        return

    quota_current = True
    for index in indices:
        try:
            _refresh_quota_index(index)
        except transport.CredentialInvalidError:
            continue
        except transport.CodexError as exc:
            quota_current = False
            print(f"[codex-gateway] automatic reset quota refresh failed for credential {index}: {exc}", file=sys.stderr)
            continue
        REFRESH_TIMERS["quota"][index] = time.time()
    if not quota_current:
        return

    auth = transport.CodexAuth()
    indices = [index for index in indices if auth.root["credentials"][index].get("invalid") is not True]
    candidate = _depleted_pool_reset_candidate(
        auth.root,
        indices,
        time.time(),
        require_applicable=True,
    )
    if candidate is None:
        return
    try:
        _refresh_reset_index(candidate)
    except transport.CodexError as exc:
        print(f"[codex-gateway] automatic reset credit refresh failed for credential {candidate}: {exc}", file=sys.stderr)
        return
    REFRESH_TIMERS["resets"][candidate] = time.monotonic()

    auth = transport.CodexAuth()
    indices = [index for index in indices if auth.root["credentials"][index].get("invalid") is not True]
    if _depleted_pool_reset_candidate(
        auth.root,
        indices,
        time.time(),
        require_applicable=True,
    ) != candidate:
        return
    try:
        _consume_reset_index(candidate)
    except transport.CodexError as exc:
        print(f"[codex-gateway] automatic reset failed for credential {candidate}: {exc}", file=sys.stderr)


def refresh_reset_credits(force: bool = False, account: str | None = None) -> None:
    with MAINTENANCE_LOCK:
        indices = _refresh_indices(account)
        now = time.monotonic()
        error = None
        for index in indices:
            if not force and now - REFRESH_TIMERS["resets"].get(index, 0.0) < RESET_REFRESH_INTERVAL:
                continue
            try:
                _refresh_reset_index(index)
            except transport.CodexError as exc:
                error = exc
                print(f"[codex-gateway] reset-credit refresh failed for credential {index}: {exc}", file=sys.stderr)
                continue
            REFRESH_TIMERS["resets"][index] = time.monotonic()
        _consume_automatic_reset(indices)
        if account is not None and error is not None:
            raise error


def _usage(auth: transport.CodexAuth, account: str | None = None) -> list[dict]:
    with auth._lock(transport.fcntl.LOCK_SH):
        root = auth._load_unlocked()
        snapshots = []
        indices = _account_indices(root, account)
        for index in indices:
            credential = root["credentials"][index]
            if credential.get("invalid") is True:
                if account is not None:
                    raise transport.CodexError(f"Codex account is marked invalid: {account}")
                continue
            snapshots.append({
                "account": credential.get("account") or f"cred-{index}",
                "email": credential.get("email"),
                "rate_limits": credential.get("rate_limits"),
                "reset_credits": credential.get("reset_credits"),
            })
        return snapshots


def _consume_reset(account: str) -> list[dict]:
    with MAINTENANCE_LOCK:
        index = _refresh_indices(account)[0]
        _consume_reset_index(index)
    return _usage(transport.CodexAuth(), account=account)


def _maintenance_loop() -> None:
    while True:
        for refresh in (refresh_quota, refresh_reset_credits):
            try:
                refresh()
            except transport.CodexError as exc:
                print(f"[codex-gateway] maintenance failed: {exc}", file=sys.stderr)
        time.sleep(MAINTENANCE_INTERVAL)


def main() -> None:
    server = ThreadingHTTPServer((DEFAULT_HOST, DEFAULT_PORT), Handler)
    threading.Thread(target=_maintenance_loop, name="codex-maintenance", daemon=True).start()
    print(
        "codex-gateway listening on http://%s:%d" % (DEFAULT_HOST, DEFAULT_PORT),
        flush=True,
    )
    server.serve_forever()

if __name__ == "__main__":
    main()
