"""Tests for helpers, the store, the local API and the command line.

Run with ``python -m unittest discover -s tests``.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import importlib.util
import io
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import zipfile
from pathlib import Path
from typing import Any
from unittest import mock

import test_onmyplate

wl = test_onmyplate.wl
SCRIPT = test_onmyplate.ROOT / "onmyplate.py"


NO_GITLAB_TOKEN = (
    "GitLab: no token. Store it in the system keychain with 'onmyplate --set-token gitlab', "
    'or set ONMYPLATE_GITLAB_TOKEN or "token_command".'
)


class DataDirCase(unittest.TestCase):
    """Gives every test a fresh data folder."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        wl.set_data_dir(self.root)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


class HelperTests(unittest.TestCase):
    def test_sanitize(self) -> None:
        self.assertEqual(wl.sanitize("a\x00b" + chr(0x202E) + "c\n"), "a b c ")
        self.assertEqual(wl.sanitize("x" * 10, 3), "xxx")
        self.assertEqual(wl.sanitize(None), "None")

    def test_to_iso(self) -> None:
        self.assertEqual(wl.to_iso("2026-10-20T10:00:00.000+0200"), "2026-10-20T10:00:00+02:00")
        self.assertEqual(wl.to_iso("2026-10-20T08:00:00Z"), "2026-10-20T08:00:00+00:00")
        self.assertEqual(wl.to_iso("2026-10-20"), "2026-10-20")
        self.assertEqual(wl.to_iso("garbage"), "garbage")
        self.assertIsNone(wl.to_iso(""))
        self.assertIsNone(wl.to_iso(None))

    def test_display_name(self) -> None:
        self.assertEqual(wl.display_name({"displayName": "Sam", "name": "sam"}), "Sam")
        self.assertEqual(wl.display_name({"username": "sam"}), "sam")
        self.assertEqual(wl.display_name(None), "")

    def test_redact_url(self) -> None:
        self.assertEqual(
            wl.redact_url("https://h.example/p/a?private_token=x#f"), "https://h.example/p/a"
        )

    def test_prune_relations(self) -> None:
        for given, expected in (
            ({"team", "participant", "reviewer"}, {"reviewer"}),
            ({"team", "participant"}, {"team"}),
            ({"participant"}, {"participant"}),
        ):
            relations = set(given)
            wl.prune_relations(relations)
            self.assertEqual(relations, expected)

    def test_merge_config_recurses_without_mutating_defaults(self) -> None:
        merged = wl.merge_config(wl.DEFAULT_CONFIG, {"port": 1, "jira": {"token": "t"}})
        self.assertEqual(merged["port"], 1)
        self.assertEqual(merged["jira"]["token"], "t")
        self.assertNotIn("token", wl.DEFAULT_CONFIG["jira"])
        self.assertEqual(merged["jira"]["type"], "auto")
        self.assertEqual(wl.merge_config({"a": 1}, None), {"a": 1})

    def test_example_config_matches_defaults(self) -> None:
        example = json.loads((test_onmyplate.ROOT / "onmyplate.config.example.json").read_text())
        self.assertEqual(example, wl.DEFAULT_CONFIG)

    def test_auth_headers(self) -> None:
        self.assertEqual(wl.bearer_or_basic("bearer", "", "t"), {"Authorization": "Bearer t"})
        basic = wl.bearer_or_basic("basic", "me@x", "t")["Authorization"]
        self.assertEqual(base64.b64decode(basic.split()[1]), b"me@x:t")

    def test_with_query(self) -> None:
        self.assertEqual(wl.with_query("https://h/x", None), "https://h/x")
        self.assertEqual(wl.with_query("https://h/x", {"a": "b c"}), "https://h/x?a=b+c")

    def test_session_token_depends_on_secret_and_start(self) -> None:
        self.assertEqual(wl.session_token("a" * 40, "x"), wl.session_token("a" * 40, "x"))
        self.assertNotEqual(wl.session_token("a" * 40, "x"), wl.session_token("b" * 40, "x"))
        self.assertNotEqual(wl.session_token("a" * 40, "x"), wl.session_token("a" * 40, "y"))

    def test_csp_allows_exactly_the_embedded_script(self) -> None:
        script = wl.INDEX_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
        digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
        csp = wl.content_security_policy(wl.INDEX_HTML)
        self.assertIn(f"script-src 'sha256-{digest}'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("default-src 'none'", csp)

    def test_ui_has_no_external_resources(self) -> None:
        self.assertIsNone(re.search(r"""(src|href)=["']https?://""", wl.INDEX_HTML))
        self.assertNotIn("@import", wl.INDEX_HTML)

    def test_versions_agree(self) -> None:
        pyproject = (test_onmyplate.ROOT / "pyproject.toml").read_text()
        self.assertIn(f'version = "{wl.VERSION}"', pyproject)
        changelog = (test_onmyplate.ROOT / "CHANGELOG.md").read_text()
        self.assertIn(f"## [{wl.VERSION}]", changelog)

    def test_login_codes_expire(self) -> None:
        codes = wl.LoginCodes()
        with mock.patch.object(wl, "LOGIN_CODE_TTL_SECONDS", -1):
            expired = codes.issue()
        self.assertFalse(codes.redeem(expired))
        self.assertFalse(codes.redeem("unknown"))


class LicenseTests(unittest.TestCase):
    def test_own_license_is_embedded_verbatim(self) -> None:
        text = (test_onmyplate.ROOT / "LICENSE").read_text("utf-8").strip()
        self.assertEqual(
            wl.LICENSES["self"], {"name": "On My Plate", "license": "MIT", "text": text}
        )

    def test_no_third_party_software_is_shipped(self) -> None:
        self.assertEqual([g["packages"] for g in wl.LICENSES["groups"]], [[], []])
        self.assertIn("only the Python standard library", wl.LICENSES["note"])

    def test_text_lists_packages_with_their_license_texts(self) -> None:
        package = {"name": "lib", "version": "1.2", "license": "ISC", "url": "", "texts": ["ISC"]}
        fake = {**wl.LICENSES, "note": "", "groups": [{"title": "Web UI", "packages": [package]}]}
        with mock.patch.object(wl, "LICENSES", fake):
            text = wl.licenses_text()
        self.assertIn("lib 1.2 (Web UI)\nLicense: ISC\n\nISC", text)
        self.assertTrue(text.startswith(f"On My Plate {wl.VERSION}: MIT License"))

    @unittest.skipUnless(shutil.which("npm"), "needs npm")
    def test_generated_files_are_up_to_date(self) -> None:
        script = test_onmyplate.ROOT / "tools" / "third_party_licenses.py"
        result = subprocess.run(
            [sys.executable, str(script), "--check"], capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class TodoParsingTests(unittest.TestCase):
    def test_open_tasks_come_first(self) -> None:
        text = wl.render_todos(
            [
                {"id": "d", "text": "Done", "done": True, "done_at": "2026-10-01"},
                {"id": "o", "text": "Open", "done": False},
            ]
        )
        self.assertLess(text.index("id:o"), text.index("id:d"))
        self.assertLess(text.index("## Open"), text.index("id:o"))
        self.assertLess(text.index("## Done"), text.index("id:d"))
        # Files written by earlier versions with German headings still parse.
        old_file = "## Offen\n\n- [ ] A <!-- id:a1 -->\n\n## Erledigt\n\n- [x] B <!-- id:b2 -->\n"
        self.assertEqual([t["text"] for t in wl.parse_todos(old_file)[0]], ["A", "B"])
        self.assertIn("✅ 2026-10-01", text)

    def test_bullets_case_and_duplicates(self) -> None:
        parsed, needs_rewrite = wl.parse_todos(
            "* [X] Star <!-- id:a1 -->\n- [ ] Copy <!-- id:a1 ref:evil:1 -->\nprose\n- [ ]   \n"
        )
        self.assertTrue(needs_rewrite)
        self.assertEqual([t["text"] for t in parsed], ["Star", "Copy"])
        self.assertTrue(parsed[0]["done"])
        self.assertNotEqual(parsed[1]["id"], "a1")
        self.assertNotIn("ref", parsed[1])

    def test_validation_drops_empty_and_limits_size(self) -> None:
        self.assertEqual(wl.validate_todos([{"id": "a", "text": "  "}]), [])
        with self.assertRaises(TypeError):
            wl.validate_todos([{"id": f"i{n}", "text": "x"} for n in range(wl.MAX_TODOS + 1)])


# --------------------------------------------------------------------------- #
# Files, backup and migration
# --------------------------------------------------------------------------- #


class SessionPerStartTests(DataDirCase):
    """A token taken from browser storage must not work against a later start."""

    def test_token_of_an_earlier_start_is_rejected(self) -> None:
        secret = wl.load_secret()
        store = wl.Store(wl.merge_config(wl.DEFAULT_CONFIG, {}), demo=True)
        first = wl.OnMyPlateServer(0, store, secret)
        first.server_close()
        second = wl.OnMyPlateServer(0, store, secret)
        self.addCleanup(second.server_close)
        threading.Thread(target=second.serve_forever, daemon=True).start()
        self.addCleanup(second.shutdown)
        self.assertNotEqual(first.session_token, second.session_token)
        port = second.server_address[1]

        def items(token: str) -> int:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request(
                "GET", "/api/items", headers={"Host": f"127.0.0.1:{port}", wl.TOKEN_HEADER: token}
            )
            return conn.getresponse().status

        self.assertEqual(items(first.session_token), 401)
        self.assertEqual(items(second.session_token), 200)


@unittest.skipUnless(os.name == "posix", "file ownership and modes are POSIX")
class ConfigTrustTests(DataDirCase):
    """Only a config that no other user can change may decide where tokens go."""

    def config(self, mode: int = 0o600) -> Path:
        path = self.root / "onmyplate.config.json"
        path.write_text("{}")
        path.chmod(mode)
        return path

    def test_private_config_is_trusted(self) -> None:
        self.root.chmod(0o700)
        self.assertIsNone(wl.untrusted_reason(self.config()))
        self.assertIsNone(wl.untrusted_reason(self.root / "missing.json"))

    def test_config_writable_by_others_is_refused(self) -> None:
        self.root.chmod(0o700)
        for mode in (0o620, 0o602, 0o666):
            with self.subTest(mode=oct(mode)):
                self.assertIn("writable by other users", wl.untrusted_reason(self.config(mode)))

    def test_folder_writable_by_others_is_refused_unless_sticky(self) -> None:
        path = self.config()
        self.root.chmod(0o777)
        self.addCleanup(self.root.chmod, 0o700)
        self.assertIn("its folder", wl.untrusted_reason(path))
        self.root.chmod(0o777 | stat.S_ISVTX)  # like /tmp: others cannot replace your file
        self.assertIsNone(wl.untrusted_reason(path))

    def test_symlink_in_an_unsafe_folder_is_refused(self) -> None:
        self.root.chmod(0o700)
        target = self.config()
        shared = self.root / "shared"
        shared.mkdir()
        shared.chmod(0o777)
        link = shared / "onmyplate.config.json"
        link.symlink_to(target)
        self.assertRegex(wl.untrusted_reason(link), r"its folder .*/shared is writable")

    def test_start_is_refused(self) -> None:
        self.root.chmod(0o700)
        path = self.config(0o666)
        with self.assertRaisesRegex(SystemExit, "Not starting"):
            wl.load_config(path)

    def test_reload_keeps_the_previous_settings(self) -> None:
        self.root.chmod(0o700)
        path = self.config()
        cfg = wl.merge_config(wl.DEFAULT_CONFIG, {n: {"enabled": False} for n in wl.SOURCE_NAMES})
        store = wl.Store(cfg, config_path=path)
        path.write_text(json.dumps({"gitlab": {"url": "https://evil.example", "token": "x"}}))
        path.chmod(0o666)
        os.utime(path, (5, 5))
        store.refresh()
        self.assertIn("Config not read", store.errors[0])
        self.assertFalse(store.cfg["gitlab"]["enabled"])
        self.assertNotIn("evil", json.dumps(store.cfg))


class TokenStorageTests(DataDirCase):
    """Tokens live in the system keychain (here: a memory stand-in), never in the config file."""

    def setUp(self) -> None:
        super().setUp()
        self.root.chmod(0o700)
        self.keychain = test_onmyplate.MemoryKeychain()
        patcher = mock.patch.object(wl, "_KEYCHAIN", self.keychain)
        patcher.start()
        self.addCleanup(patcher.stop)
        wl._COMMAND_TOKENS.clear()
        env = mock.patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def write_config(self, **sections: Any) -> Path:
        path = self.root / "onmyplate.config.json"
        wl.write_private(path, json.dumps(sections))
        return path

    def test_lookup_order(self) -> None:
        cfg = {"url": "https://gitlab.example.com"}
        self.assertEqual(wl.resolve_token("gitlab", cfg, cfg["url"]), ("", ""))
        self.keychain.set("gitlab@gitlab.example.com", "from-keychain")
        self.assertEqual(wl.resolve_token("gitlab", cfg, cfg["url"]), ("from-keychain", "keychain"))
        command = f"{sys.executable} -c \"print('from-command')\""
        with_command = {**cfg, "token_command": command}
        self.assertEqual(
            wl.resolve_token("gitlab", with_command, cfg["url"]), ("from-command", "command")
        )
        os.environ["ONMYPLATE_GITLAB_TOKEN"] = "from-env"
        self.assertEqual(wl.resolve_token("gitlab", with_command, cfg["url"]), ("from-env", "env"))

    def test_token_belongs_to_one_host(self) -> None:
        self.keychain.set("jira@jira.example.com", "for-this-host")
        self.assertEqual(
            wl.resolve_token("jira", {}, "https://jira.example.com/jira"),
            ("for-this-host", "keychain"),
        )
        self.assertEqual(wl.resolve_token("jira", {}, "https://evil.example"), ("", ""))

    def test_plaintext_tokens_move_out_of_the_config(self) -> None:
        path = self.write_config(
            gitlab={"url": "https://gitlab.example.com", "token": "glpat-abc123"},
            jira={"enabled": False},
        )
        with mock.patch("sys.stdout", io.StringIO()) as out:
            cfg = wl.load_config(path)
        self.assertIn("Moved the GitLab token", out.getvalue())
        self.assertNotIn("glpat", path.read_text())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.keychain.items, {"gitlab@gitlab.example.com": "glpat-abc123"})
        source = wl.GitLabSource(cfg["gitlab"], wl.HttpClient({}))
        self.assertEqual((source.token, source.token_origin), ("glpat-abc123", "keychain"))

    def test_without_a_keychain_plaintext_is_refused(self) -> None:
        path = self.write_config(
            gitlab={"url": "https://gitlab.example.com", "token": "glpat-abc123"}
        )
        with (
            mock.patch.object(wl, "_KEYCHAIN", None),
            self.assertRaisesRegex(SystemExit, "plain text"),
        ):
            wl.load_config(path)

    def test_tokens_added_while_running_move_on_refresh(self) -> None:
        path = self.write_config(**{n: {"enabled": False} for n in wl.SOURCE_NAMES})
        store = wl.Store(
            wl.merge_config(wl.DEFAULT_CONFIG, json.loads(path.read_text())), config_path=path
        )
        off = {n: {"enabled": False} for n in wl.SOURCE_NAMES}
        wl.write_private(
            path, json.dumps({**off, "gitlab": {"enabled": False, "token": "glpat-late"}})
        )
        os.utime(path, (3, 3))
        store.refresh()
        self.assertEqual(store.errors, [])
        self.assertNotIn("glpat", path.read_text())
        self.assertEqual(self.keychain.items, {"gitlab@gitlab.your-company.example": "glpat-late"})
        wl.write_private(
            path, json.dumps({**off, "gitlab": {"enabled": False, "token": "glpat-x"}})
        )
        os.utime(path, (4, 4))
        with mock.patch.object(wl, "_KEYCHAIN", None):
            store.refresh()
        self.assertIn("Config not read", store.errors[0])

    def test_token_command(self) -> None:
        good = f"{sys.executable} -c \"print('  cmd-token  ')\""
        self.assertEqual(wl.command_token(good, "GitLab"), "cmd-token")
        with mock.patch.object(wl, "_run") as run:
            self.assertEqual(wl.command_token(good, "GitLab"), "cmd-token")
        run.assert_not_called()  # asked once per start, not on every refresh
        with self.assertRaisesRegex(wl.SourceError, "token_command failed"):
            wl.command_token(f'{sys.executable} -c "raise SystemExit(3)"', "GitLab")
        with self.assertRaisesRegex(wl.SourceError, "could not run"):
            wl.command_token("/does/not/exist", "GitLab")

    def test_set_and_delete_token(self) -> None:
        path = self.write_config(gitlab={"url": "https://gitlab.example.com"})
        with (
            mock.patch("sys.stdin", io.StringIO("glpat-from-pipe\n")),
            mock.patch("sys.stdout", io.StringIO()) as out,
        ):
            wl.run_token("gitlab", path)
        self.assertEqual(self.keychain.items, {"gitlab@gitlab.example.com": "glpat-from-pipe"})
        self.assertIn("Stored the GitLab token for gitlab.example.com", out.getvalue())
        self.assertNotIn("glpat", out.getvalue() + path.read_text())
        with mock.patch("sys.stdout", io.StringIO()):
            wl.run_token("gitlab", path, delete=True)
        self.assertEqual(self.keychain.items, {})
        for bad in ("", "has space", 'quo"te'):
            stdin = mock.patch("sys.stdin", io.StringIO(bad + "\n"))
            with self.subTest(token=bad), stdin, self.assertRaisesRegex(SystemExit, "not a token"):
                wl.run_token("gitlab", path)

    def test_set_token_needs_the_url_first(self) -> None:
        path = self.write_config()
        with self.assertRaisesRegex(SystemExit, "Set jira.url"):
            wl.run_token("jira", path)

    def test_real_tokens_are_storable(self) -> None:
        for token in ("glpat-xY_9z-AbCdEf", "ATATT3xFfGF0aB+c/d==", "NjQ5ODk5MDk1NzY4OkV4YW1wbGU="):
            self.assertTrue(wl.RE_STORABLE_TOKEN.fullmatch(token), token)
        for token in ("two words", 'a"b', "a'b", "a\\b", "tab\there"):
            self.assertIsNone(wl.RE_STORABLE_TOKEN.fullmatch(token), token)


@unittest.skipUnless(sys.platform == "darwin" and Path("/usr/bin/security").exists(), "macOS only")
class MacKeychainTests(unittest.TestCase):
    """The real macOS backend, against a throw-away keychain file instead of the login keychain."""

    def test_store_read_and_delete(self) -> None:
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        path = str(Path(folder) / "test.keychain-db")
        security = "/usr/bin/security"
        subprocess.run(
            [security, "create-keychain", "-p", "pw", path], check=True, capture_output=True
        )
        self.addCleanup(
            subprocess.run, [security, "delete-keychain", path], capture_output=True, check=False
        )
        subprocess.run(
            [security, "unlock-keychain", "-p", "pw", path], check=True, capture_output=True
        )
        keychain = wl.MacKeychain(path)
        self.assertIsNone(keychain.get("gitlab@gitlab.example.com"))
        keychain.set("gitlab@gitlab.example.com", "glpat-Real+Token/1=")
        self.assertEqual(keychain.get("gitlab@gitlab.example.com"), "glpat-Real+Token/1=")
        keychain.set("gitlab@gitlab.example.com", "glpat-replaced")
        self.assertEqual(keychain.get("gitlab@gitlab.example.com"), "glpat-replaced")
        self.assertIsNone(keychain.get("gitlab@other.example.com"))
        self.assertTrue(keychain.delete("gitlab@gitlab.example.com"))
        self.assertIsNone(keychain.get("gitlab@gitlab.example.com"))


# Writes to the real keychain of the machine, so it runs only where asked for (CI sets it).
SYSTEM_KEYCHAIN = os.environ.get("ONMYPLATE_TEST_SYSTEM_KEYCHAIN") == "1"


@unittest.skipUnless(
    SYSTEM_KEYCHAIN, "set ONMYPLATE_TEST_SYSTEM_KEYCHAIN=1 to use the real keychain"
)
class SystemKeychainTests(unittest.TestCase):
    """The Linux and Windows backends against the real keychain of a CI machine."""

    ACCOUNT = "gitlab@onmyplate-ci.example"

    def roundtrip(self, keychain: Any) -> None:
        self.addCleanup(keychain.delete, self.ACCOUNT)
        keychain.delete(self.ACCOUNT)
        self.assertIsNone(keychain.get(self.ACCOUNT))
        keychain.set(self.ACCOUNT, "glpat-Real+Token/1=")
        self.assertEqual(keychain.get(self.ACCOUNT), "glpat-Real+Token/1=")
        keychain.set(self.ACCOUNT, "glpat-replaced")
        self.assertEqual(keychain.get(self.ACCOUNT), "glpat-replaced")
        self.assertIsNone(keychain.get("gitlab@other-host.example"))
        self.assertTrue(keychain.delete(self.ACCOUNT))
        self.assertIsNone(keychain.get(self.ACCOUNT))
        self.assertFalse(keychain.delete(self.ACCOUNT))

    def detected(self) -> Any:
        with mock.patch.object(wl, "_KEYCHAIN", False):
            return wl.system_keychain()

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux only")
    def test_secret_service(self) -> None:
        keychain = self.detected()
        self.assertIsInstance(keychain, wl.SecretServiceKeychain)
        self.roundtrip(keychain)

    @unittest.skipUnless(os.name == "nt", "Windows only")
    def test_windows_credential_manager(self) -> None:
        keychain = self.detected()
        self.assertIsInstance(keychain, wl.WindowsKeychain)
        self.roundtrip(keychain)


class UpdateTests(unittest.TestCase):
    """--update installs the latest release only after checking it against SHA256SUMS."""

    def release(self, version: str, script: bytes, sums: str | None = None) -> dict[str, bytes]:
        digest = hashlib.sha256(script).hexdigest()
        base = f"https://github.com/Gr33ndev/OnMyPlate/releases/download/v{version}/"
        api = {
            "tag_name": f"v{version}",
            "html_url": f"https://github.com/Gr33ndev/OnMyPlate/releases/tag/v{version}",
            "assets": [
                {"name": n, "browser_download_url": base + n}
                for n in ("onmyplate.py", f"onmyplate-{version}-py3-none-any.whl", "SHA256SUMS")
            ],
        }
        files = {
            wl.RELEASES_API: json.dumps(api).encode(),
            base + "onmyplate.py": script,
            base + f"onmyplate-{version}-py3-none-any.whl": b"wheel-bytes",
            base + "SHA256SUMS": (
                sums
                or f"{digest}  onmyplate.py\n{hashlib.sha256(b'wheel-bytes').hexdigest()}  onmyplate-{version}-py3-none-any.whl\n"
            ).encode(),
        }
        patcher = mock.patch.object(wl, "github_download", side_effect=lambda url, *_: files[url])
        patcher.start()
        self.addCleanup(patcher.stop)
        return files

    def script(self) -> Path:
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        path = Path(folder) / "onmyplate.py"
        path.write_text('VERSION = "old"\n')
        path.chmod(0o750)
        return path

    def test_up_to_date(self) -> None:
        self.release(wl.VERSION, b"same")
        path = self.script()
        with mock.patch("sys.stdout", io.StringIO()) as out:
            wl.run_update(path)
        self.assertIn("is up to date", out.getvalue())
        self.assertEqual(path.read_text(), 'VERSION = "old"\n')

    def test_script_is_replaced_and_the_old_one_kept(self) -> None:
        new = b'VERSION = "99.0.0"\nprint("new")\n'
        self.release("99.0.0", new)
        path = self.script()
        with mock.patch("sys.stdout", io.StringIO()) as out:
            wl.run_update(path)
        self.assertEqual(path.read_bytes(), new)
        self.assertEqual(path.stat().st_mode & 0o777, 0o750)
        self.assertEqual(
            (path.parent / f"onmyplate-{wl.VERSION}.py.bak").read_text(), 'VERSION = "old"\n'
        )
        self.assertIn("Restart On My Plate to use 99.0.0", out.getvalue())

    def test_checksum_mismatch_changes_nothing(self) -> None:
        self.release("99.0.0", b'VERSION = "99.0.0"\n', sums="0" * 64 + "  onmyplate.py\n")
        path = self.script()
        with self.assertRaisesRegex(SystemExit, "does not match SHA256SUMS"):
            wl.run_update(path)
        self.assertEqual(path.read_text(), 'VERSION = "old"\n')
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["onmyplate.py"])

    def test_wrong_version_inside_the_file_is_refused(self) -> None:
        self.release("99.0.0", b'VERSION = "1.0.0"\n')
        path = self.script()
        with (
            mock.patch("sys.stdout", io.StringIO()),
            self.assertRaisesRegex(SystemExit, "not version 99.0.0"),
        ):
            wl.run_update(path)
        self.assertEqual(path.read_text(), 'VERSION = "old"\n')

    def test_package_install_reinstalls_the_checked_wheel(self) -> None:
        self.release("99.0.0", b'VERSION = "99.0.0"\n')
        site = Path("/opt/venv/lib/python3.14/site-packages")
        done = subprocess.CompletedProcess([], 0, "", "")
        with (
            mock.patch.object(wl, "APP_DIR", site),
            mock.patch.object(wl.shutil, "which", return_value=None),
            mock.patch.object(wl, "_run", return_value=done) as run,
            mock.patch("sys.stdout", io.StringIO()),
        ):
            wl.run_update()
        command = run.call_args.args[0]
        self.assertEqual(command[1:5], ["-m", "pip", "install", "--upgrade"])
        self.assertTrue(command[-1].endswith("onmyplate-99.0.0-py3-none-any.whl"))

    def test_redirects_stay_on_github(self) -> None:
        handler = wl._GitHubRedirectHandler()
        request = urllib.request.Request("https://github.com/x")
        with self.assertRaisesRegex(wl.SourceError, "redirected to evil.example"):
            handler.redirect_request(request, None, 302, "", {}, "https://evil.example/file")
        allowed = handler.redirect_request(
            request, None, 302, "", {}, "https://release-assets.githubusercontent.com/f"
        )
        self.assertEqual(allowed.full_url, "https://release-assets.githubusercontent.com/f")


