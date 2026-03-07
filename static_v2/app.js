/**
 * Codegen Agent V2 — UI
 * SSE-driven, real-time layer + file card updates.
 * Includes: Brief Generator, Briefs panel, drag-and-drop to build.
 */

// ── State ─────────────────────────────────────────────────────────────────

const state = {
  projects: [],
  briefs: [],
  activeId: null,
  files: {},          // { file_path: { status, layer } }
  sse: null,
  logLines: 0,
  timerInterval: null,
  timerStart: null,
  pendingBriefContent: null,  // brief content waiting for build confirmation
  pendingBriefName: null,
  editingBriefName: null,
};

// ── DOM refs ──────────────────────────────────────────────────────────────

const $ = id => document.getElementById(id);

const dom = {
  projectList:      $('project-list'),
  projectCount:     $('project-count'),
  briefList:        $('brief-list'),
  briefCount:       $('brief-count'),
  emptyState:       $('empty-state'),
  projectDetail:    $('project-detail'),
  projName:         $('proj-name'),
  globalStatus:     $('global-status'),
  statDuration:     $('stat-duration'),
  statPlanned:      $('stat-files-planned'),
  statDone:         $('stat-files-done'),
  topbarStats:      $('topbar-stats'),
  qaBadge:          $('qa-badge'),
  btnRetry:         $('btn-retry'),
  btnDeleteProj:    $('btn-delete-proj'),
  manifestPanel:    $('manifest-panel'),
  manifestChips:    $('manifest-chips'),
  fileGrid:         $('file-grid'),
  fileCount:        $('file-count'),
  logBody:          $('log-body'),
  modalOverlay:     $('modal-overlay'),
  briefInput:       $('brief-input'),
  btnBuild:         $('btn-build'),
  dropZone:         $('drop-zone'),
  mainPanel:        $('main-panel'),
};

// ── Bootstrap ─────────────────────────────────────────────────────────────

async function init() {
  await Promise.all([loadProjects(), loadBriefs()]);
  bindUI();
  const active = state.projects.find(p =>
    !['COMPLETE', 'FAILED', 'LAYER_FAILED'].includes(p.status)
  ) || state.projects[0];
  if (active) selectProject(active.id);
}

// ── Sidebar tabs ──────────────────────────────────────────────────────────

function switchTab(tab) {
  document.querySelectorAll('.sidebar-tab').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.sidebar-panel').forEach(el => el.classList.add('hidden'));
  $(`tab-${tab}`).classList.add('active');
  $(`panel-${tab}`).classList.remove('hidden');
}

// ── Projects sidebar ──────────────────────────────────────────────────────

async function loadProjects() {
  try {
    const res = await fetch('/api/v2/projects');
    if (res.ok) state.projects = await res.json();
  } catch { /* ignore */ }
  renderSidebar();
}

function renderSidebar() {
  dom.projectCount.textContent = state.projects.length;
  dom.projectList.innerHTML = '';

  if (!state.projects.length) {
    dom.projectList.innerHTML =
      '<li style="color:var(--text-subtle);font-size:12px;padding:16px;pointer-events:none">No projects yet</li>';
    return;
  }

  state.projects.forEach(p => {
    const li = document.createElement('li');
    li.className = 'project-item' + (p.id === state.activeId ? ' active' : '');
    li.dataset.id = p.id;

    const name  = p.manifest?.project_name || (p.brief || '').slice(0, 32) || p.id;
    const ts    = p.created_at ? new Date(p.created_at * 1000).toLocaleTimeString() : '';
    const isDone = ['COMPLETE','FAILED','LAYER_FAILED'].includes(p.status);

    li.innerHTML = `
      <div class="p-name">${esc(name)}</div>
      <div class="p-meta">
        <span class="status-badge status-${p.status}" style="font-size:9px;padding:1px 5px">${p.status}</span>
        <span class="p-time">${ts}</span>
        <button class="pi-delete" title="${isDone ? 'Remove' : 'Cancel'}" data-id="${esc(p.id)}">✕</button>
      </div>
    `;
    li.addEventListener('click', e => {
      if (e.target.classList.contains('pi-delete')) return;
      selectProject(p.id);
    });
    li.querySelector('.pi-delete').addEventListener('click', e => {
      e.stopPropagation();
      deleteProject(p.id);
    });
    dom.projectList.appendChild(li);
  });
}

