from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import psutil
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codex_llm import CodexLLM, StopFilter
from codex_rpc import ServerSettings
from litellm.llms.custom_llm import CustomLLMError
from litellm.proxy.types_utils.utils import get_instance_fn


def handler(tmp_path: Path, mode: str = "ok", timeout: float = 10) -> CodexLLM:
    return CodexLLM(
        ServerSettings(
            (sys.executable, "-X", "utf8", str(Path(__file__).with_name("fake_app_server.py")), mode), tmp_path, timeout
        )
    )


def messages() -> list[dict[str, str]]:
    return [
        {"role": "system", "content": "Speak Korean"},
        {"role": "user", "content": "앞선 질문"},
        {"role": "assistant", "content": "앞선 답변"},
        {"role": "user", "content": "다음 질문"},
    ]


def test_roles_unicode_usage_and_no_duplicate_completed_text(tmp_path: Path) -> None:
    response = asyncio.run(handler(tmp_path).acompletion("codex/test-model", messages()))
    assert response.choices[0].message.content == "안녕 세계<STOP>hidden"
    assert response.usage.prompt_tokens == 21
    assert response.usage.completion_tokens == 8
    requests = [json.loads(line) for line in (tmp_path / "requests.jsonl").read_text(encoding="utf-8").splitlines()]
    started = next(r["params"] for r in requests if r.get("method") == "thread/start")
    assert started["model"] == "test-model"
    assert started["ephemeral"] and started["sandbox"] == "read-only"
    history = next(r["params"]["items"] for r in requests if r.get("method") == "thread/inject_items")
    assert [item["role"] for item in history] == ["system", "user", "assistant"]
    assert history[-1]["content"] == [{"type": "output_text", "text": "앞선 답변"}]
    assert not psutil.pid_exists(int((tmp_path / "child.pid").read_text()))


def test_streaming_before_turn_completion_and_close_cleans_process(tmp_path: Path) -> None:
    async def run() -> None:
        stream = handler(tmp_path, "hang-turn").astreaming("test", messages())
        first = await asyncio.wait_for(anext(stream), 5)
        assert first.choices[0].delta.content == "안녕 "
        assert first.choices[0].finish_reason is None
        pid = int((tmp_path / "child.pid").read_text())
        assert psutil.pid_exists(pid)
        await stream.aclose()
        assert not psutil.pid_exists(pid)

    asyncio.run(run())


def test_proxy_dynamic_loading_and_openai_stream_contract(tmp_path: Path) -> None:
    loaded = get_instance_fn("codex_llm.CodexLLM", str(Path(__file__).resolve().parents[1] / "config.yaml"))
    provider = loaded(handler(tmp_path).settings)
    chunks = tuple(provider.streaming("test", messages()))
    assert len({event.id for event in chunks}) == 1
    assert len({event.created for event in chunks}) == 1
    assert "".join(event.choices[0].delta.content or "" for event in chunks) == "안녕 세계<STOP>hidden"
    assert chunks[-1].choices[0].finish_reason == "stop"
    assert chunks[-2].usage.total_tokens == 29


def test_stop_split_across_deltas(tmp_path: Path) -> None:
    response = asyncio.run(handler(tmp_path).acompletion("test", messages(), {"stop": ["<STOP>"]}))
    assert response.choices[0].message.content == "안녕 세계"
    assert response.choices[0].finish_reason == "stop"


def test_partial_stop_prefix_flushed() -> None:
    stop = StopFilter(("<STOP>",))
    assert stop.feed("hello <ST") == "hello "
    assert stop.feed("", final=True) == "<ST"


@pytest.mark.parametrize(
    ("mode", "status"),
    [
        ("no-auth", 401),
        ("eof", 503),
        ("failed", 429),
        ("approval", 400),
        ("malformed", 502),
        ("hang-init", 504),
        ("hang-turn", 504),
    ],
)
def test_errors_do_not_report_success_or_leave_processes(tmp_path: Path, mode: str, status: int) -> None:
    with pytest.raises(CustomLLMError) as caught:
        asyncio.run(handler(tmp_path, mode, 0.5 if mode.startswith("hang") else 10).acompletion("test", messages()))
    assert caught.value.status_code == status
    assert not psutil.pid_exists(int((tmp_path / "child.pid").read_text()))


@pytest.mark.parametrize(
    "bad_messages",
    [
        [],
        [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}],
        [{"role": "tool", "content": "tool output"}],
    ],
)
def test_unsupported_inputs_fail_before_process_launch(tmp_path: Path, bad_messages: object) -> None:
    with pytest.raises(CustomLLMError) as caught:
        asyncio.run(handler(tmp_path).acompletion("test", bad_messages))
    assert caught.value.status_code == 400
    assert not (tmp_path / "child.pid").exists()


def test_parallel_requests_are_isolated(tmp_path: Path) -> None:
    async def run() -> None:
        paths = tuple(tmp_path / str(index) for index in range(2))
        for path in paths:
            path.mkdir()
        results = await asyncio.gather(*(handler(path).acompletion("test", messages()) for path in paths))
        assert all(result.choices[0].message.content == "안녕 세계<STOP>hidden" for result in results)
        assert (paths[0] / "child.pid").read_text() != (paths[1] / "child.pid").read_text()

    asyncio.run(run())
