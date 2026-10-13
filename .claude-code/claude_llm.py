import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import aclosing
from dataclasses import dataclass
from itertools import groupby
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from litellm import (
    APIError,
    AuthenticationError,
    BadRequestError,
    CustomLLM,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)
from litellm.litellm_core_utils.prompt_templates.factory import DEFAULT_USER_CONTINUE_MESSAGE
from litellm.types.utils import ModelResponse, ModelResponseStream

from claude_cli import (
    CliSettings,
    Conversation,
    Effort,
    Failed,
    Finished,
    Generation,
    TextChunk,
    TokenUsage,
    Turn,
    run,
)

DEFAULT_SYSTEM = "You are Claude, a conversational assistant. Respond to the supplied conversation."


class TextPart(BaseModel):
    type: Literal["text"]
    text: str


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    role: Literal["system", "developer", "user", "assistant"]
    content: str | tuple[TextPart, ...]
    name: str | None = None
    tool_calls: None = None
    function_call: None = None

    def text(self) -> str:
        body = self.content if isinstance(self.content, str) else "\n".join(part.text for part in self.content)
        return f"{self.name}: {body}" if self.name and self.role in ("user", "assistant") else body


class Options(BaseModel):
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None = None
    max_tokens: int | None = Field(default=None, gt=0)
    max_completion_tokens: int | None = Field(default=None, gt=0)
    stop: str | tuple[str, ...] | None = None
    n: Literal[1] = 1
    tools: tuple[JsonValue, ...] = ()
    functions: tuple[JsonValue, ...] = ()
    response_format: JsonValue = None


@dataclass(frozen=True, slots=True)
class Request:
    conversation: Conversation
    generation: Generation
    stops: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StopScan:
    emit: str
    pending: str
    stopped: bool


def scan_stop(stops: tuple[str, ...], pending: str, text: str, final: bool) -> StopScan:
    buffer = pending + text
    hits = tuple(buffer.find(stop) for stop in stops if stop in buffer)
    if hits:
        return StopScan(buffer[: min(hits)], "", True)
    keep = (
        0
        if final
        else max(
            (length for stop in stops for length in range(1, len(stop)) if buffer.endswith(stop[:length])),
            default=0,
        )
    )
    return StopScan(buffer[: len(buffer) - keep], buffer[len(buffer) - keep :], False)


def build_conversation(messages: tuple[ChatMessage, ...]) -> Conversation | Failed:
    system = "\n\n".join(
        text for message in messages if message.role in ("system", "developer") and (text := message.text()).strip()
    )
    turns = tuple(
        Turn("user" if role == "user" else "assistant", "\n\n".join(message.text() for message in group))
        for role, group in groupby(
            (message for message in messages if message.role in ("user", "assistant") and message.text().strip()),
            key=lambda message: message.role,
        )
    )
    if not turns:
        return Failed(400, "At least one non-empty user or assistant message is required")
    history, prompt = (
        (turns[:-1], turns[-1].text) if turns[-1].role == "user" else (turns, DEFAULT_USER_CONTINUE_MESSAGE["content"])
    )
    leading = (Turn("user", "."),) if history and history[0].role == "assistant" else ()
    return Conversation(system or DEFAULT_SYSTEM, (*leading, *history), prompt)


def generation(model: str, options: Options) -> Generation:
    effort: Effort | None = (
        "low"
        if options.reasoning_effort == "minimal"
        else None
        if options.reasoning_effort in (None, "none")
        else options.reasoning_effort
    )
    return Generation(
        model.removeprefix("claude_code/"),
        effort,
        options.reasoning_effort != "none",
        options.max_completion_tokens or options.max_tokens,
    )


def prepare(model: str, messages: object, optional_params: object) -> Request | Failed:
    try:
        parsed = TypeAdapter(tuple[ChatMessage, ...]).validate_python(messages)
        options = Options.model_validate(optional_params or {})
    except ValidationError:
        return Failed(400, "Unsupported message or parameter format; send text chat messages")
    if options.tools or options.functions or options.response_format is not None:
        return Failed(400, "This Claude proxy supports text chat; tools and response_format are unsupported")
    conversation = build_conversation(parsed)
    if isinstance(conversation, Failed):
        return conversation
    stops = tuple(stop for stop in ((options.stop,) if isinstance(options.stop, str) else options.stop or ()) if stop)
    return Request(conversation, generation(model, options), stops)


