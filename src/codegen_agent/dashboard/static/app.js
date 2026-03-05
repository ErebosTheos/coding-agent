/* Autonomous Agent Dashboard */
'use strict';

// ── State ─────────────────────────────────────────────────────────────────────
const S = {
  projects:        {},
  selectedProject: null,
  ws:              null,
  liveCount:       0,
  // Per-project post-build state
  qaFiles:         {},   // pid → [{file, issues, clean, lines}, ...]
  qaFinal:         {},   // pid → {score, approved, issues, suggestions}
  healStatus:      {},   // pid → 'running' | 'done' | 'failed'
  form: {
    tab:      'text',    // 'text' | 'upload'
    file:     null,
    priority: 'normal',
    mode:     'all',
    parallel: false,
  },
};

// ── Init ──────────────────────────────────────────────────────────────────────
async function init() {
  setupGlobalDropZone();
  setupParallelToggle();
  await loadConfig();
  await refreshAll();
  connectWS();
  setInterval(refreshAll, 10_000);
}

async function loadConfig() {
  const cfg = await api('/api/config');
  if (cfg) {
    document.getElementById('topMode').textContent = cfg.client_mode || 'mock';
  }
}

// ── WebSocket ─────────────────────────────────────────────────────────────────
function connectWS() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  S.ws = ws;
  ws.onopen    = () => setConn(true);
  ws.onmessage = e => { try { onEvent(JSON.parse(e.data)); } catch {} };
  ws.onclose   = () => { setConn(false); setTimeout(connectWS, 3000); };
  ws.onerror   = () => ws.close();
}

function setConn(ok) {
  const dot  = document.getElementById('connDot');
  const text = document.getElementById('connText');
  dot.style.background = ok ? 'var(--green)' : 'var(--red)';
  dot.style.boxShadow  = ok ? '0 0 6px var(--green)' : '0 0 6px var(--red)';
  text.textContent = ok ? 'Connected' : 'Reconnecting...';
}

function onEvent(ev) {
  pushLive(ev);
  if (ev.type === 'heartbeat') return;
  const pid = ev.project_id;

  // Handle post-build parallel events without full project refresh
  if (pid && ev.type === 'build_approved') {
    S.qaFiles[pid] = [];
    S.healStatus[pid] = 'running';
    S.qaFinal[pid] = null;
    if (S.selectedProject === pid) _updatePostBuildPanel(pid);
  } else if (pid && ev.type === 'heal_started') {
    S.healStatus[pid] = 'running';
    if (S.selectedProject === pid) _patchHealBadge(pid);
  } else if (pid && ev.type === 'heal_complete') {
    S.healStatus[pid] = (ev.data?.success === false) ? 'failed' : 'done';
    if (S.selectedProject === pid) _patchHealBadge(pid);
  } else if (pid && ev.type === 'qa_file_reviewed') {
    if (!S.qaFiles[pid]) S.qaFiles[pid] = [];
    S.qaFiles[pid].push(ev.data);
    if (S.selectedProject === pid) _appendQAFileRow(pid, ev.data);
  } else if (pid && ev.type === 'qa_result') {
    S.qaFinal[pid] = ev.data;
    if (S.selectedProject === pid) _patchQAFinal(pid, ev.data);
  } else {
    if (pid) refreshProject(pid);
  }
  refreshGlobalStats();
}

// ── API ───────────────────────────────────────────────────────────────────────
async function api(path, opts) {
  try {
    const r = await fetch(path, opts);
    if (!r.ok) { const e = await r.json().catch(() => ({})); throw new Error(e.error || `HTTP ${r.status}`); }
    return r.json();
  } catch (e) {
    console.warn('[api]', path, e.message);
    return null;
  }
}

async function refreshAll() {
  const [projects, stats] = await Promise.all([
    api('/api/projects'),
    api('/api/stats/global'),
  ]);
  if (projects) {
    projects.forEach(p => { S.projects[p.id] = p; });
    renderSidebar(projects);
    // Aggregate hero stats
    let files = 0, bugs = 0;
    projects.forEach(p => { files += p.stats?.files_created || 0; bugs += p.stats?.files_fixed || 0; });
    set('heroProjects', projects.length);
    set('heroFiles',    fmtN(files));
    set('heroBugs',     fmtN(bugs));
  }
  if (stats) {
    set('topWorkers', stats.active_workers || 0);
    set('topCalls',   fmtN(stats.calls));
    set('topCost',    stats.cost_usd.toFixed(4));
    set('heroCalls2', fmtN(stats.calls));
    set('heroCost',   stats.cost_usd.toFixed(2));
  }
  if (S.selectedProject) await refreshProject(S.selectedProject);
}

