#!/usr/bin/env python3


from __future__ import annotations
import argparse
import base64
from contextlib import contextmanager
import datetime
import fcntl
import json
import os
import pathlib
import sys
import time
import platform
import urllib.error
import urllib.request
import urllib.parse
from typing import Optional

CRED_FILE = os.path.expanduser(
    os.environ.get("CODEX_GATEWAY_CRED_FILE", "~/.codex/auth.json")
)

DEFAULT_ISSUER = "https://auth.openai.com"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
MAX_WAIT_SECS = 15 * 60


def _detect_codex_version() -> str:
    return os.environ.get("CODEX_VERSION", "0.146.0").strip() or "0.146.0"

CODEX_VERSION = _detect_codex_version()
USER_AGENT = f"codex_cli_rs/{CODEX_VERSION} ({platform.system()} {platform.machine()}) codex"
ORIGINATOR = "codex_cli_rs"
ANSI_BLUE = "\x1b[94m"
ANSI_GRAY = "\x1b[90m"
ANSI_RESET = "\x1b[0m"


def _b64url_decode(s: str) -> bytes:
    s = s.strip()
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)

def jwt_payload(jwt: str) -> dict:
    parts = jwt.split(".")
    if len(parts) != 3 or not all(parts):
        raise ValueError("invalid JWT format (expected header.payload.signature)")
    payload_b64 = parts[1]
    raw = _b64url_decode(payload_b64)
    return json.loads(raw)

def jwt_auth_claims(jwt: str) -> dict:
    try:
        payload = jwt_payload(jwt)
    except Exception as e:
        print(f"Invalid JWT format while extracting claims: {e}", file=sys.stderr)
        return {}
    auth = payload.get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        return auth
    if auth is not None:
        print("JWT payload 'https://api.openai.com/auth' is not an object", file=sys.stderr)
    else:
        print("JWT payload missing expected 'https://api.openai.com/auth' object", file=sys.stderr)
    return {}

def ensure_workspace_allowed(expected: Optional[list[str]], id_token: str) -> None:
    if not expected:
        return
    claims = jwt_auth_claims(id_token)
    actual = claims.get("chatgpt_account_id")
    if not isinstance(actual, str) or not actual:
        raise ValueError(
            "Login is restricted to a specific workspace, but the token did not include "
            "a chatgpt_account_id claim."
        )
    if actual not in expected:
        raise ValueError(
            f"Login is restricted to a specific workspace, but the token belongs to a different "
            f"workspace (expected one of {expected}, got {actual})."
        )

def parse_chatgpt_jwt_claims_summary(jwt: str) -> dict:
    try:
        payload = jwt_payload(jwt)
    except Exception:
        return {}
    email = payload.get("email")
    profile = payload.get("https://api.openai.com/profile") or {}
    if not email and isinstance(profile, dict):
        email = profile.get("email")
    auth = payload.get("https://api.openai.com/auth") or {}
    if not isinstance(auth, dict):
        auth = {}
    return {
        "email": email,
        "chatgpt_plan_type": auth.get("chatgpt_plan_type"),
        "chatgpt_user_id": auth.get("chatgpt_user_id") or auth.get("user_id"),
        "chatgpt_account_id": auth.get("chatgpt_account_id"),
        "chatgpt_account_is_fedramp": bool(auth.get("chatgpt_account_is_fedramp")),
    }


