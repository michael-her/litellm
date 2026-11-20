from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field, JsonValue, TypeAdapter

ROOT = Path(__file__).resolve().parent
AGENT = "litellm-antigravity"
LOGGER = logging.getLogger("antigravity")


class Failure(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message

    def __str__(self) -> str:
        return self.message


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cache_read_tokens: int = 0

    def block(self) -> dict[str, int]:
        prompt = self.input_tokens + self.cache_read_tokens
        return {
            "prompt_tokens": prompt,
            "completion_tokens": self.output_tokens,
            "total_tokens": prompt + self.output_tokens,
        }


class Step(BaseModel):
    step_type: str = ""
    text_delta: str = ""


class Result(BaseModel):
    status: str
    response: str = ""
    error: JsonValue = None
    denied_actions: tuple[JsonValue, ...] = ()
    usage: Usage = Field(default_factory=Usage)


class Event(BaseModel):
    event: str
    step_update: Step | None = None
    result: Result | None = None


@dataclass(frozen=True, slots=True)
class Delta:
    text: str


@dataclass(frozen=True, slots=True)
class Finished:
    response: str
    usage: Usage


@dataclass(frozen=True, slots=True)
class Settings:
    command: tuple[str, ...]
    env: Mapping[str, str]
    root: Path
    timeout: float = 600
    queue_timeout: float = 30
    concurrency: int = 2

    @classmethod
    def local(cls) -> Settings:
        exe = os.environ.get("ANTIGRAVITY_CLI", str(Path(os.environ["LOCALAPPDATA"]) / "agy/bin/agy.exe"))
        names = (
            "SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
            "HOME", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
            "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS",
        )
        return cls(
            (exe, *TypeAdapter(tuple[str, ...]).validate_json(os.environ.get("ANTIGRAVITY_CLI_ARGS", "[]"))),
            {key: os.environ[key] for key in names if key in os.environ}, ROOT / "state",
            float(os.environ.get("ANTIGRAVITY_TIMEOUT_S", "600")),
            float(os.environ.get("ANTIGRAVITY_QUEUE_TIMEOUT_S", "30")),
            int(os.environ.get("ANTIGRAVITY_MAX_CONCURRENCY", "2")),
        )


def classify(model: str, detail: str, default: int = 502) -> Failure:
    text = detail.lower()
    status = (
        429 if re.search(r"rate.?limit|quota|resource.?exhausted|\b429\b", text)
        else 401 if re.search(r"not logged|log.?in required|unauth|oauth|credential|\b401\b|authentication", text)
        else 404 if re.search(r"unknown model|model.*not found|invalid model", text)
        else 504 if re.search(r"timed? ?out|timeout", text)
        else default
    )
    return Failure(status, f"agy model={model}: {detail[-2000:]}")


def kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill.exe", "/PID", str(pid), "/T", "/F"], capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW, timeout=20,
        )
        return
    import signal
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def cleanup_request(
    workdir: Path, remove: Callable[[Path], None] = shutil.rmtree,
    delays: tuple[float, ...] = (0, 0.03, 0.1, 0.2),
) -> None:
    for index, delay in enumerate(delays):
        if delay:
            time.sleep(delay)
        try:
            remove(workdir)
            return
        except OSError as error:
            if not workdir.exists():
                return
            if index == len(delays) - 1:
                LOGGER.warning("agy request directory left after cleanup retries: %s (%s)", workdir, error)


class Client:
    def __init__(self, settings: Settings, cleanup: Callable[[Path], None] = cleanup_request) -> None:
        if settings.concurrency < 1 or settings.timeout <= 0 or settings.queue_timeout <= 0:
            raise ValueError("Antigravity concurrency and timeouts must be positive")
        self.settings = settings
        self.slots = threading.BoundedSemaphore(settings.concurrency)
        self.cleanup = cleanup

    async def run(self, model: str, prompt: str) -> AsyncIterator[Delta | Finished]:
        try:
            async with asyncio.timeout(self.settings.queue_timeout):
                while not self.slots.acquire(blocking=False):
                    await asyncio.sleep(0.025)
        except TimeoutError as error:
            raise Failure(429, f"agy model={model}: concurrency queue wait exceeded") from error
        try:
            async with asyncio.timeout(self.settings.timeout):
                async with aclosing(self._run(model, prompt)) as events:
                    async for event in events:
                        yield event
        except TimeoutError as error:
            raise Failure(504, f"agy model={model}: request timeout after {self.settings.timeout}s") from error
        except OSError as error:
            raise classify(model, str(error)) from error
        finally:
            self.slots.release()

    async def _run(self, model: str, prompt: str) -> AsyncIterator[Delta | Finished]:
        self.settings.root.mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix="req-", dir=self.settings.root))
        try:
            async with aclosing(self._process(model, prompt, workdir)) as events:
                async for event in events:
                    yield event
        finally:
            await asyncio.shield(asyncio.to_thread(self.cleanup, workdir))

    async def _process(self, model: str, prompt: str, workdir: Path) -> AsyncIterator[Delta | Finished]:
        cwd = workdir / "workspace"
        cwd.mkdir()
        stderr_path = workdir / "stderr.log"
        with stderr_path.open("wb") as stderr:
            process = await asyncio.create_subprocess_exec(
                *self.settings.command, "--input-format", "stream-json", "--output-format", "stream-json",
                "--print-timeout", "0s", "--model", model, "--agent", AGENT,
                "--disable-slash-commands", "--log-file", str(workdir / "agy.log"),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, cwd=cwd,
                env={**self.settings.env, "TEMP": str(workdir), "TMP": str(workdir)},
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                start_new_session=os.name != "nt", limit=16 * 1024 * 1024,
            )
        try:
            if process.stdin is None or process.stdout is None:
                raise Failure(502, f"agy model={model}: process pipes unavailable")
            process.stdin.write((json.dumps({"event": "user", "message": {"role": "user", "content": prompt}},
                                           ensure_ascii=False) + "\n").encode("utf-8"))
            await process.stdin.drain()
            process.stdin.close()
            async for line in process.stdout:
                try:
                    event = Event.model_validate_json(line)
                except ValueError as error:
                    tail = stderr_path.read_text(encoding="utf-8", errors="replace")[-500:]
                    raise classify(model, f"malformed agy output; stderr: {tail}") from error
                if "permission" in event.event or "tool" in event.event:
                    raise Failure(502, f"agy model={model}: forbidden tool/permission event")
                if event.step_update:
                    step = event.step_update
                    if "tool" in step.step_type or "permission" in step.step_type:
                        raise Failure(502, f"agy model={model}: forbidden tool/permission step")
                    if step.step_type == "agent_response" and step.text_delta:
                        yield Delta(step.text_delta)
                if event.event == "result" and event.result:
                    result = event.result
                    if result.denied_actions:
                        raise Failure(502, f"agy model={model}: forbidden permission action")
                    if result.status != "SUCCESS" or result.error:
                        raise classify(model, json.dumps(result.error) if result.error else result.status)
                    yield Finished(result.response, result.usage)
                    return
            await process.wait()
            tail = stderr_path.read_text(encoding="utf-8", errors="replace")[-1000:]
            raise classify(model, f"process exited {process.returncode} without result; stderr: {tail}")
        finally:
            if process.returncode is None:
                await asyncio.shield(asyncio.to_thread(kill_tree, process.pid))
            await process.wait()
