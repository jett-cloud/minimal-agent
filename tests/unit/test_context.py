from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from minimal_agent.agent import (
    ContextBuilder,
    ContextDataError,
    ContextLimitExceeded,
    HeuristicTokenCounter,
    LLMSessionSummarizer,
    load_prompt,
    parse_tool_mentions,
)
from minimal_agent.llm import (
    FakeLLM,
    ResponseCompleted,
    ResponseStarted,
    TextDelta,
    TextDone,
)
from minimal_agent.schemas import MessageKind, MessageRecord, MessageRole
from minimal_agent.storage import Database, MessageRepository, RunRepository, SessionRepository
from minimal_agent.tools import CALCULATOR_TOOL, ToolDefinition, ToolRegistry


class _NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str


async def _noop(_arguments: _NoArguments) -> None:
    return None


OTHER_TOOL = ToolDefinition(
    name="other",
    description="A second test tool.",
    args_model=_NoArguments,
    handler=_noop,
)


class _RecordingSummarizer:
    def __init__(self, result: str = "durable summary") -> None:
        self.result = result
        self.calls: list[tuple[str | None, tuple[MessageRecord, ...]]] = []

    async def summarize(
        self,
        *,
        existing_summary: str | None,
        messages: Sequence[MessageRecord],
    ) -> str:
        self.calls.append((existing_summary, tuple(messages)))
        return self.result


class _FixedCounter:
    def __init__(self, count: int) -> None:
        self.count = count

    def count_request(self, _request: object) -> int:
        return self.count


class _SummaryAwareCounter:
    def count_request(self, request: object) -> int:
        input_items = request.input  # type: ignore[attr-defined]
        has_summary = bool(input_items and input_items[0].get("role") == "system")
        return 20 if has_summary else 90


def _repositories(
    database: Database,
) -> tuple[SessionRepository, MessageRepository, RunRepository]:
    return SessionRepository(database), MessageRepository(database), RunRepository(database)


def _complete_turn(
    session_id: str,
    question: str,
    answer: str,
    messages: MessageRepository,
    runs: RunRepository,
) -> None:
    user = messages.append(
        session_id,
        role=MessageRole.USER,
        kind=MessageKind.MESSAGE,
        content={"text": question},
    )
    run = runs.create(session_id, user_message_id=user.id)
    assistant = messages.append(
        session_id,
        run_id=run.id,
        role=MessageRole.ASSISTANT,
        kind=MessageKind.MESSAGE,
        content={"text": answer},
    )
    runs.complete(run.id, final_message_id=assistant.id)


def _start_turn(
    session_id: str,
    text: str,
    messages: MessageRepository,
    runs: RunRepository,
) -> MessageRecord:
    user = messages.append(
        session_id,
        role=MessageRole.USER,
        kind=MessageKind.MESSAGE,
        content={"text": text},
    )
    runs.create(session_id, user_message_id=user.id)
    return user


def _builder(
    database: Database,
    *,
    counter: object,
    summarizer: _RecordingSummarizer,
    window: int = 100,
    keep: int = 5,
) -> ContextBuilder:
    sessions, messages, runs = _repositories(database)
    return ContextBuilder(
        sessions=sessions,
        messages=messages,
        runs=runs,
        token_counter=counter,  # type: ignore[arg-type]
        summarizer=summarizer,
        system_prompt="system contract",
        context_window_tokens=window,
        recent_turns_to_keep=keep,
    )


def test_tool_mentions_are_exact_case_sensitive_and_deduplicated() -> None:
    mentions = parse_tool_mentions(
        "@calculator then @weather and @calculator; ignore x@search and @Weather",
        ("calculator", "weather", "search"),
    )

    assert mentions.requested == ("calculator", "weather")
    assert mentions.unknown == ("Weather",)


@pytest.mark.asyncio
async def test_context_is_isolated_to_the_requested_session(database: Database) -> None:
    sessions, messages, runs = _repositories(database)
    first = sessions.create(session_id="first")
    second = sessions.create(session_id="second")
    _complete_turn(first.id, "first-history", "first-answer", messages, runs)
    current = _start_turn(first.id, "first-current", messages, runs)
    messages.append(
        second.id,
        role=MessageRole.USER,
        kind=MessageKind.MESSAGE,
        content={"text": "second-secret"},
    )
    builder = _builder(
        database,
        counter=_FixedCounter(1),
        summarizer=_RecordingSummarizer(),
    )

    prepared = await builder.prepare(
        session_id=first.id,
        current_user_message_id=current.id,
        tools=ToolRegistry(),
    )

    contents = [item.get("content") for item in prepared.request.input]
    assert contents == ["first-history", "first-answer", "first-current"]
    assert "second-secret" not in contents


