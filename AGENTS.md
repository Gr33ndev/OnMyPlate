# AGENTS.md

Guidance for AI coding agents (and humans) working on this repository.

## What this is

**On My Plate** is a local, read-only dashboard that collects the Jira, GitLab and Bitbucket issues and pull requests a user is involved in. It is deliberately a **single Python file** (`onmyplate.py`) with **no runtime dependencies** (standard library only, Python 3.11+). It serves a web UI on `127.0.0.1` that is embedded in the same file as the `INDEX_HTML` string.

## Hard rules

These are product decisions, not style preferences. Do not break them, even if a task seems to ask for it — raise the conflict instead.

1. **Single file, standard library only.** Never add a runtime import that is not in the Python 3.11 standard library. Never split `onmyplate.py` into a package. Dev tools (ruff, bandit, ESLint) are fine.
2. **Read-only.** `HttpClient.request` only allows `GET` plus the Jira search `POST` paths in `READ_ONLY_POST_PATHS`. Never widen this; features that need write access are out of scope.
3. **No outbound traffic** other than the configured instances. No telemetry, CDNs, web fonts or third-party scripts in the UI. `test_ui_has_no_external_resources` in `tests/test_app.py` guards this.
4. **Python 3.11 compatible.** 3.11 is the oldest Python with security support at the first release. Don't use anything newer, such as `type` statements or PEP 695 generics (3.12). Raise the minimum only together with `requires-python` in `pyproject.toml`, the version check at the top of `onmyplate.py`, the CI matrix and the README.
5. **Security-relevant changes need tests**: new endpoints, new server fields reaching the UI (add them to `coerce_item` / `ITEM_TEXT_LIMITS`), auth, redirects, file handling.
6. **Tokens never touch the disk in plain text.** They come from the system keychain, `token_command` or the environment (`resolve_token`); `move_tokens_to_keychain` removes any `"token"` from the config file. Never add another place that writes or logs a token.
7. **Never commit personal data.** `onmyplate.config.json`, `onmyplate.local.json`, `onmyplate.todos.md`, `onmyplate.secret` and backups are in `.gitignore`. Never paste tokens or internal URLs into code, tests or docs; use `*.example` hosts.

## Layout

| Path | Purpose |
|---|---|
| `onmyplate.py` | Everything: data files, task file parser, backup, config, HTTP client, `JiraSource` / `GitLabSource` / `BitbucketSource`, demo data, `Store`, local HTTP server, CLI, and the embedded UI (`INDEX_HTML`). Sections are separated by `# ---` banner comments. |
| `tests/test_onmyplate.py` | Original suite: fake Jira/GitLab/Bitbucket on one port, security regressions, backup. Also loads the module as `wl` for the other test files. |
| `tests/test_sources.py` | Configurable fake server (`Routes`) and detailed source tests: paging, warnings, normalisation, HTTP client errors. |
| `tests/test_app.py` | Helpers, task parsing, files/backup/migration, `Store`, local API, and CLI tests (subprocess, including a demo server smoke test). |
| `tools/ui.py` | `extract` the embedded UI to `build/ui.html` + `build/app.js`, `embed` it back, `check` they match, `demo` records the real server in demo mode into the static browser demo `docs/demo/index.html` (git-ignored, built by the Pages workflow). |
| `tools/screenshots.py` | Retakes `docs/*.png` from the real UI with headless Chrome (demo data, no demo banner). Run it after visible UI changes. |
| `tools/third_party_licenses.py` | Generates `THIRD_PARTY_LICENSES.md` and the `LICENSES` block in `onmyplate.py` (between `# BEGIN/END GENERATED LICENSES`) from the runtime dependencies; `--check` runs in CI. Never edit that block by hand. |
| `LICENSE`, `NOTICE`, `THIRD_PARTY_LICENSES.md` | Own MIT license, trademark and format notes, generated third-party list. Shown in the app under ? > About & licenses (`/api/licenses`) and by `--licenses`. |
| `docs/` | Screenshots used by the README and the GitHub Pages site (`docs/index.html`, `docs/impressum.html`, shared `docs/style.css`, plus the generated `docs/demo/`). Every page links the Impressum in its footer. |
| `.github/workflows/` | `ci.yml` (tests on 3.11–3.14, ruff, bandit, ESLint) and `pages.yml` (deploys `docs/`). |

