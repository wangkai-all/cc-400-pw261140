/* Pointwise identity-resolution console — vanilla JS, no build step. */
const $ = (id) => document.getElementById(id);
const api = async (svc, path, opts = {}) => {
  const resp = await fetch(`/api/${svc}/${path}`, {
    method: opts.method || "GET",
    headers: { "Content-Type": "application/json" },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await resp.json().catch(() => ({ error: "bad response" }));
  if (!resp.ok) throw Object.assign(new Error(data.error || "request failed"), { data });
  return data;
};
const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

let state = { version: 0, entities: [], evidence: [], autoTimer: null };

// ------------------------------------------------------------- tabs
document.querySelectorAll(".tab").forEach((btn) => {
  btn.onclick = () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $(`tab-${btn.dataset.tab}`).classList.add("active");
    refreshAll();
  };
});

async function refreshAll() {
  try {
    const [v, f, jobs] = await Promise.all([
      api("adj", "version"),
      api("rpt", "freshness"),
      api("rb", "jobs"),
    ]);
    state.version = v.version;
    $("adj-version").textContent = v.version;
    $("global-status").innerHTML =
      `裁决版本 <b>${v.version}</b> · 待归属事件 <b>${f.pending_events}</b> · ` +
      `点指标 stale <b>${f.pointwise.stale}</b> · 区间 stale <b>${f.interval.stale}</b> · ` +
      `链路 stale <b>${f.chain.stale}</b>`;
    renderJobs(jobs.jobs);
  } catch (e) {
    $("global-status").textContent = `服务不可用：${e.message}`;
  }
}

// ------------------------------------------------------------- 1 evidence
async function loadEvidence() {
  const status = $("ev-filter").value;
  const data = await api("adj", `evidence${status ? `?status=${status}` : ""}`);
  state.evidence = data.evidence;
  $("ev-body").innerHTML = data.evidence.map((e) => `
    <tr class="ev-${esc(e.evidence_type)}">
      <td>${esc(e.evidence_id)}</td><td>${esc(e.sid_a)}</td><td>${esc(e.sid_b)}</td>
      <td>${esc(e.evidence_type)}</td><td>${e.score}</td>
      <td><code>${esc(JSON.stringify(e.detail))}</code></td>
      <td>${esc(e.status)}</td>
      <td><button onclick='quickMerge(${JSON.stringify([e.sid_a, e.sid_b])})'>合并这两个</button></td>
    </tr>`).join("");
}

function quickMerge(group) {
  document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
  document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
  document.querySelector('[data-tab="entities"]').classList.add("active");
  $("tab-entities").classList.add("active");
  loadEntities().then(() => {
    const checks = document.querySelectorAll(".merge-check");
    checks.forEach((c) => { c.checked = group.includes(c.value); });
    previewImpact();
  });
}

// ------------------------------------------------------------- 2 entities
async function loadEntities() {
  const data = await api("adj", "entities");
  state.version = data.version;
  state.entities = data.entities;
  $("adj-version").textContent = data.version;
  $("entity-grid").innerHTML = data.entities.map((ent) => `
    <div class="card entity ${ent.fresh_count < ent.event_count ? "has-stale" : ""}">
      <h3>${esc(ent.eid)} ${ent.fresh_count < ent.event_count
        ? '<span class="pill warn">含失效指标</span>' : '<span class="pill ok">fresh</span>'}</h3>
      <div class="hint">事件 ${ent.event_count}（fresh ${ent.fresh_count}）</div>
      <ul>${ent.sids.map((s) => `<li>${esc(s.sid)} · ${esc(s.source_system)} · ${esc(s.display_name)}</li>`).join("")}</ul>
    </div>`).join("");

  const allSids = data.entities.flatMap((e) => e.sids.map((s) => ({ ...s, eid: e.eid })));
  $("merge-pickers").innerHTML = allSids.map((s) => `
    <label class="chip"><input type="checkbox" class="merge-check" value="${esc(s.sid)}">
      ${esc(s.sid)} (${esc(s.eid.slice(0, 8))}) ${esc(s.display_name)}</label>`).join("");
  $("split-entity").innerHTML = data.entities.map((e) =>
    `<option value="${esc(e.eid)}">${esc(e.eid)} — ${e.sids.length} 个来源身份</option>`).join("");
  $("split-entity").onchange = renderSplitSids;
  renderSplitSids();
  $("late-pickers").innerHTML = allSids.slice(0, 8).map((s) => `
    <label class="chip"><input type="checkbox" class="late-check" value="${esc(s.sid)}">
      ${esc(s.sid)} ${esc(s.display_name)}</label>`).join("");
}

function renderSplitSids() {
  const eid = $("split-entity").value;
  const ent = state.entities.find((e) => e.eid === eid);
  $("split-sids").innerHTML = (ent ? ent.sids : []).map((s) => `
    <label class="chip"><input type="checkbox" class="split-check" value="${esc(s.sid)}">
      ${esc(s.sid)} · ${esc(s.source_system)}</label>`).join("");
}

function picked(cls) {
  return [...document.querySelectorAll(`.${cls}:checked`)].map((c) => c.value);
}

async function previewImpact() {
  const sids = picked("merge-check");
  if (sids.length < 2) {
    $("impact-box").classList.remove("hidden");
    $("impact-box").innerHTML = "请至少勾选两个来源身份。";
    return;
  }
  const impact = await api("adj", "impact", { method: "POST", body: { sids } });
  $("impact-box").classList.remove("hidden");
  $("impact-box").innerHTML = `
    <h3>影响范围与成本对比（提交前预览）</h3>
    <table><thead><tr><th>策略</th><th>范围</th><th>工作量</th><th>可局部修复？</th><th>说明</th></tr></thead>
    <tbody>
      <tr><td>逐事件归属 pointwise</td><td>${impact.pointwise.scope}</td>
        <td>${impact.pointwise.rows_to_rewrite} 行</td><td>是</td><td>${esc(impact.pointwise.note)}</td></tr>
      <tr><td>区间重建 interval</td><td>${impact.interval.scope}</td>
        <td>${impact.interval.windows_to_recompute}/${impact.interval.windows_total} 个月份窗</td>
        <td><b>否</b></td><td>${esc(impact.interval.note)}</td></tr>
      <tr><td>跨实体链路 chain</td><td>${impact.chain.scope}</td>
        <td>${impact.chain.chains_to_recompute} 条链</td><td><b>否</b></td>
        <td>${esc(impact.chain.note)}</td></tr>
    </tbody></table>`;
}

async function doMerge() {
  const group = picked("merge-check");
  if (group.length < 2) return alert("至少选两个 sid");
  try {
    const r = await api("adj", "merge", {
      method: "POST",
      body: { base_version: state.version, group, rationale: "web 手动合并" },
    });
    alert(`已接受：新版本 v${r.version}，移动 ${r.moved_sids.length} 个身份，已排队 3 个重建任务`);
    $("impact-box").classList.add("hidden");
    await refreshAll();
    await loadEntities();
  } catch (e) {
    alert(`合并被拒绝：${e.message}`);
  }
}

async function doSplit() {
  const eid = $("split-entity").value;
  const sids = picked("split-check");
  if (!sids.length) return alert("勾选要拆出的 sid");
  try {
    const r = await api("adj", "split", {
      method: "POST",
      body: {
        base_version: state.version,
        specs: [{ sids, note: $("split-note").value || "undo merge" }],
      },
    });
    alert(`拆分完成：新版本 v${r.version}。旧去重指标已置为 stale，重建任务已排队——不会只改映射而留错指标。`);
    await refreshAll();
    await loadEntities();
  } catch (e) {
    alert(`拆分被拒绝：${e.message}`);
  }
}

// ------------------------------------------------------------- 3 rebuild
function renderJobs(jobs) {
  $("jobs-body").innerHTML = jobs.map((j) => {
    const pct = j.total ? Math.round((j.processed / j.total) * 100) : 0;
    const running = ["queued", "running", "interrupting"].includes(j.status);
    return `<tr class="job-${esc(j.status)}">
      <td>${esc(j.job_id)}</td><td>${esc(j.mode)}</td><td>${esc(j.status)}</td>
      <td>${esc(j.impact_scope)} <code>${esc(JSON.stringify(j.impact))}</code></td>
      <td><div class="bar"><div style="width:${pct}%"></div></div>
        ${j.processed}/${j.total || "?"} (${pct}%)</td>
      <td><code>${esc(j.cursor_event || "-")}</code></td>
      <td>${running
        ? `<button onclick="interruptJob('${j.job_id}')">中断</button>`
        : j.status === "interrupted"
          ? `<button onclick="resumeJob('${j.job_id}')">恢复</button>`
          : "—"}
        ${j.error ? `<div class="err">${esc(j.error)}</div>` : ""}</td>
    </tr>`;
  }).join("");
}
async function loadJobs() {
  renderJobs((await api("rb", "jobs")).jobs);
}
async function interruptJob(id) {
  await api("rb", `jobs/${id}/interrupt`, { method: "POST" });
  setTimeout(loadJobs, 300);
}
async function resumeJob(id) {
  await api("rb", `jobs/${id}/resume`, { method: "POST" });
  setTimeout(loadJobs, 300);
}
function toggleAuto() {
  if ($("auto-jobs").checked) state.autoTimer = setInterval(loadJobs, 1000);
  else clearInterval(state.autoTimer);
}

// ------------------------------------------------------------- 4 ingest
async function doIngest() {
  const amount = $("in-amount").value ? Number($("in-amount").value) : null;
  const r = await api("rb", "ingest", {
    method: "POST",
    body: { event_type: $("in-type").value, amount, note: "manual injection" },
  });
  $("ingest-result").innerHTML = `已注入 <code>${esc(r.ingested)}</code> — ` +
    `运行中的 pointwise/interval 任务会通过游标扫描在后续批次自动纳入。`;
  refreshAll();
}

// ------------------------------------------------------------- 5 stale
async function doLate() {
  const group = picked("late-check");
  if (group.length < 2) return alert("选两个 sid 模拟旧合并");
  try {
    await api("adj", "late", {
      method: "POST",
      body: { group, delay_versions: Number($("late-delay").value) },
    });
    alert("意外：旧裁决被接受了（不应发生）");
  } catch (e) {
    alert(`已按预期拦截：${e.data?.reason || e.message}`);
    loadStale();
  }
}
async function loadStale() {
  const data = await api("adj", "stale");
  $("stale-body").innerHTML = data.stale_decisions.map((s) => `
    <tr><td>${s.id}</td><td>${s.attempted_version}</td><td>${s.current_version}</td>
    <td>${esc(s.reason)}</td><td>${esc(s.received_at)}</td></tr>`).join("")
    || '<tr><td colspan="5" class="hint">暂无被拦截的旧裁决</td></tr>';
}

// ------------------------------------------------------------- 6 reports
async function loadFreshness() {
  const f = await api("rpt", "freshness");
  $("freshness").innerHTML = `<h3>指标新鲜度（裁决版本 v${f.adjudication_version}）</h3>
    <table><thead><tr><th>存储层</th><th>total</th><th>fresh</th><th>stale</th></tr></thead>
    <tbody>${["pointwise", "interval", "chain"].map((k) => `
      <tr><td>${k}</td><td>${f[k].total}</td><td>${f[k].fresh}</td><td>${f[k].stale}</td></tr>`).join("")}
    </tbody></table><p class="hint">待归属新事件：${f.pending_events}</p>`;
}

async function buildReport() {
  const r = await api("rpt", "reports/entity", { method: "POST", body: {} });
  const rep = r.report;
  $("report-body").innerHTML = `
    <div class="card"><h3>报表 ${esc(r.report_id)} · 构建于裁决版本 v${rep.adjudication_version}</h3>
      <p class="hint">${esc(rep.disclaimer)}</p>
      <h4>逐事件口径（可局部修复）</h4>
      <table><thead><tr><th>实体</th><th>事件数</th><th>金额</th>
      <th>fresh/stale</th><th>裁决版本</th><th>源事件出处（前5）</th></tr></thead><tbody>
      ${rep.entities.map((e) => `<tr><td>${esc(e.eid)}</td><td>${e.event_count}</td>
        <td>${e.total_amount}</td><td>${e.fresh_rows}/${e.stale_rows}</td>
        <td>${e.decided_versions.join(",")}</td>
        <td><code>${e.provenance.source_event_ids.slice(0, 5).join(", ")}</code></td></tr>`).join("")}
      </tbody></table>
      <h4>区间快照口径（不可局部修复；整窗重建）</h4>
      <table><thead><tr><th>窗口</th><th>实体</th><th>计数</th><th>金额</th>
      <th>版本</th><th>状态</th><th>源事件出处</th></tr></thead><tbody>
      ${rep.interval_view.map((s) => `<tr><td>${s.window}</td><td>${esc(s.eid.slice(0, 10))}</td>
        <td>${s.event_count}</td><td>${s.total_amount}</td><td>v${s.built_version}</td>
        <td>${s.is_fresh ? "fresh" : '<b class="err">stale</b>'}</td>
        <td><code>${s.source_event_ids.slice(0, 4).join(", ")}</code></td></tr>`).join("")}
      </tbody></table>
      <h4>跨实体行为链（影响范围最大）</h4>
      <ul>${rep.behavior_chains.map((c) => `<li><code>${esc(c.chain_key)}</code> 触及
        ${c.touches.length} 个实体 · ${c.length} 事件 · v${c.built_version} ·
        ${c.is_fresh ? "fresh" : '<b class="err">stale</b>'}</li>`).join("") || "<li>无</li>"}</ul>
    </div>`;
  loadReports();
}

async function loadReports() {
  const data = await api("rpt", "reports");
  const list = data.reports.map((r) => `<li>${esc(r.report_id)} · ${r.report_type} ·
    v${r.built_version} · ${esc(r.created_at)}</li>`).join("");
  if (list) $("report-body").insertAdjacentHTML("beforeend",
    `<div class="card"><h3>历史报表</h3><ul>${list}</ul></div>`);
}

async function doTrace() {
  const id = $("trace-id").value.trim();
  if (!id) return;
  try {
    const t = await api("rpt", `trace/${encodeURIComponent(id)}`);
    $("trace-out").textContent = JSON.stringify(t, null, 2);
  } catch (e) {
    $("trace-out").textContent = e.message;
  }
}

loadEvidence();
loadEntities();
loadStale();
loadFreshness();
refreshAll();
setInterval(refreshAll, 3000);
