# codex-gateway

A lightweight, dependency-free HTTP gateway for the Codex (ChatGPT backend)
OAuth transport. It exposes an OpenAI Responses API-compatible interface,
supports streaming and buffered responses, rotates across a local credential
pool, and includes commands for authentication and quota inspection.

> [!IMPORTANT]
> This project uses the ChatGPT/Codex OAuth backend. It is unofficial and is
> not affiliated with or endorsed by OpenAI.

## Features

- `POST /v1/responses` with streaming and non-streaming responses
- `GET /v1/models` for the configured Codex model
- `GET /v1/usage` for quota information across the credential pool
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

The gateway refreshes expiring access tokens automatically. Account selection
uses reset-aware first-fill rotation: it drains the quota that expires first,
with lower remaining quota as the tie-breaker. It keeps the final 5% of every
account in reserve until all accounts have reached that threshold, then uses
those reserves in the same order.

Writes are serialized through `auth.json.lock`, and unknown top-level keys in
the pool are preserved. An account rejected during token refresh with HTTP
401 is marked invalid. HTTP 429 expires the selected account's cached quota so
the next request refreshes quota before selecting an account.

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

The gateway records Codex rate-limit headers in the credential pool as
requests complete. Display the latest saved values with:

```bash
codex-quota
codex-quota --json
codex-quota --account ACCOUNT_NAME
```

The saved values may be stale until that account serves another request. The
`GET /v1/usage` endpoint instead queries the upstream usage endpoint for every
usable account.

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `CODEX_GATEWAY_HOST` | `127.0.0.1` | Listening interface |
| `CODEX_GATEWAY_PORT` | `8932` | Listening port |
| `CODEX_GATEWAY_TOKEN` | unset | Exact Bearer token required from clients; auth is disabled when unset |
| `CODEX_GATEWAY_CRED_FILE` | `~/.codex/auth.json` | Shared credential-pool path |
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

## Tests

```bash
python -m pip install pytest
python -m pytest -q
```

The test suite is offline; it does not use real credentials or contact the
Codex backend.

## License

[MIT](LICENSE)