## Commands

```sh
python3 -m unittest discover -s tests          # all tests, no network needed (~5 s)
python3 onmyplate.py --demo --no-browser       # run with sample data; prints a sign-in link
uvx ruff check . && uvx ruff format --check .  # lint + format (or plain `ruff` if installed)
uvx bandit -q -c pyproject.toml onmyplate.py   # security lint
npm ci && npm run lint                         # ESLint on the embedded UI
python3 tools/third_party_licenses.py --check  # license notices up to date (Python 3.11+)
```

Run all of them before you call a change done. CI runs the same set.

## Working on the UI

The UI is a string inside `onmyplate.py`. Don't edit it in place for anything non-trivial:

```sh
python3 tools/ui.py extract   # writes build/ui.html and build/app.js
# edit build/ui.html
python3 tools/ui.py embed     # writes it back into onmyplate.py
```

The live demo on GitHub Pages is recorded from the real code: `tools/ui.py demo` starts `OnMyPlateServer` in demo mode, records the page and the GET and POST answer for every `/api/...` path the UI's script mentions, and `DEMO_REPLAY` plays them back in the browser. The replay knows no endpoint by name, so API changes need no demo changes. The only visible addition is `DEMO_BANNER` (demo notice with links to the website and the Impressum). `BrowserDemoTests` in `tests/test_app.py` runs the same requests against the real server and the replay (in Node) and fails on any difference; keep it green. Preview with `python3 tools/ui.py demo` and `python3 -m http.server -d docs`.

`build/ui.html` must not contain `"""`. The CSP is derived from a hash of the inline `<script>`, so it updates automatically — but there must be exactly one inline script and no inline event handlers (`onclick=` etc.). Verify visually with `--demo` in light and dark mode.

## Conventions

- **Commits:** Conventional Commits, `<type>(<scope>): <description>`, checked by `tools/hooks/commit-msg` locally (`git config core.hooksPath tools/hooks`) and in CI. Types and scopes are listed in CONTRIBUTING.md.

- **Style:** ruff with the rule set in `pyproject.toml`, line length 100, type hints everywhere. Comments explain *why*.
- **Messages:** English, plain language, and say what to do next (see `CONFIG_HELP`, `SourceError` texts).
- **Errors from a source** are raised as `SourceError` (shown to the user) or appended to `warnings` for partial failures. One broken source must never stop the others (`_fetch_safely`).
- **Remote data is untrusted.** Pass it through `sanitize`, validate keys/paths with the `RE_*` patterns before building URLs, and keep links on the configured instance (`_own_url`, `_web_url`).
- **Files** holding personal data are written with `write_private` (atomic, mode 0600).
- **Relations** an item can have are listed in `RELATIONS`; adding one means updating the UI and `prune_relations` semantics.
- **Versioning:** bump `VERSION` in `onmyplate.py` and `version` in `pyproject.toml` together, and add a `CHANGELOG.md` entry (a test checks they agree). Add unreleased changes under *Unreleased*.

## Testing tips

- Source tests use local fake servers with `"allow_insecure_http": True`; never hit real hosts.
- In `tests/test_sources.py`, register replies per test with `Routes.routes[(method, path)] = ok(body)` or a function `(query, body) -> Reply`. Inspect `Routes.log` for what was requested. URL-encoded path segments stay encoded (`platform%2Finfra`).
- `wl.set_data_dir(tmp)` switches the global data folder; use the `DataDirCase` base class in `tests/test_app.py` for isolation.
- Bitbucket mention results are cached in `_MENTION_CACHE`; clear it between tests.