async function refreshGlobalStats() {
  const s = await api('/api/stats/global');
  if (!s) return;
  set('topWorkers', s.active_workers || 0);
  set('topCalls',   fmtN(s.calls));
  set('topCost',    s.cost_usd.toFixed(4));
  set('heroCost',   s.cost_usd.toFixed(2));
}

async function refreshProject(pid) {
  const data = await api(`/api/projects/${pid}`);
  if (!data) return;
  S.projects[pid] = data;
  // Update sidebar badge
  const el = document.querySelector(`.project-item[data-id="${pid}"]`);
  if (el) {
    el.querySelector('.state-badge').className = `state-badge state-${data.state}`;
    el.querySelector('.state-badge').textContent = data.state;
    const wk = el.querySelector('.workers-badge');
    if (wk) wk.textContent = data.active_workers > 0 ? `⚡${data.active_workers}` : '';
  }
  if (S.selectedProject === pid) renderProjectDetail(data);
}

// ── Sidebar ───────────────────────────────────────────────────────────────────
function renderSidebar(projects) {
  const list = document.getElementById('projectList');
  if (!projects.length) {
    list.innerHTML = '<div class="no-projects">No projects yet.<br>Click <b>New</b> to get started.</div>';
    return;
  }
  const sorted = [...projects].sort((a, b) => (b.stats?.updated_at || 0) - (a.stats?.updated_at || 0));
  list.innerHTML = sorted.map(p => `
    <div class="project-item${S.selectedProject === p.id ? ' active' : ''}" data-id="${p.id}" onclick="selectProject('${p.id}')">
      <div class="project-name">${esc(p.name || p.id)}</div>
      <div class="project-meta">
        <span class="state-badge state-${p.state}">${p.state}</span>
        <span class="text-sm text-muted monospace">${p.stats?.files_created || 0}f</span>
        <span class="text-sm workers-badge" style="color:var(--cyan)">${p.active_workers > 0 ? `⚡${p.active_workers}` : ''}</span>
      </div>
    </div>
  `).join('');
}

function selectProject(pid) {
  S.selectedProject = pid;
  document.querySelectorAll('.project-item').forEach(el =>
    el.classList.toggle('active', el.dataset.id === pid)
  );
  document.getElementById('homeView').style.display    = 'none';
  document.getElementById('projectView').style.display = 'block';
  const data = S.projects[pid];
  if (data) renderProjectDetail(data);
  else refreshProject(pid);
}

