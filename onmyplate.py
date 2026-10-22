#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""On My Plate: a local dashboard for Jira, GitLab and Bitbucket.

A single-file tool (standard library only, Python 3.11+) that collects the issues
and pull requests you are involved in and serves a dashboard on 127.0.0.1.
Apart from read-only API calls to the configured instances, nothing leaves the
machine.

Usage::

    python3 onmyplate.py                   # first run creates onmyplate.config.json
    python3 onmyplate.py --demo            # try the UI with sample data
    python3 onmyplate.py --export FILE.zip # backup for moving to another machine
    python3 onmyplate.py --import FILE.zip
    python3 onmyplate.py --rotate-key      # sign out every browser

All data lives in one folder: next to the script (``~/.onmyplate`` when installed
with pip or pipx), or wherever ``--data-dir`` / ``$ONMYPLATE_HOME`` points::

    onmyplate.config.json   instance URLs and tokens (secret)
    onmyplate.local.json    focus list, notes, seen state, hidden items
    onmyplate.todos.md      personal tasks as a Markdown checklist
    onmyplate.secret        secret from which browser sessions are derived

Tokens may also be supplied via ``ONMYPLATE_JIRA_TOKEN``,
``ONMYPLATE_GITLAB_TOKEN`` and ``ONMYPLATE_BITBUCKET_TOKEN``.

Security model:
    * The server binds to 127.0.0.1. Each browser signs in per start with a one-time
      link from the console; API calls then carry a session token in a header.
    * Host, Origin and Sec-Fetch-Site checks, the token header, a strict CSP and
      a framing ban protect against hostile web pages and other local ports.
    * The HTTP client only issues GET requests (plus the read-only Jira search),
      requires HTTPS and refuses cross-origin redirects, so tokens cannot leak.
    * Files holding secrets or personal data are written with mode 0600.

Sources:
    Jira       Data Center/Server (REST v2, personal access token) and Cloud
               (REST v3). On Data Center, own comments are read from the built-in
               activity stream because JQL cannot query them without plugins.
    GitLab     Self-managed or SaaS (REST v4, token with read_api).
    Bitbucket  Data Center/Server (REST "latest", HTTP access token with read
               access). Mentions are found by reading the comments of known open
               pull requests, as the API has no mention filter.
"""

from __future__ import annotations

import sys

if sys.version_info < (3, 11):  # noqa: UP036 - runs before imports that fail on older Pythons
    sys.exit("On My Plate needs Python 3.11 or newer.")

import argparse
import base64
import getpass
import hashlib
import hmac
import io
import json
import os
import random
import re
import secrets
import shlex
import shutil
import ssl
import stat

# subprocess runs the keychain tool and the user's own token_command
import subprocess  # nosec B404
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

VERSION = "0.1.1"
APP_DIR = Path(__file__).resolve().parent
SOURCE_NAMES = ("jira", "gitlab", "bitbucket")
SOURCE_LABELS = {"jira": "Jira", "gitlab": "GitLab", "bitbucket": "Bitbucket"}
SOURCE_ENV_VARS = {name: f"ONMYPLATE_{name.upper()}_TOKEN" for name in SOURCE_NAMES}

MAX_PAGES = 50
MAX_RESPONSE_BYTES = 20_000_000
MAX_REQUEST_BYTES = 2_000_000
MAX_IMPORT_FILE_BYTES = 50_000_000
MAX_TODOS = 5000

Item = dict[str, Any]
FetchResult = tuple[list[Item], list[str]]

# The project's own license and the licenses of everything it ships, shown under
# ? > About & licenses and by --licenses. Written by tools/third_party_licenses.py.
# BEGIN GENERATED LICENSES - do not edit, run tools/third_party_licenses.py
LICENSES: dict[str, Any] = json.loads(
    r"""
{
 "self": {
  "name": "On My Plate",
  "license": "MIT",
  "text": "MIT License\n\nCopyright (c) 2026 Gilian Rehm\n\nPermission is hereby granted, free of charge, to any person obtaining a copy\nof this software and associated documentation files (the \"Software\"), to deal\nin the Software without restriction, including without limitation the rights\nto use, copy, modify, merge, publish, distribute, sublicense, and/or sell\ncopies of the Software, and to permit persons to whom the Software is\nfurnished to do so, subject to the following conditions:\n\nThe above copyright notice and this permission notice shall be included in all\ncopies or substantial portions of the Software.\n\nTHE SOFTWARE IS PROVIDED \"AS IS\", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR\nIMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,\nFITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE\nAUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER\nLIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,\nOUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE\nSOFTWARE."
 },
 "note": "On My Plate uses only the Python standard library and ships no third-party code, fonts or images. Python itself is installed separately and licensed under the PSF License Agreement (https://docs.python.org/3/license.html).",
 "groups": [
  {
   "title": "Python packages",
   "packages": []
  },
  {
   "title": "Web UI",
   "packages": []
  }
 ]
}
"""
)
# END GENERATED LICENSES


# --------------------------------------------------------------------------- #
# Files and paths
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DataPaths:
    """Locations of all files On My Plate reads and writes."""

    root: Path

    @property
    def config(self) -> Path:
        return self.root / "onmyplate.config.json"

    @property
    def local(self) -> Path:
        return self.root / "onmyplate.local.json"

    @property
    def secret(self) -> Path:
        return self.root / "onmyplate.secret"

    @property
    def todos(self) -> Path:
        return self.root / "onmyplate.todos.md"


PATHS = DataPaths(APP_DIR)
_LOCAL_LOCK = threading.Lock()
_TODOS_LOCK = threading.Lock()


def default_data_dir() -> Path:
    """Next to the script when run as a file, ``~/.onmyplate`` when installed as a package."""
    if any(part in ("site-packages", "dist-packages") for part in APP_DIR.parts):
        return Path.home() / ".onmyplate"
    return APP_DIR


def set_data_dir(folder: str | Path) -> None:
    """Point all data files at ``folder`` and create it if necessary."""
    global PATHS
    root = Path(folder).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    PATHS = DataPaths(root)
    adopt_legacy_files(root)


# The tool was called "Worklist" before its public release.
LEGACY_PREFIX = "worklist."


def adopt_legacy_files(root: Path) -> None:
    """Rename data files written under the old name, unless new ones exist."""
    for name in ("config.json", "local.json", "todos.md", "secret"):
        old, new = root / (LEGACY_PREFIX + name), root / ("onmyplate." + name)
        if old.exists() and not new.exists():
            old.rename(new)
            print(f"  Renamed {old.name} to {new.name}.")


def write_private(path: Path, content: str | bytes) -> None:
    """Write ``content`` atomically to ``path``, readable only by the current user."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    data = content.encode("utf-8") if isinstance(content, str) else content
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)


def is_readable_by_others(path: Path) -> bool:
    """Return True if group or other users have access to ``path`` (POSIX only)."""
    if os.name != "posix" or not path.exists():
        return False
    return bool(path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO))


def untrusted_reason(path: Path) -> str | None:
    """Why another user could change ``path``, or None if only the current user can (POSIX).

    The config decides where tokens are sent, so a file or folder that others can
    write would let them redirect the tokens to their own server.
    """
    if os.name != "posix" or not path.exists():
        return None
    resolved = path.resolve()
    me = os.getuid()
    info = resolved.stat()
    if info.st_uid != me:
        return f"{resolved} belongs to another user"
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return f"{resolved} is writable by other users. Fix: chmod 600 {resolved}"
    # Both the folder of a symlink and the folder of its target must be safe.
    for parent in dict.fromkeys((path.absolute().parent, resolved.parent)):
        folder = parent.stat()
        if folder.st_uid not in (me, 0):
            return f"its folder {parent} belongs to another user"
        if folder.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and not folder.st_mode & stat.S_ISVTX:
            return f"its folder {parent} is writable by other users, who could replace the file"
    return None


def load_local() -> dict[str, Any]:
    with _LOCAL_LOCK:
        try:
            data = json.loads(PATHS.local.read_text("utf-8"))
        except (OSError, ValueError):
            return {"pins": [], "notes": {}}
        return data if isinstance(data, dict) else {}


def save_local(data: dict[str, Any]) -> None:
    with _LOCAL_LOCK:
        write_private(PATHS.local, json.dumps(data, ensure_ascii=False, indent=1))


def load_secret(rotate: bool = False) -> str:
    """Return the per-machine secret from which UI session tokens are derived."""
    key = ""
    if not rotate:
        try:
            key = PATHS.secret.read_text("utf-8").strip()
        except OSError:
            key = ""
    if len(key) >= 32:
        return key
    key = secrets.token_urlsafe(32)
    write_private(PATHS.secret, key)
    return key


def session_token(secret: str, boot: str) -> str:
    """Token the browser presents on every API call, valid until the server stops.

    ``boot`` is random per start. A token that leaks from browser storage, for
    example to a page that another local user serves on the same port while
    On My Plate is not running, is useless against the next start.
    """
    message = b"onmyplate-session-v3:" + boot.encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------- #
# Text helpers
# --------------------------------------------------------------------------- #


def sanitize(text: object, limit: int = 300) -> str:
    """Replace control characters such as terminal escapes in text from remote servers."""
    return "".join(ch if ch.isprintable() else " " for ch in str(text))[:limit]


def to_iso(value: str | None) -> str | None:
    """Normalize Jira/GitLab timestamps to ISO 8601 with a colon in the UTC offset."""
    if not value or len(value) == 10:
        return value or None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(value.replace("Z", "+0000"), fmt).isoformat()  # noqa: DTZ007 - formats carry %z
        except ValueError:
            continue
    return value


def display_name(user: dict[str, Any] | None) -> str:
    if not user:
        return ""
    return user.get("displayName") or user.get("name") or user.get("username") or ""


def redact_url(url: str) -> str:
    """Drop the query string so error messages never echo request parameters."""
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def prune_relations(relations: set[str]) -> None:
    """Drop generic relations when a more specific one describes the item."""
    generic = {"participant", "team"}
    if relations - generic:
        relations.difference_update(generic)
    elif len(relations) > 1:
        relations.discard("participant")


# --------------------------------------------------------------------------- #
# Personal tasks (Markdown checklist, compatible with the Obsidian Tasks format)
#   - [ ] Text 📅 2026-10-30 ➕ 2026-10-21 <!-- id:ab12cd34 ref:jira:PLAT-1 -->
# --------------------------------------------------------------------------- #

TODO_LINE = re.compile(r"^\s*[-*]\s+\[([ xX])\]\s+(.*)$")
TODO_META = re.compile(r"\s*<!--(.*?)-->\s*$")
TODO_DATES = (
    ("due", re.compile(r"\s*📅\s*(\d{4}-\d{2}-\d{2})")),
    ("done_at", re.compile(r"\s*✅\s*(\d{4}-\d{2}-\d{2})")),
    ("created", re.compile(r"\s*➕\s*(\d{4}-\d{2}-\d{2})")),
)
RE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RE_TODO_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
RE_ITEM_REF = re.compile(r"^(jira|gitlab|bitbucket):[A-Za-z0-9_.:~-]{1,200}$")
TODOS_HEADER = (
    "# On My Plate – my tasks\n\n"
    "<!-- This file is managed by On My Plate. New lines like "
    "'- [ ] Task 📅 2026-10-30' are picked up (the date is optional). Any other text "
    "outside task lines is lost when On My Plate saves. -->\n"
)


MARKDOWN_SPECIAL = re.compile(r"([\\<>\[\]])")
MARKDOWN_ESCAPED = re.compile(r"\\([\\<>\[\]])")


def _todo_text(text: object) -> str:
    """Single-line task text that cannot close or forge the metadata comment."""
    flat = sanitize(str(text).replace("\r", " ").replace("\n", " "), 1000)
    return flat.replace("<!--", "<!- -").replace("-->", "- ->").strip()


def _escape_markdown(text: str) -> str:
    """Keep task text inert in Markdown viewers (no HTML, links or remote images)."""
    return MARKDOWN_SPECIAL.sub(r"\\\1", text)


def parse_todos(text: str) -> tuple[list[Item], bool]:
    """Parse the task file; the flag is True if hand-written lines need new IDs."""
    todos: list[Item] = []
    needs_rewrite = False
    seen_ids: set[str] = set()
    for line in text.splitlines():
        match = TODO_LINE.match(line)
        if not match:
            continue
        rest, meta = match.group(2), {}
        meta_match = TODO_META.search(rest)
        if meta_match:
            for token in meta_match.group(1).split():
                if ":" in token:
                    key, value = token.split(":", 1)
                    meta[key] = value
            rest = rest[: meta_match.start()]
        todo: Item = {"done": match.group(1).lower() == "x"}
        for key, pattern in TODO_DATES:
            date_match = pattern.search(rest)
            if date_match:
                todo[key] = date_match.group(1)
                rest = rest[: date_match.start()] + rest[date_match.end() :]
        todo["text"] = _todo_text(MARKDOWN_ESCAPED.sub(r"\1", rest))
        if not todo["text"]:
            continue
        todo_id = meta.get("id", "")
        if not RE_TODO_ID.match(todo_id) or todo_id in seen_ids:
            todo_id, needs_rewrite = secrets.token_hex(4), True
        seen_ids.add(todo_id)
        todo["id"] = todo_id
        if RE_ITEM_REF.match(meta.get("ref", "")):
            todo["ref"] = meta["ref"]
        todos.append(todo)
    return todos, needs_rewrite


def render_todos(todos: list[Item]) -> str:
    def line(todo: Item) -> str:
        text = _escape_markdown(_todo_text(todo["text"]))
        parts = [f"- [{'x' if todo.get('done') else ' '}] {text}"]
        if todo.get("due"):
            parts.append(f"📅 {todo['due']}")
        if todo.get("created"):
            parts.append(f"➕ {todo['created']}")
        if todo.get("done") and todo.get("done_at"):
            parts.append(f"✅ {todo['done_at']}")
        meta = f"id:{todo['id']}" + (f" ref:{todo['ref']}" if todo.get("ref") else "")
        return " ".join(parts) + f" <!-- {meta} -->"

    open_lines = [line(t) for t in todos if not t.get("done")]
    done_lines = [line(t) for t in todos if t.get("done")]
    body = ["", "## Open", "", *open_lines, "", "## Done", "", *done_lines]
    return TODOS_HEADER + "\n".join(body).rstrip() + "\n"


def validate_todos(items: object) -> list[Item]:
    """Validate tasks posted by the UI; raise ValueError on malformed input."""
    if not isinstance(items, list) or len(items) > MAX_TODOS:
        raise TypeError("todos must be a list")
    result: list[Item] = []
    ids: set[str] = set()
    for raw in items:
        if not isinstance(raw, dict) or not isinstance(raw.get("text"), str):
            raise TypeError("bad todo")
        todo_id = raw.get("id")
        if not isinstance(todo_id, str) or not RE_TODO_ID.match(todo_id) or todo_id in ids:
            raise ValueError("bad id")
        ids.add(todo_id)
        todo: Item = {
            "id": todo_id,
            "text": _todo_text(raw["text"]),
            "done": raw.get("done") is True,
        }
        if not todo["text"]:
            continue
        for key in ("due", "created", "done_at"):
            if isinstance(raw.get(key), str) and RE_DATE.match(raw[key]):
                todo[key] = raw[key]
        if isinstance(raw.get("ref"), str) and RE_ITEM_REF.match(raw["ref"]):
            todo["ref"] = raw["ref"]
        result.append(todo)
    return result


def load_todos() -> list[Item]:
    with _TODOS_LOCK:
        try:
            text = PATHS.todos.read_text("utf-8")
        except FileNotFoundError:
            return []
        todos, needs_rewrite = parse_todos(text)
        if needs_rewrite:
            write_private(PATHS.todos, render_todos(todos))
        return todos


def save_todos(todos: list[Item]) -> None:
    with _TODOS_LOCK:
        write_private(PATHS.todos, render_todos(todos))


def migrate_legacy_todos() -> None:
    """Move tasks that pre-release versions kept in onmyplate.local.json to the task file."""
    if PATHS.todos.exists():
        return
    local = load_local()
    legacy = local.pop("todos", None)
    if not legacy:
        return
    todos: list[Item] = []
    for raw in legacy:
        if not isinstance(raw, dict) or not isinstance(raw.get("text"), str):
            continue
        valid_id = RE_TODO_ID.match(str(raw.get("id", "")))
        todo: Item = {
            "id": raw["id"] if valid_id else secrets.token_hex(4),
            "text": raw["text"],
            "done": raw.get("done") is True,
        }
        if isinstance(raw.get("created"), str):
            todo["created"] = raw["created"][:10]
        if isinstance(raw.get("ref"), str):
            todo["ref"] = raw["ref"]
        todos.append(todo)
    save_todos(validate_todos(todos))
    save_local(local)
    print(f"  Moved {len(todos)} tasks to {PATHS.todos.name}.")


# --------------------------------------------------------------------------- #
# Backup and migration between machines
# --------------------------------------------------------------------------- #

BACKUP_FILES = ("onmyplate.todos.md", "onmyplate.local.json")
CONFIG_FILE = "onmyplate.config.json"
BACKUP_README = """On My Plate backup
===============
Contains your tasks (onmyplate.todos.md) and local settings
(focus, notes, seen state, hidden items), {tokens}.

On the new machine:
  1. Put onmyplate.py into a folder (or use --data-dir / ONMYPLATE_HOME)
  2. python3 onmyplate.py --import <this-zip>
  3. python3 onmyplate.py   → create or check the config, enter new tokens
"""


def make_backup(include_config: bool = False) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        with _TODOS_LOCK, _LOCAL_LOCK:
            for path in (PATHS.todos, PATHS.local):
                if path.exists():
                    archive.writestr(path.name, path.read_bytes())
        if include_config and PATHS.config.exists():
            archive.writestr(CONFIG_FILE, PATHS.config.read_bytes())
        tokens = (
            "plus the config WITH tokens (keep this file safe)"
            if include_config
            else "without config or tokens"
        )
        archive.writestr("README.txt", BACKUP_README.format(tokens=tokens))
    return buffer.getvalue()


def import_backup(zip_path: Path) -> tuple[list[str], list[str], str]:
    """Restore known files from a backup; existing files are kept as ``*.bak-<stamp>``."""
    allowed = {*BACKUP_FILES, CONFIG_FILE}
    legacy = {LEGACY_PREFIX + name.split(".", 1)[1]: name for name in allowed}
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    restored: list[str] = []
    backed_up: list[str] = []
    with zipfile.ZipFile(zip_path) as archive:
        members = {legacy.get(entry, entry): entry for entry in archive.namelist()}
        names = [name for name in members if name in allowed]
        if not names:
            raise SystemExit("The ZIP contains no On My Plate data.")
        for name in names:
            if archive.getinfo(members[name]).file_size > MAX_IMPORT_FILE_BYTES:
                raise SystemExit(f"{name} is implausibly large, import aborted.")
            data = archive.read(members[name])
            if name.endswith(".json"):
                json.loads(data)
            target = PATHS.root / name
            if target.exists():
                write_private(target.with_name(f"{name}.bak-{stamp}"), target.read_bytes())
                backed_up.append(name)
            write_private(target, data)
            restored.append(name)
    return restored, backed_up, stamp


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG: dict[str, Any] = {
    "port": 8765,
    "refresh_minutes": 10,
    "verify_ssl": True,
    "ca_bundle": None,
    "timeout_seconds": 30,
    "jira": {
        "enabled": True,
        "url": "https://jira.your-company.example",
        "type": "auto",
        "email": "first.last@your-company.example",
        "token_command": None,
        "scoped_token": True,
        "cloud_id": None,
        "include_done": False,
        "comment_lookback_days": 90,
        "participation_jql": None,
        "scope_jql": "",
        "max_issues_per_query": 500,
    },
    "gitlab": {
        "enabled": True,
        "url": "https://gitlab.your-company.example",
        "token_command": None,
        "include_closed": False,
        "include_merge_requests": True,
        "comment_lookback_days": 90,
        "max_items_per_query": 500,
        "max_comment_lookups": 150,
        "watch_projects": [],
        "mention_lookback_days": 30,
    },
    "bitbucket": {
        "enabled": True,
        "url": "https://bitbucket.your-company.example",
        "token_command": None,
        "auth": "bearer",
        "username": "",
        "include_closed": False,
        "max_items_per_query": 500,
        "watch_repos": [],
        "mention_lookback_days": 30,
        "max_mention_scans": 200,
    },
}

CONFIG_HELP = """
Config created: {path}

Please fill in:
  jira.url     – self-hosted (Data Center/Server): e.g. https://jira.company.com, including
                 any context path such as https://company.com/jira
                 Cloud: https://company.atlassian.net
  jira.type    – "auto" (default: *.atlassian.net = Cloud, otherwise Data Center), "server", "cloud"
  jira.email   – Jira Cloud only: the e-mail address of your Atlassian account
  gitlab.url   – base URL of your GitLab instance
  bitbucket    – Bitbucket Data Center: url, including any context path

Tokens never go into this file. Store each one in the system keychain (asked for, not shown):
  python3 {script} --set-token jira        Data Center: personal access token (Jira 8.14+);
                                           Cloud: API token with scopes read:jira-work + read:jira-user
  python3 {script} --set-token gitlab      personal access token with ONLY the "read_api" scope
  python3 {script} --set-token bitbucket   personal HTTP access token, Project read + Repository read
Or let a password manager provide it: "token_command": "op read op://Private/GitLab/token"

Team repos (open PRs/MRs, plus mention search on Bitbucket), optional:
  gitlab.watch_projects   – e.g. ["platform/infra-live", "platform"]  (project or group)
  bitbucket.watch_repos   – e.g. ["PLAT/ci-pipelines", "PLAT"]      (repo or project)

On My Plate only reads. Write requests are blocked in the code.

To skip a source, set "enabled": false.
Then start again:  python3 {script}
"""


def merge_config(defaults: dict[str, Any], user: dict[str, Any] | None) -> dict[str, Any]:
    """Overlay user settings on the defaults, recursing into nested sections."""
    merged = dict(defaults)
    for key, value in (user or {}).items():
        if isinstance(value, dict) and isinstance(defaults.get(key), dict):
            merged[key] = merge_config(defaults[key], value)
        else:
            merged[key] = value
    return merged


# --------------------------------------------------------------------------- #
# Token storage: the system keychain, never the config file
# --------------------------------------------------------------------------- #

KEYCHAIN_SERVICE = "On My Plate"
# Printable ASCII without spaces, quotes or backslashes: every real API token fits, and the
# macOS keychain tool can read it from stdin without any quoting problems.
RE_STORABLE_TOKEN = re.compile(r"[!#-&(-\[\]-~]{1,4096}")
RE_ACCOUNT = re.compile(r"[a-z]+@[A-Za-z0-9.:\[\]-]+")
NO_KEYCHAIN = (
    "On My Plate does not store tokens in plain text, but found no keychain it can use "
    "(macOS Keychain, the Secret Service via secret-tool on Linux, Windows Credential Manager). "
    'Remove "token" from the config and use "token_command" or the {env} variable instead.'
)


def token_account(name: str, url: object) -> str:
    """Keychain account of a source's token: it belongs to one host and never moves to another."""
    host = (urllib.parse.urlsplit(str(url or "")).netloc or "").lower()
    account = f"{name}@{host}"
    if not RE_ACCOUNT.fullmatch(account):
        raise SourceError(f"{SOURCE_LABELS[name]}: the URL {sanitize(url, 200)} has no valid host")
    return account


def _run(command: list[str], stdin: str = "") -> subprocess.CompletedProcess[str]:
    # Fixed keychain tools with checked arguments, or the token_command from the private config.
    return subprocess.run(  # noqa: S603  # nosec B603
        command, input=stdin, capture_output=True, text=True, timeout=30, check=False
    )


class Keychain:
    """Where tokens are kept, keyed by ``token_account``."""

    label = ""

    def get(self, account: str) -> str | None:
        raise NotImplementedError

    def set(self, account: str, token: str) -> None:
        raise NotImplementedError

    def delete(self, account: str) -> bool:
        raise NotImplementedError

    def _verify(self, account: str, token: str, detail: str) -> None:
        if self.get(account) != token:
            raise SourceError(f"Could not store the token in {self.label}: {sanitize(detail, 200)}")


