"""Discover and validate model metadata from an OpenAI-compatible models API."""

import asyncio
import inspect
from typing import Any, Protocol

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError


class ModelContextWindowError(RuntimeError):
    """A stable, secret-free failure while resolving a model context window."""

    code = "MODEL_CONTEXT_WINDOW_UNAVAILABLE"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ModelContextWindow(Protocol):
    async def get(self) -> int: ...


class _ProviderModelMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    id: str = Field(min_length=1)
    context_window: int = Field(
        gt=0,
        validation_alias=AliasChoices(
            "context_window",
            "context_length",
            "max_context_tokens",
        ),
    )


class StaticModelContextWindow:
    """Fixed window used only by explicitly injected LLM implementations."""

    def __init__(self, tokens: int) -> None:
        if tokens < 1:
            raise ValueError("Model context window must be positive")
        self._tokens = tokens

    async def get(self) -> int:
        return self._tokens


class OpenAIModelContextWindow:
    """Resolve one model's window from a paginated OpenAI-compatible models API."""

    def __init__(self, models_resource: Any, *, model: str, max_pages: int = 100) -> None:
        if not model.strip():
            raise ValueError("Model name must not be empty")
        if max_pages < 1:
            raise ValueError("max_pages must be positive")
        self._models_resource = models_resource
        self._model = model.strip()
        self._max_pages = max_pages
        self._cached: int | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> int:
        if self._cached is not None:
            return self._cached
        async with self._lock:
            if self._cached is None:
                self._cached = await self._resolve()
            return self._cached

    async def _resolve(self) -> int:
        try:
            page = await self._models_resource.list()
        except Exception as error:
            raise ModelContextWindowError("The model catalogue could not be retrieved") from error

        for _ in range(self._max_pages):
            data = getattr(page, "data", None)
            if not isinstance(data, list):
                raise ModelContextWindowError("The model catalogue response is invalid")
            for raw_model in data:
                raw_id = (
                    raw_model.get("id")
                    if isinstance(raw_model, dict)
                    else getattr(raw_model, "id", None)
                )
                if raw_id != self._model:
                    continue
                payload = _model_payload(raw_model)
                try:
                    metadata = _ProviderModelMetadata.model_validate(payload)
                except ValidationError as error:
                    raise ModelContextWindowError(
                        f"Model {self._model!r} does not expose a valid context window"
                    ) from error
                return metadata.context_window

            has_next_page = getattr(page, "has_next_page", None)
            if not callable(has_next_page) or not has_next_page():
                break
            get_next_page = getattr(page, "get_next_page", None)
            if not callable(get_next_page):
                raise ModelContextWindowError("The model catalogue pagination is invalid")
            try:
                next_page = get_next_page()
                page = await next_page if inspect.isawaitable(next_page) else next_page
            except Exception as error:
                raise ModelContextWindowError(
                    "The next model catalogue page could not be retrieved"
                ) from error
        else:
            raise ModelContextWindowError("The model catalogue exceeded its page limit")

        raise ModelContextWindowError(f"Model {self._model!r} was not found in the catalogue")


def _model_payload(raw_model: object) -> object:
    if isinstance(raw_model, dict):
        return raw_model
    model_dump = getattr(raw_model, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="python")
    return raw_model
