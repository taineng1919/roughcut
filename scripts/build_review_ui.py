"""Developer-only review UI build; runtime never invokes Node or npm."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REVIEW_UI = ROOT / "review-ui"


def main() -> None:
    subprocess.run(["npm", "ci"], cwd=REVIEW_UI, check=True)
    subprocess.run(["npm", "test"], cwd=REVIEW_UI, check=True)
    subprocess.run(["npm", "run", "build"], cwd=REVIEW_UI, check=True)


if __name__ == "__main__":
    main()
