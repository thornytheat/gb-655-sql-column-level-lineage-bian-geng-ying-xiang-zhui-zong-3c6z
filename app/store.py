"""SQLite 读写：批次快照、运行清单、隔离行、键值状态。

只放数据访问，不放判定；调用方拿到的是普通字典。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

from app import config

_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    schema_id      TEXT PRIMARY KEY,
    version_label  TEXT NOT NULL,
    source_path    TEXT NOT NULL,
    source_sha256  TEXT NOT NULL,
    encoding       TEXT NOT NULL,
    bom            INTEGER NOT NULL,
    field_count    INTEGER NOT NULL,
    blocked_count  INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    payload_json   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id         TEXT PRIMARY KEY,
    batch_key      TEXT NOT NULL UNIQUE,
    old_schema_id  TEXT NOT NULL,
    new_schema_id  TEXT NOT NULL,
    version_seq    INTEGER NOT NULL,
    input_sha256   TEXT NOT NULL,
    ruleset        TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to   TEXT,
    summary_json   TEXT NOT NULL,
    payload_json   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quarantine (
    schema_id     TEXT NOT NULL,
    line          INTEGER,
    table_name    TEXT,
    column_name   TEXT,
    reason        TEXT NOT NULL,
    blocking      INTEGER NOT NULL,
    detail        TEXT,
    raw           TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lineage_parses (
    scope          TEXT PRIMARY KEY,
    expr_count     INTEGER NOT NULL,
    edge_count     INTEGER NOT NULL,
    conflict_count INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    payload_json   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rulings (
    scope              TEXT NOT NULL,
    target             TEXT NOT NULL,
    chosen_line        INTEGER NOT NULL,
    chosen_sources     TEXT NOT NULL,
    reason             TEXT NOT NULL,
    revision           INTEGER NOT NULL,
    active             INTEGER NOT NULL,
    published_revision INTEGER NOT NULL,
    decided_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    PRIMARY KEY (scope, target)
);
CREATE TABLE IF NOT EXISTS ruling_events (
    event_id    TEXT PRIMARY KEY,
    scope       TEXT NOT NULL,
    target      TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    phase       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lineage_views (
    scope        TEXT PRIMARY KEY,
    view_seq     INTEGER NOT NULL,
    published_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quarantine_schema ON quarantine(schema_id);
CREATE INDEX IF NOT EXISTS idx_ruling_events_scope ON ruling_events(scope);
"""


def db_path() -> Path:
    raw = os.environ.get("LINEAGEGUARD_DB", "").strip()
    path = Path(raw) if raw else config.DEFAULT_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _LOCK, _connect() as conn:
        conn.executescript(SCHEMA)


def reset() -> None:
    """测试与重建用：清空全部业务表。"""
    with _LOCK, _connect() as conn:
        conn.executescript(SCHEMA)
        for table in ("snapshots", "runs", "quarantine", "kv",
                      "lineage_parses", "rulings", "ruling_events", "lineage_views"):
            conn.execute(f"DELETE FROM {table}")


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value):
    if not value:
        return None
    return json.loads(value)


# ---------- 快照 ----------

def save_snapshot(snapshot: dict) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO snapshots (schema_id, version_label, source_path, source_sha256,
                   encoding, bom, field_count, blocked_count, created_at, payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(schema_id) DO UPDATE SET
                   version_label=excluded.version_label,
                   source_path=excluded.source_path,
                   source_sha256=excluded.source_sha256,
                   encoding=excluded.encoding,
                   bom=excluded.bom,
                   field_count=excluded.field_count,
                   blocked_count=excluded.blocked_count,
                   created_at=excluded.created_at,
                   payload_json=excluded.payload_json""",
            (
                snapshot["schema_id"], snapshot["version_label"], snapshot["source_path"],
                snapshot["source_sha256"], snapshot["encoding"], int(bool(snapshot["bom"])),
                int(snapshot["field_count"]), int(snapshot["blocked_count"]),
                snapshot["created_at"], _dumps(snapshot),
            ),
        )


def load_snapshot(schema_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM snapshots WHERE schema_id=?", (schema_id,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def list_snapshots(limit: int = 20) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT schema_id, version_label, source_path, source_sha256, encoding, bom,
                      field_count, blocked_count, created_at
               FROM snapshots ORDER BY created_at DESC, schema_id LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def save_quarantine(schema_id: str, rows: list[dict]) -> None:
    with _LOCK, _connect() as conn:
        conn.execute("DELETE FROM quarantine WHERE schema_id=?", (schema_id,))
        conn.executemany(
            """INSERT INTO quarantine (schema_id, line, table_name, column_name, reason,
                   blocking, detail, raw) VALUES (?,?,?,?,?,?,?,?)""",
            [
                (
                    schema_id, int(row.get("line") or 0), row.get("table") or "",
                    row.get("column") or "", row["reason"], int(bool(row.get("blocking"))),
                    row.get("detail") or "", row.get("raw") or "",
                )
                for row in rows
            ],
        )


def list_quarantine(schema_id: str) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT line, table_name, column_name, reason, blocking, detail, raw
               FROM quarantine WHERE schema_id=? ORDER BY line, reason""",
            (schema_id,),
        ).fetchall()
    return [
        {
            "line": row["line"],
            "table": row["table_name"],
            "column": row["column_name"],
            "reason": row["reason"],
            "blocking": bool(row["blocking"]),
            "detail": row["detail"],
            "raw": row["raw"],
        }
        for row in rows
    ]