async function retryProject() {
  if (!state.activeId) return;
  const proj = state.projects.find(p => p.id === state.activeId);
  if (!proj?.brief) return;

  await fetch(`/api/v2/projects/${state.activeId}`, { method: 'DELETE' });
  state.projects = state.projects.filter(p => p.id !== state.activeId);

  dom.btnRetry.classList.add('hidden');
  appendLog('Retrying project…', 'accent');

  try {
    const res = await fetch('/api/v2/projects', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ brief: proj.brief }),
    });
    const data = await res.json();
    if (data.id) {
      await loadProjects();
      selectProject(data.id);
    }
  } catch (e) {
    appendLog('Retry failed: ' + e.message, 'error');
  }
}

async function deleteProject(id) {
  try {
    await fetch(`/api/v2/projects/${id}`, { method: 'DELETE' });
  } catch { /* ignore */ }
  state.projects = state.projects.filter(p => p.id !== id);
  if (state.activeId === id) {
    if (state.sse) { state.sse.close(); state.sse = null; }
    stopTimer();
    state.activeId = null;
    dom.projectDetail.style.display = 'none';
    dom.emptyState.style.display = '';
  }
  renderSidebar();
}

// ── Briefs sidebar ─────────────────────────────────────────────────────────

async function loadBriefs() {
  try {
    const res = await fetch('/api/v2/briefs');
    if (res.ok) state.briefs = await res.json();
  } catch { /* ignore */ }
  renderBriefs();
}

function renderBriefs() {
  dom.briefCount.textContent = state.briefs.length;
  dom.briefList.innerHTML = '';

  if (!state.briefs.length) {
    dom.briefList.innerHTML =
      '<li class="brief-empty">No briefs yet — click Generate to create one</li>';
    return;
  }

  state.briefs.forEach(b => {
    const li = document.createElement('li');
    li.className = 'brief-item';
    li.draggable = true;
    li.dataset.name = b.name;

    const date = b.modified ? new Date(b.modified * 1000).toLocaleDateString() : '';
    const sizeKB = (b.size / 1024).toFixed(1);

    li.innerHTML = `
      <div class="bi-header">
        <span class="bi-icon">📄</span>
        <span class="bi-name">${esc(b.name)}</span>
      </div>
      <div class="bi-preview">${esc((b.preview || '').slice(0, 80))}</div>
      <div class="bi-meta">
        <span class="bi-date">${date}</span>
        <span class="bi-size">${sizeKB} KB</span>
        <div class="bi-actions">
          <button class="bi-btn" title="Edit" data-action="edit" data-name="${esc(b.name)}">✎</button>
          <button class="bi-btn danger" title="Delete" data-action="delete" data-name="${esc(b.name)}">✕</button>
        </div>
      </div>
    `;

    // Click to edit
    li.addEventListener('click', e => {
      const btn = e.target.closest('[data-action]');
      if (btn?.dataset.action === 'edit') { e.stopPropagation(); openBriefEditor(b.name); return; }
      if (btn?.dataset.action === 'delete') { e.stopPropagation(); deleteBrief(b.name); return; }
    });

    // Drag to build
    li.addEventListener('dragstart', e => {
      e.dataTransfer.setData('text/brief-name', b.name);
      e.dataTransfer.effectAllowed = 'copy';
      li.classList.add('dragging');
      dom.dropZone.classList.add('active');
    });
    li.addEventListener('dragend', () => {
      li.classList.remove('dragging');
      dom.dropZone.classList.remove('active');
    });

    dom.briefList.appendChild(li);
  });
}

// ── Generate brief modal ───────────────────────────────────────────────────

function openGenBriefModal() {
  $('gen-brief-overlay').classList.add('open');
  $('brief-raw-input').value = '';
  setGenerating(false);
  setTimeout(() => $('brief-raw-input').focus(), 50);
}

