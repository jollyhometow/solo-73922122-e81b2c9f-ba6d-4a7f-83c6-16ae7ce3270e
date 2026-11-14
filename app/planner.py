"""分区回填计划核心逻辑（纯函数，不依赖 Web 框架与数据库）。

语义约定
--------
分区格式:
  - daily  : "YYYY-MM-DD"      例如 2026-11-13
  - hourly : "YYYY-MM-DDTHH"   例如 2026-11-13T08

依赖偏移 offset_days（跨日偏移，单位为天）:
  下游分区对应的业务日期减去 offset_days 天，得到上游分区日期。
  - 上游 daily            -> 取该日的日分区
  - 上游 hourly, 下游 hourly -> 取该日同一小时分区
  - 上游 hourly, 下游 daily  -> 取该日全部 24 个小时分区

前置分区状态:
  - ok      : 最新运行记录为 success 且记录版本 == 任务当前配置版本
  - missing : 没有任何运行记录
  - failed  : 最新运行记录为 failed
  - stale   : 最新记录为 success 但记录版本 < 任务当前配置版本（版本过期）

展开规则（只展开必要的上游）:
  从目标分区出发 BFS；状态为 ok 的分区视为已满足，标记 skip 并停止向上展开；
  其余分区标记 backfill 并继续展开其上游。

拒绝生成:
  - 依赖在可达子图内成环
  - 可达任务的运行记录与当前配置不一致（分区格式与粒度不符 / 记录版本高于当前配置版本）
  - 展开规模超过安全上限
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

DAILY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HOURLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[0-2]\d$")

MAX_EXPAND = 5000  # 展开分区数安全上限

STATUS_LABELS = {
    "ok": "已就绪",
    "missing": "缺失",
    "failed": "失败",
    "stale": "版本过期",
}


def parse_partition(partition: str):
    """解析分区字符串，返回 (kind, date, hour)；非法返回 None。"""
    if not isinstance(partition, str):
        return None
    if DAILY_RE.match(partition):
        try:
            return ("daily", date.fromisoformat(partition), None)
        except ValueError:
            return None
    if HOURLY_RE.match(partition):
        try:
            d = date.fromisoformat(partition[:10])
        except ValueError:
            return None
        hour = int(partition[11:13])
        if hour > 23:
            return None
        return ("hourly", d, hour)
    return None


def partition_matches(granularity: str, partition: str) -> bool:
    p = parse_partition(partition)
    return bool(p) and p[0] == granularity


def shift_partitions(up_granularity: str, down_granularity: str,
                     down_partition: str, offset_days: int) -> list[str]:
    """按跨日偏移计算下游分区所需的上游分区列表。"""
    parsed = parse_partition(down_partition)
    if not parsed:
        raise ValueError(f"非法分区: {down_partition}")
    _, d, hour = parsed
    base = d - timedelta(days=offset_days)
    if up_granularity == "daily":
        return [base.isoformat()]
    if down_granularity == "hourly":
        return [f"{base.isoformat()}T{hour:02d}"]
    return [f"{base.isoformat()}T{h:02d}" for h in range(24)]


def _find_cycle(start_id: int, deps_by_down: dict, reachable: set,
                task_by_id: dict):
    """在可达子图上做 DFS，返回成环的任务名路径（含首尾重复），无环返回 None。"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {t: WHITE for t in reachable}
    color[start_id] = GRAY
    path = [start_id]
    stack = [(start_id, iter(deps_by_down.get(start_id, [])))]
    while stack:
        node, it = stack[-1]
        advanced = False
        for dep in it:
            up = dep["upstream_id"]
            if up not in reachable:
                continue
            if color.get(up) == GRAY:
                idx = path.index(up)
                cyc = path[idx:] + [up]
                return [task_by_id[t]["name"] for t in cyc]
            if color.get(up) == WHITE:
                color[up] = GRAY
                path.append(up)
                stack.append((up, iter(deps_by_down.get(up, []))))
                advanced = True
                break
        if not advanced:
            color[node] = BLACK
            path.pop()
            stack.pop()
    return None


def _reject(reason: str, snapshot: dict) -> dict:
    return {"status": "rejected", "reject_reason": reason,
            "items": [], "snapshot": snapshot, "summary": {}}


