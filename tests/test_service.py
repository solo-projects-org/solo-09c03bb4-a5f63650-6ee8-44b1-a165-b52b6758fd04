"""服务层测试：完整告警生命周期、覆盖/撤回、隔离、原子性与并发。"""

from __future__ import annotations

import threading
from datetime import date, timedelta

import pytest

from app import alerts, database

D = date.fromisoformat


def submit(campus: str, mapping: dict[str, int | None]) -> dict:
    items = [(D(day), value) for day, value in mapping.items()]
    with database.transaction() as conn:
        return database.submit_batch(conn, campus, items)


def active_days(result: dict) -> list[str]:
    return [a["sample_date"].isoformat() for a in result["active_alerts"]]


# ---------------------------------------------------------------------------
# Swagger 样例对应的完整生命周期
# ---------------------------------------------------------------------------

def test_full_lifecycle_initial_batch_raises_one(temp_db):
    result = submit("院区A", {
        "2026-09-30": 1000,
        "2026-10-07": 980,
        "2026-10-14": 1020,
        "2026-10-28": 960,
        "2026-11-04": 1010,
        "2026-11-11": 90,
        "2026-11-18": None,
    })
    assert result["received"] == 7
    assert result["resolved"] == []
    assert [a["sample_date"].isoformat() for a in result["raised"]] == ["2026-11-11"]
    alert = result["raised"][0]
    assert alert["row_count"] == 90
    assert alert["median"] == 995
    assert [(p["sample_date"].isoformat(), p["row_count"]) for p in alert["baseline"]] == [
        ("2026-10-07", 980),
        ("2026-10-14", 1020),
        ("2026-10-28", 960),
        ("2026-11-04", 1010),
    ]
    assert active_days(result) == ["2026-11-11"]


def test_full_lifecycle_late_day_raises_second_alert(temp_db):
    submit("院区A", {
        "2026-09-30": 1000, "2026-10-07": 980, "2026-10-14": 1020,
        "2026-10-28": 960, "2026-11-04": 1010, "2026-11-11": 90,
        "2026-11-18": None,
    })
    result = submit("院区A", {"2026-11-18": 300})
    assert [a["sample_date"].isoformat() for a in result["raised"]] == ["2026-11-18"]
    alert = result["raised"][0]
    assert alert["median"] == 985
    assert [p["sample_date"].isoformat() for p in alert["baseline"]] == [
        "2026-10-14", "2026-10-28", "2026-11-04", "2026-11-11",
    ]
    assert active_days(result) == ["2026-11-11", "2026-11-18"]


def test_full_lifecycle_backfill_resolves_later_alert(temp_db):
    submit("院区A", {
        "2026-09-30": 1000, "2026-10-07": 980, "2026-10-14": 1020,
        "2026-10-28": 960, "2026-11-04": 1010, "2026-11-11": 90,
        "2026-11-18": None,
    })
    submit("院区A", {"2026-11-18": 300})

    with database.read_connection() as conn:
        before = {a["sample_date"]: a["triggered_at"]
                  for a in database.get_active_alerts(conn, "院区A")}

    result = submit("院区A", {"2026-10-21": 100})
    assert result["raised"] == []
    assert [a["sample_date"].isoformat() for a in result["resolved"]] == ["2026-11-18"]
    assert active_days(result) == ["2026-11-11"]

    # 11-11 仍告警但基准已变化，且首次触发时间保留
    with database.read_connection() as conn:
        views = {a["sample_date"].isoformat(): a
                 for a in database.get_active_alerts(conn, "院区A")}
    assert set(views) == {"2026-11-11"}
    baseline_days = [p.sample_date.isoformat()
                     for p in views["2026-11-11"]["baseline"]]
    assert baseline_days == [
        "2026-10-14", "2026-10-21", "2026-10-28", "2026-11-04",
    ]
    assert views["2026-11-11"]["triggered_at"] == before[D("2026-11-11")]