def usage_block(usage: TokenUsage) -> dict[str, int]:
    return {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.prompt_tokens + usage.completion_tokens,
    }


def public_error(model: str, failure: Failed) -> Exception:
    provider = "claude_code"
    match failure.status_code:
        case 400:
            return BadRequestError(failure.message, model, provider)
        case 401:
            return AuthenticationError(failure.message, provider, model)
        case 429:
            return RateLimitError(failure.message, provider, model)
        case 503:
            return ServiceUnavailableError(failure.message, provider, model)
        case 504:
            return Timeout(failure.message, model, provider, exception_status_code=504)
        case status:
            return APIError(status, failure.message, provider, model)


class ClaudeCodeLLM(CustomLLM):
    def __init__(self, settings: CliSettings | None = None) -> None:
        super().__init__()
        self.settings = settings

    async def _events(
        self,
        model: str,
        messages: object,
        optional_params: object = None,
    ) -> AsyncIterator[TextChunk | Finished]:
        request = prepare(model, messages, optional_params)
        if isinstance(request, Failed):
            raise public_error(model, request)
        settings = self.settings or CliSettings.local()
        if isinstance(settings, Failed):
            raise public_error(model, settings)
        try:
            async with asyncio.timeout(settings.timeout):
                async with aclosing(run(settings, request.conversation, request.generation)) as outputs:
                    pending = ""
                    async for output in outputs:
                        match output:
                            case Failed():
                                raise public_error(model, output)
                            case TextChunk(text=text):
                                scanned = scan_stop(request.stops, pending, text, final=False)
                                pending = scanned.pending
                                if scanned.emit:
                                    yield TextChunk(scanned.emit)
                                if scanned.stopped:
                                    yield Finished("stop", None)
                                    return
                            case Finished():
                                tail = scan_stop(request.stops, pending, "", final=True).emit
                                if tail:
                                    yield TextChunk(tail)
                                yield output
                                return
        except TimeoutError as error:
            raise public_error(model, Failed(504, "Claude request timed out (CLAUDE_TIMEOUT_S)")) from error
        except OSError as error:
            raise public_error(model, Failed(503, f"Unable to run Claude CLI: {error}")) from error

    async def astreaming(
        self,
        model: str,
        messages: object,
        optional_params: object = None,
        **kwargs: object,
    ) -> AsyncIterator[ModelResponseStream]:
        response_id = f"chatcmpl-{uuid.uuid4()}"
        created = int(time.time())
        first = True
        async with aclosing(self._events(model, messages, optional_params)) as events:
            async for event in events:
                match event:
                    case TextChunk(text=text):
                        yield ModelResponseStream(
                            id=response_id,
                            created=created,
                            model=model,
                            choices=[
                                {"index": 0, "delta": {"content": text, **({"role": "assistant"} if first else {})}}
                            ],
                        )
                        first = False
                    case Finished(reason=reason, usage=usage):
                        if usage is not None:
                            yield ModelResponseStream(
                                id=response_id,
                                created=created,
                                model=model,
                                choices=[{"index": 0, "delta": {"content": ""}}],
                                usage=usage_block(usage),
                            )
                        yield ModelResponseStream(
                            id=response_id,
                            created=created,
                            model=model,
                            choices=[{"index": 0, "delta": {}, "finish_reason": reason}],
                        )

    def streaming(
        self,
        model: str,
        messages: object,
        optional_params: object = None,
        **kwargs: object,
    ) -> Iterator[ModelResponseStream]:
        with asyncio.Runner() as runner:
            stream = self.astreaming(model, messages, optional_params)
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
        events = tuple([event async for event in self._events(model, messages, optional_params)])
        finished = next(event for event in events if isinstance(event, Finished))
        return ModelResponse(
            model=model,
            choices=[
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "".join(event.text for event in events if isinstance(event, TextChunk)),
                    },
                    "finish_reason": finished.reason,
                }
            ],
            usage=usage_block(finished.usage) if finished.usage else None,
        )

    def completion(
        self,
        model: str,
        messages: object,
        optional_params: object = None,
        **kwargs: object,
    ) -> ModelResponse:
        return asyncio.run(self.acompletion(model, messages, optional_params))


claude_code_handler = ClaudeCodeLLM()