// ── Project detail ────────────────────────────────────────────────────────────
function renderProjectDetail(proj) {
  const view  = document.getElementById('projectView');
  const plan  = proj.build_plan || {};
  const stack = plan.tech_stack || {};
  const tasks = proj.tasks || {};
  const stats = proj.stats || {};

  const taskList   = Object.entries(tasks);
  const doneCount  = taskList.filter(([,t]) => t.status === 'done').length;
  const runCount   = taskList.filter(([,t]) => t.status === 'running').length;
  const failCount  = taskList.filter(([,t]) => t.status === 'failed').length;
  const total      = Math.max(plan.total_tasks || taskList.length, 1);
  const pct        = Math.min(100, Math.round(doneCount / total * 100));

  const gitLog  = (proj.git_log || []).slice(0, 8);
  const actLog  = (proj.activity_log || []).slice(-25).reverse();
  const brief   = proj.brief || {};

  view.innerHTML = `
    <!-- Header -->
    <div class="card">
      <div class="card-header">
        <div>
          <div style="font-size:18px;font-weight:700">${esc(brief.name || proj.id)}</div>
          <div class="text-sm text-muted" style="margin-top:3px;max-width:540px">${esc((brief.description||'').slice(0,200))}</div>
        </div>
        <div class="flex gap-8 items-center">
          <span class="state-badge state-${proj.state}" style="font-size:11px;padding:3px 10px">${proj.state}</span>
          ${proj.active_workers > 0 ? `<span class="text-sm cyan">⚡ ${proj.active_workers}</span>` : ''}
          ${(proj.state === 'FAILED' || proj.state === 'DONE') ? `<button onclick="retryProject('${proj.id}')" style="font-size:11px;padding:3px 10px;cursor:pointer;background:#1a1a2e;border:1px solid #444;color:#ccc;border-radius:4px">🔄 Retry Fix</button>` : ''}
        </div>
      </div>
      <div class="flex gap-8 items-center" style="margin-bottom:5px">
        <span class="text-sm text-muted">Build progress</span>
        <span class="text-sm monospace purple">${pct}%</span>
        <span class="text-sm text-muted">${doneCount}/${total} tasks</span>
        ${runCount  > 0 ? `<span class="text-sm cyan">${runCount} running</span>` : ''}
        ${failCount > 0 ? `<span class="text-sm red">${failCount} failed</span>` : ''}
      </div>
      <div class="progress-wrap"><div class="progress-bar" style="width:${pct}%"></div></div>
      ${brief.features?.length ? `
        <div style="margin-top:14px">
          <div class="section-label">Features</div>
          <div style="display:flex;flex-wrap:wrap;gap:6px">
            ${brief.features.slice(0,8).map(f => `<span class="stack-chip">${esc(f)}</span>`).join('')}
            ${brief.features.length > 8 ? `<span class="text-sm text-muted" style="padding:3px 8px">+${brief.features.length-8} more</span>` : ''}
          </div>
        </div>
      ` : ''}
    </div>

    <!-- Stats -->
    <div class="stats-grid">
      <div class="stat-card"><div class="stat-label">Files Created</div><div class="stat-value cyan">${fmtN(stats.files_created||0)}</div></div>
      <div class="stat-card"><div class="stat-label">Bugs Fixed</div><div class="stat-value green">${fmtN(stats.files_fixed||0)}</div></div>
      <div class="stat-card"><div class="stat-label">Git Commits</div><div class="stat-value yellow">${fmtN(stats.git_commits||0)}</div></div>
      <div class="stat-card"><div class="stat-label">API Calls</div><div class="stat-value purple">${fmtN(stats.api_calls||0)}</div></div>
      <div class="stat-card"><div class="stat-label">Cost</div><div class="stat-value yellow">$${(stats.cost_usd||0).toFixed(4)}</div></div>
      <div class="stat-card"><div class="stat-label">Workers</div><div class="stat-value blue">${proj.active_workers||0}</div></div>
    </div>

    ${Object.keys(stack).length ? `
    <!-- Stack -->
    <div class="card">
      <div class="card-header"><div class="card-title">Tech Stack</div></div>
      <div class="stack-grid">
        ${stack.language ? `<div class="stack-chip"><strong>Lang</strong> ${esc(stack.language)}</div>` : ''}
        ${stack.backend  ? `<div class="stack-chip"><strong>Backend</strong> ${esc(stack.backend)}</div>` : ''}
        ${stack.frontend ? `<div class="stack-chip"><strong>Frontend</strong> ${esc(stack.frontend)}</div>` : ''}
        ${stack.database ? `<div class="stack-chip"><strong>DB</strong> ${esc(stack.database)}</div>` : ''}
        ${(stack.other||[]).map(o => `<div class="stack-chip">${esc(o)}</div>`).join('')}
      </div>
      ${stack.reasoning ? `<div class="text-sm text-muted" style="margin-top:10px">${esc(stack.reasoning)}</div>` : ''}
    </div>
    ` : ''}

    <!-- Tasks -->
    <div class="card">
      <div class="card-header">
        <div class="card-title">Tasks</div>
        <div class="text-sm text-muted">${doneCount} / ${total} done</div>
      </div>
      <div class="task-grid">
        ${taskList.slice(0,40).map(([tid,t]) => `
          <div class="task-row">
            <span style="font-size:13px;flex-shrink:0">${taskIcon(t.status)}</span>
            <div class="task-info">
              <div class="task-title">[${esc(tid)}] ${esc(t.title||tid)}</div>
              <div class="task-files">${(t.files||[]).map(esc).join(' · ')}</div>
            </div>
            <span class="task-complexity complexity-${t.complexity||'medium'}">${(t.complexity||'med').slice(0,3)}</span>
          </div>
        `).join('')}
        ${taskList.length > 40 ? `<div class="text-sm text-muted" style="padding:8px 4px">… +${taskList.length-40} more tasks</div>` : ''}
      </div>
    </div>

    ${gitLog.length ? `
    <!-- Git log -->
    <div class="card">
      <div class="card-header"><div class="card-title">Git Log</div></div>
      <div class="git-log">
        ${gitLog.map(c => `
          <div class="git-entry">
            <span class="git-sha">${esc(c.sha)}</span>
            <span class="git-msg">${esc(c.message)}</span>
            <span class="git-date">${fmtDate(c.date)}</span>
          </div>
        `).join('')}
      </div>
    </div>
    ` : ''}

    ${_renderPostBuildPanel(proj.id, proj.state)}

    <!-- Activity -->
    <div class="card">
      <div class="card-header"><div class="card-title">Activity</div></div>
      <div class="activity-log">
        ${actLog.map(e => `
          <div class="activity-entry">
            <span class="activity-time">${fmtTime(e.t)}</span>
            <span>${esc(e.msg)}</span>
          </div>
        `).join('') || '<div class="text-muted">No activity yet.</div>'}
      </div>
    </div>
  `;
}

