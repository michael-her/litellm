"""LiteLLM CustomLLM provider for Cursor SDK agent models."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Coroutine, Iterator, Optional, Union

import httpx

from litellm import CustomLLM
from litellm.llms.custom_llm import CustomLLMError
from litellm.types.utils import ModelResponse

_CURSOR_DIR = Path(__file__).resolve().parent
if str(_CURSOR_DIR) not in sys.path:
    sys.path.insert(0, str(_CURSOR_DIR))

from cursor_env import load_cursor_local_env  # noqa: E402

load_cursor_local_env()

try:
    from cursor_sdk import (
        Agent,
        AgentOptions,
        Client,
        CursorAgentError,
        CursorClient,
        LocalAgentOptions,
    )
    from cursor_sdk.types import UserMessage
    from cursor_sdk._bridge import BridgeEndpoint, parse_discovery_line
    from cursor_sdk._vendor import resolve_bridge_path
except ImportError as exc:  # pragma: no cover - optional dependency
    raise ImportError(
        "cursor-sdk is required for the Cursor provider. "
        "Install with: uv pip install cursor-sdk"
    ) from exc


def _message_content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                elif "text" in block:
                    parts.append(str(block["text"]))
        return "\n".join(p for p in parts if p)
    return str(content)


def messages_to_prompt(messages: list) -> str:
    lines: list[str] = []
    for message in messages:
        role = str(message.get("role", "user")).upper()
        text = _message_content_to_text(message.get("content"))
        if text:
            lines.append(f"{role}: {text}")
    return "\n\n".join(lines)


def _resolve_api_key(api_key: Optional[str]) -> str:
    key = api_key or os.environ.get("CURSOR_API_KEY")
    if not key:
        raise CustomLLMError(
            status_code=401,
            message=(
                "Cursor API key not found. Set CURSOR_API_KEY in "
                ".cursor/cursor.local.env or pass api_key in litellm_params."
            ),
        )
    return key


def _resolve_cwd(litellm_params: Optional[dict]) -> str:
    params = litellm_params or {}
    metadata = params.get("metadata") or {}
    for key in ("cursor_cwd", "cwd", "workspace"):
        value = params.get(key) or metadata.get(key)
        if value:
            return str(value)

    env_cwd = os.environ.get("CURSOR_CWD")
    if env_cwd:
        return env_cwd

    return str(Path(__file__).resolve().parents[1])


def _resolve_model(model: str) -> str:
    if not model:
        return os.environ.get("COORD_WAKE_MODEL", "composer-2.5")
    return model


def _build_agent_options(
    *,
    model: str,
    api_key: str,
    cwd: str,
    litellm_params: Optional[dict],
) -> AgentOptions:
    params = litellm_params or {}
    metadata = params.get("metadata") or {}

    local_opts: dict[str, Any] = {"cwd": cwd}
    for key in ("setting_sources", "sandbox", "env"):
        if key in params:
            local_opts[key] = params[key]
        elif key in metadata:
            local_opts[key] = metadata[key]

    return AgentOptions(
        api_key=api_key,
        model=_resolve_model(model),
        local=LocalAgentOptions(**local_opts),
    )


def _extract_result_text(result: Any) -> str:
    text = getattr(result, "result", "") or ""
    if isinstance(text, str) and text.strip():
        return text

    status = getattr(result, "status", "unknown")
    raise CustomLLMError(
        status_code=502,
        message=f"Cursor agent run finished with status={status!r} and empty result.",
    )


@contextmanager
def _launch_bridge_client_windows(workspace: str) -> Iterator[Client]:
    """Launch cursor-sdk-bridge on Windows without selector-based discovery."""
    argv = [resolve_bridge_path(), "--workspace", workspace]
    process = subprocess.Popen(
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    discovery = None
    deadline = time.monotonic() + 45
    try:
        if process.stderr is None:
            raise CustomLLMError(status_code=503, message="Cursor bridge stderr unavailable.")
        for line in process.stderr:
            discovery = parse_discovery_line(line)
            if discovery is not None:
                break
            if time.monotonic() > deadline:
                break
        if discovery is None:
            exit_code = process.poll()
            raise CustomLLMError(
                status_code=503,
                message=f"Cursor bridge failed to start (exit={exit_code}).",
            )
        endpoint = BridgeEndpoint.from_discovery(discovery)
        client = Client(endpoint)
        try:
            yield client
        finally:
            client.close()
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


@contextmanager
def _cursor_client(workspace: str) -> Iterator[Client]:
    bridge_url = os.environ.get("CURSOR_SDK_BRIDGE_URL")
    bridge_token = os.environ.get("CURSOR_SDK_BRIDGE_TOKEN") or os.environ.get(
        "CURSOR_SDK_BRIDGE_AUTH_TOKEN"
    )
    if bridge_url and bridge_token:
        with Client.connect(base_url=bridge_url, auth_token=bridge_token) as client:
            yield client
        return
    if sys.platform == "win32":
        with _launch_bridge_client_windows(workspace) as client:
            yield client
        return
    with CursorClient.launch_bridge(workspace=workspace) as client:
        yield client


def _invoke_cursor_agent(
    *,
    model: str,
    messages: list,
    api_key: Optional[str],
    litellm_params: Optional[dict],
) -> str:
    resolved_key = _resolve_api_key(api_key)
    cwd = _resolve_cwd(litellm_params)
    prompt = messages_to_prompt(messages)
    if not prompt.strip():
        raise CustomLLMError(status_code=400, message="Cursor provider requires non-empty messages.")

    options = _build_agent_options(
        model=model,
        api_key=resolved_key,
        cwd=cwd,
        litellm_params=litellm_params,
    )

    try:
        with _cursor_client(cwd) as client:
            run_result = Agent.prompt(
                UserMessage(text=prompt),
                options,
                client=client,
            )
    except CursorAgentError as exc:
        raise CustomLLMError(
            status_code=getattr(exc, "status_code", 500) or 500,
            message=f"Cursor SDK error: {exc}",
        ) from exc
    except OSError as exc:
        if getattr(exc, "winerror", None) == 10038 or "10038" in str(exc):
            raise CustomLLMError(
                status_code=503,
                message=(
                    "Cursor SDK local bridge failed to start on this host. "
                    "Set CURSOR_SDK_BRIDGE_URL and CURSOR_SDK_BRIDGE_TOKEN in "
                    ".cursor/cursor.local.env to attach to a running Cursor bridge, "
                    "or run the proxy on Linux/macOS."
                ),
            ) from exc
        raise

    if getattr(run_result, "status", None) == "error":
        raise CustomLLMError(
            status_code=502,
            message=f"Cursor agent run failed (run_id={getattr(run_result, 'id', '?')}).",
        )

    return _extract_result_text(run_result)


class CursorSDKLLM(CustomLLM):
    def _fill_model_response(
        self,
        *,
        model: str,
        model_response: ModelResponse,
        text: str,
    ) -> ModelResponse:
        model_response.model = f"cursor/{model}"
        model_response.created = int(time.time())
        model_response.choices[0].message.content = text  # type: ignore[index]
        model_response.choices[0].finish_reason = "stop"  # type: ignore[index]
        return model_response

    def completion(
        self,
        model: str,
        messages: list,
        api_base: str,
        custom_prompt_dict: dict,
        model_response: ModelResponse,
        print_verbose: Callable,
        encoding,
        api_key,
        logging_obj,
        optional_params: dict,
        acompletion=None,
        litellm_params=None,
        logger_fn=None,
        headers={},
        timeout: Optional[Union[float, httpx.Timeout]] = None,
        client=None,
    ) -> ModelResponse:
        text = _invoke_cursor_agent(
            model=model,
            messages=messages,
            api_key=api_key,
            litellm_params=litellm_params,
        )
        return self._fill_model_response(model=model, model_response=model_response, text=text)

    async def acompletion(
        self,
        model: str,
        messages: list,
        api_base: str,
        custom_prompt_dict: dict,
        model_response: ModelResponse,
        print_verbose: Callable,
        encoding,
        api_key,
        logging_obj,
        optional_params: dict,
        acompletion=None,
        litellm_params=None,
        logger_fn=None,
        headers={},
        timeout: Optional[Union[float, httpx.Timeout]] = None,
        client=None,
    ) -> Union[ModelResponse, Coroutine[Any, Any, ModelResponse]]:
        text = await asyncio.to_thread(
            _invoke_cursor_agent,
            model=model,
            messages=messages,
            api_key=api_key,
            litellm_params=litellm_params,
        )
        return self._fill_model_response(model=model, model_response=model_response, text=text)


cursor_handler = CursorSDKLLM()