function closeGenBriefModal() {
  $('gen-brief-overlay').classList.remove('open');
}

function setGenerating(v) {
  const btn = $('btn-generate-brief');
  btn.classList.toggle('loading', v);
  btn.disabled = v;
}

async function submitGenerateBrief() {
  const brief_text = $('brief-raw-input').value.trim();
  const name       = brief_text.split('\n')[0].slice(0, 60).trim();

  if (!brief_text) {
    $('brief-raw-input').focus();
    return;
  }

  setGenerating(true);
  try {
    const res = await fetch('/api/v2/briefs/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, brief_text }),
    });
    const data = await res.json();
    if (data.name) {
      closeGenBriefModal();
      await loadBriefs();
      switchTab('briefs');
      // Auto-open the editor so user can review/edit before building
      openBriefEditor(data.name);
    } else {
      alert('Error: ' + (data.error || 'Unknown error'));
      setGenerating(false);
    }
  } catch (e) {
    alert('Network error: ' + e.message);
    setGenerating(false);
  }
}

// ── Brief editor modal ─────────────────────────────────────────────────────

async function openBriefEditor(name) {
  state.editingBriefName = name;
  $('brief-editor-title').textContent = name;
  $('brief-editor-area').value = 'Loading…';
  $('brief-editor-overlay').classList.add('open');

  try {
    const res = await fetch(`/api/v2/briefs/${encodeURIComponent(name)}`);
    const data = await res.json();
    $('brief-editor-area').value = data.content || '';
  } catch (e) {
    $('brief-editor-area').value = 'Error loading brief: ' + e.message;
  }
}

function closeBriefEditor() {
  $('brief-editor-overlay').classList.remove('open');
  state.editingBriefName = null;
}

