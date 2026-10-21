#!/usr/bin/env python3
"""Generate the third-party license notices from the runtime dependencies.

Covers everything On My Plate ships:
  - Python packages required at runtime (dependency tree of [project].dependencies in
    pyproject.toml, without extras)
  - npm packages bundled into the web UI (production dependency tree of package.json)

Both are empty today: On My Plate uses only the Python standard library, and npm only
provides development tools. This script keeps it that way visibly and picks up anything
that is added later.

Writes THIRD_PARTY_LICENSES.md and the LICENSES block in onmyplate.py, which the app
shows under ? > About & licenses and prints with --licenses.

Usage (from the repository root, after `npm ci`):
    python3 tools/third_party_licenses.py          # write the files
    python3 tools/third_party_licenses.py --check  # fail if they are out of date
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tomllib
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "THIRD_PARTY_LICENSES.md"
SCRIPT = ROOT / "onmyplate.py"
BLOCK = re.compile(
    r"(# BEGIN GENERATED LICENSES[^\n]*\n)(.*?)(# END GENERATED LICENSES\n)", re.DOTALL
)
LICENSE_NAMES = ("LICENSE", "LICENCE", "COPYING", "NOTICE", "AUTHORS")
NONE_SHIPPED = (
    "On My Plate uses only the Python standard library and ships no third-party code, "
    "fonts or images. Python itself is installed separately and licensed under the PSF "
    "License Agreement (https://docs.python.org/3/license.html)."
)

Package = dict[str, Any]


def python_packages() -> list[Package]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]
    seen: dict[str, Any] = {}

    def walk(requirement: str) -> None:
        name = re.split(r"[\s<>=!~;\[(]", requirement.strip(), maxsplit=1)[0]
        if ";" in requirement and "extra" in requirement.split(";", 1)[1]:
            return
        try:
            dist = distribution(name)
        except PackageNotFoundError:
            sys.exit(f"{name} is a runtime dependency but not installed: install it first")
        key = dist.metadata["Name"].lower()
        if key in seen:
            return
        seen[key] = dist
        for raw in dist.requires or []:
            walk(raw)

    for requirement in project.get("dependencies", []):
        walk(requirement)
    packages = []
    for dist in seen.values():
        meta = dist.metadata
        license_name = meta.get("License-Expression") or ""
        if not license_name:
            classifiers = [
                c.split("::")[-1].strip()
                for c in meta.get_all("Classifier") or []
                if c.startswith("License")
            ]
            license_name = ", ".join(classifiers) or (meta.get("License") or "").split("\n")[0]
        texts = [
            dist.locate_file(file).read_text(encoding="utf-8", errors="replace").strip()
            for file in sorted(dist.files or [], key=str)
            if any(part in Path(str(file)).name.upper() for part in LICENSE_NAMES)
        ]
        packages.append(
            {
                "name": meta["Name"],
                "version": dist.version,
                "license": license_name,
                "url": meta.get("Home-page") or project_url(meta),
                "texts": texts,
            }
        )
    return sorted(packages, key=lambda p: p["name"].lower())


def project_url(meta: Any) -> str:
    for entry in meta.get_all("Project-URL") or []:
        label, _, url = entry.partition(",")
        if label.strip().lower() in ("homepage", "source", "repository", "source code"):
            return url.strip()
    return ""


def npm_packages() -> list[Package]:
    npm = shutil.which("npm")
    if not npm:
        sys.exit("npm is needed to list the packages bundled into the web UI")
    listing = subprocess.run(
        [npm, "ls", "--omit=dev", "--all", "--json", "--long"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    found: dict[str, Package] = {}

    def walk(deps: dict[str, Any] | None) -> None:
        for name, info in (deps or {}).items():
            path = Path(info.get("path") or ROOT / "node_modules" / name)
            key = f"{name}@{info.get('version')}"
            if key not in found and path.is_dir():
                manifest = json.loads((path / "package.json").read_text("utf-8"))
                texts = [
                    f.read_text(encoding="utf-8", errors="replace").strip()
                    for f in sorted(path.iterdir())
                    if f.is_file() and any(part in f.name.upper() for part in LICENSE_NAMES)
                ]
                repo = manifest.get("repository")
                url = repo.get("url", "") if isinstance(repo, dict) else (repo or "")
                found[key] = {
                    "name": name,
                    "version": manifest.get("version", ""),
                    "license": manifest.get("license", ""),
                    "url": url or manifest.get("homepage", ""),
                    "texts": texts,
                }
            walk(info.get("dependencies"))

    walk(json.loads(listing.stdout or "{}").get("dependencies"))
    return sorted(found.values(), key=lambda p: p["name"].lower())


def render_markdown(groups: list[dict[str, Any]]) -> str:
    out = [
        "# Third-party licenses",
        "",
        "On My Plate itself is licensed under the MIT License (see LICENSE).",
        "",
        "This file is generated by `tools/third_party_licenses.py`. Do not edit by hand.",
        "",
    ]
    if not any(group["packages"] for group in groups):
        return "\n".join([*out, NONE_SHIPPED, ""])
    out += [
        "It ships with the following third-party software. Their license texts follow below.",
        "",
    ]
    for group in groups:
        out += [f"## {group['title']}", ""]
        if not group["packages"]:
            out += ["None.", ""]
            continue
        out += ["| Package | Version | License |", "|---|---|---|"]
        out += [
            f"| {p['name']} | {p['version']} | {p['license'] or 'see text'} |"
            for p in group["packages"]
        ]
        out.append("")
    out += ["---", ""]
    for group in groups:
        for p in group["packages"]:
            out += [f"## {p['name']} {p['version']}", "", f"License: {p['license'] or 'see text'}"]
            out += [f"Source: {p['url']}"] if p["url"] else []
            out.append("")
            for text in p["texts"] or ["(No license file shipped with the package.)"]:
                out += ["```", text, "```", ""]
    return "\n".join(out).rstrip() + "\n"


def render_block(groups: list[dict[str, Any]]) -> str:
    data = {
        "self": {
            "name": "On My Plate",
            "license": "MIT",
            "text": (ROOT / "LICENSE").read_text("utf-8").strip(),
        },
        "note": "" if any(group["packages"] for group in groups) else NONE_SHIPPED,
        "groups": groups,
    }
    encoded = json.dumps(data, indent=1)
    if '"""' in encoded:  # cannot happen with json.dumps, which escapes every quote
        sys.exit("license data must not contain three quotes in a row")
    return f'LICENSES: dict[str, Any] = json.loads(\n    r"""\n{encoded}\n"""\n)\n'


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--check", action="store_true", help="exit 1 if the files are outdated")
    args = parser.parse_args()
    groups = [
        {"title": "Python packages", "packages": python_packages()},
        {"title": "Web UI", "packages": npm_packages()},
    ]
    script = SCRIPT.read_text("utf-8")
    if not BLOCK.search(script):
        sys.exit("onmyplate.py has no '# BEGIN GENERATED LICENSES' block")
    new_script = BLOCK.sub(lambda m: m.group(1) + render_block(groups) + m.group(3), script)
    outputs = {OUTPUT: render_markdown(groups), SCRIPT: new_script}
    if args.check:
        stale = [p for p, text in outputs.items() if not p.exists() or p.read_text("utf-8") != text]
        if stale:
            names = ", ".join(str(p.relative_to(ROOT)) for p in stale)
            sys.exit(f"out of date: {names} - run tools/third_party_licenses.py")
        print("third-party licenses up to date")
        return
    for path, text in outputs.items():
        path.write_text(text, "utf-8")
        print(f"wrote {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
