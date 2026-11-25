# shellcheck shell=python
"""Discovery, in-use detection and deletion of Multica workspace data directories.

The Multica daemon keeps the data of every workspace under one root
(`MULTICA_WORKSPACES_ROOT`, default `/data/workspaces`):

    /data/workspaces/<slug>-<last 12 hex of the workspace id>/   one directory per workspace
      home-<issue>-<token>/     one directory per task (run)
      .repos/                   git repo cache, shared by all workspaces

Deleting such a directory is safe while no task of that workspace runs: the daemon recreates it
on the next task. Everything in this module is aimed at one rule:

    never delete a workspace that is (or looks) in use.

Three independent signals are collected for that (see `_detect_in_use`):

1. a live process of this host has its working directory (or an open file) inside the workspace —
   this is what a running agent task looks like;
2. the workspace was modified within `WEBUI_ACTIVITY_GRACE` seconds (the safety net for a webui
   that runs on another host or in another PID namespace than the daemon);
3. the daemon reports running tasks and the workspace holds a task directory whose `.task_lock`
   is flock()ed at this moment (only when the lock is visible in `/proc/locks`).

Nothing here needs the Multica server. Names come from the optional `multica workspace list`.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

# --- configuration -----------------------------------------------------------------------------


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name, default)
    return "" if value == "CHANGEME" else value


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


DEFAULT_ROOT = "/data/workspaces"
INFRA_DIRS = {".repos", ".webui-trash"}

# root directory of the workspace data (WEBUI_WORKSPACES_ROOT wins over the daemon's variable)
def workspaces_root() -> str:
    root = _env("WEBUI_WORKSPACES_ROOT") or _env("MULTICA_WORKSPACES_ROOT") or DEFAULT_ROOT
    return os.path.abspath(root)


def config() -> dict[str, Any]:
    return {
        "root": workspaces_root(),
        "activity_grace_seconds": _env_int("WEBUI_ACTIVITY_GRACE", 300),
        "scan_budget_seconds": _env_int("WEBUI_SCAN_BUDGET", 120),
        "cache_seconds": _env_int("WEBUI_CACHE_SECONDS", 8),
        "trash": _env_bool("WEBUI_TRASH", True),
        "keep_trash_hours": _env_float("WEBUI_KEEP_TRASH_HOURS", 0.0),
        "protect": sorted(protected_workspaces()),
        "daemon_url": daemon_url(),
        "resolve_names": _env_bool("WEBUI_RESOLVE_NAMES", True),
        "filesystem_id": _filesystem_id(),
    }


def protected_workspaces() -> set[str]:
    raw = _env("WEBUI_PROTECT_WORKSPACES")
    return {part.strip() for part in raw.split(",") if part.strip()}


def daemon_url() -> str:
    url = _env("WEBUI_DAEMON_URL")
    if url:
        return url.rstrip("/")
    port = _env("MULTICA_DAEMON_PORT") or "19514"
    return f"http://127.0.0.1:{port}"


def trash_dir(root: str) -> str:
    return os.path.join(root, ".webui-trash")


def _filesystem_id() -> str:
    """Filesystem id of the root (major:minor), so /proc/locks inodes can be matched."""
    try:
        st = os.stat(workspaces_root())
    except OSError:
        return ""
    return f"{os.major(st.st_dev)}:{os.minor(st.st_dev)}"


# --- helpers -----------------------------------------------------------------------------------

_BYTES_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def human_bytes(num: float) -> str:
    value = float(num)
    for unit in _BYTES_UNITS:
        if abs(value) < 1024 or unit == _BYTES_UNITS[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PiB"


@dataclass
class TaskDir:
    name: str
    path: str
    size_bytes: int = 0
    newest_mtime: float = 0.0
    locked: bool = False
    processes: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "size_bytes": self.size_bytes,
            "newest_mtime": self.newest_mtime,
            "locked": self.locked,
            "processes": self.processes,
        }


@dataclass
class Workspace:
    dir_name: str
    path: str
    workspace_id: str = ""
    slug: str = ""
    name: str = ""
    size_bytes: int = 0
    newest_mtime: float = 0.0
    oldest_mtime: float = 0.0
    partial: bool = False
    tasks: list[TaskDir] = field(default_factory=list)
    processes: list[dict[str, Any]] = field(default_factory=list)
    in_use: bool = False
    reasons: list[str] = field(default_factory=list)
    protected: bool = False

    @property
    def deletable(self) -> bool:
        return not self.in_use and not self.protected

    def as_dict(self) -> dict[str, Any]:
        age = max(0.0, time.time() - self.newest_mtime) if self.newest_mtime else None
        return {
            "dir": self.dir_name,
            "path": self.path,
            "workspace_id": self.workspace_id,
            "id_short": self.workspace_id[:8] if self.workspace_id else "",
            "slug": self.slug,
            "name": self.name or self.slug or self.dir_name,
            "size_bytes": self.size_bytes,
            "size": human_bytes(self.size_bytes),
            "task_count": len(self.tasks),
            "tasks": [task.as_dict() for task in self.tasks],
            "newest_mtime": self.newest_mtime,
            "last_activity_seconds": age,
            "partial": self.partial,
            "processes": self.processes,
            "in_use": self.in_use,
            "protected": self.protected,
            "deletable": self.deletable,
            "reasons": self.reasons,
        }


# --- scanning ----------------------------------------------------------------------------------


def _walk_size(path: str, deadline: float) -> tuple[int, float, bool]:
    """Size and newest mtime below `path`; symlinks are never followed."""
    size = 0
    newest = 0.0
    partial = False
    stack = [path]
    while stack:
        current = stack.pop()
        if time.monotonic() > deadline:
            partial = True
            break
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
                            size += st.st_size
                            if st.st_mtime > newest:
                                newest = st.st_mtime
                    except OSError:
                        continue
        except OSError:
            partial = partial or current != path
    try:
        st = os.stat(path, follow_symlinks=False)
        newest = max(newest, st.st_mtime)
    except OSError:
        pass
    return size, newest, partial


def _read_json(path: str) -> dict[str, Any]:
    try:
        with open(path, "rb") as handle:
            data = json.loads(handle.read(1 << 20).decode("utf-8", "replace"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _skip_process_pids() -> set[int]:
    """The webui itself and its ancestors (a webui started from a workspace shell)."""
    pids = {0}
    pid = os.getpid()
    for _ in range(32):
        pids.add(pid)
        try:
            with open(f"/proc/{pid}/stat", "rb") as handle:
                fields = handle.read().rsplit(b")", 1)[1].split()
            pid = int(fields[1])
        except (OSError, IndexError, ValueError):
            break
        if pid <= 1:
            pids.add(pid)
            break
    return pids


def _proc_cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            raw = handle.read(512)
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()


def _proc_name(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/comm", "rb") as handle:
            return handle.read(64).decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _scan_processes(roots: Iterable[str]) -> list[dict[str, Any]]:
    """Processes whose working directory or open files lie inside one of `roots`."""
    roots = [os.path.realpath(root) for root in roots if root]
    if not roots:
        return []
    skip = _skip_process_pids()
    found: list[dict[str, Any]] = []
    try:
        proc_entries = os.listdir("/proc")
    except OSError:
        return []
    for entry in proc_entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid in skip:
            continue
        paths: list[str] = []
        try:
            paths.append(os.readlink(f"/proc/{pid}/cwd"))
        except OSError:
            pass
        try:
            fd_dir = f"/proc/{pid}/fd"
            for fd in os.listdir(fd_dir)[:256]:
                try:
                    target = os.readlink(f"{fd_dir}/{fd}")
                except OSError:
                    continue
                if target.startswith("/"):
                    paths.append(target)
        except OSError:
            pass
        match = next(
            (path for path in paths if any(path == root or path.startswith(root + os.sep) for root in roots)),
            None,
        )
        if match:
            found.append(
                {
                    "pid": pid,
                    "name": _proc_name(pid),
                    "cmd": _proc_cmdline(pid)[:200],
                    "path": match,
                }
            )
    return found


def _locked_inodes() -> set[tuple[str, int]]:
    """Advisory write locks currently held, as (major:minor, inode) from /proc/locks."""
    locked: set[tuple[str, int]] = set()
    try:
        with open("/proc/locks", "rb") as handle:
            lines = handle.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return locked
    for line in lines:
        parts = line.split()
        if len(parts) < 6 or parts[1] != "ADVISORY" or parts[2] != "WRITE":
            continue
        try:
            locked.add((parts[4], int(parts[5])))
        except (IndexError, ValueError):
            continue
    return locked


def _workspace_id_from_dir(dir_name: str, ids_by_suffix: dict[str, str]) -> str:
    suffix = dir_name.rsplit("-", 1)[-1] if "-" in dir_name else ""
    return ids_by_suffix.get(suffix.lower(), "")


def _workspace_names() -> dict[str, dict[str, str]]:
    """id -> {name, slug} from the Multica server, empty when unavailable (offline, no login)."""
    cache = _names_cache.get("data")
    if cache is not None and time.monotonic() - cache["at"] < 300:
        return cache["value"]
    value: dict[str, dict[str, str]] = {}
    if _env_bool("WEBUI_RESOLVE_NAMES", True):
        try:
            proc = subprocess.run(
                ["multica", "workspace", "list", "--output", "json"],
                capture_output=True,
                timeout=_env_int("WEBUI_NAMES_TIMEOUT", 15),
                check=False,
            )
            if proc.returncode == 0:
                for item in json.loads(proc.stdout.decode("utf-8", "replace")):
                    if isinstance(item, dict) and item.get("id"):
                        value[str(item["id"])] = {
                            "name": str(item.get("name") or ""),
                            "slug": str(item.get("slug") or ""),
                        }
        except (OSError, ValueError, subprocess.SubprocessError):
            value = {}
    _names_cache["data"] = {"at": time.monotonic(), "value": value}
    return value


_names_cache: dict[str, Any] = {}

_daemon_cache: dict[str, Any] = {}


def daemon_status(timeout: float = 2.0) -> dict[str, Any]:
    """The daemon's local /health endpoint, when it is reachable from here."""
    url = daemon_url()
    cached = _daemon_cache.get("data")
    if cached is not None and time.monotonic() - cached["at"] < 3:
        return cached["value"]
    status: dict[str, Any] = {"reachable": False, "url": url}
    if url:
        try:
            import urllib.request

            with urllib.request.urlopen(f"{url}/health", timeout=timeout) as response:
                payload = json.loads(response.read(1 << 20).decode("utf-8", "replace"))
            if isinstance(payload, dict):
                status.update(
                    {
                        "reachable": True,
                        "status": payload.get("status"),
                        "daemon_id": payload.get("daemon_id"),
                        "device_name": payload.get("device_name"),
                        "uptime": payload.get("uptime"),
                        "cli_version": payload.get("cli_version"),
                        "running_task_count": payload.get("running_task_count"),
                        "active_task_count": payload.get("active_task_count"),
                        "workspaces": payload.get("workspaces") or [],
                    }
                )
        except Exception as exc:  # noqa: BLE001 - any failure just means "not reachable"
            status["error"] = str(exc)[:200]
    _daemon_cache["data"] = {"at": time.monotonic(), "value": status}
    return status


