from __future__ import annotations

import os
import subprocess
from pathlib import Path

from windows.w4_support import CORE_ROOT, child_environment
from windows.w5_review_process import ReviewCliChild, assert_port_closed
from windows.w5_review_support import seed_review_project

REVIEW_UI_ROOT = CORE_ROOT.parent / "review-ui"


def test_w5_production_browser_smoke_uses_cli_review_server(tmp_path: Path) -> None:
    project = seed_review_project(tmp_path / "中文 browser project with spaces", "roughcut_review")
    child = ReviewCliChild.start(project)
    try:
        npm = "npm.cmd" if os.name == "nt" else "npm"
        completed = subprocess.run(
            [npm, "run", "test:production-review"],
            cwd=REVIEW_UI_ROOT,
            env=child_environment({"W5_REVIEW_URL": child.url}),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        assert completed.returncode == 0, (
            "production Review browser smoke failed\n"
            f"stdout={completed.stdout}\n"
            f"stderr={completed.stderr}"
        )
    finally:
        child.stop()
    assert_port_closed(child.port)
