"""请求/响应 Pydantic 模型与 Swagger 样例。

Swagger 中的 4 个样例按顺序提交即可观察完整告警生命周期（样例均为
院区 A 的星期三数据，weekday 由日期本身决定）：

1. ``initial``：录入 6 个已收齐采样日 + 1 个未收齐日（null），
   2026-11-11 行数 90 低于最近 4 个同星期几中位数（995）的一半，告警；
2. ``late_day_arrives``：补报 2026-11-18 = 300，低于基准中位数一半，
   再新增一条告警；
3. ``backfill_old_day``：补报旧日 2026-10-21 = 100，后续日期基准改变，
   2026-11-18 不再满足告警条件而解除；
4. ``withdraw_old_day``：撤回 2026-10-21（置 null），基准复原，
   2026-11-18 重新告警。
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator

# ---------------------------------------------------------------------------
# Swagger 样例（请按 1 → 2 → 3 → 4 的顺序在同一院区上提交）
# ---------------------------------------------------------------------------

EX_BATCH_INITIAL: dict = {
    "campus": "院区A",
    "entries": [
        {"sample_date": "2026-09-30", "row_count": 1000},
        {"sample_date": "2026-10-07", "row_count": 980},
        {"sample_date": "2026-10-14", "row_count": 1020},
        {"sample_date": "2026-10-28", "row_count": 960},
        {"sample_date": "2026-11-04", "row_count": 1010},
        {"sample_date": "2026-11-11", "row_count": 90},
        # 11-18 尚未收齐：填 null，不参与任何基准
        {"sample_date": "2026-11-18", "row_count": None},
    ],
}

EX_BATCH_LATE_DAY: dict = {
    "campus": "院区A",
    "entries": [
        # 11-18 收齐：最近 4 个同星期几已收齐日为
        # 11-11(90)、11-04(1010)、10-28(960)、10-14(1020)，
        # 中位数 985，一半 492.5；300 < 492.5 → 新增告警
        {"sample_date": "2026-11-18", "row_count": 300}
    ],
}

EX_BACKFILL_OLD_DAY: dict = {
    "campus": "院区A",
    "entries": [
        # 补报旧日：10-21 进入 11-18 的最近 4 个基准，
        # 新基准 [90, 100, 960, 1010] 中位数 530，一半 265；
        # 300 不再低于 265 → 11-18 告警解除（11-11 仍告警）
        {"sample_date": "2026-10-21", "row_count": 100}
    ],
}

EX_WITHDRAW_OLD_DAY: dict = {
    "campus": "院区A",
    "entries": [
        # 撤回：10-21 重新置为未收齐（null），后续基准复原，
        # 11-18 重新满足告警条件 → 再次新增告警
        {"sample_date": "2026-10-21", "row_count": None}
    ],
}


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

class DailyCountEntry(BaseModel):
    """单个采样日的入仓行数。"""

    sample_date: date = Field(..., description="采样日（ISO 8601，如 2026-11-11）")
    row_count: StrictInt | None = Field(
        ...,
        description=(
            "当日入仓行数，非负整数；null 表示该采样日尚未收齐，"
            "收齐但无记录请显式填 0"
        ),
    )

    @field_validator("row_count")
    @classmethod
    def _reject_bool_and_negative(cls, value):
        if isinstance(value, bool):
            raise ValueError("row_count 不能是布尔值，须为非负整数或 null")
        if value is not None and value < 0:
            raise ValueError(f"row_count 不能为负数：{value}")
        return value


class BatchSubmitRequest(BaseModel):
    """一批提交：一个院区、若干采样日；同批日期不得重复。"""

    campus: str = Field(..., min_length=1, description="院区标识，去除首尾空白后不能为空")
    entries: list[DailyCountEntry] = Field(
        ..., min_length=1, description="本批采样日行数；同一天的新数据覆盖旧值"
    )

    @field_validator("campus")
    @classmethod
    def _strip_campus(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("campus 不能为空白字符串")
        return value

    @model_validator(mode="after")
    def _check_unique_dates(self) -> "BatchSubmitRequest":
        days = [entry.sample_date for entry in self.entries]
        if len(days) != len(set(days)):
            raise ValueError("同批提交中 sample_date 不能重复")
        return self


# ---------------------------------------------------------------------------
# 响应模型
# ---------------------------------------------------------------------------

class BaselinePointModel(BaseModel):
    sample_date: date = Field(..., description="基准采样日")
    row_count: int = Field(..., description="该基准日的入仓行数")


class RaisedAlert(BaseModel):
    sample_date: date = Field(..., description="新增告警的采样日")
    row_count: int = Field(..., description="该采样日的入仓行数")
    median: int | float = Field(
        ..., description="4 个基准行数的中位数（奇数和时为 x.5）"
    )
    baseline: list[BaselinePointModel] = Field(
        ..., description="基准日期及对应行数，按日期升序，恰好 4 个"
    )


class ResolvedAlert(BaseModel):
    sample_date: date = Field(..., description="本批解除告警的采样日")


class ActiveAlertDate(BaseModel):
    sample_date: date


class BatchSubmitResponse(BaseModel):
    campus: str
    received: int = Field(..., description="本批实际写入的采样日条数")
    raised: list[RaisedAlert] = Field(
        ..., description="本批新增告警（提交前不存在、提交后有效）"
    )
    resolved: list[ResolvedAlert] = Field(
        ..., description="本批解除告警（提交前有效、提交后不再成立）"
    )
    active_alerts: list[ActiveAlertDate] = Field(
        ..., description="提交后本院区全部有效告警日期，按日期升序"
    )


class DailyCountView(BaseModel):
    sample_date: date
    weekday: int = Field(..., description="星期几（0=周一 … 6=周日）")
    row_count: int | None = Field(..., description="null 表示尚未收齐")
    updated_at: str


class AlertView(BaseModel):
    sample_date: date
    weekday: int
    row_count: int
    median: int | float
    baseline: list[BaselinePointModel]
    triggered_at: str


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