// ── Post-build parallel panel ─────────────────────────────────────────────────

const _POST_BUILD_STATES = new Set(['BUILD_APPROVED','QA_RUNNING','DONE','FIXING','DOCUMENTING']);

function _renderPostBuildPanel(pid, state) {
  if (!_POST_BUILD_STATES.has(state)) return '';
  const healSt   = S.healStatus[pid] || 'running';
  const qaFinal  = S.qaFinal[pid];
  const qaFiles  = S.qaFiles[pid] || [];
  const score    = qaFinal?.score ?? null;
  const approved = qaFinal?.approved ?? false;
  const scoreCls = score === null ? '' : (score >= 80 ? 'good' : score >= 60 ? 'ok' : 'bad');

  const healBadgeCls = healSt === 'done' ? 'done' : healSt === 'failed' ? 'failed' : 'running';
  const healIcon     = healSt === 'done' ? '✓ Heal done' : healSt === 'failed' ? '✗ Heal failed' : '<span class="spinner"></span> Healing';
  const qaBadgeCls   = qaFinal ? 'done' : 'running';
  const qaIcon       = qaFinal ? `✓ QA done (${score?.toFixed(0)}/100)` : '<span class="spinner"></span> QA scanning';

  return `
    <div class="build-approved-banner" id="postBuildBanner-${pid}">
      <div class="build-approved-icon">✅</div>
      <div>
        <div class="build-approved-title">Build Approved</div>
        <div class="build-approved-sub">Docs, Heal, and QA are running in parallel</div>
      </div>
      <div class="parallel-status">
        <div class="parallel-badge ${healBadgeCls}" id="healBadge-${pid}">${healIcon}</div>
        <div class="parallel-badge ${qaBadgeCls}"   id="qaBadge-${pid}">${qaIcon}</div>
      </div>
    </div>

    <div class="qa-panel" id="qaPanel-${pid}">
      <div class="qa-panel-header">
        <div class="qa-panel-title">
          <span>🔍 Live QA Review</span>
          <span class="text-sm text-muted">${qaFiles.length} file${qaFiles.length!==1?'s':''} reviewed</span>
        </div>
        ${score !== null ? `<div class="qa-score-big ${scoreCls}">${score.toFixed(0)}<span style="font-size:14px;color:var(--text-muted)">/100</span>${approved?'<span class="text-sm green" style="margin-left:8px">✓ approved</span>':''}</div>` : '<div class="text-muted text-sm">Scanning...</div>'}
      </div>
      <div class="qa-files-list" id="qaFilesList-${pid}">
        ${qaFiles.map(f => _qaFileRowHTML(f)).join('')}
      </div>
      ${qaFinal?.issues?.length ? `
        <div class="qa-issues-list" style="margin-top:14px">
          <div class="section-label" style="margin-bottom:8px">Issues Found</div>
          ${qaFinal.issues.map(i => `<div class="qa-issue-item"><span>⚠</span><span>${esc(i)}</span></div>`).join('')}
        </div>
      ` : ''}
      ${qaFinal?.suggestions?.length ? `
        <div style="margin-top:10px">
          <div class="section-label" style="margin-bottom:8px">Suggestions</div>
          ${qaFinal.suggestions.map(s => `<div class="qa-issue-item" style="color:var(--cyan)"><span>→</span><span>${esc(s)}</span></div>`).join('')}
        </div>
      ` : ''}
    </div>
  `;
}

