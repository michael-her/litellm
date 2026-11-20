from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import sys
from functools import partial
from pathlib import Path

import psutil
import pytest
import litellm
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from antigravity_cli import Client, Failure, Settings, cleanup_request
from antigravity_llm import MAX_PROMPT_BYTES, AntigravityLLM, render
from generate_config import ROOT, build_config, main, model_ids, vertex_names
from litellm import APIConnectionError, AuthenticationError, BadRequestError, NotFoundError, RateLimitError, Timeout
from litellm.proxy.types_utils.utils import get_instance_fn


def test_proxy_dynamic_loader(tmp_path: Path) -> None:
    provider = get_instance_fn("antigravity_llm.antigravity_handler", str(ROOT / "config.generated.yaml"))
    assert provider.__class__.__name__ == "AntigravityLLM"
    provider.client = Client(settings(tmp_path))
    response = asyncio.run(provider.acompletion("fake", [{"role": "user", "content": "x"}]))
    assert response.choices[0].message.content == "final hello world"


def test_litellm_stream_wrapper_preserves_cli_usage(tmp_path: Path) -> None:
    registration = {"provider": "antigravity_test", "custom_handler": AntigravityLLM(settings(tmp_path))}
    litellm.custom_provider_map.append(registration)
    async def check() -> None:
        stream = await litellm.acompletion(
            model="antigravity_test/fake", messages=[{"role": "user", "content": "x"}],
            stream=True, stream_options={"include_usage": True},
        )
        chunks = [chunk async for chunk in stream]
        assert "".join(chunk.choices[0].delta.content or "" for chunk in chunks if chunk.choices) == "hello world"
        usage = next(chunk.usage for chunk in chunks if getattr(chunk, "usage", None) is not None)
        assert usage.prompt_tokens == 13
        assert usage.completion_tokens == 7
        assert usage.total_tokens == 20
    try:
        asyncio.run(check())
    finally:
        litellm.custom_provider_map.remove(registration)
        for registry in (litellm._custom_providers, litellm.provider_list):
            if "antigravity_test" in registry:
                registry.remove("antigravity_test")


def test_public_bad_request_mapping(tmp_path: Path) -> None:
    provider = AntigravityLLM(settings(tmp_path))
    with pytest.raises(BadRequestError):
        asyncio.run(provider.acompletion("fake", [{"role": "user", "content": "x"}], {"tools": [{}]}))
    assert not (tmp_path / "state").exists()


def settings(tmp: Path, mode: str = "ok", timeout: float = 5, concurrency: int = 2, queue: float = 1) -> Settings:
    return Settings(
        (sys.executable, str(Path(__file__).with_name("fake_agy.py")), mode),
        {"SYSTEMROOT": os.environ.get("SYSTEMROOT", ""), "PATH": os.environ["PATH"],
         "FAKE_RECORD_DIR": str(tmp / "record")}, tmp / "state", timeout, queue, concurrency,
    )


def test_transcript_and_validation() -> None:
    result = render([
        {"role": "system", "content": "rules"}, {"role": "developer", "content": "more"},
        {"role": "user", "name": "A", "content": [{"type": "text", "text": "first"}]},
        {"role": "assistant", "content": "past"}, {"role": "user", "content": "last\nline"},
    ], {"temperature": 0.2, "max_tokens": 1})
    lines = result.splitlines()
    assert [json.loads(line)["role"] for line in lines[1:-1]] == ["system", "developer", "user", "assistant", "user"]
    assert json.loads(lines[-2])["content"] == "last\nline"
    assert json.loads(lines[3])["name"] == "A"


@pytest.mark.parametrize("messages,options", [
    ([{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}], {}),
    ([{"role": "user", "content": [{"type": "input_audio", "input_audio": {}}]}], {}),
    ([{"role": "tool", "content": "x"}], {}),
    ([{"role": "assistant", "content": "x", "tool_calls": [{"id": "x"}]}], {}),
    ([{"role": "user", "content": "x"}], {"tools": [{"type": "function"}]}),
    ([{"role": "user", "content": "x"}], {"functions": [{"name": "x"}]}),
    ([{"role": "user", "content": "x"}], {"tool_choice": "auto"}),
    ([], {}),
])
def test_reject_unsupported(messages: object, options: object) -> None:
    with pytest.raises(Failure) as caught:
        render(messages, options)
    assert caught.value.status == 400


