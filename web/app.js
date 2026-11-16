/* 分析页前端逻辑(无框架,直接操作 DOM) */
"use strict";

const state = { meta: null, results: null, runMeta: null, viewLabel: "" };

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (x) => (x === null || x === undefined) ? "—" : (x * 100).toFixed(1) + "%";

async function api(path, opts) {
  const res = await fetch(path, opts ? {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(opts),
  } : undefined);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

/* ---------------------------------------------------------------- 初始化 */

async function init() {
  state.meta = await api("/api/meta");
  const ruleSel = $("cfg-rule");
  for (const r of state.meta.rules) {
    const opt = document.createElement("option");
    opt.value = r.version;
    opt.textContent = `${r.label}(生效 ${r.effective_from})`;
    opt.title = r.desc;
    ruleSel.appendChild(opt);
  }
  ruleSel.value = state.meta.defaults.rule_version;
  $("cfg-decision").value = state.meta.defaults.decision_date;
  $("cfg-threshold").value = state.meta.defaults.threshold;
  $("cfg-threshold-out").textContent = Number(state.meta.defaults.threshold).toFixed(2);
  $("cfg-threshold").addEventListener("input", (e) => {
    $("cfg-threshold-out").textContent = Number(e.target.value).toFixed(2);
  });
  $("btn-run").addEventListener("click", runBacktest);
  $("btn-save-report").addEventListener("click", saveReport);
  $("cust-close").addEventListener("click", () => { $("customer-panel").hidden = true; });
  $("fp-noint").addEventListener("change", renderFpTable);
  await Promise.all([loadRuns(), loadReports()]);
}

function gatherConfig() {
  const methods = [];
  if ($("m-naive").checked) methods.push("naive_all");
  if ($("m-noint").checked) methods.push("no_intervention");
  if ($("m-holdout").checked) methods.push("time_holdout");
  if ($("m-strat").checked) methods.push("stratified");
  return {
    rule_version: $("cfg-rule").value,
    threshold: parseFloat($("cfg-threshold").value),
    decision_date: $("cfg-decision").value,
    horizon_days: parseInt($("cfg-horizon").value, 10),
    grace_days: parseInt($("cfg-grace").value, 10),
    methods,
  };
}

/* ---------------------------------------------------------------- 回测 */

async function runBacktest() {
  $("run-status").textContent = "计算中…";
  try {
    const res = await api("/api/backtest", gatherConfig());
    state.results = res.results;
    state.runMeta = res.meta;
    state.viewLabel = `运行 #${res.meta.run_id}(谱系 L${res.meta.lineage_id} · v${res.meta.version})`;
    renderResults();
    await loadRuns();
  } catch (e) {
    $("run-status").textContent = "失败:" + e.message;
    return;
  }
  $("run-status").textContent = "";
}

function renderResults() {
  const R = state.results;
  if (!R) return;
  $("results").hidden = false;
  $("result-meta").textContent = state.viewLabel;
  const p = R.period, cfg = R.config;
  $("period-line").textContent =
    `规则 ${cfg.rule_version} · 阈值 ${cfg.threshold} · 决策日 ${p.decision_date} · ` +
    `观察窗至 ${p.window_end} · 证据截止 ${p.evidence_cutoff} · 队列 ${R.cohort_size} 户`;

  const c = R.counts;
  const badges = [
    [`队列 ${R.cohort_size}`, ""],
    [`窗内被干预 ${c.intervened_total}`, c.intervened_total ? "warn" : ""],
    [`预警且被干预 ${c.intervened_alerted}`, c.intervened_alerted ? "warn" : ""],
    [`结果迟到删失 ${c.censored_late_outcome}`, c.censored_late_outcome ? "warn" : ""],
    [`状态迟到排除 ${c.late_state}`, c.late_state ? "warn" : ""],
    [`决策后身份更正 ${c.identity_corrections_after_D}`, ""],
    [`队列内未更正重复账号 ${c.dup_in_cohort}`, c.dup_in_cohort ? "warn" : ""],
  ];
  $("counts-line").innerHTML = badges
    .map(([t, cls]) => `<span class="badge ${cls}">${esc(t)}</span>`).join("");

  renderChart(R);
  renderMetricsTables(R);
  renderStratified(R);
  $("bias-notes").innerHTML = R.bias_notes.map((n) => `<li>${esc(n)}</li>`).join("");
  renderFpTable();
}

/* ---------------------------------------------------------------- 图表 */

const CHART_COLORS = { precision: "#2f6fb2", recall: "#b2622f", f1: "#3d7a4e" };
const CHART_NAMES = { precision: "精确率", recall: "召回率", f1: "F1" };

function barChart(groups) {
  const keys = ["precision", "recall", "f1"];
  const W = 680, H = 280, padL = 46, padR = 12, padT = 30, padB = 52;
  const pw = W - padL - padR, ph = H - padT - padB;
  const gw = pw / groups.length;
  const bw = Math.min(26, (gw * 0.72) / keys.length);
  let svg = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" ` +
    `aria-label="各方法精确率、召回率、F1 对比柱状图" font-size="11">`;
  for (let i = 0; i <= 4; i++) {
    const v = i / 4, y = padT + ph * (1 - v);
    svg += `<line x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}" stroke="#e2e6eb"/>` +
      `<text x="${padL - 6}" y="${y + 4}" text-anchor="end" fill="#66707d">${(v * 100).toFixed(0)}%</text>`;
  }
  groups.forEach((g, gi) => {
    const gx = padL + gi * gw;
    const innerW = bw * keys.length + 8 * (keys.length - 1);
    const x0 = gx + (gw - innerW) / 2;
    keys.forEach((k, ki) => {
      const v = g.values[k] ?? 0;
      const h = ph * v, x = x0 + ki * (bw + 8), y = padT + ph - h;
      svg += `<rect x="${x}" y="${y}" width="${bw}" height="${h}" fill="${CHART_COLORS[k]}">` +
        `<title>${esc(g.label)} ${CHART_NAMES[k]}:${pct(v)}</title></rect>` +
        `<text x="${x + bw / 2}" y="${y - 4}" text-anchor="middle" fill="#1f2733">${(v * 100).toFixed(0)}</text>`;
    });
    svg += `<text x="${gx + gw / 2}" y="${H - padB + 16}" text-anchor="middle" fill="#1f2733">${esc(g.label)}</text>`;
  });
  let lx = padL + 8;
  for (const k of keys) {
    svg += `<rect x="${lx}" y="8" width="10" height="10" fill="${CHART_COLORS[k]}"/>` +
      `<text x="${lx + 14}" y="17" fill="#1f2733">${CHART_NAMES[k]}</text>`;
    lx += 74;
  }
  return svg + "</svg>";
}

function renderChart(R) {
  const m = R.methods, groups = [];
  if (m.naive_all) groups.push({ label: "朴素全样本", values: m.naive_all });
  if (m.no_intervention) groups.push({ label: "未干预样本", values: m.no_intervention });
  if (m.time_holdout) {
    groups.push({ label: "校准期·未干预", values: m.time_holdout.calibration.no_intervention });
    groups.push({ label: "留出期·未干预", values: m.time_holdout.holdout.no_intervention });
  }
  $("chart-wrap").innerHTML = groups.length
    ? `<p class="chart-title">方法对比(精确率 / 召回率 / F1)</p>` + barChart(groups)
    : "";
}

/* ---------------------------------------------------------------- 指标表 */

function metricRow(name, m) {
  return `<tr><td>${esc(name)}</td><td>${m.n}</td><td>${m.alerts}</td>` +
    `<td>${m.tp}</td><td>${m.fp}</td><td>${m.fn}</td><td>${m.tn}</td>` +
    `<td class="num-good">${pct(m.precision)}</td><td class="num-good">${pct(m.recall)}</td>` +
    `<td>${pct(m.f1)}</td><td>${pct(m.base_rate)}</td><td>${m.lift ?? "—"}</td></tr>`;
}

function renderMetricsTables(R) {
  const m = R.methods;
  let html = "";
  if (m.naive_all || m.no_intervention) {
    html += `<h3 class="sub">主口径指标</h3><table><thead><tr>` +
      `<th>口径</th><th>样本</th><th>预警</th><th>TP</th><th>FP</th><th>FN</th><th>TN</th>` +
      `<th>精确率</th><th>召回率</th><th>F1</th><th>基准流失率</th><th>提升度</th></tr></thead><tbody>`;
    if (m.naive_all) html += metricRow("朴素全样本(含被干预者)", m.naive_all);
    if (m.no_intervention) html += metricRow("未干预样本(近似预测质量)", m.no_intervention);
    html += "</tbody></table>";
  }
  if (m.time_holdout) {
    const t = m.time_holdout;
    const row = (label, x) => `<tr><td>${esc(label)}</td><td>${esc(x.period.decision_date)}</td>` +
      `<td>${x.cohort_size}</td><td>${pct(x.no_intervention.precision)}</td>` +
      `<td>${pct(x.no_intervention.recall)}</td><td>${pct(x.no_intervention.f1)}</td>` +
      `<td>${pct(x.naive_all.precision)}</td><td>${pct(x.naive_all.recall)}</td></tr>`;
    html += `<h3 class="sub">时间留出(同一规则与阈值,各自决策日可见数据)</h3>` +
      `<table><thead><tr><th>期间</th><th>决策日</th><th>样本</th>` +
      `<th>精确率·未干预</th><th>召回率·未干预</th><th>F1·未干预</th>` +
      `<th>精确率·朴素</th><th>召回率·朴素</th></tr></thead><tbody>` +
      row("校准期", t.calibration) + row("留出期", t.holdout) + "</tbody></table>";
  }
  $("metrics-tables").innerHTML = html;
}

function renderStratified(R) {
  const s = R.methods.stratified;
  if (!s) { $("strat-panel").hidden = true; return; }
  $("strat-panel").hidden = false;
  let html = `<table><thead><tr><th>风险分层</th><th>人数</th><th>干预</th><th>未干预</th>` +
    `<th>干预组流失率</th><th>未干预组流失率</th><th>差值</th></tr></thead><tbody>`;
  for (const b of s.bands) {
    html += `<tr><td>[${b.range[0]}, ${b.range[1]})</td><td>${b.n}</td>` +
      `<td>${b.n_intervened}</td><td>${b.n_not}</td>` +
      `<td>${pct(b.churn_intervened)}</td><td>${pct(b.churn_not)}</td>` +
      `<td class="${b.diff !== null && b.diff < 0 ? "num-good" : ""}">${pct(b.diff)}</td></tr>`;
  }
  html += `</tbody></table><p class="muted">总体粗差值:${pct(s.raw_diff)} · ` +
    `按分层人数加权的标准化差值:${pct(s.adj_diff)}。` +
    `差值为负表示干预组流失率更低(描述性)。干预分配非随机,残余混杂无法识别,不能解释为因果效果。</p>`;
  $("strat-table").innerHTML = html;
}

/* ---------------------------------------------------------------- 误报 */

function renderFpTable() {
  const R = state.results;
  if (!R) return;
  const onlyNoInt = $("fp-noint").checked;
  const fps = R.rows.filter((r) => r.alert && r.y === 0 && (!onlyNoInt || !r.intervened));
  fps.sort((a, b) => b.score - a.score);
  let html = `<p class="muted">误报 ${fps.length} 条(预警但未在窗内记录流失` +
    `${onlyNoInt ? ",仅未干预样本" : ""})。其中窗内被干预 ${fps.filter((r) => r.intervened).length} 条——` +
    `他们的"未流失"可能正是干预的结果,不能据此判原预警错误。</p>`;
  html += `<table><thead><tr><th>客户</th><th>分数</th><th>窗内被干预</th>` +
    `<th>近30天登录</th><th>90天工单</th><th>严重工单</th><th>账单问题</th><th>沉寂天数</th><th>在网天数</th>` +
    `</tr></thead><tbody>`;
  for (const r of fps.slice(0, 200)) {
    const f = r.features || {};
    html += `<tr class="clickable" data-cid="${esc(r.cid)}"><td>${esc(r.cid)}</td>` +
      `<td>${r.score.toFixed(3)}</td>` +
      `<td>${r.intervened ? '<span class="tag">是</span>' : "否"}</td>` +
      `<td>${f.logins_30 ?? "—"}</td><td>${f.tickets_90 ?? "—"}</td><td>${f.high_sev_90 ?? "—"}</td>` +
      `<td>${f.billing_180 ?? "—"}</td><td>${f.days_idle ?? "—"}</td><td>${f.tenure_days ?? "—"}</td></tr>`;
  }
  html += "</tbody></table>";
  $("fp-table").innerHTML = html;
  for (const tr of $("fp-table").querySelectorAll("tr.clickable")) {
    tr.addEventListener("click", () => openCustomer(tr.dataset.cid));
  }
}

/* ---------------------------------------------------------------- 客户证据 */

async function openCustomer(cid) {
  const cfg = state.results.config;
  const data = await api(`/api/customer?id=${encodeURIComponent(cid)}` +
    `&decision_date=${cfg.decision_date}&rule_version=${cfg.rule_version}`);
  const c = data.customer;
  $("cust-title").textContent = `${c.customer_id} ${c.name}`;
  const f = data.features_at_decision;
  let html = `<dl class="kv">` +
    `<dt>分群 / 区域</dt><dd>${esc(c.segment)} / ${esc(c.region)}</dd>` +
    `<dt>签约日</dt><dd>${esc(c.signup_date)}</dd>` +
    `<dt>决策日得分(${esc(data.rule_version)})</dt><dd><strong>${data.score_at_decision}</strong></dd>` +
    `<dt>决策日特征</dt><dd>近30天登录 ${f.logins_30},前30天 ${f.logins_prev30};` +
    `90天工单 ${f.tickets_90}(严重 ${f.high_sev_90});180天账单问题 ${f.billing_180};` +
    `沉寂 ${f.days_idle} 天;在网 ${f.tenure_days} 天</dd></dl>`;

  if (data.identity_corrections.length) {
    html += `<h3 class="sub">身份更正</h3><table><thead><tr><th>重复账号</th><th>主档</th><th>更正生效日</th></tr></thead><tbody>` +
      data.identity_corrections.map((x) =>
        `<tr><td>${esc(x.duplicate_id)}</td><td>${esc(x.primary_id)}</td><td>${esc(x.corrected_at)}</td></tr>`).join("") +
      "</tbody></table>";
  }
  if (data.interventions.length) {
    html += `<h3 class="sub">挽留干预</h3><table><thead><tr><th>动作日</th><th>类型</th><th>录入日</th><th>来源决策</th></tr></thead><tbody>` +
      data.interventions.map((x) =>
        `<tr><td>${esc(x.action_time)}</td><td>${esc(x.action_type)}</td>` +
        `<td>${esc(x.recorded_at)}</td><td>${esc(x.decision_date)}</td></tr>`).join("") +
      "</tbody></table>";
  }
  if (data.churn.length) {
    html += `<h3 class="sub">流失记录</h3><table><thead><tr><th>流失日</th><th>录入日</th><th>延迟</th></tr></thead><tbody>` +
      data.churn.map((x) => {
        const lag = (new Date(x.recorded_at) - new Date(x.churn_date)) / 86400000;
        return `<tr><td>${esc(x.churn_date)}</td><td>${esc(x.recorded_at)}</td><td>${lag} 天</td></tr>`;
      }).join("") + "</tbody></table>";
  } else {
    html += `<p class="muted">无流失记录(截至今日)。</p>`;
  }
  if (data.alerts_history.length) {
    html += `<h3 class="sub">历史预警</h3><table><thead><tr><th>触发日</th><th>规则</th><th>分数</th><th>阈值</th></tr></thead><tbody>` +
      data.alerts_history.map((x) =>
        `<tr><td>${esc(x.fired_at)}</td><td>${esc(x.rule_version)}</td>` +
        `<td>${x.score}</td><td>${x.threshold}</td></tr>`).join("") + "</tbody></table>";
  }

  html += `<h3 class="sub">活动与工单时间线(决策日 ${esc(data.decision_date)})</h3>` +
    `<table><thead><tr><th>发生日</th><th>类型</th><th>明细</th><th>入库日</th><th>来源</th><th>决策时可见</th></tr></thead><tbody>`;
  const evs = data.events.slice(-80);
  for (const e of evs) {
    html += `<tr><td>${esc(e.event_time)}</td><td>${esc(e.kind)}</td><td>${esc(e.detail)}</td>` +
      `<td>${esc(e.ingested_at)}</td><td>${esc(e.source)}</td>` +
      `<td class="${e.visible_at_decision ? "vis-yes" : "vis-no"}">${e.visible_at_decision ? "✓" : "✗"}</td></tr>`;
  }
  html += "</tbody></table>";
  $("cust-body").innerHTML = html;
  $("customer-panel").hidden = false;
}

/* ---------------------------------------------------------------- 报告 */

async function saveReport() {
  if (!state.runMeta) return;
  const name = $("report-name").value.trim() || `回测 #${state.runMeta.run_id}`;
  const notes = $("report-notes").value.trim();
  try {
    const res = await api("/api/reports", { name, run_id: state.runMeta.run_id, notes });
    $("report-status").textContent = `已保存(报告 #${res.report_id})`;
    await loadReports();
  } catch (e) {
    $("report-status").textContent = "保存失败:" + e.message;
  }
}

async function loadReports() {
  const { reports } = await api("/api/reports");
  $("reports-list").innerHTML = reports.length ? "" : '<p class="muted">暂无报告。</p>';
  for (const r of reports) {
    const div = document.createElement("div");
    div.className = "list-item";
    div.innerHTML = `<div><strong>${esc(r.name)}</strong>` +
      `<div class="meta">#${r.report_id} · ${esc(r.created_at)} · 来自运行 #${r.run_id}` +
      `${r.notes ? " · " + esc(r.notes) : ""}</div></div>`;
    const btn = document.createElement("button");
    btn.className = "small";
    btn.textContent = "查看快照";
    btn.addEventListener("click", async () => {
      const rep = await api(`/api/report?id=${r.report_id}`);
      state.results = rep.snapshot.results;
      state.runMeta = { run_id: rep.snapshot.run_id, version: rep.snapshot.version,
                        lineage_id: rep.snapshot.lineage_id };
      state.viewLabel = `报告「${rep.name}」快照(运行 #${rep.snapshot.run_id} v${rep.snapshot.version},不可变)`;
      renderResults();
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
    div.appendChild(btn);
    $("reports-list").appendChild(div);
  }
}

/* ---------------------------------------------------------------- 运行历史 */

async function loadRuns() {
  const { runs } = await api("/api/runs");
  $("runs-list").innerHTML = runs.length ? "" : '<p class="muted">暂无运行。</p>';
  for (const r of runs) {
    const div = document.createElement("div");
    div.className = "list-item";
    div.innerHTML = `<div><strong>#${r.run_id} · L${r.lineage_id} v${r.version}</strong> ` +
      `<span class="meta">${esc(r.summary)} · ${esc(r.created_at)}` +
      `${r.supersedes ? ` · 重算自 #${r.supersedes}` : ""}</span></div>`;
    const ops = document.createElement("div");
    ops.className = "ops";
    const view = document.createElement("button");
    view.className = "small"; view.textContent = "恢复查看";
    view.addEventListener("click", async () => {
      const run = await api(`/api/run?id=${r.run_id}`);
      state.results = run.results;
      state.runMeta = { run_id: run.run_id, version: run.version, lineage_id: run.lineage_id };
      state.viewLabel = `运行 #${run.run_id}(谱系 L${run.lineage_id} · v${run.version},历史版本恢复)`;
      renderResults();
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
    const redo = document.createElement("button");
    redo.className = "small"; redo.textContent = "按原配置重算";
    redo.addEventListener("click", async () => {
      const res = await api("/api/recompute", { run_id: r.run_id });
      state.results = res.results;
      state.runMeta = res.meta;
      state.viewLabel = `运行 #${res.meta.run_id}(谱系 L${res.meta.lineage_id} · v${res.meta.version},重算)`;
      renderResults();
      await loadRuns();
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
    ops.appendChild(view); ops.appendChild(redo);
    div.appendChild(ops);
    $("runs-list").appendChild(div);
  }
}

init().catch((e) => { $("run-status").textContent = "初始化失败:" + e.message; });