function _qaFileRowHTML(f) {
  const icon = f.clean ? '✓' : '⚠';
  const cls  = f.clean ? 'green' : 'yellow';
  return `
    <div class="qa-file-row">
      <div class="qa-file-icon ${cls}">${icon}</div>
      <div class="qa-file-info">
        <div class="qa-file-path">${esc(f.file)}</div>
        ${f.issues?.length ? `<div class="qa-file-issues">${f.issues.map(i=>`<span class="qa-issue-tag">${esc(i)}</span>`).join('')}</div>` : ''}
      </div>
      <div class="qa-file-lines">${f.lines || 0}L</div>
    </div>
  `;
}

function _appendQAFileRow(pid, fileData) {
  const list = document.getElementById(`qaFilesList-${pid}`);
  if (!list) return;
  const div = document.createElement('div');
  div.innerHTML = _qaFileRowHTML(fileData);
  list.appendChild(div.firstElementChild);
  list.scrollTop = list.scrollHeight;
  // Update file count label
  const panel = document.getElementById(`qaPanel-${pid}`);
  const countEl = panel?.querySelector('.qa-panel-title .text-muted');
  if (countEl) {
    const n = (S.qaFiles[pid] || []).length;
    countEl.textContent = `${n} file${n!==1?'s':''} reviewed`;
  }
}

function _patchHealBadge(pid) {
  const el = document.getElementById(`healBadge-${pid}`);
  if (!el) return;
  const st = S.healStatus[pid] || 'running';
  el.className = `parallel-badge ${st === 'done' ? 'done' : st === 'failed' ? 'failed' : 'running'}`;
  el.innerHTML = st === 'done' ? '✓ Heal done' : st === 'failed' ? '✗ Heal failed' : '<span class="spinner"></span> Healing';
}

function _patchQAFinal(pid, qa) {
  const badge = document.getElementById(`qaBadge-${pid}`);
  if (badge) {
    badge.className = 'parallel-badge done';
    badge.innerHTML = `✓ QA done (${qa.score?.toFixed(0)}/100)`;
  }
  // Re-render qa panel header score
  const panel = document.getElementById(`qaPanel-${pid}`);
  if (!panel) return;
  const score    = qa.score ?? 0;
  const approved = qa.approved ?? false;
  const scoreCls = score >= 80 ? 'good' : score >= 60 ? 'ok' : 'bad';
  const headerRight = panel.querySelector('.qa-panel-header > :last-child');
  if (headerRight) {
    headerRight.outerHTML = `<div class="qa-score-big ${scoreCls}">${score.toFixed(0)}<span style="font-size:14px;color:var(--text-muted)">/100</span>${approved?'<span class="text-sm green" style="margin-left:8px">✓ approved</span>':''}</div>`;
  }
  // Append issues + suggestions
  if (qa.issues?.length) {
    const issuesDiv = document.createElement('div');
    issuesDiv.className = 'qa-issues-list';
    issuesDiv.style.marginTop = '14px';
    issuesDiv.innerHTML = `<div class="section-label" style="margin-bottom:8px">Issues Found</div>`
      + qa.issues.map(i=>`<div class="qa-issue-item"><span>⚠</span><span>${esc(i)}</span></div>`).join('');
    panel.appendChild(issuesDiv);
  }
  if (qa.suggestions?.length) {
    const sugDiv = document.createElement('div');
    sugDiv.style.marginTop = '10px';
    sugDiv.innerHTML = `<div class="section-label" style="margin-bottom:8px">Suggestions</div>`
      + qa.suggestions.map(s=>`<div class="qa-issue-item" style="color:var(--cyan)"><span>→</span><span>${esc(s)}</span></div>`).join('');
    panel.appendChild(sugDiv);
  }
}

function _updatePostBuildPanel(pid) {
  // Full re-render of project detail when build_approved fires
  const data = S.projects[pid];
  if (data) renderProjectDetail(data);
}

// ── Live feed ─────────────────────────────────────────────────────────────────
function pushLive(ev) {
  if (ev.type === 'heartbeat') return;
  S.liveCount++;
  const el = document.getElementById('liveCount');
  if (el) el.textContent = `${S.liveCount} events`;

  const feed = document.getElementById('globalLiveFeed');
  if (!feed) return;

  const line = document.createElement('div');
  line.className = 'stream-line';
  const pid = ev.project_id ? `[${ev.project_id.slice(0,12)}] ` : '';
  line.innerHTML = `<span class="ts">${fmtTime(ev.t)}</span>${esc(pid)}${fmtEvMsg(ev)}`;
  feed.appendChild(line);
  while (feed.children.length > 500) feed.removeChild(feed.firstChild);
  feed.scrollTop = feed.scrollHeight;
}

