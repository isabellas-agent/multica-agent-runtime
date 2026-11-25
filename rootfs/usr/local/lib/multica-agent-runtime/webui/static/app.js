/* multica-agent-runtime · workspace cleanup web UI (no framework, no build step) */
'use strict';

const state = {
  data: null,
  config: null,
  filter: 'all',
  query: '',
  selected: new Set(),
  expanded: new Set(),
  targets: [],
  busy: false,
  timer: null,
};

const $ = (id) => document.getElementById(id);

/* --- formatting ---------------------------------------------------------------------------- */

function escapeHtml(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[ch]));
}

function humanBytes(bytes) {
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
  let value = Number(bytes) || 0;
  let index = 0;
  while (Math.abs(value) >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return index === 0 ? `${Math.round(value)} B` : `${value.toFixed(value < 10 ? 1 : 0)} ${units[index]}`;
}

function relativeTime(seconds) {
  if (seconds == null) return 'unknown';
  const s = Math.max(0, Number(seconds));
  if (s < 45) return 'just now';
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  if (s < 2592000) return `${Math.round(s / 86400)} d ago`;
  return `${Math.round(s / 2592000)} mo ago`;
}

function clockTime(epochSeconds) {
  if (!epochSeconds) return '–';
  try {
    return new Date(epochSeconds * 1000).toLocaleString();
  } catch {
    return '–';
  }
}

/* --- toasts -------------------------------------------------------------------------------- */

function toast(title, detail, kind) {
  const node = document.createElement('div');
  node.className = `toast ${kind || ''}`;
  node.innerHTML = `<span class="icon"></span><span class="body"><strong>${escapeHtml(title)}</strong>${
    detail ? `<span>${escapeHtml(detail)}</span>` : ''
  }</span>`;
  $('toasts').appendChild(node);
  setTimeout(() => node.remove(), kind === 'error' ? 9000 : 5000);
}

/* --- data ---------------------------------------------------------------------------------- */

async function load() {
  clearTimeout(state.timer);
  try {
    const response = await fetch('/api/workspaces', { headers: { Accept: 'application/json' } });
    if (response.status === 401) {
      $('empty').hidden = false;
      $('emptyText').textContent = 'Authentication required';
      $('emptyHint').textContent = 'Reload the page and enter the WEBUI_AUTH credentials.';
      return;
    }
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.data = await response.json();
    state.config = state.data.config || state.config;
    pruneSelection();
    render();
  } catch (error) {
    $('empty').hidden = false;
    $('emptyText').textContent = 'Could not load workspaces';
    $('emptyHint').textContent = String(error.message || error);
    $('rows').innerHTML = '';
    $('generatedAt').textContent = '';
    toast('Scan failed', String(error.message || error), 'error');
  } finally {
    scheduleRefresh();
  }
}

function scheduleRefresh() {
  clearTimeout(state.timer);
  const seconds = Number(state.config && state.config.refresh_seconds) || 15;
  state.timer = setTimeout(() => {
    if (document.visibilityState === 'visible' && !state.busy && !$('confirmDialog').open) load();
    else scheduleRefresh();
  }, seconds * 1000);
}

function pruneSelection() {
  const deletable = new Map((state.data?.workspaces || []).filter((ws) => ws.deletable).map((ws) => [ws.dir, ws]));
  for (const dir of [...state.selected]) {
    if (!deletable.has(dir)) state.selected.delete(dir);
  }
}

/* --- rendering ----------------------------------------------------------------------------- */

function visibleWorkspaces() {
  const workspaces = state.data?.workspaces || [];
  const query = state.query.trim().toLowerCase();
  return workspaces.filter((ws) => {
    const matchesFilter = state.filter === 'all' || (state.filter === 'idle' ? ws.deletable : ws.in_use);
    if (!matchesFilter) return false;
    if (!query) return true;
    const haystack = [ws.name, ws.slug, ws.dir, ws.workspace_id, ...(ws.tasks || []).map((task) => task.name)]
      .join(' ')
      .toLowerCase();
    return haystack.includes(query);
  });
}

function render() {
  const data = state.data;
  if (!data) return;
  const workspaces = data.workspaces || [];
  const totals = data.totals || {};
  const config = data.config || state.config || {};

  $('rootPath').textContent = data.root || '';

  $('statSize').textContent = totals.size || humanBytes(totals.size_bytes || 0);
  $('statSizeHint').textContent = `${totals.task_count || 0} task directories`;
  $('statCount').textContent = String(totals.workspace_count || 0);
  $('statTaskHint').textContent = totals.workspace_count === 1 ? '1 workspace directory' : `${workspaces.length} directories`;
  $('statInUse').textContent = String(totals.in_use_count || 0);
  $('statReclaim').textContent = totals.deletable_size || humanBytes(totals.deletable_size_bytes || 0);
  $('statReclaimHint').textContent = `${totals.deletable_count || 0} idle workspace${totals.deletable_count === 1 ? '' : 's'}`;

  renderDaemon(data.daemon);
  renderRows(workspaces);
  renderCounts(workspaces);
  renderSelection();

  $('generatedAt').textContent = `Scanned in ${Number(data.scan_seconds || 0).toFixed(2)}s · ${clockTime(data.generated_at)}`;
  $('footNote').textContent = config.auth
    ? 'Authenticated · deletion removes local data only; the daemon recreates it on the next task.'
    : 'Deletion removes local data only; the daemon recreates it on the next task.';
  $('selectIdleWrap').hidden = !workspaces.some((ws) => ws.deletable);
  $('deleteSelected').disabled = state.selected.size === 0 || Boolean(config.read_only) || state.busy;
}

function renderDaemon(daemon) {
  const node = $('daemon');
  node.classList.remove('up', 'busy', 'down');
  if (!daemon || !daemon.reachable) {
    node.classList.add('down');
    $('daemonText').textContent = 'daemon offline';
    node.title = `Local daemon not reachable at ${(daemon && daemon.url) || 'the configured URL'}`;
    return;
  }
  const running = Number(daemon.running_task_count || 0);
  const active = Number(daemon.active_task_count || 0);
  node.classList.add(running > 0 ? 'busy' : 'up');
  $('daemonText').textContent = running > 0 ? `${running} task${running === 1 ? '' : 's'} running` : 'daemon idle';
  node.title = `${daemon.daemon_id || 'daemon'} · ${active} active task${active === 1 ? '' : 's'} · uptime ${daemon.uptime || '?'}`;
}

function renderCounts(workspaces) {
  const all = workspaces.length;
  const idle = workspaces.filter((ws) => ws.deletable).length;
  $('countAll').textContent = String(all);
  $('countIdle').textContent = String(idle);
  $('countInUse').textContent = String(all - idle);
}

function renderRows(workspaces) {
  const visible = visibleWorkspaces();
  const rows = $('rows');
  const empty = $('empty');
  const maxSize = Math.max(1, ...workspaces.map((ws) => ws.size_bytes || 0));

  if (!visible.length) {
    rows.innerHTML = '';
    empty.hidden = false;
    $('emptyText').textContent = workspaces.length ? 'Nothing matches this filter.' : 'No workspaces found.';
    $('emptyHint').textContent = workspaces.length
      ? 'Clear the search or switch the filter.'
      : `Nothing under ${state.data.root}. New data appears here after the first task.`;
    return;
  }
  empty.hidden = true;

  rows.innerHTML = visible
    .map((ws) => {
      const selected = state.selected.has(ws.dir);
      const expanded = state.expanded.has(ws.dir);
      const checked = selected ? 'checked' : '';
      const disabled = ws.deletable ? '' : 'disabled';
      const fill = Math.max(2, Math.round(((ws.size_bytes || 0) / maxSize) * 64));
      const status = ws.protected
        ? '<span class="pill locked">Protected</span>'
        : ws.in_use
          ? '<span class="pill busy">In use</span>'
          : '<span class="pill ok">Deletable</span>';
      const age = ws.last_activity_seconds == null ? 'unknown' : relativeTime(ws.last_activity_seconds);
      const fresh = ws.last_activity_seconds != null && ws.last_activity_seconds < 3600 ? 'fresh' : '';
      const name = ws.name || ws.slug || ws.dir;
      const showSlug = ws.slug && ws.slug !== ws.dir;

      return `
        <tr class="row ${selected ? 'selected' : ''} ${expanded ? 'expanded' : ''}" data-dir="${escapeHtml(ws.dir)}">
          <td class="c-check"><input type="checkbox" data-check="${escapeHtml(ws.dir)}" ${checked} ${disabled} aria-label="Select ${escapeHtml(name)}" /></td>
          <td class="c-name">
            <button class="expand" data-expand="${escapeHtml(ws.dir)}" aria-expanded="${expanded}" aria-label="Details of ${escapeHtml(name)}">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="m9 6 6 6-6 6"/></svg>
            </button>
            <span class="ws-cell">
              <span class="ws-name">${escapeHtml(name)}</span>
              <span class="ws-dir">${escapeHtml(ws.dir)}</span>
              ${showSlug ? `<span class="ws-id">${escapeHtml(ws.workspace_id || '')}</span>` : ''}
            </span>
          </td>
          <td class="num">
            <span class="size-wrap">
              <svg class="bar" viewBox="0 0 64 4" preserveAspectRatio="none" aria-hidden="true"><rect class="track" width="64" height="4" rx="2" /><rect class="fill" width="${fill}" height="4" rx="2" /></svg>
              <span>${escapeHtml(ws.size || humanBytes(ws.size_bytes))}</span>
            </span>
          </td>
          <td class="num tasks-col">${ws.task_count || 0}</td>
          <td class="age-col"><span class="age ${fresh}" title="${escapeHtml(clockTime(ws.newest_mtime))}">${escapeHtml(age)}</span></td>
          <td>${status}</td>
          <td class="actions">
            ${
              ws.deletable
                ? `<button class="btn small danger" data-delete="${escapeHtml(ws.dir)}" ${state.config && state.config.read_only ? 'disabled' : ''}>Delete</button>`
                : `<button class="btn small ghost" data-expand="${escapeHtml(ws.dir)}">Why?</button>`
            }
          </td>
        </tr>
        ${expanded ? detailsRow(ws) : ''}`;
    })
    .join('');
}

function detailsRow(ws) {
  const tasks = (ws.tasks || [])
    .slice()
    .sort((a, b) => b.size_bytes - a.size_bytes)
    .slice(0, 8);
  const taskItems = tasks.length
    ? tasks
        .map(
          (task) =>
            `<li><code>${escapeHtml(task.name)}</code><span class="dim">${escapeHtml(humanBytes(task.size_bytes))} · ${escapeHtml(
              relativeTime(taskAge(ws, task)),
            )}</span></li>`,
        )
        .join('')
    : '<li class="note">no task directories</li>';
  const processes = (ws.processes || []).length
    ? ws.processes
        .map(
          (proc) =>
            `<li><code>${escapeHtml(proc.name || 'pid ' + proc.pid)}</code><span class="dim">pid ${escapeHtml(proc.pid)}</span></li>`,
        )
        .join('')
    : '<li class="note">no process in this workspace</li>';
  const reasons = (ws.reasons || []).length
    ? ws.reasons.map((reason) => `<li class="reason">${escapeHtml(reason)}</li>`).join('')
    : `<li class="note">${ws.protected ? 'protection list' : 'idle: safe to delete'}</li>`;

  return `
    <tr class="details-row">
      <td class="details-cell" colspan="7">
        <div class="details">
          <div class="detail-block">
            <h4>Safety checks</h4>
            <ul>${reasons}</ul>
          </div>
          <div class="detail-block">
            <h4>Tasks (${ws.tasks.length})</h4>
            <ul>${taskItems}</ul>
          </div>
          <div class="detail-block">
            <h4>Processes</h4>
            <ul>${processes}</ul>
          </div>
          <div class="detail-block">
            <h4>Location</h4>
            <ul>
              <li><code>${escapeHtml(ws.path)}</code></li>
              ${ws.workspace_id ? `<li><span class="dim">workspace ${escapeHtml(ws.workspace_id)}</span></li>` : ''}
              ${ws.partial ? '<li class="reason">size scan incomplete</li>' : ''}
            </ul>
          </div>
        </div>
      </td>
    </tr>`;
}

function taskAge(ws, task) {
  if (task.newest_mtime) return Math.max(0, Date.now() / 1000 - task.newest_mtime);
  return ws.last_activity_seconds;
}

function renderSelection() {
  const deletable = (state.data?.workspaces || []).filter((ws) => ws.deletable);
  const allSelected = deletable.length > 0 && deletable.every((ws) => state.selected.has(ws.dir));
  $('selectIdle').checked = allSelected;
  $('selectIdle').indeterminate = !allSelected && state.selected.size > 0;
  const count = state.selected.size;
  $('deleteSelected').disabled = count === 0 || Boolean(state.config && state.config.read_only) || state.busy;
  $('deleteSelected').innerHTML = count
    ? `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2M6 7l1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13"/></svg>Delete ${count}`
    : `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2M6 7l1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13"/></svg>Delete selected`;
}

/* --- selection and expansion --------------------------------------------------------------- */

function toggleSelection(dir, checked) {
  if (checked) state.selected.add(dir);
  else state.selected.delete(dir);
  const row = document.querySelector(`tr.row[data-dir="${cssEscape(dir)}"]`);
  if (row) row.classList.toggle('selected', checked);
  renderSelection();
}

function cssEscape(value) {
  return String(value).replace(/["\\]/g, '\\$&');
}

function toggleExpand(dir) {
  if (state.expanded.has(dir)) state.expanded.delete(dir);
  else state.expanded.add(dir);
  renderRows(state.data.workspaces || []);
}

/* --- deletion ------------------------------------------------------------------------------ */

function openConfirm(dirs) {
  const byDir = new Map((state.data.workspaces || []).map((ws) => [ws.dir, ws]));
  const targets = dirs.map((dir) => byDir.get(dir)).filter(Boolean);
  if (!targets.length) return;
  if (targets.some((ws) => !ws.deletable)) {
    toast('Not deletable', 'A selected workspace is in use or protected.', 'error');
    return;
  }
  state.targets = targets;
  $('confirmList').innerHTML = targets
    .map((ws) => `<li><code>${escapeHtml(ws.dir)}</code><span class="sz">${escapeHtml(ws.size || humanBytes(ws.size_bytes))}</span></li>`)
    .join('');
  const single = targets.length === 1;
  $('confirmLabel').textContent = single
    ? `Type ${targets[0].dir} to confirm`
    : 'Type DELETE ALL to confirm';
  $('confirmInput').value = '';
  $('confirmInput').placeholder = single ? targets[0].dir : 'DELETE ALL';
  $('confirmDelete').disabled = true;
  $('confirmDelete').textContent = single ? 'Delete workspace' : `Delete ${targets.length} workspaces`;
  $('confirmDialog').showModal();
  $('confirmInput').focus();
}

function confirmMatches() {
  const expected = state.targets.length === 1 ? state.targets[0].dir : 'DELETE ALL';
  return $('confirmInput').value.trim() === expected;
}

async function runDelete() {
  if (!state.targets.length || !confirmMatches()) return;
  const items = state.targets.map((ws) => ({ dir: ws.dir, confirm: ws.dir }));
  state.busy = true;
  $('confirmDelete').disabled = true;
  $('confirmDelete').textContent = 'Deleting…';
  try {
    const response = await fetch('/api/workspaces/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-WebUI': '1' },
      body: JSON.stringify({ items }),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok && !result.deleted) {
      toast('Delete failed', result.error || `HTTP ${response.status}`, 'error');
      return;
    }
    const deleted = result.deleted || [];
    const skipped = result.skipped || [];
    const failed = result.failed || [];
    const freed = deleted.reduce((sum, item) => sum + (item.size_bytes || 0), 0);
    if (deleted.length) {
      const leftovers = deleted.flatMap((item) => item.leftovers || []);
      toast(
        deleted.length === 1 ? 'Workspace deleted' : `${deleted.length} workspaces deleted`,
        `${humanBytes(freed)} freed${leftovers.length ? ` · ${leftovers.length} root-owned file(s) left in .webui-trash` : ''}`,
      );
      state.selected.clear();
      state.expanded.clear();
    }
    for (const item of skipped) {
      toast(`Skipped ${item.dir}`, [item.error, ...(item.reasons || [])].filter(Boolean).join(' · '), 'error');
    }
    for (const item of failed) {
      toast(`Failed ${item.dir || 'request'}`, item.error, 'error');
    }
    $('confirmDialog').close();
    await load();
  } catch (error) {
    toast('Delete failed', String(error.message || error), 'error');
  } finally {
    state.busy = false;
    $('confirmDelete').disabled = false;
  }
}

/* --- wiring -------------------------------------------------------------------------------- */

document.addEventListener('click', (event) => {
  const target = event.target.closest('button');
  if (!target) return;
  if (target.dataset.expand) {
    toggleExpand(target.dataset.expand);
  } else if (target.dataset.delete) {
    openConfirm([target.dataset.delete]);
  }
});

document.addEventListener('change', (event) => {
  const checkbox = event.target;
  if (checkbox.dataset && checkbox.dataset.check !== undefined) {
    toggleSelection(checkbox.dataset.check, checkbox.checked);
  }
});

$('search').addEventListener('input', (event) => {
  state.query = event.target.value;
  renderRows(state.data ? state.data.workspaces : []);
});

document.querySelectorAll('.segmented button').forEach((button) => {
  button.addEventListener('click', () => {
    state.filter = button.dataset.filter;
    document.querySelectorAll('.segmented button').forEach((other) => other.classList.toggle('active', other === button));
    renderRows(state.data ? state.data.workspaces : []);
  });
});

$('selectIdle').addEventListener('change', (event) => {
  const deletable = (state.data?.workspaces || []).filter((ws) => ws.deletable);
  if (event.target.checked) deletable.forEach((ws) => state.selected.add(ws.dir));
  else state.selected.clear();
  renderRows(state.data ? state.data.workspaces : []);
  renderSelection();
});

$('deleteSelected').addEventListener('click', () => openConfirm([...state.selected]));

$('refresh').addEventListener('click', () => load());

$('confirmInput').addEventListener('input', () => {
  $('confirmDelete').disabled = !confirmMatches();
});

$('confirmDialog').addEventListener('close', () => {
  state.targets = [];
  $('confirmDelete').disabled = true;
});

$('confirmDialog').addEventListener('submit', (event) => {
  const submitter = event.submitter;
  if (!submitter || submitter.value !== 'confirm') return; // Cancel: dialog closes itself
  event.preventDefault();
  if (confirmMatches()) runDelete();
});

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') load();
});

load();
