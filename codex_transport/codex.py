"""Client for the Codex (ChatGPT backend) Responses API.

Three upstream quirks drive this module:

* The endpoint is SSE-only (`"stream": false` is a 400) and `store` must be
  false, so non-streaming callers are served by buffering the stream.
* `response.completed` arrives with an *empty* `output` array: text, reasoning
  and tool calls only ever appear as stream events. `_ResponseBuilder`
  reassembles a normal Responses API `Response` object from those events.
* Unsupported parameters are rejected by name with a 400, so `prepare_body`
  only forwards what the backend accepts.
"""

from __future__ import annotations

import base64
import fcntl
import http.client
import json
import os
import socket
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

CRED_FILE = os.path.expanduser("~/.codex/auth.json")
REFRESH_URL = "https://auth.openai.com/oauth/token"
RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
MODELS_URL = "https://chatgpt.com/backend-api/codex/models"

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CLIENT_VERSION = "0.146.0"
DEFAULT_MODEL = "gpt-5.6-luna"

DEFAULT_FIRST_BYTE_TIMEOUT = 60.0
DEFAULT_IDLE_TIMEOUT = 30.0
REFRESH_WINDOW_SECONDS = 300
QUOTA_SAVE_INTERVAL_SECONDS = 60

# Body keys the backend accepts. `store` is accepted and forced to false.
SUPPORTED_BODY_KEYS = frozenset(
    {
        "model",
        "input",
        "instructions",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "reasoning",
        "text",
        "include",
        "stream",
        "store",
    }
)

class CodexError(Exception):
    pass

class CredentialInvalidError(CodexError):
    """Raised when a credential is rejected (401) or its refresh token dies."""

class RateLimitedError(CodexError):
    """Raised when a credential is out of quota (429)."""

class CodexStallError(CodexError):
    """Raised when the SSE stream ends or idles out before the response is done."""

class UpstreamBadRequestError(CodexError):
    """Raised when the upstream rejects the request body (HTTP 400/404/422)."""

@dataclass(frozen=True)
class StreamTimeouts:
    first_byte: float = DEFAULT_FIRST_BYTE_TIMEOUT
    idle: float = DEFAULT_IDLE_TIMEOUT

def _uuid7() -> str:
    """UUIDv7: time-ordered, which is what the Codex backend expects."""
    timestamp_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    random_bits = int.from_bytes(os.urandom(10), "big")
    value = (
        ((timestamp_ms & ((1 << 48) - 1)) << 80)
        | (0x7 << 76)
        | (((random_bits >> 62) & 0xFFF) << 64)
        | (0b10 << 62)
        | (random_bits & ((1 << 62) - 1))
    )
    return str(uuid.UUID(int=value))

SESSION_ID = _uuid7()

def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

