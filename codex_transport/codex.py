"""Codex OAuth transport for the ChatGPT Codex Responses endpoint."""

from __future__ import annotations

import base64
import copy
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import fcntl
import json
import logging
import os
import socket
from io import BytesIO
import tempfile
import time
import urllib.error
import urllib.request
import uuid

logger = logging.getLogger("codex_gateway")

CRED_FILE = os.path.expanduser(os.environ.get("CODEX_GATEWAY_CRED_FILE", "~/.codex/auth.json"))
REFRESH_URL = "https://auth.openai.com/oauth/token"
RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
MODELS_URL = "https://chatgpt.com/backend-api/codex/models"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CLIENT_VERSION = "0.146.0"
MODEL = "gpt-5.6-luna"
DEFAULT_FIRST_BYTE_TIMEOUT = 60.0
DEFAULT_THINKING_IDLE_TIMEOUT = 30.0
DEFAULT_ANSWERING_IDLE_TIMEOUT = 30.0
# Backward-compatible alias: historical `timeout` means first-byte / connect budget.
DEFAULT_TIMEOUT = DEFAULT_FIRST_BYTE_TIMEOUT
REFRESH_WINDOW_MINUTES = 5
MAX_REFRESH_AGE_DAYS = 8


def _uuid7() -> str:
    timestamp_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    random_bits = int.from_bytes(os.urandom(10), "big")
    value = (
        (timestamp_ms & ((1 << 48) - 1)) << 80
        | 0x7 << 76
        | ((random_bits >> 62) & 0xFFF) << 64
        | 0b10 << 62
        | (random_bits & ((1 << 62) - 1))
    )
    return str(uuid.UUID(int=value))


SESSION_ID = _uuid7()


class CodexError(Exception):
    pass


class CredentialInvalidError(CodexError):
    """Raised when token refresh returns 401 and the credential is marked invalid."""

    def __init__(self, message: str, *, index: int):
        super().__init__(message)
        self.index = index


class CodexStallError(CodexError):
    """Raised when a Codex SSE stream stops emitting progress events."""

    def __init__(self, message: str, *, stage: str, idle_timeout: float, last_event_type: str | None = None):
        super().__init__(message)
        self.stage = stage
        self.idle_timeout = idle_timeout
        self.last_event_type = last_event_type


@dataclass(frozen=True)
class StreamTimeouts:
    first_byte: float = DEFAULT_FIRST_BYTE_TIMEOUT
    thinking_idle: float = DEFAULT_THINKING_IDLE_TIMEOUT
    answering_idle: float = DEFAULT_ANSWERING_IDLE_TIMEOUT

    def idle_for(self, stage: str) -> float:
        if stage == "answering":
            return self.answering_idle
        if stage == "thinking":
            return self.thinking_idle
        return self.first_byte


def _response_socket(stream):
    """Best-effort extraction of the live socket behind an HTTP/SSE stream."""
    seen = set()
    stack = [stream]
    while stack:
        obj = stack.pop()
        if obj is None:
            continue
        obj_id = id(obj)
        if obj_id in seen:
            continue
        seen.add(obj_id)
        if hasattr(obj, "settimeout") and hasattr(obj, "recv"):
            return obj
        for attr in ("_sock", "socket", "raw", "fp", "_stream", "rfile"):
            child = getattr(obj, attr, None)
            if child is not None:
                stack.append(child)
    return None


def _set_socket_timeout(sock, timeout: float | None) -> None:
    if sock is None or timeout is None:
        return
    try:
        sock.settimeout(timeout)
    except OSError:
        pass


