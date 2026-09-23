"""实际执行记录校验 / 偏差报告 / 原子写入测试。运行：python test_executions.py"""
import sqlite3
import sys

sys.path.insert(0, ".")

from app import executions as ex

W0, W1 = "2026-09-23T22:00", "2026-09-24T06:00"

passed = 0
def check(name, cond, extra=""):
    global passed
    if cond:
        passed += 1
        print(f"  ✔ {name}")
    else:
        raise AssertionError(f"{name}  {extra}")


def make_state():
    return {
        "windowStart": W0, "windowEnd": W1,
        "resources": [{"name": "DBA", "capacity": 1}],
        "nodes": [
            {"id": "a", "name": "A 任务", "duration_minutes": 30,
             "resources": {"DBA": 1}, "fixed_start": None},
            {"id": "b", "name": "B 任务", "duration_minutes": 60,
             "resources": {"DBA": 1}, "fixed_start": None},
            {"id": "c", "name": "C 任务", "duration_minutes": 20,
             "resources": {}, "fixed_start": None},
        ],
        "edges": [{"source": "a", "target": "b"}],
        "blackouts": [],
    }


nodes = ex._node_index(make_state())
ids = {nid for nid, _ in nodes}

print("1. state_to_payload 转换")
p = ex.state_to_payload(make_state())
check("窗口映射", p["window_start"] == W0 and p["window_end"] == W1)
check("资源映射", p["resources"] == {"DBA": 1}, p["resources"])
check("节点数", len(p["nodes"]) == 3)
check("依赖边", p["edges"] == [{"source": "a", "target": "b"}])
check("空边/空禁排期被过滤",
      ex.state_to_payload({"nodes": [], "edges": [{"source": "", "target": ""}],
                           "blackouts": [{"name": "x"}]} )["edges"] == [])

print("2. 合法记录：执行 + 未执行")
raw = [
    {"node_id": "a", "executed": True,
     "actual_start": "2026-09-23T22:10", "actual_end": "2026-09-23T22:50"},
    {"node_id": "b", "executed": False, "reason": "上游故障顺延"},
    {"node_id": "c", "executed": True,
     "actual_start": "2026-09-24T03:00", "actual_end": "2026-09-24T03:20"},
]
norm, issues = ex.validate_records(raw, nodes)
check("无问题", issues == [], issues)
check("执行行规整", norm[0]["executed"] == 1 and norm[0]["reason"] is None
      and norm[0]["actual_start"] == "2026-09-23T22:10")
check("未执行行规整", norm[1]["executed"] == 0 and norm[1]["actual_start"] is None
      and norm[1]["reason"] == "上游故障顺延")

print("3. 缺少任务记录 → 拒绝")
_, issues = ex.validate_records(raw[:2], nodes)
check("恰好一个缺项", len(issues) == 1 and "c" in issues[0]["message"], issues)

print("4. 执行项缺开始/结束时间")
bad = [
    {"node_id": "a", "executed": True, "actual_start": "", "actual_end": "2026-09-23T22:30"},
    {"node_id": "b", "executed": True, "actual_start": "2026-09-23T23:00", "actual_end": ""},
    {"node_id": "c", "executed": False, "reason": "x"},
]
_, issues = ex.validate_records(bad, nodes)
msgs = [i["message"] for i in issues]
check("缺开始", any("缺少实际开始时间" in m for m in msgs), msgs)
check("缺结束", any("缺少实际结束时间" in m for m in msgs), msgs)

print("5. 结束早于开始 / 时间格式无效")
bad = [
    {"node_id": "a", "executed": True,
     "actual_start": "2026-09-23T23:00", "actual_end": "2026-09-23T22:00"},
    {"node_id": "b", "executed": True,
     "actual_start": "not-a-time", "actual_end": "2026-09-23T23:00"},
    {"node_id": "c", "executed": False, "reason": "x"},
]
_, issues = ex.validate_records(bad, nodes)
msgs = [i["message"] for i in issues]
check("结束早于开始", any("早于实际开始时间" in m for m in msgs), msgs)
check("格式无效", any("格式无效" in m for m in msgs), msgs)
check("结束与开始相等允许（零时长实际作业）",
      ex.validate_records([
          {"node_id": "a", "executed": True,
           "actual_start": "2026-09-23T22:00", "actual_end": "2026-09-23T22:00"},
          {"node_id": "b", "executed": False, "reason": "r"},
          {"node_id": "c", "executed": False, "reason": "r"},
      ], nodes)[1] == [])