# ---------- 运行清单 ----------

def save_run(run: dict) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO runs (run_id, batch_key, old_schema_id, new_schema_id, version_seq,
                   input_sha256, ruleset, created_at, effective_from, effective_to,
                   summary_json, payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run["run_id"], run["batch_key"], run["old_schema_id"], run["new_schema_id"],
                int(run["version_seq"]), run["input_sha256"], run["ruleset"],
                run["created_at"], run["effective_from"], run["effective_to"],
                _dumps(run["summary"]), _dumps(run),
            ),
        )


def find_run_by_key(batch_key: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM runs WHERE batch_key=?", (batch_key,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def run_by_id(run_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def latest_run() -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM runs ORDER BY version_seq DESC LIMIT 1"
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def list_runs(limit: int = 30) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT summary_json FROM runs ORDER BY version_seq DESC, run_id LIMIT ?""",
            (limit,),
        ).fetchall()
    return [_loads(row["summary_json"]) for row in rows]


def count_runs() -> int:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()
    return int(row["n"])


def next_version_seq() -> int:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT COALESCE(MAX(version_seq), 0) AS n FROM runs").fetchone()
    return int(row["n"]) + 1


def current_version_seq() -> int:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT COALESCE(MAX(version_seq), 0) AS n FROM runs").fetchone()
    return int(row["n"])


def list_windows() -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT run_id, effective_from, effective_to FROM runs ORDER BY version_seq"
        ).fetchall()
    return [dict(row) for row in rows]


# ---------- 键值 ----------

def set_kv(key: str, value) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO kv (k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, _dumps(value)),
        )


def get_kv(key: str, default=None):
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    return _loads(row["v"]) if row else default


# ---------- 列级血缘解析批次 ----------

def save_lineage_parse(parse: dict) -> None:
    """登记解析批次；同批（同 scope）重解析不改写，裁定与首次解析记录都保留。"""
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO lineage_parses
                   (scope, expr_count, edge_count, conflict_count, created_at, payload_json)
               VALUES (?,?,?,?,?,?)""",
            (
                parse["scope"], int(parse["expr_count"]), int(parse["edge_count"]),
                int(parse["conflict_count"]), parse["created_at"], _dumps(parse),
            ),
        )


def load_lineage_parse(scope: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM lineage_parses WHERE scope=?", (scope,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


# ---------- 目标列裁定 ----------

def _ruling_of(row: sqlite3.Row) -> dict:
    return {
        "scope": row["scope"],
        "target": row["target"],
        "chosen_line": int(row["chosen_line"]),
        "chosen_sources": _loads(row["chosen_sources"]) or [],
        "reason": row["reason"],
        "revision": int(row["revision"]),
        "active": bool(row["active"]),
        "published_revision": int(row["published_revision"]),
        "decided_at": row["decided_at"],
        "updated_at": row["updated_at"],
    }


def load_ruling(scope: str, target: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM rulings WHERE scope=? AND target=?", (scope, target)
        ).fetchone()
    return _ruling_of(row) if row else None


def list_rulings(scope: str) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM rulings WHERE scope=? ORDER BY target", (scope,)
        ).fetchall()
    return [_ruling_of(row) for row in rows]


def pending_rulings(scope: str) -> list[dict]:
    """决定已落库但尚未发布的裁定：发布中断后的续跑对象。"""
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM rulings WHERE scope=? AND published_revision < revision ORDER BY target",
            (scope,),
        ).fetchall()
    return [_ruling_of(row) for row in rows]


def _insert_event(conn: sqlite3.Connection, event: dict) -> bool:
    """事件按内容定主键，重试命中同一主键直接忽略，不重复登记。"""
    cursor = conn.execute(
        """INSERT OR IGNORE INTO ruling_events
               (event_id, scope, target, revision, phase, created_at, detail_json)
           VALUES (?,?,?,?,?,?,?)""",
        (
            event["event_id"], event["scope"], event["target"], int(event["revision"]),
            event["phase"], event["created_at"], _dumps(event.get("detail") or {}),
        ),
    )
    return cursor.rowcount > 0