def _detect_in_use(
    workspace: Workspace, programs: list[dict[str, Any]], locked: set[tuple[str, int]], cfg: dict[str, Any]
) -> None:
    reasons: list[str] = []
    prefix = workspace.path + os.sep
    procs = [p for p in programs if p["path"] == workspace.path or str(p["path"]).startswith(prefix)]
    if procs:
        workspace.processes = procs
        names = sorted({p["name"] or str(p["pid"]) for p in procs})
        reasons.append(f"running process ({', '.join(names[:4])})")

    for task in workspace.tasks:
        try:
            st = os.stat(os.path.join(task.path, ".task_lock"))
            inode = (f"{os.major(st.st_dev)}:{os.minor(st.st_dev)}", st.st_ino)
        except OSError:
            inode = None
        if inode and inode in locked:
            task.locked = True
            reasons.append(f"task lock held ({task.name})")

    grace = int(cfg["activity_grace_seconds"])
    if grace > 0 and workspace.newest_mtime:
        age = time.time() - workspace.newest_mtime
        if age < grace:
            reasons.append(f"modified {max(0, int(age))}s ago (grace {grace}s)")

    workspace.reasons = reasons
    workspace.in_use = bool(reasons)


def _names_index() -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    names = _workspace_names()
    ids_by_suffix: dict[str, str] = {}
    for workspace_id in names:
        ids_by_suffix[workspace_id.replace("-", "").lower()[-12:]] = workspace_id
    return names, ids_by_suffix