async function saveBrief() {
  const name = state.editingBriefName;
  if (!name) return;
  const content = $('brief-editor-area').value;
  try {
    await fetch(`/api/v2/briefs/${encodeURIComponent(name)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content }),
    });
    await loadBriefs();
    const btn = $('btn-save-brief');
    btn.textContent = 'Saved!';
    setTimeout(() => { btn.textContent = 'Save'; }, 1500);
  } catch (e) {
    alert('Save failed: ' + e.message);
  }
}

async function deleteBrief(name) {
  if (!confirm(`Delete brief "${name}"?`)) return;
  try {
    await fetch(`/api/v2/briefs/${encodeURIComponent(name)}`, { method: 'DELETE' });
    await loadBriefs();
  } catch { /* ignore */ }
}

// ── Build from brief (confirmation) ───────────────────────────────────────

async function promptBuildFromBrief(briefName) {
  // Fetch content first
  try {
    const res = await fetch(`/api/v2/briefs/${encodeURIComponent(briefName)}`);
    const data = await res.json();
    if (!data.content) { alert('Brief not found'); return; }
    state.pendingBriefContent = data.content;
    state.pendingBriefName    = briefName;
  } catch (e) {
    alert('Error loading brief: ' + e.message);
    return;
  }

  // Show preview in confirmation modal
  const preview = $('build-brief-preview');
  preview.textContent = state.pendingBriefContent.slice(0, 600) +
    (state.pendingBriefContent.length > 600 ? '\n\n…(truncated for preview)' : '');

  $('build-brief-overlay').classList.add('open');
  setBuilding(false, 'btn-confirm-build');
}

function closeBuildBriefModal() {
  $('build-brief-overlay').classList.remove('open');
  state.pendingBriefContent = null;
  state.pendingBriefName    = null;
}

async function confirmBuildFromBrief() {
  const brief = state.pendingBriefContent;
  if (!brief) return;

  setBuilding(true, 'btn-confirm-build');
  try {
    const res = await fetch('/api/v2/projects', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ brief }),
    });
    const data = await res.json();
    if (data.id) {
      closeBuildBriefModal();
      switchTab('projects');
      await loadProjects();
      selectProject(data.id);
    } else {
      alert('Error: ' + (data.error || 'Unknown'));
      setBuilding(false, 'btn-confirm-build');
    }
  } catch (e) {
    alert('Network error: ' + e.message);
    setBuilding(false, 'btn-confirm-build');
  }
}

// ── Project selection ──────────────────────────────────────────────────────

async function selectProject(id) {
  state.activeId = id;
  state.files = {};
  state.logLines = 0;
  stopTimer();

  dom.logBody.innerHTML = '<div class="log-line"><span class="ll-text" style="color:var(--text-subtle)">Connecting…</span></div>';
  dom.fileGrid.innerHTML = '';
  dom.fileCount.textContent = '0';
  dom.manifestChips.innerHTML = '';
  dom.manifestPanel.style.display = 'none';
  dom.topbarStats.style.display = 'none';
  resetLayerBar();

  dom.emptyState.style.display = 'none';
  dom.projectDetail.style.display = 'flex';

  renderSidebar();

  try {
    const res = await fetch(`/api/v2/projects/${id}`);
    if (res.ok) applyProjectState(await res.json());
  } catch { /* ignore */ }

  openSSE(id);
}

function applyProjectState(data) {
  if (!data || data.error) return;

  const idx = state.projects.findIndex(p => p.id === data.id);
  if (idx >= 0) state.projects[idx] = data;
  else state.projects.unshift(data);

  dom.projName.textContent = data.manifest?.project_name || (data.brief || '').slice(0, 60) || data.id;
  setGlobalStatus(data.status);

  if (data.manifest) {
    renderManifestChips({
      stack: data.manifest.stack,
      models: Array.isArray(data.manifest.models) ? data.manifest.models : Object.keys(data.manifest.models || {}),
      routes: data.manifest.routes,
      auth: data.manifest.auth_sub ? { sub_field: data.manifest.auth_sub } : null,
    });
  }
  if (data.files_planned != null) {
    dom.statPlanned.textContent = data.files_planned;
    dom.topbarStats.style.display = 'flex';
  }
  if (data.files_created != null) {
    dom.statDone.textContent = data.files_created;
    dom.topbarStats.style.display = 'flex';
  }
  if (data.qa_score != null) setQAScore(data.qa_score);
  if (data.duration_s) dom.statDuration.textContent = data.duration_s + 's';

  if (Array.isArray(data.layers)) {
    data.layers.forEach(lr => {
      setLayerStatus(lr.index, lr.status === 'passed' ? 'passed' : lr.status === 'failed' ? 'failed' : 'pending');
    });
  }

  if (data.file_layer_map && typeof data.file_layer_map === 'object') {
    Object.entries(data.file_layer_map).forEach(([fp, layer]) => {
      addFileCard(fp, layer, 'done');
    });
    dom.statDone.textContent = Object.keys(data.file_layer_map).length;
    dom.topbarStats.style.display = 'flex';
  }

  renderSidebar();
}

// ── SSE ───────────────────────────────────────────────────────────────────

function openSSE(id) {
  if (state.sse) { state.sse.close(); state.sse = null; }

  const es = new EventSource(`/api/v2/projects/${id}/stream`);
  state.sse = es;

  es.onmessage = e => {
    let ev;
    try { ev = JSON.parse(e.data); } catch { return; }
    handleEvent(ev);
  };

  es.onerror = () => appendLog('Connection lost — will reconnect…', 'warn');
}

function handleEvent(ev) {
  const { type, data } = ev;

  switch (type) {

    case 'state_sync':
      applyProjectState(data);
      break;

    case 'heartbeat':
      break;

    case 'status_change':
      setGlobalStatus(data.status);
      appendLog(`Status → ${data.status}`, 'accent');
      if (['PLANNING','ARCHITECTING','BUILDING','QA_RUNNING'].includes(data.status)) {
        if (!state.timerInterval) startTimer();
      }
      break;

    case 'plan_ready':
      dom.statPlanned.textContent = data.files_planned;
      dom.topbarStats.style.display = 'flex';
      dom.projName.textContent = data.project_name || dom.projName.textContent;
      renderManifestChips({
        stack: data.stack,
        models: data.models || [],
        routes: data.routes,
      });
      appendLog(`Plan ready — ${data.files_planned} files · ${(data.layers || []).length} layers`, 'success');
      appendLog(`Stack: ${data.stack} | Models: ${(data.models || []).join(', ')}`, 'primary');
      if (Array.isArray(data.file_specs)) {
        data.file_specs.forEach(f => addFileCard(f.file_path, f.layer, 'pending'));
      }
      break;

    case 'layer_started':
      setLayerStatus(data.layer, 'running');
      appendLog(`▶ Layer ${data.layer} — ${data.name} (${data.file_count} files)`, 'accent');
      break;

    case 'layer_passed':
      setLayerStatus(data.layer, 'passed');
      appendLog(`✓ Layer ${data.layer} passed — ${data.files} files · ${data.duration_s}s`, 'success');
      break;

    case 'layer_failed':
      setLayerStatus(data.layer, 'failed');
      appendLog(`✗ Layer ${data.layer} failed`, 'error');
      break;

    case 'file_done': {
      const fp = data.file || data.file_path || '';
      if (fp) {
        const finalStatus = data.status === 'retried' ? 'retried' : 'done';
        if (!state.files[fp]) addFileCard(fp, data.layer, 'generating');
        updateFileCard(fp, finalStatus, data.lines);
        dom.statDone.textContent = Object.values(state.files).filter(f =>
          f.status === 'done' || f.status === 'retried'
        ).length;
        dom.topbarStats.style.display = 'flex';
      }
      break;
    }

    case 'file_failed': {
      const fp = data.file || data.file_path || '';
      if (fp) {
        if (!state.files[fp]) addFileCard(fp, data.layer, 'generating');
        updateFileCard(fp, 'failed');
        appendLog(`✗ ${fp}`, 'error');
      }
      break;
    }

    case 'file_guard_failed':
      appendLog(`  Guard: ${data.file} — ${data.reason}`, 'warn');
      break;

    case 'wave_started': {
      const files = data.files || [];
      appendLog(`  Wave ${data.wave} — ${files.length} files in parallel`, 'info');
      files.forEach(fp => addFileCard(fp, data.layer, 'generating'));
      break;
    }

    case 'wave_generating':
      break;

    case 'healing_started':
      appendLog(`  Healing layer ${data.layer} round ${data.round}…`, 'warn');
      break;

    case 'healing_done':
      appendLog(`  Healed layer ${data.layer} — ${data.status}`,
        data.status === 'fixed' ? 'success' : 'warn');
      break;

    case 'manifest_updated':
      appendLog(`  Manifest locked from disk — ${(data.models || []).join(', ')}`, 'info');
      break;

    case 'project_done':
      setGlobalStatus('COMPLETE');
      setQAScore(data.qa_score);
      dom.statDone.textContent = data.files_created;
      stopTimer();
      dom.statDuration.textContent = data.duration_s + 's';
      appendLog(
        `✓ Complete — QA ${data.qa_score} · ${data.files_created} files · ${data.duration_s}s`,
        'success'
      );
      if (data.issues?.length) {
        data.issues.slice(0, 5).forEach(i => appendLog(`  ⚠ ${i}`, 'warn'));
      }
      loadProjects();
      break;

    case 'project_failed':
      setGlobalStatus(data.status || 'FAILED');
      stopTimer();
      appendLog(`✗ Pipeline failed: ${data.error}`, 'error');
      loadProjects();
      break;

    case 'log':
      appendLog(data.line, classifyLine(data.line));
      break;

    case 'log_history': {
      const lines = data.lines || [];
      if (!lines.length) break;
      dom.logBody.innerHTML = '';
      state.logLines = 0;
      lines.forEach(line => appendLog(line, classifyLine(line)));
      break;
    }
  }
}

// ── File cards ────────────────────────────────────────────────────────────

function fileIcon(path) {
  if (path.endsWith('.py'))   return '🐍';
  if (path.endsWith('.html')) return '🌐';
  if (path.endsWith('.css'))  return '🎨';
  if (path.endsWith('.js'))   return '📜';
  if (path.endsWith('.txt') || path.endsWith('.ini') || path.endsWith('.toml')) return '📄';
  if (path.endsWith('.json')) return '{}';
  return '📁';
}

function addFileCard(filePath, layer, status) {
  if (state.files[filePath]) {
    if (status !== 'pending') updateFileCard(filePath, status);
    return;
  }
  state.files[filePath] = { status, layer };

  const card = document.createElement('div');
  const l = layer || 1;
  card.className = `file-card ${status} fade-in`;
  card.id = cardId(filePath);
  card.title = filePath;

  const name = filePath.split('/').pop();
  const dir  = filePath.includes('/') ? filePath.slice(0, filePath.lastIndexOf('/')) : '';

  card.innerHTML = `
    <div class="fc-header">
      <span class="l-dot ld-${l}"></span>
      <span class="fc-icon">${fileIcon(filePath)}</span>
      <span class="fc-name" title="${esc(filePath)}">${esc(name)}</span>
    </div>
    <div class="fc-meta">
      <span class="fc-lines"></span>
      <span class="fc-layer lb-${l}">L${l}</span>
    </div>
  `;
  if (dir) {
    const dirEl = document.createElement('div');
    dirEl.style.cssText = 'font-size:9px;color:var(--text-subtle);font-family:var(--mono);margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap';
    dirEl.textContent = dir;
    card.querySelector('.fc-meta').before(dirEl);
  }

  card.addEventListener('click', () => openFileViewer(filePath));
  dom.fileGrid.appendChild(card);
  dom.fileCount.textContent = Object.keys(state.files).length;
}

function updateFileCard(filePath, status, lines) {
  if (!state.files[filePath]) return;
  state.files[filePath].status = status;
  const card = document.getElementById(cardId(filePath));
  if (!card) return;
  card.classList.remove('pending', 'generating', 'done', 'retried', 'failed');
  card.classList.add(status);
  if (lines != null) {
    const linesEl = card.querySelector('.fc-lines');
    if (linesEl) linesEl.textContent = lines + 'L';
  }
}

function cardId(fp) {
  return 'fc-' + fp.replace(/[^a-zA-Z0-9]/g, '_');
}

// ── Layer bar ─────────────────────────────────────────────────────────────

function setLayerStatus(layerNum, status) {
  const seg = document.querySelector(`.layer-seg[data-layer="${layerNum}"]`);
  if (!seg) return;
  seg.classList.remove('pending', 'running', 'passed', 'failed', 'needs-review');
  seg.classList.add(status);
}

function resetLayerBar() {
  document.querySelectorAll('.layer-seg').forEach(s => {
    s.classList.remove('running', 'passed', 'failed', 'needs-review');
  });
}

// ── Manifest chips ────────────────────────────────────────────────────────

function renderManifestChips(manifest) {
  dom.manifestPanel.style.display = 'block';
  const frags = [];

  if (manifest.stack) {
    frags.push(`<span class="manifest-chip">${esc(manifest.stack)}</span>`);
  }
  (manifest.models || []).forEach(m => {
    frags.push(`<span class="manifest-chip model">${esc(m)}</span>`);
  });
  if (manifest.routes != null) {
    frags.push(`<span class="manifest-chip route">${manifest.routes} routes</span>`);
  }
  if (manifest.auth?.sub_field || manifest.auth_sub) {
    const sub = manifest.auth?.sub_field || manifest.auth_sub;
    frags.push(`<span class="manifest-chip schema">auth:${esc(sub)}</span>`);
  }

  dom.manifestChips.innerHTML = frags.join('');
}

// ── QA score ─────────────────────────────────────────────────────────────

function setQAScore(score) {
  const el = dom.qaBadge;
  el.textContent = score != null ? `QA ${score}` : 'QA —';
  el.classList.remove('good', 'ok', 'bad');
  if (score == null) return;
  if (score >= 75)      el.classList.add('good');
  else if (score >= 50) el.classList.add('ok');
  else                  el.classList.add('bad');
}

// ── Status badge ──────────────────────────────────────────────────────────

function setGlobalStatus(status) {
  dom.globalStatus.textContent = status;
  dom.globalStatus.className = `status-badge status-${status}`;
  const retryable = ['FAILED', 'LAYER_FAILED', 'NEEDS_REVIEW'].includes(status);
  dom.btnRetry.classList.toggle('hidden', !retryable);
}

// ── Log panel ─────────────────────────────────────────────────────────────

const MAX_LOG = 500;

function appendLog(text, cls) {
  if (state.logLines >= MAX_LOG) {
    dom.logBody.firstChild?.remove();
  }
  const now = new Date();
  const ts  = now.toTimeString().slice(0, 8);

  const line = document.createElement('div');
  line.className = 'log-line' + (cls ? ` ${cls}` : '');
  line.innerHTML = `<span class="ll-time">${ts}</span><span class="ll-text">${esc(text)}</span>`;

  dom.logBody.appendChild(line);
  dom.logBody.scrollTop = dom.logBody.scrollHeight;
  state.logLines++;
}

function classifyLine(line) {
  const l = line.toLowerCase();
  if (l.includes('error') || l.includes('failed') || l.includes('✗')) return 'error';
  if (l.includes('warn') || l.includes('⚠'))                           return 'warn';
  if (l.includes('done') || l.includes('✓') || l.includes('passed') || l.includes('complete')) return 'success';
  if (l.includes('[v2]') || l.includes('layer') || l.includes('wave') || l.startsWith('▶'))   return 'accent';
  return '';
}

// ── Timer ─────────────────────────────────────────────────────────────────

function startTimer() {
  state.timerStart = Date.now();
  state.timerInterval = setInterval(() => {
    dom.statDuration.textContent = ((Date.now() - state.timerStart) / 1000).toFixed(0) + 's';
  }, 1000);
}

function stopTimer() {
  if (state.timerInterval) { clearInterval(state.timerInterval); state.timerInterval = null; }
}

// ── New Project modal (manual brief) ──────────────────────────────────────

function openModal() {
  dom.modalOverlay.classList.add('open');
  dom.briefInput.value = '';
  setBuilding(false, 'btn-build');
  setTimeout(() => dom.briefInput.focus(), 50);
}

function closeModal() {
  dom.modalOverlay.classList.remove('open');
}

function setBuilding(v, btnId = 'btn-build') {
  const btn = $(btnId);
  if (!btn) return;
  btn.classList.toggle('loading', v);
  btn.disabled = v;
}

async function submitBrief() {
  const brief = dom.briefInput.value.trim();
  if (!brief) { dom.briefInput.focus(); return; }

  setBuilding(true, 'btn-build');
  try {
    const res = await fetch('/api/v2/projects', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ brief }),
    });
    const data = await res.json();
    if (data.id) {
      closeModal();
      await loadProjects();
      selectProject(data.id);
    } else {
      appendLog('Error: ' + (data.error || 'unknown'), 'error');
      setBuilding(false, 'btn-build');
    }
  } catch (e) {
    appendLog('Network error: ' + e.message, 'error');
    setBuilding(false, 'btn-build');
  }
}

// ── File viewer ───────────────────────────────────────────────────────────

async function openFileViewer(filePath) {
  if (!state.activeId) return;
  const overlay = $('viewer-overlay');
  const title   = $('viewer-title');
  const pre     = $('viewer-pre');

  title.textContent = filePath;
  pre.textContent   = 'Loading…';
  overlay.classList.add('open');

  try {
    const res = await fetch(`/api/v2/projects/${state.activeId}/files/${filePath}`);
    const data = await res.json();
    if (data.content != null) {
      pre.textContent = data.content;
    } else {
      pre.textContent = data.error || 'File not available yet.';
    }
  } catch (e) {
    pre.textContent = 'Error: ' + e.message;
  }
}

function closeFileViewer() {
  $('viewer-overlay').classList.remove('open');
}

// ── Drag and Drop (brief → main panel) ────────────────────────────────────

function initDragDrop() {
  const mainPanel = dom.mainPanel;

  mainPanel.addEventListener('dragover', e => {
    if (!e.dataTransfer.types.includes('text/brief-name')) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
    dom.dropZone.classList.add('active');
  });

  mainPanel.addEventListener('dragleave', e => {
    // Only hide if truly leaving the main panel
    if (!mainPanel.contains(e.relatedTarget)) {
      dom.dropZone.classList.remove('active');
    }
  });

  mainPanel.addEventListener('drop', async e => {
    e.preventDefault();
    const briefName = e.dataTransfer.getData('text/brief-name');
    dom.dropZone.classList.remove('active');
    if (briefName) {
      await promptBuildFromBrief(briefName);
    }
  });
}

// ── Bindings ──────────────────────────────────────────────────────────────

function bindUI() {
  // Sidebar tabs
  document.querySelectorAll('.sidebar-tab').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });

  // Topbar buttons
  $('btn-new-project').addEventListener('click', openModal);
  $('btn-gen-brief').addEventListener('click', openGenBriefModal);

  // Briefs panel
  $('btn-gen-brief-sm').addEventListener('click', openGenBriefModal);

  // New project modal
  $('btn-cancel').addEventListener('click', closeModal);
  dom.btnBuild.addEventListener('click', submitBrief);
  dom.modalOverlay.addEventListener('click', e => {
    if (e.target === dom.modalOverlay) closeModal();
  });
  dom.briefInput.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') submitBrief();
  });

  // Generate brief modal
  $('btn-cancel-brief').addEventListener('click', closeGenBriefModal);
  $('btn-cancel-brief-2').addEventListener('click', closeGenBriefModal);
  $('gen-brief-overlay').addEventListener('click', e => {
    if (e.target === $('gen-brief-overlay')) closeGenBriefModal();
  });
  $('btn-generate-brief').addEventListener('click', submitGenerateBrief);
  $('brief-raw-input').addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') submitGenerateBrief();
  });

  // Brief editor modal
  $('brief-editor-close').addEventListener('click', closeBriefEditor);
  $('brief-editor-overlay').addEventListener('click', e => {
    if (e.target === $('brief-editor-overlay')) closeBriefEditor();
  });
  $('btn-save-brief').addEventListener('click', saveBrief);
  $('btn-copy-brief').addEventListener('click', () => {
    const text = $('brief-editor-area').value;
    navigator.clipboard.writeText(text).then(() => {
      const btn = $('btn-copy-brief');
      btn.textContent = 'Copied!';
      setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
    });
  });

  // Build from brief modal
  $('btn-cancel-build').addEventListener('click', closeBuildBriefModal);
  $('build-brief-overlay').addEventListener('click', e => {
    if (e.target === $('build-brief-overlay')) closeBuildBriefModal();
  });
  $('btn-confirm-build').addEventListener('click', confirmBuildFromBrief);

  // Project controls
  $('btn-retry').addEventListener('click', retryProject);
  $('btn-delete-proj').addEventListener('click', () => {
    if (state.activeId && confirm('Delete this project and all its files?')) {
      deleteProject(state.activeId);
    }
  });

  // File viewer
  $('viewer-close').addEventListener('click', closeFileViewer);
  $('viewer-overlay').addEventListener('click', e => {
    if (e.target === $('viewer-overlay')) closeFileViewer();
  });
  $('btn-copy-file').addEventListener('click', () => {
    const text = $('viewer-pre').textContent;
    navigator.clipboard.writeText(text).then(() => {
      const btn = $('btn-copy-file');
      btn.textContent = 'Copied!';
      setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
    });
  });

  // File search
  $('file-search').addEventListener('input', e => {
    const q = e.target.value.toLowerCase();
    document.querySelectorAll('.file-card').forEach(card => {
      card.style.display = (!q || card.title.toLowerCase().includes(q)) ? '' : 'none';
    });
  });

  // Log clear
  $('btn-clear-log').addEventListener('click', () => {
    dom.logBody.innerHTML = '';
    state.logLines = 0;
  });

  // Drag and drop
  initDragDrop();

  // Background poll
  setInterval(async () => {
    const snap = state.projects.map(p => p.id + p.status).join();
    await loadProjects();
    if (state.projects.map(p => p.id + p.status).join() !== snap) renderSidebar();
  }, 10000);
}

// ── Util ──────────────────────────────────────────────────────────────────

function esc(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ── Go ────────────────────────────────────────────────────────────────────

init();
