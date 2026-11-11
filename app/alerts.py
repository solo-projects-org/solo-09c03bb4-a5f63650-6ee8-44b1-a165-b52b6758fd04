"""日量异常判定的纯领域逻辑（不依赖数据库，便于单测）。

判定规则
--------
判断采样日 ``d`` 是否异常，需同时满足：

1. ``d`` 本身已收齐（日量不为 null）；
2. 本院区、与 ``d`` 同星期几、早于 ``d`` 的已收齐采样日中，最近的 4 个
   构成基准；不足 4 个时不判定、不告警；
3. ``d`` 的行数严格低于这 4 个基准行数中位数的一半。

四个数的中位数为排序后中间两数之和除以 2。为避免浮点误差，记
``median_num`` 为中间两数之和（中位数 = median_num / 2），则
“行数 < 中位数 / 2”等价于整数比较 ``4 * row_count < median_num``。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class BaselinePoint:
    """基准中的一个采样日及其入仓行数。"""

    sample_date: date
    row_count: int


@dataclass(frozen=True)
class AlertDecision:
    """一次判定的告警结论；不告警时 ``decide`` 返回 None。"""

    median_num: int
    baseline: tuple[BaselinePoint, ...]

    @property
    def median(self) -> int | float:
        """中位数：偶数为整数，奇数为 x.5。"""
        value = self.median_num / 2
        return int(value) if self.median_num % 2 == 0 else value


def median_num_of_four(values: list[int]) -> int:
    """四个非负整数中位数的分子（排序后中间两数之和）。"""
    ordered = sorted(values)
    if len(ordered) != 4:
        raise ValueError("基准必须恰好包含 4 个已收齐采样日")
    return ordered[1] + ordered[2]


def is_below_half_median(row_count: int, median_num: int) -> bool:
    """行数是否严格低于中位数的一半（全程整数运算）。"""
    return 4 * row_count < median_num


def decide(
    row_count: int | None,
    prior_collected: list[tuple[date, int]],
) -> AlertDecision | None:
    """针对单个采样日给出告警结论。

    ``prior_collected`` 为该采样日之前、同院区同星期几的**已收齐**
    （日期, 行数）序列，顺序不限；函数内部只取最近 4 个。
    未收齐（``row_count is None``）或基准不足 4 个时返回 None。
    """
    if row_count is None:
        return None

    recent = sorted(prior_collected, key=lambda point: point[0], reverse=True)[:4]
    if len(recent) < 4:
        return None

    median_num = median_num_of_four([count for _, count in recent])
    if not is_below_half_median(row_count, median_num):
        return None

    baseline = tuple(
        BaselinePoint(sample_date=d, row_count=c)
        for d, c in sorted(recent, key=lambda point: point[0])
    )
    return AlertDecision(median_num=median_num, baseline=baseline)


def collect_affected_dates(
    changed_dates: list[date],
    later_rows: dict[int, list[tuple[date, int | None]]],
) -> set[date]:
    """计算一批改动后需要重新判定的全部日期。

    重判范围 = 改动日本身 ∪ 基准可能被改动日影响的后续同星期几日期。

    对某个改动日 ``c`` 与后续日期 ``t``，``c`` 可能进入 ``t`` 的最近 4 个
    基准，当且仅当 ``c`` 与 ``t`` **之间**（不含两端）的已收齐日少于 4 个：
    其间已有 4 个已收齐日时，它们一律比 ``c`` 更近，``c`` 无论收齐与否都
    进不了 ``t`` 的基准，再往后的日期同理，即可停止遍历。未收齐日（null）
    不占用这个计数，但其本身仍需重判（撤回可能解除已有告警）。

    ``later_rows`` 按星期几（``date.weekday()``）给出该校区全部同星期几
    行（日期, 行数或 None），日期升序。
    """
    affected: set[date] = set(changed_dates)
    for changed in changed_dates:
        # 已遇到的、严格位于 changed 与当前候选日之间的已收齐日数量
        collected_between = 0
        for sample_date, row_count in later_rows.get(changed.weekday(), []):
            if sample_date <= changed:
                continue
            # 之间已有 4 个已收齐日：改动日不可能再进入候选日及其后日期的基准
            if collected_between == 4:
                break
            affected.add(sample_date)
            if row_count is not None:
                collected_between += 1
    return affected
