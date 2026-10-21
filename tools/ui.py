#!/usr/bin/env python3
"""Edit the embedded web UI of onmyplate.py as a separate file.

    python3 tools/ui.py extract   # onmyplate.py -> build/ui.html and build/app.js
    python3 tools/ui.py embed     # build/ui.html -> onmyplate.py
    python3 tools/ui.py check     # fail if build/ui.html differs from onmyplate.py
    python3 tools/ui.py demo      # write docs/demo/index.html, a browser-only demo

The UI lives inside onmyplate.py so the tool stays a single file. This helper
lets editors and ESLint work on real HTML and JavaScript files.
"""

from __future__ import annotations

import base64
import contextlib
import http.client
import importlib.util
import json
import re
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "onmyplate.py"
BUILD = ROOT / "build"
DEMO = ROOT / "docs" / "demo" / "index.html"
START = 'INDEX_HTML = r"""'
END = '"""'


def split_script() -> tuple[str, str, str]:
    text = SCRIPT.read_text("utf-8")
    head, rest = text.split(START, 1)
    html, tail = rest.split(END, 1)
    return head, html, tail


def extract() -> None:
    _, html, _ = split_script()
    BUILD.mkdir(exist_ok=True)
    (BUILD / "ui.html").write_text(html, "utf-8")
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    (BUILD / "app.js").write_text(script, "utf-8")
    print(f"Wrote {BUILD / 'ui.html'} and {BUILD / 'app.js'}")


def embed() -> None:
    html = (BUILD / "ui.html").read_text("utf-8")
    if END in html:
        sys.exit('ui.html must not contain """')
    head, _, tail = split_script()
    SCRIPT.write_text(head + START + html + END + tail, "utf-8")
    print(f"Embedded {BUILD / 'ui.html'} into {SCRIPT.name}")


def check() -> None:
    _, html, _ = split_script()
    if (BUILD / "ui.html").read_text("utf-8") != html:
        sys.exit("build/ui.html differs from onmyplate.py: run tools/ui.py embed")


# The browser demo is recorded from the real server: demo() starts
# OnMyPlateServer in demo mode, asks it every API path the UI mentions, and this
# script replays those responses. It knows no endpoint by name, so the demo
# cannot drift from the real API. Its only own rules: a POST that the server
# accepted is remembered for the tab and merged into the next GET of the same
# path, and dates move with the calendar so the demo never looks stale.
DEMO_REPLAY = """<script>
(() => {
  const RECORDED = __RECORDED__;
  const DAY = 86400000;
  const KEY = "onmyplate.demo:__RECORDED_AT__:";  // a new build starts with fresh demo state
  const shift = Math.floor((Date.now() - __RECORDED_AT__) / DAY) * DAY;
  const DATE = /^\\d{4}-\\d{2}-\\d{2}(T[\\d:.]+(Z|[+-]\\d{2}:\\d{2})?)?$/;
  const move = (v) => {
    if (shift && typeof v === "string" && DATE.test(v)) {
      const moved = new Date(Date.parse(v.length === 10 ? v + "T12:00:00Z" : v) + shift).toISOString();
      return v.length === 10 ? moved.slice(0, 10) : moved;
    }
    if (Array.isArray(v)) return v.map(move);
    if (v && typeof v === "object") return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, move(x)]));
    return v;
  };
  const saved = (path) => { try { return JSON.parse(sessionStorage.getItem(KEY + path) || "null"); } catch { return null; } };
  const save = (path, body) => { try { sessionStorage.setItem(KEY + path, JSON.stringify(body)); } catch { /* demo state stays in memory */ } };
  const realFetch = window.fetch.bind(window);
  window.fetch = async (input, opts = {}) => {
    const path = new URL(typeof input === "string" ? input : input.url, location.href).pathname;
    if (!path.startsWith("/api/")) return realFetch(input, opts);
    const method = (opts.method || "GET").toUpperCase();
    const rec = RECORDED[method + " " + path] || RECORDED["*"];
    let body = rec.base64 ? Uint8Array.from(atob(rec.body), (c) => c.charCodeAt(0)) : rec.body;
    if (!rec.base64 && rec.headers["Content-Type"].startsWith("application/json")) {
      let data = move(JSON.parse(rec.body));
      if (method === "POST" && rec.status < 300 && typeof opts.body === "string") save(path, JSON.parse(opts.body));
      const state = method === "GET" ? saved(path) : null;
      if (state && data && typeof data === "object") data = { ...data, ...state };
      body = JSON.stringify(data);
    }
    return new Response(body, { status: rec.status, headers: rec.headers });
  };
})();
</script>
"""
# The only visible addition to the real UI: a band that says this is a demo and
# links back to the website and its Impressum. On wide screens the UI fills the
# window, so the band takes its height from the layout instead of overlapping it.
DEMO_BANNER = """<style>
.demo-banner { position: sticky; top: 0; z-index: 20; display: flex; justify-content: center; align-items: center;
  gap: 12px; flex-wrap: wrap; padding: 6px 12px; background: var(--accent); color: var(--panel); font-size: 12px; }
.demo-banner a { color: var(--panel); font-weight: 600; text-decoration: underline; }
@media (max-width: 600px) { .demo-banner .demo-long { display: none; } }
@media (min-width: 1181px) {
  body { display: flex; flex-direction: column; }
  body > .demo-banner { flex: none; }
  body > .wrap { flex: 1 1 auto; height: auto; min-height: 0; width: 100%; }
}
</style>
<div class="demo-banner" role="note"><span><strong>Demo</strong> with made-up data<span class="demo-long">, running only in your browser</span></span><a href="../">About On My Plate</a><a href="../impressum.html">Impressum</a></div>
"""
DEMO_TASKS = [
    ("Prepare the sprint review demo", {"due": 1, "created": -2}),
    ("Write the runbook for this ticket", {"created": -4}),
    ("Book a room for the team offsite", {"created": -6, "done_at": -1}),
]
RECORDED_HEADERS = ("Content-Type", "Content-Disposition")