def test_full_lifecycle_withdraw_re_raises_alert(temp_db):
    submit("院区A", {
        "2026-09-30": 1000, "2026-10-07": 980, "2026-10-14": 1020,
        "2026-10-28": 960, "2026-11-04": 1010, "2026-11-11": 90,
        "2026-11-18": None,
    })
    submit("院区A", {"2026-11-18": 300})
    submit("院区A", {"2026-10-21": 100})

    result = submit("院区A", {"2026-10-21": None})
    assert [a["sample_date"].isoformat() for a in result["raised"]] == ["2026-11-18"]
    assert result["resolved"] == []
    assert active_days(result) == ["2026-11-11", "2026-11-18"]

    # 撤回后日量表中保留 null 行
    with database.read_connection() as conn:
        counts = {c["sample_date"].isoformat(): c["row_count"]
                  for c in database.get_daily_counts(conn, "院区A")}
    assert counts["2026-10-21"] is None


# ---------------------------------------------------------------------------
# null 与 0、覆盖更正
# ---------------------------------------------------------------------------

def test_zero_is_collected_and_can_alert_but_null_is_ignored(temp_db):
    # 4 个同星期几基准，中位数 10，一半 5
    base = {
        "2026-10-14": 10, "2026-10-21": 10,
        "2026-10-28": 10, "2026-11-04": 10,
    }
    submit("院区B", base)
    # 收齐但无记录：0 参与判定，触发告警
    result = submit("院区B", {"2026-11-11": 0})
    assert [a["sample_date"].isoformat() for a in result["raised"]] == ["2026-11-11"]

    # 未收齐 null：自身不判定，也不进入后续基准
    result = submit("院区B", {"2026-11-18": None})
    assert result["raised"] == [] and result["resolved"] == []
    result = submit("院区B", {"2026-11-25": 0})
    # 11-25 的基准只能取 11-11(0)、11-04、10-28、10-21；11-18 null 被跳过
    baseline_days = [
        p["sample_date"].isoformat() for p in result["raised"][0]["baseline"]
    ]
    assert "2026-11-18" not in baseline_days
    assert baseline_days == [
        "2026-10-21", "2026-10-28", "2026-11-04", "2026-11-11",
    ]


def test_correction_overwrites_old_value_and_resolves_alert(temp_db):
    submit("院区C", {
        "2026-10-14": 1000, "2026-10-21": 1000,
        "2026-10-28": 1000, "2026-11-04": 1000,
    })
    result = submit("院区C", {"2026-11-11": 10})
    assert active_days(result) == ["2026-11-11"]

    # 同一天新数据覆盖旧值：改到阈值以上，改动日本身重判后解除
    result = submit("院区C", {"2026-11-11": 600})
    assert result["raised"] == []
    assert [a["sample_date"].isoformat() for a in result["resolved"]] == ["2026-11-11"]
    assert result["active_alerts"] == []

    with database.read_connection() as conn:
        counts = {c["sample_date"].isoformat(): c["row_count"]
                  for c in database.get_daily_counts(conn, "院区C")}
    assert counts["2026-11-11"] == 600  # 旧值 10 被覆盖


def test_withdraw_alerting_day_removes_its_own_alert(temp_db):
    # 11-11 有 4 个已收齐基准而告警；撤回（置 null）后自身解除，
    # 后续 11-18 仍有 4 个更早已收齐日，告警持续但基准被刷新
    submit("院区D1", {
        "2026-10-14": 1000, "2026-10-21": 1000,
        "2026-10-28": 1000, "2026-11-04": 1000, "2026-11-11": 10,
    })
    submit("院区D1", {"2026-11-18": 10})
    result = submit("院区D1", {"2026-11-11": None})
    assert [a["sample_date"].isoformat() for a in result["resolved"]] == ["2026-11-11"]
    assert result["raised"] == []
    assert active_days(result) == ["2026-11-18"]
    with database.read_connection() as conn:
        view = database.get_active_alerts(conn, "院区D1")[0]
    assert [p.sample_date.isoformat() for p in view["baseline"]] == [
        "2026-10-14", "2026-10-21", "2026-10-28", "2026-11-04",
    ]