def ruling_decide(ruling: dict, event: dict, expected_revision: int) -> bool:
    """裁定落库：expected_revision 是乐观锁，事件与决定同事务提交。

    版本不匹配（并发下被抢先）返回 False，由调用方转成 409。
    """
    with _LOCK, _connect() as conn:
        if expected_revision == 0:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO rulings
                       (scope, target, chosen_line, chosen_sources, reason, revision,
                        active, published_revision, decided_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    ruling["scope"], ruling["target"], int(ruling["chosen_line"]),
                    _dumps(ruling["chosen_sources"]), ruling["reason"], int(ruling["revision"]),
                    int(bool(ruling["active"])), int(ruling["published_revision"]),
                    ruling["decided_at"], ruling["updated_at"],
                ),
            )
        else:
            cursor = conn.execute(
                """UPDATE rulings SET chosen_line=?, chosen_sources=?, reason=?, revision=?,
                       active=1, decided_at=?, updated_at=?
                   WHERE scope=? AND target=? AND revision=?""",
                (
                    int(ruling["chosen_line"]), _dumps(ruling["chosen_sources"]),
                    ruling["reason"], int(ruling["revision"]),
                    ruling["decided_at"], ruling["updated_at"],
                    ruling["scope"], ruling["target"], int(expected_revision),
                ),
            )
        if cursor.rowcount == 0:
            return False
        _insert_event(conn, event)
        return True


def ruling_revoke(scope: str, target: str, *, reason: str, revision: int,
                  expected_revision: int, updated_at: str, event: dict) -> bool:
    """撤销落库：只对生效中且修订号匹配的裁定生效，事件同事务登记。"""
    with _LOCK, _connect() as conn:
        cursor = conn.execute(
            """UPDATE rulings SET active=0, reason=?, revision=?, updated_at=?
               WHERE scope=? AND target=? AND revision=? AND active=1""",
            (reason, int(revision), updated_at, scope, target, int(expected_revision)),
        )
        if cursor.rowcount == 0:
            return False
        _insert_event(conn, event)
        return True


def list_ruling_events(scope: str, limit: int = 50) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT event_id, scope, target, revision, phase, created_at, detail_json
               FROM ruling_events WHERE scope=? ORDER BY rowid DESC LIMIT ?""",
            (scope, int(limit)),
        ).fetchall()
    events = []
    for row in rows:
        detail = _loads(row["detail_json"]) or {}
        events.append({
            "event_id": row["event_id"],
            "scope": row["scope"],
            "target": row["target"],
            "revision": int(row["revision"]),
            "phase": row["phase"],
            "created_at": row["created_at"],
            "reason": detail.get("reason") or "",
            "chosen_line": detail.get("chosen_line"),
        })
    return events


def count_ruling_events(scope: str) -> int:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM ruling_events WHERE scope=?", (scope,)
        ).fetchone()
    return int(row["n"])


# ---------- 发布视图 ----------

def load_lineage_view(scope: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM lineage_views WHERE scope=?", (scope,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def apply_publication(scope: str, events: list[dict], rulings: list[dict],
                      view_payload: dict) -> int:
    """发布：登记事件、推进已发布修订号、切换视图，单事务提交。

    任何一步失败都整体回滚，旧版视图原样保留；事件主键幂等，重试不重复登记。
    """
    with _LOCK, _connect() as conn:
        for event in events:
            _insert_event(conn, event)
        for ruling in rulings:
            conn.execute(
                """UPDATE rulings SET published_revision=?
                   WHERE scope=? AND target=? AND revision=? AND published_revision<?""",
                (
                    int(ruling["revision"]), scope, ruling["target"],
                    int(ruling["revision"]), int(ruling["revision"]),
                ),
            )
        row = conn.execute(
            "SELECT view_seq FROM lineage_views WHERE scope=?", (scope,)
        ).fetchone()
        seq = int(row["view_seq"]) + 1 if row else 1
        payload = dict(view_payload)
        payload["view_seq"] = seq
        conn.execute(
            """INSERT INTO lineage_views (scope, view_seq, published_at, payload_json)
               VALUES (?,?,?,?)
               ON CONFLICT(scope) DO UPDATE SET
                   view_seq=excluded.view_seq,
                   published_at=excluded.published_at,
                   payload_json=excluded.payload_json""",
            (scope, seq, payload["published_at"], _dumps(payload)),
        )
        return seq