def _is_answering_event(event: dict) -> bool:
    event_type = event.get("type") or ""
    if event_type in {
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.added",
        "response.content_part.done",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
    }:
        return True
    if event_type in {"response.output_item.added", "response.output_item.done"}:
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") in {"message", "function_call"}:
            return True
    return False


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _jwt_payload(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = json.loads(_b64url_decode(parts[1]))
        return payload if isinstance(payload, dict) else {}
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def jwt_exp(token: str) -> datetime | None:
    value = _jwt_payload(token).get("exp")
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _credential_needs_refresh(credential: dict) -> bool:
    token = credential["tokens"]["access_token"]
    expiration = jwt_exp(token)
    now = datetime.now(timezone.utc)
    if expiration is not None:
        return expiration <= now + timedelta(minutes=REFRESH_WINDOW_MINUTES)
    value = credential.get("last_refresh")
    if isinstance(value, str):
        try:
            refreshed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if refreshed.tzinfo is None:
                refreshed = refreshed.replace(tzinfo=timezone.utc)
            return refreshed < now - timedelta(days=MAX_REFRESH_AGE_DAYS)
        except ValueError:
            pass
    return False


def _quota_snapshot_from_event(event: dict, fetched_at: int | None = None) -> dict:
    limits = {}
    details = event.get("rate_limits")
    if isinstance(details, dict):
        prefix = event.get("metered_limit_name") or event.get("limit_name") or "codex"
        for window_name in ("primary", "secondary"):
            window = details.get(window_name)
            if not isinstance(window, dict):
                continue
            used_percent = window.get("used_percent")
            reset_at = window.get("reset_at")
            if isinstance(used_percent, (int, float)) and isinstance(reset_at, (int, float)):
                limit = {
                    "used_percent": float(used_percent),
                    "reset_at": int(reset_at),
                }
                window_minutes = window.get("window_minutes")
                if isinstance(window_minutes, (int, float)):
                    limit["limit_window_seconds"] = int(window_minutes * 60)
                limits[f"{prefix}_{window_name}"] = limit
    return {
        "fetched_at": int(time.time()) if fetched_at is None else int(fetched_at),
        "limits": limits,
    }


def _add_usage_windows(limits: dict, prefix: str, details) -> None:
    if not isinstance(details, dict):
        return
    for window_name in ("primary_window", "secondary_window"):
        window = details.get(window_name)
        if not isinstance(window, dict):
            continue
        used_percent = window.get("used_percent")
        reset_at = window.get("reset_at")
        if isinstance(used_percent, (int, float)) and isinstance(reset_at, (int, float)):
            suffix = window_name.removesuffix("_window")
            limit = {
                "used_percent": float(used_percent),
                "reset_at": int(reset_at),
            }
            window_seconds = window.get("limit_window_seconds")
            if isinstance(window_seconds, (int, float)):
                limit["limit_window_seconds"] = int(window_seconds)
            limits[f"{prefix}_{suffix}"] = limit


def _quota_snapshot_from_usage(payload: dict, fetched_at: int | None = None) -> dict:
    limits = {}
    _add_usage_windows(limits, "codex", payload.get("rate_limit"))
    _add_usage_windows(limits, "code_review", payload.get("code_review_rate_limit"))
    additional = payload.get("additional_rate_limits")
    if isinstance(additional, list):
        for index, item in enumerate(additional):
            if not isinstance(item, dict):
                continue
            name = item.get("metered_feature") or item.get("limit_name") or f"additional_{index}"
            _add_usage_windows(limits, str(name), item.get("rate_limit"))
    return {
        "fetched_at": int(time.time()) if fetched_at is None else int(fetched_at),
        "limits": limits,
    }


def _effective_quota(credential: dict):
    quota = credential.get("rate_limits")
    limits = quota.get("limits") if isinstance(quota, dict) else None
    if not isinstance(limits, dict):
        return None
    windows = []
    for limit in limits.values():
        if not isinstance(limit, dict):
            continue
        used_percent = limit.get("used_percent")
        reset_at = limit.get("reset_at")
        if not isinstance(used_percent, (int, float)) or not isinstance(reset_at, (int, float)):
            continue
        windows.append((int(reset_at), 100.0 - float(used_percent)))
    windows.sort()
    for index, window in enumerate(windows):
        _, remaining = window
        if not any(later_remaining <= remaining for _, later_remaining in windows[index + 1:]):
            return window
    return None


def _manual_reset_expiration(credential: dict) -> int | None:
    resets = credential.get("reset_credits")
    credits = resets.get("credits") if isinstance(resets, dict) else None
    if not isinstance(credits, list):
        return None
    now = time.time()
    expirations = []
    for credit in credits:
        if (
            not isinstance(credit, dict)
            or credit.get("status") != "available"
            or credit.get("reset_type") != "codex_rate_limits"
        ):
            continue
        expires_at = credit.get("expires_at")
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00")).timestamp()
        if isinstance(expires_at, (int, float)) and expires_at > now:
            expirations.append(int(expires_at))
    return min(expirations) if expirations else None


class CodexAuth:
    def __init__(self, path: str = CRED_FILE):
        self.path = os.path.expanduser(path)
        self.lock_path = self.path + ".lock"
        self.root = self._load()
        self.index = 0
        self.data = self.root["credentials"][0]

    def _prepare_directory(self) -> str:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, mode=0o700, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        return directory

    @contextmanager
    def _lock(self, operation: int):
        self._prepare_directory()
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        locked = False
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, operation)
            locked = True
            yield
        finally:
            if locked:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _validate_root(self, root) -> dict:
        credentials = root.get("credentials") if isinstance(root, dict) else None
        if not isinstance(credentials, list) or not credentials:
            raise CodexError("Codex credential file must contain a non-empty credentials list")
        for index, credential in enumerate(credentials):
            tokens = credential.get("tokens") if isinstance(credential, dict) else None
            if not isinstance(tokens, dict):
                raise CodexError(f"Codex credential {index} is missing the tokens object")
            for name in ("access_token", "refresh_token"):
                if not isinstance(tokens.get(name), str) or not tokens[name]:
                    raise CodexError(f"Codex credential {index} is missing {name}")
        return root

    def _load_unlocked(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as stream:
                root = json.load(stream)
        except FileNotFoundError:
            raise FileNotFoundError(
                f"No Codex credentials at {self.path}. Create a credential file "
                "containing a credentials list."
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise CodexError(f"Could not read Codex credentials at {self.path}: {exc}") from exc
        return self._validate_root(root)

    def _load(self) -> dict:
        try:
            with self._lock(fcntl.LOCK_SH):
                return self._load_unlocked()
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise CodexError(f"Could not read Codex credentials at {self.path}: {exc}") from exc

    def _select_loaded(self, root: dict, index: int) -> None:
        self.root = root
        self.index = index
        self.data = root["credentials"][index]

    def _save_unlocked(self) -> None:
        directory = self._prepare_directory()
        fd, temporary = tempfile.mkstemp(prefix=".codex-auth-", dir=directory)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.root, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def _save(self) -> None:
        with self._lock(fcntl.LOCK_EX):
            self._save_unlocked()

    def save_rate_limits(self, rate_limits: dict) -> None:
        snapshot = _quota_snapshot_from_event(rate_limits)
        if not snapshot["limits"]:
            return
        with self._lock(fcntl.LOCK_EX):
            root = self._load_unlocked()
            root["credentials"][self.index]["rate_limits"] = snapshot
            self._select_loaded(root, self.index)
            self._save_unlocked()

    @property
    def access_token(self) -> str:
        return self.data["tokens"]["access_token"]

    @property
    def account_id(self) -> str:
        value = self.data["tokens"].get("account_id")
        if isinstance(value, str) and value:
            return value
        auth = _jwt_payload(self.access_token).get("https://api.openai.com/auth", {})
        return auth.get("chatgpt_account_id", "") if isinstance(auth, dict) else ""

    def needs_refresh(self) -> bool:
        return _credential_needs_refresh(self.data)

    def _refresh_credential_unlocked(self, index: int, timeout: float = 60) -> None:
        credential = self.root["credentials"][index]
        request = urllib.request.Request(
            REFRESH_URL,
            data=json.dumps({
                "client_id": CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": credential["tokens"]["refresh_token"],
            }).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            if exc.code == 401:
                credential["invalid"] = True
                self._save_unlocked()
                raise CredentialInvalidError(
                    f"Token refresh failed: HTTP {exc.code}: {detail}",
                    index=index,
                ) from exc
            raise CodexError(f"Token refresh failed: HTTP {exc.code}: {detail}") from exc
        except (OSError, ValueError) as exc:
            raise CodexError(f"Token refresh failed: {exc}") from exc
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise CodexError("Token refresh returned no access_token")
        tokens = credential["tokens"]
        tokens["access_token"] = access_token
        for name in ("refresh_token", "id_token"):
            if isinstance(payload.get(name), str) and payload[name]:
                tokens[name] = payload[name]
        credential["last_refresh"] = datetime.now(timezone.utc).isoformat()
        credential.pop("invalid", None)

    def refresh(self, timeout: float = 60) -> None:
        observed_access_token = self.access_token
        with self._lock(fcntl.LOCK_EX):
            root = self._load_unlocked()
            self._select_loaded(root, self.index)
            if self.access_token != observed_access_token:
                return
            self._refresh_credential_unlocked(self.index, timeout)
            self._save_unlocked()

    def ensure_valid(self, timeout: float = 60) -> None:
        while self.needs_refresh():
            try:
                self.refresh(timeout=timeout)
                return
            except CredentialInvalidError:
                self.select_credential(timeout=timeout)

    def _usage_request_unlocked(self, index: int, timeout: float = 60) -> dict:
        for attempt in range(2):
            credential = self.root["credentials"][index]
            if _credential_needs_refresh(credential):
                self._refresh_credential_unlocked(index, timeout)
            account_id = credential["tokens"].get("account_id")
            if not account_id:
                claims = _jwt_payload(credential["tokens"]["access_token"])
                auth = claims.get("https://api.openai.com/auth", {})
                if isinstance(auth, dict):
                    account_id = auth.get("chatgpt_account_id")
            headers = {
                "Authorization": "Bearer " + credential["tokens"]["access_token"],
                "Content-Type": "application/json",
                "User-Agent": f"codex_cli_rs/{CLIENT_VERSION}",
            }
            if account_id:
                headers["ChatGPT-Account-ID"] = account_id
            request = urllib.request.Request(USAGE_URL, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    payload = json.load(response)
            except urllib.error.HTTPError as exc:
                if exc.code == 401 and attempt == 0:
                    self._refresh_credential_unlocked(index, timeout)
                    continue
                detail = exc.read().decode("utf-8", "replace")
                raise CodexError(f"Quota preflight failed: HTTP {exc.code}: {detail}") from exc
            except (OSError, ValueError) as exc:
                raise CodexError(f"Quota preflight failed: {exc}") from exc
            if not isinstance(payload, dict):
                raise CodexError("Quota preflight returned a non-object response")
            snapshot = _quota_snapshot_from_usage(payload)
            if not snapshot["limits"]:
                raise CodexError("Quota preflight returned no usable rate limits")
            credential["rate_limits"] = snapshot
            return snapshot
        raise CodexError("Quota preflight failed after token refresh")

    @staticmethod
    def _quota_stale(credential: dict, now: int) -> bool:
        quota = credential.get("rate_limits")
        fetched_at = quota.get("fetched_at") if isinstance(quota, dict) else None
        return not isinstance(fetched_at, (int, float)) or fetched_at < now - 3600

    @staticmethod
    def _credential_is_invalid(credential: dict) -> bool:
        return credential.get("invalid") is True

    def _choose_credential(self, usable: set[int]):
        normal = []
        reserve = []
        for index, credential in enumerate(self.root["credentials"]):
            if index not in usable or self._credential_is_invalid(credential):
                continue
            effective = _effective_quota(credential)
            if effective is None:
                continue
            reset_at, remaining = effective
            reset_expiration = _manual_reset_expiration(credential)
            deadline = min(reset_at, reset_expiration) if reset_expiration is not None else reset_at
            key = (deadline, remaining, index)
            if remaining > 5:
                normal.append((key, index))
            elif remaining > 0:
                reserve.append((key, index))
        pool = normal or reserve
        return min(pool)[1] if pool else None

    def select_credential(self, timeout: float = 60) -> int:
        with self._lock(fcntl.LOCK_EX):
            root = self._load_unlocked()
            self._select_loaded(root, 0)
            credentials = root["credentials"]
            usable = {
                index
                for index, credential in enumerate(credentials)
                if not self._credential_is_invalid(credential)
            }
            if not usable:
                raise CodexError("No usable Codex credentials (all marked invalid)")
            if len(credentials) == 1:
                return 0

            now = int(time.time())
            errors = []
            for index, credential in enumerate(credentials):
                if index not in usable:
                    continue
                if not self._quota_stale(credential, now):
                    continue
                try:
                    self._usage_request_unlocked(index, timeout)
                except CodexError as exc:
                    usable.discard(index)
                    errors.append(f"credential {index}: {exc}")

            selected = self._choose_credential(usable)
            if selected is None:
                usable = {
                    index
                    for index, credential in enumerate(credentials)
                    if not self._credential_is_invalid(credential)
                }
                errors.clear()
                for index in sorted(usable):
                    try:
                        self._usage_request_unlocked(index, timeout)
                    except CodexError as exc:
                        usable.discard(index)
                        errors.append(f"credential {index}: {exc}")
                selected = self._choose_credential(usable)

            self._save_unlocked()
            if selected is None:
                detail = "; ".join(errors)
                suffix = f": {detail}" if detail else ""
                raise CodexError(f"No Codex credential has positive quota{suffix}")
            self._select_loaded(self.root, selected)
            return selected

    def expire_rate_limits(self) -> None:
        with self._lock(fcntl.LOCK_EX):
            root = self._load_unlocked()
            credential = root["credentials"][self.index]
            quota = credential.get("rate_limits")
            if isinstance(quota, dict):
                quota["fetched_at"] = 0
            self._select_loaded(root, self.index)
            self._save_unlocked()

def _event_result(events: list[dict]) -> dict:
    response = None
    text = []
    calls = {}
    call_order = []
    aliases = {}
    pending_arguments = {}

    def find_call(*identifiers):
        for identifier in identifiers:
            if identifier is not None and identifier in calls:
                return identifier
            if identifier is not None and identifier in aliases:
                return aliases[identifier]
        return None

    def add_call(item):
        item = dict(item)
        identifiers = (item.get("id"), item.get("call_id"))
        key = find_call(*identifiers)
        if key is None:
            key = next((value for value in identifiers if value), None)
            key = key or "call_" + uuid.uuid4().hex
            calls[key] = {"type": "function_call"}
            call_order.append(key)
        existing_arguments = calls[key].get("arguments", "")
        calls[key].update(item)
        calls[key]["type"] = "function_call"
        if not calls[key].get("arguments") and existing_arguments:
            calls[key]["arguments"] = existing_arguments
        for identifier in identifiers:
            if identifier is not None:
                aliases[identifier] = key
        for identifier in identifiers:
            if identifier in pending_arguments:
                calls[key]["arguments"] = pending_arguments.pop(identifier)
                break
        calls[key].setdefault("call_id", str(item.get("call_id") or key))
        calls[key].setdefault("name", "")
        calls[key].setdefault("arguments", "")
        return key

    def append_arguments(identifier, delta):
        key = find_call(identifier)
        if key is None:
            pending_arguments[identifier] = pending_arguments.get(identifier, "") + delta
        else:
            calls[key]["arguments"] = str(calls[key].get("arguments") or "") + delta

    def set_arguments(identifier, arguments):
        key = find_call(identifier)
        if key is None:
            pending_arguments[identifier] = arguments
        else:
            calls[key]["arguments"] = arguments

    for event in events:
        event_type = event.get("type")
        if event_type == "response.output_text.delta":
            text.append(str(event.get("delta", "")))
        elif event_type in ("response.output_item.added", "response.output_item.done"):
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                add_call(item)
        elif event_type == "response.function_call_arguments.delta":
            identifier = event.get("item_id") or event.get("call_id")
            if identifier is not None:
                append_arguments(identifier, str(event.get("delta", "")))
        elif event_type == "response.function_call_arguments.done":
            identifier = event.get("item_id") or event.get("call_id")
            arguments = event.get("arguments")
            if identifier is not None and isinstance(arguments, str):
                set_arguments(identifier, arguments)
        if event_type in ("response.completed", "response.incomplete", "response.failed"):
            value = event.get("response")
            if isinstance(value, dict):
                response = dict(value)

    response = response or {}
    output = response.get("output")
    if not isinstance(output, list):
        output = []

    merged_output = []
    seen_calls = set()
    for item in output:
        if isinstance(item, dict) and item.get("type") == "function_call":
            key = find_call(item.get("id"), item.get("call_id"))
            if key is not None:
                merged = dict(item)
                tracked = calls[key]
                for name, value in tracked.items():
                    if name not in merged or (not merged[name] and value):
                        merged[name] = value
                if tracked.get("arguments"):
                    merged["arguments"] = tracked["arguments"]
                item = merged
                seen_calls.add(key)
        merged_output.append(item)

    if not merged_output and text:
        merged_output.append({
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "".join(text)}],
        })
    for key in call_order:
        if key not in seen_calls:
            merged_output.append(calls[key])
    response["output"] = merged_output
    response.setdefault("status", "completed")
    return response


def _log_sse_event(event: dict) -> None:
    if logger.isEnabledFor(logging.INFO):
        logger.info(json.dumps(event, separators=(",", ":")))


def _header_value(headers, name: str) -> str | None:
    value = headers.get(name) if headers is not None else None
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _header_float(headers, name: str) -> float | None:
    value = _header_value(headers, name)
    if value is None:
        return None
    try:
        result = float(value)
    except ValueError:
        return None
    return result if result == result and abs(result) != float("inf") else None


def _header_int(headers, name: str) -> int | None:
    value = _header_value(headers, name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _header_bool(headers, name: str) -> bool | None:
    value = _header_value(headers, name)
    if value is None:
        return None
    if value.lower() in {"true", "1"}:
        return True
    if value.lower() in {"false", "0"}:
        return False
    return None


def _rate_limits_from_headers(headers) -> dict | None:
    windows = {}
    for name in ("primary", "secondary"):
        used_percent = _header_float(headers, f"x-codex-{name}-used-percent")
        if used_percent is None:
            continue
        window = {"used_percent": used_percent}
        window_minutes = _header_int(headers, f"x-codex-{name}-window-minutes")
        reset_at = _header_int(headers, f"x-codex-{name}-reset-at")
        if window_minutes is not None:
            window["window_minutes"] = window_minutes
        if reset_at is not None:
            window["reset_at"] = reset_at
        windows[name] = window

    has_credits = _header_bool(headers, "x-codex-credits-has-credits")
    unlimited = _header_bool(headers, "x-codex-credits-unlimited")
    credits = None
    if has_credits is not None and unlimited is not None:
        credits = {
            "has_credits": has_credits,
            "unlimited": unlimited,
        }
        balance = _header_value(headers, "x-codex-credits-balance")
        if balance is not None:
            credits["balance"] = balance

    if not windows and credits is None:
        return None
    event = {"type": "codex.rate_limits"}
    if windows:
        event["rate_limits"] = windows
    if credits is not None:
        event["credits"] = credits
    return event


def parse_sse(
    stream,
    timeouts: StreamTimeouts | None = None,
    on_rate_limits=None,
    on_event=None,
) -> dict:
    events = []
    data_lines = []
    log_events = logger.isEnabledFor(logging.INFO)
    sock = _response_socket(stream) if timeouts is not None else None
    stage = "first_byte"
    last_event_type = None
    if timeouts is not None:
        _set_socket_timeout(sock, timeouts.idle_for(stage))
    if log_events:
        logger.info("---------- FROM LLM ----------")

    def note_event(event: dict) -> None:
        nonlocal stage, last_event_type
        last_event_type = event.get("type")
        if timeouts is None:
            return
        if stage == "first_byte":
            stage = "answering" if _is_answering_event(event) else "thinking"
        elif stage == "thinking" and _is_answering_event(event):
            stage = "answering"
        _set_socket_timeout(sock, timeouts.idle_for(stage))

    def stall_error(exc: Exception) -> CodexStallError:
        idle = timeouts.idle_for(stage) if timeouts is not None else 0.0
        detail = last_event_type or "none"
        return CodexStallError(
            f"Codex SSE stalled during {stage} after {idle:g}s idle "
            f"(last event={detail})",
            stage=stage,
            idle_timeout=idle,
            last_event_type=last_event_type,
        )

    try:
        stream_iter = iter(stream)
        while True:
            try:
                raw_line = next(stream_iter)
            except StopIteration:
                break
            except (TimeoutError, socket.timeout) as exc:
                raise stall_error(exc) from exc
            line = raw_line.decode("utf-8", "strict").rstrip("\r\n")
            if line:
                if line.startswith("data:"):
                    value = line[5:]
                    data_lines.append(value[1:] if value.startswith(" ") else value)
                continue
            if not data_lines:
                continue
            data = "\n".join(data_lines)
            data_lines.clear()
            if data == "[DONE]":
                break
            try:
                event = json.loads(data)
            except json.JSONDecodeError as exc:
                raise CodexError(f"Malformed Codex SSE event: {exc}") from exc
            if isinstance(event, dict):
                if log_events:
                    _log_sse_event(event)
                if event.get("type") == "codex.rate_limits" and on_rate_limits is not None:
                    on_rate_limits(event)
                events.append(event)
                if on_event is not None:
                    on_event(event)
                note_event(event)
        if data_lines:
            data = "\n".join(data_lines)
            if data != "[DONE]":
                try:
                    event = json.loads(data)
                except json.JSONDecodeError as exc:
                    raise CodexError(f"Malformed Codex SSE event: {exc}") from exc
                if isinstance(event, dict):
                    if log_events:
                        _log_sse_event(event)
                    if event.get("type") == "codex.rate_limits" and on_rate_limits is not None:
                        on_rate_limits(event)
                    events.append(event)
                    if on_event is not None:
                        on_event(event)
                    note_event(event)
    except (TimeoutError, socket.timeout) as exc:
        raise stall_error(exc) from exc
    return _event_result(events)


def _headers(auth: CodexAuth) -> dict[str, str]:
    headers = {
        "Authorization": "Bearer " + auth.access_token,
        "Content-Type": "application/json",
        "Openai-Beta": "responses=experimental",
        "Originator": "codex_cli_rs",
        "User-Agent": f"codex_cli_rs/{CLIENT_VERSION}",
        "Session_id": SESSION_ID,
        "Version": CLIENT_VERSION,
        "Accept": "text/event-stream",
    }
    if auth.account_id:
        headers["ChatGPT-Account-ID"] = auth.account_id
    return headers


def _parse_json_payload(payload: bytes) -> dict:
    try:
        result = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CodexError(f"Malformed Codex JSON response: {exc}") from exc
    if not isinstance(result, dict):
        raise CodexError("Codex JSON response is not an object")
    return result


def _parse_response_body(content_type: str, payload: bytes) -> dict:
    content_type = content_type.lower()
    if "text/event-stream" in content_type:
        return parse_sse(BytesIO(payload))
    if "json" in content_type:
        return _parse_json_payload(payload)
    if not content_type:
        # The Codex endpoint can omit Content-Type on an otherwise valid SSE
        # response.  Prefer JSON when the body is clearly JSON, then fall back
        # to the event parser used by the normal streaming response.
        if payload.lstrip().startswith(b"{"):
            try:
                return _parse_json_payload(payload)
            except CodexError:
                pass
        return parse_sse(BytesIO(payload))
    raise CodexError(f"Unexpected Codex Content-Type: {content_type!r}")


def _parse_http_response(
    response,
    timeouts: StreamTimeouts | None = None,
    auth: CodexAuth | None = None,
    on_event=None,
) -> dict:
    content_type = ""
    headers = getattr(response, "headers", None)
    if headers:
        content_type = headers.get("Content-Type", "") or ""
        rate_limits = _rate_limits_from_headers(headers)
        if rate_limits is not None and auth is not None:
            auth.save_rate_limits(rate_limits)
    lowered = content_type.lower()
    if "text/event-stream" in lowered:
        return parse_sse(
            response,
            timeouts=timeouts,
            on_rate_limits=auth.save_rate_limits if auth is not None else None,
            on_event=on_event,
        )
    if "json" in lowered:
        payload = response.read()
        result = _parse_json_payload(payload)
        if logger.isEnabledFor(logging.INFO):
            logger.info("---------- FROM LLM ----------")
            logger.info(payload.decode("utf-8", "replace"))
        return result
    if lowered:
        raise CodexError(f"Unexpected Codex Content-Type: {content_type!r}")
    # Empty Content-Type: peek enough to distinguish JSON vs SSE, then either
    # parse JSON or continue incrementally through the live stream.
    first = response.read(1)
    if not first:
        raise CodexError("Empty Codex response body")
    if first.lstrip().startswith(b"{"):
        payload = first + response.read()
        result = _parse_json_payload(payload)
        if logger.isEnabledFor(logging.INFO):
            logger.info("---------- FROM LLM ----------")
            logger.info(payload.decode("utf-8", "replace"))
        return result
    return parse_sse(
        _PrefixedStream(first, response),
        timeouts=timeouts,
        on_rate_limits=auth.save_rate_limits if auth is not None else None,
        on_event=on_event,
    )


class _PrefixedStream:
    """Yield one already-read prefix byte, then continue from the live stream."""

    def __init__(self, prefix: bytes, stream):
        self._prefix = prefix
        self._stream = stream
        self._sent_prefix = False

    def __iter__(self):
        return self

    def __next__(self):
        if not self._sent_prefix:
            self._sent_prefix = True
            if self._prefix:
                # Prefer a full first line when the underlying stream is line-based.
                rest = self._stream.readline() if hasattr(self._stream, "readline") else b""
                return self._prefix + rest
        if hasattr(self._stream, "readline"):
            line = self._stream.readline()
            if line:
                return line
            raise StopIteration
        return next(self._stream)


def _normalize_stream_timeouts(
    timeouts: StreamTimeouts | None = None,
    *,
    timeout: float | None = None,
    first_byte_timeout: float | None = None,
    thinking_idle_timeout: float | None = None,
    answering_idle_timeout: float | None = None,
) -> StreamTimeouts:
    if timeouts is not None:
        base = timeouts
    else:
        first_byte = DEFAULT_FIRST_BYTE_TIMEOUT if timeout is None else float(timeout)
        base = StreamTimeouts(first_byte=first_byte)
    return StreamTimeouts(
        first_byte=base.first_byte if first_byte_timeout is None else float(first_byte_timeout),
        thinking_idle=(
            base.thinking_idle if thinking_idle_timeout is None else float(thinking_idle_timeout)
        ),
        answering_idle=(
            base.answering_idle if answering_idle_timeout is None else float(answering_idle_timeout)
        ),
    )


def _request(auth: CodexAuth, body: dict, timeouts: StreamTimeouts, on_event=None) -> dict:
    auth.select_credential(timeout=timeouts.first_byte)
    encoded = json.dumps(body, separators=(",", ":")).encode()
    for attempt in range(2):
        auth.ensure_valid(timeout=timeouts.first_byte)
        headers = _headers(auth)
        request = urllib.request.Request(
            RESPONSES_URL,
            data=encoded,
            headers=headers,
            method="POST",
        )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("----------- TO LLM -----------")
            logger.debug(f"POST {RESPONSES_URL} {headers}")
            logger.debug(encoded.decode("utf-8"))
        try:
            with urllib.request.urlopen(request, timeout=timeouts.first_byte) as response:
                return _parse_http_response(response, timeouts=timeouts, auth=auth, on_event=on_event)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            if logger.isEnabledFor(logging.INFO):
                logger.info("---------- FROM LLM ----------")
                logger.info(detail)
            if exc.code == 401 and attempt == 0:
                try:
                    auth.refresh(timeout=timeouts.first_byte)
                except CredentialInvalidError:
                    auth.select_credential(timeout=timeouts.first_byte)
                continue
            if exc.code == 429:
                auth.expire_rate_limits()
                if attempt == 0:
                    auth.select_credential(timeout=timeouts.first_byte)
                    continue
            raise CodexError(f"Codex request failed: HTTP {exc.code}: {detail}") from exc
        except CodexStallError:
            raise
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise CodexStallError(
                    f"Codex SSE stalled during first_byte after {timeouts.first_byte:g}s idle "
                    f"(last event=none)",
                    stage="first_byte",
                    idle_timeout=timeouts.first_byte,
                    last_event_type=None,
                ) from exc
            raise CodexError(f"Codex request failed: {reason}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise CodexStallError(
                f"Codex SSE stalled during first_byte after {timeouts.first_byte:g}s idle "
                f"(last event=none)",
                stage="first_byte",
                idle_timeout=timeouts.first_byte,
                last_event_type=None,
            ) from exc
    raise CodexError("Codex request failed after token refresh")


def responses(
    body: dict,
    auth: CodexAuth | None = None,
    timeout: float | None = None,
    *,
    timeouts: StreamTimeouts | None = None,
    first_byte_timeout: float | None = None,
    thinking_idle_timeout: float | None = None,
    answering_idle_timeout: float | None = None,
    on_event=None,
) -> dict:
    if not isinstance(body, dict):
        raise TypeError("body must be a dictionary")
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise ValueError("model is required")
    if not isinstance(body.get("input"), list) or not body["input"]:
        raise ValueError("input must be a non-empty list")
    request_body = dict(body)
    request_body.pop("prompt_cache_options", None)
    request_body["input"] = copy.deepcopy(body["input"])
    request_body.setdefault("instructions", "")
    for item in request_body["input"]:
        if not isinstance(item, dict):
            continue
        if item.get("role") == "system":
            item["role"] = "developer"
        content = item.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    block.pop("prompt_cache_breakpoint", None)
    request_body["stream"] = True
    request_body.setdefault("store", False)
    request_body["tool_choice"] = "auto"
    request_body.setdefault("parallel_tool_calls", True)
    request_body.setdefault("prompt_cache_key", SESSION_ID)
    request_body.setdefault("include", ["reasoning.encrypted_content"])
    stream_timeouts = _normalize_stream_timeouts(
        timeouts,
        timeout=timeout,
        first_byte_timeout=first_byte_timeout,
        thinking_idle_timeout=thinking_idle_timeout,
        answering_idle_timeout=answering_idle_timeout,
    )
    return _request(auth or CodexAuth(), request_body, stream_timeouts, on_event=on_event)
