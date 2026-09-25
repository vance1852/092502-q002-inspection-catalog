"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checklist_templates (
    template_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version_count INTEGER NOT NULL DEFAULT 0 CHECK(version_count >= 0),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checklist_template_versions (
    version_id TEXT PRIMARY KEY,
    template_id TEXT NOT NULL REFERENCES checklist_templates(template_id),
    version_no INTEGER NOT NULL CHECK(version_no >= 1),
    status TEXT NOT NULL CHECK(status IN
        ('draft','submitted','approved','rejected','published','revoked')),
    applicability_json TEXT NOT NULL,
    items_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    drafted_by TEXT NOT NULL REFERENCES actors(actor_id),
    drafted_at TEXT NOT NULL,
    submitted_by TEXT REFERENCES actors(actor_id),
    submitted_at TEXT,
    reviewed_by TEXT REFERENCES actors(actor_id),
    reviewed_at TEXT,
    review_result TEXT CHECK(review_result IN ('approved','rejected')),
    review_reason TEXT,
    published_by TEXT REFERENCES actors(actor_id),
    published_at TEXT,
    effective_from TEXT,
    effective_to TEXT,
    revoked_by TEXT REFERENCES actors(actor_id),
    revoked_at TEXT,
    revoke_reason TEXT,
    UNIQUE(template_id, version_no)
);
CREATE TABLE IF NOT EXISTS enterprise_overrides (
    override_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    template_id TEXT REFERENCES checklist_templates(template_id),
    kind TEXT NOT NULL CHECK(kind IN ('add','remove')),
    item_code TEXT NOT NULL,
    content_json TEXT,
    reason TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','revoked')),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    revoked_by TEXT REFERENCES actors(actor_id),
    revoked_at TEXT,
    revoke_reason TEXT
);
CREATE TABLE IF NOT EXISTS inspection_tasks (
    task_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    task_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('generated','started','completed','cancelled')),
    resolution_json TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    started_by TEXT REFERENCES actors(actor_id),
    started_at TEXT,
    completed_by TEXT REFERENCES actors(actor_id),
    completed_at TEXT,
    UNIQUE(site_id, task_date)
);
CREATE TABLE IF NOT EXISTS inspection_task_items (
    task_id TEXT NOT NULL REFERENCES inspection_tasks(task_id),
    position INTEGER NOT NULL CHECK(position >= 1),
    item_code TEXT NOT NULL,
    category TEXT NOT NULL,
    content TEXT NOT NULL,
    sources_json TEXT NOT NULL,
    PRIMARY KEY(task_id, position)
);
CREATE INDEX IF NOT EXISTS idx_ctv_template ON checklist_template_versions(template_id, version_no);
CREATE INDEX IF NOT EXISTS idx_overrides_site ON enterprise_overrides(site_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_site ON inspection_tasks(site_id, task_date);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """对既有数据库补齐后加的列（新表在 SCHEMA 中已包含）。"""

        columns = {
            row["name"] for row in self.connection.execute(
                "PRAGMA table_info(inspection_tasks)")
        }
        if columns:
            if "completed_by" not in columns:
                self.connection.execute(
                    "ALTER TABLE inspection_tasks ADD COLUMN completed_by TEXT")
            if "completed_at" not in columns:
                self.connection.execute(
                    "ALTER TABLE inspection_tasks ADD COLUMN completed_at TEXT")

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