print("6. 未执行缺原因")
_, issues = ex.validate_records([
    {"node_id": "a", "executed": False, "reason": "  "},
    {"node_id": "b", "executed": False, "reason": "x"},
    {"node_id": "c", "executed": True,
     "actual_start": "2026-09-23T22:00", "actual_end": "2026-09-23T22:20"},
], nodes)
check("报原因必填", len(issues) == 1 and "未执行原因" in issues[0]["message"], issues)

print("7. 无效引用 / 重复记录")
_, issues = ex.validate_records([
    {"node_id": "a", "executed": False, "reason": "r"},
    {"node_id": "b", "executed": False, "reason": "r"},
    {"node_id": "c", "executed": False, "reason": "r"},
    {"node_id": "ghost", "executed": False, "reason": "r"},
], nodes)
check("无效 ID", any("不存在的任务 ID" in i["message"] for i in issues), issues)

_, issues = ex.validate_records([
    {"node_id": "a", "executed": False, "reason": "r1"},
    {"node_id": "a", "executed": False, "reason": "r2"},
], nodes)
check("重复记录 + b/c 缺项", len(issues) == 3
      and any("多条记录" in i["message"] for i in issues), issues)

print("8. 结构级错误抛 ExecutionValidationError")
try:
    ex.validate_records({"a": 1}, nodes)
    raise AssertionError("应抛 ExecutionValidationError")
except ex.ExecutionValidationError as e:
    check("records 非数组", "数组" in str(e))

print("9. 偏差报告：晚开始 + 超时 / 准点 / 提前且提前结束")
state = make_state()
records = [
    {"node_id": "a", "executed": 1, "actual_start": "2026-09-23T22:10",
     "actual_end": "2026-09-23T22:50", "reason": None, "recorded_at": "t1"},  # +10 / +10
    {"node_id": "b", "executed": 1, "actual_start": "2026-09-23T22:30",
     "actual_end": "2026-09-23T23:30", "reason": None, "recorded_at": "t1"},  # 计划 22:30 起 60 分
    {"node_id": "c", "executed": 1, "actual_start": "2026-09-23T22:00",
     "actual_end": "2026-09-23T22:10", "reason": None, "recorded_at": "t1"},  # 计划 22:00，时长 20 → -10
]
rep = ex.build_report(7, "夜窗", state, records)
by = {it["node_id"]: it for it in rep["items"]}
check("a 开始偏差 +10", by["a"]["start_deviation_minutes"] == 10, by["a"])
check("a 时长偏差 +10", by["a"]["duration_deviation_minutes"] == 10)
check("b 准点且时长相符", by["b"]["start_deviation_minutes"] == 0
      and by["b"]["duration_deviation_minutes"] == 0, by["b"])
check("c 时长偏差 -10", by["c"]["duration_deviation_minutes"] == -10)
check("汇总", rep["summary"] == {"total": 3, "recorded": 3, "executed": 3,
                                 "skipped": 0, "missing": 0}, rep["summary"])
check("无未执行项", rep["skipped"] == [])
check("saved_at 取最新", rep["saved_at"] == "t1")

print("10. 未执行任务进 skipped 且无偏差")
records[1] = {"node_id": "b", "executed": 0, "actual_start": None,
              "actual_end": None, "reason": "缺人", "recorded_at": "t2"}
rep = ex.build_report(7, "夜窗", state, records)
by = {it["node_id"]: it for it in rep["items"]}
check("b 偏差为 None", by["b"]["start_deviation_minutes"] is None
      and by["b"]["duration_deviation_minutes"] is None)
check("skipped 清单", rep["skipped"] == [
    {"node_id": "b", "name": "B 任务", "reason": "缺人"}], rep["skipped"])
check("汇总已执行/未执行", rep["summary"]["executed"] == 2
      and rep["summary"]["skipped"] == 1)

