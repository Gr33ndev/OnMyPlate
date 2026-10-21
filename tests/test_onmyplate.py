"""Tests for onmyplate.py, using only the standard library.

Run with ``python -m unittest discover -s tests``.
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("onmyplate", ROOT / "onmyplate.py")
wl = importlib.util.module_from_spec(_spec)
sys.modules["onmyplate"] = wl
_spec.loader.exec_module(wl)

NOW_MS = int(time.time() * 1000)


# --------------------------------------------------------------------------- #
# A fake Jira Data Center, GitLab and Bitbucket Data Center on one port
# --------------------------------------------------------------------------- #


def jira_issue(key: str, category: str = "indeterminate") -> dict[str, Any]:
    return {
        "key": key,
        "fields": {
            "summary": f"Issue {key}",
            "status": {"name": "In Progress", "statusCategory": {"key": category}},
            "issuetype": {"name": "Task"},
            "priority": {"name": "High"},
            "assignee": {"name": "sam", "displayName": "Sam Taylor"},
            "reporter": {"name": "anna", "displayName": "Anna Weber"},
            "updated": "2026-10-20T10:00:00.000+0200",
            "created": "2026-10-01T10:00:00.000+0200",
            "project": {"name": "Platform"},
            "labels": ["infra"],
            "comment": {"comments": [], "total": 0},
        },
    }


def gitlab_item(item_id: int, iid: int, kind: str = "issue", **extra: Any) -> dict[str, Any]:
    separator = "!" if kind == "mr" else "#"
    item = {
        "id": item_id,
        "iid": iid,
        "project_id": 11,
        "title": f"GitLab {kind} {iid}",
        "state": "opened",
        "web_url": f"{{base}}/platform/infra/-/{'merge_requests' if kind == 'mr' else 'issues'}/{iid}",
        "references": {"full": f"platform/infra{separator}{iid}"},
        "labels": [],
        "author": {"name": "Anna Weber"},
        "assignees": [{"name": "Sam Taylor"}],
        "updated_at": "2026-10-20T08:00:00.000Z",
        "created_at": "2026-10-10T08:00:00.000Z",
    }
    item.update(extra)
    return item


def bitbucket_pr(pr_id: int, author: str, review_status: str = "UNAPPROVED") -> dict[str, Any]:
    return {
        "id": pr_id,
        "title": f"PR {pr_id}",
        "state": "OPEN",
        "author": {"user": {"name": author, "displayName": author.title()}},
        "reviewers": [{"user": {"name": "anna", "displayName": "Anna"}, "status": review_status}],
        "fromRef": {"displayId": "feature"},
        "toRef": {"displayId": "main", "repository": {"slug": "repo", "project": {"key": "PLAT"}}},
        "updatedDate": NOW_MS,
        "createdDate": NOW_MS,
        "links": {"self": [{"href": "{base}/projects/PLAT/repos/repo/pull-requests/1/overview"}]},
    }


class FakeServers(BaseHTTPRequestHandler):
    base = ""
    requests: ClassVar[list[tuple[str, str]]] = []

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def _reply(
        self,
        body: Any,
        headers: dict[str, str] | None = None,
        content_type: str = "application/json",
    ) -> None:
        raw = (
            body
            if isinstance(body, bytes)
            else json.dumps(body).replace("{base}", self.base).encode()
        )
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:
        self.requests.append(("POST", self.path))
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        jql = body["jql"]
        issues = []
        if "assignee" in jql:
            issues = [jira_issue("PLAT-1")]
        elif "reporter" in jql:
            issues = [jira_issue("PLAT-2", "new")]
        elif "watcher" in jql:
            issues = [jira_issue("PLAT-1"), jira_issue("PLAT-3")]
        self._reply({"startAt": 0, "total": len(issues), "issues": issues})

    def do_GET(self) -> None:  # one fake per route
        self.requests.append(("GET", self.path))
        parts = urlsplit(self.path)
        path, query = parts.path, parse_qs(parts.query)
        if path == "/redirect":
            self.send_response(302)
            self.send_header("Location", self.base.replace("127.0.0.1", "localhost") + "/stolen")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        if path == "/jira/rest/api/2/myself":
            return self._reply({"name": "sam", "key": "sam", "displayName": "Sam Taylor"})
        if path == "/jira/activity":
            entries = "".join(
                f"<entry><updated>2026-10-20T10:00:00Z</updated>"
                f"<link href='{self.base}/jira/browse/{key}'/></entry>"
                for key in ("PLAT-1", "PLAT-4")
            )
            feed = f"<feed xmlns='http://www.w3.org/2005/Atom'>{entries}</feed>".encode()
            return self._reply(feed, content_type="application/atom+xml")
        if path == "/jira/rest/api/2/issue/PLAT-4":
            return self._reply(jira_issue("PLAT-4"))
        if path == "/gitlab/api/v4/user":
            return self._reply({"id": 42, "username": "sam"})
        if path == "/gitlab/api/v4/issues":
            scope = query["scope"][0]
            return self._reply([gitlab_item(1, 1)] if scope == "assigned_to_me" else [])
        if path == "/gitlab/api/v4/merge_requests":
            return self._reply([gitlab_item(100, 7, "mr")] if "reviewer_id" in query else [])
        if path == "/gitlab/api/v4/todos":
            if query.get("state") == ["pending"]:
                target = gitlab_item(5, 5)
                return self._reply(
                    [{"target_type": "Issue", "action_name": "mentioned", "target": target}]
                )
            return self._reply([])
        if path == "/gitlab/api/v4/events":
            return self._reply([])
        if path == "/bitbucket/rest/api/latest/application-properties":
            return self._reply({"version": "8.19"}, {"X-AUSERNAME": "sam"})
        if path == "/bitbucket/rest/api/latest/dashboard/pull-requests":
            role = query["role"][0]
            values = {
                "AUTHOR": [bitbucket_pr(1, "sam", "NEEDS_WORK")],
                "REVIEWER": [bitbucket_pr(2, "anna")],
                "PARTICIPANT": [bitbucket_pr(3, "jonas")],
            }[role]
            return self._reply({"values": values, "isLastPage": True})
        if path == "/bitbucket/rest/api/latest/projects/PLAT/repos/repo/pull-requests/3/activities":
            comment = {
                "author": {"name": "anna"},
                "text": "@sam could you check?",
                "createdDate": NOW_MS,
            }
            return self._reply(
                {"values": [{"action": "COMMENTED", "comment": comment}], "isLastPage": True}
            )
        if path.endswith("/activities"):
            return self._reply({"values": [], "isLastPage": True})
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()
        return None


class FakeServerCase(unittest.TestCase):
    server: ThreadingHTTPServer

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeServers)
        FakeServers.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def source_cfg(self, name: str, **extra: Any) -> dict[str, Any]:
        cfg = {"url": f"{FakeServers.base}/{name}", "token": "secret", "allow_insecure_http": True}
        cfg.update(extra)
        return wl.merge_config(wl.DEFAULT_CONFIG, {name: cfg})[name]

    def fetch(self, cls: type, name: str, **extra: Any) -> dict[str, set[str]]:
        source = cls(self.source_cfg(name, **extra), wl.HttpClient({"timeout_seconds": 5}))
        items, _ = source.fetch()
        return {item["id"]: set(item["relations"]) for item in items}


class SourceTests(FakeServerCase):
    def test_jira_data_center(self) -> None:
        found = self.fetch(wl.JiraSource, "jira", type="server")
        self.assertEqual(found["jira:PLAT-1"], {"assignee", "watcher", "commented"})
        self.assertEqual(found["jira:PLAT-2"], {"reporter"})
        self.assertEqual(found["jira:PLAT-3"], {"watcher"})
        self.assertEqual(found["jira:PLAT-4"], {"commented"})

    def test_gitlab(self) -> None:
        found = self.fetch(wl.GitLabSource, "gitlab")
        self.assertEqual(found["gitlab:issue:1"], {"assignee"})
        self.assertEqual(found["gitlab:mr:100"], {"reviewer"})
        self.assertEqual(found["gitlab:issue:5"], {"mentioned"})

    def test_bitbucket(self) -> None:
        found = self.fetch(wl.BitbucketSource, "bitbucket")
        self.assertEqual(found["bitbucket:pr:PLAT:repo:1"], {"reporter", "changes"})
        self.assertEqual(found["bitbucket:pr:PLAT:repo:2"], {"reviewer"})
        self.assertEqual(found["bitbucket:pr:PLAT:repo:3"], {"mentioned"})

    def test_client_never_writes(self) -> None:
        client = wl.HttpClient({})
        for method, path in (("PUT", "/x"), ("DELETE", "/x"), ("POST", "/rest/api/2/issue")):
            with self.assertRaises(wl.SourceError):
                client.request(method, FakeServers.base + path)

    def test_cross_origin_redirect_is_blocked(self) -> None:
        FakeServers.requests.clear()
        with self.assertRaisesRegex(wl.SourceError, "Blocked redirect"):
            wl.HttpClient({}).request("GET", FakeServers.base + "/redirect", {"Authorization": "x"})
        self.assertNotIn(("GET", "/stolen"), FakeServers.requests)

    def test_https_required(self) -> None:
        with self.assertRaises(wl.SourceError):
            wl.require_https("http://jira.example.com", {}, "Jira")
        wl.require_https("https://jira.example.com", {}, "Jira")

    def test_token_with_control_characters_is_rejected(self) -> None:
        cfg = self.source_cfg("gitlab", token="abc\ndef")
        with self.assertRaises(wl.SourceError):
            wl.GitLabSource(cfg, wl.HttpClient({}))


# --------------------------------------------------------------------------- #
# Parsing and validation
# --------------------------------------------------------------------------- #


class TodoTests(unittest.TestCase):
    def test_roundtrip_keeps_fields(self) -> None:
        todos = [
            {
                "id": "a1",
                "text": "Write runbook",
                "done": False,
                "due": "2026-10-30",
                "ref": "jira:PLAT-1",
            },
            {"id": "b2", "text": "Done thing", "done": True, "done_at": "2026-10-20"},
        ]
        parsed, needs_rewrite = wl.parse_todos(wl.render_todos(todos))
        self.assertFalse(needs_rewrite)
        self.assertEqual(parsed, todos)

    def test_hand_written_lines_get_ids(self) -> None:
        parsed, needs_rewrite = wl.parse_todos("- [ ] Buy milk 📅 2026-11-01\n- [x] Old\n")
        self.assertTrue(needs_rewrite)
        self.assertEqual([t["text"] for t in parsed], ["Buy milk", "Old"])
        self.assertEqual(parsed[0]["due"], "2026-11-01")

    def test_markup_stays_inert_in_the_file(self) -> None:
        text = "<img src=x> ![](https://e/p.png) [link](https://e) a\\b --> <!-- id:zz -->"
        rendered = wl.render_todos([{"id": "a1", "text": text, "done": False}])
        line = next(line for line in rendered.splitlines() if "id:a1" in line)
        body = line.split(" <!-- id:a1")[0]
        self.assertNotIn("](", body.replace("\\]", ""))
        self.assertIn("\\<img", body)
        parsed, _ = wl.parse_todos(rendered)
        self.assertEqual(parsed[0]["id"], "a1")
        self.assertEqual(parsed[0]["text"], wl._todo_text(text))

    def test_validation_rejects_bad_input(self) -> None:
        for bad in (
            None,
            [{"id": "a", "text": 1}],
            [{"id": "a b", "text": "x"}],
            [{"id": "a", "text": "x"}, {"id": "a", "text": "y"}],
        ):
            with self.assertRaises((TypeError, ValueError)):
                wl.validate_todos(bad)
        cleaned = wl.validate_todos([{"id": "a", "text": "x", "due": "soon", "ref": "evil:1"}])
        self.assertEqual(cleaned, [{"id": "a", "text": "x", "done": False}])


class InputHardeningTests(unittest.TestCase):
    def test_atom_feed_rejects_dtds(self) -> None:
        good = b"<feed xmlns='http://www.w3.org/2005/Atom'><entry/></feed>"
        self.assertIsNotNone(wl.parse_atom_feed(good))
        laughs = '<!DOCTYPE l [<!ENTITY a "aa"><!ENTITY b "&a;&a;">]><feed>&b;</feed>'
        for attack in (
            laughs.encode(),
            laughs.encode("utf-16"),
            laughs.lower().encode(),
            b"\xff\xfe",
        ):
            self.assertIsNone(wl.parse_atom_feed(attack))

    def test_items_are_coerced(self) -> None:
        item = wl.coerce_item(
            {
                "source": "jira",
                "id": "x",
                "title": "a" + chr(0x202E) + "b\x1b[2J",
                "labels": "notalist",
                "relations": {"assignee", "bogus"},
                "due": '2026-10-22"><img',
                "state": "weird",
            }
        )
        self.assertEqual(item["title"], "a b [2J")
        self.assertEqual(item["labels"], [])
        self.assertEqual(item["relations"], ["assignee"])
        self.assertEqual(item["due"], "2026-10-22")
        self.assertEqual(item["state"], "todo")

    def test_invalid_jira_keys_do_not_become_links(self) -> None:
        cfg = wl.merge_config(
            wl.DEFAULT_CONFIG, {"jira": {"url": "https://jira.example.com", "token": "t"}}
        )
        source = wl.JiraSource(cfg["jira"], wl.HttpClient({}))
        self.assertEqual(
            source.normalize({"key": "../../admin?", "fields": {}})["url"],
            "https://jira.example.com",
        )
        self.assertEqual(
            source.normalize({"key": "PLAT-7", "fields": {}})["url"],
            "https://jira.example.com/browse/PLAT-7",
        )


# --------------------------------------------------------------------------- #
# Local server
# --------------------------------------------------------------------------- #


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        wl.set_data_dir(cls.tmp.name)
        store = wl.Store(wl.merge_config(wl.DEFAULT_CONFIG, {}), demo=True)
        store.refresh()
        cls.server = wl.OnMyPlateServer(0, store, wl.load_secret())
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.tmp.cleanup()

    def request(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        body: Any = None,
        host: str | None = None,
    ) -> tuple[int, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(body).encode() if body is not None else None
        all_headers = {"Host": host or f"127.0.0.1:{self.port}", **(headers or {})}
        if payload is not None:
            all_headers.setdefault("Content-Type", "application/json")
        conn.request(method, path, payload, all_headers)
        response = conn.getresponse()
        return response.status, response.read()

    def login(self) -> str:
        code = self.server.login_codes.issue()
        status, body = self.request("POST", "/api/login", body={"code": code})
        self.assertEqual(status, 200)
        return json.loads(body)["token"]

    def test_page_is_public_but_api_is_not(self) -> None:
        self.assertEqual(self.request("GET", "/")[0], 200)
        self.assertEqual(self.request("GET", "/api/items")[0], 401)
        self.assertEqual(self.request("GET", "/api/items", {"X-OnMyPlate-Token": "wrong"})[0], 401)

    def test_login_codes_work_once(self) -> None:
        code = self.server.login_codes.issue()
        self.assertEqual(self.request("POST", "/api/login", body={"code": code})[0], 200)
        self.assertEqual(self.request("POST", "/api/login", body={"code": code})[0], 401)

    def test_api_with_token(self) -> None:
        token = {"X-OnMyPlate-Token": self.login()}
        status, body = self.request("GET", "/api/items", token)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["items"])
        self.assertEqual(
            self.request("POST", "/api/todos", token, {"todos": [{"id": "a1", "text": "Test"}]})[0],
            200,
        )
        self.assertEqual(
            json.loads(self.request("GET", "/api/todos", token)[1])["todos"][0]["text"], "Test"
        )

    def test_foreign_requests_are_rejected(self) -> None:
        token = {"X-OnMyPlate-Token": self.login()}
        self.assertEqual(self.request("GET", "/api/items", token, host="evil.example")[0], 403)
        self.assertEqual(
            self.request("GET", "/api/items", {**token, "Sec-Fetch-Site": "cross-site"})[0], 403
        )
        self.assertEqual(
            self.request("GET", "/api/items", {**token, "Origin": "http://127.0.0.1:1"})[0], 403
        )
        self.assertEqual(
            self.request("POST", "/api/local", {**token, "Content-Type": "text/plain"}, {})[0], 415
        )


class BackupTests(unittest.TestCase):
    def test_export_and_import(self) -> None:
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as target:
            wl.set_data_dir(source)
            wl.save_todos([{"id": "a1", "text": "Move laptop", "done": False}])
            wl.save_local({"pins": ["jira:PLAT-1"], "notes": {}})
            archive = Path(source) / "backup.zip"
            archive.write_bytes(wl.make_backup())
            self.assertNotIn("onmyplate.config.json", zipfile.ZipFile(archive).namelist())
            wl.set_data_dir(target)
            restored, _, _ = wl.import_backup(archive)
            self.assertEqual(sorted(restored), ["onmyplate.local.json", "onmyplate.todos.md"])
            self.assertEqual(wl.load_todos()[0]["text"], "Move laptop")
            self.assertEqual((Path(target) / "onmyplate.todos.md").stat().st_mode & 0o777, 0o600)

    def test_backups_from_the_old_name_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / "old.zip"
            with zipfile.ZipFile(archive, "w") as zipped:
                zipped.writestr("worklist.todos.md", "- [ ] From Worklist <!-- id:a1 -->\n")
            wl.set_data_dir(Path(folder) / "data")
            restored, _, _ = wl.import_backup(archive)
            self.assertEqual(restored, ["onmyplate.todos.md"])
            self.assertEqual(wl.load_todos()[0]["text"], "From Worklist")

    def test_files_from_the_old_name_are_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "worklist.todos.md").write_text("- [ ] Old <!-- id:a1 -->\n", "utf-8")
            (Path(folder) / "worklist.secret").write_text("x" * 40, "utf-8")
            (Path(folder) / "onmyplate.secret").write_text("y" * 40, "utf-8")
            wl.set_data_dir(folder)
            self.assertEqual(wl.load_todos()[0]["text"], "Old")
            self.assertEqual(wl.load_secret(), "y" * 40)
            self.assertTrue((Path(folder) / "worklist.secret").exists())


if __name__ == "__main__":
    unittest.main()