def _scan_workspace(
    entry: str,
    cfg: dict[str, Any],
    deadline: float,
    names: dict[str, dict[str, str]],
    ids_by_suffix: dict[str, str],
) -> Workspace | None:
    """Size, tasks and identity of one workspace directory (no in-use detection)."""
    path = os.path.join(cfg["root"], entry)
    try:
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            return None
    except OSError:
        return None

    workspace = Workspace(dir_name=entry, path=path)
    workspace.workspace_id = _workspace_id_from_dir(entry, ids_by_suffix)
    if not workspace.workspace_id:
        for task_name in sorted(os.listdir(path)):
            if task_name.startswith(".") or task_name in INFRA_DIRS:
                continue
            owner = _read_json(os.path.join(path, task_name, ".task_owner"))
            if owner.get("workspace_id"):
                workspace.workspace_id = str(owner["workspace_id"])
                break
    meta = names.get(workspace.workspace_id, {})
    workspace.name = meta.get("name", "")
    workspace.slug = meta.get("slug", "") or (entry.rsplit("-", 1)[0] if "-" in entry else entry)

    oldest: list[float] = []
    try:
        children = sorted(os.listdir(path))
    except OSError:
        children = []
    for child in children:
        child_path = os.path.join(path, child)
        try:
            is_dir = stat.S_ISDIR(os.lstat(child_path).st_mode)
        except OSError:
            continue
        if child.startswith(".") or child in INFRA_DIRS or not is_dir:
            size, newest, partial = _walk_size(child_path, deadline)
            workspace.size_bytes += size
            workspace.newest_mtime = max(workspace.newest_mtime, newest)
            workspace.partial = workspace.partial or partial
            continue
        task = TaskDir(name=child, path=child_path)
        task.size_bytes, task.newest_mtime, partial = _walk_size(child_path, deadline)
        workspace.partial = workspace.partial or partial
        workspace.size_bytes += task.size_bytes
        workspace.newest_mtime = max(workspace.newest_mtime, task.newest_mtime)
        try:
            oldest.append(os.stat(child_path).st_mtime)
        except OSError:
            pass
        workspace.tasks.append(task)
    workspace.oldest_mtime = min(oldest) if oldest else workspace.newest_mtime

    identifiers = {
        entry,
        workspace.workspace_id,
        workspace.slug,
        f"{workspace.slug}-{(workspace.workspace_id or '').replace('-', '')[-12:]}",
    }
    workspace.protected = bool(identifiers & set(cfg["protect"]))
    return workspace


