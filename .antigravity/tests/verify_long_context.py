from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from antigravity_llm import render
from verify_live import env_key


def main() -> None:
    markers = ("CHECKPOINT_START_H8V2", "CHECKPOINT_MIDDLE_C4R9", "CHECKPOINT_END_T7N3")
    padding = "배경 설명입니다. " * 250 + "x" * 6000
    messages = [
        {"role": "system", "content": "Echo every CHECKPOINT_ identifier literally present in the supplied "
         "conversation, in order. Also output OMITTED=N from the proxy notice, or OMITTED=0 if no notice. "
         "Do not infer missing identifiers. Do not repeat padding."},
        {"role": "user", "content": markers[0]},
        {"role": "assistant", "content": "Opening acknowledged."},
        *[{"role": "user" if i % 2 == 0 else "assistant", "content": padding + ("\n" + markers[1] if i == 10 else "")}
          for i in range(20)],
        {"role": "user", "content": markers[2] + "\nEcho the visible CHECKPOINT_ identifiers and OMITTED=N only."},
    ]
    full = render(messages, byte_limit=10**9)
    assert len(full.encode("utf-8")) > 220_000
    forwarded = render(messages)
    assert all(marker in forwarded for marker in markers)
    notice = json.loads(forwarded.splitlines()[1])["content"]
    match = re.search(r"(\d+) earlier messages were omitted", notice)
    assert match
    omitted = int(match[1])
    payload = ROOT / "logs/long-context-live.request.json"
    payload.write_text(json.dumps({"model": "gemini-3.8-flash-low", "messages": messages, "stream": False},
                                  ensure_ascii=False), encoding="utf-8")
    body_path = ROOT / "logs/long-context-live.body"
    header_path = ROOT / "logs/long-context-live.headers"
    key = env_key(ROOT / "antigravity.local.env", "ANTIGRAVITY_PROXY_KEY")
    result = subprocess.run([
        str(Path(os.environ["SYSTEMROOT"]) / "System32/curl.exe"), "-sS", "--max-time", "120",
        "-D", str(header_path), "-o", str(body_path), "-w", "%{http_code}",
        "http://127.0.0.1:14606/v1/chat/completions", "-H", "Authorization: Bearer " + key,
        "-H", "Content-Type: application/json", "--data-binary", "@" + str(payload),
    ], capture_output=True, text=True, timeout=130)
    print(f"curl exit={result.returncode} HTTP={result.stdout}", flush=True)
    print(f"Original transcript={len(full.encode('utf-8'))} bytes; forwarded={len(forwarded.encode('utf-8'))} "
          f"bytes; omitted={omitted} whole messages", flush=True)
    assert result.returncode == 0 and result.stdout == "200", result.stderr
    response = json.loads(body_path.read_text(encoding="utf-8"))
    content = response["choices"][0]["message"]["content"]
    print(json.dumps({"response": content, "usage": response.get("usage")}, ensure_ascii=True), flush=True)
    assert all(marker in content for marker in markers), content
    assert f"OMITTED={omitted}" in content, content
    print("Model saw START, MIDDLE, END; explicit omission count matched", flush=True)


if __name__ == "__main__":
    main()
