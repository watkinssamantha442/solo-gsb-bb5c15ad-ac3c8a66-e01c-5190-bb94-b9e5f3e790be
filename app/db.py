"""SQLite 连接、建表与轻量数据访问层。

设计要点：
- SQLite 文件路径由环境变量 SANDBOX_DB 覆盖，默认放在项目 data/ 目录；
- check_same_thread=False + 进程内 RLock 串行化写操作，FastAPI 多线程下安全；
- 评估等复杂逻辑放在 services 层，这里只负责建表与基础工具。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "sandbox.db"
DB_PATH = Path(os.environ.get("SANDBOX_DB", DEFAULT_DB_PATH))

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def get_lock() -> threading.RLock:
    """暴露给 service 层的串行化锁（评估 + 写决策需要原子执行）。"""
    return _lock


def now_iso() -> str:
    """UTC ISO8601 时间戳，用于 created_at / recorded_at 等元数据字段。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_conn() -> sqlite3.Connection:
    """返回进程级单例连接（已开启 WAL 与外键约束）。"""
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(DB_PATH), check_same_thread=False, isolation_level=None
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        _conn = conn
        _init_schema(_conn)
    return _conn


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """串行化的写事务上下文：BEGIN ... COMMIT / ROLLBACK。"""
    conn = get_conn()
    with _lock:
        conn.execute("BEGIN")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def query_all(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with _lock:
        return get_conn().execute(sql, params).fetchall()


def query_one(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    with _lock:
        return get_conn().execute(sql, params).fetchone()


def execute(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    """单条写语句（自动事务足够简单时使用）。"""
    with _lock:
        return get_conn().execute(sql, params)


SCHEMA = """
-- 抑制 / 放行规则
CREATE TABLE IF NOT EXISTS rules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    priority        INTEGER NOT NULL,            -- 数值越小优先级越高
    action          TEXT NOT NULL,               -- suppress | pass
    devices         TEXT NOT NULL DEFAULT '',    -- 逗号分隔，支持 PREFIX*；空=任意
    batches         TEXT NOT NULL DEFAULT '',    -- 同上
    levels          TEXT NOT NULL DEFAULT '',    -- info/warning/critical，逗号分隔；空=任意
    time_window     TEXT NOT NULL DEFAULT '',    -- HH:MM-HH:MM，支持跨零点；空=任意
    cooldown_sec    INTEGER NOT NULL DEFAULT 0,  -- 仅 suppress 生效，0=不做冷却
    cooldown_scope  TEXT NOT NULL DEFAULT 'device_batch', -- device_batch|device|global
    note            TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);

-- 注入的原始告警（payload_json 保留完整原文）
CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sequence    INTEGER NOT NULL UNIQUE,         -- 注入顺序，也用于按序评估
    device_id   TEXT NOT NULL,
    batch_id    TEXT NOT NULL DEFAULT '',
    level       TEXT NOT NULL DEFAULT 'info',
    alert_type  TEXT NOT NULL DEFAULT '',
    message     TEXT NOT NULL DEFAULT '',
    alert_time  TEXT,                            -- ISO8601，可能为空
    payload_json TEXT NOT NULL DEFAULT '{}'
);

-- 最近一次评估为每条告警生成的决定 + 解释
CREATE TABLE IF NOT EXISTS decisions (
    alert_id         INTEGER PRIMARY KEY REFERENCES alerts(id) ON DELETE CASCADE,
    sequence         INTEGER NOT NULL,
    verdict          TEXT NOT NULL,              -- pass | suppress
    rule_id          INTEGER,
    rule_name        TEXT NOT NULL DEFAULT '',
    reason           TEXT NOT NULL DEFAULT '',
    cooldown_seconds INTEGER,
    explanation_json TEXT NOT NULL DEFAULT '{}',
    evaluated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_verdict ON decisions(verdict);

-- 策略快照（用于“变更前后”对比）
CREATE TABLE IF NOT EXISTS policy_versions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    label      TEXT NOT NULL,
    rules_json TEXT NOT NULL,                    -- 当时全量规则的深拷贝
    created_at TEXT NOT NULL
);

-- 内置模拟接收端的收件箱
CREATE TABLE IF NOT EXISTS receiver_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id    INTEGER,
    sequence    INTEGER,
    payload_json TEXT NOT NULL,
    received_at TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'forward'  -- forward | webhook
);

-- 键值设置
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # 默认开启“逐条注入时自动转发到模拟接收端”
    conn.execute(
        "INSERT OR IGNORE INTO settings(key, value) VALUES ('auto_forward', '1')"
    )