def test_completion_stream_usage_isolation_cleanup(tmp_path: Path) -> None:
    provider = AntigravityLLM(settings(tmp_path))
    async def check() -> None:
        response = await provider.acompletion("fake", [{"role": "user", "content": "x"}])
        assert response.choices[0].message.content == "final hello world"
        assert response.usage.prompt_tokens == 13
        assert response.usage.completion_tokens == 7
        assert response.usage.total_tokens == 20
        chunks = [chunk async for chunk in provider.astreaming("fake", [{"role": "user", "content": "x"}])]
        assert "".join(chunk.choices[0].delta.content or "" for chunk in chunks) == "hello world"
        assert chunks[-2].usage.total_tokens == 20
        assert chunks[-1].choices[0].finish_reason == "stop"
    asyncio.run(check())
    records = tuple((tmp_path / "record").glob("call-*.json"))
    assert len(records) == 2
    calls = [json.loads(path.read_text()) for path in records]
    assert calls[0]["cwd"] != calls[1]["cwd"]
    for call in calls:
        assert call["initial_files"] == []
        assert "--conversation" not in call["args"]
        assert "--dangerously-skip-permissions" not in call["args"]
        assert call["args"][call["args"].index("--agent") + 1] == "litellm-antigravity"
        assert not Path(call["cwd"]).exists()
    assert list((tmp_path / "state").iterdir()) == []


@pytest.mark.parametrize("mode,kind,marker", [
    ("rate", RateLimitError, "agy-test-quota"), ("auth", AuthenticationError, "OAuth"),
    ("unknown", NotFoundError, "unknown model"), ("crash", APIConnectionError, "fake crash stderr marker"),
    ("malformed", APIConnectionError, "malformed"), ("tool", APIConnectionError, "forbidden"),
    ("permission", APIConnectionError, "forbidden"),
])
def test_errors(tmp_path: Path, mode: str, kind: type[Exception], marker: str) -> None:
    provider = AntigravityLLM(settings(tmp_path, mode))
    with pytest.raises(kind) as caught:
        asyncio.run(provider.acompletion("fake", [{"role": "user", "content": "x"}]))
    assert marker in str(caught.value)
    assert "agy model=fake" in str(caught.value)
    assert list((tmp_path / "state").iterdir()) == []


@pytest.mark.parametrize("cancel", [False, True])
def test_timeout_and_disconnect_kill_tree(tmp_path: Path, cancel: bool) -> None:
    provider = AntigravityLLM(settings(tmp_path, "timeout", timeout=1 if not cancel else 10))
    async def check() -> None:
        task = asyncio.create_task(provider.acompletion("fake", [{"role": "user", "content": "x"}]))
        if cancel:
            for _ in range(100):
                if (tmp_path / "record/child.pid").exists():
                    break
                await asyncio.sleep(0.025)
            assert (tmp_path / "record/child.pid").exists()
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else Timeout):
            await task
    asyncio.run(check())
    child = int((tmp_path / "record/child.pid").read_text())
    assert not psutil.pid_exists(child)
    assert list((tmp_path / "state").iterdir()) == []


def test_bounded_concurrency_queue(tmp_path: Path) -> None:
    provider = AntigravityLLM(settings(tmp_path, "slow", concurrency=1, queue=0.1))
    async def check() -> None:
        first = asyncio.create_task(provider.acompletion("fake", [{"role": "user", "content": "x"}]))
        await asyncio.sleep(0.025)
        with pytest.raises(RateLimitError, match="queue wait"):
            await provider.acompletion("fake", [{"role": "user", "content": "x"}])
        assert (await first).choices[0].message.content == "final hello world"
    asyncio.run(check())
    assert len(tuple((tmp_path / "record").glob("call-*.json"))) == 1


def test_models_and_fallback_generation() -> None:
    models = model_ids("Fetching models...\ngemini-4-hydrogen-medium\tHydrogen\nclaude-opus-5-5-low\tClaude\ngpt-oss-120b-medium\tGPT\n")
    vertex = ("gemini-4-hydrogen-medium", "gemini-4-carbon-low", "vertex-default", "gemini-3.8-flash")
    config = build_config(models, vertex, ROOT / "config.template.yaml")
    by_name = {entry["model_name"]: entry["litellm_params"] for entry in config["model_list"]}
    assert by_name[models[0]]["model"] == "antigravity/gemini-4-hydrogen-medium"
    for name in models[1:]:
        assert by_name[name]["model"] == "antigravity/" + name
    for name in vertex[1:]:
        assert by_name[name]["model"] == "openai/" + name
    assert config["router_settings"]["fallbacks"] == [{models[0]: ["vertex-fallback/" + models[0]]}]
    assert by_name["vertex-fallback/" + models[0]]["api_key"] == "os.environ/VERTEX_PROXY_KEY"
    assert set(vertex_names(ROOT.parent / ".vertex/config.yaml")) <= set(
        entry["model_name"] for entry in build_config(models, vertex_names(ROOT.parent / ".vertex/config.yaml"),
                                                     ROOT / "config.template.yaml")["model_list"])
    with pytest.raises(ValueError):
        model_ids("Fetching models...\n")


