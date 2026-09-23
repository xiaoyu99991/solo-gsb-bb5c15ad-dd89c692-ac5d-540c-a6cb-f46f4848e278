/* 实际执行记录工作台 —— 为已保存场景逐任务登记实际开始/结束或未执行原因。
 * 依赖 app.js 运行后提供的 SchedulerApp（仅在事件触发时调用）。 */
"use strict";

const exec = {
  scenarioId: null,
  report: null,       // 最近一次 GET/PUT 返回的偏差报告
  hasReport: false,
};

const execEl = (sel) => document.querySelector(sel);

function execEsc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function execFmtDT(s) {
  if (!s) return "—";
  const d = new Date(s);
  if (isNaN(d)) return s;
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// 有符号分钟数 → 文案/样式（正=延后·超时，负=提前·少于计划）
function execSigned(minutes, kind) {
  if (minutes === null || minutes === undefined) return { text: "—", cls: "muted" };
  if (minutes === 0) return {
    text: kind === "start" ? "准点 ±0 分" : "时长相符 ±0 分", cls: "zero",
  };
  const abs = Math.abs(minutes);
  if (kind === "start") {
    return minutes > 0
      ? { text: `晚 ${abs} 分`, cls: "late" }
      : { text: `早 ${abs} 分`, cls: "early" };
  }
  return minutes > 0
    ? { text: `超时 ${abs} 分`, cls: "late" }
    : { text: `少于计划 ${abs} 分`, cls: "early" };
}

function execToast(msg, isErr = false) {
  if (window.SchedulerApp?.toast) window.SchedulerApp.toast(msg, isErr);
}

// ---------------------------------------------------------------------------
// 打开 / 关闭
// ---------------------------------------------------------------------------
execEl("#btnExecutions").addEventListener("click", openExecModal);
execEl("#execBtnClose").addEventListener("click", () => {
  execEl("#execModal").classList.add("hidden");
});

async function openExecModal() {
  const id = execEl("#scenarioSelect").value;
  if (!id) {
    return execToast("请先在顶栏选择（或保存）一个已保存场景，再登记执行记录", true);
  }
  exec.scenarioId = id;
  execEl("#execModal").classList.remove("hidden");
  execEl("#execIssueBox").classList.add("hidden");
  execEl("#execReportBar").classList.add("hidden");
  execEl("#execSkippedBox").classList.add("hidden");
  execEl("#execSummary").textContent = "加载中…";
  execEl("#execTable tbody").innerHTML = "";

  let report;
  try {
    report = await fetchReport(id);
  } catch (e) {
    execEl("#execSummary").textContent = "";
    return execToast("加载执行记录失败：" + e.message, true);
  }
  exec.report = report;
  exec.hasReport = false;   // GET 回来仅用于预填，偏差区在首次保存后高亮
  execEl("#execScenarioName").textContent = `— ${report.scenario_name}（#${id}）`;
  renderExecSummary(report);
  renderExecTable(report);
  renderSkipped(report);
}

async function fetchReport(id) {
  const resp = await fetch(`/api/scenarios/${id}/executions`);
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.detail || `HTTP ${resp.status}`);
  }
  return resp.json();
}

// ---------------------------------------------------------------------------
// 渲染
// ---------------------------------------------------------------------------
function renderExecSummary(r) {
  const s = r.summary;
  const plan = r.plan_error
    ? `<span class="exec-bad">计划已失效（${execEsc(r.plan_error)}）</span>`
    : r.plan_feasible
      ? `<span class="exec-ok">计划可行</span>`
      : `<span class="exec-warn">计划含冲突</span>`;
  execEl("#execSummary").innerHTML =
    `${plan} ｜ 共 ${s.total} 个任务 ｜ 已登记 ${s.recorded} ｜ ` +
    `已执行 ${s.executed} ｜ 未执行 ${s.skipped}` +
    (s.missing ? ` ｜ <span class="exec-bad">待补登记 ${s.missing}</span>` : "") +
    (r.saved_at ? ` ｜ 最近保存 ${execEsc(r.saved_at.replace("T", " ").slice(0, 16))}` : "");
}

function renderExecTable(r) {
  const tb = execEl("#execTable tbody");
  tb.innerHTML = r.items.map((it) => {
    const rec = it.record;
    const executed = rec ? rec.executed : true;   // 默认按"已执行"登记
    const planned = it.scheduled
      ? `<div class="exec-plan">${execFmtDT(it.planned_start)}</div>
         <div class="exec-plan">${execFmtDT(it.planned_end)}</div>
         <div class="hint">${it.planned_duration_minutes} 分</div>`
      : `<span class="exec-bad">✕ 计划未排程</span>`;
    return `
    <tr class="exec-row ${executed ? "" : "is-skipped"}" data-node="${execEsc(it.node_id)}"
        data-scheduled="${it.scheduled ? 1 : 0}"
        data-plan-start="${execEsc(it.planned_start || "")}"
        data-plan-end="${execEsc(it.planned_end || "")}">
      <td>
        <b>${execEsc(it.name)}</b>
        <div class="hint">ID ${execEsc(it.node_id)}</div>
      </td>
      <td>${planned}</td>
      <td>
        <select class="exec-status">
          <option value="executed" ${executed ? "selected" : ""}>已执行</option>
          <option value="skipped" ${executed ? "" : "selected"}>未执行</option>
        </select>
      </td>
      <td class="exec-times"><input type="datetime-local" step="300" class="exec-start"
        value="${execEsc(rec?.executed ? rec.actual_start : "")}" /></td>
      <td class="exec-times"><input type="datetime-local" step="300" class="exec-end"
        value="${execEsc(rec?.executed ? rec.actual_end : "")}" /></td>
      <td class="exec-detail">
        <input type="text" class="exec-reason-input" maxlength="500"
          placeholder="未执行原因（必填）" value="${execEsc(rec && !rec.executed ? rec.reason : "")}" />
        <div class="exec-dev">${execDeviationHtml(it)}</div>
      </td>
    </tr>`;
  }).join("");
}