class FileTests(DataDirCase):
    def test_write_private_is_atomic_and_private(self) -> None:
        target = self.root / "f.txt"
        wl.write_private(target, "one")
        wl.write_private(target, b"two")
        self.assertEqual(target.read_text(), "two")
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["f.txt"])

    def test_readable_by_others(self) -> None:
        target = self.root / "c.json"
        self.assertFalse(wl.is_readable_by_others(target))
        target.write_text("{}")
        target.chmod(0o644)
        self.assertTrue(wl.is_readable_by_others(target))
        target.chmod(0o600)
        self.assertFalse(wl.is_readable_by_others(target))

    def test_secret_is_created_kept_and_rotated(self) -> None:
        first = wl.load_secret()
        self.assertGreaterEqual(len(first), 32)
        self.assertEqual(wl.load_secret(), first)
        self.assertNotEqual(wl.load_secret(rotate=True), first)
        self.assertEqual(wl.PATHS.secret.stat().st_mode & 0o777, 0o600)

    def test_broken_local_file_falls_back(self) -> None:
        wl.PATHS.local.write_text("{nope")
        self.assertEqual(wl.load_local(), {"pins": [], "notes": {}})
        wl.PATHS.local.write_text("[1]")
        self.assertEqual(wl.load_local(), {})

    def test_hand_written_tasks_get_ids_on_load(self) -> None:
        wl.PATHS.todos.write_text("- [ ] By hand\n")
        todo_id = wl.load_todos()[0]["id"]
        self.assertIn(f"id:{todo_id}", wl.PATHS.todos.read_text())

    def test_tasks_from_old_versions_are_migrated(self) -> None:
        old = [{"id": "x1", "text": "Old", "created": "2026-01-01T10:00:00"}, "junk"]
        wl.save_local({"pins": [], "todos": old})
        with mock.patch("sys.stdout", io.StringIO()):
            wl.migrate_legacy_todos()
        self.assertEqual(
            wl.load_todos(), [{"id": "x1", "text": "Old", "done": False, "created": "2026-01-01"}]
        )
        self.assertNotIn("todos", wl.load_local())

    def test_backup_with_config(self) -> None:
        wl.write_private(wl.PATHS.config, '{"port": 1}')
        names = zipfile.ZipFile(io.BytesIO(wl.make_backup(include_config=True))).namelist()
        self.assertIn("onmyplate.config.json", names)
        self.assertIn("README.txt", names)

    def zip_with(self, **files: str) -> Path:
        archive = self.root / "in.zip"
        with zipfile.ZipFile(archive, "w") as zipped:
            for name, content in files.items():
                zipped.writestr(name, content)
        return archive

    def test_import_keeps_existing_files(self) -> None:
        wl.save_local({"pins": ["old"]})
        archive = self.zip_with(**{"onmyplate.local.json": '{"pins": ["new"]}'})
        restored, backed_up, stamp = wl.import_backup(archive)
        self.assertEqual(
            (restored, backed_up), (["onmyplate.local.json"], ["onmyplate.local.json"])
        )
        self.assertEqual(wl.load_local(), {"pins": ["new"]})
        kept = self.root / f"onmyplate.local.json.bak-{stamp}"
        self.assertEqual(json.loads(kept.read_text()), {"pins": ["old"]})

    def test_import_rejects_bad_archives(self) -> None:
        with self.assertRaises(SystemExit):
            wl.import_backup(self.zip_with(**{"../evil.txt": "x"}))
        with self.assertRaises(ValueError):
            wl.import_backup(self.zip_with(**{"onmyplate.local.json": "{broken"}))
        with mock.patch.object(wl, "MAX_IMPORT_FILE_BYTES", 1), self.assertRaises(SystemExit):
            wl.import_backup(self.zip_with(**{"onmyplate.todos.md": "- [ ] x"}))
        self.assertFalse(wl.PATHS.local.exists())


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #


class StubSource(wl.Source):
    name = "gitlab"

    def __init__(self) -> None:
        self.result: Any = ([], [])

    def fetch(self) -> Any:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class StoreTests(unittest.TestCase):
    def test_failing_source_keeps_last_known_items(self) -> None:
        cfg = wl.merge_config(wl.DEFAULT_CONFIG, {})
        store = wl.Store(cfg)
        stub = StubSource()
        store._sources = lambda: ([stub], {})
        item = {"id": "gitlab:issue:1", "source": "gitlab", "title": "x", "relations": {"assignee"}}
        stub.result = ([item], ["a warning"])
        store.refresh()
        snapshot = store.snapshot()
        self.assertEqual(len(snapshot["items"]), 1)
        self.assertEqual((snapshot["errors"], snapshot["warnings"]), ([], ["a warning"]))
        self.assertIn("gitlab", snapshot["sources"])

        stub.result = wl.SourceError("down")
        store.refresh()
        snapshot = store.snapshot()
        self.assertEqual(snapshot["errors"], ["GitLab: down, showing last known state"])
        self.assertEqual(len(snapshot["items"]), 1)

        stub.result = RuntimeError("boom")
        store.refresh()
        self.assertEqual(
            store.errors, ["GitLab: Unexpected error (RuntimeError), showing last known state"]
        )

    def test_source_setup_errors(self) -> None:
        cfg = {
            "jira": {"enabled": True, "token": "t"},
            "gitlab": {"enabled": True, "url": "https://gitlab.example"},
            "bitbucket": {"enabled": False},
        }
        with mock.patch.dict(os.environ, {}, clear=True):
            sources, errors = wl.Store(cfg)._sources()
        self.assertEqual(sources, [])
        self.assertEqual(
            errors,
            {
                "jira": "jira: config entry 'url' is missing",
                "gitlab": NO_GITLAB_TOKEN,
            },
        )

    def test_config_is_read_again_when_it_changes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "onmyplate.config.json"
            disabled = {name: {"enabled": False} for name in wl.SOURCE_NAMES}
            path.write_text(json.dumps(disabled))
            store = wl.Store(wl.merge_config(wl.DEFAULT_CONFIG, disabled), config_path=path)
            store.refresh()
            self.assertEqual(store.errors, [])
            changed = {**disabled, "gitlab": {"enabled": True, "url": "https://gl.example"}}
            path.write_text(json.dumps(changed))
            os.utime(path, (1, 1))  # a different mtime, also on coarse file systems
            with mock.patch.dict(os.environ, {}, clear=True):
                store.refresh()
                setup = store.snapshot()["setup"]
            self.assertEqual(store.errors, [NO_GITLAB_TOKEN])
            gitlab = next(x for x in setup["sources"] if x["name"] == "gitlab")
            self.assertEqual(
                (gitlab["enabled"], gitlab["url"], gitlab["token"], gitlab["error"]),
                (True, "https://gl.example", "", NO_GITLAB_TOKEN),
            )
            self.assertEqual(setup["config"], str(path))
            path.write_text("{broken")
            os.utime(path, (2, 2))
            store.refresh()
            self.assertIn("keeping the previous settings", store.errors[0])
            self.assertTrue(store.cfg["gitlab"]["enabled"])

    def test_setup_shows_where_tokens_come_from_but_never_the_tokens(self) -> None:
        cfg = wl.merge_config(
            wl.DEFAULT_CONFIG,
            {
                "jira": {"url": "https://jira.example.com"},
                "gitlab": {"url": "https://gitlab.example.com"},
                "bitbucket": {"url": "https://bitbucket.example.com"},
            },
        )
        wl._KEYCHAIN.set("jira@jira.example.com", "keychain-s3cret")
        self.addCleanup(wl._KEYCHAIN.delete, "jira@jira.example.com")
        with mock.patch.dict(os.environ, {"ONMYPLATE_BITBUCKET_TOKEN": "env-s3cret"}, clear=True):
            store = wl.Store(cfg, config_path=Path("x.json"))
            store._sources()
            setup = store.snapshot()["setup"]
        self.assertNotIn("s3cret", json.dumps(setup))
        tokens = {x["name"]: x["token"] for x in setup["sources"]}
        self.assertEqual(tokens, {"jira": "keychain", "gitlab": "", "bitbucket": "env"})
        self.assertEqual(
            wl.Store(cfg, demo=True).snapshot()["setup"], {"config": "", "sources": []}
        )

    def test_snapshot_lists_enabled_sources_and_links(self) -> None:
        cfg = wl.merge_config(
            wl.DEFAULT_CONFIG,
            {
                "jira": {"enabled": False},
                "gitlab": {"url": "https://gl.example/"},
                "verify_ssl": False,
            },
        )
        snapshot = wl.Store(cfg).snapshot()
        self.assertEqual(snapshot["enabled"], ["gitlab", "bitbucket"])
        self.assertEqual(snapshot["links"]["gitlab"], "https://gl.example")
        self.assertTrue(any("verify_ssl" in w for w in snapshot["warnings"]))

    def test_demo_data_stays_the_same_on_refresh(self) -> None:
        store = wl.Store(wl.merge_config(wl.DEFAULT_CONFIG, {}), demo=True)
        store.refresh()
        first = store.snapshot()["items"]
        time.sleep(0.01)
        store.refresh()
        self.assertEqual(store.snapshot()["items"], first)

    def test_demo_items_are_valid(self) -> None:
        for sources in (wl.SOURCE_NAMES, ("gitlab",), ("bitbucket",)):
            items = wl.demo_items(sources)
            self.assertTrue(items)
            self.assertEqual({item["source"] for item in items}, set(sources))
            ids = [item["id"] for item in items]
            self.assertEqual(len(ids), len(set(ids)))
            for item in items:
                self.assertEqual(wl.coerce_item(item)["relations"], sorted(item["relations"]))