class MacKeychain(Keychain):
    label = "the macOS Keychain"

    def __init__(self, keychain: str | None = None) -> None:
        self.tool = "/usr/bin/security"
        self.keychain = [keychain] if keychain else []

    def get(self, account: str) -> str | None:
        args = ["find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w"]
        result = _run([self.tool, *args, *self.keychain])
        return result.stdout.rstrip("\n") or None if result.returncode == 0 else None

    def set(self, account: str, token: str) -> None:
        # The token travels through stdin: command lines are visible to other local users.
        target = f' "{self.keychain[0]}"' if self.keychain else ""
        line = (
            f'add-generic-password -U -s "{KEYCHAIN_SERVICE}" -a "{account}" -w "{token}"{target}\n'
        )
        result = _run([self.tool, "-i"], line)
        self._verify(account, token, result.stderr)

    def delete(self, account: str) -> bool:
        args = ["delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account]
        return _run([self.tool, *args, *self.keychain]).returncode == 0


class SecretServiceKeychain(Keychain):
    label = "the Secret Service keyring"

    def __init__(self, tool: str) -> None:
        self.tool = tool

    def _attributes(self, account: str) -> list[str]:
        return ["service", "onmyplate", "account", account]

    def get(self, account: str) -> str | None:
        result = _run([self.tool, "lookup", *self._attributes(account)])
        return result.stdout.strip() or None if result.returncode == 0 else None

    def set(self, account: str, token: str) -> None:
        label = f"{KEYCHAIN_SERVICE} ({account})"
        result = _run([self.tool, "store", "--label", label, *self._attributes(account)], token)
        self._verify(account, token, result.stderr)

    def delete(self, account: str) -> bool:
        return _run([self.tool, "clear", *self._attributes(account)]).returncode == 0


class WindowsKeychain(Keychain):
    label = "the Windows Credential Manager"

    def __init__(self) -> None:
        import ctypes  # noqa: PLC0415 - Windows only
        from ctypes import wintypes  # noqa: PLC0415

        class Credential(ctypes.Structure):
            _fields_: ClassVar = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        api = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
        pointer = ctypes.POINTER(Credential)
        api.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(pointer),
        ]
        api.CredReadW.restype = wintypes.BOOL
        api.CredWriteW.argtypes = [pointer, wintypes.DWORD]
        api.CredWriteW.restype = wintypes.BOOL
        api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        api.CredDeleteW.restype = wintypes.BOOL
        api.CredFree.argtypes = [ctypes.c_void_p]
        api.CredFree.restype = None
        self.ctypes, self.api, self.credential, self.pointer = ctypes, api, Credential, pointer

    @staticmethod
    def _target(account: str) -> str:
        return f"{KEYCHAIN_SERVICE}/{account}"

    def get(self, account: str) -> str | None:
        ctypes, found = self.ctypes, self.pointer()
        if not self.api.CredReadW(self._target(account), 1, 0, ctypes.byref(found)):
            return None
        try:
            size = found.contents.CredentialBlobSize
            return ctypes.string_at(found.contents.CredentialBlob, size).decode("utf-8") or None
        finally:
            self.api.CredFree(found)

    def set(self, account: str, token: str) -> None:
        ctypes, data = self.ctypes, token.encode("utf-8")
        blob = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        credential = self.credential()
        credential.Type = 1  # CRED_TYPE_GENERIC
        credential.TargetName = self._target(account)
        credential.UserName = account
        credential.CredentialBlobSize = len(data)
        credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE: this user on this machine
        ok = self.api.CredWriteW(ctypes.byref(credential), 0)
        self._verify(account, token, "" if ok else f"error {ctypes.get_last_error()}")

    def delete(self, account: str) -> bool:
        return bool(self.api.CredDeleteW(self._target(account), 1, 0))


_KEYCHAIN: Keychain | bool | None = False  # False: not looked up yet


def system_keychain() -> Keychain | None:
    """The keychain of this system, or None if there is none On My Plate can use."""
    global _KEYCHAIN
    if _KEYCHAIN is False:
        keychain: Keychain | None = None
        if sys.platform == "darwin" and Path("/usr/bin/security").exists():
            keychain = MacKeychain()
        elif os.name == "nt":
            try:
                keychain = WindowsKeychain()
            except (OSError, AttributeError):
                keychain = None
        elif tool := shutil.which("secret-tool"):
            keychain = SecretServiceKeychain(tool)
        _KEYCHAIN = keychain
    return _KEYCHAIN if isinstance(_KEYCHAIN, Keychain) else None


_COMMAND_TOKENS: dict[str, str] = {}


def command_token(command: object, label: str) -> str:
    """Run the configured token_command (a password manager or keychain CLI) once per start."""
    if not isinstance(command, str) or not command.strip():
        raise SourceError(f'{label}: "token_command" must be a command line')
    if command not in _COMMAND_TOKENS:
        try:
            result = _run(shlex.split(command))
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise SourceError(
                f"{label}: token_command could not run ({type(exc).__name__}). "
                "Check the command in the config."
            ) from None
        if result.returncode != 0 or not result.stdout.strip():
            raise SourceError(
                f"{label}: token_command failed (exit {result.returncode}). "
                "Run it in a terminal to see why."
            )
        _COMMAND_TOKENS[command] = result.stdout.strip()
    return _COMMAND_TOKENS[command]


def resolve_token(name: str, cfg: dict[str, Any], base: str) -> tuple[str, str]:
    """Return ``(token, origin)``: environment, token_command, then the system keychain.

    A "token" in the config is only honoured in memory: load_config moves tokens from
    the file into the keychain before a source ever sees them.
    """
    env = os.environ.get(SOURCE_ENV_VARS[name])
    if env:
        return env.strip(), "env"
    if cfg.get("token_command"):
        return command_token(cfg["token_command"], SOURCE_LABELS[name]), "command"
    if cfg.get("token"):
        return str(cfg["token"]).strip(), "config"
    keychain = system_keychain()
    stored = keychain.get(token_account(name, base)) if keychain else None
    return (stored, "keychain") if stored else ("", "")


def move_tokens_to_keychain(path: Path, user: dict[str, Any]) -> list[str]:
    """Move plaintext tokens out of the config file into the keychain; return the sources moved."""
    moved = []
    for name in SOURCE_NAMES:
        section = user.get(name)
        if not isinstance(section, dict) or not section.get("token"):
            continue
        keychain = system_keychain()
        if keychain is None:
            raise SourceError(NO_KEYCHAIN.format(env=SOURCE_ENV_VARS[name]))
        token = str(section["token"]).strip()
        if not RE_STORABLE_TOKEN.fullmatch(token):
            raise SourceError(
                f"{SOURCE_LABELS[name]}: the token in the config contains spaces, quotes or control characters"
            )
        keychain.set(token_account(name, section.get("url") or DEFAULT_CONFIG[name]["url"]), token)
        del section["token"]
        moved.append(SOURCE_LABELS[name])
    if moved:
        write_private(path, json.dumps(user, indent=2, ensure_ascii=False))
    return moved


# --------------------------------------------------------------------------- #
# HTTP client
# --------------------------------------------------------------------------- #


