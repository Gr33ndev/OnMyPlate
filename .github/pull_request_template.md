<!-- Title: Conventional Commits, e.g. "fix(gitlab): count work items as issues". It becomes the commit message. -->

**What changes for the user?**

**How did you test it?**

- [ ] `python3 -m unittest discover -s tests`
- [ ] `ruff check . && ruff format --check .`
- [ ] `bandit -q -c pyproject.toml onmyplate.py`
- [ ] `npm run lint` (if the UI changed; edit via `tools/ui.py extract` / `embed`)
- [ ] Entry under *Unreleased* in `CHANGELOG.md`

**Ground rules** (see [CONTRIBUTING.md](../CONTRIBUTING.md)):

- [ ] Still one file with the standard library only, Python 3.11+
- [ ] Still read-only towards Jira, GitLab and Bitbucket
- [ ] No tokens, internal URLs or ticket contents in code, tests, screenshots or this description

<!-- Security problems: please do not open a pull request, use private reporting (see SECURITY.md). -->