# --------------------------------------------------------------------------- #
# Local API
# --------------------------------------------------------------------------- #


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        wl.set_data_dir(cls.tmp.name)
        store = wl.Store(wl.merge_config(wl.DEFAULT_CONFIG, {}), demo=True)
        store.refresh()
        cls.server = wl.OnMyPlateServer(0, store, wl.load_secret())
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.token = cls.server.session_token

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def setUp(self) -> None:
        wl.set_data_dir(self.tmp.name)

    def call(
        self, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None
    ) -> tuple[int, bytes, http.client.HTTPResponse]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
        all_headers = {
            "Host": f"localhost:{self.port}",
            wl.TOKEN_HEADER: self.token,
            "Content-Type": "application/json",
            **(headers or {}),
        }
        conn.request(method, path, payload, all_headers)
        response = conn.getresponse()
        return response.status, response.read(), response

    def test_security_headers(self) -> None:
        status, page, response = self.call("GET", "/index.html")
        self.assertEqual(status, 200)
        self.assertIn(b"On My Plate", page)
        self.assertEqual(response.getheader("X-Frame-Options"), "DENY")
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertEqual(response.getheader("Content-Security-Policy"), self.server.csp)

    def test_refresh_returns_a_snapshot(self) -> None:
        status, body, _ = self.call("POST", "/api/refresh")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(data["demo"])
        self.assertEqual(data["version"], wl.VERSION)

    def test_local_state_roundtrip_drops_todos(self) -> None:
        state = {"pins": ["jira:PLAT-1"], "notes": {"jira:PLAT-1": "ask Anna"}, "todos": ["x"]}
        self.assertEqual(self.call("POST", "/api/local", state)[0], 200)
        stored = json.loads(self.call("GET", "/api/local")[1])
        self.assertEqual(stored, {"pins": ["jira:PLAT-1"], "notes": {"jira:PLAT-1": "ask Anna"}})

    def test_export_contains_no_config(self) -> None:
        wl.write_private(wl.PATHS.config, '{"jira": {"token": "secret"}}')
        status, body, response = self.call("GET", "/api/export")
        self.assertEqual(status, 200)
        self.assertIn("attachment", response.getheader("Content-Disposition"))
        self.assertNotIn("onmyplate.config.json", zipfile.ZipFile(io.BytesIO(body)).namelist())

    def test_licenses(self) -> None:
        status, body, _ = self.call("GET", "/api/licenses")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(data["version"], wl.VERSION)
        self.assertEqual(data["self"], wl.LICENSES["self"])

    def test_bad_requests(self) -> None:
        self.assertEqual(self.call("GET", "/api/nothing")[0], 404)
        self.assertEqual(self.call("POST", "/api/nothing", {})[0], 404)
        self.assertEqual(self.call("POST", "/api/local", b"[1]")[0], 400)
        self.assertEqual(self.call("POST", "/api/local", b"{nope")[0], 400)
        self.assertEqual(self.call("POST", "/api/todos", {"todos": "x"})[0], 400)
        self.assertEqual(self.call("POST", "/api/todos", {"todos": [{"id": "a b"}]})[0], 400)
        too_big = {"Content-Length": str(wl.MAX_REQUEST_BYTES + 1)}
        self.assertEqual(self.call("POST", "/api/local", b"{}", too_big)[0], 413)

    def test_login_failures(self) -> None:
        self.assertEqual(self.call("POST", "/api/login", {"code": 1})[0], 401)
        self.assertEqual(self.call("POST", "/api/login", {"code": "made-up"})[0], 401)
        self.assertEqual(self.call("POST", "/api/login", {}, {"Host": "evil.example"})[0], 403)

    def test_same_site_requests_are_rejected(self) -> None:
        self.assertEqual(
            self.call("GET", "/api/items", headers={"Sec-Fetch-Site": "same-site"})[0], 403
        )
        same_origin = {"Origin": f"http://localhost:{self.port}", "Sec-Fetch-Site": "same-origin"}
        self.assertEqual(self.call("GET", "/api/items", headers=same_origin)[0], 200)


