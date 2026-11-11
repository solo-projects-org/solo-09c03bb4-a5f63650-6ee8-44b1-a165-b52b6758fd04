"""告警领域逻辑单测：中位数、半数阈值、基准选择、受影响日期范围。"""

from __future__ import annotations

from datetime import date

import pytest

from app import alerts

D = date.fromisoformat


# ---------------------------------------------------------------------------
# 中位数与半数阈值
# ---------------------------------------------------------------------------

def test_median_num_of_four_sorted_middle_pair():
    assert alerts.median_num_of_four([10, 20, 30, 40]) == 50
    assert alerts.median_num_of_four([40, 10, 30, 20]) == 50
    assert alerts.median_num_of_four([0, 0, 0, 1]) == 0
    assert alerts.median_num_of_four([7, 7, 7, 7]) == 14


def test_median_num_requires_exactly_four():
    with pytest.raises(ValueError):
        alerts.median_num_of_four([1, 2, 3])


def test_below_half_median_integer_arithmetic():
    # 中位数 10（median_num=20），一半为 5：严格小于
    assert alerts.is_below_half_median(4, 20) is True
    assert alerts.is_below_half_median(5, 20) is False
    assert alerts.is_below_half_median(6, 20) is False

    # 中位数 5.5（median_num=11），一半为 2.75
    assert alerts.is_below_half_median(2, 11) is True
    assert alerts.is_below_half_median(3, 11) is False


def test_alert_decision_median_representation():
    prior = [(D("2026-11-04"), 10), (D("2026-10-28"), 20),
             (D("2026-10-21"), 30), (D("2026-10-14"), 40)]
    decision = alerts.decide(4, prior)
    assert decision is not None
    assert decision.median_num == 50
    assert decision.median == 25
    assert [p.sample_date for p in decision.baseline] == [
        D("2026-10-14"), D("2026-10-21"), D("2026-10-28"), D("2026-11-04")
    ]

    odd = alerts.decide(1, [(D("2026-11-04"), 1), (D("2026-10-28"), 2),
                            (D("2026-10-21"), 3), (D("2026-10-14"), 4)])
    assert odd is not None
    assert odd.median_num == 5
    assert odd.median == 2.5


# ---------------------------------------------------------------------------
# decide：未收齐 / 基准不足 / 阈值边界
# ---------------------------------------------------------------------------

def test_decide_none_when_current_not_collected():
    prior = [(D("2026-11-04"), 1), (D("2026-10-28"), 2),
             (D("2026-10-21"), 3), (D("2026-10-14"), 4)]
    assert alerts.decide(None, prior) is None


def test_decide_none_when_fewer_than_four_baselines():
    assert alerts.decide(0, []) is None
    assert alerts.decide(0, [(D("2026-11-04"), 100)]) is None
    three = [(D("2026-11-04"), 100), (D("2026-10-28"), 100),
             (D("2026-10-21"), 100)]
    assert alerts.decide(0, three) is None


def test_decide_uses_only_four_most_recent():
    # 5 个候选，最旧的一个不应进入基准
    prior = [
        (D("2026-09-16"), 1000),  # 最旧，应被排除
        (D("2026-10-14"), 1000),
        (D("2026-10-21"), 1000),
        (D("2026-10-28"), 1000),
        (D("2026-11-04"), 1000),
    ]
    decision = alerts.decide(1, prior)
    assert decision is not None
    assert D("2026-09-16") not in [p.sample_date for p in decision.baseline]
    assert len(decision.baseline) == 4


def test_decide_threshold_boundary_not_strictly_below():
    # 基准 [8, 9, 11, 12]，中位数 10，一半 5
    prior = [(D("2026-11-04"), 12), (D("2026-10-28"), 11),
             (D("2026-10-21"), 9), (D("2026-10-14"), 8)]
    assert alerts.decide(4, prior) is not None  # 4 < 5
    assert alerts.decide(5, prior) is None       # 5 不严格小于 5
    assert alerts.decide(100, prior) is None


