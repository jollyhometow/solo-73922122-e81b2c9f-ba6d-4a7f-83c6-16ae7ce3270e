# 数仓分区回填计划（FastAPI + SQLite）

面向数仓值班工程师的轻量工具：录入任务依赖、分区粒度、跨日偏移与带配置版本的运行记录后，
按指定业务日期为目标表生成分区回填计划 —— 只展开必要的上游分区，按依赖拓扑给出执行波次，
并标出**缺失 / 失败 / 版本过期**的前置；依赖成环或记录与当前配置不一致时**拒绝生成**。
计划落库时固化生成时刻的配置快照，刷新页面、重启服务后仍可查看。

## 快速开始

### Docker Compose（推荐）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

首次启动自动写入示例数据（`SEED_IF_EMPTY=0` 可关闭）；SQLite 落在命名卷 `dw_data` 中，重启不丢。

### 本地运行

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
SEED_IF_EMPTY=1 .venv/bin/python -m uvicorn app.main:app --port 8000
# 或先手动写示例数据：.venv/bin/python -m app.seed
```

### 测试

```bash
.venv/bin/python -m unittest discover tests -v
```

## 使用流程（浏览器）

1. **任务与依赖**：登记目标表（日/小时粒度），配置「下游 ← 上游 + 跨日偏移」。
   修改粒度或增删依赖会使该任务配置版本 +1。
2. **运行记录**：按分区录入 success/failed，并携带运行时配置版本。
3. **生成计划**：选目标任务 + 业务日期，一键生成；被拒绝时会说明原因并留痕。
4. **计划列表**：查看历史计划及其配置快照（含各任务当时版本）。

示例数据下，对 `ads_sales_report`、业务日期 `2026-11-13` 生成计划即可看到
3 个波次及缺失/失败/版本过期各类前置。

## 语义约定

| 概念 | 说明 |
|---|---|
| 分区格式 | 日：`YYYY-MM-DD`；小时：`YYYY-MM-DDTHH` |
| 跨日偏移 | 上游取数日期 = 下游业务日期 − offset 天；小时上游对日下游展开为全天 24 个小时分区，小时对小时取同一小时 |
| 前置状态 | `已就绪`（最新记录 success 且版本==当前）/ `缺失`（无记录）/ `失败`（最新记录 failed）/ `版本过期`（success 但版本<当前） |
| 必要展开 | 已就绪分区视为满足，停止向上展开；其余分区继续展开上游 |
| 执行波次 | 待回填分区按依赖拓扑分层，第 N 波全部完成后才能跑第 N+1 波，波内可并行 |
| 拒绝生成 | 依赖成环；可达任务的记录分区格式与粒度不符；记录版本高于当前配置版本；展开规模超限 |
| 计划快照 | 生成时固化任务/依赖/版本快照与逐项状态，存于 SQLite，重启可查 |

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/state` | 任务/依赖/记录总览 |
| POST/PUT/DELETE | `/api/tasks[/{id}]` | 任务管理（改粒度自动升版本） |
| POST/DELETE | `/api/dependencies[/{id}]` | 依赖管理（下游版本联动 +1） |
| POST/DELETE | `/api/records[/{id}]` | 运行记录（带配置版本） |
| POST | `/api/plans` | 生成计划；拒绝时返回 409 + 原因（同时落库留痕） |
| GET | `/api/plans`, `/api/plans/{id}` | 计划列表 / 详情（含快照） |
| POST | `/api/seed` | 空库时写入示例数据 |

交互式接口文档：`/docs`（Swagger UI）。

## 目录结构

```
app/
  main.py        # FastAPI 路由
  planner.py     # 核心规划器：展开/状态/波次/拒绝（纯函数，可单测）
  db.py          # SQLite 连接与建表
  seed.py        # 示例数据（CLI: python -m app.seed）
  static/index.html  # 单页操作界面
tests/test_planner.py
Dockerfile / docker-compose.yml / requirements.txt
```