def jwt_payload(token: str) -> dict:
    try:
        payload = token.split(".")[1]
    except (AttributeError, IndexError):
        return {}
    try:
        return json.loads(_b64url_decode(payload).decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return {}

def jwt_exp(token: str) -> datetime | None:
    value = jwt_payload(token).get("exp")
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None

def _headers(credential: dict) -> dict[str, str]:
    tokens = credential.get("tokens") or {}
    headers = {
        "Authorization": "Bearer " + (tokens.get("access_token") or ""),
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Openai-Beta": "responses=experimental",
        "Originator": "codex_cli_rs",
        "User-Agent": f"codex_cli_rs/{CLIENT_VERSION}",
        "Session_id": SESSION_ID,
        "Version": CLIENT_VERSION,
    }
    if tokens.get("account_id"):
        headers["ChatGPT-Account-ID"] = tokens["account_id"]
    return headers

def _error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - the body is best-effort diagnostics only
        return str(exc)
    try:
        payload = json.loads(raw)
    except ValueError:
        return raw.strip()[:500] or str(exc)
    if isinstance(payload, dict):
        if isinstance(payload.get("detail"), str):
            return payload["detail"]
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
    return raw.strip()[:500]

def _http_json(
    url: str,
    headers: dict[str, str] | None = None,
    data: dict | None = None,
    timeout: float = 30.0,
) -> tuple[int, dict | str]:
    payload = json.dumps(data).encode() if data is not None else None
    request = urllib.request.Request(url, data=payload, headers=headers or {}, method="POST" if payload else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = _error_detail(exc)
        return exc.code, detail
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        return 0, str(exc)
    try:
        return 200, json.loads(raw)
    except ValueError:
        return 200, raw

class CodexAuth:
    """Loads, refreshes and rotates the ChatGPT OAuth credentials in the pool.

    The credential file defaults to `~/.codex/auth.json`,
    so all writes take the same lock file and preserve unknown top-level keys.
    """

    def __init__(self, path: str | None = None):
        self.path = os.path.expanduser(
            path or os.environ.get("CODEX_GATEWAY_CRED_FILE") or CRED_FILE
        )
        self.lock_path = self.path + ".lock"
        self.credentials = self._load()

    def _load(self) -> list[dict]:
        try:
            with open(self.path) as handle:
                root = json.load(handle)
        except FileNotFoundError as exc:
            raise CodexError(f"codex credential file not found: {self.path}") from exc
        except ValueError as exc:
            raise CodexError(f"codex credential file is not JSON: {self.path}") from exc
        credentials = root.get("credentials") if isinstance(root, dict) else None
        if not isinstance(credentials, list) or not credentials:
            raise CodexError(f"no codex credentials in {self.path}")
        return credentials

    def _save(self) -> None:
        with open(self.lock_path, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                try:
                    with open(self.path) as handle:
                        root = json.load(handle)
                except (OSError, ValueError):
                    root = {}
                if not isinstance(root, dict):
                    root = {}
                root["credentials"] = self.credentials
                tmp = f"{self.path}.{os.getpid()}.tmp"
                with open(tmp, "w") as handle:
                    json.dump(root, handle, indent=2)
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.path)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @staticmethod
    def _used(credential: dict, name: str) -> float:
        limits = ((credential.get("rate_limits") or {}).get("limits") or {})
        window = limits.get(name)
        if isinstance(window, dict):
            try:
                return float(window.get("used_percent", 100.0))
            except (TypeError, ValueError):
                return 100.0
        return 100.0

    def _sort_key(self, credential: dict) -> tuple[float, float, float]:
        primary = self._used(credential, "codex_primary")
        secondary = self._used(credential, "codex_secondary")
        return (max(primary, secondary), primary, secondary)

    def select_credential(self) -> dict:
        usable = [c for c in self.credentials if not c.get("invalid")]
        if not usable:
            raise CodexError(f"every credential in {self.path} is marked invalid")
        return min(usable, key=self._sort_key)

    def needs_refresh(self, credential: dict) -> bool:
        expiry = jwt_exp((credential.get("tokens") or {}).get("access_token", ""))
        if expiry is None:
            return True
        return expiry - datetime.now(timezone.utc) < timedelta(seconds=REFRESH_WINDOW_SECONDS)

    def refresh(self, credential: dict) -> None:
        status, body = _http_json(
            REFRESH_URL,
            data={
                "client_id": CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": (credential.get("tokens") or {}).get("refresh_token"),
            },
            timeout=30.0,
        )
        if status != 200 or not isinstance(body, dict) or not body.get("access_token"):
            if status in (400, 401, 403):
                self.mark_invalid(credential)
                raise CredentialInvalidError(f"token refresh failed ({status}): {body}")
            raise CodexError(f"token refresh failed ({status}): {body}")
        tokens = credential.setdefault("tokens", {})
        tokens["access_token"] = body["access_token"]
        for key in ("id_token", "refresh_token", "account_id"):
            if body.get(key):
                tokens[key] = body[key]
        credential["last_refresh"] = datetime.now(timezone.utc).isoformat()
        self._save()

    def mark_invalid(self, credential: dict) -> None:
        credential["invalid"] = datetime.now(timezone.utc).isoformat()
        self._save()

    def mark_rate_limited(self, credential: dict) -> None:
        now = int(time.time())
        credential["rate_limits"] = {
            "fetched_at": now,
            "limits": {
                "codex_primary": {"used_percent": 100.0, "reset_at": now + 5 * 3600},
                "codex_secondary": {"used_percent": 100.0, "reset_at": now + 7 * 24 * 3600},
            },
        }
        self._save()

    def note_rate_limits(self, credential: dict, limits: dict) -> None:
        """Record a fresh quota snapshot, at most once a minute per credential."""
        previous = credential.get("rate_limits") or {}
        if (
            isinstance(previous, dict)
            and now_fetched(previous) is not None
            and int(time.time()) - int(now_fetched(previous)) < QUOTA_SAVE_INTERVAL_SECONDS
        ):
            return
        credential["rate_limits"] = limits
        self._save()

def now_fetched(snapshot: dict) -> int | None:
    try:
        return int(snapshot.get("fetched_at"))
    except (TypeError, ValueError):
        return None

def _sanitize_input(items):
    """Strip fields the backend rejects on replayed input items.

    Upstream done items carry `content: []` on function_call items, which the
    request schema rejects (`Unknown parameter: 'input[N].content'`), so that
    key is dropped before replay.
    """
    if not isinstance(items, list):
        return items
    cleaned = []
    for item in items:
        if isinstance(item, dict) and item.get("type") == "function_call" and "content" in item:
            item = {key: value for key, value in item.items() if key != "content"}
        cleaned.append(item)
    return cleaned

def prepare_body(body: dict) -> dict:
    """Normalize a Responses API body into what the Codex backend accepts."""
    prepared = {key: value for key, value in body.items() if key in SUPPORTED_BODY_KEYS and key != "store"}
    if "input" in prepared:
        prepared["input"] = _sanitize_input(prepared["input"])
    prepared["stream"] = True
    prepared["store"] = False
    prepared.setdefault("model", DEFAULT_MODEL)
    prepared.setdefault("instructions", "")
    prepared.setdefault("tool_choice", "auto")
    prepared.setdefault("parallel_tool_calls", True)
    prepared.setdefault("prompt_cache_key", _uuid7())
    prepared.setdefault("include", ["reasoning.encrypted_content"])
    return prepared

def _rate_limits(headers) -> dict[str, str]:
    """Pass through every x-codex-* rate limit header, verbatim."""
    if not headers:
        return {}
    return {
        name.lower(): value
        for name, value in headers.items()
        if name.lower().startswith("x-codex-")
    }

def _header_number(headers, name: str) -> float | int | None:
    if not headers:
        return None
    raw = headers.get(name)
    if raw is None:
        return None
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number

def quota_snapshot(headers) -> dict | None:
    """Turn the x-codex-* headers into a quota snapshot dict."""
    primary = _header_number(headers, "x-codex-primary-used-percent")
    secondary = _header_number(headers, "x-codex-secondary-used-percent")
    if primary is None and secondary is None:
        return None
    limits = {}
    if primary is not None:
        limits["codex_primary"] = {
            "used_percent": primary,
            "reset_at": _header_number(headers, "x-codex-primary-reset-at"),
        }
    if secondary is not None:
        limits["codex_secondary"] = {
            "used_percent": secondary,
            "reset_at": _header_number(headers, "x-codex-secondary-reset-at"),
        }
    return {"fetched_at": int(time.time()), "limits": limits}

def _normalize_message_phase(item):
    """Translate Codex-only message phases to public Responses API phases."""
    if not isinstance(item, dict):
        return item
    if item.get("type") == "message" and item.get("phase") == "final_answer":
        item = dict(item)
        item["phase"] = "final"
    return item


def _normalize_event(event):
    """Normalize Codex SSE payloads before exposing them through the gateway."""
    if not isinstance(event, dict):
        return event
    normalized = event
    item = event.get("item")
    normalized_item = _normalize_message_phase(item)
    if normalized_item is not item:
        normalized = dict(normalized)
        normalized["item"] = normalized_item
    response = event.get("response")
    if isinstance(response, dict) and isinstance(response.get("output"), list):
        output = [_normalize_message_phase(entry) for entry in response["output"]]
        if any(new is not old for new, old in zip(output, response["output"])):
            if normalized is event:
                normalized = dict(normalized)
            normalized_response = dict(response)
            normalized_response["output"] = output
            normalized["response"] = normalized_response
    return normalized


def _response_socket(response):
    """Best-effort access to the live socket behind an HTTP/SSE stream."""
    for holder in (response, getattr(response, "fp", None)):
        for attribute in ("_sock", "raw", "sock"):
            candidate = getattr(holder, attribute, None)
            if isinstance(candidate, socket.socket):
                return candidate
            nested = getattr(candidate, "_sock", None)
            if isinstance(nested, socket.socket):
                return nested
    return None

def _iter_sse(response, idle_timeout: float):
    """Yield (event_name, data) pairs, dropping to an idle timeout after the first line."""
    sock = _response_socket(response)
    name = None
    data: list[str] = []
    first = True
    for raw in response:
        if first:
            if sock is not None:
                sock.settimeout(idle_timeout)
            first = False
        line = raw.decode("utf-8", "replace").rstrip("\r\n") if isinstance(raw, bytes) else raw.rstrip("\r\n")
        if line == "":
            if data:
                yield name, "\n".join(data)
            name, data = None, []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        yield name, "\n".join(data)

class _ResponseBuilder:
    """Rebuild `response.output` from stream events.

    `response.completed` carries an empty output array upstream, so items are
    assembled from output_item.added / *.delta / output_item.done. The `done`
    item is authoritative; deltas exist so a streaming caller can inject a
    populated output into the final event as well.
    """

    def __init__(self) -> None:
        self._items: dict = {}

    @staticmethod
    def _part(item: dict | None, content_index: int) -> dict | None:
        if not isinstance(item, dict):
            return None
        content = item.setdefault("content", [])
        if not isinstance(content, list):
            return None
        while len(content) <= content_index:
            content.append({})
        part = content[content_index]
        return part if isinstance(part, dict) else None

    def _slot(self, event: dict) -> int:
        index = event.get("output_index")
        return index if isinstance(index, int) else (max(self._items) + 1 if self._items else 0)

    @staticmethod
    def _merge(prior: dict, done: dict) -> dict:
        """Done items are authoritative, but fill their gaps from the deltas.

        Some upstream events ship a `done` item whose text/arguments/summary is
        empty even though the deltas carried the content, so neither side can
        simply win.
        """
        merged = dict(done)
        prior_content = prior.get("content")
        merged_content = merged.setdefault("content", [])
        if isinstance(prior_content, list) and isinstance(merged_content, list):
            for index, part in enumerate(prior_content):
                if not isinstance(part, dict):
                    continue
                if index >= len(merged_content):
                    merged_content.append(dict(part))
                    continue
                current = merged_content[index]
                if isinstance(current, dict) and part.get("text") and not current.get("text"):
                    merged_content[index] = {**current, "text": part["text"]}
        prior_summary = prior.get("summary")
        merged_summary = merged.get("summary")
        if isinstance(prior_summary, list) and prior_summary:
            if not isinstance(merged_summary, list) or not any(
                isinstance(entry, dict) and entry.get("text") for entry in merged_summary
            ):
                merged["summary"] = prior_summary
        if prior.get("arguments") and not merged.get("arguments"):
            merged["arguments"] = prior["arguments"]
        for key, value in prior.items():
            if value in (None, "", [], {}):
                continue
            if merged.get(key) in (None, "", [], {}):
                merged[key] = value
        return merged

    def add(self, event: dict) -> None:
        etype = event.get("type") or ""
        item = event.get("item")
        if etype == "response.output_item.added" and isinstance(item, dict):
            self._items[self._slot(event)] = dict(item)
            return
        if etype == "response.output_item.done" and isinstance(item, dict):
            slot = self._slot(event)
            prior = self._items.get(slot)
            self._items[slot] = self._merge(prior, item) if isinstance(prior, dict) else dict(item)
            return
        index = event.get("output_index")
        if not isinstance(index, int):
            return
        target = self._items.get(index)
        if etype == "response.content_part.added":
            part = event.get("part")
            if target is not None and isinstance(part, dict):
                content = target.setdefault("content", [])
                if isinstance(content, list):
                    content_index = event.get("content_index", len(content))
                    if isinstance(content_index, int):
                        while len(content) <= content_index:
                            content.append({})
                        content[content_index] = dict(part)
            return
        if etype == "response.output_text.delta":
            part = self._part(target, event.get("content_index", 0) or 0)
            if part is not None:
                part["text"] = part.get("text", "") + (event.get("delta") or "")
            return
        if etype == "response.function_call_arguments.delta":
            if target is not None:
                target["arguments"] = target.get("arguments", "") + (event.get("delta") or "")
            return
        if etype == "response.reasoning_summary_part.added":
            if target is not None:
                summary = target.setdefault("summary", [])
                if isinstance(summary, list):
                    summary.append({"type": "summary_text", "text": ""})
            return
        if etype == "response.reasoning_summary_text.delta":
            if target is not None:
                summary = target.setdefault("summary", [])
                if isinstance(summary, list):
                    if not summary:
                        summary.append({"type": "summary_text", "text": ""})
                    summary[0]["text"] = summary[0].get("text", "") + (event.get("delta") or "")

    def output(self) -> list[dict]:
        ordered = sorted(self._items, key=lambda key: (key is None, key if key is not None else 0))
        return [self._items[key] for key in ordered]

def _open(auth: CodexAuth, body: dict, timeouts: StreamTimeouts):
    credential = auth.select_credential()
    if auth.needs_refresh(credential):
        auth.refresh(credential)
    request = urllib.request.Request(
        RESPONSES_URL,
        data=json.dumps(body, separators=(",", ":")).encode(),
        headers=_headers(credential),
        method="POST",
    )
    try:
        response = urllib.request.urlopen(request, timeout=timeouts.first_byte)
    except urllib.error.HTTPError as exc:
        detail = _error_detail(exc)
        if exc.code == 401:
            auth.mark_invalid(credential)
            raise CredentialInvalidError(f"codex upstream 401: {detail}") from exc
        if exc.code == 429:
            auth.mark_rate_limited(credential)
            raise RateLimitedError(f"codex upstream 429: {detail}") from exc
        if exc.code in (400, 404, 422):
            raise UpstreamBadRequestError(f"codex upstream {exc.code}: {detail}") from exc
        raise CodexError(f"codex upstream {exc.code}: {detail}") from exc
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        raise CodexError(f"codex upstream unreachable: {exc}") from exc
    return response, credential

def iter_events(body: dict, auth: CodexAuth | None = None, timeouts: StreamTimeouts | None = None, meta: dict | None = None):
    """Yield decoded SSE events from the Codex backend.

    `meta` (if given) is filled in with `email`, `status` and `rate_limits`
    once the upstream response headers are available, so callers can surface
    which account served the request and how much quota it has left.
    """
    auth = auth or CodexAuth()
    timeouts = timeouts or StreamTimeouts()
    body = prepare_body(body)
    error: Exception | None = None
    for _ in range(min(len(auth.credentials) + 1, 8)):
        try:
            response, credential = _open(auth, body, timeouts)
        except (CredentialInvalidError, RateLimitedError) as exc:
            error = exc
            continue
        if meta is not None:
            meta.update(
                {
                    "email": credential.get("email"),
                    "status": response.status,
                    "rate_limits": _rate_limits(response.headers),
                }
            )
            limits = quota_snapshot(response.headers)
            if limits:
                auth.note_rate_limits(credential, limits)
        try:
            for _name, data in _iter_sse(response, timeouts.idle):
                if data == "[DONE]":
                    return
                try:
                    event = json.loads(data)
                except ValueError:
                    continue
                yield _normalize_event(event)
            return
        except (socket.timeout, TimeoutError) as exc:
            raise CodexStallError(f"codex stream idled out after {timeouts.idle}s") from exc
        except http.client.HTTPException as exc:
            raise CodexError(f"codex stream failed: {exc}") from exc
        except OSError as exc:
            raise CodexError(f"codex stream failed: {exc}") from exc
        finally:
            response.close()
    raise error or CodexError("no codex credential could serve the request")

def responses(body: dict, auth: CodexAuth | None = None, timeouts: StreamTimeouts | None = None, meta: dict | None = None) -> dict:
    """Run a request and return a complete Responses API `Response` object."""
    builder = _ResponseBuilder()
    final: dict | None = None
    for event in iter_events(body, auth=auth, timeouts=timeouts, meta=meta):
        builder.add(event)
        etype = event.get("type")
        if etype in ("response.completed", "response.incomplete", "response.failed"):
            final = event.get("response")
        elif etype == "error":
            raise CodexError(event.get("message") or "codex stream returned an error event")
    if not isinstance(final, dict):
        raise CodexStallError("codex stream ended without a response.completed event")
    final = dict(final)
    if not final.get("output"):
        final["output"] = builder.output()
    usage = final.get("usage")
    if isinstance(usage, dict) and "attribution" in usage:
        usage = {key: value for key, value in usage.items() if key != "attribution"}
        final["usage"] = usage
    return final

def list_models(auth: CodexAuth | None = None) -> list[str]:
    auth = auth or CodexAuth()
    credential = auth.select_credential()
    if auth.needs_refresh(credential):
        auth.refresh(credential)
    status, body = _http_json(
        f"{MODELS_URL}?client_version={CLIENT_VERSION}",
        headers=_headers(credential),
        timeout=30.0,
    )
    if status != 200 or not isinstance(body, dict):
        raise CodexError(f"codex model list failed ({status}): {body}")
    return [model["slug"] for model in body.get("models") or [] if isinstance(model, dict) and model.get("slug")]

def usage(auth: CodexAuth | None = None) -> list[dict]:
    """Per-account quota, straight from /wham/usage."""
    auth = auth or CodexAuth()
    snapshots = []
    for credential in auth.credentials:
        if credential.get("invalid"):
            continue
        if auth.needs_refresh(credential):
            try:
                auth.refresh(credential)
            except CredentialInvalidError as exc:
                snapshots.append({"email": credential.get("email"), "error": str(exc)})
                continue
        status, body = _http_json(USAGE_URL, headers=_headers(credential), timeout=30.0)
        if status != 200 or not isinstance(body, dict):
            snapshots.append({"email": credential.get("email"), "error": f"{status}: {body}"})
            continue
        rate_limit = body.get("rate_limit") or {}
        snapshots.append(
            {
                "email": credential.get("email"),
                "plan_type": body.get("plan_type"),
                "allowed": rate_limit.get("allowed"),
                "limit_reached": rate_limit.get("limit_reached"),
                "primary_used_percent": (rate_limit.get("primary_window") or {}).get("used_percent"),
                "secondary_used_percent": (rate_limit.get("secondary_window") or {}).get("used_percent"),
                "primary_reset_at": (rate_limit.get("primary_window") or {}).get("reset_at"),
                "secondary_reset_at": (rate_limit.get("secondary_window") or {}).get("reset_at"),
            }
        )
    return snapshots