class SourceError(Exception):
    """A source could not be read; the message is shown to the user."""


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects to another scheme or host.

    urllib copies the Authorization and PRIVATE-TOKEN headers to the redirect
    target, which would hand the token to a foreign server.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN202, PLR0917 - stdlib signature
        old = urllib.parse.urlsplit(req.full_url)
        new = urllib.parse.urlsplit(urllib.parse.urljoin(req.full_url, newurl))
        if (old.scheme, old.netloc.lower()) != (new.scheme, new.netloc.lower()):
            raise SourceError(
                f"Blocked redirect from {old.netloc} to {new.scheme}://{new.netloc} so the token "
                "is not sent there. Please check the URL in the config."
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def require_https(url: str, cfg: dict[str, Any], label: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https" and parts.netloc:
        return
    if parts.scheme == "http" and cfg.get("allow_insecure_http"):
        return
    raise SourceError(
        f"{label}: the URL must start with https:// (the token would otherwise travel "
        'unencrypted). For tests only: "allow_insecure_http": true'
    )


class HttpClient:
    """Read-only HTTP client shared by all sources."""

    # Jira's search endpoints take the JQL in a POST body but do not modify data.
    READ_ONLY_POST_PATHS = ("/rest/api/3/search/jql", "/rest/api/2/search")
    ERROR_HINTS: ClassVar[dict[int, str]] = {
        401: " (token invalid, expired or missing a scope?)",
        403: " (missing permission or scope?)",
        404: " (wrong URL or context path?)",
    }

    def __init__(self, cfg: dict[str, Any]) -> None:
        if cfg.get("verify_ssl", True) is False:
            # Explicit opt-in via verify_ssl=false; the UI shows a permanent warning.
            context = ssl._create_unverified_context()  # noqa: S323  # nosec B323
        else:
            context = ssl.create_default_context(cafile=cfg.get("ca_bundle") or None)
        self.timeout = cfg.get("timeout_seconds", 30)
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context), _SameOriginRedirectHandler()
        )

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        body: object = None,
        *,
        accept: str = "application/json",
        raw: bool = False,
    ) -> tuple[Any, Any]:
        """Perform a request and return ``(decoded body, response headers)``."""
        path = urllib.parse.urlsplit(url).path
        if not (method == "GET" or (method == "POST" and path.endswith(self.READ_ONLY_POST_PATHS))):
            raise SourceError(f"Blocked: {method} {path}. On My Plate only performs read requests.")
        data = json.dumps(body).encode() if body is not None else None
        request_headers = {"Accept": accept, "User-Agent": f"onmyplate/{VERSION}"}
        if data is not None:
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        req = urllib.request.Request(url, data=data, headers=request_headers, method=method)  # noqa: S310 - scheme checked by require_https
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                payload = self._read_bounded(response, url)
                if raw:
                    return payload, response.headers
                try:
                    return (json.loads(payload) if payload else None), response.headers
                except ValueError:
                    raise SourceError(
                        f"Response from {redact_url(url)} is not JSON, possibly a login/SSO page "
                        "instead of the API (check token, URL or context path)."
                    ) from None
        except urllib.error.HTTPError as exc:
            detail = sanitize(exc.read(300).decode(errors="replace")).strip()
            hint = self.ERROR_HINTS.get(exc.code, "")
            raise SourceError(f"HTTP {exc.code}{hint} at {redact_url(url)} {detail}") from None
        except urllib.error.URLError as exc:
            host = urllib.parse.urlsplit(url).netloc
            raise SourceError(f"Cannot connect to {host}: {sanitize(exc.reason)}") from None
        except TimeoutError:
            raise SourceError(f"Timeout at {redact_url(url)}") from None

    def _read_bounded(self, response: Any, url: str) -> bytes:
        """Read at most MAX_RESPONSE_BYTES, giving up when a slow server exceeds the deadline."""
        deadline = time.monotonic() + self.timeout * 4
        chunks: list[bytes] = []
        size = 0
        while size < MAX_RESPONSE_BYTES:
            chunk = response.read(min(65536, MAX_RESPONSE_BYTES - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if time.monotonic() > deadline:
                raise SourceError(f"Timeout at {redact_url(url)} (response too slow)")
        return b"".join(chunks)


def with_query(url: str, params: dict[str, Any] | list[tuple[str, str]] | None) -> str:
    return url + ("?" + urllib.parse.urlencode(params) if params else "")


def bearer_or_basic(auth: str, user: str, token: str) -> dict[str, str]:
    if auth == "basic":
        credentials = base64.b64encode(f"{user}:{token}".encode()).decode()
        return {"Authorization": "Basic " + credentials}
    return {"Authorization": "Bearer " + token}


class Source:
    """Common setup for an issue or pull request source."""

    name: ClassVar[str] = ""
    env_var: ClassVar[str] = ""

    def __init__(self, cfg: dict[str, Any], http: HttpClient) -> None:
        self.cfg = cfg
        self.http = http
        self.base = cfg["url"].rstrip("/")
        self.label = SOURCE_LABELS[self.name]
        require_https(self.base, cfg, self.label)
        self.token, self.token_origin = resolve_token(self.name, cfg, self.base)
        if not self.token:
            raise SourceError(
                f"{self.label}: no token. Store it in the system keychain with "
                f"'onmyplate --set-token {self.name}', or set {self.env_var} or \"token_command\"."
            )
        if not self.token.isprintable() or any(ch.isspace() for ch in self.token):
            raise SourceError(f"{self.label}: the token contains spaces or control characters")

    def fetch(self) -> FetchResult:
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Jira
# --------------------------------------------------------------------------- #

JIRA_STATE = {"new": "todo", "indeterminate": "in_progress", "done": "done"}
RE_CLOUD_ID = re.compile(r"[0-9a-fA-F-]{20,64}")
RE_JIRA_KEY = re.compile(r"[A-Z][A-Z0-9_]*-\d+")
ATOM = "{http://www.w3.org/2005/Atom}"


def parse_atom_feed(body: bytes) -> ET.Element | None:
    """Parse an Atom feed, refusing DTDs so entity expansion attacks cannot occur.

    The check runs on decoded text and the parser receives that same text, so a
    declared encoding such as UTF-16 cannot hide a DOCTYPE from the check.
    """
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    upper = text.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        return None
    try:
        return ET.fromstring(text)  # noqa: S314  # nosec B314
    except ET.ParseError:
        return None


class JiraSource(Source):
    name = "jira"
    env_var = "ONMYPLATE_JIRA_TOKEN"
    FIELDS: ClassVar[list[str]] = [
        "summary",
        "status",
        "issuetype",
        "priority",
        "assignee",
        "reporter",
        "updated",
        "created",
        "duedate",
        "labels",
        "project",
        "comment",
    ]

    def __init__(self, cfg: dict[str, Any], http: HttpClient) -> None:
        super().__init__(cfg, http)
        jira_type = str(cfg.get("type") or "auto").lower()
        if jira_type == "auto":
            host = (urllib.parse.urlsplit(self.base).hostname or "").lower()
            jira_type = "cloud" if host.endswith((".atlassian.net", ".jira.com")) else "server"
        self.cloud = jira_type == "cloud"
        # Scoped Cloud tokens are only accepted by the Atlassian API gateway.
        self.scoped = self.cloud and cfg.get("scoped_token", True) is not False
        api_version = "/rest/api/3" if self.cloud else "/rest/api/2"
        self.api: str | None = None if self.scoped else self.base + api_version
        auth = cfg.get("auth") or ("basic" if self.cloud else "bearer")
        user = cfg.get("username") or cfg.get("email", "")
        self.headers = bearer_or_basic(auth, user, self.token)
        self.limit = int(cfg.get("max_issues_per_query", 500))
        self.me: dict[str, Any] = {}

    def _resolve_api(self) -> None:
        if self.api:
            return
        cloud_id = self.cfg.get("cloud_id")
        if not cloud_id:
            info = self.http.request("GET", self.base + "/_edge/tenant_info")[0] or {}
            cloud_id = info.get("cloudId")
        if not isinstance(cloud_id, str) or not RE_CLOUD_ID.fullmatch(cloud_id):
            raise SourceError(
                'Jira: cannot determine the cloud ID. Please set "cloud_id" in the config '
                f"(see {self.base}/_edge/tenant_info)."
            )
        self.api = f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3"

    def _call(self, method: str, url: str, body: object = None) -> Any:
        try:
            return self.http.request(method, url, self.headers, body)[0]
        except SourceError as exc:
            if self.cloud and not self.scoped and "HTTP 404" in str(exc):
                raise SourceError(
                    f'{exc} Scoped Cloud API tokens need "scoped_token": true.'
                ) from None
            raise

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._call("GET", with_query(f"{self.api}{path}", params))

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        return self._call("POST", f"{self.api}{path}", body)

    def _is_me(self, user: dict[str, Any] | None) -> bool:
        if not user:
            return False
        return any(
            self.me.get(k) and user.get(k) == self.me.get(k) for k in ("accountId", "key", "name")
        )

    def search(self, jql: str) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        if self.cloud:
            token = None
            for _ in range(MAX_PAGES):
                body: dict[str, Any] = {"jql": jql, "fields": self.FIELDS, "maxResults": 100}
                if token:
                    body["nextPageToken"] = token
                data = self._post("/search/jql", body) or {}
                page = data.get("issues") or []
                issues += page
                token = data.get("nextPageToken")
                if not page or not token or data.get("isLast") or len(issues) >= self.limit:
                    break
        else:
            start = 0
            for _ in range(MAX_PAGES):
                data = (
                    self._post(
                        "/search",
                        {"jql": jql, "startAt": start, "maxResults": 100, "fields": self.FIELDS},
                    )
                    or {}
                )
                page = data.get("issues", [])
                issues += page
                start += len(page)
                if not page or start >= data.get("total", 0) or len(issues) >= self.limit:
                    break
        return issues[: self.limit]

    def normalize(self, raw: dict[str, Any]) -> Item:
        fields = raw.get("fields") or {}
        key = raw["key"]
        status = fields.get("status") or {}
        category = (status.get("statusCategory") or {}).get("key")
        return {
            "id": "jira:" + key,
            "source": "jira",
            "key": key,
            "title": fields.get("summary") or "",
            "url": f"{self.base}/browse/{key}" if RE_JIRA_KEY.fullmatch(key) else self.base,
            "project": (fields.get("project") or {}).get("name") or key.split("-")[0],
            "status": status.get("name", ""),
            "state": JIRA_STATE.get(category, "todo"),
            "type": (fields.get("issuetype") or {}).get("name", ""),
            "priority": (fields.get("priority") or {}).get("name", ""),
            "assignee": display_name(fields.get("assignee")),
            "author": display_name(fields.get("reporter")),
            "updated": to_iso(fields.get("updated")),
            "created": to_iso(fields.get("created")),
            "due": fields.get("duedate"),
            "labels": fields.get("labels") or [],
            "relations": set(),
        }

    def _comment_state(self, raw: dict[str, Any]) -> tuple[bool, bool]:
        """Return ``(commented by me, comment list truncated)`` for a search result."""
        block = (raw.get("fields") or {}).get("comment") or {}
        comments = block.get("comments") or []
        if any(self._is_me(c.get("author")) for c in comments):
            return True, False
        return False, (block.get("total") or 0) > len(comments)

    def _issue_path(self, key: str) -> str:
        return f"/issue/{urllib.parse.quote(key, safe='')}"

    def fetch(self) -> FetchResult:
        self._resolve_api()
        warnings: list[str] = []
        try:
            self.me = self._get("/myself") or {}
        except SourceError as exc:
            self.me = {}
            warnings.append(
                f"Jira: cannot read your own user, so 'Commented' is not detected ({exc})."
            )
        done_filter = "" if self.cfg.get("include_done") else " AND statusCategory != Done"
        scope = f" AND ({self.cfg['scope_jql']})" if self.cfg.get("scope_jql") else ""
        days = int(self.cfg.get("comment_lookback_days", 90))
        participation = self.cfg.get("participation_jql")
        if participation is None and self.cloud:
            participation = f'issue in updatedBy(currentUser(), "-{days}d")'

        queries = [
            ("assignee", "assignee = currentUser()"),
            ("reporter", "reporter = currentUser()"),
            ("watcher", "watcher = currentUser()"),
        ]
        if participation:
            queries.append(("participant", participation))

        def run(query: tuple[str, str]) -> tuple[str, list[dict[str, Any]], str | None]:
            relation, jql = query
            try:
                return (
                    relation,
                    self.search(f"({jql}){done_filter}{scope} ORDER BY updated DESC"),
                    None,
                )
            except SourceError as exc:
                return relation, [], f"Jira query '{relation}' failed: {exc}"

        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(run, queries))
        if all(error for _, _, error in results):
            raise SourceError(results[0][2])

        items: dict[str, Item] = {}
        recheck: list[str] = []
        for relation, raws, error in results:
            if error:
                warnings.append(error)
            for raw in raws:
                item = items.setdefault(raw["key"], self.normalize(raw))
                item["relations"].add(relation)
                commented, truncated = self._comment_state(raw)
                if commented:
                    item["relations"].add("commented")
                elif truncated and relation == "participant":
                    recheck.append(raw["key"])

        def commented_by_me(key: str) -> tuple[str, bool]:
            try:
                data = (
                    self._get(
                        f"{self._issue_path(key)}/comment",
                        {"maxResults": 100, "orderBy": "-created"},
                    )
                    or {}
                )
            except SourceError:
                return key, False
            return key, any(self._is_me(c.get("author")) for c in data.get("comments", []))

        with ThreadPoolExecutor(6) as pool:
            for key, mine in pool.map(commented_by_me, sorted(set(recheck))[:60]):
                if mine:
                    items[key]["relations"].add("commented")

        if not self.cloud and not participation:
            self._add_commented_from_activity(items, days, warnings)

        for item in items.values():
            prune_relations(item["relations"])
        return list(items.values()), warnings

    def _add_commented_from_activity(
        self, items: dict[str, Item], days: int, warnings: list[str]
    ) -> None:
        keys, warning = self._activity_comment_keys(days)
        if warning:
            warnings.append(warning)
        for key in keys:
            if key in items:
                items[key]["relations"].add("commented")
        missing = [key for key in keys if key not in items][:80]

        def load(key: str) -> Any:
            try:
                return self._get(self._issue_path(key), {"fields": ",".join(self.FIELDS)})
            except SourceError:
                return None

        with ThreadPoolExecutor(6) as pool:
            for raw in pool.map(load, missing):
                if not isinstance(raw, dict) or not raw.get("key"):
                    continue
                item = self.normalize(raw)
                if item["state"] == "done" and not self.cfg.get("include_done"):
                    continue
                item["relations"].add("commented")
                items[item["key"]] = item

    def _activity_comment_keys(self, days: int) -> tuple[list[str], str | None]:
        """Read keys of issues I commented on from the Data Center activity stream.

        JQL cannot search comment authors without plugins (JRASERVER-62780), and
        Safeguards restrict ``updatedBy()`` to a few fields since 8.20.12 / 8.22.2
        (JRASERVER-77874), so it is no reliable source on Data Center.
        """
        user = self.me.get("name") or self.me.get("key")
        if not user:
            return [], None
        params = [
            ("streams", f"user IS {user}"),
            ("issues", "activity IS comment:post"),
            ("maxResults", "200"),
        ]
        if self.headers["Authorization"].startswith("Basic "):
            params.append(("os_authType", "basic"))
        try:
            body, _ = self.http.request(
                "GET",
                with_query(self.base + "/activity", params),
                self.headers,
                accept="application/atom+xml",
                raw=True,
            )
        except SourceError as exc:
            return [], f"Jira: cannot read the activity stream, 'Commented' is missing ({exc})."
        root = parse_atom_feed(body)
        if root is None:
            return (
                [],
                "Jira: the activity stream returned no valid Atom XML, 'Commented' is missing.",
            )
        cutoff = datetime.now(UTC) - timedelta(days=days)
        # Only the path counts: Jira's own base URL may differ from the configured one
        # (host alias, proxy). The key is checked and then loaded from the configured URL.
        browse = re.compile(r"/browse/([A-Z][A-Z0-9_]*-\d+)(?:[?#]|$)")
        keys: list[str] = []
        for entry in root.iter(ATOM + "entry"):
            stamp = entry.findtext(ATOM + "updated") or entry.findtext(ATOM + "published") or ""
            try:
                if datetime.fromisoformat(stamp) < cutoff:
                    continue
            except ValueError:
                pass
            for link in entry.iter(ATOM + "link"):
                match = browse.search(urllib.parse.urlsplit(link.get("href") or "").path + "?")
                if match:
                    keys.append(match.group(1))
                    break
        return list(dict.fromkeys(keys)), None


# --------------------------------------------------------------------------- #
# GitLab
# --------------------------------------------------------------------------- #

# Work items (GitLab 17+: issues, tasks, epics, objectives …) live in the issues table, so
# they share the issue ID and API; to-dos and notes created in the new UI name them "WorkItem".
GITLAB_KINDS = {"Issue": "issue", "WorkItem": "issue", "MergeRequest": "mr"}
GITLAB_TODO_RELATIONS = {
    "mentioned": "mentioned",
    "directly_addressed": "mentioned",
    "review_requested": "reviewer",
    "approval_required": "reviewer",
    "assigned": "assignee",
}
RE_GITLAB_PATH = re.compile(r"[\w.\-]+(/[\w.\-]+)*")


class GitLabSource(Source):
    name = "gitlab"
    env_var = "ONMYPLATE_GITLAB_TOKEN"

    def __init__(self, cfg: dict[str, Any], http: HttpClient) -> None:
        super().__init__(cfg, http)
        self.api = self.base + "/api/v4"
        self.headers = {"PRIVATE-TOKEN": self.token}
        self.limit = int(cfg.get("max_items_per_query", 500))
        self.include_closed = bool(cfg.get("include_closed", False))

    def _get(self, path: str, params: dict[str, Any] | None = None) -> tuple[Any, Any]:
        return self.http.request("GET", with_query(self.api + path, params), self.headers)

    def paged(self, path: str, params: dict[str, Any], limit: int | None = None) -> list[Any]:
        limit = limit or self.limit
        results: list[Any] = []
        page = 1
        for _ in range(MAX_PAGES):
            data, headers = self._get(path, {**params, "per_page": 100, "page": page})
            if not isinstance(data, list) or not data:
                break
            results += data
            next_page = headers.get("X-Next-Page") if headers else None
            if (
                not next_page
                or not next_page.isdigit()
                or int(next_page) <= page
                or len(results) >= limit
            ):
                break
            page = int(next_page)
        return results[:limit]

    def _own_url(self, url: object, project_path: str, kind: str, iid: object) -> str:
        """Keep links on this instance; rebuild them from path and number otherwise."""
        if isinstance(url, str) and url.startswith(self.base + "/"):
            return url
        if "/" in project_path and RE_GITLAB_PATH.fullmatch(project_path) and str(iid).isdigit():
            segment = "merge_requests" if kind == "mr" else "issues"
            return f"{self.base}/{urllib.parse.quote(project_path)}/-/{segment}/{iid}"
        return self.base

    @staticmethod
    def _scoped_label(labels: list[str], *prefixes: str) -> str | None:
        for label in labels:
            lowered = label.lower()
            if any(lowered.startswith(prefix + "::") for prefix in prefixes):
                # In nested scopes such as workflow::backend::review the value follows the last ::.
                return label.rsplit("::", 1)[1]
        return None

    def normalize(self, raw: dict[str, Any], kind: str) -> Item:
        labels = [
            lbl if isinstance(lbl, str) else lbl.get("name", "") for lbl in raw.get("labels") or []
        ]
        state = raw.get("state", "opened")
        draft = raw.get("draft") or raw.get("work_in_progress")
        workflow = self._scoped_label(labels, "workflow", "status")
        if state in ("closed", "merged"):
            category = "done"
        elif (kind == "mr" and not draft) or (
            workflow
            and any(w in workflow.lower() for w in ("doing", "progress", "review", "arbeit"))
        ):
            category = "in_progress"
        else:
            category = "todo"
        if kind == "mr":
            status = {"merged": "Merged", "closed": "Closed"}.get(state) or (
                "Draft" if draft else "Open"
            )
        else:
            status = "Closed" if state == "closed" else (workflow or "Open")
        full_ref = (raw.get("references") or {}).get("full") or ""
        separator = "!" if kind == "mr" else "&" if "&" in full_ref and "#" not in full_ref else "#"
        project_path = (
            full_ref.rsplit(separator, 1)[0]
            if separator in full_ref
            else str(raw.get("project_id"))
        )
        assignees = raw.get("assignees") or ([raw["assignee"]] if raw.get("assignee") else [])
        branch = ""
        if kind == "mr" and raw.get("source_branch"):
            branch = f"{raw.get('source_branch')} → {raw.get('target_branch')}"
        return {
            "id": f"gitlab:{kind}:{raw['id']}",
            "source": "gitlab",
            "key": f"{project_path.split('/')[-1]}{separator}{raw.get('iid')}",
            "title": raw.get("title") or "",
            "url": self._own_url(raw.get("web_url"), project_path, kind, raw.get("iid")),
            "project": project_path,
            "status": status,
            "state": category,
            "type": "MR"
            if kind == "mr"
            else (raw.get("issue_type") or str(raw.get("type") or "issue").lower())
            .replace("_", " ")
            .capitalize(),
            "priority": self._scoped_label(labels, "priority", "prio") or "",
            "assignee": ", ".join(display_name(a) for a in assignees if a),
            "author": display_name(raw.get("author")),
            "updated": to_iso(raw.get("updated_at")),
            "created": to_iso(raw.get("created_at")),
            "due": raw.get("due_date"),
            "labels": labels,
            "relations": set(),
            "branch": branch,
        }

    def fetch(self) -> FetchResult:
        me, _ = self._get("/user")
        warnings: list[str] = []
        state_filter = {} if self.include_closed else {"state": "opened"}
        jobs = [
            ("assignee", "issue", "/issues", {"scope": "assigned_to_me"}),
            ("reporter", "issue", "/issues", {"scope": "created_by_me"}),
        ]
        if self.cfg.get("include_merge_requests", True):
            jobs += [
                ("assignee", "mr", "/merge_requests", {"scope": "assigned_to_me"}),
                ("reporter", "mr", "/merge_requests", {"scope": "created_by_me"}),
                ("reviewer", "mr", "/merge_requests", {"scope": "all", "reviewer_id": me["id"]}),
            ]

        items: dict[str, Item] = {}
        by_ref: dict[tuple[Any, str, Any], Item] = {}

        def add(raw: dict[str, Any], kind: str, relation: str) -> None:
            if not self.include_closed and raw.get("state") not in (None, "opened"):
                return
            item_id = f"gitlab:{kind}:{raw['id']}"
            item = items.get(item_id)
            if not item:
                item = items[item_id] = self.normalize(raw, kind)
                by_ref[(raw.get("project_id"), kind, raw.get("iid"))] = item
            item["relations"].add(relation)
            # Only single-item responses carry "subscribed"; list endpoints never do.
            if raw.get("subscribed"):
                item["relations"].add("watcher")

        def run(
            job: tuple[str, str, str, dict[str, Any]],
        ) -> tuple[str, str, list[Any], str | None]:
            relation, kind, path, params = job
            try:
                return relation, kind, self.paged(path, {**state_filter, **params}), None
            except SourceError as exc:
                return (
                    relation,
                    kind,
                    [],
                    f"GitLab query {path} ({relation}) failed: {exc}",
                )

        with ThreadPoolExecutor(5) as pool:
            for relation, kind, raws, error in pool.map(run, jobs):
                if error:
                    warnings.append(error)
                for raw in raws:
                    add(raw, kind, relation)

        self._add_from_todos(items, add, warnings)
        self._add_team_merge_requests(add, warnings)
        self._add_commented(by_ref, add, warnings)

        for item in items.values():
            prune_relations(item["relations"])
        return list(items.values()), warnings

    def _add_from_todos(
        self, items: dict[str, Item], add: Callable[..., None], warnings: list[str]
    ) -> None:
        """Mentions and review requests come from GitLab to-dos, also from done ones."""
        try:
            for todo in self.paged("/todos", {"state": "pending"}, limit=300):
                kind = GITLAB_KINDS.get(todo.get("target_type"))
                if kind and todo.get("target"):
                    relation = GITLAB_TODO_RELATIONS.get(todo.get("action_name"), "participant")
                    add(todo["target"], kind, relation)
        except SourceError as exc:
            warnings.append(f"GitLab: cannot read to-do items: {exc}")

        days = int(self.cfg.get("mention_lookback_days", 30))
        since = datetime.now(UTC) - timedelta(days=days)
        for action in ("mentioned", "directly_addressed"):
            try:
                done = self.paged("/todos", {"state": "done", "action": action}, limit=200)
            except SourceError as exc:
                warnings.append(f"GitLab: cannot read answered mentions ({exc}).")
                return
            for todo in done:
                kind = GITLAB_KINDS.get(todo.get("target_type"))
                target = todo.get("target")
                if not kind or not target:
                    continue
                try:
                    recent = datetime.fromisoformat(to_iso(todo.get("created_at")) or "") >= since
                except ValueError:
                    recent = False
                existing = items.get(f"gitlab:{kind}:{target.get('id')}")
                if recent and not (existing and "mentioned" in existing["relations"]):
                    add(target, kind, "mentioned_done")

    def _add_team_merge_requests(self, add: Callable[..., None], warnings: list[str]) -> None:
        paths: list[str] = []
        for entry in self.cfg.get("watch_projects") or []:
            if isinstance(entry, str) and RE_GITLAB_PATH.fullmatch(entry.strip()):
                paths.append(entry.strip())
            else:
                warnings.append(
                    f"GitLab: ignoring watch_projects entry '{sanitize(entry, 80)}' "
                    "(expected group/project)."
                )

        def merge_requests(path: str) -> tuple[str, list[Any], SourceError | None]:
            encoded = urllib.parse.quote(path, safe="")
            params = {"state": "opened", "scope": "all"}
            try:
                return path, self.paged(f"/projects/{encoded}/merge_requests", params), None
            except SourceError as exc:
                if "HTTP 404" not in str(exc):
                    return path, [], exc
            try:
                return path, self.paged(f"/groups/{encoded}/merge_requests", params), None
            except SourceError as exc:
                return path, [], exc

        with ThreadPoolExecutor(4) as pool:
            for path, merge_reqs, error in pool.map(merge_requests, paths):
                if error:
                    warnings.append(f"GitLab: cannot read MRs of {path} ({error}).")
                for raw in merge_reqs:
                    if isinstance(raw, dict):
                        add(raw, "mr", "team")

    def _add_commented(
        self,
        by_ref: dict[tuple[Any, str, Any], Item],
        add: Callable[..., None],
        warnings: list[str],
    ) -> None:
        """Use the events API; per GitLab's docs it covers issue comments reliably."""
        days = int(self.cfg.get("comment_lookback_days", 90))
        after = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
        refs: list[tuple[int, str, int]] = []
        try:
            events = self.paged(
                "/events",
                {"action": "commented", "target_type": "note", "after": after},
                limit=2000,
            )
        except SourceError as exc:
            warnings.append(f"GitLab: cannot read comments: {exc}")
            events = []
        for event in events:
            note = event.get("note") or {}
            kind = GITLAB_KINDS.get(note.get("noteable_type"))
            try:
                project_id, iid = int(event.get("project_id")), int(note.get("noteable_iid"))
            except (TypeError, ValueError):
                continue
            if kind and project_id > 0 and iid > 0:
                refs.append((project_id, kind, iid))

        missing = []
        for ref in dict.fromkeys(refs):
            if ref in by_ref:
                by_ref[ref]["relations"].add("commented")
            else:
                missing.append(ref)
        cap = int(self.cfg.get("max_comment_lookups", 150))
        if len(missing) > cap:
            warnings.append(f"GitLab: loaded only the {cap} most recently commented items.")

        def load(ref: tuple[int, str, int]) -> tuple[str, Any]:
            project_id, kind, iid = ref
            segment = "merge_requests" if kind == "mr" else "issues"
            try:
                return kind, self._get(f"/projects/{project_id}/{segment}/{iid}")[0]
            except SourceError:
                return kind, None

        with ThreadPoolExecutor(8) as pool:
            for kind, raw in pool.map(load, missing[:cap]):
                if raw:
                    add(raw, kind, "commented")


# --------------------------------------------------------------------------- #
# Bitbucket Data Center / Server
# --------------------------------------------------------------------------- #

RE_BITBUCKET_PROJECT = re.compile(r"~?[\w.\-]+")
RE_BITBUCKET_REPO = re.compile(r"[\w.\-]+")
RE_BITBUCKET_WATCH = re.compile(r"~?[\w.\-]+(/[\w.\-]+)?")
_MENTION_CACHE: dict[tuple[str, str, str, Any], tuple[Any, str | None]] = {}


class BitbucketSource(Source):
    name = "bitbucket"
    env_var = "ONMYPLATE_BITBUCKET_TOKEN"

    def __init__(self, cfg: dict[str, Any], http: HttpClient) -> None:
        super().__init__(cfg, http)
        self.api = self.base + "/rest/api/latest"
        self.headers = bearer_or_basic(
            cfg.get("auth", "bearer"), cfg.get("username", ""), self.token
        )
        self.limit = int(cfg.get("max_items_per_query", 500))
        self.include_closed = bool(cfg.get("include_closed", False))

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.http.request("GET", with_query(self.api + path, params), self.headers)[0]

    def paged(self, path: str, params: dict[str, Any]) -> list[Any]:
        results: list[Any] = []
        start = 0
        for _ in range(MAX_PAGES):
            data = self._get(path, {**params, "start": start, "limit": 100}) or {}
            values = data.get("values") if isinstance(data, dict) else None
            if not isinstance(values, list) or not values:
                break
            results += values
            next_start = data.get("nextPageStart")
            if (
                data.get("isLastPage", True)
                or not isinstance(next_start, int)
                or next_start <= start
                or len(results) >= self.limit
            ):
                break
            start = next_start
        return results[: self.limit]

    def _repo_path(self, project: str, repo: str) -> str:
        quote = urllib.parse.quote
        return f"/projects/{quote(project)}/repos/{quote(repo)}"

    @staticmethod
    def _from_millis(value: object) -> str | None:
        if isinstance(value, (int, float)) and value > 0:
            return datetime.fromtimestamp(value / 1000, UTC).isoformat()
        return None

    def _web_url(self, pr: dict[str, Any], project: str, repo: str) -> str:
        for link in (pr.get("links") or {}).get("self") or []:
            href = link.get("href") if isinstance(link, dict) else None
            if isinstance(href, str) and href.startswith(self.base + "/"):
                return href
        pr_id = str(pr.get("id", ""))
        if (
            RE_BITBUCKET_PROJECT.fullmatch(project or "")
            and RE_BITBUCKET_REPO.fullmatch(repo or "")
            and pr_id.isdigit()
        ):
            return f"{self.base}{self._repo_path(project, repo)}/pull-requests/{pr_id}/overview"
        return self.base

    def normalize(self, pr: dict[str, Any]) -> Item:
        repo = (pr.get("toRef") or {}).get("repository") or {}
        project = (repo.get("project") or {}).get("key") or ""
        slug = repo.get("slug") or ""
        reviewers = [r for r in pr.get("reviewers") or [] if isinstance(r, dict)]
        needs_work = any(r.get("status") == "NEEDS_WORK" for r in reviewers)
        approved = sum(1 for r in reviewers if r.get("status") == "APPROVED")
        state, draft = pr.get("state", "OPEN"), pr.get("draft") is True
        if state in ("MERGED", "DECLINED"):
            category, status = "done", {"MERGED": "Merged", "DECLINED": "Declined"}[state]
        elif draft:
            category, status = "todo", "Draft"
        elif needs_work:
            category, status = "in_progress", "Needs work"
        else:
            category = "in_progress"
            status = f"{approved}/{len(reviewers)} approved" if reviewers else "Open"
        source_ref = (pr.get("fromRef") or {}).get("displayId")
        target_ref = (pr.get("toRef") or {}).get("displayId", "")
        pr_id = pr.get("id")
        return {
            "id": f"bitbucket:pr:{project}:{slug}:{pr_id}",
            "source": "bitbucket",
            "key": f"{slug}#{pr_id}",
            "title": pr.get("title") or "",
            "url": self._web_url(pr, project, slug),
            "project": f"{project}/{slug}" if project else slug,
            "status": status,
            "state": category,
            "type": "PR",
            "priority": "",
            "assignee": ", ".join(display_name(r.get("user")) for r in reviewers),
            "author": display_name((pr.get("author") or {}).get("user")),
            "updated": self._from_millis(pr.get("updatedDate")),
            "created": self._from_millis(pr.get("createdDate")),
            "due": None,
            "labels": [],
            "relations": set(),
            "branch": f"{source_ref} → {target_ref}" if source_ref else "",
            "_needs_work": needs_work,
            "_open": state == "OPEN",
            "_scan": (project, slug, pr_id, pr.get("updatedDate"), pr.get("description") or ""),
        }

    def _whoami(self) -> str | None:
        """Bitbucket has no /myself; every response carries the user in X-AUSERNAME."""
        try:
            _, headers = self.http.request(
                "GET", self.api + "/application-properties", self.headers
            )
        except SourceError:
            headers = None
        # The header is URL-encoded for names with special characters.
        name = urllib.parse.unquote(headers.get("X-AUSERNAME") or "") if headers else ""
        if name.lower() == "anonymous":
            name = ""
        return name or self.cfg.get("username") or None

    def _team_repos(self, warnings: list[str]) -> list[tuple[str, str]]:
        repos: list[tuple[str, str]] = []
        for entry in self.cfg.get("watch_repos") or []:
            if not isinstance(entry, str) or not RE_BITBUCKET_WATCH.fullmatch(entry.strip()):
                warnings.append(
                    f"Bitbucket: ignoring watch_repos entry '{sanitize(entry, 80)}' "
                    "(expected PROJ or PROJ/repo)."
                )
                continue
            target = entry.strip()
            if "/" in target:
                project, repo = target.split("/", 1)
                repos.append((project, repo))
                continue
            try:
                listed = self.paged(f"/projects/{urllib.parse.quote(target)}/repos", {})
                repos.extend(
                    (target, repo["slug"])
                    for repo in listed
                    if isinstance(repo, dict) and repo.get("slug")
                )
            except SourceError as exc:
                warnings.append(f"Bitbucket: cannot read project {target} ({exc}).")
        return list(dict.fromkeys(repos))

    def _mention_state(self, scan: tuple[str, str, Any, Any, str], me: str) -> str | None:
        """Return "open", "done" (I commented after the last mention) or None.

        Results are cached per pull request version to avoid repeated calls.
        """
        project, slug, pr_id, updated, description = scan
        cache_key = (self.base, project, slug, pr_id)
        cached = _MENTION_CACHE.get(cache_key)
        if cached and cached[0] == updated:
            return cached[1]
        # A sentence may end right after the name ("thanks @sam."), but "@sam.smith" is someone else.
        pattern = re.compile(
            r'(?<![\w.@-])@"?' + re.escape(me) + r'"?(?![\w-]|\.[\w-])', re.IGNORECASE
        )
        last_mention = 1 if pattern.search(description) else 0
        last_own_comment = 0

        def walk(root: object) -> None:
            nonlocal last_mention, last_own_comment
            stack = [root]
            while stack:
                comment = stack.pop()
                if not isinstance(comment, dict):
                    continue
                created = comment.get("createdDate")
                created = created if isinstance(created, (int, float)) else 0
                author = str((comment.get("author") or {}).get("name") or "").lower()
                if author == me.lower():
                    last_own_comment = max(last_own_comment, created)
                elif pattern.search(str(comment.get("text") or "")):
                    last_mention = max(last_mention, created)
                replies = comment.get("comments")
                if isinstance(replies, list):
                    stack.extend(replies)

        path = f"{self._repo_path(project, slug)}/pull-requests/{int(pr_id)}/activities"
        data = self._get(path, {"limit": 100}) or {}
        for activity in data.get("values") or []:
            if isinstance(activity, dict) and activity.get("action") == "COMMENTED":
                walk(activity.get("comment"))
        result = None
        if last_mention:
            result = "done" if last_own_comment > last_mention else "open"
        _MENTION_CACHE[cache_key] = (updated, result)
        return result

    def fetch(self) -> FetchResult:
        warnings: list[str] = []
        state_filter = {} if self.include_closed else {"state": "OPEN"}
        roles = {
            "author": {"role": "AUTHOR"},
            "review_open": {"role": "REVIEWER", "participantStatus": "UNAPPROVED"},
            "review_all": {"role": "REVIEWER"},
            "participant": {"role": "PARTICIPANT"},
        }

        def run(
            entry: tuple[str, dict[str, str]],
        ) -> tuple[str, list[Any] | None, SourceError | None]:
            key, params = entry
            try:
                return key, self.paged("/dashboard/pull-requests", {**state_filter, **params}), None
            except SourceError as exc:
                return key, None, exc

        with ThreadPoolExecutor(4) as pool:
            results = {key: (prs, error) for key, prs, error in pool.map(run, roles.items())}
        if all(prs is None for prs, _ in results.values()):
            raise SourceError(str(results["author"][1]))
        for key, (_, error) in results.items():
            if error and key != "review_open":
                warnings.append(f"Bitbucket query {roles[key]['role']} failed: {error}")

        items: dict[str, Item] = {}

        def add(pr: object, relation: str) -> Item | None:
            if not isinstance(pr, dict):
                return None
            fresh = self.normalize(pr)
            item = items.get(fresh["id"])
            if not item:
                if not self.include_closed and not fresh["_open"]:
                    return None
                item = items[fresh["id"]] = fresh
            item["relations"].add(relation)
            return item

        authored = results["author"][0] or []
        for pr in authored:
            item = add(pr, "reporter")
            if item and item["_needs_work"]:
                item["relations"].add("changes")
        open_reviews, review_filter_error = results["review_open"]
        open_ids = {self.normalize(pr)["id"] for pr in open_reviews or [] if isinstance(pr, dict)}
        for pr in results["review_all"][0] or open_reviews or []:
            if not isinstance(pr, dict):
                continue
            # Older versions lack the participantStatus filter; treat every review as open.
            still_open = review_filter_error or self.normalize(pr)["id"] in open_ids
            add(pr, "reviewer" if still_open else "participant")
        for pr in results["participant"][0] or []:
            add(pr, "participant")

        self._add_team_pull_requests(add, warnings)
        self._add_mentions(items, authored, warnings)

        result = []
        for item in items.values():
            prune_relations(item["relations"])
            for internal in ("_needs_work", "_open", "_scan"):
                item.pop(internal, None)
            result.append(item)
        return result, warnings

    def _add_team_pull_requests(
        self, add: Callable[[object, str], Item | None], warnings: list[str]
    ) -> None:
        def repo_prs(
            repo: tuple[str, str],
        ) -> tuple[tuple[str, str], list[Any], SourceError | None]:
            try:
                path = f"{self._repo_path(*repo)}/pull-requests"
                return repo, self.paged(path, {"state": "OPEN"}), None
            except SourceError as exc:
                return repo, [], exc

        with ThreadPoolExecutor(6) as pool:
            for (project, slug), prs, error in pool.map(repo_prs, self._team_repos(warnings)):
                if error:
                    warnings.append(f"Bitbucket: cannot read PRs of {project}/{slug} ({error}).")
                for pr in prs:
                    add(pr, "team")

    def _add_mentions(
        self, items: dict[str, Item], authored: list[Any], warnings: list[str]
    ) -> None:
        """Bitbucket has no mention filter, so comments of known open PRs are read."""
        me = self._whoami() or next(
            (
                ((pr.get("author") or {}).get("user") or {}).get("name")
                for pr in authored
                if isinstance(pr, dict)
            ),
            None,
        )
        if not me:
            warnings.append(
                "Bitbucket: your username is unknown, so mentions are not searched "
                '(please set "username" in the Bitbucket config).'
            )
            return
        days = int(self.cfg.get("mention_lookback_days", 30))
        cutoff = (time.time() - days * 86400) * 1000
        candidates = [
            item
            for item in items.values()
            if item["_open"]
            and "reporter" not in item["relations"]
            and (item["_scan"][3] or 0) >= cutoff
        ]
        candidates.sort(key=lambda item: -(item["_scan"][3] or 0))
        cap = int(self.cfg.get("max_mention_scans", 200))
        if len(candidates) > cap:
            warnings.append(
                f"Bitbucket: searched mentions only in the {cap} most recently updated PRs."
            )

        def scan(item: Item) -> tuple[Item, str | None, bool]:
            try:
                return item, self._mention_state(item["_scan"], me), False
            except (SourceError, ValueError, TypeError):
                return item, None, True

        failed = 0
        with ThreadPoolExecutor(6) as pool:
            for item, state, error in pool.map(scan, candidates[:cap]):
                failed += error
                if state == "open":
                    item["relations"].add("mentioned")
                elif state == "done":
                    item["relations"].add("mentioned_done")
        if failed:
            warnings.append(f"Bitbucket: cannot read comments of {failed} PR(s).")


SOURCE_CLASSES: dict[str, type[Source]] = {
    "jira": JiraSource,
    "gitlab": GitLabSource,
    "bitbucket": BitbucketSource,
}


# --------------------------------------------------------------------------- #
# Demo data
# --------------------------------------------------------------------------- #

DEMO_ME = "Sam Taylor"


def demo_items(sources: tuple[str, ...] = SOURCE_NAMES) -> list[Item]:
    """Return plausible sample items for the demo mode."""
    items = _demo_base_items()
    for item in items:
        if item["source"] == "jira" and "reviewer" in item["relations"]:
            item["relations"] = (set(item["relations"]) - {"reviewer"}) | {"assignee"}
    if "jira" not in sources:
        _convert_jira_demo_items(items)
    items += _demo_pull_requests()
    for item in items:
        if item["type"] in ("PR", "MR") and not item.get("branch"):
            slug = re.sub(r"[^a-z0-9]+", "-", item["title"].lower()).strip("-")[:28]
            item["branch"] = f"feature/{slug} → main"
        _make_demo_people_consistent(item)
    return [item for item in items if item["source"] in sources]


def _convert_jira_demo_items(items: list[Item]) -> None:
    repos = ["infra-live", "ci-templates", "billing-service", "helm-charts"]
    for index, item in enumerate(items):
        if item["source"] != "jira":
            continue
        repo, number = repos[index % len(repos)], 100 + index * 7
        if index % 3 == 0:
            renamed = {
                "assignee": "reviewer",
                "watcher": "participant",
                "mentioned": "participant",
                "commented": "participant",
            }
            relations = {renamed.get(r, r) for r in item["relations"]}
            if index % 6 == 0 and "reporter" in relations:
                relations.add("changes")
            status = (
                "Needs work"
                if "changes" in relations
                else "Draft"
                if item["state"] == "todo"
                else "1/2 approved"
            )
            item.update(
                id=f"bitbucket:pr:PLAT:{repo}:{number}",
                source="bitbucket",
                key=f"{repo}#{number}",
                project=f"PLAT/{repo}",
                type="PR",
                priority="",
                due=None,
                labels=[],
                relations=relations,
                status=status,
                state="todo" if item["state"] == "todo" else "in_progress",
                assignee="Anna Weber, Jonas Keller",
            )
        else:
            workflow = {"todo": "To Do", "in_progress": "Doing", "done": "Done"}[item["state"]]
            status = {"todo": "Open", "in_progress": "In progress", "done": "Closed"}[item["state"]]
            item.update(
                id=f"gitlab:issue:{number}",
                source="gitlab",
                key=f"{repo}#{number}",
                project=f"platform/{repo}",
                type="Issue",
                priority="",
                labels=["workflow::" + workflow],
                status=status,
            )


def _demo_pull_requests() -> list[Item]:
    samples = [
        (
            "bitbucket",
            "Upgrade Gradle wrapper to 8.10",
            "ci-pipelines",
            {"team"},
            "Open",
            "renovate/gradle-8.10",
        ),
        (
            "bitbucket",
            "Fix flaky timeout in export tests",
            "billing-service",
            {"mentioned"},
            "1/2 approved",
            "bugfix/export-timeout",
        ),
        (
            "bitbucket",
            "Introduce OpenTelemetry tracing",
            "platform-libs",
            {"mentioned_done"},
            "Open",
            "feature/otel-tracing",
        ),
        (
            "gitlab",
            "Bump ingress-nginx chart to 4.11",
            "helm-charts",
            {"team"},
            "Open",
            "renovate/ingress-nginx",
        ),
        (
            "gitlab",
            "Add cost labels to all namespaces",
            "infra-live",
            {"team"},
            "Draft",
            "feature/cost-labels",
        ),
        (
            "gitlab",
            "Rotate registry credentials job",
            "ci-templates",
            {"mentioned"},
            "Open",
            "feature/registry-rotation",
        ),
    ]
    now = datetime.now(UTC)
    result = []
    for index, (source, title, repo, relations, status, branch) in enumerate(samples):
        updated = (now - timedelta(hours=3 + index * 9)).isoformat()
        number = 300 + index
        bitbucket = source == "bitbucket"
        result.append(
            {
                "id": f"bitbucket:pr:PLAT:{repo}:{number}" if bitbucket else f"gitlab:mr:{number}",
                "source": source,
                "key": f"{repo}{'#' if bitbucket else '!'}{number}",
                "title": title,
                "url": "#",
                "project": f"PLAT/{repo}" if bitbucket else f"platform/{repo}",
                "status": status,
                "state": "todo" if status == "Draft" else "in_progress",
                "type": "PR" if bitbucket else "MR",
                "priority": "",
                "assignee": "Mara Schmid",
                "author": ["Anna Weber", "Jonas Keller", "Lukas Brandt"][index % 3],
                "updated": updated,
                "created": updated,
                "due": None,
                "labels": [],
                "relations": set(relations),
                "branch": f"{branch} → main",
            }
        )
    return result


def _make_demo_people_consistent(item: Item) -> None:
    """Only show the demo user as author/assignee where the relations say so."""
    relations = set(item["relations"])
    if "reviewer" in relations and "reporter" in relations:
        relations.discard("reporter")
        item["relations"] = relations
    if "reporter" in relations:
        item["author"] = DEMO_ME
    elif item["author"] == DEMO_ME:
        item["author"] = "Jonas Keller"
    if item["assignee"] == DEMO_ME and "assignee" not in relations:
        item["assignee"] = "Lukas Brandt"
    if "assignee" in relations and item["type"] != "PR":
        item["assignee"] = DEMO_ME


def _demo_base_items() -> list[Item]:
    rng = random.Random(7)  # noqa: S311 - deterministic demo data, not security relevant
    now = datetime.now(UTC)
    titles = [
        "Move the billing service Helm chart to the new ingress class",
        "Flaky integration test in the billing pipeline job",
        "Migrate staging Terraform state to a remote backend",
        "Renovate config: group base image updates",
        "Tighten the Prometheus alert for certificate expiry",
        "Runner cache keeps filling up: add a cleanup job",
        "Update the platform team onboarding docs",
        "Upgrade the dev cluster to Kubernetes 1.33",
        "Automate Artifactory secret rotation",
        "Analyse build times of the Java services",
        "Grafana dashboard for deployment frequency",
        "Standardise the logging format on JSON",
        "Add SBOM generation to CI",
        "Define a rollback strategy for database migrations",
        "Generate release notes from merge requests",
        "Ingress timeouts on large uploads",
        "Switch the base image to distroless",
        "Policy check for container registries",
    ]
    jira_states = [
        ("To Do", "todo"),
        ("In Progress", "in_progress"),
        ("In Review", "in_progress"),
        ("Blocked", "todo"),
        ("Done", "done"),
    ]
    relation_pool = ["assignee", "reporter", "watcher", "commented", "mentioned", "reviewer"]
    people = [DEMO_ME, "Anna Weber", "Jonas Keller", "Mara Schmid", "Lukas Brandt", ""]
    projects = {"PLAT": "Platform", "BILL": "Billing", "OPS": "Operations"}
    items: list[Item] = []
    for index, title in enumerate(titles):
        updated = now - timedelta(minutes=rng.randint(5, 60 * 24 * 20))
        relations = set(rng.sample(relation_pool, rng.choice([1, 1, 2, 2, 3])))
        if index % 4 == 1:
            relations = {"reporter"} | ({"commented"} if index % 8 == 1 else set())
        due = None
        if rng.random() < 0.4:
            due = (now + timedelta(days=rng.randint(-4, 21))).strftime("%Y-%m-%d")
        common = {
            "title": title,
            "url": "#",
            "updated": updated.isoformat(),
            "due": due,
            "relations": relations,
        }
        if index % 3:
            project = rng.choice(list(projects))
            key = f"{project}-{rng.randint(100, 2400)}"
            status, state = rng.choice(jira_states)
            issue_type = rng.choice(["Task", "Bug", "Story"])
            priority = rng.choice(["High", "Medium", "Low", "Highest"])
            items.append(
                {
                    **common,
                    "id": "jira:" + key,
                    "source": "jira",
                    "key": key,
                    "project": projects[project],
                    "status": status,
                    "state": state,
                    "type": issue_type,
                    "priority": priority,
                    "assignee": rng.choice(people),
                    "author": rng.choice(people[:5]),
                    "created": (updated - timedelta(days=9)).isoformat(),
                    "labels": rng.sample(["k8s", "ci", "security", "infra"], 1),
                }
            )
            continue
        kind = rng.choice(["issue", "mr"])
        project = rng.choice(
            ["platform/infra-live", "platform/ci-templates", "billing/invoice-service"]
        )
        iid = rng.randint(10, 900)
        if kind == "mr":
            relations.discard("watcher")
            relations.add(rng.choice(["reviewer", "reporter"]))
            status, state = rng.choice([("Open", "in_progress"), ("Draft", "todo")])
        else:
            status, state = rng.choice([("Open", "todo"), ("In progress", "in_progress")])
        items.append(
            {
                **common,
                "id": f"gitlab:{kind}:{index}",
                "source": "gitlab",
                "key": f"{project.split('/')[-1]}{'!' if kind == 'mr' else '#'}{iid}",
                "project": project,
                "status": status,
                "state": state,
                "type": "MR" if kind == "mr" else "Issue",
                "priority": "",
                "assignee": rng.choice(people),
                "author": rng.choice(people[:5]),
                "created": (updated - timedelta(days=4)).isoformat(),
                "labels": ["platform"],
            }
        )
    bitbucket = [
        ("Bump base image to distroless", "infra-images", "Needs work", {"reporter", "changes"}),
        ("Add retry to artifact upload step", "ci-pipelines", "1/2 approved", {"reviewer"}),
        ("Refactor invoice mapper", "billing-service", "Open", {"participant"}),
    ]
    for index, (title, repo, status, relations) in enumerate(bitbucket):
        updated = now - timedelta(hours=rng.randint(2, 200))
        items.append(
            {
                "id": f"bitbucket:pr:PLAT:{repo}:{index + 40}",
                "source": "bitbucket",
                "key": f"{repo}#{index + 40}",
                "title": title,
                "url": "#",
                "project": f"PLAT/{repo}",
                "status": status,
                "state": "in_progress",
                "type": "PR",
                "priority": "",
                "assignee": "Anna Weber, Jonas Keller",
                "author": DEMO_ME,
                "updated": updated.isoformat(),
                "created": (updated - timedelta(days=2)).isoformat(),
                "due": None,
                "labels": [],
                "relations": relations,
            }
        )
    return items


# --------------------------------------------------------------------------- #
# State and refresh
# --------------------------------------------------------------------------- #


VERIFY_SSL_WARNING = (
    "Security: verify_ssl is off, TLS certificates are not checked. "
    'Prefer pointing "ca_bundle" at your company CA certificate.'
)


class Store:
    """Holds the latest items per source and refreshes them in the background.

    With a ``config_path``, every refresh reads the config again if the file
    changed, so new URLs or tokens apply without a restart.
    """

    def __init__(
        self,
        cfg: dict[str, Any],
        demo: bool = False,
        demo_sources: tuple[str, ...] = SOURCE_NAMES,
        config_path: Path | None = None,
    ) -> None:
        self.cfg = cfg
        self.demo = demo
        self.demo_sources = demo_sources
        self.config_path = config_path
        self.config_mtime = self._mtime()
        self.lock = threading.Lock()
        self.refresh_lock = threading.Lock()
        self.by_source: dict[str, dict[str, Any]] = {}
        self.source_errors: dict[str, str] = {}
        self.token_origins: dict[str, str] = {}
        self.demo_cache: list[Item] | None = None
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.static_warnings: list[str] = []
        self.fetched_at: str | None = None
        self.refreshing = False
        self._check_verify_ssl()

    def _mtime(self) -> float | None:
        try:
            return self.config_path.stat().st_mtime if self.config_path else None
        except OSError:
            return None

    def _check_verify_ssl(self) -> None:
        if VERIFY_SSL_WARNING in self.static_warnings:
            self.static_warnings.remove(VERIFY_SSL_WARNING)
        if not self.demo and self.cfg.get("verify_ssl", True) is False:
            self.static_warnings.append(VERIFY_SSL_WARNING)

    def _reload_config(self) -> str | None:
        """Read the config again if it changed; return an error message if it is broken."""
        mtime = self._mtime()
        if self.demo or self.config_path is None or mtime == self.config_mtime:
            return None
        reason = untrusted_reason(self.config_path)
        if reason:
            return f"Config not read because {reason}; keeping the previous settings"
        try:
            user = json.loads(self.config_path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            user = exc
        if not isinstance(user, dict):
            reason = user if isinstance(user, Exception) else "not a JSON object"
            return f"Config: cannot read {self.config_path.name} ({reason}), keeping the previous settings"
        try:
            move_tokens_to_keychain(self.config_path, user)
        except SourceError as exc:
            return f"Config not read: {exc} Keeping the previous settings."
        self.cfg, self.config_mtime = merge_config(DEFAULT_CONFIG, user), self._mtime()
        self._check_verify_ssl()
        with self.lock:
            for name in list(self.by_source):
                if not (self.cfg.get(name) or {}).get("enabled"):
                    del self.by_source[name]
        return None

    def _sources(self) -> tuple[list[Source], dict[str, str]]:
        http = HttpClient(self.cfg)
        sources: list[Source] = []
        errors: dict[str, str] = {}
        for name in SOURCE_NAMES:
            section = self.cfg.get(name) or {}
            if not section.get("enabled"):
                continue
            try:
                source = SOURCE_CLASSES[name](section, http)
            except SourceError as exc:
                errors[name] = str(exc)
                self.token_origins.pop(name, None)
            except KeyError as exc:
                errors[name] = f"{name}: config entry {exc} is missing"
            else:
                sources.append(source)
                self.token_origins[name] = source.token_origin
        return sources, errors

    def refresh(self) -> None:
        if not self.refresh_lock.acquire(blocking=False):
            with self.refresh_lock:
                return
        try:
            self.refreshing = True
            now = datetime.now(UTC).isoformat()
            config_error = self._reload_config()
            source_errors: dict[str, str] = {}
            if self.demo:
                # The same sample data on every refresh, as a real server would return it;
                # fresh timestamps would mark everything as changed.
                if self.demo_cache is None:
                    self.demo_cache = demo_items(self.demo_sources)
                results = {"demo": (self.demo_cache, [], None)}
            else:
                sources, source_errors = self._sources()
                with ThreadPoolExecutor(3) as pool:
                    results = dict(pool.map(_fetch_safely, sources))
            errors = [config_error] if config_error else []
            errors += source_errors.values()
            warnings: list[str] = []
            with self.lock:
                for name, (items, source_warnings, error) in results.items():
                    warnings += source_warnings
                    if error:
                        stale = ", showing last known state" if name in self.by_source else ""
                        errors.append(f"{SOURCE_LABELS.get(name, name)}: {error}{stale}")
                        source_errors[name] = error
                        continue
                    self.by_source[name] = {"items": [coerce_item(i) for i in items], "ok_at": now}
                self.errors, self.warnings, self.fetched_at = errors, warnings, now
                self.source_errors = source_errors
        finally:
            self.refreshing = False
            self.refresh_lock.release()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            enabled = (
                list(self.demo_sources)
                if self.demo
                else [n for n in SOURCE_NAMES if (self.cfg.get(n) or {}).get("enabled")]
            )
            links = {
                name: str((self.cfg.get(name) or {}).get("url") or "").rstrip("/")
                for name in enabled
                if not self.demo
            }
            return {
                "items": [item for s in self.by_source.values() for item in s["items"]],
                "errors": self.errors,
                "warnings": self.static_warnings + self.warnings,
                "fetched_at": self.fetched_at,
                "refreshing": self.refreshing,
                "demo": self.demo,
                "version": VERSION,
                "links": links,
                "enabled": enabled,
                "sources": {name: s["ok_at"] for name, s in self.by_source.items()},
                "setup": self._setup(),
            }

    def _setup(self) -> dict[str, Any]:
        """Per source: enabled, URL, where the token comes from and the last result. No secrets."""
        if self.demo:
            return {"config": "", "sources": []}
        sources = []
        for name in SOURCE_NAMES:
            section = self.cfg.get(name) or {}
            sources.append(
                {
                    "name": name,
                    "label": SOURCE_LABELS[name],
                    "enabled": bool(section.get("enabled")),
                    "url": sanitize(section.get("url") or "", 300),
                    "token": self.token_origins.get(name, ""),
                    "ok_at": (self.by_source.get(name) or {}).get("ok_at"),
                    "error": sanitize(self.source_errors.get(name, ""), 500),
                }
            )
        return {"config": str(self.config_path or ""), "sources": sources}


RELATIONS = frozenset(
    {
        "assignee",
        "changes",
        "reporter",
        "reviewer",
        "commented",
        "mentioned",
        "mentioned_done",
        "watcher",
        "participant",
        "team",
    }
)
ITEM_TEXT_LIMITS = {
    "id": 300,
    "key": 200,
    "title": 500,
    "url": 2000,
    "project": 300,
    "status": 100,
    "type": 100,
    "priority": 100,
    "assignee": 500,
    "author": 200,
    "branch": 500,
}
ITEM_STATES = ("todo", "in_progress", "done")


def coerce_item(item: Item) -> Item:
    """Force every field into the type and size the UI expects.

    Values come from remote servers, so a malformed field must neither break
    rendering nor smuggle control or bidi characters into the page.
    """
    clean: Item = {"source": item.get("source") if item.get("source") in SOURCE_NAMES else "jira"}
    for field, limit in ITEM_TEXT_LIMITS.items():
        value = item.get(field)
        clean[field] = sanitize(value, limit).strip() if value is not None else ""
    clean["state"] = item.get("state") if item.get("state") in ITEM_STATES else "todo"
    for field in ("updated", "created"):
        value = item.get(field)
        clean[field] = sanitize(value, 40) if isinstance(value, str) else None
    due = item.get("due")
    clean["due"] = due[:10] if isinstance(due, str) and RE_DATE.match(due[:10]) else None
    labels = item.get("labels")
    clean["labels"] = (
        [sanitize(lbl, 100) for lbl in labels[:50]] if isinstance(labels, list) else []
    )
    clean["relations"] = sorted(RELATIONS.intersection(item.get("relations") or ()))
    return clean


def _fetch_safely(source: Source) -> tuple[str, tuple[list[Item], list[str], str | None]]:
    """Fetch one source; any failure becomes an error message instead of a crash."""
    try:
        items, warnings = source.fetch()
    except SourceError as exc:
        return source.name, ([], [], str(exc))
    except Exception as exc:  # noqa: BLE001 - one broken source must not stop the server
        return source.name, ([], [], f"Unexpected error ({type(exc).__name__})")
    return source.name, (items, warnings, None)


# --------------------------------------------------------------------------- #
# Local web server
# --------------------------------------------------------------------------- #

LOGIN_CODE_TTL_SECONDS = 15 * 60
TOKEN_HEADER = "X-OnMyPlate-Token"  # noqa: S105 - header name, not a secret


class LoginCodes:
    """One-time codes that the console link carries; each is exchanged once for a session."""

    def __init__(self) -> None:
        self._codes: dict[str, float] = {}
        self._lock = threading.Lock()

    def issue(self) -> str:
        code = secrets.token_urlsafe(24)
        with self._lock:
            self._codes[code] = time.monotonic() + LOGIN_CODE_TTL_SECONDS
        return code

    def redeem(self, code: str) -> bool:
        now = time.monotonic()
        with self._lock:
            self._codes = {c: exp for c, exp in self._codes.items() if exp > now}
            for known in list(self._codes):
                if hmac.compare_digest(known.encode(), code.encode()):
                    del self._codes[known]
                    return True
        return False


def content_security_policy(page: str) -> str:
    """Allow only the page's own inline script, identified by its hash."""
    script = page.split("<script>", 1)[1].split("</script>", 1)[0]
    digest = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode()
    return (
        f"default-src 'none'; script-src 'sha256-{digest}'; style-src 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'; object-src 'none'"
    )


class OnMyPlateServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, store: Store, secret: str) -> None:
        super().__init__(("127.0.0.1", port), RequestHandler)
        port = self.server_address[1]
        self.store = store
        self.session_token = session_token(secret, secrets.token_hex(16))
        self.login_codes = LoginCodes()
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.allowed_origins = {f"http://{host}" for host in self.allowed_hosts}
        self.csp = content_security_policy(INDEX_HTML)


class RequestHandler(BaseHTTPRequestHandler):
    """Serves the UI and its JSON API.

    The page itself holds no data and is served to anyone on loopback. Every
    API call must carry the session token in a custom header. Browsers never
    attach such a header on their own, and other origins (including other
    ports on 127.0.0.1) cannot read the token from this origin's storage, so
    neither CSRF nor a local web server can use the API.
    """

    server: OnMyPlateServer
    server_version = "onmyplate"
    sys_version = ""
    timeout = 30

    def log_message(self, fmt: str, *args: Any) -> None:
        """Silence access logs; they would only contain local paths."""

    def _send(
        self,
        code: int,
        body: object,
        content_type: str = "application/json; charset=utf-8",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        payload = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(payload)),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Cross-Origin-Opener-Policy": "same-origin",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Content-Security-Policy": self.server.csp,
            **(extra_headers or {}),
        }
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _check_request(self, path: str) -> bool:
        """Reject DNS rebinding and cross-site requests before anything else."""
        if self.headers.get("Host") not in self.server.allowed_hosts:
            self._send(403, {"error": "forbidden host"})
            return False
        if path.startswith("/api/"):
            if self.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
                self._send(403, {"error": "cross-site request"})
                return False
            origin = self.headers.get("Origin")
            if origin and origin not in self.server.allowed_origins:
                self._send(403, {"error": "forbidden origin"})
                return False
        return True

    def _authorize(self, path: str) -> bool:
        if not self._check_request(path):
            return False
        token = self.headers.get(TOKEN_HEADER) or ""
        if not hmac.compare_digest(token.encode(), self.server.session_token.encode()):
            self._send(401, {"error": "not signed in: open the link from the console"})
            return False
        return True

    def do_GET(self) -> None:  # stdlib hook name
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/", "/index.html"):
            if self._check_request(path):
                self._send(200, INDEX_HTML, "text/html; charset=utf-8")
            return
        if not self._authorize(path):
            return
        if path == "/api/items":
            self._send(200, self.server.store.snapshot())
        elif path == "/api/local":
            self._send(200, load_local())
        elif path == "/api/licenses":
            self._send(200, {"version": VERSION, **LICENSES})
        elif path == "/api/todos":
            self._send(
                200, {"todos": load_todos(), "file": str(PATHS.todos), "data_dir": str(PATHS.root)}
            )
        elif path == "/api/export":
            filename = f"onmyplate-backup-{datetime.now():%Y-%m-%d}.zip"
            self._send(
                200,
                make_backup(include_config=False),
                "application/zip",
                {"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        else:
            self._send(404, {"error": "not found"})

    def _read_json(self) -> dict[str, Any] | None:
        """Read a JSON object body strictly; on failure, send the error response."""
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            self._send(415, {"error": "json expected"})
            return None
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError:
            self._send(411, {"error": "length required"})
            return None
        if length <= 0:
            self._send(400, {"error": "empty body"})
            return None
        if length > MAX_REQUEST_BYTES:
            self._send(413, {"error": "too large"})
            return None
        try:
            data = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            data = None
        if not isinstance(data, dict):
            self._send(400, {"error": "invalid json"})
            return None
        return data

    def do_POST(self) -> None:  # stdlib hook name
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/login":
            self._login()
            return
        if not self._authorize(path):
            return
        if path == "/api/refresh":
            self.server.store.refresh()
            self._send(200, self.server.store.snapshot())
            return
        if path not in ("/api/local", "/api/todos"):
            self._send(404, {"error": "not found"})
            return
        data = self._read_json()
        if data is None:
            return
        if path == "/api/local":
            data.pop("todos", None)
            save_local(data)
        else:
            try:
                save_todos(validate_todos(data.get("todos")))
            except (TypeError, ValueError) as exc:
                self._send(400, {"error": str(exc)})
                return
        self._send(200, {"ok": True})

    def _login(self) -> None:
        """Exchange a one-time code from the console link for the session token."""
        if not self._check_request("/api/login"):
            return
        data = self._read_json()
        if data is None:
            return
        code = data.get("code")
        if not isinstance(code, str) or not self.server.login_codes.redeem(code):
            self._send(401, {"error": "login link expired or already used"})
            return
        self._send(200, {"token": self.server.session_token})


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Local dashboard for your issues and pull requests"
    )
    parser.add_argument(
        "--data-dir", help="folder for all data (default: $ONMYPLATE_HOME, else next to the script)"
    )
    parser.add_argument(
        "--config", help="path to the config file (default: <data folder>/onmyplate.config.json)"
    )
    parser.add_argument(
        "--port",
        type=int,
        help="port, 0 for any free one (default from the config, otherwise 8765)",
    )
    parser.add_argument(
        "--demo",
        nargs="?",
        const=",".join(SOURCE_NAMES),
        metavar="SOURCES",
        help="start with sample data and no network access, optionally for some "
        "sources only, e.g. --demo gitlab,bitbucket",
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="do not open the browser automatically"
    )
    parser.add_argument(
        "--export", metavar="ZIP", help="write a backup for moving to another machine and exit"
    )
    parser.add_argument(
        "--with-config",
        action="store_true",
        help="include the config with tokens in the export",
    )
    parser.add_argument(
        "--import", dest="import_zip", metavar="ZIP", help="restore a backup and exit"
    )
    parser.add_argument(
        "--set-token",
        choices=SOURCE_NAMES,
        metavar="SOURCE",
        help="store the token for jira, gitlab or bitbucket in the system keychain and exit",
    )
    parser.add_argument(
        "--delete-token",
        choices=SOURCE_NAMES,
        metavar="SOURCE",
        help="remove the stored token for jira, gitlab or bitbucket from the system keychain and exit",
    )
    parser.add_argument(
        "--licenses",
        action="store_true",
        help="show the license of On My Plate and of the third-party software it ships, and exit",
    )
    parser.add_argument(
        "--rotate-key",
        action="store_true",
        help="create a new access secret; every browser has to sign in again",
    )
    return parser.parse_args()


def licenses_text() -> str:
    """The license notices as plain text, as shown under ? > About & licenses."""
    own = LICENSES.get("self") or {}
    parts = [f"On My Plate {VERSION}: {own.get('license', 'MIT')} License", "", own.get("text", "")]
    if LICENSES.get("note"):
        parts += ["", "Third-party software", "", LICENSES["note"]]
    for group in LICENSES.get("groups") or []:
        for package in group.get("packages") or []:
            parts += ["", "-" * 72, f"{package['name']} {package['version']} ({group['title']})"]
            parts += [f"License: {package['license'] or 'see text'}"]
            parts += [f"Source: {package['url']}"] if package.get("url") else []
            parts += ["", *package.get("texts", [])]
    return "\n".join(parts).rstrip() + "\n"


def run_token(name: str, config_path: Path, delete: bool = False) -> None:
    """Store (asked for without echo, or read from a pipe) or delete a source's token."""
    cfg = load_config(config_path)
    if cfg is None:
        return
    label, url = SOURCE_LABELS[name], (cfg.get(name) or {}).get("url")
    if url == DEFAULT_CONFIG[name]["url"]:
        raise SystemExit(
            f"Set {name}.url in {config_path} first: the token is stored for that host."
        )
    keychain = system_keychain()
    if keychain is None:
        raise SystemExit(NO_KEYCHAIN.format(env=SOURCE_ENV_VARS[name]))
    try:
        account = token_account(name, url)
    except SourceError as exc:
        raise SystemExit(str(exc)) from None
    host = account.split("@", 1)[1]
    if delete:
        found = keychain.delete(account)
        print(
            f"{'Removed' if found else 'There was no'} {label} token for {host} in {keychain.label}."
        )
        return
    prompt = f"{label} token for {host} (not shown): "
    token = (getpass.getpass(prompt) if sys.stdin.isatty() else sys.stdin.readline()).strip()
    if not RE_STORABLE_TOKEN.fullmatch(token):
        raise SystemExit(
            "That is not a token: it is empty or contains spaces, quotes or control characters."
        )
    try:
        keychain.set(account, token)
    except SourceError as exc:
        raise SystemExit(str(exc)) from None
    print(f"Stored the {label} token for {host} in {keychain.label}.")
    print("Start On My Plate, or press Refresh (R) if it is already running.")


def run_export(target: Path, include_config: bool) -> None:
    migrate_legacy_todos()
    write_private(target, make_backup(include_config=include_config))
    print(f"Backup written: {target.resolve()}")
    if include_config:
        print("  Contains the config. Tokens stay in the keychain and are not included.")
    else:
        print("  Without config or tokens.")
    print("  On the new machine, store the tokens again with --set-token.")


def run_import(source: Path) -> None:
    restored, backed_up, stamp = import_backup(source)
    print(f"Restored into {PATHS.root}: {', '.join(restored)}")
    if backed_up:
        print(f"  Existing files kept as *.bak-{stamp}: {', '.join(backed_up)}")
    if CONFIG_FILE not in restored:
        print(
            "  Next: run python3 onmyplate.py, fill in the config and store tokens with --set-token."
        )
        return
    # A config from someone else could send your tokens elsewhere, so show where they go.
    cfg = merge_config(DEFAULT_CONFIG, json.loads(PATHS.config.read_text("utf-8")))
    print("  Check the imported config before starting. Tokens will be sent to:")
    for name in SOURCE_NAMES:
        section = cfg.get(name) or {}
        if section.get("enabled"):
            insecure = " (allow_insecure_http!)" if section.get("allow_insecure_http") else ""
            print(f"    {SOURCE_LABELS[name]}: {sanitize(section.get('url'), 200)}{insecure}")
    if cfg.get("verify_ssl", True) is False:
        print("  Warning: verify_ssl is off in the imported config.")


def load_config(path: Path) -> dict[str, Any] | None:
    """Return the merged config, or create a template and return None on first run."""
    reason = untrusted_reason(path)
    if reason:
        raise SystemExit(f"Not starting: the config decides where tokens go, but {reason}.")
    if not path.exists():
        write_private(path, json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False))
        print(CONFIG_HELP.format(path=path, script=Path(sys.argv[0]).name))
        return None
    user = json.loads(path.read_text("utf-8"))
    try:
        moved = move_tokens_to_keychain(path, user)
    except SourceError as exc:
        raise SystemExit(f"Not starting: {exc}") from None
    if moved:
        keychain = system_keychain()
        print(
            f"  Moved the {', '.join(moved)} token from {path.name} into {keychain.label if keychain else 'the keychain'}."
        )
    return merge_config(DEFAULT_CONFIG, user)


