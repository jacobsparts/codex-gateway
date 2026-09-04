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
