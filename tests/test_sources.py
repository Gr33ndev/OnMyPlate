"""Source and HTTP client tests against a configurable fake server.

Run with ``python -m unittest discover -s tests``.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import unittest
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import test_onmyplate

wl = test_onmyplate.wl

NOW_MS = int(time.time() * 1000)
NOW_ISO = datetime.now(UTC).isoformat()
Reply = tuple[int, Any, dict[str, str]]


def ok(body: Any, **headers: str) -> Reply:
    return 200, body, headers


def status(code: int, body: Any = None) -> Reply:
    return code, body if body is not None else {"error": "fake"}, {}


class Routes(BaseHTTPRequestHandler):
    """Answers each ``(method, path)`` with a fixed reply or a function of the query."""

    routes: ClassVar[dict[tuple[str, str], Reply | Callable[..., Reply]]] = {}
    log: ClassVar[list[dict[str, Any]]] = []

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def _handle(self, method: str) -> None:
        parts = urlsplit(self.path)
        query = {key: values[0] for key, values in parse_qs(parts.query).items()}
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        self.log.append(
            {
                "method": method,
                "path": parts.path,
                "query": query,
                "body": body,
                "auth": self.headers.get("Authorization") or self.headers.get("PRIVATE-TOKEN"),
            }
        )
        route = self.routes.get((method, parts.path), status(404))
        code, payload, headers = route(query, body) if callable(route) else route
        if isinstance(payload, bytes):
            raw, content_type = payload, headers.pop("Content-Type", "text/html")
        else:
            raw, content_type = json.dumps(payload).encode(), "application/json"
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(raw)


class FakeServer(ThreadingHTTPServer):
    # Sources send up to eight requests at once; the default backlog of 5 resets some on macOS.
    request_queue_size = 32


class RoutesCase(unittest.TestCase):
    server: ThreadingHTTPServer
    base: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = FakeServer(("127.0.0.1", 0), Routes)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        Routes.routes = {}
        Routes.log = []
        wl._MENTION_CACHE.clear()

    def source(self, cls: type, name: str, **extra: Any) -> Any:
        cfg = {"url": f"{self.base}/{name}", "token": "secret", "allow_insecure_http": True}
        cfg.update(extra)
        merged = wl.merge_config(wl.DEFAULT_CONFIG, {name: cfg})[name]
        return cls(merged, wl.HttpClient({"timeout_seconds": 5}))

    @staticmethod
    def relations(items: list[dict[str, Any]]) -> dict[str, set[str]]:
        return {item["id"]: set(item["relations"]) for item in items}


# --------------------------------------------------------------------------- #
# HTTP client
# --------------------------------------------------------------------------- #


class HttpClientTests(RoutesCase):
    def test_json_and_headers_are_returned(self) -> None:
        Routes.routes[("GET", "/x")] = ok({"a": 1}, **{"X-Test": "yes"})
        body, headers = wl.HttpClient({}).request("GET", self.base + "/x")
        self.assertEqual(body, {"a": 1})
        self.assertEqual(headers["X-Test"], "yes")
        self.assertEqual(Routes.log[0]["method"], "GET")

    def test_login_page_instead_of_json_is_explained(self) -> None:
        Routes.routes[("GET", "/sso")] = ok(b"<html>Sign in</html>")
        with self.assertRaisesRegex(wl.SourceError, "not JSON"):
            wl.HttpClient({}).request("GET", self.base + "/sso?token=abc")

    def test_http_errors_carry_a_hint_but_no_query(self) -> None:
        Routes.routes[("GET", "/x")] = status(401, {"message": "bad\x1b[31m token"})
        with self.assertRaises(wl.SourceError) as ctx:
            wl.HttpClient({}).request("GET", self.base + "/x?private=1")
        message = str(ctx.exception)
        self.assertIn("HTTP 401", message)
        self.assertIn("token invalid", message)
        self.assertNotIn("private=1", message)
        self.assertNotIn("\x1b", message)

    def test_unreachable_host(self) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        with self.assertRaisesRegex(wl.SourceError, "Cannot connect"):
            wl.HttpClient({"timeout_seconds": 2}).request("GET", f"http://127.0.0.1:{port}/")

    def test_responses_are_bounded(self) -> None:
        Routes.routes[("GET", "/big")] = ok({"data": "x" * 1000})
        with mock.patch.object(wl, "MAX_RESPONSE_BYTES", 100), self.assertRaises(wl.SourceError):
            wl.HttpClient({}).request("GET", self.base + "/big")

    def test_only_jira_search_may_post(self) -> None:
        Routes.routes[("POST", "/rest/api/2/search")] = ok({"issues": []})
        body, _ = wl.HttpClient({}).request("POST", self.base + "/rest/api/2/search", body={})
        self.assertEqual(body, {"issues": []})
        with self.assertRaisesRegex(wl.SourceError, "only performs read requests"):
            wl.HttpClient({}).request("POST", self.base + "/rest/api/2/issue", body={})
        self.assertEqual([entry["path"] for entry in Routes.log], ["/rest/api/2/search"])

    def test_same_origin_redirects_are_followed(self) -> None:
        Routes.routes[("GET", "/old")] = (302, b"", {"Location": "/new"})
        Routes.routes[("GET", "/new")] = ok({"moved": True})
        body, _ = wl.HttpClient({}).request("GET", self.base + "/old")
        self.assertEqual(body, {"moved": True})


# --------------------------------------------------------------------------- #
# Jira
# --------------------------------------------------------------------------- #


def jira_issue(key: str, comments: list[Any] | None = None, total: int = 0) -> dict[str, Any]:
    issue = test_onmyplate.jira_issue(key)
    issue["fields"]["comment"] = {"comments": comments or [], "total": total}
    return issue


class JiraTests(RoutesCase):
    API = "/jira/rest/api/3"

    def test_cloud_search_pages_and_rechecks_comments(self) -> None:
        def search(_: dict[str, str], body: dict[str, Any]) -> Reply:
            jql = body["jql"]
            if "assignee" in jql:
                if body.get("nextPageToken") == "page2":
                    return ok({"issues": [jira_issue("PLAT-2")], "isLast": True})
                return ok({"issues": [jira_issue("PLAT-1")], "nextPageToken": "page2"})
            if "updatedBy" in jql:
                return ok({"issues": [jira_issue("PLAT-9", total=5)], "isLast": True})
            return ok({"issues": [], "isLast": True})

        mine = {"author": {"accountId": "me-1"}}
        Routes.routes = {
            ("GET", self.API + "/myself"): ok({"accountId": "me-1"}),
            ("POST", self.API + "/search/jql"): search,
            ("GET", self.API + "/issue/PLAT-9/comment"): ok({"comments": [mine]}),
        }
        source = self.source(
            wl.JiraSource, "jira", type="cloud", scoped_token=False, scope_jql="project = PLAT"
        )
        items, warnings = source.fetch()
        self.assertEqual(warnings, [])
        self.assertEqual(
            self.relations(items),
            {
                "jira:PLAT-1": {"assignee"},
                "jira:PLAT-2": {"assignee"},
                "jira:PLAT-9": {"commented"},
            },
        )
        searches = [entry for entry in Routes.log if entry["path"].endswith("/search/jql")]
        self.assertTrue(all("AND (project = PLAT)" in s["body"]["jql"] for s in searches))
        self.assertTrue(all("statusCategory != Done" in s["body"]["jql"] for s in searches))
        self.assertTrue(all(entry["auth"].startswith("Basic ") for entry in Routes.log))

    def test_all_queries_failing_is_an_error(self) -> None:
        Routes.routes[("GET", "/jira/rest/api/2/myself")] = ok({"name": "sam"})
        source = self.source(wl.JiraSource, "jira", type="server", participation_jql="x = y")
        with self.assertRaisesRegex(wl.SourceError, "failed"):
            source.fetch()

    def test_some_queries_failing_is_a_warning(self) -> None:
        def search(_: dict[str, str], body: dict[str, Any]) -> Reply:
            if "assignee" in body["jql"]:
                return ok({"issues": [jira_issue("PLAT-1")], "total": 1})
            return status(500)

        Routes.routes = {
            ("GET", "/jira/rest/api/2/myself"): status(403),
            ("POST", "/jira/rest/api/2/search"): search,
        }
        source = self.source(wl.JiraSource, "jira", type="server", include_done=True)
        items, warnings = source.fetch()
        self.assertEqual(self.relations(items), {"jira:PLAT-1": {"assignee"}})
        self.assertTrue(any("cannot read your own user" in w for w in warnings))
        self.assertTrue(any("'reporter' failed" in w for w in warnings))
        jql = next(e["body"]["jql"] for e in Routes.log if e["method"] == "POST")
        self.assertNotIn("statusCategory", jql)
        self.assertTrue(Routes.log[0]["auth"].startswith("Bearer "))

    def test_request_participants_and_mentions(self) -> None:
        def comment(name: str, created: str, body: str = "ok") -> dict[str, Any]:
            return {"author": {"name": name}, "created": created, "body": body}

        mention = "please check, [~sam]"
        answered = [
            comment("anna", "2026-10-01T10:00:00.000+0200", mention),
            comment("sam", "2026-10-02T10:00:00.000+0200"),
        ]
        asked_again = [*answered, comment("anna", "2026-10-03T10:00:00.000+0200", mention)]

        def search(_: dict[str, str], body: dict[str, Any]) -> Reply:
            jql = body["jql"]
            if "Request participants" in jql:
                return ok({"issues": [jira_issue("SD-1")], "total": 1})
            if "description ~ currentUser()" in jql:
                issues = [
                    jira_issue("PLAT-1"),
                    jira_issue("PLAT-2", answered),
                    jira_issue("PLAT-3", asked_again),
                ]
                return ok({"issues": issues, "total": 3})
            return ok({"issues": [], "total": 0})

        Routes.routes = {
            ("GET", "/jira/rest/api/2/myself"): ok({"name": "sam", "key": "sam"}),
            ("POST", "/jira/rest/api/2/search"): search,
        }
        source = self.source(wl.JiraSource, "jira", type="server", participation_jql="x = y")
        items, warnings = source.fetch()
        self.assertEqual(warnings, [])
        self.assertEqual(
            self.relations(items),
            {
                "jira:SD-1": {"watcher"},
                "jira:PLAT-1": {"mentioned"},
                "jira:PLAT-2": {"mentioned_done", "commented"},
                "jira:PLAT-3": {"mentioned", "commented"},
            },
        )

    def test_unsupported_optional_queries_are_skipped_quietly(self) -> None:
        def search(_: dict[str, str], body: dict[str, Any]) -> Reply:
            jql = body["jql"]
            if "Request participants" in jql or "description ~" in jql:
                return status(400)
            return ok({"issues": [jira_issue("PLAT-1")], "total": 1})

        Routes.routes = {
            ("GET", "/jira/rest/api/2/myself"): ok({"name": "sam"}),
            ("POST", "/jira/rest/api/2/search"): search,
        }
        source = self.source(wl.JiraSource, "jira", type="server", participation_jql="x = y")
        items, warnings = source.fetch()
        self.assertEqual(warnings, [])
        self.assertEqual(
            self.relations(items), {"jira:PLAT-1": {"assignee", "reporter", "watcher"}}
        )

    def test_followed_boards_add_their_filter(self) -> None:
        def search(_: dict[str, str], body: dict[str, Any]) -> Reply:
            jql = body["jql"]
            if jql.startswith("(assignee"):
                return ok({"issues": [jira_issue("PLAT-1")], "total": 1})
            if jql.startswith("(filter = 10100)"):
                return ok({"issues": [jira_issue("PLAT-1"), jira_issue("PLAT-5")], "total": 2})
            return ok({"issues": [], "total": 0})

        agile = "/jira/rest/agile/1.0/board"
        Routes.routes = {
            ("GET", "/jira/rest/api/2/myself"): ok({"name": "sam"}),
            ("POST", "/jira/rest/api/2/search"): search,
            ("GET", agile + "/42/configuration"): ok({"filter": {"id": "10100"}}),
            ("GET", agile + "/43/configuration"): status(403),
        }
        jira = f"{self.base}/jira"
        source = self.source(
            wl.JiraSource,
            "jira",
            type="server",
            participation_jql="x = y",
            watch_boards=[
                f"{jira}/secure/RapidBoard.jspa?rapidView=42&view=planning",
                f"{jira}/secure/RapidBoard.jspa?rapidView=43",
                "https://jira.elsewhere.example/secure/RapidBoard.jspa?rapidView=42",
            ],
        )
        items, warnings = source.fetch()
        self.assertEqual(
            self.relations(items), {"jira:PLAT-1": {"assignee"}, "jira:PLAT-5": {"board"}}
        )
        self.assertEqual(len(warnings), 2)
        self.assertIn("cannot read board 43", warnings[0])
        self.assertIn("ignoring watch_boards entry", warnings[1])
        self.assertEqual(
            wl.JiraSource.board_id(jira, f"{jira}/jira/software/projects/PLAT/boards/7/backlog"),
            "7",
        )
        self.assertIsNone(
            wl.JiraSource.board_id(jira, f"{jira}/secure/RapidBoard.jspa?rapidView=../1")
        )

    def test_followed_boards_can_be_narrowed(self) -> None:
        searched: list[str] = []

        def search(_: dict[str, str], body: dict[str, Any]) -> Reply:
            searched.append(body["jql"])
            return ok({"issues": [], "total": 0})

        agile = "/jira/rest/agile/1.0/board"
        Routes.routes = {
            ("GET", "/jira/rest/api/2/myself"): ok({"name": "sam"}),
            ("POST", "/jira/rest/api/2/search"): search,
            ("GET", agile + "/42/configuration"): ok({"filter": {"id": "10100"}}),
            ("GET", agile + "/42/quickfilter/7"): ok({"id": 7, "jql": "component = Payments"}),
            ("GET", agile + "/42/quickfilter/8"): status(404),
        }
        jira = f"{self.base}/jira"
        board = f"{jira}/secure/RapidBoard.jspa?rapidView=42"
        source = self.source(
            wl.JiraSource,
            "jira",
            type="server",
            participation_jql="x = y",
            watch_boards=[
                f"{board}&quickFilter=7",
                {"url": board, "jql": "labels = mine\n  AND priority = High"},
                {"url": f"{board}&quickFilter=8", "jql": "labels = x"},
            ],
        )
        _, warnings = source.fetch()
        tail = " AND statusCategory != Done ORDER BY updated DESC"
        self.assertEqual(
            sorted(jql for jql in searched if "filter = 10100" in jql),
            [
                "((filter = 10100) AND (component = Payments))" + tail,
                "((filter = 10100) AND (labels = mine AND priority = High))" + tail,
            ],
        )
        # A quick filter that cannot be read must not widen the board to all its issues.
        self.assertEqual(len(warnings), 1)
        self.assertIn("cannot read quick filter 8 of board 42", warnings[0])

    def test_cloud_type_is_detected_and_cloud_id_checked(self) -> None:
        cfg = wl.merge_config(
            wl.DEFAULT_CONFIG,
            {"jira": {"url": "https://acme.atlassian.net", "token": "t", "cloud_id": "../evil"}},
        )["jira"]
        source = wl.JiraSource(cfg, wl.HttpClient({}))
        self.assertTrue(source.cloud)
        self.assertIsNone(source.api)
        with self.assertRaisesRegex(wl.SourceError, "cloud ID"):
            source._resolve_api()
        source.cfg = {**cfg, "cloud_id": "0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0"}
        source._resolve_api()
        self.assertEqual(
            source.api,
            "https://api.atlassian.com/ex/jira/0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0/rest/api/3",
        )

    def test_activity_stream_ignores_old_entries(self) -> None:
        old = "2001-01-01T00:00:00Z"
        entries = "".join(
            f"<entry><updated>{stamp}</updated><link href='{self.base}/jira/browse/{key}'/></entry>"
            for key, stamp in (("PLAT-1", NOW_ISO), ("PLAT-2", old), ("PLAT-1", NOW_ISO))
        )
        feed = f"<feed xmlns='http://www.w3.org/2005/Atom'>{entries}</feed>".encode()
        Routes.routes[("GET", "/jira/activity")] = (
            200,
            feed,
            {"Content-Type": "application/atom+xml"},
        )
        source = self.source(wl.JiraSource, "jira", type="server")
        source.me = {"name": "sam"}
        keys, warning = source._activity_comment_keys(30)
        self.assertEqual((keys, warning), (["PLAT-1"], None))
        self.assertEqual(Routes.log[0]["query"]["streams"], "user IS sam")


class JiraHintsAndAliasTests(RoutesCase):
    def test_activity_links_from_another_base_url_are_used(self) -> None:
        entries = "".join(
            f"<entry><updated>{NOW_ISO}</updated><link href='{href}'/></entry>"
            for href in (
                "https://jira-alias.example/browse/PLAT-7?focusedCommentId=1",
                "https://jira-alias.example/browse/PLAT-8/extra",
                "https://jira-alias.example/browse/not-a-key",
            )
        )
        feed = f"<feed xmlns='http://www.w3.org/2005/Atom'>{entries}</feed>".encode()
        Routes.routes[("GET", "/jira/activity")] = (
            200,
            feed,
            {"Content-Type": "application/atom+xml"},
        )
        source = self.source(wl.JiraSource, "jira", type="server")
        source.me = {"name": "sam"}
        self.assertEqual(source._activity_comment_keys(30), (["PLAT-7"], None))

    def test_404_hints_fit_the_product(self) -> None:
        server = self.source(wl.JiraSource, "jira", type="server")
        with self.assertRaises(wl.SourceError) as ctx:
            server._get("/myself")
        self.assertIn("wrong URL or context path", str(ctx.exception))
        self.assertNotIn("scoped_token", str(ctx.exception))
        cloud = self.source(wl.JiraSource, "jira", type="cloud", scoped_token=False)
        with self.assertRaisesRegex(wl.SourceError, '"scoped_token": true'):
            cloud._get("/myself")


# --------------------------------------------------------------------------- #
# GitLab
# --------------------------------------------------------------------------- #


def gitlab_item(item_id: int, iid: int, kind: str = "issue", **extra: Any) -> dict[str, Any]:
    item = test_onmyplate.gitlab_item(item_id, iid, kind, **extra)
    item["web_url"] = item["web_url"].replace("{base}", "https://elsewhere.example")
    return item


class GitLabTests(RoutesCase):
    API = "/gitlab/api/v4"

    def test_fetch_combines_every_signal(self) -> None:
        def issues(query: dict[str, str], _: Any) -> Reply:
            if query["scope"] == "assigned_to_me":
                if query["page"] == "1":
                    return ok([gitlab_item(1, 1)], **{"X-Next-Page": "2"})
                return ok([gitlab_item(2, 2)])
            return ok([gitlab_item(3, 3, state="closed")])

        def merge_requests(query: dict[str, str], _: Any) -> Reply:
            if query.get("reviewer_id") == "42":
                labels = ["workflow::backlog", "priority::high"]
                return ok([gitlab_item(100, 7, "mr", draft=True, labels=labels)])
            return ok([])

        def todos(query: dict[str, str], _: Any) -> Reply:
            if query.get("action") == "mentioned":
                target = gitlab_item(4, 4)
                todo = {"target_type": "Issue", "target": target, "created_at": NOW_ISO}
                return ok([todo])
            return ok([])

        events = [
            {
                "project_id": 11,
                "target_type": "Note",
                "note": {"noteable_type": "Issue", "noteable_iid": 1},
            },
            {
                "project_id": 11,
                "target_type": "Note",
                "note": {"noteable_type": "Issue", "noteable_iid": 9},
            },
            {
                "project_id": "x",
                "target_type": "Note",
                "note": {"noteable_type": "Issue", "noteable_iid": 1},
            },
            # A reply in a thread: GitLab's target_type=note filter would drop it.
            {
                "project_id": 11,
                "target_type": "DiscussionNote",
                "note": {"noteable_type": "Issue", "noteable_iid": 12},
            },
        ]

        def user_events(query: dict[str, str], _: Any) -> Reply:
            wanted = query.get("target_type")
            return ok([e for e in events if not wanted or e["target_type"] == wanted.capitalize()])

        Routes.routes = {
            ("GET", self.API + "/user"): ok({"id": 42}),
            ("GET", self.API + "/issues"): issues,
            ("GET", self.API + "/merge_requests"): merge_requests,
            ("GET", self.API + "/todos"): todos,
            ("GET", self.API + "/projects/platform%2Finfra/merge_requests"): status(404),
            ("GET", self.API + "/groups/platform%2Finfra/merge_requests"): ok(
                [gitlab_item(101, 8, "mr")]
            ),
            ("GET", self.API + "/events"): user_events,
            ("GET", self.API + "/projects/11/issues/9"): ok(gitlab_item(9, 9)),
            ("GET", self.API + "/projects/11/issues/12"): ok(gitlab_item(12, 12)),
        }
        source = self.source(
            wl.GitLabSource, "gitlab", watch_projects=["platform/infra", "not a path!"]
        )
        items, warnings = source.fetch()
        self.assertEqual(
            self.relations(items),
            {
                "gitlab:issue:1": {"assignee", "commented"},
                "gitlab:issue:2": {"assignee"},
                "gitlab:mr:100": {"reviewer"},
                "gitlab:issue:4": {"mentioned_done"},
                "gitlab:mr:101": {"team"},
                "gitlab:issue:9": {"commented"},
                "gitlab:issue:12": {"commented"},
            },
        )
        self.assertEqual(len(warnings), 1)
        self.assertIn("ignoring watch_projects entry", warnings[0])
        draft = next(item for item in items if item["id"] == "gitlab:mr:100")
        self.assertEqual(
            (draft["status"], draft["state"], draft["priority"]), ("Draft", "todo", "high")
        )
        self.assertTrue(draft["url"].startswith(f"{self.base}/gitlab/platform/infra/-/"))
        self.assertTrue(all(entry["auth"] == "secret" for entry in Routes.log))

    def test_followed_boards_add_open_issues_in_their_scope(self) -> None:
        Routes.routes = {
            ("GET", self.API + "/projects/platform%2Finfra/boards/7"): ok(
                {"id": 7, "labels": [{"name": "team-a"}], "milestone": {"title": "Upcoming"}}
            ),
            ("GET", self.API + "/projects/platform%2Finfra/issues"): ok([gitlab_item(300, 3)]),
            ("GET", self.API + "/groups/platform/boards"): ok([{"id": 1, "labels": []}]),
            ("GET", self.API + "/groups/platform/issues"): ok([gitlab_item(301, 4)]),
        }
        gitlab = f"{self.base}/gitlab"
        source = self.source(
            wl.GitLabSource,
            "gitlab",
            watch_boards=[
                f"{gitlab}/platform/infra/-/boards/7",
                f"{gitlab}/groups/platform/-/boards",
                "https://gitlab.elsewhere.example/platform/infra/-/boards/7",
            ],
        )
        added: list[tuple[Any, str, str]] = []
        warnings: list[str] = []
        source._add_board_issues(
            lambda raw, kind, rel: added.append((raw["id"], kind, rel)), warnings
        )
        self.assertEqual(sorted(added), [(300, "issue", "board"), (301, "issue", "board")])
        self.assertEqual(len(warnings), 1)
        self.assertIn("ignoring watch_boards entry", warnings[0])
        query = next(e["query"] for e in Routes.log if e["path"].endswith("infra/issues"))
        self.assertEqual(
            (query["state"], query["labels"], query["milestone"]), ("opened", "team-a", "Upcoming")
        )
        group_query = next(e["query"] for e in Routes.log if e["path"].endswith("form/issues"))
        self.assertNotIn("labels", group_query)

    def test_work_items_count_as_issues(self) -> None:
        task = gitlab_item(20, 4, issue_type="task", type="TASK")
        epic = gitlab_item(
            21,
            3,
            issue_type="epic",
            type="EPIC",
            project_id=None,
            references={"full": "platform&3"},
            web_url=f"{self.base}/gitlab/groups/platform/-/epics/3",
        )
        todos = [
            {"target_type": "WorkItem", "action_name": "assigned", "target": task},
            {"target_type": "WorkItem", "action_name": "mentioned", "target": epic},
        ]
        events = [{"project_id": 11, "note": {"noteable_type": "WorkItem", "noteable_iid": 9}}]
        Routes.routes = {
            ("GET", self.API + "/user"): ok({"id": 42}),
            ("GET", self.API + "/todos"): lambda q, _: ok(
                todos if q.get("state") == "pending" else []
            ),
            ("GET", self.API + "/events"): ok(events),
            ("GET", self.API + "/projects/11/issues/9"): ok(gitlab_item(9, 9)),
        }
        items, _ = self.source(wl.GitLabSource, "gitlab", include_merge_requests=False).fetch()
        by_id = {item["id"]: item for item in items}
        self.assertEqual(set(by_id["gitlab:issue:20"]["relations"]), {"assignee"})
        self.assertEqual(by_id["gitlab:issue:20"]["type"], "Task")
        self.assertEqual(set(by_id["gitlab:issue:9"]["relations"]), {"commented"})
        epic_item = by_id["gitlab:issue:21"]
        self.assertEqual(
            (epic_item["key"], epic_item["project"], epic_item["type"]),
            ("platform&3", "platform", "Epic"),
        )
        self.assertTrue(epic_item["url"].endswith("/groups/platform/-/epics/3"))

    def test_merge_requests_can_be_turned_off(self) -> None:
        Routes.routes[("GET", self.API + "/user")] = ok({"id": 42})
        source = self.source(wl.GitLabSource, "gitlab", include_merge_requests=False)
        _, warnings = source.fetch()
        self.assertFalse(any(e["path"].endswith("/merge_requests") for e in Routes.log))
        self.assertTrue(warnings)

    def test_nested_scoped_labels_and_issue_types(self) -> None:
        source = self.source(wl.GitLabSource, "gitlab")
        labels = ["workflow::backend::In review", "priority::high"]
        item = source.normalize(gitlab_item(1, 1, labels=labels, issue_type="test_case"), "issue")
        self.assertEqual((item["status"], item["state"]), ("In review", "in_progress"))
        self.assertEqual((item["priority"], item["type"]), ("high", "Test case"))

    def test_board_lists_set_status(self) -> None:
        def board(*names: str) -> dict[str, Any]:
            lists = [{"position": i, "label": {"name": n}} for i, n in enumerate(names)]
            return {"lists": lists}

        Routes.routes = {
            ("GET", self.API + "/projects/platform%2Finfra/boards"): ok([board("Doing", "Test")]),
            ("GET", self.API + "/groups/platform/boards"): status(403),
        }
        source = self.source(wl.GitLabSource, "gitlab")
        both = source.normalize(gitlab_item(1, 1, labels=["test", "Doing", "bug"]), "issue")
        plain = source.normalize(gitlab_item(2, 2, labels=["bug"]), "issue")
        scoped = source.normalize(gitlab_item(3, 3, labels=["workflow::Ready", "Test"]), "issue")
        warnings: list[str] = []
        source._apply_board_lists([both, plain, scoped], warnings)
        self.assertEqual((both["status"], both["state"]), ("Test", "in_progress"))
        self.assertEqual((plain["status"], plain["state"]), ("Open", "todo"))
        self.assertEqual(scoped["status"], "Ready")
        self.assertEqual(warnings, [])

    def test_normalize_states(self) -> None:
        source = self.source(wl.GitLabSource, "gitlab")
        merged = source.normalize(gitlab_item(1, 1, "mr", state="merged"), "mr")
        self.assertEqual((merged["status"], merged["state"]), ("Merged", "done"))
        doing = source.normalize(gitlab_item(2, 2, labels=[{"name": "Status::Doing"}]), "issue")
        self.assertEqual((doing["status"], doing["state"]), ("Doing", "in_progress"))
        unknown = source.normalize(
            gitlab_item(3, 3, references={}, project_id=5, web_url="javascript:x"), "issue"
        )
        self.assertEqual(unknown["url"], source.base)


# --------------------------------------------------------------------------- #
# Bitbucket
# --------------------------------------------------------------------------- #


def bitbucket_pr(
    pr_id: int, author: str = "anna", project: str = "PLAT", repo: str = "repo", **extra: Any
) -> dict[str, Any]:
    pr = test_onmyplate.bitbucket_pr(pr_id, author)
    pr["toRef"]["repository"] = {"slug": repo, "project": {"key": project}}
    pr["links"] = {"self": [{"href": "https://elsewhere.example/pr"}]}
    pr.update(extra)
    return pr


class BitbucketTests(RoutesCase):
    API = "/bitbucket/rest/api/latest"

    def activities(self, *comments: dict[str, Any]) -> Reply:
        values = [{"action": "COMMENTED", "comment": c} for c in comments]
        return ok({"values": values, "isLastPage": True})

    def test_fetch_combines_every_signal(self) -> None:
        def dashboard(query: dict[str, str], _: Any) -> Reply:
            role = query["role"]
            if role == "AUTHOR":
                if query["start"] == "0":
                    page = [bitbucket_pr(1, "sam", reviewers=[])]
                    return ok({"values": page, "isLastPage": False, "nextPageStart": 1})
                return ok({"values": [bitbucket_pr(4, "sam")], "isLastPage": True})
            if role == "REVIEWER" and "participantStatus" in query:
                return ok({"values": [bitbucket_pr(2)], "isLastPage": True})
            if role == "REVIEWER":
                values = [bitbucket_pr(2), bitbucket_pr(3, state="MERGED"), bitbucket_pr(6)]
                return ok({"values": values, "isLastPage": True})
            return ok({"values": [], "isLastPage": True})

        mention = {"author": {"name": "anna"}, "text": "@sam ok?", "createdDate": NOW_MS - 5000}
        mention["comments"] = [{"author": {"name": "SAM"}, "text": "yes", "createdDate": NOW_MS}]
        prs = self.API + "/projects/PLAT/repos/repo/pull-requests"
        Routes.routes = {
            ("GET", self.API + "/application-properties"): ok({}, **{"X-AUSERNAME": "sam"}),
            ("GET", self.API + "/dashboard/pull-requests"): dashboard,
            ("GET", self.API + "/projects/PLAT/repos"): ok(
                {"values": [{"slug": "repo"}], "isLastPage": True}
            ),
            ("GET", prs): ok(
                {"values": [bitbucket_pr(5, description="@sam please look")], "isLastPage": True}
            ),
            ("GET", self.API + "/projects/OPS/repos/tools/pull-requests"): status(404),
            ("GET", prs + "/2/activities"): self.activities(mention),
            ("GET", prs + "/5/activities"): self.activities(),
            ("GET", prs + "/6/activities"): self.activities(),
        }
        source = self.source(
            wl.BitbucketSource, "bitbucket", watch_repos=["PLAT", "OPS/tools", "bad entry!"]
        )
        items, warnings = source.fetch()
        self.assertEqual(
            self.relations(items),
            {
                "bitbucket:pr:PLAT:repo:1": {"reporter"},
                "bitbucket:pr:PLAT:repo:4": {"reporter"},
                "bitbucket:pr:PLAT:repo:2": {"reviewer", "mentioned_done"},
                "bitbucket:pr:PLAT:repo:5": {"mentioned"},
                "bitbucket:pr:PLAT:repo:6": {"participant"},
            },
        )
        self.assertEqual(len(warnings), 2)
        self.assertIn("ignoring watch_repos entry", warnings[0])
        self.assertIn("OPS/tools", warnings[1])
        own = next(item for item in items if item["id"] == "bitbucket:pr:PLAT:repo:1")
        self.assertEqual(
            own["url"], f"{source.base}/projects/PLAT/repos/repo/pull-requests/1/overview"
        )
        self.assertNotIn("_scan", own)

    def test_mention_results_are_cached_per_version(self) -> None:
        prs = self.API + "/projects/PLAT/repos/repo/pull-requests"
        mention = {"author": {"name": "anna"}, "text": "cc @sam", "createdDate": NOW_MS}
        Routes.routes[("GET", prs + "/1/activities")] = self.activities(mention)
        source = self.source(wl.BitbucketSource, "bitbucket")
        scan = ("PLAT", "repo", 1, NOW_MS, "")
        self.assertEqual(source._mention_state(scan, "sam"), "open")
        self.assertEqual(source._mention_state(scan, "sam"), "open")
        self.assertEqual(len(Routes.log), 1)
        self.assertEqual(source._mention_state((*scan[:3], NOW_MS + 1, ""), "sam"), "open")
        self.assertEqual(len(Routes.log), 2)

    def test_mentions_at_the_end_of_a_sentence_count(self) -> None:
        prs = self.API + "/projects/PLAT/repos/repo/pull-requests"
        source = self.source(wl.BitbucketSource, "bitbucket")
        cases = {
            "Thanks @sam.": "open",
            "@sam, could you": "open",
            "(@sam)": "open",
            '@"sam" please': "open",
            "@sam.smith knows": None,
            "@samuel knows": None,
            "mail sam@sam.example": None,
        }
        for number, (text, expected) in enumerate(cases.items(), 1):
            with self.subTest(text=text):
                comment = {"author": {"name": "anna"}, "text": text, "createdDate": NOW_MS}
                Routes.routes[("GET", f"{prs}/{number}/activities")] = self.activities(comment)
                scan = ("PLAT", "repo", number, NOW_MS, "")
                self.assertEqual(source._mention_state(scan, "sam"), expected)

    def test_username_header_is_decoded(self) -> None:
        source = self.source(wl.BitbucketSource, "bitbucket", username="fallback")
        Routes.routes[("GET", self.API + "/application-properties")] = ok(
            {}, **{"X-AUSERNAME": "j%C3%BCrgen%40corp"}
        )
        self.assertEqual(source._whoami(), "jürgen@corp")
        Routes.routes[("GET", self.API + "/application-properties")] = ok(
            {}, **{"X-AUSERNAME": "anonymous"}
        )
        self.assertEqual(source._whoami(), "fallback")

    def test_jira_keys_from_title_branch_and_links(self) -> None:
        source = self.source(wl.BitbucketSource, "bitbucket")
        pr = {
            "id": 5,
            "title": "PLAT-12: fix export (UTF-8)",
            "fromRef": {"displayId": "feature/PLAT-12-export"},
            "description": "Needs UTF-8. See https://jira.example/browse/OPS-7 and PLAT-99.",
        }
        item = source.normalize(pr)
        self.assertEqual(item["jira_keys"], ["PLAT-12", "UTF-8", "OPS-7"])
        coerced = wl.coerce_item({**item, "jira_keys": ["OPS-7", "<b>", 3]})
        self.assertEqual(coerced["jira_keys"], ["OPS-7"])

    def test_all_dashboard_queries_failing_is_an_error(self) -> None:
        source = self.source(wl.BitbucketSource, "bitbucket")
        with self.assertRaisesRegex(wl.SourceError, "HTTP 404"):
            source.fetch()

    def test_normalize_states(self) -> None:
        source = self.source(wl.BitbucketSource, "bitbucket")
        cases = {
            "MERGED": ({"state": "MERGED"}, ("Merged", "done")),
            "draft": ({"draft": True}, ("Draft", "todo")),
            "needs work": (
                {"reviewers": [{"user": {"name": "a"}, "status": "NEEDS_WORK"}]},
                ("Needs work", "in_progress"),
            ),
            "approved": (
                {"reviewers": [{"user": {"name": "a"}, "status": "APPROVED"}]},
                ("1/1 approved", "in_progress"),
            ),
        }
        for name, (extra, expected) in cases.items():
            with self.subTest(name):
                item = source.normalize(bitbucket_pr(1, **extra))
                self.assertEqual((item["status"], item["state"]), expected)
        evil = source.normalize(bitbucket_pr(1, project="../x"))
        self.assertEqual(evil["url"], source.base)


class SourceSetupTests(unittest.TestCase):
    def test_token_from_environment_wins(self) -> None:
        cfg = wl.merge_config(
            wl.DEFAULT_CONFIG, {"gitlab": {"url": "https://gitlab.example", "token": "file"}}
        )["gitlab"]
        with mock.patch.dict(os.environ, {"ONMYPLATE_GITLAB_TOKEN": "env"}):
            self.assertEqual(wl.GitLabSource(cfg, wl.HttpClient({})).token, "env")

    def test_missing_token(self) -> None:
        cfg = wl.merge_config(wl.DEFAULT_CONFIG, {})["bitbucket"]
        no_env = mock.patch.dict(os.environ, {}, clear=True)
        with no_env, self.assertRaisesRegex(wl.SourceError, "no token"):
            wl.BitbucketSource(cfg, wl.HttpClient({}))

    def test_insecure_http_needs_opt_in(self) -> None:
        cfg = {"url": "http://gitlab.example", "token": "t"}
        with self.assertRaisesRegex(wl.SourceError, "https://"):
            wl.GitLabSource(cfg, wl.HttpClient({}))
        wl.GitLabSource({**cfg, "allow_insecure_http": True}, wl.HttpClient({}))


if __name__ == "__main__":
    unittest.main()
