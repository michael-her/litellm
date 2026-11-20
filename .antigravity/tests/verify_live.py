from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def env_key(path: Path, name: str) -> str:
    return next(line.partition("=")[2].strip().strip("\"'") for line in path.read_text(encoding="utf-8-sig").splitlines()
                if line.startswith(name + "="))


def curl(label: str, model: str = "", stream: bool = False, wrong_key: bool = False,
         vertex: bool = False, expected_status: int | None = None,
         proxy_port: int = 14606, timeout_s: float = 240) -> dict[str, object] | None:
    key = env_key(ROOT.parent / ".vertex/vertex.local.env", "VERTEX_PROXY_KEY") if vertex else env_key(
        ROOT / "antigravity.local.env", "ANTIGRAVITY_PROXY_KEY")
    port = 14604 if vertex else proxy_port
    url = f"http://127.0.0.1:{port}/v1/" + ("chat/completions" if model else "models")
    args = [str(Path(os.environ["SYSTEMROOT"]) / "System32/curl.exe"), "-sS", "--max-time", str(timeout_s),
            "-D", str(ROOT / f"logs/{label}.headers"), "-o", str(ROOT / f"logs/{label}.body"),
            "-w", "%{http_code}", url, "-H", "Authorization: Bearer " + ("wrong-test-key" if wrong_key else key)]
    if model:
        payload = ROOT / f"logs/{label}.request.json"
        payload.write_text(json.dumps({"model": model, "messages": [{"role": "user", "content": "Reply with OK."}],
                                       "stream": stream, **({"stream_options": {"include_usage": True}} if stream else {})}),
                           encoding="utf-8")
        args.extend(["-H", "Content-Type: application/json", "--data-binary", "@" + str(payload)])
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout_s + 10)
    print(f"{label}: curl exit={result.returncode} HTTP={result.stdout}; {'POST' if model else 'GET'} {url}; model={model}",
          flush=True)
    if result.returncode != 0:
        raise RuntimeError(f"curl failed: {result.stderr}")
    if expected_status is not None:
        assert result.stdout == str(expected_status), f"Expected HTTP {expected_status}, got {result.stdout}"
    body = (ROOT / f"logs/{label}.body").read_text(encoding="utf-8")
    if stream:
        if not body.startswith("data:"):
            print(body[:4000], flush=True)
        for line in body.splitlines():
            if line.startswith("data:"):
                print(line[:700], flush=True)
        return None
    parsed = json.loads(body)
    if not model and "data" in parsed:
        names = sorted(entry["id"] for entry in parsed["data"])
        print(f"models={len(names)} " + json.dumps(names), flush=True)
    elif "choices" in parsed:
        print(json.dumps({key: parsed.get(key) for key in ("model", "choices", "usage")}, ensure_ascii=False), flush=True)
    else:
        print(json.dumps(parsed, ensure_ascii=False)[:4000], flush=True)
    return parsed


if __name__ == "__main__":
    if len(sys.argv) > 1:
        curl(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "")
    else:
        curl("models", expected_status=200)
        curl("nonstream", "gemini-3.8-flash-low", expected_status=200)
        curl("stream", "gemini-3.8-flash-low", stream=True, expected_status=200)
        curl("hydrogen", "gemini-4-hydrogen-medium", expected_status=200)
        curl("wrong-auth", wrong_key=True, expected_status=401)
        curl("vertex-models", vertex=True, expected_status=200)
