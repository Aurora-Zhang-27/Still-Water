"""macOS notifications (after the scheduled jobs run). Shown on this Mac only, never sent anywhere else."""

from __future__ import annotations

import json
import subprocess
import sys


def notify(title: str, message: str) -> bool:
    if sys.platform != "darwin":
        return False
    # json.dumps escaping is compatible with AppleScript strings
    script = f"display notification {json.dumps(message, ensure_ascii=False)} with title {json.dumps(title, ensure_ascii=False)}"
    try:
        result = subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0
