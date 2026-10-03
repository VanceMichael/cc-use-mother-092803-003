"""SQLite 持久化层：项目、账户、房源、合同、事件、资金指令、分类账、审计日志。"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS developers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS banks (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    scope_id TEXT
);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    developer_id TEXT NOT NULL REFERENCES developers(id),
    bank_id TEXT NOT NULL REFERENCES banks(id),
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS escrow_accounts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL UNIQUE REFERENCES projects(id),
    account_no TEXT NOT NULL,
    bank_id TEXT NOT NULL REFERENCES banks(id),
    currency TEXT NOT NULL DEFAULT 'CNY',
    balance_cents INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS units (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    unit_no TEXT NOT NULL,
    price_cents INTEGER NOT NULL CHECK (price_cents > 0),
    status TEXT NOT NULL DEFAULT 'available',
    UNIQUE (project_id, unit_no)
);
CREATE TABLE IF NOT EXISTS buyers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    id_no TEXT NOT NULL,
    bank_account TEXT NOT NULL DEFAULT '',
    UNIQUE (name, id_no)
);
CREATE TABLE IF NOT EXISTS contracts (
    id TEXT PRIMARY KEY,
    contract_no TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL REFERENCES projects(id),
    unit_id TEXT NOT NULL UNIQUE REFERENCES units(id),
    buyer_id TEXT NOT NULL REFERENCES buyers(id),
    total_price_cents INTEGER NOT NULL CHECK (total_price_cents > 0),
    deposit_cents INTEGER NOT NULL CHECK (deposit_cents >= 0),
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payment_schedule (
    id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL REFERENCES contracts(id),
    seq INTEGER NOT NULL,
    item_type TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    due_date TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    paid_at TEXT,
    UNIQUE (contract_id, seq)
);
CREATE TABLE IF NOT EXISTS contract_events (
    id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL REFERENCES contracts(id),
    event_type TEXT NOT NULL,
    seq INTEGER NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    confirmed_by TEXT NOT NULL,
    confirmed_at TEXT NOT NULL,
    UNIQUE (contract_id, event_type)
);
CREATE TABLE IF NOT EXISTS fund_instructions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    contract_id TEXT REFERENCES contracts(id),
    flow_type TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    destination TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    idem_key TEXT NOT NULL UNIQUE,
    bank_ref TEXT NOT NULL DEFAULT '',
    failure_code TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    settled_at TEXT
);
CREATE TABLE IF NOT EXISTS bank_callbacks (
    callback_id TEXT PRIMARY KEY,
    instr_id TEXT NOT NULL REFERENCES fund_instructions(id),
    status TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL REFERENCES escrow_accounts(id),
    contract_id TEXT REFERENCES contracts(id),
    direction TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    balance_after_cents INTEGER NOT NULL,
    instr_id TEXT NOT NULL REFERENCES fund_instructions(id),
    summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL DEFAULT '',
    entity_id TEXT NOT NULL DEFAULT '',
    request_id TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS idempotent_requests (
    request_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_tokens (
    token TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    issued_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);
"""


def to_cents(amount: Any) -> int:
    """人民币元 -> 分，拒绝超过两位小数或负数。"""
    d = Decimal(str(amount)).quantize(Decimal("0.01"))
    if d < 0:
        raise ValueError("金额不能为负")
    return int(d * 100)


def from_cents(cents: int) -> str:
    return f"{cents / 100:.2f}"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.RLock()
        with self._conn:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    # ---- 通用辅助 --------------------------------------------------------
    @staticmethod
    def row(r: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(r) if r is not None else None

    def get(self, conn: sqlite3.Connection, table: str, key: str, col: str = "id") -> dict[str, Any] | None:
        q = f"SELECT * FROM {table} WHERE {col} = ?"
        return self.row(conn.execute(q, (key,)).fetchone())

    def insert(self, conn: sqlite3.Connection, table: str, data: dict[str, Any]) -> None:
        cols = ", ".join(data)
        marks = ", ".join("?" for _ in data)
        conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(data.values()))

    def audit(self, conn: sqlite3.Connection, actor_id: str, action: str,
              entity_type: str, entity_id: str, request_id: str,
              detail: dict[str, Any] | None = None) -> None:
        conn.execute(
            "INSERT INTO audit_log (ts, actor_id, action, entity_type, entity_id, request_id, detail)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now(), actor_id, action, entity_type, entity_id, request_id,
             json.dumps(detail or {}, ensure_ascii=False)),
        )