def open_browser(code: str, url: str) -> None:
    """Open the UI without putting the login code on a command line.

    Command lines of running processes are visible to other local users, so
    the browser gets the path of a private redirect file instead of the link.
    """
    target = json.dumps(f"{url}#code={code}")
    page = (
        "<!doctype html><meta charset='utf-8'><title>On My Plate</title>"
        f"<script>location.replace({target})</script>"
    )
    launcher = PATHS.root / f".onmyplate-open-{secrets.token_hex(6)}.html"
    write_private(launcher, page)
    webbrowser.open(launcher.as_uri())
    cleanup = threading.Timer(120, launcher.unlink, kwargs={"missing_ok": True})
    cleanup.daemon = True
    cleanup.start()


def refresh_forever(store: Store, interval_minutes: int) -> None:
    interval = max(1, interval_minutes) * 60
    while True:
        store.refresh()
        for error in store.errors:
            print("  ! " + sanitize(error, 500))
        time.sleep(interval)


def main() -> None:
    args = parse_args()
    if args.licenses:
        print(licenses_text(), end="")
        return
    if args.demo:
        set_data_dir(tempfile.mkdtemp(prefix="onmyplate-demo-"))
    else:
        set_data_dir(args.data_dir or os.environ.get("ONMYPLATE_HOME") or default_data_dir())

    if args.set_token or args.delete_token:
        config_path = Path(args.config).expanduser() if args.config else PATHS.config
        run_token(args.set_token or args.delete_token, config_path, delete=bool(args.delete_token))
        return
    if args.export:
        run_export(Path(args.export).expanduser(), args.with_config)
        return
    if args.import_zip:
        run_import(Path(args.import_zip).expanduser())
        return

    config_path = Path(args.config).expanduser() if args.config else PATHS.config
    cfg = merge_config(DEFAULT_CONFIG, {}) if args.demo else load_config(config_path)
    if cfg is None:
        return

    demo_sources = tuple(s for s in (args.demo or "").split(",") if s in SOURCE_NAMES)
    store = Store(
        cfg,
        demo=bool(args.demo),
        demo_sources=demo_sources or SOURCE_NAMES,
        config_path=None if args.demo else config_path,
    )
    if not args.demo and is_readable_by_others(config_path):
        message = (
            f"Security: {config_path.name} is readable by other users (it names your "
            f"servers and settings). Fix: chmod 600 {config_path}"
        )
        print("  ! " + message)
        store.static_warnings.append(message)
    migrate_legacy_todos()

    port = args.port if args.port is not None else int(cfg.get("port", 8765))
    server = OnMyPlateServer(port, store, load_secret(rotate=args.rotate_key))
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    login_url = f"{url}#code={server.login_codes.issue()}"
    threading.Thread(
        target=refresh_forever, args=(store, int(cfg.get("refresh_minutes", 10))), daemon=True
    ).start()
    print(f"On My Plate is running at {url}  (Ctrl+C to stop){'  [Demo]' if args.demo else ''}")
    print(f"Sign in (link valid for 15 minutes, single use, sessions end on restart):  {login_url}")
    print(f"Data folder: {PATHS.root}")
    if args.rotate_key:
        print("  New access secret created: every browser has to sign in again.")
    if not args.no_browser:
        threading.Timer(0.8, open_browser, args=(server.login_codes.issue(), url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>On My Plate</title>
<style>
:root {
  --bg: #f5f6f8; --panel: #ffffff; --panel-2: #f9fafb; --text: #17191c; --muted: #5b626d;
  --faint: #6a717d; --border: #e3e6ea; --border-strong: #cfd4da; --accent: #2f5bd3;
  --accent-soft: #e8eefc; --jira: #2f6fe0; --gitlab: #d9542b; --bitbucket: #138a8a; --danger: #c43d3d;
  --danger-soft: #fbeaea; --warn: #8a5f00; --warn-soft: #fdf4dc; --ok: #2b8a57; --ok-soft: #e5f4ec;
  --st-todo: #eef0f3; --st-todo-t: #505865; --st-prog: #e6eefc; --st-prog-t: #2a52b8;
  --st-done: #e5f4ec; --st-done-t: #23744a; --shadow: 0 1px 2px rgba(16,24,40,.05);
  --c-todo: #2f5bd3; --c-wait: #8a5f00; --c-watch: #6b5bd3; --c-team: #7a828e;
  --mono: ui-monospace, "SF Mono", "Cascadia Code", Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #111315; --panel: #181b1e; --panel-2: #1d2024; --text: #e7e9ec; --muted: #9aa2ad;
    --faint: #858c97; --border: #2a2e33; --border-strong: #383d44; --accent: #7b9cf5;
    --accent-soft: #1f2a44; --jira: #6c9cf4; --gitlab: #ef7a52; --bitbucket: #3fc1bd; --danger: #ef7676;
    --danger-soft: #3a1f21; --warn: #e3b341; --warn-soft: #33290f; --ok: #5cc48c; --ok-soft: #18321f;
    --st-todo: #24282d; --st-todo-t: #b2b9c3; --st-prog: #1e2a45; --st-prog-t: #8fb0f7;
    --st-done: #18321f; --st-done-t: #74cf98; --shadow: none;
    --c-todo: #7b9cf5; --c-wait: #e3b341; --c-watch: #a79bf5; --c-team: #8b939e;
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #111315; --panel: #181b1e; --panel-2: #1d2024; --text: #e7e9ec; --muted: #9aa2ad;
  --faint: #858c97; --border: #2a2e33; --border-strong: #383d44; --accent: #7b9cf5;
  --accent-soft: #1f2a44; --jira: #6c9cf4; --gitlab: #ef7a52; --bitbucket: #3fc1bd; --danger: #ef7676;
  --danger-soft: #3a1f21; --warn: #e3b341; --warn-soft: #33290f; --ok: #5cc48c; --ok-soft: #18321f;
  --st-todo: #24282d; --st-todo-t: #b2b9c3; --st-prog: #1e2a45; --st-prog-t: #8fb0f7;
  --st-done: #18321f; --st-done-t: #74cf98; --shadow: none;
  --c-todo: #7b9cf5; --c-wait: #e3b341; --c-watch: #a79bf5; --c-team: #8b939e;
  color-scheme: dark;
}
* { box-sizing: border-box; }
html, body { margin: 0; }
body {
  background: var(--bg); color: var(--text);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  -webkit-font-smoothing: antialiased;
}
button, input, select, textarea { font: inherit; color: inherit; }
a { color: inherit; }
[hidden] { display: none !important; }
.wrap { max-width: 1600px; margin: 0 auto; padding-block: 18px 40px;
  padding-left: max(20px, env(safe-area-inset-left, 0px)); padding-right: max(20px, env(safe-area-inset-right, 0px)); }

/* Header */
header { display: flex; align-items: center; gap: 14px; margin-bottom: 16px; flex-wrap: wrap; }
.brand { display: flex; align-items: baseline; gap: 10px; }
.brand h1 { font-size: 18px; margin: 0; letter-spacing: -.01em; }
.brand .demo { font-size: 11px; padding: 1px 7px; border-radius: 99px; background: var(--warn-soft); color: var(--warn); }
.meta { color: var(--muted); font-size: 13px; }
.spacer { flex: 1; }
.btn {
  border: 1px solid var(--border-strong); background: var(--panel); border-radius: 8px;
  padding: 6px 12px; cursor: pointer; display: inline-flex; align-items: center; gap: 6px; text-decoration: none;
}
.btn:hover { border-color: var(--accent); }
.btn.sm { padding: 2px 9px; font-size: 12px; border-radius: 6px; }
.btn.on { background: var(--accent-soft); border-color: var(--accent); color: var(--accent); }
.btn.spin svg { animation: spin 1s linear infinite; }
.btn.icon { padding: 6px 9px; min-width: 34px; justify-content: center; font-weight: 600; }
.btn[aria-pressed="true"] { background: var(--accent-soft); border-color: var(--accent); color: var(--accent); }
.btn:focus-visible, .seg button:focus-visible, .stat:focus-visible, .clear:focus-visible, .ib:focus-visible, a:focus-visible {
  outline: 2px solid var(--accent); outline-offset: 2px; }
@media (prefers-reduced-motion: reduce) { .btn.spin svg { animation: none; } * { scroll-behavior: auto !important; } }
@keyframes spin { to { transform: rotate(360deg); } }
.kbd { font-family: var(--mono); font-size: 11px; color: var(--faint); border: 1px solid var(--border); border-radius: 4px; padding: 0 4px; }

.banner { border-radius: 8px; padding: 9px 12px; margin-bottom: 10px; font-size: 13px; }
.banner.err { background: var(--danger-soft); color: var(--danger); }
.banner.warn { background: var(--warn-soft); color: var(--warn); }
.banner ul { margin: 4px 0 0; padding-left: 18px; }
.banner summary { cursor: pointer; }

/* KPIs */
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(118px, 1fr)); gap: 8px; margin-bottom: 14px; }
.view-list .stats { grid-template-columns: repeat(auto-fit, minmax(92px, 1fr)); gap: 6px; }
.view-list .stat { padding: 6px 10px; }
.view-list .stat .n { font-size: 17px; }
.view-list .stat .l { font-size: 11.5px; line-height: 1.25; }
.view-dash .stats { grid-template-columns: repeat(5, minmax(0, 1fr)); }
.view-issues .stats { grid-template-columns: repeat(4, minmax(0, 1fr)); }
.view-prs .stats { grid-template-columns: repeat(4, minmax(0, 1fr)); }
.view-dash .stat, .view-issues .stat, .view-prs .stat { padding: 8px 12px 8px 15px; display: flex; align-items: baseline; gap: 8px; flex-wrap: nowrap; min-width: 0; }
/* Extra text wraps below the label instead of being cut off. */
.view-dash .stat, .view-issues .stat, .view-prs .stat { flex-wrap: wrap; row-gap: 0; }
.view-dash .stat .kx, .view-issues .stat .kx, .view-prs .stat .kx { white-space: normal; flex-basis: 100%; min-width: 0; }
.view-dash .stat .l, .view-issues .stat .l, .view-prs .stat .l { white-space: nowrap; }
.view-dash .stat .n, .view-issues .stat .n, .view-prs .stat .n { font-size: 19px; }
.view-dash .stat .l, .view-issues .stat .l, .view-prs .stat .l { margin-top: 0; font-size: 12.5px; color: var(--text); }
.stat .kx { color: var(--muted); font-size: 12px; }
.stat {
  background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px;
  text-align: left; cursor: pointer; box-shadow: var(--shadow); position: relative; overflow: hidden;
}
.stat.static { cursor: default; }
.stat .go { position: absolute; right: 9px; top: 50%; transform: translateY(-50%); color: var(--muted); font-size: 15px; line-height: 1; }
button.stat[data-goto] { padding-right: 24px !important; }
.stat.on { border-color: var(--accent); background: var(--accent-soft); }
.stat .n { font-size: 22px; font-weight: 650; font-variant-numeric: tabular-nums; line-height: 1.1; }
.stat .l { color: var(--muted); font-size: 12px; margin-top: 2px; }
.stat .n small { font-size: 13px; font-weight: 550; color: var(--muted); margin-left: 4px; }
.stat.alert .n { color: var(--danger); }
.stat .bar { position: absolute; left: 0; top: 0; bottom: 0; width: 3px; }

