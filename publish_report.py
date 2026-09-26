"""
Copy morning_report.html → index.html and push to GitHub Pages.
Phone link: https://jassmithx7-dev.github.io/PartsPicker/
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
REPORT = BASE / "morning_report.html"
INDEX = BASE / "index.html"
HISTORY = BASE / "price_history.json"


def run(cmd: list[str]) -> None:
    print(">", " ".join(cmd))
    subprocess.run(cmd, cwd=str(BASE), check=True)


def main() -> int:
    if not REPORT.exists():
        print("No morning_report.html — run a scrape first (run_now.bat).")
        return 1

    INDEX.write_bytes(REPORT.read_bytes())
    print(f"Wrote {INDEX.name} ({INDEX.stat().st_size:,} bytes)")

    run(["git", "add", "morning_report.html", "index.html"])
    if HISTORY.exists():
        run(["git", "add", "price_history.json"])

    # Only commit if something changed
    status = subprocess.run(
        ["git", "status", "--porcelain", "--", "morning_report.html", "index.html", "price_history.json"],
        cwd=str(BASE),
        capture_output=True,
        text=True,
        check=True,
    )
    if not status.stdout.strip():
        print("Nothing new to publish.")
        print("Phone link: https://jassmithx7-dev.github.io/PartsPicker/")
        return 0

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    run(["git", "commit", "-m", f"Publish morning report ({stamp})"])
    run(["git", "push", "origin", "HEAD"])
    print()
    print("Published.")
    print("Phone link: https://jassmithx7-dev.github.io/PartsPicker/")
    print("(GitHub Pages can take 1–2 minutes to update.)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as e:
        print(f"Publish failed: {e}")
        raise SystemExit(1)