def snapshot(force: bool = False) -> dict[str, Any]:
    """All workspaces with size, activity and in-use state (cached for `WEBUI_CACHE_SECONDS`)."""
    cfg = config()
    cached = _cache.get("data")
    if not force and cached is not None and time.monotonic() - cached["at"] < cfg["cache_seconds"]:
        return cached["value"]

    root = cfg["root"]
    started = time.monotonic()
    deadline = started + max(5, int(cfg["scan_budget_seconds"]))
    names, ids_by_suffix = _names_index()

    try:
        entries = sorted(os.listdir(root))
    except OSError:
        entries = []

    workspaces: list[Workspace] = []
    for entry in entries:
        if entry.startswith(".") or entry in INFRA_DIRS:
            continue
        workspace = _scan_workspace(entry, cfg, deadline, names, ids_by_suffix)
        if workspace is not None:
            workspaces.append(workspace)

    programs = _scan_processes([workspace.path for workspace in workspaces])
    locked = _locked_inodes()
    for workspace in workspaces:
        _detect_in_use(workspace, programs, locked, cfg)

    workspaces.sort(key=lambda item: item.size_bytes, reverse=True)
    daemon = daemon_status()
    deletable = [workspace for workspace in workspaces if workspace.deletable]
    payload = {
        "root": root,
        "generated_at": time.time(),
        "scan_seconds": round(time.monotonic() - started, 3),
        "grace_seconds": cfg["activity_grace_seconds"],
        "protect": sorted(cfg["protect"]),
        "daemon": daemon,
        "workspaces": [workspace.as_dict() for workspace in workspaces],
        "totals": {
            "workspace_count": len(workspaces),
            "task_count": sum(len(workspace.tasks) for workspace in workspaces),
            "size_bytes": sum(workspace.size_bytes for workspace in workspaces),
            "size": human_bytes(sum(workspace.size_bytes for workspace in workspaces)),
            "in_use_count": sum(1 for workspace in workspaces if workspace.in_use),
            "deletable_count": len(deletable),
            "deletable_size_bytes": sum(workspace.size_bytes for workspace in deletable),
            "deletable_size": human_bytes(sum(workspace.size_bytes for workspace in deletable)),
        },
    }
    _cache["data"] = {"at": time.monotonic(), "value": payload}
    return payload


