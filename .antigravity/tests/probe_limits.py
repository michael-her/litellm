from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / "logs/limits"
QUESTION = "Reply with only the exact CHECKPOINT_ identifiers visible in the supplied text, in order; do not invent missing identifiers."


def text(size: int, korean: bool = False) -> str:
    unit = "가" if korean else "x"
    start, middle, end = "CHECKPOINT_START_K7Q9", "CHECKPOINT_MIDDLE_J4T8", "CHECKPOINT_END_P6V2"
    fixed = f"{QUESTION}\n{start}\n\n{middle}\n\n{end}\n{QUESTION}"
    count = max(0, (size - len(fixed.encode())) // len(unit.encode()))
    return f"{QUESTION}\n{start}\n{unit * (count // 2)}\n{middle}\n{unit * (count - count // 2)}\n{end}\n{QUESTION}"


def probe(label: str, contents: tuple[object, ...], extra: tuple[str, ...] = ()) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="probe-", dir=LOGS) as cwd:
        log = Path(cwd) / "agy.log"
        payload = "".join(json.dumps({"event": "user", "message": {"role": "user", "content": content}},
                                    ensure_ascii=False) + "\n" for content in contents)
        (LOGS / f"{label}.input.jsonl").write_text(payload, encoding="utf-8")
        result = subprocess.run([
            str(Path(os.environ["LOCALAPPDATA"]) / "agy/bin/agy.exe"), "--input-format", "stream-json",
            "--output-format", "stream-json", "--print-timeout", "0s", "--model", "gemini-3.8-flash-low",
            "--agent", "litellm-antigravity", "--disable-slash-commands", "--log-file", str(log), *extra,
        ], input=payload, capture_output=True, text=True, encoding="utf-8", timeout=150, cwd=cwd,
            creationflags=subprocess.CREATE_NO_WINDOW)
        (LOGS / f"{label}.stdout.jsonl").write_text(result.stdout, encoding="utf-8")
        (LOGS / f"{label}.stderr.log").write_text(result.stderr, encoding="utf-8")
        if log.exists():
            (LOGS / f"{label}.agy.log").write_bytes(log.read_bytes())
        print(label, "exit", result.returncode, "content-bytes", [len(json.dumps(c, ensure_ascii=False).encode())
                                                                 for c in contents], flush=True)
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("event") == "result":
                print(json.dumps(event["result"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    label, mode, size = sys.argv[1], sys.argv[2], int(sys.argv[3])
    value = text(size, korean=mode.endswith("ko"))
    content = [{"type": "text", "text": value}] if mode.startswith("parts") else value
    probe(label, (content,))