def _decode_response(raw: str, empty: dict | str) -> dict | str:
    if not raw.strip():
        return empty
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _http_post(
    url: str,
    data: bytes,
    content_type: str,
    timeout: float,
    extra_headers: Optional[dict] = None,
) -> tuple[int, dict | str]:
    headers = {
        "Content-Type": content_type,
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "originator": ORIGINATOR,
    }
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, _decode_response(raw, {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
        return e.code, _decode_response(raw, raw)
    except Exception as e:
        raise IOError(f"POST {url} transport failure: {e}") from e


def _http_json_post(
    url: str,
    body_obj: dict,
    extra_headers: Optional[dict] = None,
    timeout: float = 30,
) -> tuple[int, dict | str]:
    return _http_post(
        url,
        json.dumps(body_obj).encode("utf-8"),
        "application/json",
        timeout,
        extra_headers,
    )


def _http_form_post(
    url: str,
    form: dict[str, str],
    timeout: float = 30,
) -> tuple[int, dict | str]:
    return _http_post(
        url,
        urllib.parse.urlencode(form).encode("utf-8"),
        "application/x-www-form-urlencoded",
        timeout,
    )


def request_user_code(issuer: str, client_id: str) -> dict:
    base = issuer.rstrip("/")
    api_base = f"{base}/api/accounts"
    url = f"{api_base}/deviceauth/usercode"

    status, body = _http_json_post(url, {"client_id": client_id})
    if status == 404:
        raise FileNotFoundError(
            "device code login is not enabled for this Codex server. "
            "Use the browser login or verify the server URL."
        )
    if not (200 <= status < 300):
        raise IOError(f"deviceauth/usercode returned status {status}: {body}")
    if not isinstance(body, dict):
        raise IOError(f"deviceauth/usercode returned non-JSON: {body}")

    device_auth_id = body.get("device_auth_id")
    user_code = body.get("user_code") if "user_code" in body else body.get("usercode")
    interval_raw = body.get("interval", "5")
    if not device_auth_id or not user_code:
        raise IOError(f"deviceauth/usercode missing fields: {body}")

    try:
        interval = int(str(interval_raw).strip())
    except Exception:
        print(f"Warning: could not parse interval {interval_raw!r}, defaulting to 5", file=sys.stderr)
        interval = 5
    verification_url = f"{base}/codex/device"
    return {
        "device_auth_id": device_auth_id,
        "user_code": user_code,
        "interval": interval,
        "verification_url": verification_url,
    }

def device_code_prompt(verification_url: str, code: str) -> str:
    return (
        f"Enable device code authorization for Codex\n"
        f"Personal: https://chatgpt.com/#settings/Security\n"
        f"Business: https://chatgpt.com/admin/permissions\n"
        f"\nFollow these steps to sign in with ChatGPT using device code authorization:\n"
        f"\n1. Open this link in your browser and sign in to your account\n"
        f"   {ANSI_BLUE}{verification_url}{ANSI_RESET}\n"
        f"\n2. Enter this one-time code {ANSI_GRAY}(expires in 15 minutes){ANSI_RESET}\n"
        f"   {ANSI_BLUE}{code}{ANSI_RESET}\n"
    )

def poll_for_token(issuer: str, device_auth_id: str, user_code: str, interval: int) -> dict:
    base = issuer.rstrip("/")
    api_base = f"{base}/api/accounts"
    url = f"{api_base}/deviceauth/token"
    body = {"device_auth_id": device_auth_id, "user_code": user_code}
    start = time.monotonic()
    max_wait = MAX_WAIT_SECS
    attempt = 0
    while True:
        elapsed = time.monotonic() - start
        if elapsed >= max_wait:
            raise TimeoutError(f"device auth timed out after {max_wait//60} minutes")
        attempt += 1
        status, resp_body = _http_json_post(url, body)
        if 200 <= status < 300:
            if not isinstance(resp_body, dict):
                raise IOError(f"deviceauth/token returned non-JSON on success: {resp_body}")

            if "authorization_code" not in resp_body or "code_verifier" not in resp_body:
                raise IOError(f"deviceauth/token missing fields: {resp_body}")
            return resp_body
        if status in (403, 404):

            remaining = max_wait - (time.monotonic() - start)
            sleep_for = min(float(interval), remaining)
            if sleep_for <= 0:
                raise TimeoutError(f"device auth timed out after {max_wait//60} minutes")
            if attempt == 1:
                print(f"Waiting for you to enter the code in the browser (polling every {interval}s) ...", file=sys.stderr)
            time.sleep(sleep_for)
            continue

        raise IOError(f"deviceauth/token returned status {status}: {resp_body}")

def exchange_code_for_tokens(issuer: str, client_id: str, redirect_uri: str, code_verifier: str, code: str) -> dict:
    base = issuer.rstrip("/")
    token_endpoint = f"{base}/oauth/token"
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }

    status, body = _http_form_post(token_endpoint, form)
    if not (200 <= status < 300):

        detail = body if isinstance(body, str) else json.dumps(body)
        raise IOError(f"token endpoint returned status {status}: {detail}")
    if not isinstance(body, dict):
        raise IOError(f"token endpoint returned non-JSON: {body}")
    for k in ("id_token", "access_token", "refresh_token"):
        if k not in body or not body[k]:
            raise IOError(f"token endpoint missing {k}: {body}")
    return {"id_token": body["id_token"], "access_token": body["access_token"], "refresh_token": body["refresh_token"]}


def resolve_codex_home(cli_value: Optional[str] = None) -> pathlib.Path:
    if cli_value:
        return pathlib.Path(cli_value).expanduser().resolve()
    env = os.environ.get("CODEX_HOME")
    if env and env.strip():
        return pathlib.Path(env).expanduser().resolve()
    return pathlib.Path.home() / ".codex"

def build_auth_json(
    id_token: str,
    access_token: str,
    refresh_token: str,
    api_key: Optional[str] = None,
) -> dict:

    summary = parse_chatgpt_jwt_claims_summary(id_token)
    account_id = summary.get("chatgpt_account_id")
    if isinstance(account_id, str):
        account_id = account_id.strip() or None
    else:
        account_id = None
    email = summary.get("email")
    if isinstance(email, str):
        email = email.strip() or None
    else:
        email = None
    now = datetime.datetime.now(datetime.timezone.utc)
    last_refresh = now.isoformat().replace("+00:00", "Z")
    tokens_obj: dict = {
        "id_token": id_token,
        "access_token": access_token,
        "refresh_token": refresh_token,
    }
    if account_id:
        tokens_obj["account_id"] = account_id
    auth_obj: dict = {
        "auth_mode": "chatgpt",
        "tokens": tokens_obj,
        "last_refresh": last_refresh,
    }
    if email:
        auth_obj["email"] = email
    if api_key and api_key.strip():
        auth_obj["OPENAI_API_KEY"] = api_key.strip()
    return auth_obj

def save_auth_json(
    codex_home: pathlib.Path,
    id_token: str,
    access_token: str,
    refresh_token: str,
    api_key: Optional[str] = None,
) -> pathlib.Path:

    auth_obj = build_auth_json(id_token, access_token, refresh_token, api_key)
    codex_home.mkdir(parents=True, exist_ok=True)
    dest = codex_home / "auth.json"

    with locked_open(dest, "w", exclusive=True) as f:
        json.dump(auth_obj, f, indent=2)
        f.write("\n")
        try:
            os.chmod(dest, 0o600)
        except Exception:
            pass
    return dest


def print_auth_config_and_claims(id_token: str, access_token: str, refresh_token: str, api_key: Optional[str] = None) -> dict:
    auth_obj = build_auth_json(id_token, access_token, refresh_token, api_key)
    print(json.dumps(auth_obj, indent=2))
    return auth_obj


@contextmanager
def locked_open(path=CRED_FILE, mode="r", *, exclusive=False):
    path = os.fspath(path)
    lock_path = path + ".lock"
    os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    locked = False
    try:
        try:
            os.fchmod(fd, 0o600)
        except Exception:
            pass

        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        locked = True
        with open(path, mode, encoding="utf-8") as f:
            if exclusive:
                try:
                    os.fchmod(f.fileno(), 0o600)
                except OSError:
                    pass
            yield f
    finally:
        if locked:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def save_credential(auth: dict, path: str = CRED_FILE) -> str:
    path = os.path.expanduser(path)
    with locked_open(path, "a+", exclusive=True) as f:
        f.seek(0)
        try:
            data = json.load(f)
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        credentials = data.get("credentials")
        if not isinstance(credentials, list):
            credentials = []
            data["credentials"] = credentials

        target_account_id = auth.get("tokens", {}).get("account_id")
        target_email = auth.get("email")
        match = None
        if target_account_id:
            for cred in credentials:
                cred_tokens = cred.get("tokens") if isinstance(cred, dict) else None
                cred_account_id = (
                    cred_tokens.get("account_id")
                    if isinstance(cred_tokens, dict)
                    else None
                )
                if cred_account_id == target_account_id and cred.get("email") == target_email:
                    match = cred
                    break

        if match is None:
            credentials.append(auth)
        else:
            match["tokens"] = auth["tokens"]
            match["last_refresh"] = auth["last_refresh"]
            if "email" in auth:
                match["email"] = auth["email"]
            match.pop("invalid", None)

        f.seek(0)
        f.truncate()
        json.dump(data, f, indent=2)
        f.write("\n")
    return path


def run_device_code_login(
    issuer: str = DEFAULT_ISSUER,
    client_id: str = CLIENT_ID,
    forced_workspace_ids: Optional[list[str]] = None,
    cred_file: str = CRED_FILE,
) -> str:

    issuer = issuer.rstrip("/")


    uc = request_user_code(issuer, client_id)
    verification_url = uc["verification_url"]
    user_code = uc["user_code"]
    interval = uc["interval"]
    device_auth_id = uc["device_auth_id"]


    prompt = device_code_prompt(verification_url, user_code)
    print(prompt)


    return complete_device_code_login(
        issuer,
        client_id,
        device_auth_id,
        user_code,
        interval,
        cred_file,
        forced_workspace_ids,
    )

def request_device_code_only(issuer: str = DEFAULT_ISSUER, client_id: str = CLIENT_ID) -> dict:
    return request_user_code(issuer, client_id)

def complete_device_code_login(
    issuer: str,
    client_id: str,
    device_auth_id: str,
    user_code: str,
    interval: int,
    cred_file: str = CRED_FILE,
    forced_workspace_ids: Optional[list[str]] = None,
) -> str:
    poll_resp = poll_for_token(issuer, device_auth_id, user_code, interval)
    redirect_uri = f"{issuer.rstrip('/')}/deviceauth/callback"
    tokens = exchange_code_for_tokens(issuer, client_id, redirect_uri, poll_resp["code_verifier"], poll_resp["authorization_code"])
    if forced_workspace_ids:
        ensure_workspace_allowed(forced_workspace_ids, tokens["id_token"])
    auth = build_auth_json(tokens["id_token"], tokens["access_token"], tokens["refresh_token"])
    return save_credential(auth, cred_file)


def _matches_account_selector(selector: str, account_name: Optional[str], index: int) -> bool:
    s = selector.strip()
    if account_name and s == account_name:
        return True
    if s.lower() == f"cred-{index}".lower():
        return True
    return False


def remove_credentials(
    identifiers: list[str] | str,
    *,
    path: str = CRED_FILE,
) -> list[dict]:
    """Remove credentials matching given identifier(s) (account name or cred-N).

    Returns list of removed credentials.
    """
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Credentials file not found: {path}")

    if isinstance(identifiers, str):
        identifiers = [identifiers]
    target_ids = [i for i in (identifiers or []) if i]

    if not target_ids:
        raise ValueError("At least one identifier (account name or cred-N) is required.")

    removed: list[dict] = []

    with locked_open(path, "r+", exclusive=True) as f:
        f.seek(0)
        try:
            data = json.load(f)
        except Exception as exc:
            raise ValueError(f"Could not parse credentials file {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"Invalid format in credentials file {path}: root is not an object")

        credentials = data.get("credentials")
        if not isinstance(credentials, list):
            credentials = []

        kept: list[dict] = []
        for idx, cred in enumerate(credentials):
            if not isinstance(cred, dict):
                kept.append(cred)
                continue

            cred_account = cred.get("account")

            match = False
            for ident in target_ids:
                if _matches_account_selector(ident, cred_account, idx):
                    match = True
                    break

            if match:
                cred_copy = dict(cred)
                cred_copy["_index"] = idx
                removed.append(cred_copy)
            else:
                kept.append(cred)

        if removed:
            data["credentials"] = kept
            f.seek(0)
            f.truncate()
            json.dump(data, f, indent=2)
            f.write("\n")

    return removed


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Headless Codex device-code login and credential management.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--issuer", default=os.environ.get("CODEX_ISSUER", DEFAULT_ISSUER),
                   help="Issuer base URL (default https://auth.openai.com, or $CODEX_ISSUER)")
    p.add_argument("--client-id", default=os.environ.get("CODEX_CLIENT_ID", CLIENT_ID),
                   help="OAuth client_id (default app_EMoamEEZ73f0CkXaXp7hrann, or $CODEX_CLIENT_ID)")
    p.add_argument("--cred-file", default=CRED_FILE,
                   help=f"Path to the pooled auth file (default {CRED_FILE})")
    p.add_argument("--workspace-id", action="append", dest="workspace_ids", default=None,
                   help="Optional workspace restriction (may be repeated). Mirrors forced_chatgpt_workspace_id.")
    p.add_argument("--print-only", action="store_true",
                   help="Only request the code and print verification_url + user_code, do not poll.")
    p.add_argument("--json", action="store_true",
                   help="When used with --print-only, emit JSON {verification_url, user_code, device_auth_id, interval}")
    p.add_argument("--remove", dest="remove_targets", nargs="+", metavar="IDENTIFIER", default=None,
                   help="Remove credential(s) matching account name or cred-N from credentials file")
    return p


def _describe_credential(cred: dict) -> str:
    account = cred.get("account")
    idx = cred.get("_index")
    label = account or (f"cred-{idx}" if idx is not None else None)

    email = cred.get("email") or "<no email>"
    tokens = cred.get("tokens") or {}
    account_id = tokens.get("account_id") if isinstance(tokens, dict) else None
    last_refresh = cred.get("last_refresh")
    bits = []
    if label:
        bits.append(f"account={label}")
    bits.append(f"email={email}")
    bits.append(f"account_id={account_id or '<none>'}")
    if last_refresh:
        bits.append(f"last_refresh={last_refresh}")
    return ", ".join(bits)


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    cred_file = os.path.expanduser(args.cred_file)

    if args.remove_targets is not None:
        identifiers = list(args.remove_targets)
        if not identifiers:
            print("Error: --remove requires at least one identifier (account name or cred-N).", file=sys.stderr)
            return 2

        try:
            removed = remove_credentials(
                identifiers=identifiers,
                path=cred_file,
            )
        except FileNotFoundError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("\nCancelled by user.", file=sys.stderr)
            return 130

        if not removed:
            print(f"No credentials matched in {cred_file}: {', '.join(identifiers)}", file=sys.stderr)
            return 1

        print(f"Removed {len(removed)} credential(s) from {cred_file}:")
        for cred in removed:
            print(f"  - {_describe_credential(cred)}")
        return 0

    issuer = args.issuer.rstrip("/")
    client_id = args.client_id

    try:
        if args.print_only:
            uc = request_user_code(issuer, client_id)
            if args.json:
                print(json.dumps(uc, indent=2))
            else:
                print(device_code_prompt(uc["verification_url"], uc["user_code"]))
            return 0

        dest = run_device_code_login(
            issuer=issuer,
            client_id=client_id,
            forced_workspace_ids=args.workspace_ids,
            cred_file=cred_file,
        )
        print(f"Credentials saved to {dest}", file=sys.stderr)
        return 0

    except FileNotFoundError as e:
        print(f"Device code not supported: {e}", file=sys.stderr)
        print("Hint: use browser login (`codex login`) or verify issuer URL.", file=sys.stderr)
        return 2
    except TimeoutError as e:
        print(f"Timed out: {e}", file=sys.stderr)
        return 3
    except (IOError, ValueError) as e:
        print(f"Login failed: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled by user.", file=sys.stderr)
        return 130

if __name__ == "__main__":
    raise SystemExit(main())
