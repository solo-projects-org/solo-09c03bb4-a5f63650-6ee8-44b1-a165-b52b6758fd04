"""FastAPI 层测试：状态码、响应结构、Swagger 样例、参数校验与事务回滚。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import database
from app.main import app
from app.schemas import (
    EX_BACKFILL_OLD_DAY,
    EX_BATCH_INITIAL,
    EX_BATCH_LATE_DAY,
    EX_WITHDRAW_OLD_DAY,
)

client = TestClient(app)


def test_health(temp_db):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "daily-volume-alert"}


def test_swagger_examples_run_as_lifecycle(temp_db):
    # ① 初始录入：11-11 新增告警
    resp = client.post("/api/v1/daily-counts/batch", json=EX_BATCH_INITIAL)
    assert resp.status_code == 200
    body = resp.json()
    assert body["received"] == 7
    assert [a["sample_date"] for a in body["raised"]] == ["2026-11-11"]
    raised = body["raised"][0]
    assert raised["row_count"] == 90
    assert raised["median"] == 995
    assert {p["sample_date"] for p in raised["baseline"]} == {
        "2026-10-07", "2026-10-14", "2026-10-28", "2026-11-04",
    }
    assert body["resolved"] == []

    # ② 补报到货：11-18 新增告警
    resp = client.post("/api/v1/daily-counts/batch", json=EX_BATCH_LATE_DAY)
    body = resp.json()
    assert [a["sample_date"] for a in body["raised"]] == ["2026-11-18"]
    assert body["raised"][0]["median"] == 985
    assert [a["sample_date"] for a in body["active_alerts"]] == [
        "2026-11-11", "2026-11-18",
    ]

    # ③ 补报旧日：11-18 解除
    resp = client.post("/api/v1/daily-counts/batch", json=EX_BACKFILL_OLD_DAY)
    body = resp.json()
    assert body["raised"] == []
    assert [a["sample_date"] for a in body["resolved"]] == ["2026-11-18"]
    assert [a["sample_date"] for a in body["active_alerts"]] == ["2026-11-11"]

    # ④ 撤回旧日：11-18 再次新增
    resp = client.post("/api/v1/daily-counts/batch", json=EX_WITHDRAW_OLD_DAY)
    body = resp.json()
    assert [a["sample_date"] for a in body["raised"]] == ["2026-11-18"]
    assert body["resolved"] == []


def test_get_daily_counts_and_alerts_views(temp_db):
    client.post("/api/v1/daily-counts/batch", json=EX_BATCH_INITIAL)

    resp = client.get("/api/v1/daily-counts", params={"campus": "院区A"})
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 7
    assert all(set(r) == {"sample_date", "weekday", "row_count", "updated_at"}
               for r in rows)
    by_day = {r["sample_date"]: r for r in rows}
    assert by_day["2026-11-18"]["row_count"] is None  # 未收齐
    assert by_day["2026-11-11"]["row_count"] == 90
    assert by_day["2026-11-11"]["weekday"] == 2        # 星期三
    # 日期升序
    assert [r["sample_date"] for r in rows] == sorted(r["sample_date"] for r in rows)

    resp = client.get("/api/v1/alerts", params={"campus": "院区A"})
    assert resp.status_code == 200
    alerts = resp.json()
    assert [a["sample_date"] for a in alerts] == ["2026-11-11"]
    assert len(alerts[0]["baseline"]) == 4
    assert alerts[0]["median"] == 995

    # 未知院区返回空列表
    assert client.get("/api/v1/alerts", params={"campus": "不存在"}).json() == []
    assert client.get("/api/v1/daily-counts", params={"campus": "不存在"}).json() == []


def test_validation_errors_return_422(temp_db):
    base = {
        "campus": "院区A",
        "entries": [{"sample_date": "2026-11-11", "row_count": 1}],
    }

    # 负数
    bad = {"campus": "院区A",
           "entries": [{"sample_date": "2026-11-11", "row_count": -1}]}
    assert client.post("/api/v1/daily-counts/batch", json=bad).status_code == 422

    # 布尔值
    bad = {"campus": "院区A",
           "entries": [{"sample_date": "2026-11-11", "row_count": True}]}
    assert client.post("/api/v1/daily-counts/batch", json=bad).status_code == 422

    # 浮点
    bad = {"campus": "院区A",
           "entries": [{"sample_date": "2026-11-11", "row_count": 1.5}]}
    assert client.post("/api/v1/daily-counts/batch", json=bad).status_code == 422

    # 非法日期
    bad = {"campus": "院区A",
           "entries": [{"sample_date": "2026-13-40", "row_count": 1}]}
    assert client.post("/api/v1/daily-counts/batch", json=bad).status_code == 422

    # 同批日期重复
    bad = {"campus": "院区A", "entries": [
        {"sample_date": "2026-11-11", "row_count": 1},
        {"sample_date": "2026-11-11", "row_count": 2},
    ]}
    assert client.post("/api/v1/daily-counts/batch", json=bad).status_code == 422

    # 空 entries
    bad = {"campus": "院区A", "entries": []}
    assert client.post("/api/v1/daily-counts/batch", json=bad).status_code == 422

    # 空白院区
    bad = {"campus": "   ", "entries": base["entries"]}
    assert client.post("/api/v1/daily-counts/batch", json=bad).status_code == 422

    # GET 查询空院区
    assert client.get("/api/v1/alerts", params={"campus": "  "}).status_code == 422

    # 校验失败不落库
    assert client.get("/api/v1/daily-counts",
                      params={"campus": "院区A"}).json() == []


def test_openapi_contains_ordered_examples_and_docs(temp_db):
    schema = client.get("/openapi.json").json()
    body = schema["paths"]["/api/v1/daily-counts/batch"]["post"]["requestBody"]
    examples = body["content"]["application/json"]["examples"]
    assert list(examples) == ["1_initial", "2_late_day", "3_backfill", "4_withdraw"]
    summaries = " ".join(e["summary"] for e in examples.values())
    for keyword in ("录入", "补报", "撤回"):
        assert keyword in summaries
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200


def test_method_endpoint(temp_db):
    resp = client.get("/method")
    assert resp.status_code == 200
    text = resp.text
    assert "星期几" in text and "中位数" in text and "回滚" in text


def test_500_path_rolls_back_and_keeps_service_up(temp_db):
    # 手动打补丁并延迟恢复：TestClient 的 anyio 门户会在请求结束时提前清理
    # monkeypatch，无法覆盖回滚后的验证步骤
    original = database._evaluate

    def boom(conn, campus, target):
        raise RuntimeError("boom")

    database._evaluate = boom
    try:
        resp = client.post(
            "/api/v1/daily-counts/batch",
            json={"campus": "院区Q",
                  "entries": [{"sample_date": "2026-11-11", "row_count": 1}]},
        )
        assert resp.status_code == 500
        assert "已回滚" in resp.json()["detail"]
        # 失败事务整体回滚：补丁仍生效，库中无任何残留
        assert client.get("/api/v1/daily-counts",
                          params={"campus": "院区Q"}).json() == []
    finally:
        database._evaluate = original

    # 恢复后服务可继续正常提交
    resp = client.post(
        "/api/v1/daily-counts/batch",
        json={"campus": "院区Q",
              "entries": [{"sample_date": "2026-11-11", "row_count": 1}]},
    )
    assert resp.status_code == 200
    assert resp.json()["received"] == 1