# --------------------------------------------------------------------------- #
# Browser demo for GitHub Pages
# --------------------------------------------------------------------------- #


def load_ui_tool() -> Any:
    spec = importlib.util.spec_from_file_location(
        "ui_tool", test_onmyplate.ROOT / "tools" / "ui.py"
    )
    ui_tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ui_tool)
    return ui_tool


# Runs the demo page's replay script in Node with the browser APIs it needs, then
# performs the requests given as JSON and prints what the UI would receive.
NODE_HARNESS = r"""
const fs = require("fs");
const [page, scenario] = process.argv.slice(2).map((f) => fs.readFileSync(f, "utf8"));
const store = new Map();
globalThis.window = globalThis;
globalThis.location = { href: "https://example.github.io/OnMyPlate/demo/" };
globalThis.sessionStorage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, String(v)) };
globalThis.fetch = async () => { throw new Error("the demo must not use the network"); };
eval(page.split("<script>")[1].split("</script>")[0]);
(async () => {
  const out = [];
  for (const [method, path, body] of JSON.parse(scenario)) {
    const opts = { method, headers: { "Content-Type": "application/json" } };
    if (body !== null) opts.body = JSON.stringify(body);
    const r = await fetch(path, opts);
    const type = r.headers.get("Content-Type") || "";
    out.push({ status: r.status, type, body: type.startsWith("application/json") ? await r.text() : null });
  }
  console.log(JSON.stringify(out));
})();
"""

