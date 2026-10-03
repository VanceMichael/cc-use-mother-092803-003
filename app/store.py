"""SQLite 持久化层。

关键设计：
- ``idempotent_results`` 以 request_id 保存每次请求的结果，重放返回同一结果；
- ``bank_events`` 以 (event_type, bank_serial) 唯一约束，银行回调重送不会重复划款；
- ``ledger_entries`` 只追加不修改，是资金去向还原的唯一事实来源；
- 账户余额用触发器维护的冗余字段，服务层在同一事务内按台账校验，防止透支。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    username    TEXT PRIMARY KEY,
    role        TEXT NOT NULL,
    project_id  TEXT,                       -- developer/regulator 的可操作项目
    buyer_id    TEXT,                       -- buyer 身份对应的购房人编号
    display_name TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    project_id  TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',  -- active / overdue / closed
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS escrow_accounts (
    account_no  TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects(project_id),
    bank_name   TEXT NOT NULL,
    balance     INTEGER NOT NULL DEFAULT 0,   -- 监管账户当前余额（分）
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS units (
    project_id  TEXT NOT NULL REFERENCES projects(project_id),
    unit_no     TEXT NOT NULL,
    building    TEXT NOT NULL DEFAULT '',
    total_price INTEGER NOT NULL,             -- 备案总价（分）
    status      TEXT NOT NULL DEFAULT 'available', -- available / signed
    PRIMARY KEY (project_id, unit_no)
);

CREATE TABLE IF NOT EXISTS contracts (
    contract_no TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects(project_id),
    unit_no     TEXT NOT NULL,
    buyer_id    TEXT NOT NULL,
    buyer_name  TEXT NOT NULL DEFAULT '',
    total_price INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TEXT NOT NULL,
    FOREIGN KEY (project_id, unit_no) REFERENCES units(project_id, unit_no)
);

CREATE TABLE IF NOT EXISTS receivables (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_no     TEXT NOT NULL REFERENCES contracts(contract_no),
    fee_type        TEXT NOT NULL,           -- deposit / down_payment / mortgage
    due_amount      INTEGER NOT NULL,
    received_amount INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'pending', -- pending / received / released / refunded
    released_amount INTEGER NOT NULL DEFAULT 0,
    refunded_amount INTEGER NOT NULL DEFAULT 0,
    bank_serial     TEXT,                    -- 入账时银行流水号
    received_at     TEXT,
    UNIQUE (contract_no, fee_type)
);

CREATE TABLE IF NOT EXISTS node_attestations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   TEXT NOT NULL REFERENCES projects(project_id),
    node         TEXT NOT NULL,
    attested_at  TEXT NOT NULL,
    attested_by  TEXT NOT NULL,
    evidence     TEXT NOT NULL DEFAULT '',
    UNIQUE (project_id, node)
);

CREATE TABLE IF NOT EXISTS outgo_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,             -- node_release / refund / forfeit
    payee        TEXT NOT NULL,             -- developer / buyer
    fee_type     TEXT NOT NULL DEFAULT '',  -- refund/forfeit 对应费用桶
    contract_no  TEXT NOT NULL REFERENCES contracts(contract_no),
    project_id   TEXT NOT NULL,
    node         TEXT NOT NULL DEFAULT '',  -- node_release 对应节点
    amount       INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending', -- pending/approved/paid/rejected/failed
    reason       TEXT NOT NULL DEFAULT '',
    created_by   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    approved_by  TEXT,
    approved_at  TEXT,
    bank_serial  TEXT,
    paid_at      TEXT,
    fail_reason  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS bank_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type   TEXT NOT NULL,             -- inbound / release / refund
    bank_serial  TEXT NOT NULL,
    payload      TEXT NOT NULL,
    received_at  TEXT NOT NULL,
    UNIQUE (event_type, bank_serial)
);

-- 只追加台账：购房人每一分钱的进入与最终去向
CREATE TABLE IF NOT EXISTS ledger_entries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_no  TEXT NOT NULL,
    project_id   TEXT NOT NULL,
    account_no   TEXT,
    fee_type     TEXT NOT NULL,
    direction    TEXT NOT NULL,             -- receive / release / refund / forfeit
    amount       INTEGER NOT NULL,
    bank_serial  TEXT,
    ref_type     TEXT NOT NULL DEFAULT '',  -- receivable / release_request / settlement
    ref_id       TEXT NOT NULL DEFAULT '',
    reason       TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id   TEXT NOT NULL,
    actor        TEXT NOT NULL,
    action       TEXT NOT NULL,
    accepted     INTEGER NOT NULL,
    summary      TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotent_results (
    request_id   TEXT PRIMARY KEY,
    accepted     INTEGER NOT NULL,
    state        TEXT NOT NULL,
    message      TEXT NOT NULL,
    data         TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str = ":memory:") -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---- 基础工具 ---------------------------------------------------------

    def now(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def query_all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    # ---- 幂等结果 ---------------------------------------------------------

    def cached_result(self, request_id: str) -> tuple[bool, str, str, dict[str, Any]] | None:
        row = self.query_one(
            "SELECT accepted, state, message, data FROM idempotent_results WHERE request_id=?",
            (request_id,),
        )
        if row is None:
            return None
        return bool(row["accepted"]), row["state"], row["message"], json.loads(row["data"])

    def save_result(self, request_id: str, accepted: bool, state: str,
                    message: str, data: dict[str, Any]) -> None:
        self.execute(
            "INSERT OR REPLACE INTO idempotent_results VALUES (?,?,?,?,?,?)",
            (request_id, int(accepted), state, message,
             json.dumps(data, ensure_ascii=False), self.now()),
        )

    # ---- 台账与余额（同一事务） -------------------------------------------

    def append_ledger(self, *, contract_no: str, project_id: str, account_no: str | None,
                      fee_type: str, direction: str, amount: int,
                      bank_serial: str | None, ref_type: str, ref_id: str, reason: str) -> int:
        cur = self.execute(
            """INSERT INTO ledger_entries
               (contract_no, project_id, account_no, fee_type, direction, amount,
                bank_serial, ref_type, ref_id, reason, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (contract_no, project_id, account_no, fee_type, direction, amount,
             bank_serial, ref_type, str(ref_id), reason, self.now()),
        )
        if account_no is not None:
            delta = amount if direction == "receive" else -amount
            # release / refund / forfeit 都是出账；触发器式余额更新 + 透支拦截
            row = self.query_one(
                "SELECT balance FROM escrow_accounts WHERE account_no=?", (account_no,))
            if row is None:
                raise ValueError(f"监管账户不存在: {account_no}")
            new_balance = row["balance"] + delta
            if new_balance < 0:
                raise ValueError(f"监管账户余额不足，拒绝出账 {amount} 分（当前 {row['balance']} 分）")
            self.execute("UPDATE escrow_accounts SET balance=? WHERE account_no=?",
                         (new_balance, account_no))
        return int(cur.lastrowid)

    def bank_event_seen(self, event_type: str, bank_serial: str, payload: dict[str, Any]) -> bool:
        """登记银行回调；已存在同类型同流水号返回 True（重送）。"""
        try:
            self.execute(
                "INSERT INTO bank_events (event_type, bank_serial, payload, received_at) VALUES (?,?,?,?)",
                (event_type, bank_serial, json.dumps(payload, ensure_ascii=False), self.now()),
            )
            return False
        except sqlite3.IntegrityError:
            return True

    def ledger_for_contract(self, contract_no: str) -> list[sqlite3.Row]:
        return self.query_all(
            "SELECT * FROM ledger_entries WHERE contract_no=? ORDER BY id", (contract_no,))

    def close(self) -> None:
        self.conn.close()
