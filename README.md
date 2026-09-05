# codex-gateway

A pure-Python, OpenAI Responses API-compatible gateway for one or more
Codex accounts. It includes CLI utilities for signing in, managing accounts,
and checking quota.

> [!IMPORTANT]
> This project uses the ChatGPT/Codex OAuth backend. It is unofficial and is
> not affiliated with or endorsed by OpenAI.

## Features

- `POST /v1/responses` with streaming and non-streaming responses
- `GET /v1/models` for available Codex models
- `GET /v1/usage` for cached quota and reset information across the credential pool
- `GET /health` and `GET /healthz` health checks
- OAuth token refresh and reset-aware first-fill account rotation
- Response reconstruction for the Codex backend's SSE-only protocol
- `codex-auth` and `codex-quota` command-line utilities
- Python standard library only at runtime

## Installation

Python 3.10 or newer is required.

Install directly from GitHub:

```bash
python -m pip install "git+https://github.com/jacobsparts/codex-gateway.git"
```

For an isolated command-line installation, use [`pipx`](https://pipx.pypa.io/):

```bash
pipx install "git+https://github.com/jacobsparts/codex-gateway.git"
```

This installs the `codex_transport` Python package and these commands:

- `codex-gateway`
- `codex-auth`
- `codex-quota`

With a normal pip installation, the active Python environment's scripts
directory must be on `PATH`. A virtual environment or pipx handles that
without modifying the system Python installation.

For development:

```bash
git clone https://github.com/jacobsparts/codex-gateway.git
cd codex-gateway
python -m pip install -e .
```

## Authentication

All components use one standalone credential pool:

```text
~/.codex/auth.json
```

Override it by setting `CODEX_GATEWAY_CRED_FILE` to an absolute or
user-relative path. The auth utility, quota utility, and gateway all honor the
same setting.

Add an account with the device-code login flow:

```bash
codex-auth
```

The command prints a URL and one-time code, waits for authorization, then adds
or updates the account in the credential pool. Add multiple accounts by
running it again. Remove an account by its label or zero-based `cred-N` index:

```bash
codex-auth --remove cred-0
```

See all options with `codex-auth --help`.

## Running the gateway

```bash
codex-gateway
```

By default it listens on `http://127.0.0.1:8932`.

```bash
curl http://127.0.0.1:8932/health
curl http://127.0.0.1:8932/v1/models
curl http://127.0.0.1:8932/v1/usage
```

Example response request:

```bash
curl http://127.0.0.1:8932/v1/responses \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "gpt-5.6-luna",
    "input": [{"role": "user", "content": "Hello"}]
  }'
```

To require clients to authenticate to the local gateway, set a token and send
it as a Bearer token:

```bash
export CODEX_GATEWAY_TOKEN='choose-a-secret'
codex-gateway
```

## Quota

Display the quota and available manual resets saved in the local credential
pool:

```bash
codex-quota
codex-quota --json
codex-quota --account ACCOUNT_NAME
```

By default, the command reads only local state and does not require the gateway
to be running. Ask the gateway to refresh both quota and reset-credit data
before displaying it with:

```bash
codex-quota --refresh
```

Use a manual quota reset for one account:

```bash
codex-quota --reset ACCOUNT_NAME
```

The reset runs immediately. Use an account label or its zero-based `cred-N`
index. The gateway performs the reset and updates the saved quota and reset
information before the command displays it.

While running, the gateway refreshes quota when it has not been updated by request
activity for one hour, and refreshes reset-credit data daily in a background
maintenance thread. An applicable reset is used automatically when it expires within
ten minutes. The gateway also uses the earliest-expiring applicable reset when every
usable account has 5% or less remaining in its longest-duration quota window and the
reset's account is more than 24 hours from its effective quota reset. The gateway
refreshes and rechecks the relevant quota and reset data before using a reset, then
refreshes that account's state again afterward.

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `CODEX_GATEWAY_HOST` | `127.0.0.1` | Listening interface |
| `CODEX_GATEWAY_PORT` | `8932` | Listening port |
| `CODEX_GATEWAY_TOKEN` | unset | Exact Bearer token required from clients; auth is disabled when unset |
| `CODEX_GATEWAY_CRED_FILE` | `~/.codex/auth.json` | Shared credential-pool path |
| `CODEX_GATEWAY_URL` | `http://127.0.0.1:8932` | Gateway URL used by `codex-quota` |
| `CODEX_ISSUER` | `https://auth.openai.com` | OAuth issuer used by `codex-auth` |
| `CODEX_CLIENT_ID` | Codex CLI client ID | OAuth client ID used by `codex-auth` |
| `CODEX_VERSION` | bundled fallback | Codex version advertised by `codex-auth` |

## Python API

The installed transport can also be imported directly:

```python
from codex_transport import codex

response = codex.responses({
    "model": codex.MODEL,
    "input": [{"role": "user", "content": "Hello"}],
})
```

The utilities support module invocation as well:

```bash
python -m codex_transport.utility.codex_auth
python -m codex_transport.utility.codex_quota
```

## Codex backend compatibility

The gateway handles several backend-specific details:

- The backend is SSE-only, so buffered responses are assembled from stream
  events while upstream requests force `stream: true` and `store: false`.
- Terminal events can carry an empty `output`; text and function calls are
  reconstructed from their event streams.
- Unsupported prompt-cache fields are omitted before forwarding.
- Expiring access tokens are refreshed automatically.
- With multiple accounts, reset-aware first-fill rotation uses the earliest
  effective deadline across quota resets and available manual-reset expirations,
  with lower remaining quota as the tie-breaker. The final 5% of each account
  is held in reserve until every account reaches that level.
- Credential-file writes are serialized with `auth.json.lock`, and unknown
  top-level keys are preserved.
- An account whose token refresh returns HTTP 401 is marked invalid. On HTTP 429,
  the gateway expires that account's cached quota, selects again, and retries the
  request once on another available account.
- Background maintenance refreshes quota and reset credits without delaying
  Responses API requests. It uses applicable resets near expiry and, when every
  usable account's longest-duration quota window reaches its final 5%, uses the
  earliest-expiring applicable reset for an account whose effective quota reset is
  more than 24 hours away. Live state is rechecked before a reset is consumed.

## Tests

```bash
python -m pip install pytest
python -m pytest -q
```

The test suite is offline; it does not use real credentials or contact the
Codex backend.

## Related Projects

Part of a family of developer tools for agentic coding and model gateways:

- **[Code Agent](https://github.com/jacobsparts/code-agent)** — A Python REPL-native coding agent designed around lean context, persistent execution state, and infinite context via lossless turn coalescing.
- **[AgentLib](https://github.com/jacobsparts/agentlib)** — A lightweight, production-proven library for building and shipping LLM agents quickly, where composable agents are defined as Python classes—making it both simple and powerful.
- **[codex-gateway](https://github.com/jacobsparts/codex-gateway)** — Pure-Python OpenAI Responses API-compatible gateway for Codex/ChatGPT OAuth accounts with quota management, account rotation, and automated resets.
- **[cursor-gateway](https://github.com/jacobsparts/cursor-gateway)** — Pure-Python OpenAI-compatible Chat Completions gateway that wraps the Cursor Agent API with synthetic checkpoints to provide real native tool calling and cache-friendly session routing.

## License

[MIT](LICENSE)