/* Toolbar */
.toolbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 14px; }
.search { position: relative; flex: 1 1 240px; }
.search input {
  width: 100%; padding: 7px 10px 7px 32px; border: 1px solid var(--border-strong);
  border-radius: 8px; background: var(--panel); outline: none;
}
.search input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
.search svg { position: absolute; left: 10px; top: 50%; transform: translateY(-50%); color: var(--faint); }
.seg { display: inline-flex; border: 1px solid var(--border-strong); border-radius: 8px; overflow: hidden; background: var(--panel); }
.seg button { border: 0; background: none; padding: 6px 11px; cursor: pointer; color: var(--muted); }
.seg button + button { border-left: 1px solid var(--border); }
.seg button.on { background: var(--accent-soft); color: var(--accent); font-weight: 600; }
select {
  border: 1px solid var(--border-strong); border-radius: 8px; background: var(--panel);
  padding: 6px 8px; cursor: pointer;
}
.lbl { color: var(--muted); font-size: 12px; }
.short { display: none; }
.view-dash .list-only, .view-issues .list-only, .view-prs .list-only, .view-list .dash-only { display: none !important; }

/* Layout */
.layout { display: grid; grid-template-columns: minmax(0, 1fr) 320px; gap: 16px; align-items: start; }
.card { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; box-shadow: var(--shadow); }
.layout.noside { grid-template-columns: minmax(0, 1fr); }
.layout.noside aside { display: none; }

