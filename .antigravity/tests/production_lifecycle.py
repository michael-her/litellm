from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def production_script(name: str) -> None:
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    subprocess.run([str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-File", str(ROOT / name)], check=True, timeout=120)


def with_production_restore(
    check: Callable[[], None], port: int, run_script: Callable[[str], None] = production_script,
) -> None:
    if port != 14606:
        check()
        return
    try:
        run_script("stop-server.ps1")
        check()
    finally:
        run_script("start-server.ps1")