print("11. 缺失 / 悬空记录")
rep = ex.build_report(7, "夜窗", state, [
    {"node_id": "a", "executed": 1, "actual_start": "2026-09-23T22:00",
     "actual_end": "2026-09-23T22:30", "reason": None, "recorded_at": "t"},
    {"node_id": "ghost", "executed": 0, "actual_start": None,
     "actual_end": None, "reason": "r", "recorded_at": "t"},
])
check("missing=b,c", rep["missing"] == ["b", "c"], rep["missing"])
check("dangling=ghost", len(rep["dangling"]) == 1
      and rep["dangling"][0]["node_id"] == "ghost")
check("recorded 只计有效 ID", rep["summary"]["recorded"] == 1)

print("12. 计划失效（场景自身排不出）时仍出报告")
bad_state = make_state()
bad_state["windowEnd"] = "2026-09-23T21:00"   # 窗口倒置 → solve 抛错
rep = ex.build_report(7, "坏场景", bad_state, [])
check("plan_error 有值", rep["plan_error"], rep)
check("全部标记未排程", all(not it["scheduled"] for it in rep["items"]))
check("全部 missing", rep["summary"]["missing"] == 3)

print("13. 计划未排程的任务（循环依赖）不产生偏差基线")
cyc = make_state()
cyc["edges"].append({"source": "b", "target": "a"})  # a ↔ b 成环，c 可排
records = [
    {"node_id": "a", "executed": 0, "actual_start": None, "actual_end": None,
     "reason": "取消", "recorded_at": "t"},
    {"node_id": "b", "executed": 1, "actual_start": "2026-09-23T23:00",
     "actual_end": "2026-09-24T00:00", "reason": None, "recorded_at": "t"},
    {"node_id": "c", "executed": 1, "actual_start": "2026-09-23T22:00",
     "actual_end": "2026-09-23T22:20", "reason": None, "recorded_at": "t"},
]
rep = ex.build_report(7, "环", cyc, records)
by = {it["node_id"]: it for it in rep["items"]}
check("a/b 未排程", not by["a"]["scheduled"] and not by["b"]["scheduled"])
check("b 已执行但无基线 → 偏差 None", by["b"]["start_deviation_minutes"] is None)
check("c 正常排程且准点", by["c"]["scheduled"] and by["c"]["start_deviation_minutes"] == 0)

print("14. SQLite 整批替换：拒绝时不留部分数据")
conn = sqlite3.connect(":memory:")
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA foreign_keys = ON")
conn.execute("CREATE TABLE scenarios (id INTEGER PRIMARY KEY, name TEXT)")
conn.execute(ex.EXECUTIONS_DDL)
conn.execute("INSERT INTO scenarios (id, name) VALUES (1, 's1')")

first, _ = ex.validate_records(raw, nodes)
ex.replace_records(conn, 1, first, "now-1")
check("首次写入 3 行", len(ex.fetch_records(conn, 1)) == 3)

# 模拟一次被拒绝的保存：校验有问题 → 不调用 replace_records，旧数据原样保留
_, bad_issues = ex.validate_records(
    [{"node_id": "a", "executed": True, "actual_start": None, "actual_end": None}],
    nodes,
)
check("拒绝前发现问题", bool(bad_issues))
check("旧数据完整保留", len(ex.fetch_records(conn, 1)) == 3)

# 合法的第二次保存整批替换
second, _ = ex.validate_records([
    {"node_id": "a", "executed": False, "reason": "取消"},
    {"node_id": "b", "executed": False, "reason": "取消"},
    {"node_id": "c", "executed": False, "reason": "取消"},
], nodes)
ex.replace_records(conn, 1, second, "now-2")
rows = ex.fetch_records(conn, 1)
check("替换后仍 3 行且全未执行",
      len(rows) == 3 and all(r["executed"] == 0 for r in rows))
check("recorded_at 已更新", all(r["recorded_at"] == "now-2" for r in rows))

print("15. 外键级联删除")
conn.execute("DELETE FROM scenarios WHERE id = 1")
check("场景删除后记录级联清空", ex.fetch_records(conn, 1) == [])
conn.close()

print(f"\n全部 {passed} 项检查通过 ✔")