def test_generic_medium_aliases_and_original_vertex_fallbacks() -> None:
    aliases = ("gemini-4-hydrogen", "gemini-4-neon", "gemini-4-argon", "gemini-3.8-flash", "future-family")
    vertex_only = ("gemini-4-carbon", "gemini-4-carbon-low", "gemini-4-carbon-medium", "gemini-4-carbon-high",
                   "gemini-3.5-flash", "gemini-3.5-flash-lite", "vertex-default")
    suffixed = tuple("not-an-alias-" + effort for effort in ("low", "medium", "high", "xhigh"))
    exact = "exact-family"
    agy = (*tuple(name + "-medium" for name in aliases), "future-family-low", exact, exact + "-medium",
           "agy-only-family", "agy-only-family-medium",
           *tuple(name + "-medium" for name in suffixed))
    vertex = (*aliases, *vertex_only, *suffixed, exact)
    config = build_config(agy, vertex, ROOT / "config.template.yaml")
    entries = {entry["model_name"]: entry["litellm_params"] for entry in config["model_list"]}
    fallbacks = {name: targets for entry in config["router_settings"]["fallbacks"] for name, targets in entry.items()}
    for name in aliases:
        assert entries[name]["model"] == "antigravity/" + name + "-medium"
        assert fallbacks[name] == ["vertex-fallback/" + name]
        assert entries["vertex-fallback/" + name]["model"] == "openai/" + name
        assert entries["vertex-fallback/" + name]["api_base"] == "http://127.0.0.1:14604/v1"
    for name in (*vertex_only, *suffixed):
        assert entries[name]["model"] == "openai/" + name
        assert name not in fallbacks
        assert "vertex-fallback/" + name not in entries
    assert entries[exact]["model"] == "antigravity/" + exact + "-medium"
    assert fallbacks[exact] == ["vertex-fallback/" + exact]
    assert entries["vertex-fallback/" + exact]["model"] == "openai/" + exact
    assert entries["agy-only-family"]["model"] == "antigravity/agy-only-family"
    assert set(vertex) <= set(entries)


def test_generated_route_metadata_identifies_medium_target(tmp_path: Path) -> None:
    root = tmp_path / ".antigravity"
    root.mkdir()
    vertex = tmp_path / ".vertex"
    vertex.mkdir()
    (root / "config.template.yaml").write_bytes((ROOT / "config.template.yaml").read_bytes())
    (vertex / "config.yaml").write_text(yaml.safe_dump({"model_list": [
        {"model_name": "future-family"}, {"model_name": "gemini-4-carbon"},
    ]}), encoding="utf-8")
    main(root, (sys.executable, "-c", "print('future-family-medium\\tFuture Medium')"))
    routes = json.loads((root / "models.generated.json").read_text())
    assert routes["future-family"] == "agy:future-family-medium-with-vertex-fallback"
    assert routes["gemini-4-carbon"] == "vertex"
    config = yaml.safe_load((root / "config.generated.yaml").read_text())
    assert {entry["model_name"]: entry["litellm_params"]["model"] for entry in config["model_list"]}[
        "future-family"] == "antigravity/future-family-medium"


def test_discovery_failure_retains_last_good(tmp_path: Path) -> None:
    (tmp_path / "config.generated.yaml").write_text("last-good-config", encoding="utf-8")
    main(tmp_path, (sys.executable, str(Path(__file__).with_name("fake_agy.py")), "models-fail"))
    assert (tmp_path / "config.generated.yaml").read_text() == "last-good-config"
    (tmp_path / "config.generated.yaml").unlink()
    with pytest.raises(RuntimeError, match="no last good config"):
        main(tmp_path, (sys.executable, str(Path(__file__).with_name("fake_agy.py")), "models-fail"))


def test_streaming_fallback_http_contract() -> None:
    from verify_fallback import main as verify_fallback
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    verify_fallback(port, "pytest-stream-fallback")


