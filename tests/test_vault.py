from __future__ import annotations

import time

import pytest

from app.audit import AuditLog
from app.db import Database
from app.vault import (
    AmbiguousHost,
    DuplicateLabel,
    EntryNotFound,
    InvalidMasterPassword,
    Vault,
    VaultError,
    VaultLocked,
    host_matches,
    normalize_domains,
)

MASTER = "correct horse battery staple"


@pytest.fixture()
def vault(tmp_path):
    db = Database(tmp_path / "vault.db")
    return Vault(db, AuditLog(db), idle_seconds=3600)


def test_initialize_unlock_and_wrong_password(vault):
    assert vault.initialized() is False
    assert vault.status()["unlocked"] is False

    vault.initialize(MASTER)
    assert vault.initialized() is True
    assert vault.status()["unlocked"] is True

    vault.lock()
    assert vault.status()["unlocked"] is False

    with pytest.raises(InvalidMasterPassword):
        vault.unlock("not the password")
    with pytest.raises(VaultLocked):
        vault.get_secret_by_label("missing")

    vault.unlock(MASTER)
    assert vault.status()["unlocked"] is True


def test_entry_roundtrip_and_ciphertext_at_rest(vault):
    vault.initialize(MASTER)
    entry = vault.add_entry(
        "GitHub",
        ["https://github.com/login", "gist.github.com"],
        username="octocat",
        password="s3cret-value-42",
        url="https://github.com/login",
        notes="personal",
        totp_secret="JBSWY3DPEHPK3PXP",
    )
    assert entry["domains"] == ["github.com", "gist.github.com"]

    listed = vault.list_entries()
    assert listed[0]["label"] == "GitHub"
    assert "password" not in listed[0]
    assert "totp_secret" not in listed[0]

    secret = vault.reveal(entry["id"])
    assert secret["username"] == "octocat"
    assert secret["password"] == "s3cret-value-42"
    assert secret["notes"] == "personal"
    assert secret["has_totp"] is True
    assert "totp_secret" not in secret

    row = vault.db.one(
        "SELECT nonce, ciphertext FROM vault_entries WHERE id = ?", (entry["id"],)
    )
    assert len(row["nonce"]) == 12
    assert b"s3cret-value-42" not in row["ciphertext"]
    assert b"octocat" not in row["ciphertext"]

    with pytest.raises(DuplicateLabel):
        vault.add_entry("GitHub", ["github.com"], password="x")
    with pytest.raises(VaultError):
        vault.add_entry("No domains", [], password="x")


def test_update_and_clear_totp(vault):
    vault.initialize(MASTER)
    entry = vault.add_entry("Mail", ["mail.example.com"], password="pw")
    vault.update_entry(entry["id"], totp_secret="JBSWY3DPEHPK3PXP")
    assert vault.get_secret(entry["id"])["totp_secret"] == "JBSWY3DPEHPK3PXP"
    assert vault.reveal(entry["id"])["has_totp"] is True
    vault.update_entry(entry["id"], totp_secret="")
    assert vault.get_secret(entry["id"])["totp_secret"] == ""
    assert vault.reveal(entry["id"])["has_totp"] is False


def test_locked_vault_refuses_secret_access(vault):
    vault.initialize(MASTER)
    entry = vault.add_entry("Mail", ["mail.example.com"], password="pw")
    vault.lock()

    with pytest.raises(VaultLocked):
        vault.reveal(entry["id"])
    with pytest.raises(VaultLocked):
        vault.add_entry("Other", ["example.com"], password="pw")


def test_auto_lock_after_idle(vault):
    vault.initialize(MASTER)
    assert vault.status()["unlocked"] is True
    vault._last_activity = time.monotonic() - 7200
    status = vault.status()
    assert status["unlocked"] is False
    assert status["seconds_until_lock"] == 0


def test_change_master_password_reencrypts(vault):
    vault.initialize(MASTER)
    vault.add_entry("Bank", ["bank.example.com"], username="me", password="old-pw")

    vault.change_master_password(MASTER, "a brand new master passphrase")
    vault.lock()

    with pytest.raises(InvalidMasterPassword):
        vault.unlock(MASTER)

    vault.unlock("a brand new master passphrase")
    secret = vault.get_secret_by_label("Bank")
    assert secret["password"] == "old-pw"
    assert secret["username"] == "me"


def test_secret_for_host_prefers_exact_and_detects_ambiguity(vault):
    vault.initialize(MASTER)
    vault.add_entry("Parent", ["example.com"], username="parent", password="p")
    vault.add_entry(
        "Sub", ["login.example.com"], username="sub", password="s"
    )

    sub = vault.secret_for_host("login.example.com")
    assert sub["label"] == "Sub"
    parent = vault.secret_for_host("www.example.com")
    assert parent["label"] == "Parent"
    with pytest.raises(EntryNotFound):
        vault.secret_for_host("other.example.net")

    vault.add_entry("Sub second", ["login.example.com"], password="s2")
    with pytest.raises(AmbiguousHost):
        vault.secret_for_host("login.example.com")

    vault.lock()
    with pytest.raises(VaultLocked):
        vault.secret_for_host("www.example.com")


def test_domain_helpers():
    assert normalize_domains(
        ["https://GitHub.com/login", "*.example.org", " gist.github.com "]
    ) == ["github.com", "example.org", "gist.github.com"]
    assert host_matches("login.github.com", ["github.com"]) is True
    assert host_matches("github.com", ["github.com"]) is True
    assert host_matches("notgithub.com", ["github.com"]) is False
