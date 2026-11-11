"""SQLite 持久化：日量与告警状态在同一事务内提交。

并发说明
--------
每批提交在一个进程内的全局串行锁中执行 ``BEGIN IMMEDIATE`` 事务：
拿到锁后立即取得 SQLite 保留锁，读判定与写提交之间不会插入其它批次，
因此本批返回的新增/解除告警严格以它实际提交前后的状态为准。
事务失败（含提交失败）整体回滚，不留任何部分写入。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator

from . import alerts

# 数据库路径可用环境变量覆盖；默认落在工作目录 data/ 下。
DEFAULT_DB_PATH = Path(
    os.environ.get("DAILY_ALERT_DB", str(Path.cwd() / "data" / "daily_alert.db"))
)

# 测试可通过 set_db_path() 切换到临时库；生产路径由环境变量/默认值决定。
_DB_PATH: Path | None = None


def set_db_path(db_path: Path | str | None) -> None:
    """覆盖全局数据库路径（主要供测试隔离使用）。"""
    global _DB_PATH
    _DB_PATH = Path(db_path) if db_path is not None else None


def current_db_path() -> Path:
    return _DB_PATH if _DB_PATH is not None else DEFAULT_DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_counts (
    campus     TEXT NOT NULL,
    sample_day TEXT NOT NULL,
    weekday    INTEGER NOT NULL,
    row_count  INTEGER,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (campus, sample_day)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS alerts (
    campus       TEXT NOT NULL,
    sample_day   TEXT NOT NULL,
    weekday      INTEGER NOT NULL,
    row_count    INTEGER NOT NULL,
    median_num   INTEGER NOT NULL,
    baseline     TEXT NOT NULL,
    triggered_at TEXT NOT NULL,
    PRIMARY KEY (campus, sample_day)
) WITHOUT ROWID;
"""

# 进程内串行锁：保证同一进程的并发批次逐个进入事务。
_batch_lock = threading.Lock()


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """打开（必要时初始化）数据库连接。"""
    path = Path(db_path) if db_path is not None else current_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.executescript(_SCHEMA)
    return conn


@contextmanager
def read_connection() -> Iterator[sqlite3.Connection]:
    """只读查询上下文：共享串行锁，保证读到的是已提交状态。"""
    with _batch_lock:
        conn = connect()
        try:
            yield conn
        finally:
            conn.close()


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """单批独占的即时写事务；任何异常都会整体回滚。"""
    with _batch_lock:
        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------

def _iso(value: date) -> str:
    return value.isoformat()


def _parse_day(value: str) -> date:
    return date.fromisoformat(value)


