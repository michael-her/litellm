from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import aclosing, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

Effort = Literal["low", "medium", "high", "xhigh", "max"]

PASSTHROUGH_ENV = (
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATH",
    "PATHEXT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "HOME",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "SSL_CERT_FILE",
    "NODE_EXTRA_CA_CERTS",
    "CLAUDE_CODE_OAUTH_TOKEN",
)


@dataclass(frozen=True, slots=True)
class Turn:
    role: Literal["user", "assistant"]
    text: str


@dataclass(frozen=True, slots=True)
class Conversation:
    system: str
    history: tuple[Turn, ...]
    prompt: str


@dataclass(frozen=True, slots=True)
class Generation:
    model: str
    effort: Effort | None
    thinking: bool
    max_output_tokens: int | None


@dataclass(frozen=True, slots=True)
class TokenUsage:
    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True, slots=True)
class TextChunk:
    text: str


@dataclass(frozen=True, slots=True)
class Finished:
    reason: Literal["stop", "length"]
    usage: TokenUsage | None


@dataclass(frozen=True, slots=True)
class Failed:
    status_code: int
    message: str


CliOutput = TextChunk | Finished | Failed


@dataclass(frozen=True, slots=True)
class CliSettings:
    command: tuple[str, ...]
    env: Mapping[str, str]
    root: Path
    timeout: float = 600

    @classmethod
    def local(cls) -> CliSettings | Failed:
        if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            return Failed(503, "Set CLAUDE_CODE_OAUTH_TOKEN in .claude-code/claude.local.env (run claude setup-token)")
        cli = resolve_cli()
        if isinstance(cli, Failed):
            return cli
        return cls(
            (cli,),
            {name: value for name in PASSTHROUGH_ENV if (value := os.environ.get(name))},
            Path(tempfile.gettempdir()) / "litellm-claude",
            float(os.environ.get("CLAUDE_TIMEOUT_S", "600")),
        )


def resolve_cli() -> str | Failed:
    override = os.environ.get("CLAUDE_CLI")
    if override:
        return (
            override
            if Path(override).is_file()
            else Failed(503, "CLAUDE_CLI must point to an existing claude executable")
        )
    found = shutil.which("claude.exe" if os.name == "nt" else "claude") or shutil.which("claude")
    return found or Failed(503, "Claude Code CLI not found. Set CLAUDE_CLI in .claude-code/claude.local.env")


class _Delta(BaseModel):
    type: str | None = None
    text: str = ""
    stop_reason: str | None = None


class _Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens


class _Message(BaseModel):
    usage: _Usage | None = None


class _StreamEvent(BaseModel):
    type: str
    delta: _Delta | None = None
    message: _Message | None = None
    usage: _Usage | None = None


class _CliEvent(BaseModel):
    type: str
    event: _StreamEvent | None = None
    parent_tool_use_id: str | None = None
    error: str | None = None
    is_error: bool = False
    api_error_status: int | None = None
    result: str | None = None
    usage: _Usage | None = None


ERROR_STATUS = {"authentication_failed": 401, "rate_limit": 429}