def load_module() -> Any:
    spec = importlib.util.spec_from_file_location("onmyplate", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["onmyplate"] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def demo_server(app: Any) -> Iterator[Callable[..., dict[str, Any]]]:
    """Run the real server in demo mode with the demo tasks; yield a request function."""
    data_dir = tempfile.mkdtemp(prefix="onmyplate-demo-")
    app.set_data_dir(data_dir)
    store = app.Store(app.merge_config(app.DEFAULT_CONFIG, {}), demo=True)
    store.refresh()
    server = app.OnMyPlateServer(0, store, app.load_secret())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    def call(method: str, path: str, body: object = None) -> dict[str, Any]:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        headers = {"Host": f"127.0.0.1:{port}", app.TOKEN_HEADER: server.session_token}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, path, payload, headers)
        response = conn.getresponse()
        raw = response.read()
        recorded = {
            "status": response.status,
            "headers": {
                h: response.getheader(h) for h in RECORDED_HEADERS if response.getheader(h)
            },
        }
        try:
            recorded["body"] = raw.decode("utf-8")
        except UnicodeDecodeError:
            recorded["body"], recorded["base64"] = base64.b64encode(raw).decode(), True
        return recorded

    try:
        today = datetime.now(UTC).date()
        ref = next(i["id"] for i in store.snapshot()["items"] if i["source"] == "jira")
        tasks = []
        for number, (text, days) in enumerate(DEMO_TASKS, 1):
            task: dict[str, Any] = {
                "id": f"demo{number:04d}",
                "text": text,
                "done": "done_at" in days,
            }
            task.update({key: str(today + timedelta(days=d)) for key, d in days.items()})
            tasks.append(task)
        tasks[1]["ref"] = ref
        call("POST", "/api/todos", {"todos": tasks})
        yield call
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(data_dir, ignore_errors=True)


def api_paths(page: str) -> list[str]:
    """Every API path the UI's script mentions."""
    script = page.split("<script>", 1)[1].split("</script>", 1)[0]
    return sorted(set(re.findall(r'"(/api/[\w/-]+)"', script)))


def record_demo(app: Any) -> tuple[str, dict[str, dict[str, Any]]]:
    """Record the page and the GET and POST response of every API path the UI uses."""
    with demo_server(app) as call:
        page = call("GET", "/")["body"]
        recorded: dict[str, dict[str, Any]] = {}
        for path in api_paths(page):
            got = recorded[f"GET {path}"] = call("GET", path)
            # Posting back what GET returned leaves the server's state unchanged.
            again: Any = {}
            if got["status"] == 200 and got["headers"]["Content-Type"].startswith(
                "application/json"
            ):
                again = json.loads(got["body"])
            recorded[f"POST {path}"] = call("POST", path, again if isinstance(again, dict) else {})
        recorded["*"] = call("GET", "/api/not-a-path")  # the answer for anything else
        return page, recorded


def build_demo_page(page: str, recorded: dict[str, dict[str, Any]], recorded_at_ms: int) -> str:
    replay = DEMO_REPLAY.replace(
        "__RECORDED__", json.dumps(recorded, ensure_ascii=False).replace("</", "<\\/")
    ).replace("__RECORDED_AT__", str(recorded_at_ms))
    page = page.replace(
        "<title>On My Plate</title>",
        '<title>On My Plate – Demo</title>\n<meta name="robots" content="noindex">',
        1,
    )
    body = re.search(r"<body[^>]*>\n", page)
    page = page[: body.end()] + DEMO_BANNER + page[body.end() :]
    return page.replace("<script>", replay + "<script>", 1)


def demo() -> None:
    recorded_at_ms = int(time.time() * 1000)
    page, recorded = record_demo(load_module())
    DEMO.parent.mkdir(parents=True, exist_ok=True)
    DEMO.write_text(build_demo_page(page, recorded, recorded_at_ms), "utf-8")
    print(f"Wrote {DEMO} ({len(recorded)} recorded responses)")


if __name__ == "__main__":
    commands = {"extract": extract, "embed": embed, "check": check, "demo": demo}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        sys.exit(__doc__)
    commands[sys.argv[1]]()
