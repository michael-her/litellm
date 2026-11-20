import asyncio
import bisect
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import aclosing
from itertools import accumulate
from typing import Literal

from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from litellm import APIConnectionError, AuthenticationError, BadRequestError, CustomLLM, NotFoundError, RateLimitError, Timeout
from litellm.types.utils import ModelResponse, ModelResponseStream

from antigravity_cli import Client, Delta, Failure, Finished, Settings

MAX_PROMPT_BYTES = 180_000
TRANSCRIPT_HEADER = "Conversation transcript (one JSON message per line):\n"
TRANSCRIPT_FOOTER = "\nContinue with the assistant reply."


class TextPart(BaseModel):
    type: Literal["text"]
    text: str


class Message(BaseModel):
    role: Literal["system", "developer", "user", "assistant"]
    content: str | tuple[TextPart, ...]
    name: str | None = None
    tool_calls: None = None
    function_call: None = None

    def text(self) -> str:
        return self.content if isinstance(self.content, str) else "\n".join(part.text for part in self.content)


class Options(BaseModel):
    tools: tuple[JsonValue, ...] = ()
    functions: tuple[JsonValue, ...] = ()
    tool_choice: JsonValue = None
    function_call: JsonValue = None


def omission_notice(count: int) -> str:
    return json.dumps({
        "role": "system",
        "content": f"Proxy context notice: {count} earlier messages were omitted at whole-message boundaries "
        "to fit the agy input limit. System/developer instructions, opening turns, and the latest user turn "
        "are intact. Do not infer the omitted content.",
    }, separators=(",", ":"))


def transcript(lines: tuple[str, ...], omitted: int = 0) -> str:
    note = (omission_notice(omitted),) if omitted else ()
    return TRANSCRIPT_HEADER + "\n".join((*note, *lines)) + TRANSCRIPT_FOOTER


def fit_transcript(messages: tuple[Message, ...], lines: tuple[str, ...], byte_limit: int) -> str:
    full = transcript(lines)
    original_bytes = len(full.encode("utf-8"))
    if original_bytes <= byte_limit:
        return full
    conversation = tuple(i for i, message in enumerate(messages) if message.role in ("user", "assistant"))
    latest_user = max((i for i, message in enumerate(messages) if message.role == "user"), default=len(messages) - 1)
    required = frozenset((
        *(i for i, message in enumerate(messages) if message.role in ("system", "developer")),
        *conversation[:2], *range(latest_user, len(messages)),
    ))
    removable = tuple(i for i in range(len(messages)) if i not in required)
    saved_bytes = tuple(accumulate(len(lines[i].encode("utf-8")) + 1 for i in removable))
    notice_bytes = len(omission_notice(len(removable)).encode("utf-8")) + 1
    count = bisect.bisect_left(saved_bytes, original_bytes + notice_bytes - byte_limit) + 1
    if count > len(removable):
        raise Failure(400, f"agy cannot fit intact system/developer messages, opening turns, and the latest user turn "
                      f"within its safe {byte_limit}-byte UTF-8 transcript budget; shorten the required messages")
    omitted = frozenset(removable[:count])
    result = transcript(tuple(line for i, line in enumerate(lines) if i not in omitted), count)
    logging.getLogger("antigravity").warning(
        "agy context: omitted %d whole earlier messages; UTF-8 transcript %d -> %d bytes (budget %d)",
        count, original_bytes, len(result.encode("utf-8")), byte_limit,
    )
    return result


def render(messages: object, optional_params: object = None, byte_limit: int = MAX_PROMPT_BYTES) -> str:
    try:
        parsed = TypeAdapter(tuple[Message, ...]).validate_python(messages)
        options = Options.model_validate(optional_params or {})
    except ValidationError as error:
        raise Failure(400, "agy accepts text messages only; image/audio/tool messages are unsupported") from error
    if options.tools or options.functions or options.tool_choice is not None or options.function_call is not None:
        raise Failure(400, "agy does not support tools or function calling")
    if not parsed or not any(message.text().strip() for message in parsed):
        raise Failure(400, "agy requires a non-empty conversation")
    lines = tuple(
        json.dumps({"role": message.role, **({"name": message.name} if message.name else {}), "content": message.text()},
                   ensure_ascii=False, separators=(",", ":"))
        for message in parsed
    )
    return fit_transcript(parsed, lines, byte_limit)


def public_error(model: str, error: Failure) -> Exception:
    match error.status:
        case 400:
            return BadRequestError(error.message, model, "antigravity")
        case 401:
            return AuthenticationError(error.message, "antigravity", model)
        case 404:
            return NotFoundError(error.message, model, "antigravity")
        case 429:
            return RateLimitError(error.message, "antigravity", model)
        case 504:
            return Timeout(error.message, model, "antigravity", exception_status_code=504)
        case _:
            return APIConnectionError(error.message, "antigravity", model)


class AntigravityLLM(CustomLLM):
    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__()
        self.client = Client(settings or Settings.local())

    async def _events(self, model: str, messages: object, optional_params: object) -> AsyncIterator[Delta | Finished]:
        try:
            prompt = render(messages, optional_params)
            async with aclosing(self.client.run(model.removeprefix("antigravity/"), prompt)) as events:
                async for event in events:
                    yield event
        except Failure as error:
            raise public_error(model, error) from error

    async def acompletion(
        self, model: str, messages: object, optional_params: object = None, **kwargs: object,
    ) -> ModelResponse:
        events = tuple([event async for event in self._events(model, messages, optional_params)])
        result = next(event for event in events if isinstance(event, Finished))
        return ModelResponse(
            model=model,
            choices=[{"index": 0, "message": {"role": "assistant", "content": result.response}, "finish_reason": "stop"}],
            usage=result.usage.block(),
        )

    async def astreaming(
        self, model: str, messages: object, optional_params: object = None, **kwargs: object,
    ) -> AsyncIterator[ModelResponseStream]:
        response_id = "chatcmpl-" + uuid.uuid4().hex
        created = int(time.time())
        async with aclosing(self._events(model, messages, optional_params)) as events:
            async for event in events:
                if isinstance(event, Delta):
                    yield ModelResponseStream(
                        id=response_id, created=created, model=model,
                        choices=[{"index": 0, "delta": {"role": "assistant", "content": event.text}}],
                    )
                else:
                    yield ModelResponseStream(
                        id=response_id, created=created, model=model,
                        choices=[{"index": 0, "delta": {}}], usage=event.usage.block(),
                    )
                    yield ModelResponseStream(
                        id=response_id, created=created, model=model,
                        choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    )

    def completion(self, model: str, messages: object, optional_params: object = None, **kwargs: object) -> ModelResponse:
        return asyncio.run(self.acompletion(model, messages, optional_params, **kwargs))

    def streaming(
        self, model: str, messages: object, optional_params: object = None, **kwargs: object,
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


antigravity_handler = AntigravityLLM()