def workspace_state(dir_name: str) -> dict[str, Any] | None:
    """State of a single workspace directory, scanned on its own (used right before deleting)."""
    cfg = config()
    names, ids_by_suffix = _names_index()
    deadline = time.monotonic() + max(5, int(cfg["scan_budget_seconds"]))
    workspace = _scan_workspace(dir_name, cfg, deadline, names, ids_by_suffix)
    if workspace is None:
        return None
    _detect_in_use(workspace, _scan_processes([workspace.path]), _locked_inodes(), cfg)
    return workspace.as_dict()


_cache: dict[str, Any] = {}


def invalidate() -> None:
    _cache.pop("data", None)
    _daemon_cache.pop("data", None)


# --- deletion ----------------------------------------------------------------------------------


class DeleteError(Exception):
    def __init__(self, message: str, status: int = 400, reasons: list[str] | None = None):
        super().__init__(message)
        self.status = status
        self.reasons = reasons or []


def _validate_dir_name(dir_name: str) -> None:
    if not isinstance(dir_name, str) or not dir_name:
        raise DeleteError("workspace directory is required")
    if dir_name in (".", "..") or dir_name.startswith(".") or "/" in dir_name or "\\" in dir_name:
        raise DeleteError("invalid workspace directory")
    root = workspaces_root()
    path = os.path.join(root, dir_name)
    if os.path.realpath(os.path.dirname(path)) != os.path.realpath(root):
        raise DeleteError("workspace directory is outside the workspaces root")
    try:
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            raise DeleteError("not a directory")
    except OSError:
        raise DeleteError("workspace directory does not exist", status=404)