/* Board columns */
.dash { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr) minmax(0, 1fr); gap: 16px; align-items: start; }
.col { min-width: 0; }
.col > .chead {
  padding: 12px 14px 10px; border-bottom: 1px solid var(--border); display: flex; gap: 10px; align-items: flex-start;
  border-top: 3px solid var(--cc); border-radius: 12px 12px 0 0;
}
.chead .ttl { margin: 0; font-weight: 650; font-size: 15px; display: flex; align-items: baseline; gap: 8px; }
.chead .ttl .cnt { color: var(--muted); font-weight: 500; font-size: 13px; font-variant-numeric: tabular-nums; }
.chead .hint { color: var(--muted); font-size: 12px; margin-top: 1px; }
.col[data-col=todo] { --cc: var(--c-todo); }
.col[data-col=waiting] { --cc: var(--c-wait); }
.col[data-col=watch] { --cc: var(--c-watch); }
.col[data-col=todo-issues], .col[data-col=todo-prs], .col[data-col=next] { --cc: var(--c-todo); }
.col[data-col=changed] { --cc: var(--c-watch); }
.overview .col[data-col=waiting] { --cc: var(--c-wait); }
.overview { display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(0, 1fr); gap: 16px; align-items: start; }
.ostack { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
.chead .clear { margin-left: auto; white-space: nowrap; align-self: center; }
.view-dash .stats { grid-template-columns: repeat(5, minmax(0, 1fr)); }
button.stat { font: inherit; color: inherit; }
button.stat:hover { border-color: var(--accent); }
button.stat:hover .go { color: var(--accent); }
.why { font-size: 12px; font-weight: 600; color: var(--muted); }
.why.warn { color: var(--warn); } .why.crit { color: var(--danger); }
.badge.wide { text-transform: none; letter-spacing: 0; font-weight: 600; }
.dash.four { grid-template-columns: repeat(4, minmax(0, 1fr)); }
.col[data-col="pr:review"] { --cc: var(--c-todo); }
.col[data-col="pr:mine"] { --cc: var(--c-wait); }
.col[data-col="pr:involved"] { --cc: var(--c-watch); }
.col[data-col="pr:team"] { --cc: var(--c-team); }
.br { font-family: var(--mono); font-size: 11.5px; color: var(--faint); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 100%; }
.badge.at { background: var(--accent-soft); color: var(--accent); text-transform: none; }
.sec { margin: 0; padding: 10px 14px 4px; font-size: 11px; font-weight: 650; letter-spacing: .04em; text-transform: uppercase; color: var(--muted); display: flex; gap: 6px; }
.sec .cnt { color: var(--faint); }
.sec.alert { color: var(--danger); }
.cfoot { padding: 8px 14px 12px; font-size: 12px; color: var(--faint); display: flex; gap: 10px; flex-wrap: wrap; }
.cempty { padding: 18px 14px 20px; color: var(--muted); font-size: 13px; }

.drow { position: relative; padding: 8px 14px 9px 14px; border-top: 1px solid var(--border); }
.sec + .drow { border-top: 0; }
.drow:hover, .drow.open { background: var(--panel-2); }
.drow.cur, .row.cur { background: var(--accent-soft); box-shadow: inset 4px 0 0 var(--accent); }
.drow:focus, .row:focus { outline: none; }
.drow:focus-visible, .row:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.drow.cur .hov { display: flex; }
.drow.cur .l1 .status { visibility: hidden; }
.drow .l1 { display: flex; align-items: center; gap: 7px; min-width: 0; padding-right: 2px; }
.drow .l1 .spacer { flex: 1; }
.drow .ttl {
  margin-top: 2px; cursor: pointer; font-weight: 500; display: -webkit-box; -webkit-line-clamp: 2;
  -webkit-box-orient: vertical; overflow: hidden; word-break: break-word;
}
.drow .ttl:hover { color: var(--accent); }
.drow .l3 { display: flex; flex-wrap: wrap; gap: 4px 8px; margin-top: 4px; font-size: 12px; color: var(--muted); align-items: center; }
.drow.quiet .ttl { color: var(--muted); font-weight: 450; }
.drow.dim { opacity: .55; }
.badge { font-size: 10.5px; font-weight: 700; padding: 0 6px; border-radius: 4px; letter-spacing: .02em; text-transform: uppercase; }
.badge.new { background: var(--ok-soft); color: var(--ok); }
.badge.chg { background: var(--accent-soft); color: var(--accent); }
.badge.man { background: var(--st-todo); color: var(--st-todo-t); }
.hov { position: absolute; right: 10px; top: 6px; display: none; gap: 2px; background: var(--panel-2); border-radius: 6px; }
.drow:hover .hov { display: flex; }
.drow:hover .l1 .status { visibility: hidden; }
.ib { border: 1px solid transparent; background: none; border-radius: 6px; padding: 3px 5px; cursor: pointer; color: var(--muted); line-height: 0; }
.ib:hover { border-color: var(--border-strong); color: var(--text); background: var(--panel); }
.ib.on { color: #e0a100; }
.ib.word { line-height: 1; font-size: 11.5px; font-weight: 600; padding: 3px 7px; }

/* List view */
.group + .group { border-top: 1px solid var(--border); }
.ghead {
  display: flex; align-items: center; gap: 8px; padding: 10px 14px; cursor: pointer; user-select: none;
  font-weight: 600; font-size: 13px; position: sticky; top: 0; background: var(--panel-2); z-index: 1;
}
.group:first-child .ghead { border-radius: 12px 12px 0 0; }
.ghead .cnt { color: var(--muted); font-weight: 500; font-variant-numeric: tabular-nums; }
.ghead .chev { transition: transform .15s; color: var(--faint); }
.group.closed .chev { transform: rotate(-90deg); }
.group.closed .rows { display: none; }
.row {
  display: grid; grid-template-columns: 22px minmax(0, 1fr) auto; gap: 10px; align-items: start;
  padding: 9px 14px; border-top: 1px solid var(--border);
}
.row:hover, .row.open { background: var(--panel-2); }
.pin { border: 0; background: none; padding: 2px 0 0; cursor: pointer; color: var(--faint); line-height: 0; }
.pin:hover { color: var(--warn); }
.pin.on { color: #e0a100; }
.main { min-width: 0; }
.line1 { display: flex; align-items: baseline; gap: 8px; min-width: 0; }
.dot { width: 8px; height: 8px; border-radius: 50%; flex: none; }
.dot.jira { background: var(--jira); } .dot.gitlab { background: var(--gitlab); } .dot.bitbucket { background: var(--bitbucket); }
.key { font-family: var(--mono); font-size: 12px; color: var(--muted); text-decoration: none; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.key:hover { color: var(--accent); text-decoration: underline; }
a.key { text-decoration: underline dotted; text-underline-offset: 3px; text-decoration-color: var(--border-strong); }
.rtop { display: flex; gap: 6px; align-items: center; }
.ib.ext { color: var(--accent); }
.ib.off, .btn.off { color: var(--muted); cursor: not-allowed; opacity: .6; }
.btn.off:hover { border-color: var(--border-strong); }
span.key { text-decoration: none; }
.pinrow .ib { padding: 2px 4px; }
.title { cursor: pointer; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 500; }
.title:hover { color: var(--accent); }
.line2 { display: flex; flex-wrap: wrap; gap: 4px 10px; margin-top: 3px; padding-left: 16px; font-size: 12px; color: var(--muted); }
.due-late { color: var(--danger) !important; font-weight: 600; }
.due-soon { color: var(--warn) !important; font-weight: 600; }
.right { display: flex; flex-direction: column; align-items: flex-end; gap: 5px; }
.status { font-size: 11.5px; padding: 1px 8px; border-radius: 99px; white-space: nowrap; font-weight: 550; flex: none; }
.status.todo { background: var(--st-todo); color: var(--st-todo-t); }
.status.in_progress { background: var(--st-prog); color: var(--st-prog-t); }
.status.done { background: var(--st-done); color: var(--st-done-t); }
.rels { display: flex; gap: 4px; flex-wrap: wrap; justify-content: flex-end; }
.rel { font-size: 11px; padding: 0 6px; border-radius: 4px; border: 1px solid var(--border); color: var(--muted); white-space: nowrap; }
.rel.assignee, .rel.reviewer { border-color: var(--accent); color: var(--accent); }
.rel.changes { border-color: var(--danger); color: var(--danger); }

.detail { grid-column: 2 / 4; padding: 6px 0 4px 16px; display: grid; gap: 8px; }
.drow .detail { padding: 8px 0 2px; }
.detail .facts { display: flex; flex-wrap: wrap; gap: 4px 16px; font-size: 12px; color: var(--muted); }
.detail .facts b { color: var(--text); font-weight: 550; }
.tags { display: flex; gap: 4px; flex-wrap: wrap; }
.tag { font-size: 11px; background: var(--st-todo); color: var(--st-todo-t); border-radius: 4px; padding: 0 6px; }
textarea {
  width: 100%; min-height: 60px; resize: vertical; border: 1px solid var(--border-strong);
  border-radius: 8px; padding: 7px 9px; background: var(--panel); outline: none;
}
textarea:focus { border-color: var(--accent); }
.detail .actions { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
.empty { padding: 40px 16px; text-align: center; color: var(--muted); }

/* Sidebar */
aside { position: sticky; top: 16px; display: grid; grid-template-columns: minmax(0, 1fr); gap: 16px; min-width: 0; }
aside h2 { font-size: 13px; margin: 0; padding: 12px 14px 8px; display: flex; justify-content: space-between; }
aside h2 span { color: var(--muted); font-weight: 500; }
.todo-add { display: flex; gap: 6px; padding: 0 14px 10px; flex-wrap: wrap; }
.todo-add input[type=date], .tedit input[type=date] { flex: 1 1 130px; width: auto; min-width: 0; }
.todo-add .due-lbl { display: flex; align-items: center; gap: 6px; flex: 1 1 150px; min-width: 0; color: var(--muted); font-size: 12px; }
.todo-add .due-lbl input { flex: 1; }
.todo-add #todoInput { flex: 1 1 100%; }
.tedit { display: flex; gap: 6px; flex-wrap: wrap; width: 100%; }
.tedit input {
  flex: 1 1 160px; min-width: 0; padding: 4px 8px; border: 1px solid var(--accent);
  border-radius: 6px; background: var(--panel); outline: none;
}
.tdue { margin-left: 6px; font-size: 12px; color: var(--muted); white-space: nowrap; }
.tlist .txt { cursor: text; }
.foot { margin-top: 4px; color: var(--faint); font-size: 12px; display: flex; gap: 4px 8px; flex-wrap: wrap; align-items: baseline; line-height: 1.5; }
.foot code { font-family: var(--mono); word-break: break-all; color: var(--muted); }
.foot a { color: var(--accent); }
.todo-add input {
  flex: 1; min-width: 0; padding: 6px 9px; border: 1px solid var(--border-strong);
  border-radius: 8px; background: var(--panel); outline: none;
}
.todo-add input:focus { border-color: var(--accent); }
.tlist { list-style: none; margin: 0; padding: 0 0 6px; }
.tlist li { display: flex; gap: 8px; align-items: flex-start; padding: 5px 14px; }
.tlist li:hover { background: var(--panel-2); }
.tlist li.done .txt { text-decoration: line-through; color: var(--faint); }
.tlist input[type=checkbox] { margin-top: 3px; accent-color: var(--accent); }
.tlist .txt { flex: 1; min-width: 0; word-break: break-word; }
.tlist .txt a { font-family: var(--mono); font-size: 12px; color: var(--muted); }
.x { border: 0; background: none; color: var(--faint); cursor: pointer; padding: 0 2px; visibility: hidden; }
.tlist li:hover .x, .tlist li:focus-within .x { visibility: visible; }
.x:hover { color: var(--danger); }
.sub { color: var(--faint); font-size: 12px; padding: 4px 14px 12px; }
.pinrow { display: grid; grid-template-columns: auto minmax(0, 1fr) auto auto; column-gap: 7px; row-gap: 1px; padding: 7px 14px; align-items: center; cursor: pointer; }
.pinrow:hover { background: var(--panel-2); }
.pinrow .key { grid-column: 2; }
.pinrow .t { grid-column: 2 / -1; grid-row: 2; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.clear { border: 0; background: none; color: var(--accent); cursor: pointer; font-size: 12px; padding: 0; }

/* Toast */
.toast { position: fixed; left: 50%; bottom: 20px; transform: translateX(-50%); z-index: 60; display: flex; gap: 14px; align-items: center;
  background: var(--text); color: var(--bg); border-radius: 10px; padding: 9px 14px; font-size: 13px; box-shadow: 0 8px 30px rgba(0,0,0,.25); max-width: calc(100% - 32px); }
.toast button { border: 0; background: none; color: inherit; font: inherit; font-weight: 700; text-decoration: underline; cursor: pointer; padding: 4px; }
.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }

/* Setup and about */
.setup { padding: 14px 18px 4px; border-top: 1px solid var(--border); font-size: 13px; }
.setup h4 { margin: 0 0 8px; font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); }
.setup table { width: 100%; border-collapse: collapse; }
.setup td, .setup th { text-align: left; padding: 6px 8px 6px 0; border-top: 1px solid var(--border); vertical-align: top; }
.setup th { font-size: 11px; color: var(--muted); font-weight: 600; border-top: 0; }
.setup .ok { color: var(--ok); font-weight: 600; } .setup .bad { color: var(--danger); font-weight: 600; } .setup .off { color: var(--muted); }
.setup p { margin: 8px 0; color: var(--muted); }
.setup code, .about code { font-family: var(--mono); font-size: 12px; word-break: break-all; color: var(--text); }
.banner .clear { font-size: 13px; margin-left: 6px; }

/* Help overlay */
.overlay { position: fixed; inset: 0; background: rgba(10, 12, 16, .45); display: grid; place-items: center; z-index: 50; padding: 16px; }
.dialog { background: var(--panel); border: 1px solid var(--border); border-radius: 14px; width: min(760px, 100%); max-height: 90vh; overflow: auto; box-shadow: 0 20px 60px rgba(0,0,0,.25); }
.dhead { display: flex; justify-content: space-between; align-items: center; padding: 14px 18px; border-bottom: 1px solid var(--border); }
.dhead h3 { margin: 0; font-size: 15px; }
.keys { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 18px; padding: 16px 18px 20px; }
.keys h4 { margin: 0 0 8px; font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); }
.keys dl { margin: 0; display: grid; grid-template-columns: auto 1fr; gap: 7px 10px; align-items: baseline; font-size: 13px; }
.keys dt { white-space: nowrap; } .keys dd { margin: 0; color: var(--muted); }
.about { display: flex; flex-wrap: wrap; gap: 6px 14px; align-items: baseline; padding: 12px 18px 16px; border-top: 1px solid var(--border); font-size: 13px; color: var(--muted); }
.licenses { padding: 0 18px 18px; font-size: 13px; }
.licenses h4 { margin: 14px 0 6px; font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); }
.licenses p { margin: 0 0 8px; color: var(--muted); }
.licenses pre { margin: 0 0 8px; padding: 10px 12px; background: var(--panel-2); border: 1px solid var(--border); border-radius: 8px; white-space: pre-wrap; font: 11.5px/1.5 var(--mono); color: var(--text); }
.licenses summary { cursor: pointer; padding: 4px 0; }
kbd { font-family: var(--mono); font-size: 11.5px; border: 1px solid var(--border-strong); border-bottom-width: 2px; border-radius: 5px; padding: 0 5px; background: var(--panel-2); color: var(--text); }

/* Wide screens: app layout without page scroll, each column scrolls on its own */
@media (min-width: 1181px) {
  html, body { height: 100%; }
  body { overflow: hidden; }
  .wrap { height: 100%; display: flex; flex-direction: column; padding-block: 14px 0; max-width: none; }
  header { margin-bottom: 12px; }
  .stats { margin-bottom: 12px; }
  .toolbar { margin-bottom: 12px; }
  .layout { flex: 1; min-height: 0; grid-template-columns: minmax(0, 1fr) 300px; align-items: stretch; }
  #list { min-height: 0; overflow: hidden; padding-bottom: 14px; }
  .dash { height: 100%; align-items: start; }
  .col { max-height: 100%; overflow: auto; overscroll-behavior: contain; }
  .col > .chead { position: sticky; top: 0; background: var(--panel); z-index: 2; }
  .overview { height: 100%; }
  .overview > .col { max-height: 100%; }
  .ostack { height: 100%; min-height: 0; }
  .ostack > .col { flex: 0 1 auto; min-height: 0; overflow: auto; }
  .view-list #list > .card { max-height: 100%; overflow: auto; overscroll-behavior: contain; }
  aside { position: static; overflow: auto; min-height: 0; align-content: start; padding-bottom: 14px; }
  .foot { margin-top: 0; padding: 0 2px; }
}

@media (max-width: 1180px) {
  /* Two columns and a narrow sidebar: the overview stays side by side on small laptops. */
  .layout { grid-template-columns: minmax(0, 1fr) 260px; }
  .overview { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
  .dash { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .dash:not(.four) .col[data-col=todo] { grid-column: 1 / -1; }
  .dash.four { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 900px) {
  .overview { grid-template-columns: minmax(0, 1fr); }
  .view-prs .stats, .view-issues .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .layout { grid-template-columns: 1fr; }
  aside { position: static; grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .stats, .view-dash .stats { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}
@media (max-width: 720px) {
  .wrap { padding-block: 14px 30px; padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px)); }
  .keys { grid-template-columns: minmax(0, 1fr); }
  .dash { grid-template-columns: minmax(0, 1fr); }
  .stats, .view-dash .stats, .view-issues .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  aside, .view-dash aside, .view-prs aside { grid-template-columns: minmax(0, 1fr); }
  .dash.four { grid-template-columns: minmax(0, 1fr); }
  .row { grid-template-columns: 22px minmax(0, 1fr); }
  .right { grid-column: 2; flex-direction: row; align-items: center; justify-content: flex-start; flex-wrap: wrap; }
  .rels { justify-content: flex-start; }
  .detail { grid-column: 1 / 3; padding-left: 0; }
  .title { white-space: normal; }
  .kbd { display: none; }
  .hov { display: flex; position: static; background: none; margin-top: 4px; }
  .drow:hover .l1 .status, .drow.cur .l1 .status { visibility: visible; }
  /* A compact header: one scrolling row of key figures and icon-only buttons. */
  header { gap: 8px 10px; margin-bottom: 10px; }
  .meta { flex-basis: 100%; order: 1; font-size: 12px; }
  header .spacer { display: none; }
  #view { order: 2; flex: 1 1 auto; } #view button { flex: 1; padding: 6px 8px; }
  #view .full { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
  #view .short { display: inline; }
  #refresh, #sideBtn, #helpBtn { order: 2; }
  #refresh .lbl-txt { display: none; }
  .stats, .view-dash .stats, .view-issues .stats, .view-prs .stats, .view-list .stats {
    display: flex; overflow-x: auto; scrollbar-width: none; gap: 6px; margin-bottom: 10px; }
  .stats::-webkit-scrollbar { display: none; }
  .stats .stat { flex: 0 0 auto; padding: 6px 26px 6px 12px !important; }
  .stats .stat .kx { display: none; }
  .toolbar { margin-bottom: 10px; }
  .chead .hint { display: none; }
}
@media (pointer: coarse) {
  .kbd, .kbd-hint { display: none !important; }
  .ib { min-width: 40px; min-height: 40px; display: inline-grid; place-items: center; }
  .btn, .seg button, select { min-height: 40px; }
  .tlist li { padding-block: 8px; }
  .tlist input[type=checkbox] { width: 24px; height: 24px; margin-top: 0; }
  .x { visibility: visible; min-width: 36px; min-height: 36px; }
  .clear { padding: 8px 0; }
}
</style>
</head>
<body class="view-dash">
<div class="wrap">
  <header>
    <div class="brand"><h1>On My Plate</h1><span id="demo" class="demo" hidden>Demo data</span></div>
    <span class="meta" id="meta">Loading …</span>
    <div class="spacer"></div>
    <div class="seg" id="view" role="group" aria-label="View"><button data-v="dash" title="Dashboard (D)">Dashboard</button><button data-v="issues" title="Issues (I)">Issues</button><button data-v="prs" title="Pull Requests (P)"><span class="full">Pull Requests</span><span class="short" aria-hidden="true">PRs</span></button><button data-v="list" title="List (L)">List</button></div>
    <button class="btn" id="refresh" title="Refresh (R)">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 4v5h-5"/></svg>
      <span class="lbl-txt">Refresh</span> <span class="kbd">R</span>
    </button>
    <button class="btn icon" id="sideBtn" title="Show/hide focus &amp; tasks (F)" aria-label="Show focus and tasks" aria-pressed="true">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M15 4v16"/></svg>
    </button>
    <button class="btn icon" id="helpBtn" title="Shortcuts, sources and about (?)" aria-label="Shortcuts, sources and about">?</button>
  </header>

  <div id="banners"></div>
  <div class="stats" id="stats"></div>

  <div class="toolbar">
    <label class="search" for="q">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
      <input id="q" type="search" placeholder="Search: key, title, project, person, label …  ( / )" aria-label="Search" autocomplete="off">
    </label>
    <div class="seg" id="source" role="group" aria-label="Source">
      <button data-v="all">All</button><button data-v="jira">Jira</button><button data-v="gitlab">GitLab</button><button data-v="bitbucket">Bitbucket</button>
    </div>
    <div class="seg list-only" id="states" role="group" aria-label="Status">
      <button data-v="todo">Open</button><button data-v="in_progress">In progress</button><button data-v="done">Closed</button>
    </div>
    <label class="lbl list-only" for="group">Group</label>
    <select id="group" class="list-only">
      <option value="state">Status</option><option value="project">Project</option>
      <option value="source">Source</option><option value="relation">Relation</option><option value="none">None</option>
    </select>
    <label class="lbl list-only" for="sort">Sort</label>
    <select id="sort" class="list-only">
      <option value="updated">Last updated</option><option value="due">Due date</option>
      <option value="priority">Priority</option><option value="key">Key</option>
    </select>
  </div>

  <div class="layout">
    <main id="list"></main>
    <aside>
      <section class="card" id="focusCard">
        <h2>Focus <span id="pinCount"></span></h2>
        <div id="pins"></div>
      </section>
      <section class="card">
        <h2>My tasks <span id="todoCount"></span></h2>
        <form class="todo-add" id="todoForm">
          <input id="todoInput" placeholder="New task …" aria-label="New task" autocomplete="off" maxlength="1000">
          <label class="due-lbl">Due <input id="todoDue" type="date" title="Due date (optional)" aria-label="Due date (optional)"></label>
          <button class="btn" type="submit" title="Add task" aria-label="Add task">Add</button>
        </form>
        <ul class="tlist" id="todos"></ul>
        <div class="sub" id="todoFoot"></div>
      </section>
    </aside>
  </div>
</div>

<div id="toast" class="toast" hidden></div>
<div id="live" class="sr-only" role="status" aria-live="polite"></div>

<div class="overlay" id="help" hidden>
  <div class="dialog" role="dialog" aria-modal="true" aria-labelledby="helpTitle">
    <div class="dhead"><h3 id="helpTitle">Shortcuts, sources &amp; about</h3><button class="btn icon" data-act="help-close" title="Close (Esc)" aria-label="Close">✕</button></div>
    <div class="keys">
      <section><h4>Navigate</h4>
        <dl><dt><kbd>J</kbd> <kbd>K</kbd> / <kbd>↓</kbd> <kbd>↑</kbd></dt><dd>Next / previous item</dd>
            <dt><kbd>←</kbd> <kbd>→</kbd></dt><dd>Switch column</dd>
            <dt><kbd>/</kbd></dt><dd>Search</dd>
            <dt><kbd>Esc</kbd></dt><dd>Close / clear selection</dd></dl></section>
      <section><h4>Selected item</h4>
        <dl><dt><kbd>Enter</kbd></dt><dd>Expand / collapse details</dd>
            <dt><kbd>O</kbd></dt><dd>Open in Jira / GitLab / Bitbucket</dd>
            <dt><kbd>S</kbd></dt><dd>Add to focus (star)</dd>
            <dt><kbd>M</kbd></dt><dd>Mark as seen</dd>
            <dt><kbd>X</kbd></dt><dd>Hide until something changes</dd>
            <dt><kbd>T</kbd></dt><dd>Copy to my tasks</dd></dl></section>
      <section><h4>View</h4>
        <dl><dt><kbd>D</kbd> <kbd>I</kbd> <kbd>P</kbd> <kbd>L</kbd></dt><dd>Dashboard · Issues · Pull Requests · List</dd>
            <dt><kbd>F</kbd></dt><dd>Show / hide focus &amp; tasks</dd>
            <dt><kbd>N</kbd></dt><dd>New task</dd>
            <dt><kbd>R</kbd></dt><dd>Refresh</dd>
            <dt><kbd>?</kbd></dt><dd>This overview</dd></dl></section>
    </div>
    <section class="setup" id="setup" aria-labelledby="setupTitle"></section>
    <div class="about"><span>On My Plate <span id="aboutVersion"></span> · open source under the MIT License</span>
      <span id="dataInfo"></span>
      <button class="clear" data-act="licenses" aria-expanded="false" aria-controls="licenses">About &amp; licenses</button></div>
    <div class="licenses" id="licenses" hidden></div>
  </div>
</div>

<script>
(() => {
"use strict";
const REL = { assignee: "Assigned", changes: "Needs work", reporter: "Created", reviewer: "Review", commented: "Commented",
              mentioned: "Mentioned", mentioned_done: "Mentioned (answered)", watcher: "Watching", participant: "Involved",
              team: "Team repo" };
const REL_ORDER = Object.keys(REL);
const STATE = { in_progress: "In progress", todo: "Open", done: "Closed" };
const STATE_ORDER = ["in_progress", "todo", "done"];
const SRC = { jira: "Jira", gitlab: "GitLab", bitbucket: "Bitbucket" };
const PRIO = { blocker: 0, highest: 0, critical: 1, high: 1, major: 2, medium: 2, normal: 2,
               minor: 3, low: 3, lowest: 4, trivial: 4 };
const COLS = [
  { id: "todo", title: "To do", hint: "Assigned to you, review requested, changes needed or mentioned" },
  { id: "waiting", title: "Waiting on others", hint: "Created by you, now with someone else" },
  { id: "watch", title: "Watchlist", hint: "Watching, commented, involved or from team repos" },
];
const COL_NAME = Object.fromEntries(COLS.map(c => [c.id, c.title]));
const DASH_COLS = [
  { id: "todo-issues", sec: "todo", title: "Issues · to do", hint: "Assigned to you or mentioned", empty: "No open issues for you." },
  { id: "todo-prs", sec: "todo", title: "PRs · to do", hint: "Review requested, changes needed or mentioned", empty: "No PRs waiting for you." },
  { id: "waiting", sec: "waiting", title: "Waiting on others", hint: "Your issues and PRs that are with someone else", empty: "Empty." },
  { id: "watch", sec: "watch", title: "Watchlist", hint: "Watching, commented, involved or from team repos", empty: "Empty." },
];
const ISSUE_COLS = COLS.map(c => ({ ...c, sec: c.id, empty: c.id === "todo" ? "Nothing open. Well done." : "Empty." }));
function boardCol(it, view = ui.view) {
  const b = bucketOf(it);
  if (view === "dash") return b === "todo" ? (isPR(it) ? "todo-prs" : "todo-issues") : b;
  return b;
}
const PR_COLS = [
  { id: "review", title: "Your reviews", hint: "You are a reviewer and have not finished yet" },
  { id: "mine", title: "Your PRs", hint: "Created by or assigned to you" },
  { id: "involved", title: "Mentioned & involved", hint: "Mentioned, commented, already reviewed or watching" },
  { id: "team", title: "Team repos", hint: "Other open PRs in your team repos" },
];
const isPR = (it) => it.type === "PR" || it.type === "MR";
function prCol(it) {
  const r = it.relations;
  if (r.includes("reviewer")) return "review";
  if (r.includes("reporter") || r.includes("changes") || r.includes("assignee")) return "mine";
  if (r.some(x => x !== "team")) return "involved";
  return "team";
}
const STALE_DAYS = 14;

let data = { items: [], errors: [], warnings: [] };
let local = {};
const LOCAL_DEF = () => ({ pins: [], notes: {}, seen: {}, seenStatus: {}, hidden: {}, bucket: {}, seenInit: false });

// Personal tasks are stored server-side in onmyplate.todos.md.
let todos = [], dataDir = "", todosDirty = false, editing = null, todoT;
const todayISO = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; };
const newId = () => [...crypto.getRandomValues(new Uint8Array(4))].map(b => b.toString(16).padStart(2, "0")).join("");
const todoDue = (t) => t.due ? dueInfo({ due: t.due, state: t.done ? "done" : "todo" }) : null;
function saveTodos() {
  todosDirty = true; clearTimeout(todoT);
  todoT = setTimeout(async () => {
    const r = await api("/api/todos", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ todos }) }, null);
    todosDirty = false;
    if (!r || !r.ok) { data.errors = [...(data.errors || []), "Your tasks could not be saved."]; renderMeta(); }
  }, 300);
}
async function loadTodos() {
  if (todosDirty || editing) return;   // keep unsaved local edits
  const r = await api("/api/todos", {}, null);
  if (r && Array.isArray(r.todos)) {
    todos = r.todos.filter(t => t && typeof t.id === "string" && typeof t.text === "string");
    dataDir = typeof r.data_dir === "string" ? r.data_dir : "";
  }
}
const DEF_UI = { view: "dash", q: "", source: "all", rels: [], states: ["todo", "in_progress"], group: "state",
                 sort: "updated", collapsed: {}, open: null, showDone: false, showHidden: {}, side: true };
const ui = Object.assign({}, DEF_UI);
try { Object.assign(ui, JSON.parse(localStorage.getItem("onmyplate.ui") || "{}")); } catch { /* storage unavailable: use defaults */ }
ui.q = ""; ui.showHidden = {};

const $ = (s) => document.querySelector(s);
const TOUCH = matchMedia("(pointer: coarse)").matches;
if (TOUCH) $("#q").placeholder = "Search";
// Screen readers hear status changes through a polite live region.
const announce = (msg) => { $("#live").textContent = ""; setTimeout(() => { $("#live").textContent = msg; }, 50); };
let toastT;
// Short confirmation with an undo for actions that remove something.
function toast(msg, undo) {
  const el = $("#toast");
  el.innerHTML = `<span>${esc(msg)}</span>${undo ? `<button type="button">Undo</button>` : ""}`;
  el.hidden = false; announce(msg + (undo ? ". Undo is available." : ""));
  if (undo) el.querySelector("button").onclick = () => { el.hidden = true; undo(); announce("Undone"); };
  const close = () => { toastT = setTimeout(() => { el.hidden = true; }, 10000); };
  clearTimeout(toastT); close();
  el.onmouseenter = el.onfocusin = () => clearTimeout(toastT);   // stays while you reach for Undo
  el.onmouseleave = el.onfocusout = close;
}
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function safeUrl(u) {
  if (!/^https?:\/\//i.test(u || "")) return "#";
  const bases = Object.values(data.links || {}).filter(b => typeof b === "string" && b);
  return !bases.length || bases.some(b => u === b || u.startsWith(b + "/")) ? u : "#";
}
const ts = (iso) => { const t = Date.parse(iso || ""); return isNaN(t) ? 0 : t; };
function saveUi() { try { localStorage.setItem("onmyplate.ui", JSON.stringify(ui)); } catch { /* view settings are optional */ } }

let saveT;
function saveLocal() {
  clearTimeout(saveT);
  saveT = setTimeout(() => api("/api/local", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(local) }, null), 350);
}

function ago(iso) {
  const t = ts(iso); if (!t) return "";
  const s = (Date.now() - t) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  if (s < 86400 * 7) { const n = Math.floor(s / 86400); return `${n} ${n === 1 ? "day" : "days"} ago`; }
  return new Date(t).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}
function dueInfo(it) {
  if (!it.due) return null;
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const d = new Date(it.due + "T00:00:00"), days = Math.round((d - today) / 86400000);
  const label = d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
  if (it.state === "done") return { cls: "", text: `due ${label}`, urgent: false };
  if (days < 0) return { cls: "due-late", text: `overdue since ${label}`, urgent: true };
  if (days <= 3) return { cls: "due-soon", text: days === 0 ? "due today" : `due ${label}`, urgent: true };
  return { cls: "", text: `due ${label}`, urgent: false };
}
const prioRank = (p) => PRIO[(p || "").toLowerCase()] ?? (/^\d$/.test(p || "") ? +p : 5);

// --- Classification -------------------------------------------------------
function autoBucket(it) {
  const r = it.relations;
  if (r.includes("assignee") || r.includes("reviewer") || r.includes("mentioned") || r.includes("changes")) return "todo";
  if (r.includes("reporter")) return "waiting";
  return "watch";
}
const bucketOf = (it) => COL_NAME[local.bucket[it.id]] ? local.bucket[it.id] : autoBucket(it);
function change(it) {
  const seen = local.seen[it.id];
  if (seen === undefined) return local.seenInit ? "new" : null;
  return ts(it.updated) > ts(seen) ? "chg" : null;
}
const isHidden = (it) => local.hidden[it.id] !== undefined && ts(it.updated) <= ts(local.hidden[it.id]);
function markSeen(ids) {
  const byId = new Map(data.items.map(i => [i.id, i]));
  for (const id of ids) {
    const it = byId.get(id);
    if (it) { local.seen[id] = it.updated || new Date().toISOString(); local.seenStatus[id] = it.status || ""; }
  }
  saveLocal();
}
function hideItem(it) {
  const before = { hidden: local.hidden[it.id], seen: local.seen[it.id], seenStatus: local.seenStatus[it.id] };
  local.hidden[it.id] = it.updated || new Date().toISOString(); local.seen[it.id] = local.hidden[it.id]; local.seenStatus[it.id] = it.status || "";
  if (ui.open === it.id) ui.open = null;
  saveLocal();
  toast(`Hidden until something changes: ${it.key}`, () => {
    for (const k of ["hidden", "seen", "seenStatus"]) { if (before[k] === undefined) delete local[k][it.id]; else local[k][it.id] = before[k]; }
    saveLocal(); render();
  });
}
function changeText(it) {
  const ch = change(it);
  if (ch === "new") return "New for you";
  if (!ch) return "";
  const prev = local.seenStatus[it.id];
  return prev && prev !== it.status ? `Status: ${prev} → ${it.status}` : `updated ${ago(it.updated)}`;
}

// --- Filters ----------------------------------------------------------------
function matches(it) {
  if (ui.source !== "all" && it.source !== ui.source) return false;
  const q = ui.q.trim().toLowerCase();
  if (q) {
    const hay = [it.key, it.title, it.project, it.assignee, it.author, it.status, it.type, (it.labels || []).join(" ")].join(" ").toLowerCase();
    if (!q.split(/\s+/).every(t => hay.includes(t))) return false;
  }
  return true;
}
const base = () => data.items.filter(it => matches(it) && (!ui.states.length || ui.states.includes(it.state)));
const filtered = (list) => ui.rels.length ? list.filter(it => it.relations.some(r => ui.rels.includes(r))) : list;
function sorted(list, mode) {
  const by = {
    updated: (a, b) => ts(b.updated) - ts(a.updated),
    due: (a, b) => (a.due || "9999").localeCompare(b.due || "9999") || by.updated(a, b),
    priority: (a, b) => prioRank(a.priority) - prioRank(b.priority) || by.updated(a, b),
    key: (a, b) => a.key.localeCompare(b.key, "en", { numeric: true }),
  };
  return list.slice().sort(by[mode || ui.sort] || by.updated);
}

// --- Building blocks ------------------------------------------------------
const ICON = {
  pin: (on) => `<svg width="14" height="14" viewBox="0 0 24 24" fill="${on ? "currentColor" : "none"}" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2L12 17.3l-5.6 2.9 1.1-6.2L3 9.6l6.2-.9z"/></svg>`,
  hide: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M3 3l18 18"/><path d="M10.6 5.1A10 10 0 0 1 12 5c6 0 9.5 7 9.5 7a17 17 0 0 1-2.7 3.6M6.6 6.6C3.9 8.4 2.5 12 2.5 12S6 19 12 19c1.6 0 3-.4 4.3-1"/></svg>`,
  show: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M2.5 12S6 5 12 5s9.5 7 9.5 7-3.5 7-9.5 7-9.5-7-9.5-7z"/><circle cx="12" cy="12" r="3"/></svg>`,
  ext: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg>`,
};
const relTags = (it) => REL_ORDER.filter(r => it.relations.includes(r)).map(r => `<span class="rel ${r}">${REL[r]}</span>`).join("");
const openUrl = (it) => safeUrl(it.url);
// Links to anything but the configured instances are refused; show them as unavailable instead of a dead link.
const noLink = () => data.demo ? "Links are disabled in the demo" : "No link: the address is not on a configured instance";
const extLink = (it, act = "seen-link") => openUrl(it) === "#"
  ? `<span class="ib ext off" role="img" aria-label="${noLink()}" title="${noLink()}">${ICON.ext}</span>`
  : `<a class="ib ext" href="${esc(openUrl(it))}" target="_blank" rel="noopener noreferrer" data-act="${act}" data-ref="${esc(it.id)}" title="Open in ${SRC[it.source]} (O)" aria-label="Open ${esc(it.key)} in ${SRC[it.source]}">${ICON.ext}</a>`;
function openIssue(id) {
  const it = data.items.find(i => i.id === id); if (!it || openUrl(it) === "#") return;
  window.open(openUrl(it), "_blank", "noopener,noreferrer"); openSeen([id]); setTimeout(render, 0);
}
const keyLink = (it) => safeUrl(it.url) === "#" ? `<span class="key" title="${noLink()}">${esc(it.key)}</span>`
  : `<a class="key" href="${esc(safeUrl(it.url))}" target="_blank" rel="noopener noreferrer" data-act="seen-link">${esc(it.key)}</a>`;
const statusPill = (it) => `<span class="status ${it.state}">${esc(it.status || STATE[it.state])}</span>`;

function detailHtml(it) {
  const cur = bucketOf(it), manual = !!local.bucket[it.id];
  const moves = COLS.filter(c => c.id !== cur).map(c => `<button class="btn sm" data-act="move" data-to="${c.id}">→ ${c.title}</button>`).join("")
    + (manual ? `<button class="btn sm" data-act="move" data-to="">Sort automatically</button>` : "");
  return `<div class="detail">
    <div class="facts"><span>Created by <b>${esc(it.author || "—")}</b></span><span>${isPR(it) && it.source === "bitbucket" ? "Reviewers" : "Assignee"} <b>${esc(it.assignee || "—")}</b></span>
      <span>created ${ago(it.created)}</span>${it.priority ? `<span>Priority <b>${esc(it.priority)}</b></span>` : ""}<span>${esc(it.type)}</span>
      ${it.branch ? `<span>Branch <b>${esc(it.branch)}</b></span>` : ""}<span>Project <b>${esc(it.project)}</b></span></div>
    ${(it.labels || []).length ? `<div class="tags">${it.labels.map(l => `<span class="tag">${esc(l)}</span>`).join("")}</div>` : ""}
    <div class="tags">${relTags(it)}</div>
    <textarea data-act="note" placeholder="Private note (stays on this machine) …">${esc(local.notes[it.id] || "")}</textarea>
    <div class="actions">${safeUrl(it.url) === "#" ? `<span class="btn sm off" aria-disabled="true" title="${noLink()}">Open in ${SRC[it.source]} ↗</span>`
      : `<a class="btn sm on" href="${esc(safeUrl(it.url))}" target="_blank" rel="noopener noreferrer" data-act="seen-link">Open in ${SRC[it.source]} ↗</a>`}
      <button class="btn sm" data-act="mktodo">Add to my tasks</button>${moves}</div>
  </div>`;
}

// --- Dashboard ------------------------------------------------------------
function dRowHtml(it, hiddenNow, extra = {}) {
  const ch = change(it), due = dueInfo(it), pinned = local.pins.includes(it.id), open = ui.open === it.id;
  const hide = new Set(extra.hide || []);
  const stale = bucketOf(it) === "waiting" && Date.now() - ts(it.updated) > STALE_DAYS * 86400000;
  const quiet = bucketOf(it) === "watch" && !ch;
  const meta = [
    extra.reason ? `<span class="why ${extra.tone || ""}">${esc(extra.reason)}</span>` : "",
    extra.change ? `<span class="badge ${ch === "new" ? "new" : "chg"} wide">${esc(extra.change)}</span>`
      : ch === "new" ? `<span class="badge new">new</span>` : ch === "chg" ? `<span class="badge chg">changed</span>` : "",
    it.relations.includes("mentioned") ? `<span class="badge at">@ mentioned</span>` : "",
    isPR(it) && ui.view === "dash" ? `<span class="badge man">${esc(it.type)}</span>` : "",
    local.bucket[it.id] ? `<span class="badge man">manual</span>` : "",
    due && !hide.has("due") ? `<span class="${due.cls}">${due.text}</span>` : "",
    it.assignee && bucketOf(it) !== "todo" && !hide.has("who") ? `<span>→ ${esc(it.assignee)}</span>` : "",
    hide.has("age") ? "" : `<span${stale ? ` class="due-soon" title="No activity for more than ${STALE_DAYS} days"` : ""}>${ago(it.updated)}</span>`,
    local.notes[it.id] ? `<span title="Has a note">📝</span>` : "",
    it.branch ? `<span class="br" title="${esc(it.branch)}">⎇ ${esc(it.branch)}</span>` : "",
  ].join("");
  return `<div class="drow${open ? " open" : ""}${quiet ? " quiet" : ""}${hiddenNow ? " dim" : ""}" data-id="${esc(it.id)}" tabindex="-1" aria-label="${esc(it.key + ": " + it.title)}">
    <div class="l1"><span class="dot ${it.source}" title="${SRC[it.source]}"></span>${keyLink(it)}<span class="spacer"></span>${statusPill(it)}</div>
    <div class="ttl" data-act="toggle" role="button" aria-expanded="${open}" title="${esc(it.title)}\nClick: details · Ctrl/⌘-click or middle click: open in ${SRC[it.source]}">${esc(it.title)}</div>
    <div class="l3">${meta}</div>
    <div class="hov">
      ${extLink(it)}
      <button class="ib${pinned ? " on" : ""}" data-act="pin" title="${pinned ? "Remove from focus (S)" : "Add to focus (S)"}" aria-label="${pinned ? "Remove from focus" : "Add to focus"}" aria-pressed="${pinned}">${ICON.pin(pinned)}</button>
      ${ch ? `<button class="ib word" data-act="seen" title="Clear the new/changed marker (M)">Seen</button>` : ""}
      ${hiddenNow ? `<button class="ib" data-act="unhide" title="Show again (X)" aria-label="Show again">${ICON.show}</button>`
                  : `<button class="ib" data-act="hide" title="Hide until something changes (X)" aria-label="Hide until something changes">${ICON.hide}</button>`}
    </div>
    ${open ? detailHtml(it) : ""}
  </div>`;
}

function colSections(id, list) {
  const byUpd = sorted(list, "updated");
  if (id === "todo") {
    const urgent = byUpd.filter(it => dueInfo(it)?.urgent).sort((a, b) => a.due.localeCompare(b.due));
    const rest = byUpd.filter(it => !urgent.includes(it));
    const reason = (it) => local.bucket[it.id] ? "Added manually"
      : it.relations.includes("changes") ? "Changes requested"
      : it.relations.includes("reviewer") ? "Review requested"
      : it.relations.includes("assignee") ? "Assigned to me" : "Mentioned";
    const order = ["Changes requested", "Review requested", "Assigned to me", "Mentioned", "Added manually"];
    const secs = urgent.length ? [["Due / overdue", urgent, true]] : [];
    for (const name of order) {
      const part = rest.filter(it => reason(it) === name)
        .sort((a, b) => (a.state === "in_progress" ? 0 : 1) - (b.state === "in_progress" ? 0 : 1) || ts(b.updated) - ts(a.updated));
      if (part.length) secs.push([name, part]);
    }
    return secs;
  }
  if (id === "waiting") {
    const cut = Date.now() - STALE_DAYS * 86400000;
    return [["Active", byUpd.filter(it => ts(it.updated) >= cut)],
            [`Quiet for ${STALE_DAYS}+ days`, byUpd.filter(it => ts(it.updated) < cut)]].filter(s => s[1].length);
// Opening an item counts as seeing it, but it stays where it was until the next refresh,
// so it does not vanish from "Since your last visit" or jump to "Unchanged" under the cursor.
const openedSeen = new Set();
function openSeen(ids) { ids.forEach(id => openedSeen.add(id)); markSeen(ids); }
function dismissSeen(ids) { ids.forEach(id => openedSeen.delete(id)); markSeen(ids); }
const listedAsChanged = (it) => !!change(it) || openedSeen.has(it.id);
  }
  return [["New / changed", byUpd.filter(listedAsChanged)],
          ["Unchanged", byUpd.filter(it => !listedAsChanged(it))]].filter(s => s[1].length);
}

function renderPRs() {
  const open = data.items.filter(it => isPR(it) && it.state !== "done" && matches(it));
  const cols = {}, hid = {};
  PR_COLS.forEach(c => { cols[c.id] = []; hid[c.id] = []; });
  for (const it of open) (isHidden(it) ? hid : cols)[prCol(it)].push(it);
  const changes = cols.mine.filter(it => it.relations.includes("changes")).length;
  const mentioned = open.filter(it => it.relations.includes("mentioned") && !isHidden(it)).length;
  const teamNew = cols.team.filter(it => change(it)).length;
  const kpi = (n, l, color, extra, alert) => `<div class="stat static${alert ? " alert" : ""}"><span class="bar" style="background:${color}"></span>
      <div class="n">${n}</div><div class="l">${l}</div>${extra ? `<span class="kx">${extra}</span>` : ""}</div>`;
  $("#stats").innerHTML =
    kpi(cols.review.length, "Open reviews", "var(--c-todo)") +
    kpi(cols.mine.length, "Your open PRs", "var(--c-wait)", changes ? `${changes} need work` : "", changes > 0) +
    kpi(mentioned, "Open mentions", "var(--c-watch)") +
    kpi(cols.team.length, "Team PRs", "var(--c-team)", teamNew ? `${teamNew} new/changed` : "");
  if (!data.items.length) {
    $("#list").innerHTML = `<div class="card empty">${data.fetched_at ? "No items found." : "Loading data …"}</div>`;
    return;
  }
  const byUpd = (l) => sorted(l, "updated");
  const sections = {
    review: (l) => [["New / changed", byUpd(l.filter(listedAsChanged))], ["Open", byUpd(l.filter(it => !listedAsChanged(it)))]],
    mine: (l) => [["Changes requested", byUpd(l.filter(it => it.relations.includes("changes")))],
                  ["In review", byUpd(l.filter(it => !it.relations.includes("changes") && it.state !== "todo"))],
                  ["Draft", byUpd(l.filter(it => !it.relations.includes("changes") && it.state === "todo"))]],
    involved: (l) => [["Mentioned", byUpd(l.filter(it => it.relations.includes("mentioned")))],
                      ["New / changed", byUpd(l.filter(it => !it.relations.includes("mentioned") && listedAsChanged(it)))],
                      ["Unchanged", byUpd(l.filter(it => !it.relations.includes("mentioned") && !listedAsChanged(it)))]],
    team: (l) => [["New / changed", byUpd(l.filter(listedAsChanged))], ["Unchanged", byUpd(l.filter(it => !listedAsChanged(it)))]],
  };
  const empty = { review: "No open reviews.", mine: "You have no open PRs.", involved: "Nothing here.",
                  team: (data.enabled || []).length ? "No other PRs. Add team repos in the config under watch_projects / watch_repos." : "Empty." };
  $("#list").innerHTML = `<div class="dash four">${PR_COLS.map(c => {
    const key = "pr:" + c.id, list = cols[c.id], hidden = hid[c.id], showH = ui.showHidden[key];
    let body = sections[c.id](list).filter(s => s[1].length).map(([name, items]) =>
      `<h3 class="sec${name === "Changes requested" ? " alert" : ""}">${esc(name)} <span class="cnt">${items.length}</span></h3>${items.map(it => dRowHtml(it, false)).join("")}`).join("");
    if (!list.length) body = `<div class="cempty">${empty[c.id]}</div>`;
    if (showH && hidden.length) body += `<h3 class="sec">Hidden <span class="cnt">${hidden.length}</span></h3>` + byUpd(hidden).map(it => dRowHtml(it, true)).join("");
    const changed = list.filter(change).length;
    const foot = [
      changed ? `<button class="clear" data-act="seen-col" data-col="${key}">Mark all ${changed} as seen</button>` : "",
      hidden.length ? `<button class="clear" data-act="toggle-hidden" data-col="${key}">${showH ? "Collapse hidden items" : `Show ${hidden.length} hidden`}</button>` : "",
    ].filter(Boolean).join("");
    return `<section class="card col" data-col="${key}">
      <div class="chead"><div><h2 class="ttl">${c.title} <span class="cnt">${list.length}</span></h2><div class="hint">${c.hint}</div></div></div>
      ${body}${foot ? `<div class="cfoot">${foot}</div>` : ""}</section>`;
  }).join("")}</div>`;
}

// --- Dashboard overview: next up, changes since last visit, follow-ups ------
const NEXT_GROUPS = [
  { id: "focus", title: "Focus", hint: "Starred by you" },
  { id: "changes", title: "Changes requested on your PRs", tone: "crit" },
  { id: "review", title: "Colleagues waiting for your review", tone: "warn" },
  { id: "due", title: "Due", tone: "crit" },
  { id: "mention", title: "Someone is waiting for your reply" },
  { id: "doing", title: "In progress" },
  { id: "assigned", title: "Assigned to you" },
];
function nextGroup(it) {
  const r = it.relations, due = dueInfo(it);
  if (local.pins.includes(it.id)) return "focus";
  if (r.includes("changes")) return "changes";
  if (r.includes("reviewer")) return "review";
  if (due?.urgent && bucketOf(it) === "todo") return "due";
  if (r.includes("mentioned")) return "mention";
  if (bucketOf(it) !== "todo") return null;
  return it.state === "in_progress" ? "doing" : "assigned";
}
function nextReason(it, group) {
  const days = (iso) => Math.max(0, Math.floor((Date.now() - ts(iso)) / 86400000));
  const due = dueInfo(it);
  switch (group) {
    case "changes": return { reason: "Address review feedback", tone: "crit" };
    case "review": { const d = days(it.created); return { reason: `${it.author || "Author"} waiting${d ? ` for ${d} ${d === 1 ? "day" : "days"}` : " since today"}`, tone: d >= 2 ? "warn" : "" }; }
    case "due": return { reason: due.text, tone: due.cls === "due-late" ? "crit" : "warn", hide: ["due"] };
    case "mention": return {};
    case "focus": return due?.urgent ? { reason: due.text, tone: "warn", hide: ["due"] } : {};
    default: return due ? { reason: due.text, tone: due.urgent ? "warn" : "", hide: ["due"] } : {};
  }
}
function renderOverview() {
  const open = data.items.filter(it => it.state !== "done" && matches(it) && !isHidden(it));
  const groups = Object.fromEntries(NEXT_GROUPS.map(g => [g.id, []]));
  for (const it of open) { const g = nextGroup(it); if (g) groups[g].push(it); }
  groups.review.sort((a, b) => ts(a.created) - ts(b.created));            // longest waiting first
  groups.due.sort((a, b) => (a.due || "").localeCompare(b.due || ""));
  for (const g of ["focus", "changes", "mention", "doing", "assigned"]) groups[g] = sorted(groups[g], "updated");
  const queue = NEXT_GROUPS.flatMap(g => groups[g.id]);

  const changed = sorted(open.filter(listedAsChanged), "updated");
  const unseen = changed.filter(it => change(it)).length;
  const waiting = open.filter(it => bucketOf(it) === "waiting").sort((a, b) => ts(a.updated) - ts(b.updated));
  const stale = waiting.filter(it => Date.now() - ts(it.updated) > STALE_DAYS * 86400000).length;
  const ownUrgent = todos.filter(t => !t.done && todoDue(t)?.urgent).length;

  const kpi = (n, l, color, extra, alert, goto) => `<${goto ? "button" : "div"} class="stat${alert ? " alert" : ""}${goto ? "" : " static"}"${goto ? ` data-goto="${goto}" title="Opens the ${goto === "prs" ? "Pull Requests" : "Issues"} tab"` : ""}><span class="bar" style="background:${color}"></span>
      <div class="n">${n}</div><div class="l">${l}</div>${extra ? `<span class="kx">${extra}</span>` : ""}${goto ? `<span class="go" aria-hidden="true">›</span>` : ""}</${goto ? "button" : "div"}>`;
  const blocking = groups.review.length + groups.changes.length;
  $("#stats").innerHTML =
    kpi(queue.length, "Next up", "var(--c-todo)", groups.doing.length ? `${groups.doing.length} in progress` : "") +
    kpi(blocking, "PRs waiting for you", "var(--c-todo)", groups.changes.length ? `${groups.changes.length} Needs work` : "", groups.changes.length > 0, "prs") +
    kpi(groups.due.length + ownUrgent, "Due / overdue", "var(--danger)", ownUrgent ? `incl. ${ownUrgent} of your tasks` : "", groups.due.length + ownUrgent > 0, "issues") +
    kpi(unseen, "New / changed", "var(--c-watch)") +
    kpi(waiting.length, "With others", "var(--c-wait)", stale ? `${stale} quiet` : "", false, "issues");

  if (!data.items.length) {
    $("#list").innerHTML = `<div class="card empty">${!data.fetched_at ? "Loading data …" : data.errors?.length
      ? `No items loaded yet. <button class="clear" data-act="show-setup">Check sources</button>` : "No items found."}</div>`;
    return;
  }
  const nextBody = NEXT_GROUPS.filter(g => groups[g.id].length).map(g =>
    `<h3 class="sec${g.tone === "crit" ? " alert" : ""}">${esc(g.title)} <span class="cnt">${groups[g.id].length}</span></h3>` +
    groups[g.id].map(it => dRowHtml(it, false, nextReason(it, g.id))).join("")).join("")
    || `<div class="cempty">Nothing is waiting for you. New assignments, reviews and mentions show up here as they arrive.</div>`;
  const changedBody = changed.length
    ? changed.map(it => dRowHtml(it, false, change(it) ? { change: changeText(it), hide: changeText(it).startsWith("updated") ? ["age"] : [] } : { reason: "Seen" })).join("")
    : `<div class="cempty">All caught up. Changes to your issues and PRs show up here.</div>`;
  const waitingBody = waiting.length
    ? waiting.map(it => {
        const d = Math.floor((Date.now() - ts(it.updated)) / 86400000);
        const who = it.assignee ? (isPR(it) ? `Review: ${it.assignee}` : `with ${it.assignee}`) : "unassigned";
        return dRowHtml(it, false, { reason: `${who} · ${d ? `quiet for ${d} ${d === 1 ? "day" : "days"}` : "active today"}`,
                                     tone: d > STALE_DAYS ? "warn" : "", hide: ["who", "age"] });
      }).join("")
    : `<div class="cempty">Nothing of yours is with others right now.</div>`;
  const head = (title, n, hint, action) => `<div class="chead"><div><h2 class="ttl">${title} <span class="cnt">${n}</span></h2><div class="hint">${hint}</div></div>${action || ""}</div>`;
  $("#list").innerHTML = `<div class="overview">
    <section class="card col" data-col="next">${head("Next up", queue.length, `Sorted by urgency, start at the top. ${TOUCH ? "Tap ☆" : "Press S"} to pin something to the very top.`)}${nextBody}</section>
    <div class="ostack">
      <section class="card col" data-col="changed">${head("Since your last visit", changed.length, "New and changed items from all sources",
        unseen ? `<button class="clear" data-act="seen-all">Mark all ${unseen} as seen</button>` : "")}${changedBody}</section>
      <section class="card col" data-col="waiting">${head("Follow up", waiting.length, "Your issues and PRs with others, quietest first")}${waitingBody}</section>
    </div>
  </div>`;
}

function renderDash() {
  const issuesOnly = ui.view === "issues";
  const defs = issuesOnly ? ISSUE_COLS : DASH_COLS;
  const open = data.items.filter(it => it.state !== "done" && matches(it) && (!issuesOnly || !isPR(it)));
  const cols = {}, hid = {};
  defs.forEach(c => { cols[c.id] = []; hid[c.id] = []; });
  for (const it of open) (isHidden(it) ? hid : cols)[boardCol(it)].push(it);
  const todoAll = issuesOnly ? cols.todo : [...cols["todo-issues"], ...cols["todo-prs"]];

  const ownUrgent = todos.filter(t => !t.done && todoDue(t)?.urgent).length;
  const urgent = todoAll.filter(it => dueInfo(it)?.urgent).length + ownUrgent;
  const changedWatch = cols.watch.filter(it => change(it)).length;
  const stale = cols.waiting.filter(it => Date.now() - ts(it.updated) > STALE_DAYS * 86400000).length;
  const kpi = (n, l, color, extra, alert) => `<div class="stat static${alert ? " alert" : ""}"><span class="bar" style="background:${color}"></span>
      <div class="n">${n}</div><div class="l">${l}</div>${extra ? `<span class="kx">${extra}</span>` : ""}</div>`;
  const issueTodo = issuesOnly ? cols.todo : cols["todo-issues"];
  let k = kpi(issueTodo.length, issuesOnly ? "To do" : "Issues to do", "var(--c-todo)", issueTodo.filter(i => i.state === "in_progress").length + " in progress");
  if (!issuesOnly) {
    const prChanges = cols["todo-prs"].filter(i => i.relations.includes("changes")).length;
    k += kpi(cols["todo-prs"].length, "PRs to do", "var(--c-todo)", prChanges ? `${prChanges} need work` : "", prChanges > 0);
  }
  k += kpi(urgent, "Due / overdue", "var(--danger)", ownUrgent ? `incl. ${ownUrgent} of your tasks` : "", urgent > 0) +
       kpi(cols.waiting.length, "Waiting on others", "var(--c-wait)", stale ? `${stale} quiet` : "") +
       kpi(changedWatch, "Watchlist changed", "var(--c-watch)", `of ${cols.watch.length}`);
  $("#stats").innerHTML = k;

  if (!data.items.length) {
    $("#list").innerHTML = `<div class="card empty">${data.fetched_at ? "No items found." : "Loading data …"}</div>`;
    return;
  }
  $("#list").innerHTML = `<div class="dash${issuesOnly ? "" : " four"}">${defs.map(c => {
    const list = cols[c.id], hidden = hid[c.id], showH = ui.showHidden[c.id];
    const secs = colSections(c.sec, list);
    const changed = list.filter(it => change(it)).length;
    let body = secs.map(([name, items, alert]) =>
      `<h3 class="sec${alert ? " alert" : ""}">${esc(name)} <span class="cnt">${items.length}</span></h3>${items.map(it => dRowHtml(it, false)).join("")}`).join("");
    if (!list.length) body = `<div class="cempty">${c.empty}</div>`;
    if (showH && hidden.length) body += `<h3 class="sec">Hidden <span class="cnt">${hidden.length}</span></h3>` + sorted(hidden, "updated").map(it => dRowHtml(it, true)).join("");
    const foot = [
      changed ? `<button class="clear" data-act="seen-col" data-col="${c.id}">Mark all ${changed} as seen</button>` : "",
      hidden.length ? `<button class="clear" data-act="toggle-hidden" data-col="${c.id}">${showH ? "Collapse hidden items" : `Show ${hidden.length} hidden`}</button>` : "",
    ].filter(Boolean).join("");
    return `<section class="card col" data-col="${c.id}">
      <div class="chead"><div><h2 class="ttl">${c.title} <span class="cnt">${list.length}</span></h2><div class="hint">${c.hint}</div></div></div>
      ${body}${foot ? `<div class="cfoot">${foot}</div>` : ""}</section>`;
  }).join("")}</div>`;
}

// --- List view --------------------------------------------------------------
function listGroups(list) {
  if (ui.group === "none") return [["All", list]];
  const m = new Map();
  const push = (k, it) => { if (!m.has(k)) m.set(k, []); m.get(k).push(it); };
  for (const it of list) {
    if (ui.group === "state") push(STATE[it.state], it);
    else if (ui.group === "project") push(it.project || "—", it);
    else if (ui.group === "source") push(SRC[it.source] || it.source, it);
    else if (ui.group === "relation") push(REL[REL_ORDER.find(r => it.relations.includes(r)) || "participant"], it);
  }
  const order = ui.group === "state" ? STATE_ORDER.map(s => STATE[s]) : ui.group === "relation" ? REL_ORDER.map(r => REL[r]) : null;
  return [...m.keys()].sort((a, b) => order ? order.indexOf(a) - order.indexOf(b) : a.localeCompare(b, "en")).map(k => [k, m.get(k)]);
}
function rowHtml(it) {
  const pinned = local.pins.includes(it.id), open = ui.open === it.id, due = dueInfo(it), ch = change(it);
  const meta = [esc(it.project), esc(it.type), it.assignee ? `→ ${esc(it.assignee)}` : "unassigned",
                it.priority ? esc(it.priority) : "", `updated ${ago(it.updated)}`, COL_NAME[bucketOf(it)]]
    .filter(Boolean).map(x => `<span>${x}</span>`).join("");
  return `<div class="row${open ? " open" : ""}" data-id="${esc(it.id)}" tabindex="-1" aria-label="${esc(it.key + ": " + it.title)}">
    <button class="pin${pinned ? " on" : ""}" data-act="pin" title="${pinned ? "Remove from focus (S)" : "Add to focus (S)"}" aria-label="${pinned ? "Remove from focus" : "Add to focus"}" aria-pressed="${pinned}">${ICON.pin(pinned)}</button>
    <div class="main">
      <div class="line1"><span class="dot ${it.source}" title="${SRC[it.source]}"></span>${keyLink(it)}
        <span class="title" data-act="toggle" role="button" aria-expanded="${open}" title="${esc(it.title)}">${esc(it.title)}</span>
        ${ch === "new" ? `<span class="badge new">new</span>` : ch === "chg" ? `<span class="badge chg">changed</span>` : ""}</div>
      <div class="line2">${meta}${due ? `<span class="${due.cls}">${due.text}</span>` : ""}${local.notes[it.id] ? "<span>📝</span>" : ""}</div>
    </div>
    <div class="right"><div class="rtop">${extLink(it)}${statusPill(it)}</div><div class="rels">${relTags(it)}</div></div>
    ${open ? detailHtml(it) : ""}
  </div>`;
}
function renderList() {
  const b = base(), list = sorted(filtered(b));
  $("#stats").innerHTML = REL_ORDER.map(r => {
    const n = b.filter(it => it.relations.includes(r)).length;
    return `<button class="stat${ui.rels.includes(r) ? " on" : ""}" data-rel="${r}" aria-pressed="${ui.rels.includes(r)}" title="Show only: ${REL[r]}"><div class="n">${n}</div><div class="l">${REL[r]}</div></button>`;
  }).join("");
  if (!data.items.length) {
    $("#list").innerHTML = `<div class="card empty">${data.fetched_at ? "No items found." : "Loading data …"}</div>`;
  } else if (!list.length) {
    $("#list").innerHTML = `<div class="card empty">Nothing matches the filters. <button class="clear" data-act="reset">Reset filters</button></div>`;
  } else {
    $("#list").innerHTML = `<div class="card">${listGroups(list).map(([name, items]) => {
      const closed = ui.collapsed[ui.group + ":" + name];
      return `<section class="group${closed ? " closed" : ""}">
        <div class="ghead" data-group="${esc(name)}"><svg class="chev" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="m6 9 6 6 6-6"/></svg>
          ${esc(name)} <span class="cnt">${items.length}</span></div>
        <div class="rows">${items.map(rowHtml).join("")}</div></section>`;
    }).join("")}</div>`;
  }
}

// --- Rendering --------------------------------------------------------------
function render() {
  document.body.className = "view-" + ui.view;
  document.querySelector(".layout").classList.toggle("noside", !ui.side);
  $("#sideBtn").setAttribute("aria-pressed", String(!!ui.side));
  const rowHadFocus = !!document.activeElement?.matches?.("#list .drow, #list .row");
  document.querySelectorAll("#view button").forEach(x => { x.classList.toggle("on", x.dataset.v === ui.view); x.setAttribute("aria-pressed", String(x.dataset.v === ui.view)); });
  const enabled = new Set([...(data.enabled || []), ...data.items.map(i => i.source)]);
  document.querySelectorAll("#source button").forEach(x => {
    x.hidden = x.dataset.v !== "all" && enabled.size > 0 && !enabled.has(x.dataset.v);
    x.classList.toggle("on", x.dataset.v === ui.source); x.setAttribute("aria-pressed", String(x.dataset.v === ui.source));
  });
  document.querySelectorAll("#states button").forEach(x => { x.classList.toggle("on", ui.states.includes(x.dataset.v)); x.setAttribute("aria-pressed", String(ui.states.includes(x.dataset.v))); });
  $("#group").value = ui.group; $("#sort").value = ui.sort;
  ui.view === "dash" ? renderOverview() : ui.view === "issues" ? renderDash() : ui.view === "prs" ? renderPRs() : renderList();
  renderSide(); renderMeta();
  setCur(cur, false, rowHadFocus);
}

// --- Keyboard selection -------------------------------------------------------
let cur = null;
const rowEls = () => [...document.querySelectorAll("#list .drow, #list .row")];
// The selected row is a real focus target (roving tabindex), so screen readers follow J/K.
function setCur(id, scroll = true, focus = scroll) {
  document.querySelectorAll("#list .cur").forEach(x => { x.classList.remove("cur"); x.tabIndex = -1; });
  const el = id && document.querySelector(`#list [data-id="${CSS.escape(id)}"]`);
  cur = el ? id : null;
  if (el) {
    el.classList.add("cur"); el.tabIndex = 0;
    if (focus) el.focus({ preventScroll: true });
    if (scroll) el.scrollIntoView({ block: "nearest" });
  } else { const first = rowEls()[0]; if (first) first.tabIndex = 0; }
}
function moveCur(step) {
  const rows = rowEls(); if (!rows.length) return;
  const i = rows.findIndex(r => r.dataset.id === cur);
  const next = i < 0 ? (step > 0 ? 0 : rows.length - 1) : Math.min(rows.length - 1, Math.max(0, i + step));
  const id = rows[next].dataset.id;
  if (ui.open && ui.open !== id) { ui.open = null; cur = id; render(); }
  setCur(id);
}
function moveCol(step) {
  const cols = [...document.querySelectorAll("#list .col")]; if (!cols.length) return moveCur(step);
  const curEl = cur && document.querySelector(`#list [data-id="${CSS.escape(cur)}"]`);
  let ci = curEl ? cols.indexOf(curEl.closest(".col")) : -1;
  for (let k = 0; k < cols.length; k++) {
    ci = (ci + step + cols.length) % cols.length;
    const first = cols[ci].querySelector(".drow");
    if (first) { if (ui.open) { ui.open = null; cur = first.dataset.id; render(); } setCur(first.dataset.id); return; }
  }
}

function renderSide() {
  const open = todos.map((t, i) => [t, i]).filter(([t]) => !t.done)
    .sort((a, b) => (a[0].due || "9999").localeCompare(b[0].due || "9999") || a[1] - b[1]).map(x => x[0]);
  const done = todos.filter(t => t.done);
  const show = ui.showDone ? [...open, ...done] : open;
  const byId = Object.fromEntries(data.items.map(i => [i.id, i]));
  $("#todoCount").textContent = open.length ? open.length + " open" : "";
  $("#todos").innerHTML = show.map(t => {
    if (editing === t.id) {
      return `<li data-tid="${esc(t.id)}"><form class="tedit" data-tid="${esc(t.id)}">
        <input class="tin" name="text" value="${esc(t.text)}" maxlength="1000" autocomplete="off" aria-label="Task">
        <input type="date" name="due" value="${esc(t.due || "")}" title="Due date" aria-label="Due date">
        <button class="btn sm" type="submit">OK</button></form></li>`;
    }
    const ref = t.ref && byId[t.ref], due = todoDue(t);
    const refHtml = ref ? (safeUrl(ref.url) === "#" ? ` <span class="key">${esc(ref.key)}</span>`
        : ` <a href="${esc(safeUrl(ref.url))}" target="_blank" rel="noopener noreferrer" data-act="noop">${esc(ref.key)}</a>`)
      : t.ref && t.ref.startsWith("jira:") && /^https:\/\//.test(data.links?.jira || "")
        ? ` <a href="${esc(data.links.jira + "/browse/" + encodeURIComponent(t.ref.slice(5)))}" target="_blank" rel="noopener noreferrer" data-act="noop">${esc(t.ref.slice(5))}</a>`
      : t.ref ? ` <span class="key">${esc(t.ref.split(":").pop())}</span>` : "";
    return `<li class="${t.done ? "done" : ""}" data-tid="${esc(t.id)}">
      <input type="checkbox" data-act="tdone" ${t.done ? "checked" : ""} aria-label="Done: ${esc(t.text)}">
      <span class="txt" data-act="tedit" title="Click to edit" role="button" tabindex="0">${esc(t.text)}${due ? `<span class="tdue ${due.cls}">${due.text}</span>` : ""}${refHtml}</span>
      <button class="x" data-act="tdel" title="Delete" aria-label="Delete task: ${esc(t.text)}">✕</button></li>`;
  }).join("") || `<li class="sub" style="padding:2px 14px 6px">No tasks yet. Add anything that does not live in a ticket.</li>`;
  $("#todoFoot").innerHTML = done.length
    ? `<button class="clear" data-act="tshow">${ui.showDone ? "Hide completed" : `Show ${done.length} completed`}</button>
       ${ui.showDone ? ` · <button class="clear" data-act="tclean">Delete completed</button>` : ""}` : "";
  const pins = local.pins.map(id => byId[id]).filter(Boolean);
  $("#pinCount").textContent = pins.length || "";
  $("#pins").innerHTML = pins.length ? pins.map(it => `<div class="pinrow" data-jump="${esc(it.id)}" title="${esc(it.title)}">
      <span class="dot ${it.source}"></span><span class="key">${esc(it.key)}${change(it) ? ` <span class="badge chg">changed</span>` : ""}</span>
      ${statusPill(it)}${extLink(it, "seen-ext")}<span class="t">${esc(it.title)}</span></div>`).join("")
    : `<div class="sub">Items you star (☆ or S) land here: your priorities for today.</div>`;
}

let refreshing = false;
function renderMeta() {
  $("#demo").hidden = !data.demo;
  $("#refresh").classList.toggle("spin", !!data.refreshing || refreshing);
  $("#meta").textContent = data.fetched_at === "locked" ? "locked" : data.fetched_at ? `${data.items.length} items · updated ${ago(data.fetched_at)}` : "Loading …";
  let h = "";
  if (data.errors?.length) h += `<div class="banner err" role="alert"><b>Could not load everything</b>${data.setup?.config ? `<button class="clear" data-act="show-setup">Check sources</button>` : ""}<ul>${data.errors.map(e => `<li>${esc(e)}</li>`).join("")}</ul></div>`;
  if (data.warnings?.length) h += `<details class="banner warn"><summary>${data.warnings.length} ${data.warnings.length === 1 ? "notice" : "notices"}</summary><ul>${data.warnings.map(e => `<li>${esc(e)}</li>`).join("")}</ul></details>`;
  $("#banners").innerHTML = h;
}

// --- Data -------------------------------------------------------------------
// The session token lives in this origin's storage and travels in a header, so
// other sites and other local ports can neither read nor replay it.
const TOKEN_KEY = "onmyplate.token";
let token = "";
try { token = localStorage.getItem(TOKEN_KEY) || ""; } catch { /* storage unavailable: token stays in memory */ }
async function signIn() {
  const code = new URLSearchParams(location.hash.slice(1)).get("code");
  if (!code) return;
  history.replaceState(null, "", location.pathname);
  try {
    const r = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code }) });
    const j = r.ok ? await r.json() : null;
    if (j && typeof j.token === "string") {
      token = j.token;
      try { localStorage.setItem(TOKEN_KEY, token); } catch { /* keep the in-memory token */ }
    }
  } catch { /* the next API call reports the server as unreachable */ }
}
async function api(path, opts, fallback) {
  try {
    const r = await fetch(path, { ...opts, headers: { ...(opts?.headers || {}), "X-OnMyPlate-Token": token } });
    if (r.status === 401) {
      if (path !== "/api/items" && path !== "/api/refresh") return fallback;
      return { items: [], errors: ["Not signed in. Sessions end when On My Plate restarts: open the sign-in link printed in the console (it works once and for 15 minutes; restart On My Plate for a new one)."], warnings: [], fetched_at: "locked" };
    }
    if (!r.ok) throw new Error("HTTP " + r.status);
    const j = await r.json();
    const isItems = path === "/api/items" || path === "/api/refresh";
    return isItems ? Object.assign({ items: [], errors: [], warnings: [] }, j) : j;
  } catch {
    if (path !== "/api/items" && path !== "/api/refresh") return fallback;
    return Object.assign({}, fallback, { errors: ["Local On My Plate server unreachable. Is the script still running?"] });
  }
}
async function downloadBackup() {
  try {
    const r = await fetch("/api/export", { headers: { "X-OnMyPlate-Token": token } });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const name = /filename="([\w.-]+)"/.exec(r.headers.get("Content-Disposition") || "")?.[1] || "onmyplate-backup.zip";
    const url = URL.createObjectURL(await r.blob());
    const a = Object.assign(document.createElement("a"), { href: url, download: name });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  } catch {
    data.errors = [...(data.errors || []), "The backup could not be downloaded."]; renderMeta();
  }
}
function baseline() {
  // On the very first load everything counts as seen, so only later changes are highlighted.
  if (local.seenInit || !data.fetched_at || data.fetched_at === "locked" || !data.items.length) return;
  data.items.forEach((it, i) => {
    local.seen[it.id] = data.demo && i % 4 === 0 ? "2000-01-01T00:00:00Z" : it.updated;
    local.seenStatus[it.id] = data.demo && i % 8 === 0 && it.state !== "todo" ? (it.source === "jira" ? "To Do" : "Open") : (it.status || "");
  });
  local.seenInit = true; saveLocal();
}
async function load() {
  data = await api("/api/items", {}, data);
  await loadTodos();
  baseline();
  if (/INPUT|TEXTAREA/.test(document.activeElement?.tagName) && data.fetched_at) { renderMeta(); }
  else render();
  if ((data.refreshing || !data.fetched_at) && data.fetched_at !== "locked") setTimeout(load, 1500);
}
async function refresh() {
  if (refreshing) return;
  refreshing = true; renderMeta(); openedSeen.clear();
  data = await api("/api/refresh", { method: "POST" }, data);
  refreshing = false; baseline(); render();
  announce(data.errors?.length ? `Refreshed with ${data.errors.length} ${data.errors.length === 1 ? "error" : "errors"}` : `Refreshed: ${data.items.length} items`);
}

