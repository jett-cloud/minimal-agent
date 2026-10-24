"""Build model context from one durable Session and compact old completed turns."""

import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from minimal_agent.agent.compression import SessionSummarizer
from minimal_agent.llm import LLMRequest
from minimal_agent.schemas import MessageKind, MessageRecord, MessageRole, RunStatus
from minimal_agent.storage import (
    InvalidStateError,
    MessageRepository,
    RunRepository,
    SessionRepository,
)
from minimal_agent.tools import ToolRegistry

_TOOL_MENTION = re.compile(r"(?<![A-Za-z0-9_@-])@([A-Za-z0-9_-]{1,64})(?![A-Za-z0-9_-])")
_TERMINAL_RUN_STATUSES = frozenset({RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED})


class ContextDataError(RuntimeError):
    """Raised when durable messages cannot be converted into valid model input."""

    code = "CONTEXT_DATA_INVALID"


class ContextLimitExceeded(RuntimeError):
    """Raised when context remains at the configured threshold without safe candidates."""

    code = "CONTEXT_LIMIT_EXCEEDED"


class TokenCounter(Protocol):
    """Replaceable provider-aware or heuristic input token estimator."""

    def count_request(self, request: LLMRequest) -> int: ...


class HeuristicTokenCounter:
    """Dependency-free estimate suitable when a provider tokenizer is unavailable.

    Non-ASCII characters count as one token and ASCII text as roughly four characters per
    token. A small per-item overhead accounts for message and tool framing. Deployments that
    have an exact model tokenizer can replace this implementation through ``TokenCounter``.
    """

    def count_request(self, request: LLMRequest) -> int:
        serialized = json.dumps(
            {
                "instructions": request.instructions,
                "input": request.input,
                "tools": request.tools,
                "tool_choice": request.tool_choice,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        ascii_count = sum(character.isascii() for character in serialized)
        non_ascii_count = len(serialized) - ascii_count
        framing_overhead = 8 * (len(request.input) + len(request.tools) + 1)
        return max(1, math.ceil(ascii_count / 4) + non_ascii_count + framing_overhead)


@dataclass(frozen=True, slots=True)
class ToolMentions:
    requested: tuple[str, ...]
    unknown: tuple[str, ...]


def parse_tool_mentions(text: str, registered_names: Sequence[str]) -> ToolMentions:
    """Find exact, case-sensitive mentions while preserving first-occurrence order."""

    registered = set(registered_names)
    requested: list[str] = []
    unknown: list[str] = []
    for match in _TOOL_MENTION.finditer(text):
        name = match.group(1)
        destination = requested if name in registered else unknown
        if name not in destination:
            destination.append(name)
    return ToolMentions(requested=tuple(requested), unknown=tuple(unknown))


def load_prompt(path: Path) -> str:
    """Load a required non-empty UTF-8 prompt file."""

    try:
        prompt = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ContextDataError(f"Cannot read prompt file: {path}") from error
    if not prompt.strip():
        raise ContextDataError(f"Prompt file is empty: {path}")
    return prompt.strip()


@dataclass(frozen=True, slots=True)
class PreparedContext:
    request: LLMRequest
    estimated_input_tokens: int
    trigger_tokens: int
    summary_through_seq: int | None
    requested_tools: tuple[str, ...]
    unknown_tool_mentions: tuple[str, ...]
    compression_applied: bool = False


class ContextBuilder:
    """Rebuild a single Session context and compact only a safe, contiguous prefix."""

    def __init__(
        self,
        *,
        sessions: SessionRepository,
        messages: MessageRepository,
        runs: RunRepository,
        token_counter: TokenCounter,
        summarizer: SessionSummarizer,
        system_prompt: str,
        context_window_tokens: int,
        trigger_ratio: float = 0.80,
        recent_turns_to_keep: int = 5,
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
        reasoning_summary: str | None = None,
    ) -> None:
        if not system_prompt.strip():
            raise ValueError("System prompt must not be empty")
        if context_window_tokens < 1:
            raise ValueError("context_window_tokens must be configured explicitly")
        if not 0 < trigger_ratio < 1:
            raise ValueError("trigger_ratio must be between 0 and 1")
        if recent_turns_to_keep < 1:
            raise ValueError("recent_turns_to_keep must be positive")
        self._sessions = sessions
        self._messages = messages
        self._runs = runs
        self._token_counter = token_counter
        self._summarizer = summarizer
        self._system_prompt = system_prompt.strip()
        self._trigger_tokens = max(1, math.floor(context_window_tokens * trigger_ratio))
        self._recent_turns_to_keep = recent_turns_to_keep
        self._max_output_tokens = max_output_tokens
        self._reasoning_effort = reasoning_effort
        self._reasoning_summary = reasoning_summary

    async def prepare(
        self,
        *,
        session_id: str,
        current_user_message_id: str,
        tools: ToolRegistry,
    ) -> PreparedContext:
        """Build context, performing at most one summary advancement when required."""

        context = self._build(
            session_id=session_id,
            current_user_message_id=current_user_message_id,
            tools=tools,
        )
        if context.estimated_input_tokens < self._trigger_tokens:
            return context

        session = self._sessions.require(session_id)
        candidates = self._compression_candidates(
            session_id=session_id,
            after_seq=session.summary_through_seq or 0,
        )
        if not candidates:
            raise ContextLimitExceeded(
                "Context reached the configured threshold and no completed old turns can be "
                "compressed"
            )

        summary = await self._summarizer.summarize(
            existing_summary=session.summary,
            messages=candidates,
        )
        from_seq = session.summary_from_seq or candidates[0].seq
        through_seq = candidates[-1].seq
        try:
            self._sessions.update_summary(
                session_id,
                summary=summary,
                from_seq=from_seq,
                through_seq=through_seq,
            )
        except InvalidStateError:
            # Another builder may have advanced the same Session while this LLM request ran.
            # Accept that winner only if it covers at least our candidate range.
            latest = self._sessions.require(session_id)
            if latest.summary_through_seq is None or latest.summary_through_seq < through_seq:
                raise

        rebuilt = self._build(
            session_id=session_id,
            current_user_message_id=current_user_message_id,
            tools=tools,
        )
        if rebuilt.estimated_input_tokens >= self._trigger_tokens:
            raise ContextLimitExceeded(
                "Context remains above the configured threshold after safe compression"
            )
        return replace(rebuilt, compression_applied=True)

    def _build(
        self,
        *,
        session_id: str,
        current_user_message_id: str,
        tools: ToolRegistry,
    ) -> PreparedContext:
        session = self._sessions.require(session_id)
        current = self._messages.get(current_user_message_id)
        if current is None or current.session_id != session_id or current.role != MessageRole.USER:
            raise ContextDataError("Current user message does not belong to this Session")
        current_run = self._runs.get(current.run_id) if current.run_id else None
        if (
            current_run is None
            or current_run.status != RunStatus.RUNNING
            or current_run.user_message_id != current.id
        ):
            raise ContextDataError("Current user message does not own the running Run")
        user_text = _required_text(current)
        mentions = parse_tool_mentions(user_text, tools.names)
        selected_names = mentions.requested or tools.names
        instructions = self._system_prompt
        if mentions.requested:
            requested = ", ".join(f"@{name}" for name in mentions.requested)
            instructions += (
                "\n\nApplication directive for this turn: the user explicitly requested "
                f"{requested}. Use the requested tool; if required arguments are missing, ask "
                "for them instead of guessing. No unlisted tool is available for this turn."
            )

        input_items: list[dict[str, object]] = []
        if session.summary:
            input_items.append(
                {
                    "role": "system",
                    "content": (
                        "Session summary covering messages through sequence "
                        f"{session.summary_through_seq}:\n{session.summary}"
                    ),
                }
            )
        after_seq = session.summary_through_seq or 0
        remaining = self._load_all_messages(session_id, after_seq=after_seq)
        if current.seq <= after_seq or all(message.id != current.id for message in remaining):
            raise ContextDataError("Current user message is incorrectly covered or missing")
        for message in remaining:
            input_items.extend(_message_to_input(message))

        request = LLMRequest(
            instructions=instructions,
            input=input_items,
            tools=tools.openai_tools(selected_names),
            tool_choice="auto",
            max_output_tokens=self._max_output_tokens,
            reasoning_effort=self._reasoning_effort,
            reasoning_summary=self._reasoning_summary,
        )
        return PreparedContext(
            request=request,
            estimated_input_tokens=self._token_counter.count_request(request),
            trigger_tokens=self._trigger_tokens,
            summary_through_seq=session.summary_through_seq,
            requested_tools=mentions.requested,
            unknown_tool_mentions=mentions.unknown,
        )

    def _compression_candidates(
        self,
        *,
        session_id: str,
        after_seq: int,
    ) -> list[MessageRecord]:
        terminal_runs = [
            run
            for run in self._runs.list_for_session(session_id)
            if run.status in _TERMINAL_RUN_STATUSES
        ]
        old_runs = terminal_runs[: -self._recent_turns_to_keep]
        old_run_ids = {run.id for run in old_runs}
        if not old_run_ids:
            return []

        candidates: list[MessageRecord] = []
        for message in self._load_all_messages(session_id, after_seq=after_seq):
            if message.run_id not in old_run_ids:
                break
            candidates.append(message)
        return candidates

    def _load_all_messages(self, session_id: str, *, after_seq: int) -> list[MessageRecord]:
        records: list[MessageRecord] = []
        cursor = after_seq
        while True:
            batch = self._messages.list_after(session_id, after_seq=cursor, limit=1000)
            records.extend(batch)
            if len(batch) < 1000:
                return records
            cursor = batch[-1].seq


def _required_text(message: MessageRecord) -> str:
    text = message.content.get("text")
    if not isinstance(text, str):
        raise ContextDataError(f"Message {message.id} has no string text field")
    return text


def _message_to_input(message: MessageRecord) -> list[dict[str, object]]:
    if message.kind is MessageKind.MESSAGE:
        if message.role not in {MessageRole.USER, MessageRole.ASSISTANT}:
            raise ContextDataError(f"Text message {message.id} has an invalid role")
        return [{"role": message.role.value, "content": _required_text(message)}]

    if message.kind is MessageKind.TOOL_CALL:
        call_id = message.content.get("call_id")
        name = message.content.get("name")
        arguments = message.content.get("arguments")
        if not isinstance(call_id, str) or not isinstance(name, str):
            raise ContextDataError(f"Tool call message {message.id} is malformed")
        if isinstance(arguments, dict):
            arguments = json.dumps(
                arguments,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        if not isinstance(arguments, str):
            raise ContextDataError(f"Tool call message {message.id} has invalid arguments")
        items: list[dict[str, object]] = []
        continuation_items = message.content.get("continuation_items", [])
        if not isinstance(continuation_items, list) or not all(
            isinstance(item, dict) for item in continuation_items
        ):
            raise ContextDataError(f"Tool call message {message.id} has invalid continuation data")
        items.extend(continuation_items)
        function_call: dict[str, object] = {
            "type": "function_call",
            "call_id": call_id,
            "name": name,
            "arguments": arguments,
        }
        reasoning_content = message.content.get("reasoning_content")
        if reasoning_content is not None:
            if not isinstance(reasoning_content, str):
                raise ContextDataError(
                    f"Tool call message {message.id} has invalid reasoning content"
                )
            function_call["reasoning_content"] = reasoning_content
        items.append(function_call)
        return items

    if message.kind is MessageKind.TOOL_RESULT:
        call_id = message.content.get("call_id")
        if not isinstance(call_id, str):
            raise ContextDataError(f"Tool result message {message.id} is malformed")
        output = message.content.get("output", message.content.get("result"))
        if output is None:
            raise ContextDataError(f"Tool result message {message.id} has no output")
        if not isinstance(output, str):
            output = json.dumps(
                output,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        return [{"type": "function_call_output", "call_id": call_id, "output": output}]

    if message.kind is MessageKind.SYSTEM_ERROR:
        return [
            {
                "role": "system",
                "content": "Previous structured system error: "
                + json.dumps(
                    message.content,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ),
            }
        ]

    raise ContextDataError(f"Unsupported message kind: {message.kind}")
