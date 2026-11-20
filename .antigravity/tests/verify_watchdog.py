from __future__ import annotations

import argparse
import json
import socket
import time

import httpx
import psutil

from production_lifecycle import ROOT, production_script
from verify_live import curl, env_key


def listening() -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", 14606)) == 0


def recorded(name: str) -> int:
    return int((ROOT / name).read_text(encoding="utf-8-sig").strip())


def wait_ready() -> None:
    key = env_key(ROOT / "antigravity.local.env", "ANTIGRAVITY_PROXY_KEY")
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            response = httpx.get("http://127.0.0.1:14606/v1/models",
                                 headers={"Authorization": "Bearer " + key}, timeout=1)
            if response.status_code == 200 and (ROOT / "server.pid").exists():
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError("Production failed to become ready")


def main() -> None:
    supervisor = recorded("supervisor.pid")
    server = recorded("server.pid")
    try:
        print(f"Kill server only PID={server}; preserve supervisor={supervisor}", flush=True)
        psutil.Process(server).kill()
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if (ROOT / "server.pid").exists() and recorded("server.pid") != server:
                break
            time.sleep(0.5)
        wait_ready()
        assert recorded("supervisor.pid") == supervisor
        assert recorded("server.pid") != server
        curl("watchdog-recovered-models", expected_status=200)
        print(f"Recovered server={recorded('server.pid')} same supervisor={supervisor}", flush=True)
        production_script("start-server.ps1")
        assert recorded("supervisor.pid") == supervisor
        old_server = recorded("server.pid")
        production_script("stop-server.ps1")
        assert not psutil.pid_exists(supervisor)
        assert not psutil.pid_exists(old_server)
        started = time.monotonic()
        deadline = started + 70
        while time.monotonic() < deadline:
            assert not listening(), "Watchdog fought intentional stop"
            time.sleep(0.5)
        print(f"Intentional stop: both PIDs gone; 14606 stayed down {time.monotonic()-started:.1f}s", flush=True)
    finally:
        production_script("start-server.ps1")
        wait_ready()
    curl("watchdog-restored-models", expected_status=200)
    ids = {name: recorded(name) for name in ("supervisor.pid", "server.pid")}
    for name, pid in ids.items():
        process = psutil.Process(pid)
        chain = [(item.pid, item.name(), item.ppid()) for item in (process, *process.parents())]
        print(f"{name} ancestry={chain}", flush=True)
        assert any(item[1].lower() == "explorer.exe" for item in chain)
        assert not any("codex" in item[1].lower() for item in chain)
    process = psutil.Process(ids["server.pid"])
    assert not {"ANTIGRAVITY_CLI", "ANTIGRAVITY_CLI_ARGS"} & process.environ().keys()
    assert str(ROOT / "config.generated.yaml").lower() in " ".join(process.cmdline()).lower()
    (ROOT / "logs/watchdog-production.json").write_text(json.dumps(ids), encoding="utf-8")
    print(f"Production restored detached; fake overrides absent; PIDs={ids}", flush=True)


def verify_survival() -> None:
    saved = json.loads((ROOT / "logs/watchdog-production.json").read_text(encoding="utf-8"))
    for name, pid in saved.items():
        assert recorded(name) == pid
        process = psutil.Process(pid)
        chain = [(item.pid, item.name(), item.ppid()) for item in (process, *process.parents())]
        print(f"Unchanged {name} ancestry={chain}", flush=True)
        assert any(item[1].lower() == "explorer.exe" for item in chain)
        assert not any("codex" in item[1].lower() for item in chain)
    server = psutil.Process(saved["server.pid"])
    assert not {"ANTIGRAVITY_CLI", "ANTIGRAVITY_CLI_ARGS"} & server.environ().keys()
    print("Server command=" + " ".join(server.cmdline()), flush=True)
    assert str(ROOT / "config.generated.yaml").lower() in " ".join(server.cmdline()).lower()
    print("Production environment: fake CLI overrides absent", flush=True)
    curl("watchdog-final-models", expected_status=200)
    curl("watchdog-final-flash", "gemini-3.8-flash-low", expected_status=200)
    curl("watchdog-final-vertex-models", vertex=True, expected_status=200)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--survival", action="store_true", help="Read-only PID checks plus one live Flash Low completion")
    args = parser.parse_args()
    if args.survival:
        verify_survival()
    else:
        main()