@pytest.mark.asyncio
async def test_registered_tool_mention_restricts_tools_and_unknown_is_plain_text(
    database: Database,
) -> None:
    sessions, messages, runs = _repositories(database)
    session = sessions.create()
    current = _start_turn(
        session.id,
        "请用 @calculator 计算，并保留普通文本 @missing。",
        messages,
        runs,
    )
    builder = _builder(
        database,
        counter=_FixedCounter(1),
        summarizer=_RecordingSummarizer(),
    )

    prepared = await builder.prepare(
        session_id=session.id,
        current_user_message_id=current.id,
        tools=ToolRegistry([CALCULATOR_TOOL, OTHER_TOOL]),
    )

    assert prepared.requested_tools == ("calculator",)
    assert prepared.unknown_tool_mentions == ("missing",)
    assert [tool["name"] for tool in prepared.request.tools] == ["calculator"]
    assert "explicitly requested @calculator" in prepared.request.instructions
    assert prepared.request.input[-1]["content"].endswith("@missing。")


@pytest.mark.asyncio
async def test_compression_keeps_five_recent_complete_turns_and_advances_once(
    database: Database,
) -> None:
    sessions, messages, runs = _repositories(database)
    session = sessions.create()
    for index in range(6):
        _complete_turn(session.id, f"q{index}", f"a{index}", messages, runs)
    current = _start_turn(session.id, "current", messages, runs)
    summarizer = _RecordingSummarizer()
    builder = _builder(
        database,
        counter=_SummaryAwareCounter(),
        summarizer=summarizer,
    )

    prepared = await builder.prepare(
        session_id=session.id,
        current_user_message_id=current.id,
        tools=ToolRegistry(),
    )

    updated = sessions.require(session.id)
    assert prepared.compression_applied is True
    assert updated.summary == "durable summary"
    assert (updated.summary_from_seq, updated.summary_through_seq) == (1, 2)
    assert [message.content["text"] for message in summarizer.calls[0][1]] == ["q0", "a0"]
    remaining_text = [
        item["content"]
        for item in prepared.request.input
        if item.get("role") in {"user", "assistant"}
    ]
    assert remaining_text == [
        "q1",
        "a1",
        "q2",
        "a2",
        "q3",
        "a3",
        "q4",
        "a4",
        "q5",
        "a5",
        "current",
    ]

    repeated = await builder.prepare(
        session_id=session.id,
        current_user_message_id=current.id,
        tools=ToolRegistry(),
    )
    assert repeated.compression_applied is False
    assert len(summarizer.calls) == 1
    assert sessions.require(session.id).summary_through_seq == 2


@pytest.mark.asyncio
async def test_running_turn_is_never_compressed_and_limit_error_is_structured(
    database: Database,
) -> None:
    sessions, messages, runs = _repositories(database)
    session = sessions.create()
    current = _start_turn(session.id, "x" * 1000, messages, runs)
    summarizer = _RecordingSummarizer()
    builder = _builder(
        database,
        counter=_FixedCounter(80),
        summarizer=summarizer,
    )

    with pytest.raises(ContextLimitExceeded) as captured:
        await builder.prepare(
            session_id=session.id,
            current_user_message_id=current.id,
            tools=ToolRegistry(),
        )

    assert captured.value.code == "CONTEXT_LIMIT_EXCEEDED"
    assert summarizer.calls == []
    assert sessions.require(session.id).summary is None


@pytest.mark.asyncio
async def test_context_replays_tool_call_and_result_after_current_user(database: Database) -> None:
    sessions, messages, runs = _repositories(database)
    session = sessions.create()
    current = _start_turn(session.id, "calculate", messages, runs)
    run_id = runs.list_for_session(session.id)[0].id
    messages.append(
        session.id,
        run_id=run_id,
        role=MessageRole.ASSISTANT,
        kind=MessageKind.TOOL_CALL,
        content={"call_id": "call_1", "name": "calculator", "arguments": {"expression": "1+1"}},
    )
    messages.append(
        session.id,
        run_id=run_id,
        role=MessageRole.TOOL,
        kind=MessageKind.TOOL_RESULT,
        content={"call_id": "call_1", "result": {"ok": True, "data": "2"}},
    )
    builder = _builder(
        database,
        counter=_FixedCounter(1),
        summarizer=_RecordingSummarizer(),
    )

    prepared = await builder.prepare(
        session_id=session.id,
        current_user_message_id=current.id,
        tools=ToolRegistry([CALCULATOR_TOOL]),
    )

    assert prepared.request.input[1] == {
        "type": "function_call",
        "call_id": "call_1",
        "name": "calculator",
        "arguments": '{"expression":"1+1"}',
    }
    assert prepared.request.input[2] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": '{"ok":true,"data":"2"}',
    }