function fmtEvMsg(ev) {
  const d = ev.data || {};
  switch (ev.type) {
    case 'project_started':   return `<span class="purple">▶ Project started</span>`;
    case 'project_enqueued':  return `<span class="purple">⬡ Queued: ${esc(d.name||d.id)}</span>`;
    case 'state_change':      return `<span class="cyan">→ ${esc(d.state)}</span>`;
    case 'task_start':        return `<span class="blue">⚙ ${esc(d.title||d.task_id)}</span>`;
    case 'task_done':         return `<span class="green">✓ ${esc(d.task_id)} (${(d.files||[]).length}f)</span>`;
    case 'bug_found':         return `<span class="yellow">⚠ bug in ${esc(d.file)}</span>`;
    case 'bug_fixed':         return `<span class="green">✔ fixed ${esc(d.file)}${d.escalated?' (complex tier)':''}</span>`;
    case 'git_commit':        return `<span class="yellow">⬡ ${esc(d.sha)} ${esc((d.message||'').slice(0,50))}</span>`;
    case 'build_approved':    return `<span class="green">✅ BUILD APPROVED — ${d.files||0} files — Heal + QA launching</span>`;
    case 'heal_started':      return `<span class="yellow"><span class="spinner"></span> Healer started</span>`;
    case 'heal_complete':     return d.success===false ? `<span class="red">✗ Heal failed: ${esc((d.error||'').slice(0,60))}</span>` : `<span class="green">✓ Heal complete</span>`;
    case 'qa_file_reviewed':  return `<span class="${d.clean?'green':'yellow'}">${d.clean?'✓':'⚠'} QA: ${esc(d.file)} (${d.lines||0}L)${d.issues?.length?` — ${d.issues.length} issue(s)`:''}</span>`;
    case 'qa_result':         return `<span class="${d.approved||d.score>=75?'green':'yellow'}">🔍 QA final: ${d.score?.toFixed(0)||0}/100 ${d.approved?'✓ approved':'⚠ not approved'}</span>`;
    case 'build_complete':    return `<span class="green">🏁 Done — qa=${d.qa_score?.toFixed(0)||0}/100 ${d.api_calls||0} calls $${(d.cost_usd||0).toFixed(4)}</span>`;
    case 'docs_done':         return `<span class="cyan">📄 Docs done</span>`;
    case 'project_failed':    return `<span class="red">✗ failed: ${esc((d.error||'').slice(0,60))}</span>`;
    case 'inbox_file':        return `<span class="purple">📂 ${esc(d.file)} ${d.source==='ui'?'(via UI)':''}</span>`;
    case 'terminal_line':     return `<span class="text-muted monospace">${esc(d.line||'')}</span>`;
    default:                  return `<span class="text-muted">${esc(ev.type)}</span>`;
  }
}

