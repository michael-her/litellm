from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path

import pytest

from production_lifecycle import ROOT, with_production_restore


def test_failed_fixture_restores_detached_production() -> None:
    calls: list[str] = []

    def fail() -> None:
        calls.append("fixture")
        raise RuntimeError("fixture failed")

    with pytest.raises(RuntimeError, match="fixture failed"):
        with_production_restore(fail, 14606, calls.append)
    assert calls == ["stop-server.ps1", "fixture", "start-server.ps1"]


def test_failed_stop_still_attempts_detached_restore() -> None:
    calls: list[str] = []

    def script(name: str) -> None:
        calls.append(name)
        if name == "stop-server.ps1":
            raise RuntimeError("stop failed")

    with pytest.raises(RuntimeError, match="stop failed"):
        with_production_restore(lambda: calls.append("fixture"), 14606, script)
    assert calls == ["stop-server.ps1", "start-server.ps1"]


def test_isolated_fixture_never_stops_production() -> None:
    calls: list[str] = []
    with_production_restore(lambda: calls.append("fixture"), 19000, calls.append)
    assert calls == ["fixture"]


def test_foreign_port_is_rejected_without_stopping_owner() -> None:
    with socket.socket() as owner:
        owner.bind(("127.0.0.1", 0))
        owner.listen()
        port = owner.getsockname()[1]
        powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        result = subprocess.run(
            [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(ROOT / "start-server.ps1")],
            env={**os.environ, "ANTIGRAVITY_PROXY_PORT": str(port)},
            capture_output=True, text=True, timeout=20,
        )
        assert result.returncode != 0
        assert f"Port {port} is occupied by another process" in result.stderr
        assert f"PID {os.getpid()}" in result.stderr
        assert owner.getsockname()[1] == port