// --- Events -----------------------------------------------------------------
$("#q").addEventListener("input", e => { ui.q = e.target.value; render(); });
$("#view").addEventListener("click", e => { const v = e.target.dataset.v; if (v) { ui.view = v; ui.open = null; saveUi(); render(); } });
$("#source").addEventListener("click", e => { const v = e.target.dataset.v; if (v) { ui.source = v; saveUi(); render(); } });
$("#states").addEventListener("click", e => {
  const v = e.target.dataset.v; if (!v) return;
  ui.states = ui.states.includes(v) ? ui.states.filter(x => x !== v) : [...ui.states, v];
  saveUi(); render();
});
$("#group").addEventListener("change", e => { ui.group = e.target.value; saveUi(); render(); });
$("#sort").addEventListener("change", e => { ui.sort = e.target.value; saveUi(); render(); });
$("#refresh").addEventListener("click", refresh);
$("#sideBtn").addEventListener("click", () => { ui.side = !ui.side; saveUi(); render(); });
$("#helpBtn").addEventListener("click", () => toggleHelp(true));
$("#help").addEventListener("click", e => { if (e.target.id === "help") toggleHelp(false); });
// The license notices are generated into onmyplate.py (LICENSES) and served by /api/licenses.
async function toggleLicenses(button) {
  const box = $("#licenses");
  if (box.hidden && !box.childElementCount) {
    const lic = await api("/api/licenses", {}, null);
    if (!lic || !lic.self) { box.innerHTML = `<p>The license notices could not be loaded.</p>`; }
    else {
      const pkg = (p, title) => `<details><summary>${esc(p.name)} ${esc(p.version)} · ${esc(p.license || "see text")} <span class="sub">(${esc(title)})</span></summary>
        ${p.url ? `<p>Source: ${esc(p.url)}</p>` : ""}${(p.texts || []).map(t => `<pre>${esc(t)}</pre>`).join("")}</details>`;
      const packages = (lic.groups || []).flatMap(g => (g.packages || []).map(p => pkg(p, g.title)));
      box.innerHTML = `<h4>On My Plate ${esc(lic.version)} · ${esc(lic.self.license)} License</h4><pre>${esc(lic.self.text)}</pre>
        <h4>Third-party software</h4>${lic.note ? `<p>${esc(lic.note)}</p>` : ""}${packages.join("")}`;
    }
  }
  box.hidden = !box.hidden;
  button.setAttribute("aria-expanded", String(!box.hidden));
  if (!box.hidden) box.scrollIntoView({ behavior: "smooth", block: "start" });
}
let helpReturn = null;
function toggleHelp(show, section) {
  $("#help").hidden = !show;
  if (show) {
    helpReturn = document.activeElement;
    $("#aboutVersion").textContent = data.version || "";
    renderSetup();
    $("#dataInfo").innerHTML = dataDir ? `· Data folder <code>${esc(dataDir)}</code> · <a href="/api/export" data-act="export">Download backup</a> (tasks, notes, settings; no tokens)` : "";
    (section ? $(section) : null)?.scrollIntoView({ block: "start" });
    $("#help .dialog button").focus();
  } else if (helpReturn?.isConnected) helpReturn.focus();
}
// Which sources are set up and whether they answered, so a broken setup is visible in the app.
function renderSetup() {
  const st = data.setup || {};
  const rows = (st.sources || []).map(x => {
    const state = !x.enabled ? `<span class="off">off</span>` : x.error ? `<span class="bad">Error</span>` : x.ok_at ? `<span class="ok">OK</span>` : `<span class="off">not checked yet</span>`;
    const tok = !x.enabled ? "" : { keychain: "system keychain", env: "environment variable", command: "token_command", config: "config" }[x.token]
      || (x.error ? `<span class="bad">missing</span>` : "not checked yet");
    return `<tr><td>${esc(x.label)}</td><td>${state}</td><td>${x.enabled ? esc(x.url || "") : "—"}</td><td>${tok}</td></tr>
      ${x.error ? `<tr><td></td><td colspan="3" class="bad" style="font-weight:400">${esc(x.error)}</td></tr>` : ""}`;
  }).join("");
  $("#setup").innerHTML = `<h4 id="setupTitle">Sources</h4>${data.demo ? "<p>Demo mode: sample data, no sources are contacted.</p>" : ""}
    ${rows ? `<table><thead><tr><th>Source</th><th>Status</th><th>URL</th><th>Token</th></tr></thead><tbody>${rows}</tbody></table>` : ""}
    ${st.config ? `<p>Change URLs or sources in <code>${esc(st.config)}</code>, then press <b>Refresh</b>: the file is read again, no restart needed.</p>
      <p>Tokens are never stored in that file. Store one in the system keychain with <code>onmyplate --set-token jira</code> (or <code>gitlab</code>, <code>bitbucket</code>),
      or provide it with <code>"token_command"</code> or <code>ONMYPLATE_JIRA_TOKEN</code>, <code>ONMYPLATE_GITLAB_TOKEN</code>, <code>ONMYPLATE_BITBUCKET_TOKEN</code>.</p>` : ""}`;
}
$("#help").addEventListener("keydown", e => {   // keep Tab inside the open dialog
  if (e.key !== "Tab") return;
  const f = [...$("#help").querySelectorAll("button, a[href], summary, [tabindex='0']")].filter(x => x.offsetParent);
  if (!f.length) return;
  if (e.shiftKey && document.activeElement === f[0]) { e.preventDefault(); f[f.length - 1].focus(); }
  else if (!e.shiftKey && document.activeElement === f[f.length - 1]) { e.preventDefault(); f[0].focus(); }
});
$("#stats").addEventListener("click", e => {
  const g = e.target.closest("[data-goto]")?.dataset.goto;
  if (g) { ui.view = g; saveUi(); render(); return; }
  const r = e.target.closest("[data-rel]")?.dataset.rel; if (!r) return;
  ui.rels = ui.rels.includes(r) ? ui.rels.filter(x => x !== r) : [...ui.rels, r];
  saveUi(); render();
});
$("#todoForm").addEventListener("submit", e => {
  e.preventDefault();
  const text = $("#todoInput").value.trim(); if (!text) return;
  const t = { id: newId(), text, done: false, created: todayISO() };
  if ($("#todoDue").value) t.due = $("#todoDue").value;
  todos.unshift(t);
  $("#todoInput").value = ""; $("#todoDue").value = ""; saveTodos(); render();
});
document.addEventListener("submit", e => {
  const f = e.target.closest(".tedit"); if (!f) return;
  e.preventDefault();
  const t = todos.find(x => x.id === f.dataset.tid), text = f.elements.text.value.trim();
  if (t && text) { t.text = text; if (f.elements.due.value) t.due = f.elements.due.value; else delete t.due; saveTodos(); }
  editing = null; render();
});
window.addEventListener("hashchange", async () => { if (location.hash.includes("code=")) { await signIn(); load(); } });
window.addEventListener("focus", async () => { if (!token) return; await loadTodos(); renderSide(); });   // pick up edits made directly in the .md file

