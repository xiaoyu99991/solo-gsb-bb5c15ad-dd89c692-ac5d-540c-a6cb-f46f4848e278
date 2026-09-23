"""实际执行记录：校验、整批原子写入、相对计划的偏差计算。

每个计划任务（场景 nodes 中的一个 id）恰好对应一条记录：
- 已执行：actual_start / actual_end 均必填，且结束不得早于开始；
- 未执行：reason 必填。

保存采用"先全量校验、后整批替换"：任一记录不合法（缺时间、结束早于开始、
任务 ID 无效、有遗漏或重复）都抛出 :class:`ExecutionValidationError`，
数据库不留任何部分数据。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from . import scheduler

EXECUTIONS_DDL = """
CREATE TABLE IF NOT EXISTS execution_records (
    scenario_id  INTEGER NOT NULL,
    node_id      TEXT NOT NULL,
    executed     INTEGER NOT NULL,
    actual_start TEXT,
    actual_end   TEXT,
    reason       TEXT,
    recorded_at  TEXT NOT NULL,
    PRIMARY KEY (scenario_id, node_id),
    FOREIGN KEY (scenario_id) REFERENCES scenarios(id) ON DELETE CASCADE
)
"""

MAX_REASON_LEN = 500


class ExecutionValidationError(ValueError):
    """结构级致命错误（请求体不是记录数组等）。"""

    def __init__(self, issues: list[dict[str, Any]]):
        self.issues = issues
        super().__init__("；".join(i["message"] for i in issues))


# ---------------------------------------------------------------------------
# 编辑器 state → 排程载荷
# ---------------------------------------------------------------------------

def state_to_payload(state: dict[str, Any]) -> dict[str, Any]:
    """把保存的编辑器 state 转成排程器输入（镜像前端 buildPayload）。"""
    return {
        "window_start": state.get("windowStart"),
        "window_end": state.get("windowEnd"),
        "resources": {
            str(r.get("name")): r.get("capacity")
            for r in (state.get("resources") or [])
            if r.get("name") is not None
        },
        "nodes": [
            {
                "id": n.get("id"),
                "name": n.get("name") or n.get("id"),
                "duration_minutes": n.get("duration_minutes"),
                "resources": n.get("resources") or {},
                "fixed_start": n.get("fixed_start"),
            }
            for n in (state.get("nodes") or [])
        ],
        "edges": [
            {"source": e.get("source"), "target": e.get("target")}
            for e in (state.get("edges") or [])
            if e.get("source") and e.get("target")
        ],
        "blackouts": [
            {"name": b.get("name"), "start": b.get("start"), "end": b.get("end")}
            for b in (state.get("blackouts") or [])
            if b.get("start") and b.get("end")
        ],
    }


def _node_index(state: dict[str, Any]) -> list[tuple[str, str]]:
    """按编辑器顺序返回 (node_id, node_name)。"""
    return [
        (str(n.get("id", "")), str(n.get("name") or n.get("id") or ""))
        for n in (state.get("nodes") or [])
    ]


# ---------------------------------------------------------------------------
# 校验 + 规整
# ---------------------------------------------------------------------------

def _parse_actual(value: Any, nid: str, label: str, field: str,
                  issues: list[dict[str, Any]]) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        issues.append({
            "node_id": nid,
            "message": f"「{label}」已标记为执行，但缺少实际{field}时间",
        })
        return None
    raw = text[:-1] if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        issues.append({
            "node_id": nid,
            "message": f"「{label}」的实际{field}时间格式无效：{text}",
        })
        return None
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt


def _iso_minute(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


def validate_records(
    raw_records: Any,
    nodes: list[tuple[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """全量校验提交的记录，返回 (规整后的行, 问题列表)。

    问题列表为空才允许写入。结构级错误（records 非数组、元素非对象）抛
    :class:`ExecutionValidationError`。
    """
    if not isinstance(raw_records, list):
        raise ExecutionValidationError([
            {"node_id": None, "message": "records 必须是数组"}
        ])

    id_to_label = {nid: (name or nid) for nid, name in nodes}
    valid_ids = set(id_to_label)

    issues: list[dict[str, Any]] = []
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()

    for raw in raw_records:
        if not isinstance(raw, dict):
            issues.append({"node_id": None, "message": "每条记录必须是对象"})
            continue
        nid = str(raw.get("node_id", "")).strip()
        label = id_to_label.get(nid, nid or "（空 ID）")
        if not nid:
            issues.append({"node_id": None, "message": "存在缺少任务 ID 的记录"})
            continue
        if nid in seen:
            issues.append({
                "node_id": nid,
                "message": f"任务「{label}」（ID {nid}）提交了多条记录，每个任务恰好一条",
            })
            continue
        seen.add(nid)

        executed = bool(raw.get("executed"))
        if executed:
            start = _parse_actual(raw.get("actual_start"), nid, label, "开始", issues)
            end = _parse_actual(raw.get("actual_end"), nid, label, "结束", issues)
            if start is not None and end is not None and end < start:
                issues.append({
                    "node_id": nid,
                    "message": f"「{label}」的实际结束时间（{_iso_minute(end)}）"
                               f"早于实际开始时间（{_iso_minute(start)}）",
                })
            normalized.append({
                "node_id": nid,
                "executed": 1,
                "actual_start": _iso_minute(start) if start else None,
                "actual_end": _iso_minute(end) if end else None,
                "reason": None,
            })
        else:
            reason = str(raw.get("reason") or "").strip()
            if not reason:
                issues.append({
                    "node_id": nid,
                    "message": f"「{label}」已标记为未执行，必须填写未执行原因",
                })
            normalized.append({
                "node_id": nid,
                "executed": 0,
                "actual_start": None,
                "actual_end": None,
                "reason": reason[:MAX_REASON_LEN] or None,
            })

    for missing in sorted(valid_ids - seen):
        issues.append({
            "node_id": missing,
            "message": f"缺少任务「{id_to_label[missing]}」（ID {missing}）的执行记录，"
                       f"每个任务恰好一条",
        })
    for extra in sorted(seen - valid_ids):
        issues.append({
            "node_id": extra,
            "message": f"记录引用了场景中不存在的任务 ID：{extra}",
        })

    return normalized, issues


# ---------------------------------------------------------------------------
# 存取（整批替换，调用方须在同一事务/连接内）
# ---------------------------------------------------------------------------

def replace_records(conn, scenario_id: int, normalized: list[dict[str, Any]],
                    now: str) -> None:
    """整批替换某场景的全部记录（DELETE + INSERT，同一事务内提交）。"""
    conn.execute(
        "DELETE FROM execution_records WHERE scenario_id = ?", (scenario_id,)
    )
    conn.executemany(
        """
        INSERT INTO execution_records
            (scenario_id, node_id, executed, actual_start, actual_end, reason, recorded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (scenario_id, r["node_id"], r["executed"],
             r["actual_start"], r["actual_end"], r["reason"], now)
            for r in normalized
        ],
    )