def test_decide_half_median_half_integer_case():
    # 基准 [1, 2, 3, 4]，中位数 2.5，一半 1.25
    prior = [(D("2026-11-04"), 4), (D("2026-10-28"), 3),
             (D("2026-10-21"), 2), (D("2026-10-14"), 1)]
    assert alerts.decide(1, prior) is not None  # 1 < 1.25
    assert alerts.decide(2, prior) is None       # 2 >= 1.25


def test_decide_zero_row_count_can_alert():
    prior = [(D("2026-11-04"), 10), (D("2026-10-28"), 10),
             (D("2026-10-21"), 10), (D("2026-10-14"), 10)]
    decision = alerts.decide(0, prior)
    assert decision is not None  # 收齐但无记录的 0 也参与判定


def test_decide_zero_median_never_alerts():
    # 基准全为 0：中位数一半也是 0，0 不严格小于 0
    prior = [(D("2026-11-04"), 0), (D("2026-10-28"), 0),
             (D("2026-10-21"), 0), (D("2026-10-14"), 0)]
    assert alerts.decide(0, prior) is None


# ---------------------------------------------------------------------------
# collect_affected_dates：重判范围
# ---------------------------------------------------------------------------

WED = 2  # 2026 年这些日期都是星期三


def _rows(pairs):
    return [(D(d), v) for d, v in pairs]


def test_affected_includes_changed_dates_and_four_collected_successors():
    changed = [D("2026-10-21")]
    later = {
        WED: _rows([
            ("2026-10-21", 100),
            ("2026-10-28", 960),   # 之间 0 个 → 影响
            ("2026-11-04", 1010),  # 1
            ("2026-11-11", 90),    # 2
            ("2026-11-18", 300),   # 3
            ("2026-11-25", 500),   # 之间 4 个 → 改动日进不了其基准
            ("2026-12-02", 500),   # 遍历在此之前停止
        ])
    }
    affected = alerts.collect_affected_dates(changed, later)
    assert affected == {
        D("2026-10-21"), D("2026-10-28"), D("2026-11-04"),
        D("2026-11-11"), D("2026-11-18"),
    }
    assert D("2026-11-25") not in affected
    assert D("2026-12-02") not in affected


def test_affected_null_days_do_not_consume_baseline_seats():
    changed = [D("2026-10-21")]
    later = {
        WED: _rows([
            ("2026-10-21", 100),
            ("2026-10-28", None),   # 未收齐：仍需重判且不占“之间已收齐”计数
            ("2026-11-04", 1010),   # 之间 0
            ("2026-11-11", 90),     # 1
            ("2026-11-18", 300),    # 2
            ("2026-11-25", 500),    # 3
            ("2026-12-02", 500),    # 之间 4 个（null 不占计数）→ 不影响
            ("2026-12-09", 500),
        ])
    }
    affected = alerts.collect_affected_dates(changed, later)
    assert D("2026-10-28") in affected  # null 日也重判
    assert D("2026-11-25") in affected
    assert D("2026-12-02") not in affected
    assert D("2026-12-09") not in affected


def test_affected_multiple_changed_dates_union():
    changed = [D("2026-10-14"), D("2026-10-21")]
    later = {
        WED: _rows([
            ("2026-10-14", 1020),
            ("2026-10-21", 100),
            ("2026-10-28", 960),
            ("2026-11-04", 1010),
            ("2026-11-11", 90),
            ("2026-11-18", 300),
            ("2026-11-25", 500),
        ])
    }
    affected = alerts.collect_affected_dates(changed, later)
    # 两个改动日都在集合中，并集只覆盖“之间已收齐日 < 4”的后续日期
    assert changed[0] in affected and changed[1] in affected
    # 对 10-14 而言 11-25 之间已有 5 个已收齐日（含 10-21…11-18），不受影响
    assert D("2026-11-25") not in affected
