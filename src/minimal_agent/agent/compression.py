"""Session summary protocol and provider-neutral LLM implementation."""

import json
from collections.abc import Sequence
from typing import Protocol

from minimal_agent.llm import (
    LLMClient,
    LLMRequest,
    RefusalDone,
    ResponseCompleted,
    ResponseFailed,
    ResponseIncomplete,
    TextDelta,
    TextDone,
)
from minimal_agent.schemas import MessageRecord


class ContextCompressionError(RuntimeError):
    """Raised when the summary model cannot produce a usable summary."""

    code = "CONTEXT_COMPRESSION_FAILED"


class SessionSummarizer(Protocol):
    async def summarize(
        self,
        *,
        existing_summary: str | None,
        messages: Sequence[MessageRecord],
    ) -> str: ...


class LLMSessionSummarizer:
    """Create a Session summary through the provider-neutral streaming LLM protocol."""

    def __init__(
        self,
        llm: LLMClient,
        *,
        prompt: str,
        max_output_tokens: int | None = None,
    ) -> None:
        if not prompt.strip():
            raise ValueError("Summary prompt must not be empty")
        if max_output_tokens is not None and max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        self._llm = llm
        self._prompt = prompt.strip()
        self._max_output_tokens = max_output_tokens

    async def summarize(
        self,
        *,
        existing_summary: str | None,
        messages: Sequence[MessageRecord],
    ) -> str:
        payload = {
            "existing_summary": existing_summary,
            "completed_messages": [
                {
                    "seq": message.seq,
                    "role": message.role.value,
                    "kind": message.kind.value,
                    "content": message.content,
                }
                for message in messages
            ],
        }
        request = LLMRequest(
            instructions=self._prompt,
            input=[
                {
                    "role": "user",
                    "content": json.dumps(
                        payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        allow_nan=False,
                    ),
                }
            ],
            tools=[],
            tool_choice="none",
            max_output_tokens=self._max_output_tokens,
        )

        deltas: list[str] = []
        completed_text: str | None = None
        completed = False
        async for event in self._llm.stream(request):
            if isinstance(event, TextDelta):
                deltas.append(event.delta)
            elif isinstance(event, TextDone):
                completed_text = event.text
            elif isinstance(event, RefusalDone):
                raise ContextCompressionError("Summary request was refused")
            elif isinstance(event, ResponseFailed):
                raise ContextCompressionError(
                    f"Summary request failed ({event.error_code}): {event.message}"
                )
            elif isinstance(event, ResponseIncomplete):
                raise ContextCompressionError(
                    f"Summary response was incomplete: {event.reason or 'unknown reason'}"
                )
            elif isinstance(event, ResponseCompleted):
                completed = True

        if not completed:
            raise ContextCompressionError("Summary stream ended without completion")
        summary = "".join(deltas) if deltas else (completed_text or "")
        if not summary.strip():
            raise ContextCompressionError("Summary response was empty")
        return summary.strip()
