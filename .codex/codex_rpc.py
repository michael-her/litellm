from __future__ import annotations

import asyncio
import itertools
import json
import os
import shutil
import subprocess
import tempfile
import tomllib
from collections import deque
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from pydantic import JsonValue, TypeAdapter, ValidationError

JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


class RpcError(Exception):
    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


def object_value(value: JsonValue) -> dict[str, JsonValue]:
    try:
        return JSON_OBJECT.validate_python(value)
    except ValidationError as error:
        raise RpcError("Invalid object in Codex JSON-RPC response") from error


def resolve_cli() -> str:
    override = os.environ.get("CODEX_CLI")
    if override:
        if Path(override).is_file():
            return override
        raise RpcError("CODEX_CLI must point to an existing native codex executable", 503)
    native = shutil.which("codex.exe" if os.name == "nt" else "codex")
    if native:
        return native
    shim = shutil.which("codex.cmd")
    if shim:
        root = Path(shim).parent / "node_modules" / "@openai" / "codex"
        candidates = tuple(root.glob("node_modules/@openai/codex-*/vendor/*/bin/codex.exe"))
        if candidates:
            return str(candidates[0])
    raise RpcError("Codex CLI not found. Set CODEX_CLI in .codex/codex.local.env", 503)


def server_command() -> tuple[str, ...]:
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    config_file = codex_home / "config.toml"
    config = tomllib.loads(config_file.read_text(encoding="utf-8")) if config_file.exists() else {}
    disabled = (
        "shell_tool",
        "unified_exec",
        "apps",
        "plugins",
        "hooks",
        "multi_agent",
        "multi_agent_v2",
        "browser_use",
        "computer_use",
        "image_generation",
        "view_image",
        "code_mode",
        "code_mode_host",
        "skill_search",
        "memories",
        "goals",
        "sleep_tool",
    )
    overrides = (
        "project_doc_max_bytes=0",
        'web_search="disabled"',
        'model_provider="openai"',
        "features.skip_host_skill_discovery=true",
        *(f"features.{name}=false" for name in disabled),
        *(f"mcp_servers.{name}.enabled=false" for name in config.get("mcp_servers", {})),
    )
    return (resolve_cli(), "app-server", "--listen", "stdio://", *(arg for item in overrides for arg in ("-c", item)))


@dataclass(frozen=True, slots=True)
class ServerSettings:
    command: tuple[str, ...]
    cwd: Path
    timeout: float = 600

    @classmethod
    def local(cls) -> ServerSettings:
        workspace = Path(tempfile.gettempdir()) / "litellm-codex-workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        return cls(server_command(), workspace, float(os.environ.get("CODEX_TIMEOUT_S", "600")))


class RpcClient:
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self.ids = itertools.count(1)
        self.pending: deque[dict[str, JsonValue]] = deque()
        self.turn: tuple[str, str] | None = None

    async def send(self, message: Mapping[str, JsonValue]) -> None:
        if self.process.stdin is None:
            raise RpcError("Codex stdin is unavailable", 503)
        self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        await self.process.stdin.drain()

    async def receive(self) -> dict[str, JsonValue]:
        if self.process.stdout is None:
            raise RpcError("Codex stdout is unavailable", 503)
        line = await self.process.stdout.readline()
        if not line:
            raise RpcError("Codex app-server closed the connection before completing the request", 503)
        try:
            message = JSON_OBJECT.validate_json(line)
        except ValidationError as error:
            raise RpcError("Invalid JSON-RPC response from Codex") from error
        if "method" in message and "id" in message:
            await self.send({"id": message["id"], "error": {"code": -32601, "message": "Chat proxy has no tools"}})
            raise RpcError("Codex requested a tool or approval that this chat proxy does not support", 400)
        return message

    async def call(self, method: str, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        request_id = next(self.ids)
        await self.send({"id": request_id, "method": method, "params": params})
        while True:
            message = await self.receive()
            if message.get("id") != request_id:
                self.pending.append(message)
                continue
            if "error" in message:
                error = object_value(message["error"])
                status = 400 if error.get("code") == -32602 else 502
                raise RpcError(str(error.get("message", "Codex JSON-RPC request failed")), status)
            return object_value(message.get("result", {}))

    async def event(self) -> dict[str, JsonValue]:
        return self.pending.popleft() if self.pending else await self.receive()


@asynccontextmanager
async def connect(settings: ServerSettings) -> AsyncIterator[RpcClient]:
    process = await asyncio.create_subprocess_exec(
        *settings.command,
        cwd=settings.cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        limit=16 * 1024 * 1024,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    client = RpcClient(process)
    try:
        await client.call(
            "initialize",
            {
                "clientInfo": {"name": "litellm_codex_proxy", "version": "1.0.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        await client.send({"method": "initialized", "params": {}})
        yield client
    finally:
        if process.returncode is None:
            if client.turn:
                with suppress(OSError, RpcError):
                    await client.send(
                        {
                            "id": next(client.ids),
                            "method": "turn/interrupt",
                            "params": {
                                "threadId": client.turn[0],
                                "turnId": client.turn[1],
                            },
                        }
                    )
            with suppress(ProcessLookupError):
                process.terminate()
        await process.wait()