def _rmtree_force(path: str) -> list[str]:
    """Remove a tree, fixing permissions where possible; returns paths that survived.

    Files a nested (rootful) container created as root cannot be removed by the agent user; they
    stay in the trash and are reported instead of failing the whole request.
    """
    surviving: list[str] = []

    def remove(target: str, remove_fn) -> None:  # noqa: ANN001 - callable passed in
        try:
            remove_fn(target)
            return
        except OSError:
            pass
        for mode in (0o700, 0o600):
            try:
                os.chmod(os.path.dirname(target), 0o700)
                os.chmod(target, mode)
                remove_fn(target)
                return
            except OSError:
                continue
        surviving.append(target)

    for current, dirs, files in os.walk(path, topdown=False, onerror=lambda _exc: None):
        for name in files:
            remove(os.path.join(current, name), os.unlink)
        for name in dirs:
            target = os.path.join(current, name)
            if os.path.islink(target):
                remove(target, os.unlink)
            else:
                remove(target, os.rmdir)
    remove(path, os.rmdir)
    return surviving[:20]


def _purge_trash(root: str, keep_hours: float) -> None:
    """Drop trash entries; `keep_hours` 0 removes them right away."""
    path = trash_dir(root)
    try:
        entries = os.listdir(path)
    except OSError:
        return
    now = time.time()
    for entry in entries:
        entry_path = os.path.join(path, entry)
        try:
            age_hours = (now - os.lstat(entry_path).st_mtime) / 3600
        except OSError:
            continue
        if age_hours >= keep_hours:
            try:
                _rmtree_force(entry_path)
            except OSError:
                continue


def delete_workspace(dir_name: str, confirm: str, dry_run: bool = False) -> dict[str, Any]:
    """Delete one workspace's data directory. Raises DeleteError when that is not safe."""
    _validate_dir_name(dir_name)
    if not isinstance(confirm, str) or confirm.strip() != dir_name:
        raise DeleteError("confirmation does not match the workspace directory")

    cfg = config()
    current = workspace_state(dir_name)
    if current is None:
        raise DeleteError("workspace directory does not exist", status=404)
    if current["protected"]:
        raise DeleteError("workspace is protected by WEBUI_PROTECT_WORKSPACES", status=403)
    if current["in_use"]:
        raise DeleteError("workspace is in use", status=409, reasons=current["reasons"])

    root = cfg["root"]
    path = os.path.join(root, dir_name)
    result = {
        "dir": dir_name,
        "size_bytes": current["size_bytes"],
        "size": human_bytes(current["size_bytes"]),
        "dry_run": dry_run,
        "trashed": False,
        "leftovers": [],
    }
    if dry_run:
        return result

    staging = path
    if cfg["trash"]:
        trash = trash_dir(root)
        staging = os.path.join(trash, f"{dir_name}.{int(time.time())}")
        try:
            os.makedirs(trash, mode=0o700, exist_ok=True)
            os.rename(path, staging)  # atomic: the daemon never sees a half-deleted directory
            os.utime(staging, None)
            result["trashed"] = True
        except OSError as exc:
            raise DeleteError(f"could not move the workspace into the trash: {exc.strerror}", status=500)
    result["leftovers"] = _rmtree_force(staging)
    if cfg["trash"]:
        _purge_trash(root, float(cfg["keep_trash_hours"]))
    invalidate()
    return result


_delete_lock = threading.Lock()


def delete_many(items: Any, dry_run: bool = False) -> dict[str, Any]:
    """Delete several workspaces; per-item results, a failure never aborts the others."""
    if not isinstance(items, list) or not items:
        raise DeleteError("no workspaces selected")
    if len(items) > 100:
        raise DeleteError("too many workspaces selected at once")
    deleted: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    # one deletion at a time keeps the scan cache and /proc view consistent
    with _delete_lock:
        for item in items:
            if not isinstance(item, dict):
                failed.append({"dir": "", "error": "invalid request item"})
                continue
            dir_name = str(item.get("dir") or "")
            try:
                result = delete_workspace(dir_name, str(item.get("confirm") or ""), dry_run=dry_run)
                deleted.append(result)
            except DeleteError as exc:
                entry = {"dir": dir_name, "error": str(exc), "status": exc.status, "reasons": exc.reasons}
                (skipped if exc.status in (403, 409) else failed).append(entry)
    return {"deleted": deleted, "failed": failed, "skipped": skipped, "dry_run": dry_run}
