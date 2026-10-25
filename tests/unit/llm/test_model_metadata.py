from types import SimpleNamespace

import pytest

from minimal_agent.llm import ModelContextWindowError, OpenAIModelContextWindow


class _Page:
    def __init__(self, data: list[object], next_page: "_Page | None" = None) -> None:
        self.data = data
        self._next_page = next_page

    def has_next_page(self) -> bool:
        return self._next_page is not None

    async def get_next_page(self) -> "_Page":
        if self._next_page is None:
            raise RuntimeError("No next page")
        return self._next_page


class _ModelsResource:
    def __init__(self, page: _Page) -> None:
        self.page = page
        self.calls = 0

    async def list(self) -> _Page:
        self.calls += 1
        return self.page


@pytest.mark.asyncio
async def test_model_context_window_finds_target_across_pages_and_caches() -> None:
    # 验证跨页查找模型上下文窗口并缓存结果。
    second = _Page(
        [
            SimpleNamespace(
                id="target", model_dump=lambda **_: {"id": "target", "context_window": 1_048_576}
            )
        ]
    )
    resource = _ModelsResource(_Page([{"id": "other", "context_window": 32_000}], second))
    resolver = OpenAIModelContextWindow(resource, model="target")

    assert await resolver.get() == 1_048_576
    assert await resolver.get() == 1_048_576
    assert resource.calls == 1


@pytest.mark.asyncio
async def test_model_context_window_accepts_compatible_alias() -> None:
    # 验证模型上下文窗口接受兼容别名。
    resolver = OpenAIModelContextWindow(
        _ModelsResource(_Page([{"id": "target", "context_length": 128_000}])),
        model="target",
    )

    assert await resolver.get() == 128_000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"id": "target"},
        {"id": "target", "context_window": 0},
        {"id": "target", "context_window": "128000"},
    ],
)
async def test_model_context_window_rejects_invalid_target_metadata(
    payload: dict[str, object],
) -> None:
    # 验证拒绝无效的目标模型元数据。
    resolver = OpenAIModelContextWindow(_ModelsResource(_Page([payload])), model="target")

    with pytest.raises(ModelContextWindowError, match="does not expose"):
        await resolver.get()


@pytest.mark.asyncio
async def test_model_context_window_rejects_missing_model() -> None:
    # 验证目标模型缺失时返回错误。
    resolver = OpenAIModelContextWindow(
        _ModelsResource(_Page([{"id": "other", "context_window": 32_000}])),
        model="target",
    )

    with pytest.raises(ModelContextWindowError, match="was not found"):
        await resolver.get()
