from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psutil
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claude_cli import PASSTHROUGH_ENV, CliSettings, Failed
from claude_llm import ClaudeCodeLLM, scan_stop
from litellm import APIError, AuthenticationError, BadRequestError, RateLimitError, ServiceUnavailableError, Timeout
from litellm.proxy.types_utils.utils import get_instance_fn

FAKE_CLI = Path(__file__).with_name("fake_claude.py")


def settings(tmp_path: Path, mode: str = "ok", timeout: float = 10) -> CliSettings:
    record = tmp_path / "record"
    record.mkdir(exist_ok=True)
    return CliSettings(
        (sys.executable, "-X", "utf8", str(FAKE_CLI), mode),
        {"SYSTEMROOT": os.environ.get("SYSTEMROOT", ""), "PATH": os.environ["PATH"], "FAKE_RECORD_DIR": str(record)},
        tmp_path / "root",
        timeout,
    )


def handler(tmp_path: Path, mode: str = "ok", timeout: float = 10) -> ClaudeCodeLLM:
    return ClaudeCodeLLM(settings(tmp_path, mode, timeout))


def call(tmp_path: Path) -> dict[str, object]:
    return json.loads((tmp_path / "record" / "call.json").read_text(encoding="utf-8"))


def child_pid(tmp_path: Path) -> int:
    return int((tmp_path / "record" / "child.pid").read_text())


def assert_cleaned(tmp_path: Path) -> None:
    assert not psutil.pid_exists(child_pid(tmp_path))
    assert list((tmp_path / "root").iterdir()) == []


def messages() -> list[dict[str, object]]:
    return [
        {"role": "system", "content": "Speak Korean"},
        {"role": "user", "content": "앞선 질문", "name": "Alice"},
        {"role": "assistant", "content": [{"type": "text", "text": "앞선 답변"}]},
        {"role": "developer", "content": "Stay in character"},
        {"role": "user", "content": "다음 질문"},
    ]


def session_turns(record: dict[str, object]) -> list[tuple[str, str]]:
    session = record["session"]
    assert isinstance(session, list)
    return [
        (
            line["type"],
            line["message"]["content"] if line["type"] == "user" else line["message"]["content"][0]["text"],
        )
        for line in session
    ]


def test_history_system_prompt_and_isolation_reach_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    response = asyncio.run(handler(tmp_path).acompletion("claude_code/claude-sonnet-5-5", messages()))
    assert response.choices[0].message.content == "안녕 세계<STOP>hidden"
    assert response.choices[0].finish_reason == "stop"
    assert (response.usage.prompt_tokens, response.usage.completion_tokens) == (21, 8)
    record = call(tmp_path)
    assert record["system"] == "Speak Korean\n\nStay in character"
    assert record["stdin"] == "다음 질문"
    assert session_turns(record) == [("user", "Alice: 앞선 질문"), ("assistant", "앞선 답변")]
    session = record["session"]
    assert isinstance(session, list)
    assert [line["parentUuid"] for line in session] == [None, session[0]["uuid"]]
    args = record["args"]
    assert isinstance(args, list)
    assert args[args.index("--model") + 1] == "claude-sonnet-5-5"
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--setting-sources") + 1] == ""
    assert {"--safe-mode", "--strict-mcp-config", "--no-session-persistence", "--fork-session"} <= set(args)
    assert "--effort" not in args
    env = record["env"]
    assert isinstance(env, dict)
    assert "CLAUDECODE" not in env
    assert "MAX_THINKING_TOKENS" not in env and "CLAUDE_CODE_MAX_OUTPUT_TOKENS" not in env
    assert Path(env["CLAUDE_CONFIG_DIR"]).parent.parent == tmp_path / "root"
    assert_cleaned(tmp_path)


def test_assistant_edges_and_consecutive_roles_are_normalized(tmp_path: Path) -> None:
    conversation = [
        {"role": "assistant", "content": "인사말"},
        {"role": "user", "content": "첫째"},
        {"role": "user", "content": "   "},
        {"role": "user", "content": "둘째"},
        {"role": "assistant", "content": "답변"},
    ]
    asyncio.run(handler(tmp_path).acompletion("claude-opus-5-5", conversation))
    record = call(tmp_path)
    assert session_turns(record) == [
        ("user", "."),
        ("assistant", "인사말"),
        ("user", "첫째\n\n둘째"),
        ("assistant", "답변"),
    ]
    assert record["stdin"] == "Please continue."
    assert record["system"].startswith("You are Claude")


