# 院区病例入仓日量告警 API

院区病例底表入仓行数突然变少时，值班员需要快速判断是否存在漏载。本服务按
**院区 + 采样日**维护入仓行数，并基于同星期几的历史日量自动判定异常、维护
告警状态，支持补报、更正与撤回后的增量重判。

## 判定规则

对某院区的采样日 `d`，**同时满足**以下条件才发出告警：

1. `d` 当天已收齐（行数不为 `null`）；
2. 本院区、与 `d` **同星期几**、且**早于 `d`** 的已收齐采样日中，最近的
   4 个构成基准；不足 4 个时不判定、不告警；
3. `d` 的行数**严格低于**这 4 个基准行数中位数的一半。

约定与细节：

- 行数为**非负整数**；未收齐填 `null`，收齐但没有记录填 `0`（`0` 正常参与
  判定，`null` 不参与任何基准、自身也不判定）。
- 4 个数的中位数为排序后中间两数之和除以 2。实现全程整数运算：记中间两数
  之和为 `median_num`，判定式为 `4 * row_count < median_num`，无浮点误差。
- 同批日期不允许重复；同一天的新数据覆盖旧值（含撤回为 `null`）。
- 补报、更正或撤回后，只重判**改动日**与**基准可能被它影响的后续同星期几
  日期**：改动日与某后续日之间已有 4 个已收齐日时，改动日不可能再进入后者
  的基准，遍历即停止；未收齐日不占用该计数，但本身仍会重判。
- 不同院区数据完全隔离。

## 目录结构

```
.
├── app/
│   ├── __init__.py
│   ├── alerts.py        # 纯领域逻辑：中位数、半数阈值、基准选择、重判范围
│   ├── database.py      # SQLite 表结构、事务、增量重判与告警状态同步
│   ├── main.py          # FastAPI 路由（含 4 个有序 Swagger 样例）
│   └── schemas.py       # Pydantic 请求/响应模型与样例数据
├── tests/               # pytest：领域逻辑、服务层、API、随机交叉验证、并发
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── requirements-dev.txt
```

## 启动方式（Docker Compose）

```bash
docker compose up --build
```

SQLite 数据库文件位于容器内 `/data/daily_alert.db`，通过命名卷 `alert-data`
持久化，重启容器不丢数据；路径可用环境变量 `DAILY_ALERT_DB` 覆盖。

启动后：

- Swagger UI：<http://localhost:8000/docs> （可直接选用内置样例提交）
- ReDoc：<http://localhost:8000/redoc>
- 健康检查：<http://localhost:8000/health>
- 规则说明：<http://localhost:8000/method>

停止：`docker compose down`（加 `-v` 同时删除数据卷）。

## Swagger 样例演示（先录入日量，再补报旧日）

Swagger 的请求体示例按 ① → ② → ③ → ④ 顺序在同一院区提交，可观察完整告警
生命周期（样例日期均为星期三）：

| 步骤 | 样例 | 内容 | 结果 |
| --- | --- | --- | --- |
| ① | `1_initial` | 6 个已收齐日 + 1 个 `null` 未收齐日 | 2026-11-11 行数 90 < 基准中位数 995 的一半，**新增告警** |
| ② | `2_late_day` | 2026-11-18 收齐为 300 | 300 < 中位数 985 的一半 492.5，**再新增一条告警** |
| ③ | `3_backfill` | 补报旧日 2026-10-21 = 100 | 11-18 的基准被改写，新中位数 530 的一半为 265，300 不再低于它，**告警解除**；11-11 仍告警（基准同步刷新） |
| ④ | `4_withdraw` | 撤回 2026-10-21（置 `null`） | 后续基准复原，11-18 重新满足条件，**再次新增告警** |

也可用 curl 复现（以第 ① 步为例）：

