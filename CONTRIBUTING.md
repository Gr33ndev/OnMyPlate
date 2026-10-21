# Contributing

Thanks for your interest! Bug reports, ideas and pull requests are welcome. Everyone taking part follows the [code of conduct](CODE_OF_CONDUCT.md).

## Ground rules

These keep On My Plate what it is:

- **One file, standard library only.** `onmyplate.py` must keep running with nothing but Python 3.11+. Development tools (ruff, ESLint) are fine; runtime dependencies are not.
- **Read-only.** On My Plate never changes anything on the servers it talks to. Features that need write access are out of scope.
- **Local and private.** No telemetry, no external requests besides the configured instances, no third-party scripts or fonts in the UI.
- **License notices.** If a dependency is ever added to what ships (the `dependencies` in `pyproject.toml` or in `package.json`), run `python3 tools/third_party_licenses.py`. It updates `THIRD_PARTY_LICENSES.md` and the notices shown in the app; CI fails until you do.
- **Security first.** New API endpoints, server fields that reach the UI, or changes to authentication need tests. See `tests/test_onmyplate.py`.

## Workflow

[AGENTS.md](AGENTS.md) describes the code layout, conventions and test helpers.

1. Fork, then create a branch (`fix/…`, `feat/…`, `docs/…`). Turn on the git hooks once:
   ```sh
   git config core.hooksPath tools/hooks
   ```
   They check the commit message, run ruff before each commit and the tests before each push.
2. Make your change. For UI work, use `python3 tools/ui.py extract` and edit `build/ui.html`, then run `python3 tools/ui.py embed`.
3. Run the checks:
   ```sh
   python3 -m unittest discover -s tests
   ruff check . && ruff format --check .
   bandit -q -c pyproject.toml onmyplate.py
   npm ci && npm run lint
   ```
4. Open a pull request with a title in [commit format](#commits-and-pull-request-titles) that describes what changes for the user. Add an entry under *Unreleased* in `CHANGELOG.md`. The pull request template has a short checklist.

## Commits and pull request titles

We use [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/). The `commit-msg` hook and CI check it, for every commit and every pull request title.

```
<type>(<scope>): <short description in imperative mood, English, lowercase>
```

Examples: `fix(gitlab): count work items as issues`, `feat(ui): undo for deleted tasks`, `docs: explain scoped Jira tokens`.

| Type | Used for |
|---|---|
| `feat` | new feature for users |
| `fix` | bug fix |
| `docs` | documentation only |
| `refactor` | restructuring without changed behavior |
| `perf` | faster or more efficient |
| `test` | tests only |
| `build` | packaging, dependencies |
| `ci` | GitHub Actions |
| `chore` | maintenance that fits no other category |
| `revert` | reverts a commit |

- **Scope** (optional, lowercase): for example `jira`, `gitlab`, `bitbucket`, `ui`, `server`, `cli`, `tasks`, `backup`, `security`, `demo`, `site`, `deps`, `release`.
- **Breaking changes** (for example a changed config option or file format): `!` after the type, as in `feat(cli)!: …`, and a `BREAKING CHANGE: …` paragraph in the body that says what users have to adjust.
- The subject has at most 100 characters. Explain the why in the body.
- Pull requests are squash-merged, so the **pull request title** becomes the commit message.
- No tokens, internal URLs or ticket contents in commits.

## Style

- **Python:** ruff with the rule set in `pyproject.toml`, formatted with `ruff format` (line length 100), with type hints.
- **JavaScript:** modern browser JS with no build step, checked by ESLint.
- **Comments** explain *why*, not *what*.
- **Messages and UI text** are in English and plain language. Error messages say what to do next.

## Testing against real servers

Please don't paste tokens, internal URLs or ticket content into issues. If a source behaves unexpectedly, describe it this way instead:

- Your server version, e.g. Jira Data Center 9.12 or GitLab 17.3.
- The notice On My Plate shows, with hostnames removed.
