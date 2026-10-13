import json
import os
import sys
import time
from pathlib import Path

mode = sys.argv[1]
args = sys.argv[2:]
record = Path(os.environ["FAKE_RECORD_DIR"])
(record / "child.pid").write_text(str(os.getpid()))


def option(name: str) -> str | None:
    return args[args.index(name) + 1] if name in args else None


resume = option("--resume")
(record / "call.json").write_text(
    json.dumps(
        {
            "args": args,
            "cwd": os.getcwd(),
            "env": dict(os.environ),
            "stdin": sys.stdin.buffer.read().decode("utf-8"),
            "system": Path(option("--system-prompt-file") or "").read_bytes().decode("utf-8"),
            "session": [json.loads(line) for line in Path(resume).read_text(encoding="utf-8").splitlines()]
            if resume
            else None,
        },
        ensure_ascii=False,
    ),
    encoding="utf-8",
)


def emit(event: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def delta(text: str, kind: str = "text_delta", parent: str | None = None) -> None:
    key = "text" if kind == "text_delta" else "thinking"
    emit(
        {
            "type": "stream_event",
            "parent_tool_use_id": parent,
            "event": {"type": "content_block_delta", "index": 0, "delta": {"type": kind, key: text}},
        }
    )


emit({"type": "system", "subtype": "init", "tools": []})
sys.stdout.write("not json\n")
if mode == "eof":
    sys.exit(3)
failures = {
    "auth": ("authentication_failed", None, "Not logged in"),
    "usage-limit": ("rate_limit", None, "You've hit your usage limit"),
    "overloaded": ("server_error", 529, "API Error: 529 Overloaded"),
}
if mode in failures:
    error, status, text = failures[mode]
    emit({"type": "assistant", "error": error, "message": {}})
    emit({"type": "result", "is_error": True, "api_error_status": status, "result": text})
    sys.exit(1)
emit(
    {
        "type": "stream_event",
        "event": {
            "type": "message_start",
            "message": {"usage": {"input_tokens": 2, "cache_creation_input_tokens": 4, "cache_read_input_tokens": 6}},
        },
    }
)
delta("secret plan", "thinking_delta")
delta("안녕 ")
delta("ignored subagent text", parent="toolu_1")
if mode == "hang":
    time.sleep(60)
delta("세계<ST")
delta("OP>hidden")
if mode == "max-tokens":
    emit(
        {
            "type": "stream_event",
            "event": {
                "type": "message_delta",
                "delta": {"stop_reason": "max_tokens", "stop_sequence": None},
                "usage": {"output_tokens": 7},
            },
        }
    )
    delta(" retried continuation")
    emit({"type": "assistant", "error": "max_output_tokens", "message": {}})
    emit({"type": "result", "is_error": True, "result": "API Error: response exceeded the output token maximum"})
    sys.exit(1)
emit({"type": "message_stop"})
emit(
    {
        "type": "result",
        "is_error": False,
        "stop_reason": "end_turn",
        "result": "안녕 세계<STOP>hidden",
        "usage": {
            "input_tokens": 3,
            "cache_creation_input_tokens": 5,
            "cache_read_input_tokens": 13,
            "output_tokens": 8,
        },
    }
)
