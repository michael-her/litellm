import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import aclosing
from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter, ValidationError

from litellm import CustomLLM
from litellm.llms.custom_llm import CustomLLMError
from litellm.types.llms.openai import ChatCompletionUsageBlock
from litellm.types.utils import GenericStreamingChunk, ModelResponse, ModelResponseStream

from codex_rpc import RpcError, ServerSettings, connect, object_value


class TextPart(BaseModel):
    type: Literal["text"]
    text: str


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["system", "developer", "user", "assistant"]
    content: str | tuple[TextPart, ...]
    name: str | None = None

    def text(self) -> str:
        return self.content if isinstance(self.content, str) else "\n".join(part.text for part in self.content)

    def item(self) -> dict[str, JsonValue]:
        text = f"{self.name}: {self.text()}" if self.name else self.text()
        return {
            "type": "message",
            "role": self.role,
            "content": [
                {
                    "type": "output_text" if self.role == "assistant" else "input_text",
                    "text": text,
                }
            ],
        }


class Options(BaseModel):
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"] | None = None
    stop: str | tuple[str, ...] | None = None
    n: Literal[1] = 1
    tools: tuple[JsonValue, ...] = ()
    functions: tuple[JsonValue, ...] = ()
    response_format: JsonValue = None


class StopFilter:
    def __init__(self, stops: tuple[str, ...]) -> None:
        self.stops = tuple(stop for stop in stops if stop)
        self.buffer = ""
        self.stopped = False

    def feed(self, text: str, final: bool = False) -> str:
        self.buffer += text
        positions = tuple(self.buffer.find(stop) for stop in self.stops if stop in self.buffer)
        if positions:
            result = self.buffer[: min(positions)]
            self.buffer = ""
            self.stopped = True
            return result
        keep = (
            0
            if final
            else max(
                (
                    length
                    for stop in self.stops
                    for length in range(1, len(stop))
                    if self.buffer.endswith(stop[:length])
                ),
                default=0,
            )
        )
        result = self.buffer[:-keep] if keep else self.buffer
        self.buffer = self.buffer[-keep:] if keep else ""
        return result


def chunk(text: str, finished: bool = False, usage: ChatCompletionUsageBlock | None = None) -> GenericStreamingChunk:
    return {
        "text": text,
        "is_finished": finished,
        "finish_reason": "stop" if finished else "",
        "index": 0,
        "tool_use": None,
        "usage": usage,
    }


async def generate(
    settings: ServerSettings,
    model: str,
    messages: tuple[ChatMessage, ...],
    options: Options,
) -> AsyncIterator[GenericStreamingChunk]:
    if not messages or not any(message.text().strip() for message in messages):
        raise RpcError("Non-empty text messages are required", 400)
    if options.tools or options.functions or options.response_format is not None:
        raise RpcError("This Codex proxy supports text chat; tools and response_format are unsupported", 400)
    stop_filter = StopFilter((options.stop,) if isinstance(options.stop, str) else options.stop or ())
    async with asyncio.timeout(settings.timeout), connect(settings) as client:
        account = await client.call("account/read", {"refreshToken": False})
        if account.get("account") is None:
            raise RpcError("Codex is not logged in. Run codex login using your ChatGPT account", 401)
        result = await client.call(
            "thread/start",
            {
                "model": model.removeprefix("codex/"),
                "modelProvider": "openai",
                "cwd": str(settings.cwd),
                "ephemeral": True,
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "personality": "none",
                "baseInstructions": "You are a conversational assistant. Respond to the supplied conversation. "
                "Return only the assistant reply. Do not use tools or interact with files or the operating system.",
                "developerInstructions": "",
                "serviceName": "litellm-codex",
            },
        )
        thread_id = str(object_value(result["thread"])["id"])
        history = messages[:-1] if messages[-1].role == "user" else messages
        if history:
            await client.call("thread/inject_items", {"threadId": thread_id, "items": [m.item() for m in history]})
        turn = await client.call(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [
                    {
                        "type": "text",
                        "text": messages[-1].text()
                        if messages[-1].role == "user"
                        else "Continue the conversation with the next assistant reply.",
                    }
                ],
                "effort": options.reasoning_effort or "medium",
            },
        )
        turn_id = str(object_value(turn["turn"])["id"])
        client.turn = (thread_id, turn_id)
        usage: ChatCompletionUsageBlock | None = None
        seen: set[str] = set()
        while True:
            event = await client.event()
            params = object_value(event.get("params", {}))
            if params.get("threadId") != thread_id:
                continue
            if params.get("turnId", turn_id) != turn_id:
                continue
            match event.get("method"):
                case "item/agentMessage/delta":
                    seen.add(str(params["itemId"]))
                    text = stop_filter.feed(str(params["delta"]))
                    if text:
                        yield chunk(text)
                    if stop_filter.stopped:
                        yield chunk("", True, usage)
                        return
                case "item/completed":
                    item = object_value(params["item"])
                    if item.get("type") == "agentMessage" and str(item["id"]) not in seen:
                        text = stop_filter.feed(str(item.get("text", "")))
                        if text:
                            yield chunk(text)
                        if stop_filter.stopped:
                            yield chunk("", True, usage)
                            return
                case "thread/tokenUsage/updated":
                    tokens = object_value(object_value(params["tokenUsage"])["total"])
                    usage = {
                        "prompt_tokens": int(str(tokens["inputTokens"])),
                        "completion_tokens": int(str(tokens["outputTokens"])),
                        "total_tokens": int(str(tokens["totalTokens"])),
                    }
                case "turn/completed":
                    completed = object_value(params["turn"])
                    if completed.get("id") != turn_id:
                        continue
                    client.turn = None
                    if completed.get("status") != "completed":
                        error = object_value(completed.get("error") or {})
                        info = error.get("codexErrorInfo")
                        status = 429 if info == "usageLimitExceeded" else 502
                        raise RpcError(str(error.get("message", "Codex turn was interrupted")), status)
                    yield chunk(stop_filter.feed("", final=True), True, usage)
                    return


