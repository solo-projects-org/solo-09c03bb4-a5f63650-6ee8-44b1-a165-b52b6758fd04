"""FastAPI 应用：院区病例入仓日量告警服务。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Body, FastAPI, HTTPException, Query

from . import database
from .schemas import (
    EX_BACKFILL_OLD_DAY,
    EX_BATCH_INITIAL,
    EX_BATCH_LATE_DAY,
    EX_WITHDRAW_OLD_DAY,
    AlertView,
    BatchSubmitRequest,
    BatchSubmitResponse,
    DailyCountView,
    HealthResponse,
)

app = FastAPI(
    title="院区病例入仓日量告警 API",
    description=(
        "按院区、采样日记录入仓行数并判定日量异常。判定某天是否异常时，"
        "只看本院区此前最近 4 个与当天同星期几且已收齐的采样日；凑齐 4 天、"
        "当天也已收齐且行数低于这 4 天中位数的一半时发出告警。补报、更正或"
        "撤回后只重判有改动的日期和基准可能改变的后续日期；日量与告警状态在"
        "同一 SQLite 事务内提交，失败整体回滚。"
    ),
    version="1.0.0",
)


@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["运维"],
    summary="健康检查",
)
def health() -> dict:
    return {"status": "ok", "service": "daily-volume-alert"}


@app.post(
    "/api/v1/daily-counts/batch",
    response_model=BatchSubmitResponse,
    tags=["日量告警"],
    summary="提交一批日量，返回本批新增与解除的告警",
)
def submit_batch(
    payload: Annotated[
        BatchSubmitRequest,
        Body(
            openapi_examples={
                "1_initial": {
                    "summary": "① 录入日量：7 个采样日（含 1 个未收齐 null）",
                    "description": (
                        "2026-11-11 行数 90，低于最近 4 个同星期几已收齐日"
                        "[960, 980, 1010, 1020] 中位数 995 的一半 → 新增告警；"
                        "2026-11-18 为 null，不参与判定"
                    ),
                    "value": EX_BATCH_INITIAL,
                },
                "2_late_day": {
                    "summary": "② 补报到货：2026-11-18 收齐为 300",
                    "description": (
                        "300 低于其最近 4 个同星期几基准中位数 985 的一半"
                        "（492.5）→ 再新增一条告警"
                    ),
                    "value": EX_BATCH_LATE_DAY,
                },
                "3_backfill": {
                    "summary": "③ 补报旧日：2026-10-21 = 100",
                    "description": (
                        "旧日进入 11-18 的最近 4 个基准，新中位数 530 的一半"
                        "为 265，300 不再低于它 → 11-18 告警解除；11-11 仍告警"
                    ),
                    "value": EX_BACKFILL_OLD_DAY,
                },
                "4_withdraw": {
                    "summary": "④ 撤回旧日：2026-10-21 置 null",
                    "description": (
                        "撤回后后续日期基准复原，11-18 重新满足告警条件 → "
                        "再次新增告警"
                    ),
                    "value": EX_WITHDRAW_OLD_DAY,
                },
            }
        ),
    ]
):
    items = [(entry.sample_date, entry.row_count) for entry in payload.entries]
    try:
        with database.transaction() as conn:
            return database.submit_batch(conn, payload.campus, items)
    except Exception as exc:  # pragma: no cover - 防御性兜底
        raise HTTPException(status_code=500, detail=f"批次提交失败，已回滚：{exc}")


@app.get(
    "/api/v1/daily-counts",
    response_model=list[DailyCountView],
    tags=["日量告警"],
    summary="查询某院区全部采样日日量（按日期升序）",
)
def list_daily_counts(campus: str = Query(..., description="院区标识")) -> list[dict]:
    if not campus.strip():
        raise HTTPException(status_code=422, detail="campus 不能为空")
    with database.read_connection() as conn:
        return database.get_daily_counts(conn, campus.strip())


@app.get(
    "/api/v1/alerts",
    response_model=list[AlertView],
    tags=["日量告警"],
    summary="查询某院区当前有效告警（含基准日期与行数）",
)
def list_alerts(campus: str = Query(..., description="院区标识")) -> list[dict]:
    if not campus.strip():
        raise HTTPException(status_code=422, detail="campus 不能为空")
    with database.read_connection() as conn:
        views = database.get_active_alerts(conn, campus.strip())
    return [
        {
            "sample_date": view["sample_date"],
            "weekday": view["weekday"],
            "row_count": view["row_count"],
            "median": view["median"],
            "baseline": [
                {
                    "sample_date": p.sample_date,
                    "row_count": p.row_count,
                }
                for p in view["baseline"]
            ],
            "triggered_at": view["triggered_at"],
        }
        for view in views
    ]


@app.get(
    "/method",
    tags=["日量告警"],
    summary="判定规则说明",
)
def method() -> dict:
    return {
        "scope": "仅在同一院区内判定，不同院区数据互不影响",
        "weekday_rule": "只选取与被判定采样日同星期几的历史采样日",
        "baseline_rule": (
            "取被判定日之前、最近 4 个已收齐（row_count 非 null）的同星期几"
            "采样日；不足 4 个不判定"
        ),
        "median_rule": "4 个基准行数排序后取中间两数之和除以 2",
        "alert_rule": "当天已收齐且 row_count 严格小于中位数的一半才告警",
        "integer_arithmetic": (
            "记中间两数之和为 median_num，则判定式为 4*row_count < median_num，"
            "全程整数运算，无浮点误差"
        ),
        "null_vs_zero": "null=尚未收齐，不参与基准、自身不判定；0=收齐但无记录，正常参与",
        "reevaluation": (
            "补报/更正/撤回后重算改动日及后续同星期几日期；当改动日与某后续日"
            "之间已有 4 个已收齐日时停止，未收齐日不占用该计数"
        ),
        "atomicity": "日量与告警状态在同一 SQLite 事务内提交，失败整体回滚",
        "concurrency": (
            "批次经进程内串行锁 + BEGIN IMMEDIATE 逐个提交，返回变化以各批实际"
            "提交前后的状态为准"
        ),
    }