# What the UI does in a session, plus requests it never makes.
DEMO_SCENARIO = [
    ["GET", "/api/items", None],
    ["GET", "/api/local", None],
    ["GET", "/api/todos", None],
    ["POST", "/api/todos", {"todos": [{"id": "t1", "text": "From the test", "done": False}]}],
    ["GET", "/api/todos", None],
    ["POST", "/api/local", {"pins": ["jira:PLAT-1"], "notes": {"jira:PLAT-1": "note"}}],
    ["GET", "/api/local", None],
    ["POST", "/api/refresh", None],
    ["GET", "/api/export", None],
    ["POST", "/api/login", {"code": "made-up"}],
    ["GET", "/api/unknown", None],
]
DATE_LIKE = re.compile(r"^\d{4}-\d{2}-\d{2}(T[\d:.]+(Z|[+-]\d{2}:\d{2})?)?$")


def comparable(response: dict[str, Any]) -> dict[str, Any]:
    """Status, type and JSON body, with times and temporary folders blanked out."""

    def blank(value: Any) -> Any:
        if isinstance(value, str) and DATE_LIKE.match(value):
            return "<date>"
        if isinstance(value, str) and "onmyplate-demo-" in value:
            return "<data dir>"
        if isinstance(value, list):
            return [blank(v) for v in value]
        if isinstance(value, dict):
            return {k: blank(v) for k, v in value.items()}
        return value

    body = response["body"]
    is_json = response["type"].startswith("application/json")
    return {
        "status": response["status"],
        "type": response["type"],
        "body": blank(json.loads(body)) if is_json and body else None,
    }