def test_withdraw_baseline_member_dissolves_dependent_alert(temp_db):
    # 11-18 之前恰好 4 个已收齐日（10-21…11-11），11-11 自身基准不足不告警；
    # 撤回 11-11 后 11-18 只剩 3 个已收齐前序日 → 基准不足 → 告警解除
    submit("院区D2", {
        "2026-10-21": 1000, "2026-10-28": 1000,
        "2026-11-04": 1000, "2026-11-11": 10,
    })
    result = submit("院区D2", {"2026-11-18": 10})
    assert active_days(result) == ["2026-11-18"]

    result = submit("院区D2", {"2026-11-11": None})
    assert result["raised"] == []
    assert [a["sample_date"].isoformat() for a in result["resolved"]] == ["2026-11-18"]
    assert result["active_alerts"] == []
    with database.read_connection() as conn:
        counts = {c["sample_date"].isoformat(): c["row_count"]
                  for c in database.get_daily_counts(conn, "院区D2")}
    assert counts["2026-11-11"] is None  # 撤回后保留 null 行


def test_campus_isolation(temp_db):
    submit("院区E", {
        "2026-10-14": 1000, "2026-10-21": 1000,
        "2026-10-28": 1000, "2026-11-04": 1000, "2026-11-11": 10,
    })
    # 院区F 数据很少，不足 4 个基准，绝不告警
    result = submit("院区F", {
        "2026-10-14": 1, "2026-11-11": 0,
    })
    assert result["raised"] == [] and result["resolved"] == []
    with database.read_connection() as conn:
        alerts_e = database.get_active_alerts(conn, "院区E")
        alerts_f = database.get_active_alerts(conn, "院区F")
    assert [a["sample_date"].isoformat() for a in alerts_e] == ["2026-11-11"]
    assert alerts_f == []


def test_other_weekdays_are_not_baseline(temp_db):
    # 星期三 2026-11-11 的判定只能参考星期三；混入 4 个星期一的高值也无用
    submit("院区G", {
        "2026-10-12": 1000, "2026-10-19": 1000,  # 星期一
        "2026-10-26": 1000, "2026-11-02": 1000,  # 星期一
        "2026-11-04": 50,                        # 星期三，唯一同星期几前序
        "2026-11-11": 1,                         # 星期三，基准不足 4 个
    })
    with database.read_connection() as conn:
        assert database.get_active_alerts(conn, "院区G") == []


def test_half_median_with_x5_median(temp_db):
    # 基准 [1, 2, 3, 4]：中位数 2.5，一半 1.25；1 告警，2 不告警
    submit("院区H", {
        "2026-10-14": 1, "2026-10-21": 2,
        "2026-10-28": 3, "2026-11-04": 4,
    })
    result = submit("院区H", {"2026-11-11": 1})
    assert result["raised"][0]["median"] == 2.5
    result = submit("院区H", {"2026-11-11": 2})  # 覆盖为 2，解除
    assert [a["sample_date"].isoformat() for a in result["resolved"]] == ["2026-11-11"]


# ---------------------------------------------------------------------------
# 原子性：事务中途失败不留部分写入
# ---------------------------------------------------------------------------

def test_failure_rolls_back_everything(temp_db, monkeypatch):
    def boom(conn, campus, target):
        raise RuntimeError("simulated evaluation failure")

    monkeypatch.setattr(database, "_evaluate", boom)
    with pytest.raises(RuntimeError):
        with database.transaction() as conn:
            database.submit_batch(conn, "院区X", [
                (D("2026-11-11"), 10), (D("2026-11-04"), 20),
            ])

    with database.read_connection() as conn:
        assert database.get_daily_counts(conn, "院区X") == []
        assert database.get_active_alerts(conn, "院区X") == []


