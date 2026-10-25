from datetime import UTC, datetime

from minimal_agent.schemas import StreamEventRecord
from minimal_agent.streaming import reconstruct_reasoning


def _event(
    event_id: int,
    event_type: str,
    payload: dict[str, object],
) -> StreamEventRecord:
    return StreamEventRecord(
        id=event_id,
        run_id="run_1",
        seq=event_id,
        event_type=event_type,
        payload=payload,
        source_seq=event_id,
        created_at=datetime.now(UTC),
    )


def test_reconstruct_reasoning_prefers_done_text_and_hides_duplicate_lifecycle() -> None:
    # 验证重建推理优先使用完成文本并隐藏重复生命周期。
    history = reconstruct_reasoning(
        [
            _event(
                1,
                "reasoning.started",
                {"item_id": "reason_1", "source": "lifecycle", "content_type": "activity"},
            ),
            _event(
                2,
                "reasoning.delta",
                {
                    "item_id": "reason_1",
                    "source": "reasoning_text",
                    "content_type": "raw",
                    "delta": "分",
                },
            ),
            _event(
                3,
                "reasoning.delta",
                {
                    "item_id": "reason_1",
                    "source": "reasoning_text",
                    "content_type": "raw",
                    "delta": "析",
                },
            ),
            _event(
                4,
                "reasoning.done",
                {
                    "item_id": "reason_1",
                    "source": "reasoning_text",
                    "content_type": "raw",
                    "text": "完整分析",
                    "completed": True,
                },
            ),
        ]
    )

    assert len(history) == 1
    assert history[0].model_dump() == {
        "item_id": "reason_1",
        "source": "reasoning_text",
        "content_type": "raw",
        "text": "完整分析",
        "completed": True,
        "first_event_id": 2,
        "last_event_id": 4,
    }


def test_reconstruct_reasoning_keeps_activity_when_no_text_is_available() -> None:
    # 验证没有推理文本时仍保留活动记录。
    history = reconstruct_reasoning(
        [
            _event(
                1,
                "reasoning.started",
                {"item_id": "reason_1", "source": "lifecycle", "content_type": "activity"},
            ),
            _event(
                2,
                "reasoning.done",
                {
                    "item_id": "reason_1",
                    "source": "lifecycle",
                    "content_type": "activity",
                    "text": None,
                    "completed": True,
                },
            ),
        ]
    )

    assert [(item.content_type, item.text, item.completed) for item in history] == [
        ("activity", "", True)
    ]