// ── Global drop zone ──────────────────────────────────────────────────────────
function setupGlobalDropZone() {
  const zone = document.getElementById('globalDropzone');
  zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('dragover'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
  zone.addEventListener('drop', e => {
    e.preventDefault();
    zone.classList.remove('dragover');
    const file = e.dataTransfer.files[0];
    if (file) { openModal(); setModalFile(file); }
  });
}

// ── Modal ─────────────────────────────────────────────────────────────────────
function openModal(preFile) {
  clearSubmitStatus();
  document.getElementById('modalOverlay').classList.add('open');
  if (preFile) setModalFile(preFile);
  setTimeout(() => document.getElementById('briefText')?.focus(), 200);
}

function closeModal() {
  document.getElementById('modalOverlay').classList.remove('open');
}

function closeModalIfBg(e) {
  if (e.target === document.getElementById('modalOverlay')) closeModal();
}

function switchTab(tab) {
  S.form.tab = tab;
  document.getElementById('tabUpload').classList.toggle('active', tab === 'upload');
  document.getElementById('tabText').classList.toggle('active', tab === 'text');
  document.getElementById('panelUpload').style.display = tab === 'upload' ? '' : 'none';
  document.getElementById('panelText').style.display   = tab === 'text'   ? '' : 'none';
}

function handleModalDragOver(e) {
  e.preventDefault();
  document.getElementById('modalDropzone').classList.add('dragover');
}
function handleModalDragLeave() {
  document.getElementById('modalDropzone').classList.remove('dragover');
}
function handleModalDrop(e) {
  e.preventDefault();
  document.getElementById('modalDropzone').classList.remove('dragover');
  const file = e.dataTransfer.files[0];
  if (file) setModalFile(file);
}
function handleFileSelect(e) {
  const file = e.target.files[0];
  if (file) setModalFile(file);
}
function setModalFile(file) {
  S.form.file = file;
  document.getElementById('fileChosen').style.display    = '';
  document.getElementById('fileChosenName').textContent  = file.name;
  document.getElementById('modalDropzone').style.display = 'none';
}
function clearFile() {
  S.form.file = null;
  document.getElementById('fileChosen').style.display    = 'none';
  document.getElementById('modalDropzone').style.display = '';
  document.getElementById('modalFileInput').value        = '';
}

function selectRadio(group, val) {
  S.form[group] = val;
  document.querySelectorAll(`#${group}Row .radio-btn`).forEach(el =>
    el.classList.toggle('selected', el.dataset.val === val)
  );
}

function setupParallelToggle() {
  document.getElementById('parallelToggle').addEventListener('change', e => {
    S.form.parallel = e.target.checked;
    document.getElementById('parallelLabel').textContent = e.target.checked
      ? 'Parallel (runs alongside other active projects)'
      : 'Sequential (waits for active projects)';
  });
}

function showSubmitStatus(msg, type) {
  const el = document.getElementById('submitStatus');
  el.style.display = '';
  el.className = `submit-status ${type}`;
  el.innerHTML = type === 'loading'
    ? `<span class="spinner"></span> ${esc(msg)}`
    : `<span>${type === 'success' ? '✓' : '✗'}</span> ${esc(msg)}`;
}
function clearSubmitStatus() {
  const el = document.getElementById('submitStatus');
  if (el) el.style.display = 'none';
}

async function submitProject() {
  const btn  = document.getElementById('submitBtn');
  const text = document.getElementById('briefText')?.value?.trim();
  clearSubmitStatus();

  // Accept either a file OR typed description (file takes priority)
  if (!S.form.file && !text) {
    showSubmitStatus('Describe your project or upload a brief document.', 'error');
    return;
  }

  btn.disabled = true;
  showSubmitStatus('Submitting…', 'loading');

  try {
    const fd = new FormData();
    if (S.form.file) {
      fd.append('file', S.form.file);
    } else {
      fd.append('text', text);
    }
    fd.append('parallel', S.form.parallel ? 'true' : 'false');
    fd.append('priority', S.form.priority);
    fd.append('mode',     S.form.mode);

    const r    = await fetch('/api/submit', { method: 'POST', body: fd });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);

    showSubmitStatus(`Queued! The agent is picking it up now.`, 'success');
    // Reset form
    document.getElementById('briefText').value = '';
    clearFile();
    setTimeout(() => { closeModal(); refreshAll(); }, 1400);

  } catch (err) {
    showSubmitStatus(err.message || 'Submission failed.', 'error');
  } finally {
    btn.disabled = false;
  }
}

// ── Formatters ────────────────────────────────────────────────────────────────
function esc(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function fmtN(n) {
  if (n >= 1_000_000) return (n/1_000_000).toFixed(1)+'M';
  if (n >= 1_000)     return (n/1_000).toFixed(1)+'K';
  return String(n);
}
function fmtTime(t) {
  if (!t) return '';
  return new Date(t*1000).toLocaleTimeString('en-US',{hour12:false});
}
function fmtDate(d) {
  if (!d) return '';
  try { return new Date(d).toLocaleDateString('en-US',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}); }
  catch { return d; }
}
function taskIcon(s) {
  return s==='done'    ? '<span class="green">✓</span>'
       : s==='running' ? '<span class="cyan">⟳</span>'
       : s==='failed'  ? '<span class="red">✗</span>'
       :                 '<span class="text-muted">○</span>';
}
function set(id, v) {
  const el = document.getElementById(id);
  if (el) el.textContent = v;
}

async function retryProject(id) {
  try {
    const r = await fetch(`/api/projects/${id}/retry`, {method:'POST'});
    const d = await r.json();
    pushLive({type:'state_change', project_id:id, t:Date.now()/1000, data:{state: d.status==='retrying'?'FIXING':'ERROR'}});
  } catch(e) {
    console.error('retry failed', e);
  }
}

init();