# ---------------------------------------------------------------------------
# 并发：两线程交错提交，不丢更新，最终状态与串行重算一致
# ---------------------------------------------------------------------------

def test_concurrent_batches_no_lost_updates(temp_db):
    thread_one = {
        "2026-10-14": 1000, "2026-10-28": 1000,
        "2026-11-11": 10, "2026-11-25": 1000,
    }
    thread_two = {
        "2026-10-21": 1000, "2026-11-04": 1000,
        "2026-11-18": 10,
    }
    results: list[dict] = []
    barrier = threading.Barrier(2)

    def worker(mapping):
        barrier.wait()
        results.append(submit("院区Y", mapping))

    t1 = threading.Thread(target=worker, args=(thread_one,))
    t2 = threading.Thread(target=worker, args=(thread_two,))
    t1.start(); t2.start(); t1.join(); t2.join()

    # 所有日期都落库，无丢失
    with database.read_connection() as conn:
        counts = {c["sample_date"].isoformat(): c["row_count"]
                  for c in database.get_daily_counts(conn, "院区Y")}
    assert counts == {**thread_one, **thread_two}

    # 每个响应内部新增/解除互斥
    for result in results:
        raised = {a["sample_date"] for a in result["raised"]}
        resolved = {a["sample_date"] for a in result["resolved"]}
        assert raised.isdisjoint(resolved)

    # 最终告警与按最终数据从头串行重算的结论一致：
    # 11-11 与 11-18 基准均为 4 个 1000，中位数 1000 一半 500，10 < 500
    with database.read_connection() as conn:
        final_alerts = {
            a["sample_date"].isoformat()
            for a in database.get_active_alerts(conn, "院区Y")
        }
    assert final_alerts == {"2026-11-11", "2026-11-18"}


def test_pure_decide_matches_service_on_final_state(temp_db):
    """用 alerts.decide 独立复算服务层最终告警，交叉验证一致性。"""
    submit("院区Z", {
        "2026-09-30": 1000, "2026-10-07": 980, "2026-10-14": 1020,
        "2026-10-28": 960, "2026-11-04": 1010, "2026-11-11": 90,
        "2026-11-18": 300,
    })
    with database.read_connection() as conn:
        rows = database.get_daily_counts(conn, "院区Z")
        service_alerts = {
            a["sample_date"] for a in database.get_active_alerts(conn, "院区Z")
        }

    recomputed = _recompute_alerts(rows)
    assert recomputed == service_alerts


def _recompute_alerts(rows: list[dict]) -> set[date]:
    """按最终日量从头独立重算全部告警（不经过服务层增量逻辑）。"""
    collected = [(r["sample_date"], r["row_count"]) for r in rows
                 if r["row_count"] is not None]
    result: set[date] = set()
    for target, value in collected:
        prior = [(d, v) for d, v in collected if d < target
                 and d.weekday() == target.weekday()]
        if alerts.decide(value, prior) is not None:
            result.add(target)
    return result


def test_random_batches_match_brute_force_recompute(temp_db):
    """随机多批次（含更正与撤回）后，增量维护结果与全量重算逐次一致。"""
    import random

    rng = random.Random(20261111)
    campus = "随机院区"
    # 12 个连续星期三，行数在 0..1200 之间随机，约 15% 概率为 null
    days = [date(2026, 9, 2) + timedelta(weeks=i) for i in range(12)]
    for round_no in range(40):
        k = rng.randint(1, 4)
        picked = rng.sample(days, k)
        mapping = {}
        for d in picked:
            if rng.random() < 0.15:
                mapping[d.isoformat()] = None          # 未收齐/撤回
            else:
                mapping[d.isoformat()] = rng.randint(0, 1200)  # 补报/更正
        submit(campus, mapping)

        with database.read_connection() as conn:
            rows = database.get_daily_counts(conn, campus)
            service_alerts = {
                a["sample_date"]
                for a in database.get_active_alerts(conn, campus)
            }
        assert _recompute_alerts(rows) == service_alerts, round_no