def session_lines(history: tuple[Turn, ...], session_id: str, cwd: Path) -> tuple[str, ...]:
    ids = tuple(str(uuid.uuid4()) for _ in history)
    return tuple(
        json.dumps(
            {
                "parentUuid": ids[index - 1] if index else None,
                "isSidechain": False,
                "type": turn.role,
                "message": {"role": "user", "content": turn.text}
                if turn.role == "user"
                else {
                    "id": f"msg_{ids[index].replace('-', '')}",
                    "type": "message",
                    "role": "assistant",
                    "model": "<history>",
                    "content": [{"type": "text", "text": turn.text}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
                "uuid": ids[index],
                "timestamp": "1970-01-01T00:00:00.000Z",
                "userType": "external",
                "cwd": str(cwd),
                "sessionId": session_id,
            },
            ensure_ascii=False,
        )
        for index, turn in enumerate(history)
    )


def cli_arguments(generation: Generation, system_file: Path, session_file: Path | None) -> tuple[str, ...]:
    return (
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--model",
        generation.model,
        "--safe-mode",
        "--tools",
        "",
        "--strict-mcp-config",
        "--setting-sources",
        "",
        "--permission-prompts",
        "none",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--system-prompt-file",
        str(system_file),
        *(("--effort", generation.effort) if generation.effort else ()),
        *(("--resume", str(session_file), "--fork-session") if session_file else ()),
    )


def cli_env(settings: CliSettings, generation: Generation, config_dir: Path) -> dict[str, str]:
    return {
        **settings.env,
        "CLAUDE_CONFIG_DIR": str(config_dir),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        **({"MAX_THINKING_TOKENS": "0"} if not generation.thinking else {}),
        **(
            {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(generation.max_output_tokens)} if generation.max_output_tokens else {}
        ),
    }


def failure(event: _CliEvent, assistant_error: str | None) -> Failed:
    status = event.api_error_status or ERROR_STATUS.get(assistant_error or "", 502)
    return Failed(status, event.result or "Claude CLI request failed")


async def run(settings: CliSettings, conversation: Conversation, generation: Generation) -> AsyncIterator[CliOutput]:
    settings.root.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="req-", dir=settings.root))
    try:
        async with aclosing(_run_in(settings, conversation, generation, workdir)) as outputs:
            async for output in outputs:
                yield output
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def _run_in(
    settings: CliSettings, conversation: Conversation, generation: Generation, workdir: Path
) -> AsyncIterator[CliOutput]:
    cwd = workdir / "ws"
    cwd.mkdir()
    system_file = workdir / "system.txt"
    system_file.write_text(conversation.system, encoding="utf-8", newline="")
    session_file = workdir / "history.jsonl" if conversation.history else None
    if session_file:
        lines = session_lines(conversation.history, str(uuid.uuid4()), cwd)
        session_file.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="")
    stderr_path = workdir / "stderr.log"
    with stderr_path.open("wb") as stderr:
        process = await asyncio.create_subprocess_exec(
            *settings.command,
            *cli_arguments(generation, system_file, session_file),
            cwd=cwd,
            env=cli_env(settings, generation, workdir / "config"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            limit=16 * 1024 * 1024,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    try:
        if process.stdin is None or process.stdout is None:
            yield Failed(503, "Claude CLI pipes are unavailable")
            return
        process.stdin.write(conversation.prompt.encode("utf-8"))
        await process.stdin.drain()
        process.stdin.close()
        assistant_error: str | None = None
        prompt_tokens = 0
        async for line in process.stdout:
            try:
                event = _CliEvent.model_validate_json(line)
            except ValidationError:
                continue
            if event.parent_tool_use_id is not None:
                continue
            match event.type:
                case "stream_event" if (
                    event.event
                    and event.event.type == "content_block_delta"
                    and event.event.delta
                    and event.event.delta.type == "text_delta"
                    and event.event.delta.text
                ):
                    yield TextChunk(event.event.delta.text)
                case "stream_event" if event.event and event.event.type == "message_start" and event.event.message:
                    prompt_tokens = (event.event.message.usage or _Usage()).prompt_tokens()
                case "stream_event" if (
                    event.event
                    and event.event.type == "message_delta"
                    and event.event.delta
                    and event.event.delta.stop_reason == "max_tokens"
                ):
                    yield Finished("length", TokenUsage(prompt_tokens, (event.event.usage or _Usage()).output_tokens))
                    return
                case "assistant" if event.error:
                    assistant_error = event.error
                case "result" if event.is_error:
                    yield failure(event, assistant_error)
                    return
                case "result":
                    usage = event.usage or _Usage()
                    yield Finished("stop", TokenUsage(usage.prompt_tokens(), usage.output_tokens))
                    return
                case _:
                    pass
        await process.wait()
        detail = stderr_path.read_text(encoding="utf-8", errors="replace").strip()[-500:]
        yield Failed(503, f"Claude CLI exited ({process.returncode}) without a result. {detail}".strip())
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        await process.wait()