def test_cleanup_retries_transient_lock(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    workdir = tmp_path / "locked-request"
    workdir.mkdir()
    calls: list[Path] = []
    def remove(path: Path) -> None:
        calls.append(path)
        if len(calls) < 3:
            raise PermissionError("test file lock")
        shutil.rmtree(path)
    cleanup_request(workdir, remove, (0, 0, 0, 0))
    assert calls == [workdir] * 3
    assert not workdir.exists()
    assert not caplog.records


def test_cleanup_failure_preserves_agy_error(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    calls: list[Path] = []
    def remove(path: Path) -> None:
        calls.append(path)
        raise PermissionError("test persistent file lock")
    client = Client(settings(tmp_path, "rate"), partial(cleanup_request, remove=remove, delays=(0, 0, 0)))
    async def check() -> None:
        with pytest.raises(Failure, match="agy-test-quota") as caught:
            async for _ in client.run("fake", "x"):
                pytest.fail("A quota failure must not emit content")
        assert caught.value.status == 429
    asyncio.run(check())
    assert len(calls) == 3
    assert len(set(calls)) == 1
    assert calls[0].is_dir()
    assert "agy request directory left after cleanup retries" in caplog.text
    assert str(calls[0]) in caplog.text
    assert "test persistent file lock" in caplog.text
    shutil.rmtree(calls[0])


def test_large_korean_history_preserves_required_messages_in_fake_agy(tmp_path: Path) -> None:
    system = "SYSTEM_SENTINEL: 원래 시스템 지침을 그대로 유지합니다."
    latest = "LATEST_SENTINEL: 마지막 사용자 입력과 한글 끝문자 '끝'을 그대로 유지합니다."
    history = [
        {"role": "system", "content": system},
        {"role": "user", "content": "OPENING_USER"},
        {"role": "assistant", "content": "OPENING_ASSISTANT"},
        *[{"role": "user" if i % 2 == 0 else "assistant", "content": f"MIDDLE_{i}:" + "한글 배경 자료. " * 3000}
          for i in range(10)],
        {"role": "developer", "content": "DEVELOPER_SENTINEL"},
        {"role": "user", "content": latest},
    ]
    assert len(json.dumps(history, ensure_ascii=False).encode("utf-8")) > 200_000
    asyncio.run(AntigravityLLM(settings(tmp_path)).acompletion("fake", history))
    call = json.loads(next((tmp_path / "record").glob("call-*.json")).read_text(encoding="utf-8"))
    prompt = call["prompt"]["message"]["content"]
    assert len(prompt.encode("utf-8")) <= MAX_PROMPT_BYTES
    records = [json.loads(line) for line in prompt.splitlines()[1:-1]]
    assert "earlier messages were omitted" in records[0]["content"]
    assert {"role": "system", "content": system} in records
    assert {"role": "developer", "content": "DEVELOPER_SENTINEL"} in records
    assert {"role": "user", "content": "OPENING_USER"} in records
    assert {"role": "assistant", "content": "OPENING_ASSISTANT"} in records
    assert records[-1] == {"role": "user", "content": latest}
    kept = [record for record in records[1:] if record["content"].startswith("MIDDLE_")]
    assert kept and len(kept) < 10
    first_kept = int(kept[0]["content"].split(":", 1)[0].removeprefix("MIDDLE_"))
    assert kept == history[3 + first_kept:13]
    assert f"{10 - len(kept)} earlier messages were omitted" in records[0]["content"]
    assert list((tmp_path / "state").iterdir()) == []


@pytest.mark.parametrize("role", ["system", "developer", "user"])
def test_oversized_required_message_returns_bad_request_without_spawning(tmp_path: Path, role: str) -> None:
    messages = [{"role": role, "content": "한" * 70_000}]
    if role != "user":
        messages.append({"role": "user", "content": "Latest user stays intact"})
    with pytest.raises(BadRequestError, match="180000-byte UTF-8"):
        asyncio.run(AntigravityLLM(settings(tmp_path)).acompletion("fake", messages))
    assert not (tmp_path / "record").exists()


def test_exact_utf8_budget_and_complete_json_lines() -> None:
    messages = [{"role": "user", "content": "한글\n문자 \\\" 경계 끝"}]
    value = render(messages)
    limit = len(value.encode("utf-8"))
    assert limit > len(value)
    assert render(messages, byte_limit=limit) == value
    with pytest.raises(Failure, match="UTF-8 transcript budget"):
        render(messages, byte_limit=limit - 1)
    assert json.loads(value.splitlines()[1])["content"] == messages[0]["content"]


def test_all_system_messages_and_latest_user_with_assistant_prefill_are_preserved() -> None:
    messages = [
        {"role": "user", "content": "opening"}, {"role": "assistant", "content": "opening answer"},
        {"role": "user", "content": "old" * 1000}, {"role": "system", "content": "SYSTEM_IN_HISTORY"},
        {"role": "assistant", "content": "old answer" * 1000},
        {"role": "user", "content": "LATEST_USER"}, {"role": "assistant", "content": "ASSISTANT_PREFILL"},
    ]
    result = render(messages, byte_limit=1000)
    records = [json.loads(line) for line in result.splitlines()[1:-1]]
    assert records[1:] == [messages[i] for i in (0, 1, 3, 5, 6)]
    assert "2 earlier messages were omitted" in records[0]["content"]
