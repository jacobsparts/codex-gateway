#!/usr/bin/env python3
"""Codex quota display backed by the gateway's local credential cache.

The utility asks the local gateway to refresh stale data, waits for that request
to finish, then renders the quota and reset data persisted in auth.json.

Data source mirrors codex_transport.codex.CodexAuth rate_limits:
    rate_limits = {
        "fetched_at": <unix ts>,
        "limits": {
            "codex_primary": {"used_percent": 98.0, "reset_at": 1788400450},
            ...
        }
    }
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import time

DEFAULT_FILE = os.path.expanduser(
    os.environ.get("CODEX_GATEWAY_CRED_FILE", "~/.codex/auth.json")
)
DEFAULT_GATEWAY = os.environ.get("CODEX_GATEWAY_URL", "http://127.0.0.1:8932")
GATEWAY_TOKEN = os.environ.get("CODEX_GATEWAY_TOKEN")

# ── colour helpers ─────────────────────────────────────────────────────────────
try:
    USE_COLOR = sys.stdout.isatty()  # type: ignore[attr-defined]
except Exception:
    USE_COLOR = False

def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if USE_COLOR else s

def bold(s: str) -> str:   return _c("1", s)
def green(s: str) -> str:  return _c("32", s)
def yellow(s: str) -> str: return _c("33", s)
def red(s: str) -> str:    return _c("31", s)
def dim(s: str) -> str:    return _c("2", s)
def cyan(s: str) -> str:   return _c("36", s)

def bar(pct: float | int | None, width: int = 20) -> str:
    if pct is None:
        return dim("░" * width)
    try:
        p = float(pct)
    except Exception:
        return dim("░" * width)
    p = max(0, min(100, p))
    filled = round(p / 100 * width)
    colour = green if p >= 70 else (yellow if p >= 30 else red)
    return colour("█" * filled) + dim("░" * (width - filled))

# ── time formatting (mirrors cpa-quota) ─────────────────────────────────────────
def fmt_remaining(ts) -> str:
    if not ts:
        return "-"
    try:
        if isinstance(ts, (int, float)) and ts > 1e9:
            dt = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
        else:
            dt = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc).astimezone()
        delta = dt - datetime.datetime.now(dt.tzinfo)
        total = max(0, int(delta.total_seconds()))
        days, rem = divmod(total, 86400)
        hours, rem = divmod(rem, 3600)
        minutes = rem // 60
        if days:
            return f"{days}d {hours}h {minutes}m"
        if hours:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"
    except Exception:
        return str(ts)[:16]

def fmt_ts(ts) -> str:
    if not ts:
        return "-"
    try:
        if isinstance(ts, (int, float)) and ts > 1e9:
            dt = datetime.datetime.fromtimestamp(ts)
        else:
            dt = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt.strftime("%m/%d %H:%M")
    except Exception:
        return str(ts)[:16]

def fmt_age(fetched_at) -> str:
    if not isinstance(fetched_at, (int, float)):
        return "-"
    try:
        dt = datetime.datetime.fromtimestamp(fetched_at, datetime.timezone.utc)
        delta = datetime.datetime.now(datetime.timezone.utc) - dt
        total = int(delta.total_seconds())
        if total < 0:
            total = 0
        days, rem = divmod(total, 86400)
        hours, rem = divmod(rem, 3600)
        mins = rem // 60
        if days:
            return f"{days}d {hours}h ago"
        if hours:
            return f"{hours}h {mins}m ago"
        return f"{mins}m ago"
    except Exception:
        return "-"

# ── JWT helpers ─────────────────────────────────────────────────────────────────
def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

def _jwt_payload(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = json.loads(_b64url_decode(parts[1]))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}

def _extract_account_info(cred: dict) -> dict:
    """Return {email, plan, account_id, name, exp} from tokens."""
    tokens = cred.get("tokens") or {}
    id_token = tokens.get("id_token") or ""
    access_token = tokens.get("access_token") or ""
    account_id = tokens.get("account_id") or ""

    payload = _jwt_payload(id_token) if id_token else {}
    access_payload = _jwt_payload(access_token) if access_token else {}
    auth = payload.get("https://api.openai.com/auth")
    auth = auth if isinstance(auth, dict) else {}

    if not account_id:
        access_auth = access_payload.get("https://api.openai.com/auth")
        access_auth = access_auth if isinstance(access_auth, dict) else {}
        account_id = access_auth.get("chatgpt_account_id") or ""
        if not auth.get("chatgpt_plan_type"):
            auth = {**access_auth, **auth}

    email = payload.get("email") or ""
    if not email:
        profile = access_payload.get("https://api.openai.com/profile")
        profile = profile if isinstance(profile, dict) else {}
        email = profile.get("email") or access_payload.get("email") or ""

    plan = (auth.get("chatgpt_plan_type") or "").strip() if isinstance(auth, dict) else ""
    name = payload.get("name") or ""
    # expiry
    exp = payload.get("exp")
    return {
        "email": email,
        "plan": plan or None,
        "account_id": account_id or auth.get("chatgpt_account_id") or "",
        "name": name,
        "exp": exp,
        "access_exp": access_payload.get("exp"),
        "auth": auth,
    }

# ── label mapping ───────────────────────────────────────────────────────────────
_LABEL_MAP = {
    "codex_primary": "5h",
    "codex_secondary": "7d",
    "code_review_primary": "Review 5h",
    "code_review_secondary": "Review 7d",
    "codex_code_review_primary": "Review 5h",
    "codex_code_review_secondary": "Review 7d",
}

def label_for_key(key: str) -> str:
    if key in _LABEL_MAP:
        return _LABEL_MAP[key]
    # generic: replace underscores, handle primary/secondary suffix
    # e.g. "my_feature_primary" -> "my_feature 5h"
    if key.endswith("_primary"):
        base = key[: -len("_primary")]
        return f"{base} 5h"
    if key.endswith("_secondary"):
        base = key[: -len("_secondary")]
        return f"{base} 7d"
    return key

# ── loading ─────────────────────────────────────────────────────────────────────
def refresh_cache(gateway: str, account: str | None = None, reset: bool = False) -> None:
    headers = {"Accept": "application/json"}
    if GATEWAY_TOKEN:
        headers["Authorization"] = "Bearer " + GATEWAY_TOKEN
    if reset:
        request = urllib.request.Request(
            gateway.rstrip("/") + "/v1/usage/reset",
            data=json.dumps({"account": account}).encode(),
            headers={**headers, "Content-Type": "application/json"},
            method="POST",
        )
    else:
        query = {"resets": "1"}
        if account:
            query["account"] = account
        request = urllib.request.Request(
            gateway.rstrip("/") + "/v1/usage?" + urllib.parse.urlencode(query),
            headers=headers,
        )
    try:
        with urllib.request.urlopen(request) as response:
            json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"Gateway returned HTTP {exc.code}: {detail}") from exc
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Could not refresh quota through {gateway}: {exc}") from exc


def load_auth_file(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"Error: no auth file at {path}", file=sys.stderr)
        sys.exit(1)
    except (OSError, json.JSONDecodeError) as e:
        print(f"Error reading {path}: {e}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(data, dict) or not isinstance(data.get("credentials"), list):
        print(f"Error: {path} must contain {{'credentials': [...]}}", file=sys.stderr)
        sys.exit(1)
    if not data["credentials"]:
        print(f"Error: no credentials in {path}", file=sys.stderr)
        sys.exit(1)
    return data

def build_entries(data: dict) -> list[dict]:
    entries = []
    for idx, cred in enumerate(data.get("credentials") or []):
        if not isinstance(cred, dict):
            continue
        account = cred.get("account") or f"cred-{idx}"
        info = _extract_account_info(cred)
        # rate limits
        rl = cred.get("rate_limits") or {}
        fetched_at = rl.get("fetched_at") if isinstance(rl, dict) else None
        limits = rl.get("limits") if isinstance(rl, dict) else None
        if not isinstance(limits, dict):
            limits = {}
        windows = []
        for key, val in limits.items():
            if not isinstance(val, dict):
                continue
            used = val.get("used_percent")
            reset_at = val.get("reset_at")
            if used is None or reset_at is None:
                continue
            try:
                used_f = float(used)
                remaining = max(0, min(100, round(100 - used_f)))
            except (TypeError, ValueError):
                used_f = None
                remaining = None
            windows.append({
                "key": key,
                "label": label_for_key(key),
                "used_percent": used_f,
                "remaining_pct": remaining,
                "reset_at": reset_at,
                "reset": fmt_remaining(reset_at),
            })
        # sort: primary-like first, then by label
        windows.sort(key=lambda w: (0 if "5h" in w["label"] else 1, w["label"]))

        reset_data = cred.get("reset_credits") or {}
        reset_credits = [
            credit
            for credit in reset_data.get("credits", [])
            if isinstance(credit, dict)
            and credit.get("status") == "available"
            and credit.get("reset_type") == "codex_rate_limits"
        ]

        entries.append({
            "index": idx,
            "account": account,
            "email": info.get("email"),
            "plan": info.get("plan"),
            "account_id": info.get("account_id"),
            "name": info.get("name"),
            "invalid": bool(cred.get("invalid")),
            "last_refresh": cred.get("last_refresh"),
            "fetched_at": fetched_at,
            "fetched_age": fmt_age(fetched_at) if fetched_at else "-",
            "available_resets": reset_data.get("available_count", len(reset_credits)),
            "reset_credits": reset_credits,
            "windows": windows,
            "raw": cred,
            "access_exp": info.get("access_exp"),
            "id_exp": info.get("exp"),
        })
    return entries

# ── display (mirrors cpa-quota print_section) ───────────────────────────────────
def print_section(entries: list[dict]) -> None:
    print()
    print(bold("═══ Codex Quota ═══"))
    if not entries:
        print(dim("  (no credentials)"))
        return
    for e in entries:
        # label: account + email
        label = e.get("account") or f"cred-{e['index']}"
        email = e.get("email")
        if email and email != label:
            label = f"{label} ({email})"
        status = red(" [INVALID]") if e.get("invalid") else ""
        fetched = ""
        if e.get("fetched_at"):
            stale = ""
            try:
                if time.time() - float(e["fetched_at"]) > 3600:
                    stale = yellow(" (stale >1h)")
            except Exception:
                pass
            fetched = f"  Fetched: {dim(e['fetched_age'])}{stale}"
        print(
            f"\n  {cyan(label)}{status}  "
            f"Plan: {bold(e.get('plan') or 'unknown')}{fetched}"
        )
        for number, credit in enumerate(e["reset_credits"], 1):
            expires_at = credit.get("expires_at")
            print(f"      Reset {number}: expires {fmt_ts(expires_at)} ({fmt_remaining(expires_at)})")
        if e.get("invalid"):
            print(f"    {red('✗ marked invalid — needs re-auth')}")
            continue
        windows = e.get("windows") or []
        if not windows:
            print(f"    {dim('(no quota data)')}")
            continue
        for w in windows:
            pct = w.get("remaining_pct")
            reset = w.get("reset") or "-"
            label_w = w.get("label") or w.get("key") or "?"
            pct_str = f"{pct:3d}%" if pct is not None else " --"
            print(f"    {bar(pct)} {pct_str}  {label_w:<20s} reset {reset}")

def main() -> None:
    ap = argparse.ArgumentParser(description="Show cached Codex quota after asking the gateway to refresh stale data")
    ap.add_argument("--file", dest="file", default=DEFAULT_FILE, help="path to the pooled auth file (default ~/.codex/auth.json)")
    ap.add_argument("--gateway", default=DEFAULT_GATEWAY, help="codex-gateway URL")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--account", default=None, help="show one account by label or cred-N")
    group.add_argument("--reset", default=None, metavar="ACCOUNT", help="immediately use one manual reset")
    ap.add_argument("--json", dest="json_out", action="store_true", help="output JSON instead of coloured bars")
    ap.add_argument("--no-color", dest="no_color", action="store_true", help="disable ANSI colours")
    args = ap.parse_args()

    global USE_COLOR
    if args.no_color:
        USE_COLOR = False
    # also respect NO_COLOR env
    if os.environ.get("NO_COLOR"):
        USE_COLOR = False

    account = args.reset or args.account
    try:
        refresh_cache(args.gateway, account=account, reset=args.reset is not None)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    path = os.path.expanduser(args.file)
    data = load_auth_file(path)
    entries = build_entries(data)

    if account:
        entries = [e for e in entries if e["account"] == account or f"cred-{e['index']}" == account]
        if not entries:
            print(dim(f"No matching account '{account}'"), file=sys.stderr)
            sys.exit(1)

    if args.json_out:
        # strip raw for cleaner output, but include windows
        fields = (
            "account",
            "email",
            "plan",
            "account_id",
            "invalid",
            "last_refresh",
            "fetched_at",
            "available_resets",
            "reset_credits",
            "windows",
        )
        out = [{key: entry[key] for key in fields} for entry in entries]
        print(json.dumps(out, indent=2, default=str))
        return

    print_section(entries)
    print()

if __name__ == "__main__":
    main()