function execDeviationHtml(it) {
  if (!exec.hasReport) {
    return it.scheduled
      ? `<span class="hint">保存后显示相对计划的开始/时长偏差</span>`
      : `<span class="hint">该任务计划未排程</span>`;
  }
  const rec = it.record;
  if (!rec) return `<span class="exec-bad">缺少记录</span>`;
  if (!rec.executed) {
    return `<span class="exec-bad">🚫 未执行：${execEsc(rec.reason || "")}</span>`;
  }
  if (!it.scheduled) {
    return `<span class="exec-warn">实际已执行，但计划中未排程（无偏差基线）</span>`;
  }
  const s = execSigned(it.start_deviation_minutes, "start");
  const d = execSigned(it.duration_deviation_minutes, "duration");
  return `<span class="exec-dev-item ${s.cls}">开始偏差：${execEsc(s.text)}</span>
          <span class="exec-dev-item ${d.cls}">时长偏差：${execEsc(d.text)}</span>`;
}

function renderSkipped(r) {
  const box = execEl("#execSkippedBox");
  if (!exec.hasReport || !r.skipped.length) {
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");
  execEl("#execSkippedList").innerHTML = r.skipped
    .map((x) => `<li><b>${execEsc(x.name)}</b>（ID ${execEsc(x.node_id)}）：${execEsc(x.reason)}</li>`)
    .join("");
}

function renderReport(r) {
  exec.hasReport = true;
  renderExecSummary(r);
  renderExecTable(r);
  renderSkipped(r);

  const bar = execEl("#execReportBar");
  const s = r.summary;
  if (s.executed === s.total) {
    bar.className = "exec-report-bar ok";
    bar.textContent = `✓ 全部 ${s.total} 个任务均已执行并登记`;
  } else {
    bar.className = "exec-report-bar warn";
    bar.textContent = `已执行 ${s.executed}/${s.total}，未执行 ${s.skipped} 项（见下方清单）`;
  }
  bar.classList.remove("hidden");
}

// ---------------------------------------------------------------------------
// 行内交互：状态切换、按计划填充
// ---------------------------------------------------------------------------
execEl("#execTable tbody").addEventListener("change", (ev) => {
  if (!ev.target.classList.contains("exec-status")) return;
  ev.target.closest(".exec-row").classList.toggle(
    "is-skipped", ev.target.value === "skipped");
});

execEl("#execBtnFillPlanned").addEventListener("click", () => {
  let filled = 0;
  execEl("#execTable tbody").querySelectorAll(".exec-row").forEach((tr) => {
    if (tr.classList.contains("is-skipped")) return;
    const sIn = tr.querySelector(".exec-start"), eIn = tr.querySelector(".exec-end");
    if (!sIn.value && tr.dataset.planStart) { sIn.value = tr.dataset.planStart; filled++; }
    if (!eIn.value && tr.dataset.planEnd) { eIn.value = tr.dataset.planEnd; filled++; }
  });
  execToast(filled ? `已用计划时间填充 ${filled} 个空项` : "没有需要填充的空项");
});

// ---------------------------------------------------------------------------
// 保存（整批，服务端任一不合法即拒绝）
// ---------------------------------------------------------------------------
execEl("#execBtnSave").addEventListener("click", saveExecutions);

function collectRecords() {
  const records = [];
  execEl("#execTable tbody").querySelectorAll(".exec-row").forEach((tr) => {
    const skipped = tr.classList.contains("is-skipped");
    records.push({
      node_id: tr.dataset.node,
      executed: !skipped,
      actual_start: skipped ? null : (tr.querySelector(".exec-start").value || null),
      actual_end: skipped ? null : (tr.querySelector(".exec-end").value || null),
      reason: skipped ? tr.querySelector(".exec-reason-input").value.trim() : null,
    });
  });
  return records;
}

async function saveExecutions() {
  if (!exec.scenarioId) return;
  const box = execEl("#execIssueBox");
  box.classList.add("hidden");

  const resp = await fetch(`/api/scenarios/${exec.scenarioId}/executions`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ records: collectRecords() }),
  });

  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    const detail = err.detail;
    const issues = detail && typeof detail === "object" && Array.isArray(detail.issues)
      ? detail.issues
      : [{ message: typeof detail === "string" ? detail : `HTTP ${resp.status}` }];
    execEl("#execTable tbody").querySelectorAll(".exec-row.row-error")
      .forEach((tr) => tr.classList.remove("row-error"));
    box.innerHTML =
      `<div><b>整次保存已拒绝，未写入任何数据（${issues.length} 个问题）：</b></div>` +
      issues.map((i) => {
        const tr = i.node_id
          ? document.querySelector(`.exec-row[data-node="${CSS.escape(i.node_id)}"]`)
          : null;
        if (tr) tr.classList.add("row-error");
        return `<div class="tpl-issue-item">
          <span class="tpl-issue-tag error">${execEsc(i.node_id || "全局")}</span>
          <span>${execEsc(i.message)}</span></div>`;
      }).join("");
    box.classList.remove("hidden");
    execToast("存在不合法记录，整次保存已拒绝", true);
    return;
  }

  const report = await resp.json();
  exec.report = report;
  renderReport(report);
  execToast(`执行记录已保存：已执行 ${report.summary.executed}，未执行 ${report.summary.skipped}`);
}
