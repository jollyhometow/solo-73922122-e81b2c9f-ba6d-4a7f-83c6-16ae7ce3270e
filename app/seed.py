"""示例数据：一个小型数仓 DAG，覆盖 缺失/失败/版本过期/已就绪 各种前置状态。

DAG（箭头为“依赖于”）：
    ads_sales_report (daily, v3)
      ├── dws_order_daily (daily, v2)  offset 0
      │     ├── ods_orders (daily, v1)        offset 0
      │     └── dwd_order_events (hourly, v2) offset 1
      └── dws_user_daily (daily, v1)   offset 1
            ├── ods_users (daily, v1)         offset 0
            └── ods_orders (daily, v1)        offset 1

对业务日期 2026-11-13 生成计划时可以看到：
  - ods_orders@2026-11-13 成功且版本当前 -> 已就绪，停止向上展开
  - dwd_order_events@2026-11-12 各小时为 v1 成功 -> 版本过期（当前 v2）
  - dwd_order_events@2026-11-12T14 失败、T15 缺失
  - dws_user_daily@2026-11-12 失败 -> 展开 ods_users / ods_orders@2026-11-11
"""
from __future__ import annotations

import sys

from .db import get_conn, init_db

BIZ = "2026-11-13"
PREV = "2026-11-12"

TASKS = [
    # (name, granularity, config_version, description)
    ("ods_orders", "daily", 1, "订单 ODS 层"),
    ("ods_users", "daily", 1, "用户 ODS 层"),
    ("dwd_order_events", "hourly", 2, "订单事件明细（小时）"),
    ("dws_order_daily", "daily", 2, "订单日汇总"),
    ("dws_user_daily", "daily", 1, "用户日汇总"),
    ("ads_sales_report", "daily", 3, "销售报表 ADS"),
]

# (downstream, upstream, offset_days)
DEPS = [
    ("dws_order_daily", "ods_orders", 0),
    ("dws_order_daily", "dwd_order_events", 1),
    ("dws_user_daily", "ods_users", 0),
    ("dws_user_daily", "ods_orders", 1),
    ("ads_sales_report", "dws_order_daily", 0),
    ("ads_sales_report", "dws_user_daily", 1),
]


def _records(task_id: dict[str, int]) -> list[tuple]:
    """(task, partition, status, config_version, finished_at)"""
    recs = [
        ("ods_orders", BIZ, "success", 1, "2026-11-14T01:10:00"),
        ("ods_orders", PREV, "success", 1, "2026-11-13T01:10:00"),
        ("ods_users", BIZ, "success", 1, "2026-11-14T01:20:00"),
        ("ods_users", PREV, "success", 1, "2026-11-13T01:20:00"),
        ("dws_order_daily", PREV, "success", 2, "2026-11-13T03:00:00"),
        ("dws_user_daily", PREV, "failed", 1, "2026-11-13T03:10:00"),
        ("ads_sales_report", PREV, "success", 2, "2026-11-13T04:00:00"),
    ]
    # dwd_order_events 前一日小时分区：多数为 v1 成功（版本过期），
    # T14 失败（v2），T15 缺失（无记录）
    for h in range(24):
        part = f"{PREV}T{h:02d}"
        if h == 14:
            recs.append(("dwd_order_events", part, "failed", 2,
                         "2026-11-13T15:30:00"))
        elif h == 15:
            continue  # 缺失
        else:
            recs.append(("dwd_order_events", part, "success", 1,
                         f"2026-11-13T{h:02d}:40:00"))
    return recs


def seed(conn=None) -> None:
    own = conn is None
    conn = conn or get_conn()
    try:
        ids: dict[str, int] = {}
        for name, gran, ver, desc in TASKS:
            cur = conn.execute(
                "INSERT INTO tasks (name, granularity, config_version, description)"
                " VALUES (?, ?, ?, ?)", (name, gran, ver, desc))
            ids[name] = cur.lastrowid
        for down, up, off in DEPS:
            conn.execute(
                "INSERT INTO dependencies (downstream_id, upstream_id, offset_days)"
                " VALUES (?, ?, ?)", (ids[down], ids[up], off))
        for tname, part, status, ver, ts in _records(ids):
            conn.execute(
                "INSERT INTO run_records"
                " (task_id, partition, status, config_version, finished_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (ids[tname], part, status, ver, ts))
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()


def seed_if_empty() -> bool:
    init_db()
    with get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
        if n:
            return False
        seed(conn)
        return True


def main() -> None:
    force = "--force" in sys.argv
    init_db()
    with get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
        if n and not force:
            print(f"已有 {n} 个任务，跳过（--force 可追加示例数据）")
            return
        seed(conn)
    print("示例数据已写入。目标示例：任务 ads_sales_report，业务日期 " + BIZ)


if __name__ == "__main__":
    main()