def test_single_user_message_runs_without_resume(tmp_path: Path) -> None:
    asyncio.run(handler(tmp_path).acompletion("claude-opus-5-5", [{"role": "user", "content": "hi"}]))
    record = call(tmp_path)
    assert record["session"] is None
    assert "--resume" not in record["args"]


@pytest.mark.parametrize(
    ("params", "effort", "env"),
    [
        ({"reasoning_effort": "none"}, None, {"MAX_THINKING_TOKENS": "0"}),
        ({"reasoning_effort": "minimal"}, "low", {}),
        ({"reasoning_effort": "xhigh", "max_tokens": 900}, "xhigh", {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "900"}),
        ({"max_tokens": 900, "max_completion_tokens": 300}, None, {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "300"}),
    ],
)
def test_generation_options_map_to_cli(
    tmp_path: Path, params: dict[str, object], effort: str | None, env: dict[str, str]
) -> None:
    asyncio.run(handler(tmp_path).acompletion("claude-opus-5-5", messages(), params))
    record = call(tmp_path)
    args = record["args"]
    assert (args[args.index("--effort") + 1] if "--effort" in args else None) == effort
    controls = {"MAX_THINKING_TOKENS", "CLAUDE_CODE_MAX_OUTPUT_TOKENS"}
    assert {key: value for key, value in record["env"].items() if key in controls} == env


def test_max_tokens_finishes_with_length_before_cli_recovery_retries(tmp_path: Path) -> None:
    response = asyncio.run(handler(tmp_path, "max-tokens").acompletion("claude-opus-5-5", messages()))
    assert response.choices[0].message.content == "안녕 세계<STOP>hidden"
    assert response.choices[0].finish_reason == "length"
    assert (response.usage.prompt_tokens, response.usage.completion_tokens) == (12, 7)
    assert_cleaned(tmp_path)


def test_proxy_dynamic_loading_and_openai_stream_contract(tmp_path: Path) -> None:
    loaded = get_instance_fn("claude_llm.ClaudeCodeLLM", str(Path(__file__).resolve().parents[1] / "config.yaml"))
    provider = loaded(settings(tmp_path))
    chunks = tuple(provider.streaming("claude-opus-5-5", messages()))
    assert len({chunk.id for chunk in chunks}) == 1
    assert "".join(chunk.choices[0].delta.content or "" for chunk in chunks) == "안녕 세계<STOP>hidden"
    assert chunks[0].choices[0].delta.role == "assistant"
    assert chunks[-2].usage.total_tokens == 29
    assert chunks[-1].choices[0].finish_reason == "stop"
    assert_cleaned(tmp_path)


def test_stop_split_across_deltas_ends_early(tmp_path: Path) -> None:
    response = asyncio.run(handler(tmp_path).acompletion("claude-opus-5-5", messages(), {"stop": ["<STOP>"]}))
    assert response.choices[0].message.content == "안녕 세계"
    assert response.choices[0].finish_reason == "stop"
    assert_cleaned(tmp_path)


def test_stop_sequence_returns_without_waiting_for_cli_to_finish(tmp_path: Path) -> None:
    response = asyncio.run(handler(tmp_path, "hang", 5).acompletion("claude-opus-5-5", messages(), {"stop": "녕"}))
    assert response.choices[0].message.content == "안"
    assert response.choices[0].finish_reason == "stop"
    assert_cleaned(tmp_path)


def test_partial_stop_prefix_is_held_then_flushed() -> None:
    held = scan_stop(("<STOP>",), "", "hello <ST", final=False)
    assert (held.emit, held.pending, held.stopped) == ("hello ", "<ST", False)
    assert scan_stop(("<STOP>",), held.pending, "", final=True).emit == "<ST"


def test_streaming_close_kills_cli_and_removes_workdir(tmp_path: Path) -> None:
    async def run() -> None:
        stream = handler(tmp_path, "hang").astreaming("claude-opus-5-5", messages())
        first = await asyncio.wait_for(anext(stream), 10)
        assert first.choices[0].delta.content == "안녕 "
        assert psutil.pid_exists(child_pid(tmp_path))
        await stream.aclose()
        assert_cleaned(tmp_path)

    asyncio.run(run())


@pytest.mark.parametrize(
    ("mode", "error_type", "status", "message"),
    [
        ("auth", AuthenticationError, 401, "Not logged in"),
        ("usage-limit", RateLimitError, 429, "usage limit"),
        ("overloaded", APIError, 529, "Overloaded"),
        ("eof", ServiceUnavailableError, 503, "exited (3)"),
        ("hang", Timeout, 504, "timed out"),
    ],
)
def test_cli_failures_map_to_litellm_errors(
    tmp_path: Path, mode: str, error_type: type[Exception], status: int, message: str
) -> None:
    with pytest.raises(error_type) as caught:
        asyncio.run(handler(tmp_path, mode, 1 if mode == "hang" else 10).acompletion("claude-opus-5-5", messages()))
    assert caught.value.status_code == status
    assert message in str(caught.value)
    assert_cleaned(tmp_path)


