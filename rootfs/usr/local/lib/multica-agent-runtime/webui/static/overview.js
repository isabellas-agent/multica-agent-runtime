/* Overview: resource usage, agent tasks, daemon, storage, Docker, top processes */
'use strict';

(() => {
  const state = { timer: null, system: null, agents: null, loading: false, sparks: new Map() };
  const POLL_MS = 3000;

  /* --- sparkline: one series, 2px line, soft area, crosshair + tooltip on hover ------------ */

  function sparkline(node, points, key, options) {
    const W = 240;
    const H = 40;
    const values = points.map((p) => (p[key] == null ? null : Number(p[key])));
    const present = values.filter((v) => v != null);
    if (present.length < 2) {
      node.innerHTML = '<span class="spark-empty">collecting…</span>';
      state.sparks.delete(node);
      return;
    }
    const max = Math.max(options.max || 0, ...present) || 1;
    const step = W / Math.max(1, values.length - 1);
    const y = (v) => H - 2 - (v / max) * (H - 6);
    let line = '';
    values.forEach((v, i) => {
      if (v == null) return;
      line += `${line ? 'L' : 'M'}${(i * step).toFixed(1)},${y(v).toFixed(1)}`;
    });
    const firstIndex = values.findIndex((v) => v != null);
    const area = `${line}L${((values.length - 1) * step).toFixed(1)},${H}L${(firstIndex * step).toFixed(1)},${H}Z`;
    node.innerHTML = `
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">
        <path class="spark-area" d="${area}" />
        <path class="spark-line" d="${line}" />
        <line class="spark-cross" x1="0" x2="0" y1="0" y2="${H}" visibility="hidden" />
      </svg>
      <span class="spark-dot" hidden></span>`;
    state.sparks.set(node, { points, values, key, options, max, step, W, H, y });
  }

  function sparkHover(event) {
    const node = event.currentTarget;
    const spark = state.sparks.get(node);
    if (!spark) return;
    const rect = node.getBoundingClientRect();
    const ratio = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
    const index = Math.round(ratio * (spark.values.length - 1));
    const value = spark.values[index];
    if (value == null) return;
    const cross = node.querySelector('.spark-cross');
    const x = index * spark.step;
    cross.setAttribute('x1', x);
    cross.setAttribute('x2', x);
    cross.setAttribute('visibility', 'visible');
    const dot = node.querySelector('.spark-dot');
    dot.hidden = false;
    dot.style.left = `${(x / spark.W) * 100}%`;
    dot.style.top = `${(spark.y(value) / spark.H) * 100}%`;
    const when = new Date(spark.points[index].t * 1000).toLocaleTimeString();
    showTooltip(`<strong>${escapeHtml(spark.options.format(value))}</strong><span>${escapeHtml(spark.options.label)} · ${escapeHtml(when)}</span>`, event.clientX, event.clientY);
  }

  function sparkLeave(event) {
    const node = event.currentTarget;
    const cross = node.querySelector('.spark-cross');
    if (cross) cross.setAttribute('visibility', 'hidden');
    const dot = node.querySelector('.spark-dot');
    if (dot) dot.hidden = true;
    hideTooltip();
  }

  document.querySelectorAll('#view-overview .spark').forEach((node) => {
    node.addEventListener('pointermove', sparkHover);
    node.addEventListener('pointerleave', sparkLeave);
  });

  /* --- meters ------------------------------------------------------------------------------ */

  function severity(percent) {
    if (percent >= 90) return 'crit';
    if (percent >= 75) return 'warn';
    return '';
  }

  function meter(label, used, total, detail) {
    const percent = total ? Math.min(100, (100 * used) / total) : 0;
    const level = severity(percent);
    const icon = level === 'crit' ? '<span class="sev crit">critical</span>' : level === 'warn' ? '<span class="sev warn">high</span>' : '';
    return `
      <div class="meter ${level}">
        <div class="meter-head"><span class="meter-label">${escapeHtml(label)}</span>${icon}<span class="meter-value">${percent.toFixed(0)}%</span></div>
        <div class="meter-track"><div class="meter-fill" style="width:${percent.toFixed(1)}%"></div></div>
        <div class="meter-detail">${escapeHtml(detail)}</div>
      </div>`;
  }

  /* --- rendering --------------------------------------------------------------------------- */

  function setKpi(id, value, hint) {
    const node = $(id);
    node.querySelector('.value').textContent = value;
    node.querySelector('.hint').textContent = hint;
  }

  function renderSystem(sys) {
    const host = sys.host || {};
    const cg = sys.cgroup || {};
    const procs = sys.processes || {};
    const net = sys.network || {};
    const history = sys.history || [];
    const cpus = host.cpus_usable || host.cpus || 1;

    setKpi(
      'kpiCpu',
      host.cpu_percent == null ? '–' : `${host.cpu_percent.toFixed(0)}%`,
      `${host.cpus || '?'} CPUs on the node · load ${(host.load || []).map((v) => v.toFixed(2)).join(' ') || '–'}`,
    );
    setKpi(
      'kpiMem',
      host.mem_used == null ? '–' : humanBytes(host.mem_used),
      host.mem_total ? `of ${humanBytes(host.mem_total)} on the node` : 'node memory',
    );
    setKpi(
      'kpiProcs',
      procs.cpu_cores == null ? '–' : `${procs.cpu_cores.toFixed(2)} cores`,
      `${procs.count || 0} processes · ${humanBytes(procs.rss || 0)} RSS`,
    );
    setKpi('kpiNet', humanRate(net.rx_rate), `out ${humanRate(net.tx_rate)} · ${humanBytes(net.rx_total || 0)} received`);

    sparkline($('kpiCpu').querySelector('.spark'), history, 'host_cpu', { max: 100, label: 'node CPU', format: (v) => `${v.toFixed(1)}%` });
    sparkline($('kpiMem').querySelector('.spark'), history, 'mem_used', { max: host.mem_total, label: 'node memory used', format: humanBytes });
    sparkline($('kpiProcs').querySelector('.spark'), history, 'procs_cores', { max: Math.min(cpus, 1), label: 'CPU of visible processes', format: (v) => `${v.toFixed(2)} cores` });
    sparkline($('kpiNet').querySelector('.spark'), history, 'rx', { label: 'received', format: humanRate });

    $('disks').innerHTML = (sys.filesystems || []).length
      ? sys.filesystems
          .map((fs) => meter(fs.path, fs.used, fs.used + fs.free, `${humanBytes(fs.used)} used · ${humanBytes(fs.free)} free of ${humanBytes(fs.total)}`))
          .join('')
      : '<p class="muted">No filesystem information.</p>';

    const limits = [];
    if (cg.available) {
      limits.push(['CPU limit', cg.cpu_limit_cores ? `${cg.cpu_limit_cores.toFixed(2)} cores` : 'none']);
      limits.push(['CPU used', cg.cpu_cores == null ? '–' : `${cg.cpu_cores.toFixed(2)} cores`]);
      limits.push(['Memory', cg.memory_current == null ? '–' : `${humanBytes(cg.memory_current)}${cg.memory_max ? ` of ${humanBytes(cg.memory_max)}` : ' (no limit)'}`]);
      if (cg.pids_current != null) limits.push(['Tasks (pids)', String(cg.pids_current)]);
    } else {
      limits.push(['cgroup', 'not available (cgroup v1?)']);
    }
    limits.push(['Node uptime', duration(host.uptime_seconds)]);
    if (host.swap_total) limits.push(['Swap', `${humanBytes(host.swap_used)} of ${humanBytes(host.swap_total)}`]);
    $('limits').innerHTML = limits.map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd>`).join('');
    $('gpus').innerHTML = (sys.gpus || [])
      .map((gpu) =>
        meter(
          `GPU ${gpu.index} · ${gpu.name}`,
          gpu.memory_used,
          gpu.memory_total,
          `${gpu.utilization == null ? '–' : gpu.utilization.toFixed(0)}% busy · ${humanBytes(gpu.memory_used)} of ${humanBytes(gpu.memory_total)} VRAM${gpu.temperature != null ? ` · ${gpu.temperature.toFixed(0)} °C` : ''}`,
        ),
      )
      .join('');

    $('procHint').textContent = `by CPU · ${procs.count || 0} visible processes`;
    $('procRows').innerHTML = (sys.top || [])
      .map(
        (p) => `
        <tr>
          <td class="num mono">${p.pid}</td>
          <td class="proc-cell"><span class="proc-name">${escapeHtml(p.name)}</span><span class="proc-cmd" title="${escapeHtml(p.cmd)}">${escapeHtml(p.cmd)}</span></td>
          <td class="num">${p.cpu.toFixed(1)}%</td>
          <td class="num">${escapeHtml(humanBytes(p.rss))}</td>
          <td class="num">${p.threads}</td>
          <td>${escapeHtml(duration(p.running_seconds))}</td>
        </tr>`,
      )
      .join('');
  }

  function renderAgents(data) {
    renderDaemonPill(data.daemon);
    renderProcessView(data.process_view);
    const daemon = data.daemon || {};
    const pill = $('daemonPill');
    pill.className = `pill ${daemon.reachable ? (daemon.running_task_count > 0 ? 'busy' : 'ok') : 'locked'}`;
    pill.textContent = daemon.reachable ? (daemon.status || 'running') : 'offline';

    const info = [];
    if (daemon.reachable) {
      info.push(['Daemon id', daemon.daemon_id || '–']);
      info.push(['Version', daemon.cli_version || '–']);
      info.push(['Uptime', daemon.uptime || '–']);
      info.push(['Tasks', `${daemon.running_task_count ?? 0} running · ${daemon.active_task_count ?? 0} active${daemon.resource_wait_task_count ? ` · ${daemon.resource_wait_task_count} waiting` : ''}`]);
      if ((daemon.agents || []).length) info.push(['Agent CLIs', daemon.agents.join(', ')]);
      info.push(['Workspaces', String((daemon.workspaces || []).length)]);
      if (daemon.server_url) info.push(['Server', daemon.server_url]);
    } else {
      info.push(['Status', 'not reachable']);
      info.push(['URL', daemon.url || '–']);
      if (daemon.error) info.push(['Error', daemon.error]);
    }
    const proc = data.daemon_process;
    if (proc) info.push(['Process', `pid ${proc.pid} · ${proc.cpu}% CPU · ${humanBytes(proc.rss)}`]);
    $('daemonInfo').innerHTML = info.map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(v)}</dd>`).join('');

    const tasks = data.tasks || [];
    $('agentEmpty').hidden = tasks.length > 0;
    $('agentEmpty').querySelector('p').textContent =
      data.process_view && data.process_view.mode === 'blind'
        ? 'No agent process is visible from this container (see the banner above).'
        : 'No agent task is running.';
    $('agentsHint').textContent = `${tasks.length} running · CPU and memory summed over each process tree`;
    const shell = Boolean(UI.config.shell);
    $('agentRows').innerHTML = tasks
      .map((task) => {
        const where = task.task_dir
          ? `<span class="ws-name mono">${escapeHtml(task.task_dir)}</span><span class="ws-dir">${escapeHtml(task.workspace_dir)}${task.issue_id ? ` · issue ${escapeHtml(shortId(task.issue_id))}` : ''}</span>`
          : `<span class="ws-dir">${escapeHtml(task.cwd || 'unknown directory')}</span>`;
        const open = shell && task.task_dir
          ? `<button class="btn small ghost" data-shell-dir="${escapeHtml(task.workspace_dir)}" data-shell-task="${escapeHtml(task.task_dir)}">Shell</button>`
          : '';
        return `
          <tr>
            <td><span class="agent-cli">${escapeHtml(task.cli)}</span>${task.model ? `<span class="ws-dir">${escapeHtml(task.model)}</span>` : ''}</td>
            <td class="c-name"><span class="ws-cell">${where}</span></td>
            <td class="num">${task.cpu.toFixed(1)}%</td>
            <td class="num">${escapeHtml(humanBytes(task.rss))}</td>
            <td class="num" title="${escapeHtml(task.children.join(', '))}">${task.process_count}</td>
            <td>${escapeHtml(duration(task.running_seconds))}</td>
            <td class="actions">${open}</td>
          </tr>`;
      })
      .join('');

    const docker = data.docker || {};
    $('dockerHint').textContent = docker.available ? `${(docker.containers || []).length} containers` : 'not available';
    $('dockerList').innerHTML = docker.available
      ? (docker.containers || []).length
        ? docker.containers
            .slice(0, 12)
            .map(
              (c) => `<li><span class="pill ${c.state === 'running' ? 'ok' : 'locked'}">${escapeHtml(c.state || '?')}</span><span class="li-main"><span class="mono">${escapeHtml(c.name)}</span><span class="ws-dir">${escapeHtml(c.image)} · ${escapeHtml(c.status)}</span></span></li>`,
            )
            .join('')
        : '<li class="muted">No containers.</li>'
      : `<li class="muted">${escapeHtml(docker.error || 'Docker CLI or socket not available here.')}</li>`;
  }

  async function load() {
    if (state.loading) return;
    state.loading = true;
    try {
      const [system, agents] = await Promise.all([api('/api/system'), api('/api/agents')]);
      state.system = system;
      state.agents = agents;
      renderSystem(system);
      renderAgents(agents);
      const count = (agents.terminals || []).length;
      $('termCount').hidden = count === 0;
      $('termCount').textContent = String(count);
    } catch (error) {
      toast('Could not load the overview', String(error.message || error), 'error');
    } finally {
      state.loading = false;
    }
  }

  function schedule() {
    clearTimeout(state.timer);
    state.timer = setTimeout(async () => {
      if (UI.current !== 'overview') return;
      if (document.visibilityState === 'visible') await load();
      schedule();
    }, POLL_MS);
  }

  document.addEventListener('click', (event) => {
    const button = event.target.closest('button[data-shell-task]');
    if (!button) return;
    openTerminal({ dir: button.dataset.shellDir, task: button.dataset.shellTask });
  });

  registerView('overview', {
    show() {
      load().then(schedule);
    },
    hide() {
      clearTimeout(state.timer);
    },
    refresh() {
      load();
    },
  });
})();
