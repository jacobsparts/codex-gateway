import os

from codex_transport import codex
from codex_transport.utility import codex_auth, codex_quota


def test_defaults_are_standalone():
    expected = os.path.expanduser(os.environ.get("CODEX_GATEWAY_CRED_FILE", "~/.codex/auth.json"))
    assert codex.CRED_FILE == expected
    assert codex_auth.CRED_FILE == expected
    assert codex_quota.DEFAULT_FILE == expected


def test_auth_write_sets_private_permissions(tmp_path):
    path = tmp_path / "auth.json"
    codex_auth.save_credential({"tokens": {"account_id": "acct"}}, str(path))
    assert path.stat().st_mode & 0o777 == 0o600


def test_quota_build_entries_reads_pool_shape():
    entries = codex_quota.build_entries({"credentials": [{"account": "primary", "tokens": {}}]})
    assert len(entries) == 1
    assert entries[0]["account"] == "primary"


def _version_tuple(value):
    return tuple(int(part) for part in value.split("."))


def test_bundled_client_version_supports_gpt_6_astra():
    # The upstream model catalog declares 0.153.0 as gpt-6-astra's minimum.
    assert _version_tuple(codex.DEFAULT_CLIENT_VERSION) >= (0, 153, 0)
    assert codex.CLIENT_VERSION == codex_auth.CODEX_VERSION


def test_transport_advertises_client_version_consistently():
    class Auth:
        access_token = "token"
        account_id = "account"

    headers = codex._headers(Auth())
    assert headers["Version"] == codex.CLIENT_VERSION
    assert headers["User-Agent"].startswith(f"codex_cli_rs/{codex.CLIENT_VERSION}")