class BrowserDemoTests(unittest.TestCase):
    """The GitHub Pages demo must behave exactly like the real server in demo mode."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.ui_tool = load_ui_tool()
        cls.page, cls.recorded = cls.ui_tool.record_demo(wl)
        cls.demo_page = cls.ui_tool.build_demo_page(cls.page, cls.recorded, int(time.time() * 1000))

    def test_demo_serves_the_unchanged_ui(self) -> None:
        self.assertEqual(self.page, wl.INDEX_HTML)
        start = self.demo_page.index("<script>")
        end = self.demo_page.index("</script>\n", start) + len("</script>\n")
        replay = self.demo_page[start:end]
        self.assertNotIn("</", replay.split("const RECORDED = ", 1)[1].split(";\n", 1)[0])
        self.assertEqual(self.demo_page.count(self.ui_tool.DEMO_BANNER), 1)
        self.assertIn('href="../impressum.html"', self.ui_tool.DEMO_BANNER)
        restored = self.demo_page.replace(replay, "", 1).replace(self.ui_tool.DEMO_BANNER, "", 1)
        restored = restored.replace(
            "<title>On My Plate – Demo</title>", "<title>On My Plate</title>"
        )
        restored = restored.replace('\n<meta name="robots" content="noindex">', "")
        self.assertEqual(restored, wl.INDEX_HTML)

    def test_every_api_path_of_the_ui_is_recorded(self) -> None:
        paths = self.ui_tool.api_paths(wl.INDEX_HTML)
        self.assertTrue({"/api/items", "/api/todos", "/api/local", "/api/refresh"} <= set(paths))
        expected = {f"{method} {path}" for path in paths for method in ("GET", "POST")} | {"*"}
        self.assertEqual(set(self.recorded), expected)
        self.assertEqual(self.recorded["GET /api/items"]["status"], 200)

    @unittest.skipUnless(shutil.which("node"), "needs Node.js to run the demo's script")
    def test_demo_answers_like_the_real_server(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            files = {"harness.js": NODE_HARNESS, "page.html": self.demo_page}
            files["scenario.json"] = json.dumps(DEMO_SCENARIO)
            for name, content in files.items():
                (Path(folder) / name).write_text(content, "utf-8")
            result = subprocess.run(
                [shutil.which("node"), *(str(Path(folder) / name) for name in files)],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
        demo = json.loads(result.stdout)

        real = []
        with self.ui_tool.demo_server(wl) as call:
            for method, path, body in DEMO_SCENARIO:
                response = call(
                    method, path, body if body is not None else {} if method == "POST" else None
                )
                response["type"] = response["headers"].get("Content-Type", "")
                real.append(response)

        self.assertEqual(len(demo), len(DEMO_SCENARIO))
        for step, demo_response, real_response in zip(DEMO_SCENARIO, demo, real, strict=True):
            with self.subTest(step=step[:2]):
                self.assertEqual(comparable(demo_response), comparable(real_response))


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #


@unittest.skipUnless(shutil.which("sh"), "needs a POSIX shell")
class CommitMessageHookTests(unittest.TestCase):
    """tools/hooks/commit-msg is the single rule for commits, used by git and by CI."""

    def check(self, message: str) -> int:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "MSG"
            path.write_text(message)
            hook = test_onmyplate.ROOT / "tools" / "hooks" / "commit-msg"
            result = subprocess.run(
                [shutil.which("sh"), str(hook), str(path)], capture_output=True, check=False
            )
            return result.returncode

    def test_conventional_messages_pass(self) -> None:
        for message in (
            "feat: add a setup screen",
            "fix(gitlab): count work items as issues\n\nWork items share the issue table.",
            "feat(api)!: rename the export endpoint",
            "docs: explain tokens",
            "# comment line from git\nchore(deps): bump eslint",
            "Merge branch 'main' into fix/x",
            "fixup! fix(jira): something",
        ):
            with self.subTest(message=message):
                self.assertEqual(self.check(message), 0)

    def test_other_messages_fail(self) -> None:
        for message in (
            "Fix bug",
            "feat:missing space",
            "feature: unknown type",
            "fix(Jira): upper-case scope",
            "fix: " + "x" * 100,
            "",
        ):
            with self.subTest(message=message):
                self.assertEqual(self.check(message), 1)


def run_cli(*args: str, data_dir: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("ONMYPLATE_")}
    command = [sys.executable, str(SCRIPT), *args]
    if data_dir is not None:
        command += ["--data-dir", str(data_dir)]
    return subprocess.run(command, capture_output=True, text=True, env=env, timeout=30, check=False)


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_help(self) -> None:
        result = run_cli("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("--demo", result.stdout)

    def test_licenses(self) -> None:
        result = run_cli("--licenses")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MIT License", result.stdout)
        self.assertIn("Third-party software", result.stdout)

    def test_config_writable_by_others_stops_the_start(self) -> None:
        if os.name != "posix":
            self.skipTest("file modes are POSIX")
        config = self.root / "onmyplate.config.json"
        config.write_text("{}")
        config.chmod(0o666)
        result = run_cli("--no-browser", data_dir=self.root)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Not starting", result.stderr)

    def test_first_run_writes_a_private_config(self) -> None:
        result = run_cli("--no-browser", data_dir=self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Config created", result.stdout)
        config = self.root / "onmyplate.config.json"
        self.assertEqual(json.loads(config.read_text()), wl.DEFAULT_CONFIG)
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)

    def test_export_and_import(self) -> None:
        source, target = self.root / "old", self.root / "new"
        source.mkdir()
        (source / "onmyplate.todos.md").write_text("- [ ] Carry over <!-- id:a1 -->\n")
        (source / "onmyplate.config.json").write_text('{"jira": {"url": "https://j.example"}}')
        archive = self.root / "backup.zip"

        result = run_cli("--export", str(archive), "--with-config", data_dir=source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Tokens stay in the keychain", result.stdout)

        result = run_cli("--import", str(archive), data_dir=target)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Tokens will be sent to", result.stdout)
        self.assertIn("Jira: https://j.example", result.stdout)
        self.assertIn("Carry over", (target / "onmyplate.todos.md").read_text())

    def test_demo_server_starts_and_signs_in(self) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        process = subprocess.Popen(
            [
                sys.executable,
                "-u",
                str(SCRIPT),
                "--demo",
                "gitlab",
                "--no-browser",
                "--port",
                str(port),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(process.wait, 10)
        self.addCleanup(process.kill)
        code = None
        for line in process.stdout:
            match = re.search(r"#code=([\w-]+)", line)
            if match:
                code = match.group(1)
                break
        process.stdout.close()
        self.assertIsNotNone(code)

        def call(method: str, path: str, body: Any = None, token: str = "") -> Any:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            headers = {"Content-Type": "application/json", wl.TOKEN_HEADER: token}
            conn.request(method, path, json.dumps(body) if body else None, headers)
            return json.loads(conn.getresponse().read())

        token = call("POST", "/api/login", {"code": code})["token"]
        snapshot = call("GET", "/api/items", token=token)
        self.assertTrue(snapshot["demo"])
        self.assertEqual(snapshot["enabled"], ["gitlab"])
        self.assertEqual({item["source"] for item in snapshot["items"]}, {"gitlab"})


if __name__ == "__main__":
    unittest.main()