@pytest.mark.asyncio
async def test_current_message_must_belong_to_session(database: Database) -> None:
    sessions, messages, runs = _repositories(database)
    first = sessions.create()
    second = sessions.create()
    current = _start_turn(first.id, "hello", messages, runs)
    builder = _builder(
        database,
        counter=_FixedCounter(1),
        summarizer=_RecordingSummarizer(),
    )

    with pytest.raises(ContextDataError):
        await builder.prepare(
            session_id=second.id,
            current_user_message_id=current.id,
            tools=ToolRegistry(),
        )


@pytest.mark.asyncio
async def test_current_message_must_own_a_running_run(database: Database) -> None:
    sessions, messages, runs = _repositories(database)
    session = sessions.create()
    old_user = messages.append(
        session.id,
        role=MessageRole.USER,
        kind=MessageKind.MESSAGE,
        content={"text": "already complete"},
    )
    run = runs.create(session.id, user_message_id=old_user.id)
    answer = messages.append(
        session.id,
        run_id=run.id,
        role=MessageRole.ASSISTANT,
        kind=MessageKind.MESSAGE,
        content={"text": "done"},
    )
    runs.complete(run.id, final_message_id=answer.id)
    builder = _builder(
        database,
        counter=_FixedCounter(1),
        summarizer=_RecordingSummarizer(),
    )

    with pytest.raises(ContextDataError, match="running Run"):
        await builder.prepare(
            session_id=session.id,
            current_user_message_id=old_user.id,
            tools=ToolRegistry(),
        )


@pytest.mark.asyncio
async def test_concurrent_summary_winner_prevents_duplicate_coverage(database: Database) -> None:
    sessions, messages, runs = _repositories(database)
    session = sessions.create()
    for index in range(6):
        _complete_turn(session.id, f"q{index}", f"a{index}", messages, runs)
    current = _start_turn(session.id, "current", messages, runs)

    class _RacingSummarizer:
        async def summarize(
            self,
            *,
            existing_summary: str | None,
            messages: Sequence[MessageRecord],
        ) -> str:
            assert existing_summary is None
            sessions.update_summary(
                session.id,
                summary="concurrent winner",
                from_seq=messages[0].seq,
                through_seq=messages[-1].seq,
            )
            return "stale loser"

    builder = ContextBuilder(
        sessions=sessions,
        messages=messages,
        runs=runs,
        token_counter=_SummaryAwareCounter(),
        summarizer=_RacingSummarizer(),
        system_prompt="system contract",
        context_window_tokens=100,
    )

    prepared = await builder.prepare(
        session_id=session.id,
        current_user_message_id=current.id,
        tools=ToolRegistry(),
    )

    assert prepared.compression_applied is True
    assert sessions.require(session.id).summary == "concurrent winner"
    assert sessions.require(session.id).summary_through_seq == 2


def test_heuristic_counter_counts_non_ascii_and_tool_schema() -> None:
    counter = HeuristicTokenCounter()
    from minimal_agent.llm import LLMRequest

    short = LLMRequest(instructions="x", input=[{"role": "user", "content": "hello"}])
    rich = LLMRequest(
        instructions="中文上下文",
        input=[{"role": "user", "content": "你好世界"}],
        tools=ToolRegistry([CALCULATOR_TOOL]).openai_tools(),
    )

    assert counter.count_request(rich) > counter.count_request(short) > 0


def test_checked_in_prompts_are_non_empty_and_describe_required_contracts() -> None:
    system_prompt = load_prompt(Path("prompts/system.md"))
    summary_prompt = load_prompt(Path("prompts/summarize.md"))

    assert "@calculator" in system_prompt
    assert "exactly one action" in system_prompt
    assert "user preferences" in summary_prompt
    assert "important tool results" in summary_prompt


@pytest.mark.asyncio
async def test_llm_summarizer_collects_deltas_without_duplicating_done_text() -> None:
    llm = FakeLLM(
        [
            [
                ResponseStarted(response_id="response_1"),
                TextDelta(item_id="item_1", delta="compact "),
                TextDelta(item_id="item_1", delta="summary"),
                TextDone(item_id="item_1", text="compact summary"),
                ResponseCompleted(response_id="response_1"),
            ]
        ]
    )
    summarizer = LLMSessionSummarizer(llm, prompt="summarize")

    result = await summarizer.summarize(existing_summary=None, messages=[])

    assert result == "compact summary"
    assert llm.requests[0].tools == []
    assert llm.requests[0].tool_choice == "none"
