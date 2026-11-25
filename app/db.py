"""SQLite storage layer.

Muse is a single-user agent, so a small synchronous SQLite wrapper with one
serialized connection is plenty. WAL mode keeps the scheduler and HTTP handlers
from blocking each other.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id         TEXT NOT NULL,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL DEFAULT '',
    attachments_json TEXT NOT NULL DEFAULT '[]',
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);

CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT NOT NULL,
    prompt       TEXT NOT NULL,
    kind         TEXT NOT NULL,
    schedule     TEXT NOT NULL,
    timezone     TEXT NOT NULL,
    active       INTEGER NOT NULL DEFAULT 1,
    next_run_at  REAL,
    last_run_at  REAL,
    last_status  TEXT,
    last_result  TEXT,
    run_count    INTEGER NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL,
    agent_id     INTEGER,
    context      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS task_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      INTEGER NOT NULL,
    started_at   REAL NOT NULL,
    finished_at  REAL,
    status       TEXT NOT NULL,
    result       TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_runs_task ON task_runs(task_id, id);

CREATE TABLE IF NOT EXISTS vault_meta (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    kdf            TEXT NOT NULL,
    salt           BLOB NOT NULL,
    memory_cost    INTEGER NOT NULL,
    time_cost      INTEGER NOT NULL,
    parallelism    INTEGER NOT NULL,
    canary_nonce   BLOB NOT NULL,
    canary_ct      BLOB NOT NULL,
    created_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS vault_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    label       TEXT NOT NULL UNIQUE,
    domains     TEXT NOT NULL DEFAULT '',
    nonce       BLOB NOT NULL,
    ciphertext  BLOB NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     TEXT NOT NULL,
    tool        TEXT NOT NULL,
    args_json   TEXT NOT NULL,
    summary     TEXT NOT NULL DEFAULT '',
    host        TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pending',
    result      TEXT,
    created_at  REAL NOT NULL,
    resolved_at REAL
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, id);

CREATE TABLE IF NOT EXISTS trusted_hosts (
    host       TEXT PRIMARY KEY,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS credential_requests (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id           TEXT NOT NULL,
    label             TEXT NOT NULL DEFAULT '',
    domain            TEXT NOT NULL,
    login_url         TEXT NOT NULL DEFAULT '',
    reason            TEXT NOT NULL DEFAULT '',
    username_selector TEXT NOT NULL DEFAULT '',
    password_selector TEXT NOT NULL DEFAULT '',
    submit_selector   TEXT NOT NULL DEFAULT '',
    kind              TEXT NOT NULL DEFAULT 'login',
    otp_selector      TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'pending',
    entry_id          INTEGER,
    result            TEXT,
    created_at        REAL NOT NULL,
    resolved_at       REAL
);
CREATE INDEX IF NOT EXISTS idx_credential_requests_status
    ON credential_requests(status, id);

CREATE TABLE IF NOT EXISTS audit (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    actor   TEXT NOT NULL,
    action  TEXT NOT NULL,
    detail  TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT 'ok'
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);

CREATE TABLE IF NOT EXISTS memories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content    TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT '',
    scope      TEXT NOT NULL DEFAULT 'lead',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_skills (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scope        TEXT NOT NULL,
    name         TEXT NOT NULL,
    summary      TEXT NOT NULL DEFAULT '',
    instructions TEXT NOT NULL,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    UNIQUE(scope, name)
);
CREATE INDEX IF NOT EXISTS idx_agent_skills_scope ON agent_skills(scope, id);

CREATE TABLE IF NOT EXISTS artifacts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    filename   TEXT NOT NULL,
    prompt     TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);

-- Multi-agent team -------------------------------------------------------
CREATE TABLE IF NOT EXISTS agents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    role         TEXT NOT NULL DEFAULT '',
    instructions TEXT NOT NULL DEFAULT '',
    model        TEXT NOT NULL DEFAULT '',
    trusted_commands TEXT NOT NULL DEFAULT '[]',
    status       TEXT NOT NULL DEFAULT 'active',
    created_by   TEXT NOT NULL DEFAULT 'user',
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agents_status ON agents(status, id);

CREATE TABLE IF NOT EXISTS agent_tasks (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    title              TEXT NOT NULL,
    prompt             TEXT NOT NULL,
    agent_id           INTEGER,
    created_by_agent_id INTEGER,
    parent_task_id     INTEGER,
    status             TEXT NOT NULL DEFAULT 'queued',
    depth              INTEGER NOT NULL DEFAULT 0,
    result             TEXT,
    error              TEXT,
    run_count          INTEGER NOT NULL DEFAULT 0,
    created_at         REAL NOT NULL,
    started_at         REAL,
    finished_at        REAL
);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_status ON agent_tasks(status, id);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_agent ON agent_tasks(agent_id, id);

CREATE TABLE IF NOT EXISTS agent_messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    from_agent_id INTEGER,
    to_agent_id   INTEGER,
    task_id       INTEGER,
    kind          TEXT NOT NULL DEFAULT 'message',
    content       TEXT NOT NULL,
    read_at       REAL,
    created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_messages_to
    ON agent_messages(to_agent_id, read_at, id);
CREATE INDEX IF NOT EXISTS idx_agent_messages_task ON agent_messages(task_id, id);
"""


def now_ts() -> float:
    return time.time()


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after the first release."""
        columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(approvals)")
        }
        if "host" not in columns:
            self._conn.execute(
                "ALTER TABLE approvals ADD COLUMN host TEXT NOT NULL DEFAULT ''"
            )
        credential_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(credential_requests)")
        }
        if "kind" not in credential_columns:
            self._conn.execute(
                "ALTER TABLE credential_requests ADD COLUMN kind TEXT NOT NULL DEFAULT 'login'"
            )
        if "otp_selector" not in credential_columns:
            self._conn.execute(
                "ALTER TABLE credential_requests ADD COLUMN otp_selector TEXT NOT NULL DEFAULT ''"
            )
        task_columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(tasks)")
        }
        if "agent_id" not in task_columns:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN agent_id INTEGER")
        if "context" not in task_columns:
            self._conn.execute(
                "ALTER TABLE tasks ADD COLUMN context TEXT NOT NULL DEFAULT ''"
            )
        agent_columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(agents)")
        }
        if "trusted_commands" not in agent_columns:
            self._conn.execute(
                "ALTER TABLE agents ADD COLUMN trusted_commands TEXT NOT NULL DEFAULT '[]'"
            )
        memory_columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(memories)")
        }
        if "scope" not in memory_columns:
            from .scope import scope_from_source

            self._conn.execute(
                "ALTER TABLE memories ADD COLUMN scope TEXT NOT NULL DEFAULT 'lead'"
            )
            rows = self._conn.execute("SELECT id, source FROM memories").fetchall()
            self._conn.executemany(
                "UPDATE memories SET scope = ? WHERE id = ?",
                [
                    (scope_from_source(row["source"]), row["id"])
                    for row in rows
                ],
            )

    # -- low level --------------------------------------------------------
    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> None:
        with self._lock:
            self._conn.executemany(sql, seq)

    def insert(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return int(cur.lastrowid)

    def query(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def one(self, sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return row[0] if row else None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """All-or-nothing unit of work for multi-statement updates."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()