def fetch_records(conn, scenario_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT node_id, executed, actual_start, actual_end, reason, recorded_at
        FROM execution_records WHERE scenario_id = ?
        """,
        (scenario_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 偏差报告
# ---------------------------------------------------------------------------

def _deviations(plan: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """相对计划的开始偏差与时长偏差（分钟，正=延后/延长）。"""
    if not record["executed"] or not record["actual_start"]:
        return {"start_deviation_minutes": None, "duration_deviation_minutes": None}
    actual_start = datetime.fromisoformat(record["actual_start"])
    actual_end = datetime.fromisoformat(record["actual_end"])
    planned_start = datetime.fromisoformat(plan["start"])
    start_dev = round((actual_start - planned_start).total_seconds() / 60)
    actual_minutes = (actual_end - actual_start).total_seconds() / 60
    duration_dev = round(actual_minutes - plan["duration_minutes"])
    return {
        "start_deviation_minutes": start_dev,
        "duration_deviation_minutes": duration_dev,
    }


def build_report(scenario_id: int, scenario_name: str, state: dict[str, Any],
                 records: list[dict[str, Any]]) -> dict[str, Any]:
    """组装 GET/PUT 返回体：每个计划任务恰好一项，附偏差与未执行信息。"""
    nodes = _node_index(state)
    valid_ids = {nid for nid, _ in nodes}

    plan_error: str | None = None
    plan_by_id: dict[str, dict[str, Any]] = {}
    feasible = False
    try:
        result = scheduler.solve(state_to_payload(state))
        plan_by_id = {it["id"]: it for it in result["items"]}
        feasible = bool(result.get("feasible"))
    except (ValueError, TypeError, KeyError) as exc:
        plan_error = str(exc)

    record_by_id = {r["node_id"]: r for r in records}

    # 展示顺序：已排程任务按计划开始时间，其余按编辑器中的任务顺序
    ordered_ids = [it["id"] for it in plan_by_id.values()]
    ordered_ids += [nid for nid, _ in nodes if nid not in plan_by_id]

    items: list[dict[str, Any]] = []
    executed_count = skipped_count = 0
    for nid in ordered_ids:
        name = next((nm for i, nm in nodes if i == nid), nid)
        plan = plan_by_id.get(nid)
        record = record_by_id.get(nid)

        record_out = None
        deviations = {"start_deviation_minutes": None, "duration_deviation_minutes": None}
        if record is not None:
            is_exec = bool(record["executed"])
            executed_count += 1 if is_exec else 0
            skipped_count += 0 if is_exec else 1
            record_out = {
                "executed": is_exec,
                "actual_start": record["actual_start"],
                "actual_end": record["actual_end"],
                "reason": record["reason"],
                "recorded_at": record["recorded_at"],
            }
            if is_exec and plan is not None:
                deviations = _deviations(plan, record)

        items.append({
            "node_id": nid,
            "name": name,
            "scheduled": plan is not None,
            "planned_start": plan["start"] if plan else None,
            "planned_end": plan["end"] if plan else None,
            "planned_start_label": plan["start_label"] if plan else None,
            "planned_end_label": plan["end_label"] if plan else None,
            "planned_duration_minutes": plan["duration_minutes"] if plan else None,
            "record": record_out,
            **deviations,
        })

    missing = sorted(valid_ids - set(record_by_id))
    dangling = [
        {
            "node_id": nid,
            "executed": bool(r["executed"]),
            "reason": r["reason"],
        }
        for nid, r in sorted(record_by_id.items())
        if nid not in valid_ids
    ]
    skipped_items = [
        {"node_id": it["node_id"], "name": it["name"],
         "reason": it["record"]["reason"]}
        for it in items
        if it["record"] and not it["record"]["executed"]
    ]
    saved_at = max((r["recorded_at"] for r in records), default=None)

    return {
        "scenario_id": scenario_id,
        "scenario_name": scenario_name,
        "plan_feasible": feasible,
        "plan_error": plan_error,
        "items": items,
        "missing": missing,
        "dangling": dangling,
        "skipped": skipped_items,
        "saved_at": saved_at,
        "summary": {
            "total": len(nodes),
            "recorded": len(valid_ids & set(record_by_id)),
            "executed": executed_count,
            "skipped": skipped_count,
            "missing": len(missing),
        },
    }
