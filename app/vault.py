"""Encrypted credential vault.

Design notes
------------
* The master password is never stored. A 32-byte key is derived with Argon2id
  (64 MiB, t=3, p=2) and a random 16-byte salt.
* Every entry is sealed separately with AES-256-GCM. The entry id is used as
  additional authenticated data, so rows cannot be swapped around.
* The key lives in memory only, is zeroised on lock, and auto-locks after
  inactivity.
* Labels and allowed domains are stored in clear so the UI can list entries
  while locked. Usernames, passwords, URLs and notes are always encrypted.
* Secret values are never returned to the language model. The only agent tool
  is ``vault_fill``, which types them into a page whose host is domain-bound
  to the entry.
"""
from __future__ import annotations

import json
import secrets
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import urlparse

import argon2
from argon2.low_level import Type as Argon2Type, hash_secret_raw
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .audit import AuditLog
from .db import Database, now_ts

CANARY = b"muse-vault-canary-v1"
CANARY_AAD = b"muse-vault-canary"
KEY_LEN = 32
NONCE_LEN = 12


class VaultError(Exception):
    pass


class VaultLocked(VaultError):
    pass


class InvalidMasterPassword(VaultError):
    pass


class VaultAlreadyInitialized(VaultError):
    pass


class DuplicateLabel(VaultError):
    pass


class EntryNotFound(VaultError):
    pass


class AmbiguousHost(VaultError):
    def __init__(self, host: str, labels: List[str]) -> None:
        self.host = host
        self.labels = labels
        super().__init__(
            "Multiple vault entries are bound to %s: %s. Choose one by label."
            % (host, ", ".join(labels))
        )


def normalize_domain(value: str) -> str:
    value = (value or "").strip().lower()
    if not value:
        return ""
    if "://" in value:
        value = urlparse(value).hostname or ""
    else:
        value = value.split("/", 1)[0]
    if value.startswith("*."):
        value = value[2:]
    value = value.lstrip(".")
    if value.count(":") == 1:
        value = value.split(":", 1)[0]
    return value.strip()


