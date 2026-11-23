# shellcheck shell=bash
# Per-task sandbox (bubblewrap) for the agent CLIs the daemon starts. AGENT_SANDBOX:
#   off (default)  the daemon starts the CLIs directly
#   on             MULTICA_<CLI>_PATH -> sandbox shims; if bwrap cannot create namespaces here, log it
#                  and run unsandboxed
#   required       as on, but the daemon does not start without a working sandbox
# A MULTICA_<CLI>_PATH that is already set is kept (that CLI is then not sandboxed).
SANDBOX_SHIMS=/usr/local/lib/multica-agent-runtime/sandbox-bin

# the namespaces agent-sandbox needs: user (implicit), pid, ipc, uts, a fresh /proc
sandbox_preflight() {
  bwrap --ro-bind / / --unshare-pid --unshare-ipc --unshare-uts --proc /proc --dev /dev true 2>&1
}

setup_sandbox() {
  local mode=${AGENT_SANDBOX:-off} out cli var active=()
  case "$mode" in
    off | 0 | false | no) return 0 ;;
    on | 1 | true | yes) mode=on ;;
    required) ;;
    *)
      warn "AGENT_SANDBOX=$mode unknown (off, on, required): sandbox off"
      return 0
      ;;
  esac
  if ! out=$(sandbox_preflight); then
    if [ "$mode" = required ]; then
      log "sandbox: bwrap cannot create namespaces here: $out"
      log "AGENT_SANDBOX=required: not starting the daemon"
      exit 1
    fi
    warn "sandbox: bwrap cannot create namespaces here ($out): agents run unsandboxed"
    agent_env AGENT_SANDBOX_ACTIVE none
    return 0
  fi
  for cli in claude codex pi omp; do
    command -v "$cli" >/dev/null || continue
    var=MULTICA_${cli^^}_PATH
    if is_set "$var"; then
      warn "sandbox: $var is set, $cli runs unsandboxed"
      continue
    fi
    export "$var=$SANDBOX_SHIMS/$cli"
    active+=("$cli")
  done
  # The daemon's warm session pool (Multica >= v0.12) keeps Claude/Codex running between turns and hands
  # them MULTICA_TOKEN/MULTICA_TASK_ID through a file below the daemon's /tmp (MULTICA_TASK_ENV_FILE)
  # instead of the environment: agent-sandbox then sees no task and the file is not inside the sandbox
  # ("requires MULTICA_TOKEN to be a task-scoped mat_ token"). Upstream turns the pool off only for
  # custom profile commands, not for MULTICA_<CLI>_PATH shims, so turn it off here.
  if [ ${#active[@]} -gt 0 ]; then
    case "${MULTICA_WARM_POOL:-}" in
      "") export MULTICA_WARM_POOL=off ;;
      0 | false | off | no) ;;
      *) warn "sandbox: MULTICA_WARM_POOL=$MULTICA_WARM_POOL: warm Claude/Codex sessions get no task token in the sandbox" ;;
    esac
  fi
  mkdir -p "${AGENT_SANDBOX_HOMES:-/data/agent-homes}"
  # runtime-doctor in the shell container (masked /proc, no bwrap) reads this from agent.env
  agent_env AGENT_SANDBOX_PREFLIGHT ok
  agent_env AGENT_SANDBOX_ACTIVE "${active[*]:-none}"
  log "sandbox: ${active[*]:-no CLI} in bubblewrap per task (homes ${AGENT_SANDBOX_HOMES:-/data/agent-homes}, pass-through: ${AGENT_SANDBOX_PASS_ENV:-none})"
}
