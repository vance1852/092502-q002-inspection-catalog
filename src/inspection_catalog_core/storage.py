"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
import threading
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
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(organization_id)
);
CREATE TABLE IF NOT EXISTS checklist_versions (
    version_id TEXT PRIMARY KEY,
    template_id TEXT NOT NULL REFERENCES checklist_templates(template_id),
    version_number INTEGER NOT NULL CHECK(version_number >= 1),
    status TEXT NOT NULL CHECK(status IN ('draft','pending_review','published','rolled_back')),
    name TEXT NOT NULL,
    items_json TEXT NOT NULL,
    items_hash TEXT NOT NULL,
    effective_from TEXT,
    effective_until TEXT,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    submitted_by TEXT,
    submitted_at TEXT,
    reviewed_by TEXT,
    reviewed_at TEXT,
    review_decision TEXT CHECK(review_decision IS NULL OR review_decision IN ('approved','rejected')),
    review_comment TEXT,
    published_at TEXT,
    rolled_back_by TEXT,
    rolled_back_at TEXT,
    rollback_reason TEXT,
    UNIQUE(template_id, version_number)
);
CREATE TABLE IF NOT EXISTS checklist_overlays (
    overlay_id TEXT PRIMARY KEY,
    template_id TEXT NOT NULL REFERENCES checklist_templates(template_id),
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    revision INTEGER NOT NULL CHECK(revision >= 1),
    reason TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','revoked')),
    changes_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    revoked_by TEXT,
    revoked_at TEXT,
    revoke_reason TEXT,
    UNIQUE(template_id, site_id, revision)
);
CREATE TABLE IF NOT EXISTS inspection_tasks (
    task_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    task_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','started','completed','cancelled')),
    template_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    UNIQUE(site_id, task_date)
);
CREATE TABLE IF NOT EXISTS inspection_task_items (
    task_id TEXT NOT NULL REFERENCES inspection_tasks(task_id),
    position INTEGER NOT NULL CHECK(position >= 0),
    code TEXT NOT NULL,
    item_json TEXT NOT NULL,
    source_json TEXT NOT NULL,
    PRIMARY KEY(task_id, position)
);
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
        # 单连接配合多线程 HTTP 服务：应用级串行化所有数据库访问，
        # 使并发提交/复核/发布由后来者读到已提交状态并得到确定的业务冲突，
        # 同时避免多线程并发使用同一个 SQLite 连接。使用可重入锁，
        # 允许读路径在已经持锁的用例中被嵌套调用。
        self._tx_lock = threading.RLock()

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        with self._tx_lock:
            self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self.connection
            except Exception:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """在读锁内提供连接，保证读到已提交快照且不与写事务并发。"""

        with self._tx_lock:
            yield self.connection

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