def get_daily_counts(
    conn: sqlite3.Connection, campus: str
) -> list[dict]:
    """返回某院区全部采样日（含未收齐的 null），按日期升序。"""
    rows = conn.execute(
        """
        SELECT sample_day, weekday, row_count, updated_at
        FROM daily_counts
        WHERE campus = ?
        ORDER BY sample_day
        """,
        (campus,),
    ).fetchall()
    return [
        {
            "sample_date": _parse_day(r["sample_day"]),
            "weekday": r["weekday"],
            "row_count": r["row_count"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


def get_active_alerts(
    conn: sqlite3.Connection, campus: str
) -> list[dict]:
    """返回某院区当前全部有效告警，按采样日升序。"""
    rows = conn.execute(
        """
        SELECT sample_day, weekday, row_count, median_num, baseline,
               triggered_at
        FROM alerts
        WHERE campus = ?
        ORDER BY sample_day
        """,
        (campus,),
    ).fetchall()
    result = []
    for r in rows:
        baseline = [
            alerts.BaselinePoint(
                sample_date=_parse_day(p["sample_date"]),
                row_count=p["row_count"],
            )
            for p in json.loads(r["baseline"])
        ]
        decision = alerts.AlertDecision(
            median_num=r["median_num"], baseline=tuple(baseline)
        )
        result.append(
            {
                "sample_date": _parse_day(r["sample_day"]),
                "weekday": r["weekday"],
                "row_count": r["row_count"],
                "median": decision.median,
                "baseline": baseline,
                "triggered_at": r["triggered_at"],
            }
        )
    return result


def _prior_collected(
    conn: sqlite3.Connection, campus: str, target: date
) -> list[tuple[date, int]]:
    """同院区、同星期几、早于 target 的已收齐采样日（取最近 4 个）。"""
    rows = conn.execute(
        """
        SELECT sample_day, row_count
        FROM daily_counts
        WHERE campus = ?
          AND weekday = ?
          AND sample_day < ?
          AND row_count IS NOT NULL
        ORDER BY sample_day DESC
        LIMIT 4
        """,
        (campus, target.weekday(), _iso(target)),
    ).fetchall()
    return [(_parse_day(r["sample_day"]), r["row_count"]) for r in rows]


def _later_weekday_rows(
    conn: sqlite3.Connection, campus: str, weekday: int
) -> list[tuple[date, int | None]]:
    rows = conn.execute(
        """
        SELECT sample_day, row_count
        FROM daily_counts
        WHERE campus = ? AND weekday = ?
        ORDER BY sample_day
        """,
        (campus, weekday),
    ).fetchall()
    return [(_parse_day(r["sample_day"]), r["row_count"]) for r in rows]


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------

def _upsert_daily_count(
    conn: sqlite3.Connection,
    campus: str,
    sample_date: date,
    row_count: int | None,
) -> None:
    conn.execute(
        """
        INSERT INTO daily_counts
            (campus, sample_day, weekday, row_count, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(campus, sample_day) DO UPDATE SET
            weekday = excluded.weekday,
            row_count = excluded.row_count,
            updated_at = excluded.updated_at
        """,
        (
            campus,
            _iso(sample_date),
            sample_date.weekday(),
            row_count,
            _now_utc(),
        ),
    )


def _replace_alert(
    conn: sqlite3.Connection,
    campus: str,
    sample_date: date,
    row_count: int,
    decision: alerts.AlertDecision,
) -> None:
    payload = [
        {"sample_date": p.sample_date.isoformat(), "row_count": p.row_count}
        for p in decision.baseline
    ]
    # 告警持续存在时保留首次触发时间 triggered_at，仅刷新基准与行数。
    conn.execute(
        """
        INSERT INTO alerts
            (campus, sample_day, weekday, row_count, median_num, baseline,
             triggered_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(campus, sample_day) DO UPDATE SET
            weekday = excluded.weekday,
            row_count = excluded.row_count,
            median_num = excluded.median_num,
            baseline = excluded.baseline
        """,
        (
            campus,
            _iso(sample_date),
            sample_date.weekday(),
            row_count,
            decision.median_num,
            json.dumps(payload, ensure_ascii=False),
            _now_utc(),
        ),
    )


def _delete_alert(conn: sqlite3.Connection, campus: str, sample_date: date) -> None:
    conn.execute(
        "DELETE FROM alerts WHERE campus = ? AND sample_day = ?",
        (campus, _iso(sample_date)),
    )


def _evaluate(
    conn: sqlite3.Connection, campus: str, target: date
) -> dict | None:
    """按当前库内状态重判单日，返回有效告警视图，否则 None。"""
    row = conn.execute(
        "SELECT row_count FROM daily_counts WHERE campus = ? AND sample_day = ?",
        (campus, _iso(target)),
    ).fetchone()
    if row is None:
        return None
    decision = alerts.decide(
        row_count=row["row_count"],
        prior_collected=_prior_collected(conn, campus, target),
    )
    if decision is None:
        return None
    return {
        "sample_date": target,
        "row_count": row["row_count"],
        "median": decision.median,
        "baseline": list(decision.baseline),
        "median_num": decision.median_num,
    }


def submit_batch(
    conn: sqlite3.Connection,
    campus: str,
    items: list[tuple[date, int | None]],
) -> dict:
    """在调用方已持有的事务内提交一批日量并重判受影响日期。

    调用方负责事务边界（见 :func:`transaction`）；本函数只做读写，
    抛异常即由上下文回滚，保证原子性。
    """
    changed_dates = [d for d, _ in items]

    # 1. 提交前快照：本批可能解除的告警必须是此前已存在的告警。
    before_rows = conn.execute(
        "SELECT sample_day, median_num, baseline FROM alerts WHERE campus = ?",
        (campus,),
    ).fetchall()
    before = {r["sample_day"]: r for r in before_rows}

    # 2. 覆盖写入本批日量（同一天的新数据覆盖旧值，含撤回为 null）。
    for sample_date, row_count in items:
        _upsert_daily_count(conn, campus, sample_date, row_count)

    # 3. 计算受影响日期：改动日 + 基准可能变化的后续同星期几日期。
    weekdays = {d.weekday() for d in changed_dates}
    later_rows = {
        wd: _later_weekday_rows(conn, campus, wd) for wd in weekdays
    }
    affected = alerts.collect_affected_dates(changed_dates, later_rows)

    # 4. 逐个重判并同步告警表（按日期升序，结果与顺序无关、便于推理）。
    raised: list[dict] = []
    resolved: list[dict] = []
    for target in sorted(affected):
        current = _evaluate(conn, campus, target)
        key = _iso(target)
        had_alert = key in before
        if current is None:
            if had_alert:
                _delete_alert(conn, campus, target)
                resolved.append({"sample_date": target})
            continue

        decision = alerts.AlertDecision(
            median_num=current["median_num"],
            baseline=tuple(current["baseline"]),
        )
        _replace_alert(conn, campus, target, current["row_count"], decision)
        if not had_alert:
            raised.append(
                {
                    "sample_date": target,
                    "row_count": current["row_count"],
                    "median": current["median"],
                    "baseline": [
                        {
                            "sample_date": p.sample_date,
                            "row_count": p.row_count,
                        }
                        for p in current["baseline"]
                    ],
                }
            )

    return {
        "campus": campus,
        "received": len(items),
        "raised": raised,
        "resolved": resolved,
        "active_alerts": [
            {"sample_date": _parse_day(r["sample_day"])}
            for r in conn.execute(
                "SELECT sample_day FROM alerts WHERE campus = ? ORDER BY sample_day",
                (campus,),
            ).fetchall()
        ],
    }