class CodexLLM(CustomLLM):
    def __init__(self, settings: ServerSettings | None = None) -> None:
        super().__init__()
        self.settings = settings

    async def _events(
        self,
        model: str,
        messages: object,
        optional_params: object = None,
        **kwargs: object,
    ) -> AsyncIterator[GenericStreamingChunk]:
        try:
            parsed = TypeAdapter(tuple[ChatMessage, ...]).validate_python(messages)
            options = Options.model_validate(optional_params or {})
            async with aclosing(generate(self.settings or ServerSettings.local(), model, parsed, options)) as stream:
                async for event in stream:
                    yield event
        except RpcError as error:
            raise CustomLLMError(error.status_code, str(error)) from error
        except ValidationError as error:
            raise CustomLLMError(400, "Unsupported message or parameter format; send text chat messages") from error
        except TimeoutError as error:
            raise CustomLLMError(504, "Codex request timed out (CODEX_TIMEOUT_S)") from error
        except OSError as error:
            raise CustomLLMError(503, f"Unable to communicate with Codex app-server: {error}") from error

    async def astreaming(
        self,
        model: str,
        messages: object,
        optional_params: object = None,
        **kwargs: object,
    ) -> AsyncIterator[ModelResponseStream]:
        response_id = f"chatcmpl-{uuid.uuid4()}"
        created: int | None = None
        first = True
        async with aclosing(self._events(model, messages, optional_params, **kwargs)) as events:
            async for event in events:
                if created is None:
                    created = int(time.time())
                if event["text"] or event["usage"] is not None:
                    yield ModelResponseStream(
                        id=response_id,
                        created=created,
                        model=model,
                        choices=[
                            {
                                "index": 0,
                                "delta": {"content": event["text"], **({"role": "assistant"} if first else {})},
                            }
                        ],
                        **({"usage": event["usage"]} if event["usage"] is not None else {}),
                    )
                    first = False
                if event["is_finished"]:
                    yield ModelResponseStream(
                        id=response_id,
                        created=created,
                        model=model,
                        choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    )

    def streaming(
        self,
        model: str,
        messages: object,
        optional_params: object = None,
        **kwargs: object,
    ) -> Iterator[ModelResponseStream]:
        with asyncio.Runner() as runner:
            stream = self.astreaming(model, messages, optional_params, **kwargs)
            try:
                while True:
                    try:
                        yield runner.run(anext(stream))
                    except StopAsyncIteration:
                        return
            finally:
                runner.run(stream.aclose())

    async def acompletion(
        self,
        model: str,
        messages: object,
        optional_params: object = None,
        **kwargs: object,
    ) -> ModelResponse:
        chunks = tuple([event async for event in self._events(model, messages, optional_params, **kwargs)])
        return ModelResponse(
            model=model,
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "".join(event["text"] for event in chunks)},
                    "finish_reason": "stop",
                }
            ],
            usage=chunks[-1].get("usage") if chunks else None,
        )

    def completion(
        self,
        model: str,
        messages: object,
        optional_params: object = None,
        **kwargs: object,
    ) -> ModelResponse:
        return asyncio.run(self.acompletion(model, messages, optional_params, **kwargs))


codex_handler = CodexLLM()
