from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def emit(event: dict[str, object]) -> None:
    print(json.dumps(event), flush=True)


mode = sys.argv[1]
args = sys.argv[2:]
if mode == "fallback-probe" and "--model" in args:
    mode = "late-rate" if args[args.index("--model") + 1] == "gemini-4-argon-medium" else "rate"
if "models" in args:
    if mode == "models-fail":
        sys.exit(1)
    models = Path(__file__).resolve().parents[1] / "models.generated.json"
    for model, route in json.loads(models.read_text(encoding="utf-8")).items():
        if route.startswith("agy"):
            print(model + "\t" + model)
    sys.exit(0)
record_dir = Path(os.environ.get("FAKE_RECORD_DIR", str(Path(__file__).resolve().parents[1] / "logs")))
record_dir.mkdir(exist_ok=True, parents=True)
prompt = json.loads(sys.stdin.buffer.readline())
(record_dir / f"call-{os.getpid()}.json").write_text(json.dumps({
    "args": args, "cwd": os.getcwd(), "initial_files": list(os.listdir()), "prompt": prompt,
}), encoding="utf-8")
if mode in ("timeout", "slow"):
    child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(120)"],
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    (record_dir / "child.pid").write_text(str(child.pid))
    if mode == "timeout":
        time.sleep(120)
    time.sleep(0.3)
    child.terminate()
    child.wait()
if mode in ("rate", "late-rate", "auth", "unknown"):
    emit({"event": "init", "init": {"tools": ["registry-only"]}})
    if mode == "late-rate":
        emit({"event": "step_update", "step_update": {"step_type": "agent_response", "text_delta": "AGY_FIRST_TOKEN"}})
        time.sleep(0.2)
    detail = {"rate": "429 RESOURCE_EXHAUSTED agy-test-quota", "late-rate": "429 agy-late-quota",
              "auth": "not logged in OAuth authentication",
              "unknown": "unknown model"}[mode]
    emit({"event": "result", "result": {"status": "ERROR", "error": {"message": detail}}})
    sys.exit(1)
if mode == "crash":
    print("fake crash stderr marker", file=sys.stderr, flush=True)
    sys.exit(7)
if mode == "malformed":
    print("not json", flush=True)
    time.sleep(120)
if mode in ("tool", "permission"):
    emit({"event": "step_update", "step_update": {"step_type": mode}})
    time.sleep(120)
emit({"event": "init", "init": {"tools": ["registry-only"]}})
emit({"event": "step_update", "step_update": {"step_type": "agent_thinking", "text_delta": "SECRET REASONING"}})
for text in ("hello ", "world"):
    emit({"event": "step_update", "step_update": {"step_type": "agent_response", "text_delta": text}})
emit({"event": "result", "result": {"status": "SUCCESS", "response": "final hello world", "usage": {
    "input_tokens": 10, "cache_read_tokens": 3, "output_tokens": 7, "thinking_tokens": 2,
}}})