@pytest.mark.parametrize(
    ("bad_messages", "params"),
    [
        ([], None),
        ([{"role": "system", "content": "only system"}], None),
        ([{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}], None),
        ([{"role": "tool", "content": "tool output", "tool_call_id": "1"}], None),
        ([{"role": "assistant", "content": "x", "tool_calls": [{"id": "1"}]}, {"role": "user", "content": "y"}], None),
        ([{"role": "user", "content": "hi"}], {"tools": [{"type": "function"}]}),
        ([{"role": "user", "content": "hi"}], {"response_format": {"type": "json_object"}}),
        ([{"role": "user", "content": "hi"}], {"max_tokens": 0}),
    ],
)
def test_unsupported_inputs_fail_before_launch(tmp_path: Path, bad_messages: object, params: object) -> None:
    with pytest.raises(BadRequestError) as caught:
        asyncio.run(handler(tmp_path).acompletion("claude-opus-5-5", bad_messages, params))
    assert caught.value.status_code == 400
    assert not (tmp_path / "record" / "child.pid").exists()


def test_local_settings_require_token_and_filter_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    missing = CliSettings.local()
    assert isinstance(missing, Failed) and missing.status_code == 503
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "token")
    monkeypatch.setenv("CLAUDE_CLI", sys.executable)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    configured = CliSettings.local()
    assert isinstance(configured, CliSettings)
    assert configured.command == (sys.executable,)
    assert configured.env["CLAUDE_CODE_OAUTH_TOKEN"] == "token"
    assert set(configured.env) <= set(PASSTHROUGH_ENV)


class FakeAnthropic(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []
    stop_reason = "end_turn"
    url = ""

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        type(self).requests.append(body)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        events = (
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": body["model"],
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 11, "output_tokens": 1},
                },
            },
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "실제 "}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "CLI"}},
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": type(self).stop_reason, "stop_sequence": None},
                "usage": {"output_tokens": 4},
            },
            {"type": "message_stop"},
        )
        self.wfile.write(b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events))

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def anthropic() -> Iterator[type[FakeAnthropic]]:
    FakeAnthropic.requests = []
    FakeAnthropic.stop_reason = "end_turn"
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAnthropic)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    FakeAnthropic.url = f"http://127.0.0.1:{server.server_address[1]}"
    yield FakeAnthropic
    server.shutdown()


REAL_CLI = shutil.which("claude.exe" if os.name == "nt" else "claude")


@pytest.mark.skipif(REAL_CLI is None, reason="Claude Code CLI is not installed")
@pytest.mark.parametrize("stop_reason", ["end_turn", "max_tokens"])
def test_real_cli_sends_roles_in_order_without_tools(
    tmp_path: Path, anthropic: type[FakeAnthropic], stop_reason: str
) -> None:
    anthropic.stop_reason = stop_reason
    real = CliSettings(
        (str(REAL_CLI),),
        {
            **{
                name: value
                for name in PASSTHROUGH_ENV
                if name != "CLAUDE_CODE_OAUTH_TOKEN" and (value := os.environ.get(name))
            },
            "ANTHROPIC_BASE_URL": anthropic.url,
            "ANTHROPIC_API_KEY": "sk-test",
        },
        Path(os.environ.get("TEMP", tmp_path)) / f"lct-{os.getpid()}",
        120,
    )
    response = asyncio.run(
        ClaudeCodeLLM(real).acompletion("claude_code/claude-sonnet-5-5", messages(), {"max_tokens": 777})
    )
    assert response.choices[0].message.content == "실제 CLI"
    assert response.choices[0].finish_reason == ("length" if stop_reason == "max_tokens" else "stop")
    assert len(anthropic.requests) == 1
    body = anthropic.requests[0]
    assert body["model"] == "claude-sonnet-5-5"
    assert body["max_tokens"] == 777
    assert body.get("tools", []) == []
    assert body["system"][-1]["text"] == "Speak Korean\n\nStay in character"
    chat = [
        (message["role"], "".join(part.get("text", "") for part in message["content"]))
        if isinstance(message["content"], list)
        else (message["role"], message["content"])
        for message in body["messages"]
        if message["role"] != "system"
    ]
    assert chat == [("user", "Alice: 앞선 질문"), ("assistant", "앞선 답변"), ("user", "다음 질문")]
    assert list(real.root.iterdir()) == []