def build_plan(tasks: list[dict], deps: list[dict], records: list[dict],
               target_task_id: int, biz_date: str,
               now: str | None = None) -> dict:
    """生成回填计划。输入为字典列表（tasks/deps/records），输出计划字典。"""
    now = now or datetime.now().isoformat(timespec="seconds")
    task_by_id = {t["id"]: t for t in tasks}
    target = task_by_id.get(target_task_id)

    snapshot = {
        "generated_at": now,
        "target_task_id": target_task_id,
        "target_task_name": target["name"] if target else None,
        "biz_date": biz_date,
        "tasks": [],
        "dependencies": [],
    }

    if target is None:
        return _reject(f"目标任务不存在: id={target_task_id}", snapshot)
    parsed_biz = parse_partition(biz_date)
    if not parsed_biz or parsed_biz[0] != "daily":
        return _reject(f"非法业务日期: {biz_date}（应为 YYYY-MM-DD）", snapshot)

    deps_by_down: dict[int, list[dict]] = {}
    for d in deps:
        deps_by_down.setdefault(d["downstream_id"], []).append(d)

    # 目标可达的任务集合
    reachable: set[int] = set()
    stack = [target_task_id]
    while stack:
        tid = stack.pop()
        if tid in reachable:
            continue
        reachable.add(tid)
        for d in deps_by_down.get(tid, []):
            stack.append(d["upstream_id"])

    snapshot["tasks"] = [
        {k: task_by_id[t][k] for k in ("id", "name", "granularity", "config_version")}
        for t in sorted(reachable)
    ]
    snapshot["dependencies"] = [
        {"id": d["id"],
         "downstream": task_by_id[d["downstream_id"]]["name"],
         "upstream": task_by_id[d["upstream_id"]]["name"],
         "offset_days": d["offset_days"]}
        for d in deps if d["downstream_id"] in reachable
    ]

    # 1) 依赖成环 -> 拒绝
    cycle = _find_cycle(target_task_id, deps_by_down, reachable, task_by_id)
    if cycle:
        return _reject("依赖成环，拒绝生成: " + " → ".join(cycle), snapshot)

    # 2) 运行记录与当前配置不一致 -> 拒绝
    problems = []
    for r in records:
        if r["task_id"] not in reachable:
            continue
        t = task_by_id[r["task_id"]]
        if not partition_matches(t["granularity"], r["partition"]):
            problems.append(
                f"记录#{r['id']} 任务[{t['name']}] 分区[{r['partition']}] "
                f"与粒度[{t['granularity']}]不符")
        if r["config_version"] > t["config_version"]:
            problems.append(
                f"记录#{r['id']} 任务[{t['name']}] 分区[{r['partition']}] "
                f"记录版本v{r['config_version']}高于当前配置v{t['config_version']}")
    if problems:
        shown = "；".join(problems[:5])
        more = f" 等{len(problems)}处" if len(problems) > 5 else ""
        return _reject(f"运行记录与当前配置不一致，拒绝生成: {shown}{more}", snapshot)

    # 每个 (任务, 分区) 的最新记录（按完成时间、再按 id）
    latest: dict[tuple[int, str], dict] = {}
    for r in records:
        key = (r["task_id"], r["partition"])
        cur = latest.get(key)
        if cur is None or (r["finished_at"], r["id"]) > (cur["finished_at"], cur["id"]):
            latest[key] = r

    def status_of(task: dict, partition: str) -> str:
        r = latest.get((task["id"], partition))
        if r is None:
            return "missing"
        if r["status"] != "success":
            return "failed"
        if r["config_version"] < task["config_version"]:
            return "stale"
        return "ok"

    # 3) 从目标分区出发，只展开必要的上游
    biz = date.fromisoformat(biz_date)
    if target["granularity"] == "daily":
        target_parts = [biz.isoformat()]
    else:
        target_parts = [f"{biz.isoformat()}T{h:02d}" for h in range(24)]

    items: dict[str, dict] = {}
    edges: dict[str, set[str]] = {}  # down_key -> {up_key}
    queue = [(target_task_id, p) for p in target_parts]
    while queue:
        tid, part = queue.pop()
        task = task_by_id[tid]
        key = f"{task['name']}:{part}"
        if key in items:
            continue
        if len(items) >= MAX_EXPAND:
            return _reject(f"展开分区数超过上限{MAX_EXPAND}，疑似配置错误，拒绝生成",
                           snapshot)
        st = status_of(task, part)
        items[key] = {
            "key": key,
            "task_id": tid,
            "task_name": task["name"],
            "granularity": task["granularity"],
            "partition": part,
            "status": st,
            "action": "skip" if st == "ok" else "backfill",
            "wave": None,
            "depends_on": [],
        }
        edges.setdefault(key, set())
        if st == "ok":
            continue  # 已满足的前置不再向上展开
        for d in deps_by_down.get(tid, []):
            up = task_by_id[d["upstream_id"]]
            for up_part in shift_partitions(up["granularity"], task["granularity"],
                                            part, d["offset_days"]):
                up_key = f"{up['name']}:{up_part}"
                edges[key].add(up_key)
                queue.append((up["id"], up_part))

    # 4) 拓扑波次（仅对待回填节点排序；skip 节点视为已就绪）
    backfill_keys = {k for k, it in items.items() if it["action"] == "backfill"}
    ups_of = {k: sorted(u for u in edges.get(k, ()) if u in backfill_keys)
              for k in backfill_keys}
    downs_of: dict[str, list[str]] = {}
    indeg: dict[str, int] = {}
    for k, ups in ups_of.items():
        indeg[k] = len(ups)
        for u in ups:
            downs_of.setdefault(u, []).append(k)
    wave: dict[str, int] = {}
    ready = [k for k, v in indeg.items() if v == 0]
    for k in ready:
        wave[k] = 1
    queue2 = list(ready)
    while queue2:
        u = queue2.pop(0)
        for dn in downs_of.get(u, []):
            wave[dn] = max(wave.get(dn, 1), wave[u] + 1)
            indeg[dn] -= 1
            if indeg[dn] == 0:
                queue2.append(dn)
    if len(wave) != len(backfill_keys):
        return _reject("分区级依赖成环，拒绝生成", snapshot)
    for k, w in wave.items():
        items[k]["wave"] = w

    for k, it in items.items():
        it["depends_on"] = sorted(edges.get(k, ()))

    ordered = sorted(items.values(),
                     key=lambda x: (x["wave"] or 0, x["task_name"], x["partition"]))
    summary = {
        "total": len(items),
        "backfill": len(backfill_keys),
        "skip": len(items) - len(backfill_keys),
        "waves": max(wave.values(), default=0),
        "by_status": {s: sum(1 for it in items.values() if it["status"] == s)
                      for s in ("ok", "missing", "failed", "stale")},
    }
    return {"status": "generated", "reject_reason": "", "items": ordered,
            "snapshot": snapshot, "summary": summary}
