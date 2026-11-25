#!/usr/bin/env python3
# shellcheck shell=python
"""Functional test of the workspace cleanup web UI (standard library only).

    python3 tests/webui.py            # from the repo, or /tests/webui.py inside the image

It builds a throw-away workspaces root, so it never touches real data: an idle workspace, a
protected one and one with a live process in it. Run by tests/smoke.sh.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def webui_dir() -> str:
    candidates = [
        os.environ.get("WEBUI_DIR", ""),
        "/usr/local/lib/multica-agent-runtime/webui",
        os.path.join(HERE, "..", "rootfs", "usr", "local", "lib", "multica-agent-runtime", "webui"),
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(os.path.join(candidate, "server.py")):
            return os.path.abspath(candidate)
    raise SystemExit("webui/server.py not found (set WEBUI_DIR)")


WEBUI = webui_dir()
sys.path.insert(0, WEBUI)

ROOT = tempfile.mkdtemp(prefix="webui-test-")
os.environ.update(
    {
        "WEBUI_WORKSPACES_ROOT": ROOT,
        "WEBUI_ACTIVITY_GRACE": "300",
        "WEBUI_RESOLVE_NAMES": "0",
        "WEBUI_DAEMON_URL": "http://127.0.0.1:9",  # closed port: daemon status stays "offline"
        "WEBUI_CACHE_SECONDS": "0",
        "WEBUI_KEEP_TRASH_HOURS": "0",
        "WEBUI_SCAN_BUDGET": "30",
        "WEBUI_PROTECT_WORKSPACES": "beta-222222222222",
    }
)

import server as webui_server  # noqa: E402
import workspace_store as store  # noqa: E402

OLD = time.time() - 3 * 86400


def age_tree(path: str, mtime: float = OLD) -> None:
    """Give everything below `path` (and the directories) an old mtime."""
    for current, dirs, files in os.walk(path, topdown=False):
        for name in files:
            os.utime(os.path.join(current, name), (mtime, mtime))
        os.utime(current, (mtime, mtime))


def root_dir() -> str:
    """The root the store currently scans (tests switch it per class)."""
    return os.environ["WEBUI_WORKSPACES_ROOT"]


def make_workspace(dir_name: str, task: str = "home-1-0123456789ab", payload: int = 4096) -> str:
    workspace = os.path.join(root_dir(), dir_name)
    workdir = os.path.join(workspace, task, "workdir")
    os.makedirs(os.path.join(workspace, task, "logs"), exist_ok=True)
    os.makedirs(workdir, exist_ok=True)
    with open(os.path.join(workspace, task, ".task_owner"), "w", encoding="utf-8") as handle:
        json.dump({"workspace_id": f"00000000-0000-0000-0000-{dir_name.rsplit('-', 1)[-1]}", "task_id": "task"}, handle)
    with open(os.path.join(workspace, task, ".task_lock"), "w", encoding="utf-8"):
        pass
    with open(os.path.join(workdir, "data.bin"), "wb") as handle:
        handle.write(b"x" * payload)
    age_tree(workspace)
    return workspace


class WorkspaceStoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ["WEBUI_WORKSPACES_ROOT"] = ROOT
        store.invalidate()
        cls.alpha = make_workspace("alpha-111111111111")
        cls.beta = make_workspace("beta-222222222222")
        cls.gamma = make_workspace("gamma-333333333333")
        os.makedirs(os.path.join(ROOT, ".repos", "cache"), exist_ok=True)
        with open(os.path.join(ROOT, ".repos", "cache", "x"), "w", encoding="utf-8") as handle:
            handle.write("repo cache")
        # a live process in gamma: this is what a running task looks like
        cls.proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            cwd=os.path.join(cls.gamma, "home-1-0123456789ab", "workdir"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(50):
            if cls.proc.poll() is None:
                break
            time.sleep(0.1)
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        shutil.rmtree(ROOT, ignore_errors=True)

    def by_dir(self, snapshot: dict) -> dict[str, dict]:
        return {item["dir"]: item for item in snapshot["workspaces"]}

    def test_snapshot_lists_workspaces(self) -> None:
        snapshot = store.snapshot(force=True)
        items = self.by_dir(snapshot)
        self.assertEqual(sorted(items), ["alpha-111111111111", "beta-222222222222", "gamma-333333333333"])
        self.assertNotIn(".repos", items)
        self.assertEqual(snapshot["totals"]["workspace_count"], 3)
        self.assertGreaterEqual(snapshot["totals"]["size_bytes"], 3 * 4096)
        self.assertEqual(items["alpha-111111111111"]["task_count"], 1)
        self.assertEqual(items["alpha-111111111111"]["workspace_id"], "00000000-0000-0000-0000-111111111111")

    def test_in_use_detection(self) -> None:
        items = self.by_dir(store.snapshot(force=True))
        self.assertFalse(items["alpha-111111111111"]["in_use"])
        self.assertTrue(items["alpha-111111111111"]["deletable"])
        self.assertTrue(items["gamma-333333333333"]["in_use"])
        self.assertFalse(items["gamma-333333333333"]["deletable"])
        self.assertTrue(any("process" in reason for reason in items["gamma-333333333333"]["reasons"]))
        self.assertTrue(items["gamma-333333333333"]["processes"])
        self.assertEqual(items["gamma-333333333333"]["processes"][0]["pid"], self.proc.pid)
        self.assertTrue(items["beta-222222222222"]["protected"])
        self.assertFalse(items["beta-222222222222"]["deletable"])

    def test_recent_activity_blocks_deletion(self) -> None:
        path = os.path.join(ROOT, "delta-444444444444", "home-2-aaaaaaaaaaaa", "workdir")
        os.makedirs(path, exist_ok=True)
        try:
            with open(os.path.join(path, "fresh.txt"), "w", encoding="utf-8") as handle:
                handle.write("just written")
            item = self.by_dir(store.snapshot(force=True))["delta-444444444444"]
            self.assertTrue(item["in_use"])
            self.assertTrue(any("seconds ago" in reason or "s ago" in reason for reason in item["reasons"]))
            with self.assertRaises(store.DeleteError) as raised:
                store.delete_workspace("delta-444444444444", "delta-444444444444")
            self.assertEqual(raised.exception.status, 409)
        finally:
            shutil.rmtree(os.path.join(ROOT, "delta-444444444444"), ignore_errors=True)

    def test_refusals(self) -> None:
        cases = [
            ("gamma-333333333333", "gamma-333333333333", 409),  # in use
            ("beta-222222222222", "beta-222222222222", 403),  # protected
            ("alpha-111111111111", "wrong", 400),  # confirmation mismatch
            ("../etc", "../etc", 400),  # traversal
            (".repos", ".repos", 400),  # infrastructure directory
            ("missing-000000000000", "missing-000000000000", 404),
        ]
        for dir_name, confirm, status in cases:
            with self.subTest(dir=dir_name):
                with self.assertRaises(store.DeleteError) as raised:
                    store.delete_workspace(dir_name, confirm)
                self.assertEqual(raised.exception.status, status)
        self.assertTrue(os.path.isdir(os.path.join(ROOT, ".repos")))

    def test_dry_run_and_delete(self) -> None:
        target = make_workspace("zeta-555555555555")
        dry = store.delete_workspace("zeta-555555555555", "zeta-555555555555", dry_run=True)
        self.assertTrue(dry["dry_run"])
        self.assertGreater(dry["size_bytes"], 0)
        self.assertTrue(os.path.isdir(target))

        result = store.delete_workspace("zeta-555555555555", "zeta-555555555555")
        self.assertEqual(result["dir"], "zeta-555555555555")
        self.assertFalse(os.path.exists(target))
        self.assertEqual(result["leftovers"], [])
        self.assertEqual(os.listdir(os.path.join(ROOT, ".webui-trash")), [])
        self.assertNotIn("zeta-555555555555", self.by_dir(store.snapshot(force=True)))

    def test_delete_many_reports_per_item(self) -> None:
        one = make_workspace("one-666666666666")
        result = store.delete_many(
            [
                {"dir": "one-666666666666", "confirm": "one-666666666666"},
                {"dir": "gamma-333333333333", "confirm": "gamma-333333333333"},
                {"dir": "../etc", "confirm": "../etc"},
            ]
        )
        self.assertEqual([item["dir"] for item in result["deleted"]], ["one-666666666666"])
        self.assertEqual([item["dir"] for item in result["skipped"]], ["gamma-333333333333"])
        self.assertEqual([item["dir"] for item in result["failed"]], ["../etc"])
        self.assertFalse(os.path.exists(one))
        with self.assertRaises(store.DeleteError):
            store.delete_many([])


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = tempfile.mkdtemp(prefix="webui-http-")
        os.environ["WEBUI_WORKSPACES_ROOT"] = cls.root
        store.invalidate()
        cls.keep = make_workspace("keep-888888888888")
        cls.auth = ("admin", "s3cret")
        cls.server = webui_server.Server(("127.0.0.1", 0), cls.auth, False)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        shutil.rmtree(cls.root, ignore_errors=True)

    def request(self, method: str, path: str, body: dict | None = None, headers: dict | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        hdrs = dict(headers or {})
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        connection.request(method, path, body=payload, headers=hdrs)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, response.getheaders(), data

    @staticmethod
    def basic(user: str, password: str) -> dict[str, str]:
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    def test_requires_auth(self) -> None:
        status, headers, _ = self.request("GET", "/api/workspaces")
        self.assertEqual(status, 401)
        self.assertTrue(any(name.lower() == "www-authenticate" for name, _ in headers))
        status, _, _ = self.request("GET", "/api/workspaces", headers=self.basic("admin", "wrong"))
        self.assertEqual(status, 401)

    def test_index_and_assets(self) -> None:
        status, headers, body = self.request("GET", "/", headers=self.basic("admin", "s3cret"))
        self.assertEqual(status, 200)
        self.assertIn(b"Workspace cleanup", body)
        csp = dict((name.lower(), value) for name, value in headers)["content-security-policy"]
        self.assertIn("default-src 'none'", csp)
        for asset, needle in (("/app.js", b"runDelete"), ("/style.css", b"--accent"), ("/favicon.svg", b"<svg")):
            status, _, body = self.request("GET", asset, headers=self.basic("admin", "s3cret"))
            self.assertEqual(status, 200)
            self.assertIn(needle, body)
        status, _, _ = self.request("GET", "/../../etc/passwd", headers=self.basic("admin", "s3cret"))
        self.assertEqual(status, 404)

    def test_workspaces_endpoint(self) -> None:
        status, _, body = self.request("GET", "/api/workspaces", headers=self.basic("admin", "s3cret"))
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["root"], self.root)
        self.assertIn("keep-888888888888", [item["dir"] for item in payload["workspaces"]])
        self.assertIn("totals", payload)
        self.assertFalse(payload["config"]["read_only"])

        status, _, body = self.request("GET", "/api/health", headers=self.basic("admin", "s3cret"))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "ok")

    def test_delete_needs_csrf_header(self) -> None:
        body = {"items": [{"dir": "keep-888888888888", "confirm": "keep-888888888888"}]}
        status, _, _ = self.request("POST", "/api/workspaces/delete", body, self.basic("admin", "s3cret"))
        self.assertEqual(status, 403)
        status, _, _ = self.request(
            "POST",
            "/api/workspaces/delete",
            body,
            {**self.basic("admin", "s3cret"), "X-WebUI": "1", "Origin": "http://evil.example"},
        )
        self.assertEqual(status, 403)
        self.assertTrue(os.path.isdir(self.keep))

    def test_delete_flow(self) -> None:
        workspace = make_workspace("http-777777777777")
        headers = {**self.basic("admin", "s3cret"), "X-WebUI": "1"}
        # mismatched confirmation is refused server-side
        status, _, body = self.request(
            "POST", "/api/workspaces/delete", {"items": [{"dir": "http-777777777777", "confirm": "nope"}]}, headers
        )
        self.assertEqual(status, 400)
        self.assertTrue(os.path.isdir(workspace))

        status, _, body = self.request(
            "POST",
            "/api/workspaces/delete",
            {"items": [{"dir": "http-777777777777", "confirm": "http-777777777777"}]},
            headers,
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["deleted"][0]["dir"], "http-777777777777")
        self.assertFalse(os.path.exists(workspace))

        # second attempt: gone
        status, _, _ = self.request(
            "POST",
            "/api/workspaces/delete",
            {"items": [{"dir": "http-777777777777", "confirm": "http-777777777777"}]},
            headers,
        )
        self.assertIn(status, (207, 404))

    def test_read_only_server(self) -> None:
        server = webui_server.Server(("127.0.0.1", 0), None, True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=30)
            connection.request(
                "POST",
                "/api/workspaces/delete",
                body=json.dumps({"items": [{"dir": "x", "confirm": "x"}]}),
                headers={"Content-Type": "application/json", "X-WebUI": "1"},
            )
            response = connection.getresponse()
            response.read()
            connection.close()
            self.assertEqual(response.status, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
