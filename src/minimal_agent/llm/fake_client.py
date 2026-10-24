"""Scripted LLM implementation for deterministic tests."""

import asyncio
from collections.abc import AsyncIterator, Iterable

from minimal_agent.llm.base import LLMError, LLMRequest
from minimal_agent.llm.events import LLMEvent

type FakeResponse = Iterable[LLMEvent] | Exception


class FakeLLM:
    def __init__(self, responses: Iterable[FakeResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[LLMRequest] = []

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMEvent]:
        self.requests.append(request)
        if not self._responses:
            raise LLMError(
                "FAKE_SCRIPT_EXHAUSTED",
                "Fake LLM has no scripted response remaining",
                retryable=False,
            )
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        for event in response:
            await asyncio.sleep(0)
            yield event