document.addEventListener("click", e => {
  const el = e.target.closest("[data-act],[data-group],[data-jump]"); if (!el) return;
  if (el.classList.contains("ghead")) {
    const k = ui.group + ":" + el.dataset.group; ui.collapsed[k] = !ui.collapsed[k]; saveUi(); render(); return;
  }
  if (el.dataset.jump) {
    const id = el.dataset.jump; ui.open = id; ui.q = ""; $("#q").value = ""; ui.source = "all";
    if (ui.view === "list") { ui.rels = []; ui.states = []; }
    const it = data.items.find(i => i.id === id);
    if (it && (ui.view === "dash" || ui.view === "issues") && isHidden(it)) ui.showHidden[boardCol(it)] = true;
    if (it && ui.view === "issues" && isPR(it)) ui.view = "dash";
    if (it && ui.view === "prs" && isHidden(it)) ui.showHidden["pr:" + prCol(it)] = true;
    if (it && ui.view === "prs" && !isPR(it)) ui.view = "dash";
    markSeen([id]); render();
    document.querySelector(`#list [data-id="${CSS.escape(id)}"]`)?.scrollIntoView({ behavior: "smooth", block: "center" });
    return;
  }
  const act = el.dataset.act, id = el.closest("[data-id]")?.dataset.id, tid = el.closest("li")?.dataset.tid;
  if (id && el.closest("#list")) cur = id;
  if (act === "help-close") { toggleHelp(false); return; }
  if (act === "licenses") { toggleLicenses(el); return; }
  if (act === "show-setup") { toggleHelp(true, "#setup"); return; }
  const it = id && data.items.find(i => i.id === id);
  switch (act) {
    case "pin": local.pins = local.pins.includes(id) ? local.pins.filter(x => x !== id) : [...local.pins, id]; saveLocal(); render(); break;
    case "toggle":
      if (e.ctrlKey || e.metaKey || e.shiftKey) { openIssue(id); break; }
      ui.open = ui.open === id ? null : id; if (ui.open) openSeen([id]); render(); break;
    case "seen-ext": openSeen([el.dataset.ref]); setTimeout(render, 0); break;
    case "seen-link": openSeen([id]); setTimeout(render, 0); break;
    case "seen": dismissSeen([id]); render(); break;
    case "seen-col": {
      const col = el.dataset.col;
      const inCol = col.startsWith("pr:") ? (i => isPR(i) && prCol(i) === col.slice(3))
        : (i => boardCol(i) === col && (ui.view !== "issues" || !isPR(i)));
      dismissSeen(data.items.filter(i => i.state !== "done" && matches(i) && !isHidden(i) && inCol(i) && listedAsChanged(i)).map(i => i.id));
      render(); break;
    }
    case "hide": hideItem(it); render(); break;
    case "seen-all": dismissSeen(data.items.filter(i => i.state !== "done" && matches(i) && !isHidden(i) && listedAsChanged(i)).map(i => i.id)); render(); break;
    case "unhide": delete local.hidden[id]; saveLocal(); render(); break;
    case "toggle-hidden": ui.showHidden[el.dataset.col] = !ui.showHidden[el.dataset.col]; render(); break;
    case "move": if (el.dataset.to) local.bucket[id] = el.dataset.to; else delete local.bucket[id]; saveLocal(); render(); break;
    case "mktodo": todos.unshift({ id: newId(), text: it.title, ref: id, done: false, created: todayISO() }); saveTodos(); renderSide(); break;
    case "tdone": {
      const t = todos.find(x => x.id === tid); if (!t) break;
      t.done = el.checked; if (t.done) t.done_at = todayISO(); else delete t.done_at;
      saveTodos(); render(); break;
    }
    case "tedit": editing = tid; renderSide(); document.querySelector(".tedit .tin")?.focus(); break;
    case "tdel": {
      const before = todos.slice(), t = todos.find(x => x.id === tid);
      todos = todos.filter(x => x.id !== tid); saveTodos(); render();
      toast(`Task deleted: ${t ? t.text.slice(0, 60) : ""}`, () => { todos = before; saveTodos(); render(); });
      break;
    }
    case "tshow": ui.showDone = !ui.showDone; saveUi(); renderSide(); break;
    case "tclean": {
      const before = todos.slice(), n = todos.filter(t => t.done).length;
      todos = todos.filter(t => !t.done); saveTodos(); renderSide();
      toast(`${n} completed ${n === 1 ? "task" : "tasks"} deleted`, () => { todos = before; saveTodos(); renderSide(); });
      break;
    }
    case "export": e.preventDefault(); downloadBackup(); break;
    case "reset": Object.assign(ui, { q: "", source: "all", rels: [], states: ["todo", "in_progress"] }); $("#q").value = ""; saveUi(); render(); break;
  }
});
// Clicking or tabbing into a row selects it.
document.addEventListener("focusin", e => {
  const row = e.target.closest?.("#list .drow, #list .row");
  if (row && row.dataset.id !== cur) setCur(row.dataset.id, false, false);
});
document.addEventListener("auxclick", e => {   // middle click on a row opens the ticket
  if (e.button !== 1) return;
  const t = e.target.closest("[data-act=toggle]"); if (!t) return;
  e.preventDefault(); openIssue(t.closest("[data-id]").dataset.id);
});
document.addEventListener("input", e => {
  if (e.target.dataset.act !== "note") return;
  const id = e.target.closest("[data-id]").dataset.id, v = e.target.value;
  if (v.trim()) local.notes[id] = v; else delete local.notes[id];
  saveLocal();
});
document.addEventListener("keydown", e => {
  if ((e.key === "Enter" || e.key === " ") && e.target.matches?.('.tlist .txt[role="button"]')) { e.preventDefault(); e.target.click(); return; }
  const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName);
  const plain = !e.metaKey && !e.ctrlKey && !e.altKey;
  if (!$("#help").hidden) { if (e.key === "Escape" || e.key === "?") { e.preventDefault(); toggleHelp(false); } return; }
  if (e.key === "Escape") {
    if (editing) { editing = null; renderSide(); }
    else if (typing) document.activeElement.blur();
    else if (ui.open) { ui.open = null; render(); }
    else setCur(null);
    return;
  }
  if (typing || !plain) return;
  const it = cur && data.items.find(i => i.id === cur);
  const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
  const act = {
    "/": () => $("#q").focus(),
    "?": () => toggleHelp(true),
    "r": refresh,
    "f": () => { ui.side = !ui.side; saveUi(); render(); },
    "n": () => { if (!ui.side) { ui.side = true; saveUi(); render(); } $("#todoInput").focus(); },
    "d": () => { ui.view = "dash"; saveUi(); render(); }, "p": () => { ui.view = "prs"; saveUi(); render(); },
    "l": () => { ui.view = "list"; saveUi(); render(); }, "i": () => { ui.view = "issues"; saveUi(); render(); },
    "j": () => moveCur(1), "ArrowDown": () => moveCur(1), "k": () => moveCur(-1), "ArrowUp": () => moveCur(-1),
    "ArrowRight": () => moveCol(1), "ArrowLeft": () => moveCol(-1),
    "Enter": () => { if (it) { ui.open = ui.open === it.id ? null : it.id; if (ui.open) openSeen([it.id]); render(); setCur(it.id); } },
    " ": () => { if (it && document.activeElement?.dataset.id === it.id) { ui.open = ui.open === it.id ? null : it.id; if (ui.open) openSeen([it.id]); render(); setCur(it.id); } },
    "o": () => { if (it) openIssue(it.id); else if (ui.open) openIssue(ui.open); },
    "s": () => { if (it) { local.pins = local.pins.includes(it.id) ? local.pins.filter(x => x !== it.id) : [...local.pins, it.id]; saveLocal(); render(); } },
    "m": () => { if (it) { dismissSeen([it.id]); render(); } },
    "x": () => { if (!it) return; const rows = rowEls(), i = rows.findIndex(r => r.dataset.id === it.id);
                 const nextId = (rows[i + 1] || rows[i - 1])?.dataset.id;
                 if (isHidden(it)) { delete local.hidden[it.id]; saveLocal(); } else hideItem(it);
                 cur = nextId || null; render(); },
    "t": () => { if (it) { todos.unshift({ id: newId(), text: it.title, ref: it.id, done: false, created: todayISO() }); saveTodos(); renderSide(); } },
  }[k];
  if (act) { e.preventDefault(); act(); }
});

(async () => {
  await signIn();
  local = LOCAL_DEF();
  const stored = await api("/api/local", {}, {});
  const dict = (v) => {
    const o = Object.create(null);
    if (v && typeof v === "object" && !Array.isArray(v)) {
      for (const [k, x] of Object.entries(v)) if (typeof x === "string") o[k] = x;
    }
    return o;
  };
  local.pins = Array.isArray(stored.pins) ? stored.pins.filter(p => typeof p === "string") : [];
  for (const k of ["notes", "seen", "seenStatus", "hidden", "bucket"]) local[k] = dict(stored[k]);
  local.seenInit = stored.seenInit === true;
  await loadTodos();
  render();
  load();
  setInterval(load, 60000);          // pick up the server's background refresh
  setInterval(renderMeta, 30000);    // keep relative times current
})();
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