```bash
curl -X POST http://localhost:8000/api/v1/daily-counts/batch \
  -H "Content-Type: application/json" \
  -d '{"campus":"院区A","entries":[
    {"sample_date":"2026-09-30","row_count":1000},
    {"sample_date":"2026-10-07","row_count":980},
    {"sample_date":"2026-10-14","row_count":1020},
    {"sample_date":"2026-10-28","row_count":960},
    {"sample_date":"2026-11-04","row_count":1010},
    {"sample_date":"2026-11-11","row_count":90},
    {"sample_date":"2026-11-18","row_count":null}
  ]}'
```

## 接口

### `POST /api/v1/daily-counts/batch`：提交一批日量

请求体：

```json
{
  "campus": "院区A",
  "entries": [
    {"sample_date": "2026-11-11", "row_count": 90},
    {"sample_date": "2026-11-18", "row_count": null}
  ]
}
```

响应（新增告警附基准日期与对应行数；另含提交后全部有效告警日期）：

```json
{
  "campus": "院区A",
  "received": 7,
  "raised": [
    {
      "sample_date": "2026-11-11",
      "row_count": 90,
      "median": 995,
      "baseline": [
        {"sample_date": "2026-10-07", "row_count": 980},
        {"sample_date": "2026-10-14", "row_count": 1020},
        {"sample_date": "2026-10-28", "row_count": 960},
        {"sample_date": "2026-11-04", "row_count": 1010}
      ]
    }
  ],
  "resolved": [],
  "active_alerts": [{"sample_date": "2026-11-11"}]
}
```

- `raised`：本批**新增**告警（提交前不存在、提交后有效），携带基准日期、
  基准行数与中位数。
- `resolved`：本批**解除**告警（提交前有效、提交后不再成立）。
- 持续有效的告警若基准被改写，只静默刷新基准，首次触发时间 `triggered_at`
  保留，不出现在两个变化列表中。

### `GET /api/v1/daily-counts?campus=...`

查询某院区全部采样日（含 `null` 未收齐日），按日期升序。

### `GET /api/v1/alerts?campus=...`

查询某院区当前全部有效告警，含中位数、基准日期与行数、首次触发时间。

状态码：

| 场景 | 状态码 |
| --- | --- |
| 批次提交成功 | 200，返回新增/解除告警 |
| 负数/布尔/浮点行数、同批日期重复、空 entries、空白院区、非法日期 | 422 |
| 事务执行异常 | 500，整批已回滚，不留部分写入 |

## 事务与并发

- 日量与告警状态在**同一个 SQLite 事务**内提交（`BEGIN IMMEDIATE`）：判定
  读取、日量 upsert、告警增删全部成功才 `COMMIT`，任何异常整体 `ROLLBACK`。
- 进程内全局串行锁保证并发批次逐个进入事务；每批返回的新增/解除以它
  **实际提交前后的状态**为准。SQLite 另启用 WAL 与 `busy_timeout`。
- 部署为单 uvicorn 进程即可满足串行语义；如需多进程，可改用单写者队列或
  基于 `SQLITE_BUSY` 的重试。

## 本地开发（无 Docker 时）

```bash
python3 -m venv --without-pip .venv
.venv/bin/python get-pip.py            # 若 ensurepip 不可用
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
DAILY_ALERT_DB=./data/daily_alert.db \
  .venv/bin/uvicorn app.main:app --reload --port 8000
.venv/bin/python -m pytest tests -q
```

测试覆盖：

- 领域逻辑：中位数分子、半数阈值边界（含 `x.5` 中位数）、只取最近 4 个基准、
  重判范围的边界与 `null` 占位；
- 服务层：完整告警生命周期、`0` 与 `null` 语义、覆盖更正、撤回解除、院区/
  星期隔离、事务回滚无残留、双线程并发不丢更新；
- **随机属性测试**：40 轮随机批次（含补报、更正、撤回）后，增量维护的告警
  与按最终数据从头全量重算的结论逐轮一致；
- API：4 个 Swagger 样例端到端、422 校验、500 回滚、OpenAPI 示例顺序。
