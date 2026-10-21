#!/usr/bin/env python3
"""Take the README and website screenshots from the real UI with headless Chrome.

    python3 tools/screenshots.py            # writes docs/*.png
    CHROME=/path/to/chrome python3 tools/screenshots.py

The page is the browser demo from tools/ui.py (the real UI, answered from responses
recorded from the real server), without the demo banner and with two starred items.
Screenshots are 1440x860 CSS pixels at 2x, the size the README and website expect.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ui

DOCS = ui.ROOT / "docs"
SHOTS = {
    "dashboard.png": ("dash", "light"),
    "dashboard-dark.png": ("dash", "dark"),
    "pull-requests.png": ("prs", "light"),
}
CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
)


def find_chrome() -> str:
    for candidate in (os.environ.get("CHROME"), *CHROME_CANDIDATES):
        if candidate and (Path(candidate).exists() or shutil.which(candidate)):
            return shutil.which(candidate) or candidate
    sys.exit("Chrome or Chromium not found: set CHROME=/path/to/chrome")


def page_for(view: str, theme: str, page: str, recorded: dict, recorded_at: int) -> str:
    html = ui.build_demo_page(page, recorded, recorded_at).replace(ui.DEMO_BANNER, "", 1)
    items = json.loads(recorded["GET /api/items"]["body"])["items"]
    pins = [i["id"] for i in items if i["key"] in ("infra-live!635", "infra-images#40")]
    key = re.search(r'const KEY = "([^"]+)"', html).group(1)
    setup = f"""<script>
try {{
  localStorage.setItem("onmyplate.ui", {json.dumps(json.dumps({"view": view}))});
  sessionStorage.setItem({json.dumps(key + "/api/local")}, {json.dumps(json.dumps({"pins": pins, "notes": {}}))});
}} catch {{}}
document.documentElement.dataset.theme = {json.dumps(theme)};
</script>
<style>
#demo {{ display: none !important; }}
/* The empty date field shows the format of the machine's locale (tt.mm.jjjj, mm/dd/yyyy …). */
#todoDue::-webkit-datetime-edit {{ color: transparent; }}
</style>
"""
    return html.replace("<script>", setup + "<script>", 1)


def main() -> None:
    chrome = find_chrome()
    recorded_at = int(time.time() * 1000)
    page, recorded = ui.record_demo(ui.load_module())
    with tempfile.TemporaryDirectory() as folder:
        for name, (view, theme) in SHOTS.items():
            html = Path(folder) / f"{name}.html"
            html.write_text(page_for(view, theme, page, recorded, recorded_at), "utf-8")
            profile = Path(folder) / f"profile-{name}"
            subprocess.run(
                [
                    chrome,
                    "--headless=new",
                    "--disable-gpu",
                    "--hide-scrollbars",
                    "--lang=en-US",
                    f"--user-data-dir={profile}",
                    "--window-size=1440,860",
                    "--force-device-scale-factor=2",
                    "--virtual-time-budget=4000",
                    f"--screenshot={DOCS / name}",
                    html.as_uri(),
                ],
                check=True,
                capture_output=True,
                timeout=120,
                env={**os.environ, "LANG": "en_US.UTF-8", "LANGUAGE": "en_US"},
            )
            print(f"Wrote {DOCS / name}")


if __name__ == "__main__":
    main()
