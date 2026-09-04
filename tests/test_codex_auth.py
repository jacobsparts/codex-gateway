import json
import pytest

from codex_transport.utility import codex_auth


def _sample_creds():
    return [
        {
            "auth_mode": "chatgpt",
            "account": "openai-primary",
            "tokens": {
                "id_token": "id1",
                "access_token": "acc1",
                "refresh_token": "rf1",
                "account_id": "acct-111",
            },
            "last_refresh": "2026-08-10T00:00:00Z",
            "email": "user1@example.com",
        },
        {
            "auth_mode": "chatgpt",
            "tokens": {
                "id_token": "id2",
                "access_token": "acc2",
                "refresh_token": "rf2",
                "account_id": "acct-222",
            },
            "last_refresh": "2026-08-11T00:00:00Z",
            "email": "user2@example.com",
        },
        {
            "auth_mode": "chatgpt",
            "tokens": {
                "id_token": "id3",
                "access_token": "acc3",
                "refresh_token": "rf3",
                "account_id": "acct-333",
            },
            "last_refresh": "2026-08-12T00:00:00Z",
            "email": "user1@example.com",
        },
        {
            "auth_mode": "chatgpt",
            "tokens": {
                "id_token": "id4",
                "access_token": "acc4",
                "refresh_token": "rf4",
                "account_id": "acct-444",
            },
            "last_refresh": "2026-08-13T00:00:00Z",
            "email": "user4@example.com",
        },
    ]


def _write_creds(path, creds):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"credentials": creds}, f, indent=2)


def test_remove_credentials_by_account_name(tmp_path):
    cred_file = str(tmp_path / "codex-auth.json")
    _write_creds(cred_file, _sample_creds())

    removed = codex_auth.remove_credentials(
        "openai-primary",
        path=cred_file,
    )
    assert len(removed) == 1
    assert removed[0]["account"] == "openai-primary"

    data = json.load(open(cred_file))
    remaining = [c["tokens"]["account_id"] for c in data["credentials"]]
    assert remaining == ["acct-222", "acct-333", "acct-444"]


def test_remove_credentials_by_cred_index(tmp_path):
    cred_file = str(tmp_path / "codex-auth.json")
    _write_creds(cred_file, _sample_creds())

    # "cred-2" matches index 2
    removed = codex_auth.remove_credentials(
        ["cred-2"],
        path=cred_file,
    )
    assert len(removed) == 1
    assert removed[0]["tokens"]["account_id"] == "acct-333"

    data = json.load(open(cred_file))
    remaining = [c["tokens"]["account_id"] for c in data["credentials"]]
    assert remaining == ["acct-111", "acct-222", "acct-444"]


def test_remove_credentials_numeric_index_does_not_match(tmp_path):
    cred_file = str(tmp_path / "codex-auth.json")
    _write_creds(cred_file, _sample_creds())

    # Plain numeric index "1" should NOT match; must be "cred-1"
    removed = codex_auth.remove_credentials(
        ["1"],
        path=cred_file,
    )
    assert len(removed) == 0

    data = json.load(open(cred_file))
    assert len(data["credentials"]) == 4


def test_remove_credentials_multiple(tmp_path):
    cred_file = str(tmp_path / "codex-auth.json")
    _write_creds(cred_file, _sample_creds())

    removed = codex_auth.remove_credentials(
        ["openai-primary", "cred-3"],
        path=cred_file,
    )
    assert len(removed) == 2
    assert removed[0]["account"] == "openai-primary"
    assert removed[1]["tokens"]["account_id"] == "acct-444"

    data = json.load(open(cred_file))
    remaining = [c["tokens"]["account_id"] for c in data["credentials"]]
    assert remaining == ["acct-222", "acct-333"]


def test_remove_credentials_missing_file():
    with pytest.raises(FileNotFoundError):
        codex_auth.remove_credentials(["cred-0"], path="/nonexistent/path/creds.json")


def test_remove_credentials_empty_identifiers(tmp_path):
    cred_file = str(tmp_path / "codex-auth.json")
    _write_creds(cred_file, _sample_creds())

    with pytest.raises(ValueError):
        codex_auth.remove_credentials([], path=cred_file)


def test_cli_remove_single(tmp_path, capsys):
    cred_file = str(tmp_path / "codex-auth.json")
    _write_creds(cred_file, _sample_creds())

    rc = codex_auth.main(["--cred-file", cred_file, "--remove", "cred-1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Removed 1 credential(s)" in out
    assert "cred-1" in out

    data = json.load(open(cred_file))
    assert len(data["credentials"]) == 3


def test_cli_remove_multiple(tmp_path, capsys):
    cred_file = str(tmp_path / "codex-auth.json")
    _write_creds(cred_file, _sample_creds())

    rc = codex_auth.main(["--cred-file", cred_file, "--remove", "openai-primary", "cred-3"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Removed 2 credential(s)" in out

    data = json.load(open(cred_file))
    assert len(data["credentials"]) == 2


def test_cli_remove_no_match(tmp_path, capsys):
    cred_file = str(tmp_path / "codex-auth.json")
    _write_creds(cred_file, _sample_creds())

    rc = codex_auth.main(["--cred-file", cred_file, "--remove", "nonexistent"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "No credentials matched" in err