def normalize_domains(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    for raw in values:
        domain = normalize_domain(raw)
        if domain and domain not in result:
            result.append(domain)
    return result


def host_matches(host: str, domains: Sequence[str]) -> bool:
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False
    for domain in domains:
        if host == domain or host.endswith("." + domain):
            return True
    return False


class Vault:
    KDF = "argon2id"

    def __init__(
        self,
        db: Database,
        audit: AuditLog,
        idle_seconds: int = 900,
        memory_cost: int = 65536,
        time_cost: int = 3,
        parallelism: int = 2,
    ) -> None:
        self.db = db
        self.audit = audit
        self.idle_seconds = int(idle_seconds)
        self.memory_cost = int(memory_cost)
        self.time_cost = int(time_cost)
        self.parallelism = int(parallelism)
        self._key: Optional[bytearray] = None
        self._last_activity = 0.0

    # -- state ------------------------------------------------------------
    def _meta(self) -> Optional[Dict[str, Any]]:
        return self.db.one("SELECT * FROM vault_meta WHERE id = 1")

    def initialized(self) -> bool:
        return self._meta() is not None

    def _touch(self) -> None:
        self._last_activity = time.monotonic()

    def _is_unlocked(self) -> bool:
        if self._key is None:
            return False
        if self.idle_seconds > 0 and (
            time.monotonic() - self._last_activity > self.idle_seconds
        ):
            self.lock()
            return False
        return True

    def status(self) -> Dict[str, Any]:
        unlocked = self._is_unlocked()
        remaining = 0
        if unlocked and self.idle_seconds > 0:
            elapsed = time.monotonic() - self._last_activity
            remaining = max(0, int(self.idle_seconds - elapsed))
        return {
            "initialized": self.initialized(),
            "unlocked": unlocked,
            "idle_lock_seconds": self.idle_seconds,
            "seconds_until_lock": remaining,
        }

    def lock(self) -> None:
        if self._key is not None:
            for index in range(len(self._key)):
                self._key[index] = 0
        self._key = None
        self._last_activity = 0.0

    # -- crypto -----------------------------------------------------------
    def _derive(self, password: str, salt: bytes, params: Dict[str, Any]) -> bytes:
        return hash_secret_raw(
            secret=password.encode("utf-8"),
            salt=salt,
            time_cost=int(params["time_cost"]),
            memory_cost=int(params["memory_cost"]),
            parallelism=int(params["parallelism"]),
            hash_len=KEY_LEN,
            type=Argon2Type.ID,
        )

    def _key_for_use(self) -> bytes:
        if not self._is_unlocked() or self._key is None:
            raise VaultLocked("Vault is locked.")
        self._touch()
        return bytes(self._key)

    @staticmethod
    def _aad(entry_id: int) -> bytes:
        return ("muse-entry:%d" % entry_id).encode("ascii")

    # -- lifecycle --------------------------------------------------------
    def initialize(self, master_password: str) -> None:
        if self.initialized():
            raise VaultAlreadyInitialized("Vault is already initialized.")
        if len(master_password) < 10:
            raise VaultError("Master password must be at least 10 characters.")
        params = {
            "memory_cost": self.memory_cost,
            "time_cost": self.time_cost,
            "parallelism": self.parallelism,
        }
        salt = secrets.token_bytes(16)
        key = self._derive(master_password, salt, params)
        nonce = secrets.token_bytes(NONCE_LEN)
        canary_ct = AESGCM(key).encrypt(nonce, CANARY, CANARY_AAD)
        self.db.execute(
            "INSERT INTO vault_meta (id, kdf, salt, memory_cost, time_cost, "
            "parallelism, canary_nonce, canary_ct, created_at) "
            "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.KDF,
                salt,
                params["memory_cost"],
                params["time_cost"],
                params["parallelism"],
                nonce,
                canary_ct,
                now_ts(),
            ),
        )
        self._key = bytearray(key)
        self._touch()
        self.audit.log("user", "vault_initialize", "vault created")

    def unlock(self, master_password: str) -> None:
        meta = self._meta()
        if not meta:
            raise VaultError("Vault has not been initialized yet.")
        params = {
            "time_cost": meta["time_cost"],
            "memory_cost": meta["memory_cost"],
            "parallelism": meta["parallelism"],
        }
        try:
            key = self._derive(master_password, bytes(meta["salt"]), params)
        except argon2.exceptions.Argon2Error as exc:  # pragma: no cover
            raise VaultError("Key derivation failed: %s" % exc) from exc
        try:
            plain = AESGCM(key).decrypt(
                bytes(meta["canary_nonce"]), bytes(meta["canary_ct"]), CANARY_AAD
            )
        except InvalidTag:
            raise InvalidMasterPassword("Incorrect master password.")
        if plain != CANARY:  # pragma: no cover - tamper detection
            raise InvalidMasterPassword("Incorrect master password.")
        self.lock()
        self._key = bytearray(key)
        self._touch()
        self.audit.log("user", "vault_unlock", "vault unlocked")

    def change_master_password(self, old_password: str, new_password: str) -> None:
        meta = self._meta()
        if not meta:
            raise VaultError("Vault has not been initialized yet.")
        if len(new_password) < 10:
            raise VaultError("New master password must be at least 10 characters.")
        old_key = self._key_for_use()
        params = {
            "time_cost": meta["time_cost"],
            "memory_cost": meta["memory_cost"],
            "parallelism": meta["parallelism"],
        }
        candidate = self._derive(old_password, bytes(meta["salt"]), params)
        try:
            AESGCM(candidate).decrypt(
                bytes(meta["canary_nonce"]), bytes(meta["canary_ct"]), CANARY_AAD
            )
        except InvalidTag:
            raise InvalidMasterPassword("Incorrect master password.")
        if bytes(candidate) != old_key:
            raise InvalidMasterPassword("Incorrect master password.")

        salt = secrets.token_bytes(16)
        new_key = self._derive(new_password, salt, params)
        old_aes = AESGCM(old_key)
        new_aes = AESGCM(new_key)

        rewritten = []
        for row in self.db.query("SELECT id, nonce, ciphertext FROM vault_entries"):
            payload = old_aes.decrypt(
                bytes(row["nonce"]), bytes(row["ciphertext"]), self._aad(row["id"])
            )
            nonce = secrets.token_bytes(NONCE_LEN)
            rewritten.append(
                (nonce, new_aes.encrypt(nonce, payload, self._aad(row["id"])), row["id"])
            )

        nonce = secrets.token_bytes(NONCE_LEN)
        canary_ct = new_aes.encrypt(nonce, CANARY, CANARY_AAD)
        with self.db.transaction():
            self.db.execute(
                "UPDATE vault_meta SET salt = ?, canary_nonce = ?, canary_ct = ? "
                "WHERE id = 1",
                (salt, nonce, canary_ct),
            )
            for item_nonce, item_ct, entry_id in rewritten:
                self.db.execute(
                    "UPDATE vault_entries SET nonce = ?, ciphertext = ?, updated_at = ? "
                    "WHERE id = ?",
                    (item_nonce, item_ct, now_ts(), entry_id),
                )
        self.lock()
        self._key = bytearray(new_key)
        self._touch()
        self.audit.log("user", "vault_change_master_password", "master password rotated")

    # -- entries ----------------------------------------------------------
    def list_entries(self) -> List[Dict[str, Any]]:
        rows = self.db.query(
            "SELECT id, label, domains, created_at, updated_at "
            "FROM vault_entries ORDER BY label"
        )
        for row in rows:
            row["domains"] = [d for d in row["domains"].split(",") if d]
        return rows

    def _row(self, entry_id: int) -> Dict[str, Any]:
        row = self.db.one("SELECT * FROM vault_entries WHERE id = ?", (entry_id,))
        if not row:
            raise EntryNotFound("Vault entry not found.")
        return row

    def _row_by_label(self, label: str) -> Dict[str, Any]:
        row = self.db.one("SELECT * FROM vault_entries WHERE label = ?", (label,))
        if not row:
            raise EntryNotFound("No vault entry labelled %r." % label)
        return row

    def get_secret(self, entry_id: int) -> Dict[str, Any]:
        key = self._key_for_use()
        row = self._row(entry_id)
        payload = AESGCM(key).decrypt(
            bytes(row["nonce"]), bytes(row["ciphertext"]), self._aad(entry_id)
        )
        data = json.loads(payload.decode("utf-8"))
        data["label"] = row["label"]
        data["domains"] = [d for d in row["domains"].split(",") if d]
        return data

    def get_secret_by_label(self, label: str) -> Dict[str, Any]:
        # Require an unlocked vault before touching rows so a locked vault does
        # not even disclose which labels exist.
        self._key_for_use()
        row = self._row_by_label(label)
        return self.get_secret(int(row["id"]))

    def entries_for_host(self, host: str) -> List[Dict[str, Any]]:
        """Entries whose allowed domains match this host (subdomains included)."""
        host = (host or "").strip().lower().rstrip(".")
        if not host:
            return []
        return [
            entry
            for entry in self.list_entries()
            if host_matches(host, entry["domains"])
        ]

    def secret_for_host(self, host: str) -> Dict[str, Any]:
        """Find the credential bound to the current host.

        Exact-domain matches win over parent-domain matches. If several entries
        remain, raise AmbiguousHost so the agent can ask which label to use.
        """
        entries = self.entries_for_host(host)
        if not entries:
            raise EntryNotFound("No vault entry is bound to %s." % host)
        exact = [entry for entry in entries if host in entry["domains"]]
        candidates = exact or entries
        if len(candidates) > 1:
            raise AmbiguousHost(host, [entry["label"] for entry in candidates])
        return self.get_secret(int(candidates[0]["id"]))

    def add_entry(
        self,
        label: str,
        domains: Iterable[str],
        username: str = "",
        password: str = "",
        url: str = "",
        notes: str = "",
        totp_secret: str = "",
    ) -> Dict[str, Any]:
        key = self._key_for_use()
        label = (label or "").strip()
        if not label or len(label) > 80:
            raise VaultError("Label is required (max 80 characters).")
        if not password:
            raise VaultError("Password is required.")
        clean_domains = normalize_domains(domains)
        if not clean_domains:
            raise VaultError(
                "At least one allowed domain is required so the agent cannot "
                "autofill this credential on an unrelated site."
            )
        if self.db.one("SELECT id FROM vault_entries WHERE label = ?", (label,)):
            raise DuplicateLabel("An entry with this label already exists.")

        entry_id = self.db.insert(
            "INSERT INTO vault_entries (label, domains, nonce, ciphertext, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (label, ",".join(clean_domains), b"", b"", now_ts(), now_ts()),
        )
        payload = json.dumps(
            {
                "username": username or "",
                "password": password,
                "url": url or "",
                "notes": notes or "",
                "totp_secret": totp_secret or "",
            },
            ensure_ascii=False,
        ).encode("utf-8")
        nonce = secrets.token_bytes(NONCE_LEN)
        ciphertext = AESGCM(key).encrypt(nonce, payload, self._aad(entry_id))
        try:
            self.db.execute(
                "UPDATE vault_entries SET nonce = ?, ciphertext = ?, updated_at = ? "
                "WHERE id = ?",
                (nonce, ciphertext, now_ts(), entry_id),
            )
        except Exception:
            self.db.execute("DELETE FROM vault_entries WHERE id = ?", (entry_id,))
            raise
        self.audit.log(
            "user", "vault_add_entry", {"label": label, "domains": clean_domains}
        )
        return {"id": entry_id, "label": label, "domains": clean_domains}

    def update_entry(
        self,
        entry_id: int,
        label: Optional[str] = None,
        domains: Optional[Iterable[str]] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        url: Optional[str] = None,
        notes: Optional[str] = None,
        totp_secret: Optional[str] = None,
    ) -> Dict[str, Any]:
        key = self._key_for_use()
        row = self._row(entry_id)
        current = self.get_secret(entry_id)

        new_label = (label if label is not None else row["label"]).strip()
        if not new_label:
            raise VaultError("Label is required.")
        existing = self.db.one(
            "SELECT id FROM vault_entries WHERE label = ? AND id != ?",
            (new_label, entry_id),
        )
        if existing:
            raise DuplicateLabel("An entry with this label already exists.")

        if domains is None:
            clean_domains = [d for d in row["domains"].split(",") if d]
        else:
            clean_domains = normalize_domains(domains)
        if not clean_domains:
            raise VaultError("At least one allowed domain is required.")

        new_password = password if password is not None else current["password"]
        if not new_password:
            raise VaultError("Password is required.")
        payload = json.dumps(
            {
                "username": username if username is not None else current["username"],
                "password": new_password,
                "url": url if url is not None else current["url"],
                "notes": notes if notes is not None else current["notes"],
                "totp_secret": (
                    totp_secret
                    if totp_secret is not None
                    else current.get("totp_secret", "")
                ),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        nonce = secrets.token_bytes(NONCE_LEN)
        ciphertext = AESGCM(key).encrypt(nonce, payload, self._aad(entry_id))
        self.db.execute(
            "UPDATE vault_entries SET label = ?, domains = ?, nonce = ?, "
            "ciphertext = ?, updated_at = ? WHERE id = ?",
            (
                new_label,
                ",".join(clean_domains),
                nonce,
                ciphertext,
                now_ts(),
                entry_id,
            ),
        )
        self.audit.log(
            "user",
            "vault_update_entry",
            {"label": new_label, "domains": clean_domains},
        )
        return {"id": entry_id, "label": new_label, "domains": clean_domains}

    def delete_entry(self, entry_id: int) -> None:
        row = self._row(entry_id)
        self.db.execute("DELETE FROM vault_entries WHERE id = ?", (entry_id,))
        self.audit.log("user", "vault_delete_entry", {"label": row["label"]})

    def reveal(self, entry_id: int) -> Dict[str, Any]:
        secret = self.get_secret(entry_id)
        secret["has_totp"] = bool(secret.pop("totp_secret", ""))
        self.audit.log("user", "vault_reveal", {"label": secret["label"]})
        return secret
