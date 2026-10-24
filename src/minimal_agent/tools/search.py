"""Validated local motherboard catalogue and deterministic search tool."""

import re
import unicodedata
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from minimal_agent.tools.base import ToolDefinition, ToolSideEffect

_WHITESPACE = re.compile(r"\s+")
_NON_ALPHANUMERIC = re.compile(r"[^\w]+", flags=re.UNICODE)


def normalize_search_text(value: str) -> str:
    """Normalize text without depending on locale or fuzzy-search libraries."""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    return _WHITESPACE.sub(" ", _NON_ALPHANUMERIC.sub(" ", normalized)).strip()


class MemorySpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["DDR4", "DDR5"]
    slots: Annotated[int, Field(ge=1, le=8)]
    max_capacity_gb: Annotated[int, Field(ge=1)] | None
    max_speed_mt_s: Annotated[int, Field(ge=1)] | None
    ecc_support: str | None


class ExpansionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pcie_x16_slots: Annotated[int, Field(ge=0, le=8)]
    m2_slots: Annotated[int, Field(ge=0, le=16)]
    sata_ports: Annotated[int, Field(ge=0, le=16)]


class NetworkSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ethernet: str | None
    wifi: str | None
    bluetooth: str | None


class SourceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    url: HttpUrl
    verified_at: date


class MainboardRecord(BaseModel):
    """One version-independent, searchable motherboard record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{2,127}$")]
    brand: Annotated[str, Field(min_length=1, max_length=64)]
    model: Annotated[str, Field(min_length=1, max_length=160)]
    aliases: list[Annotated[str, Field(min_length=1, max_length=160)]]
    chipset: Annotated[str, Field(min_length=1, max_length=64)]
    socket: Annotated[str, Field(min_length=1, max_length=64)]
    form_factor: Literal["ATX", "Micro-ATX", "Mini-ITX"]
    cpu_support: list[Annotated[str, Field(min_length=1, max_length=160)]]
    memory: MemorySpec
    expansion: ExpansionSpec
    network: NetworkSpec
    audio: str | None
    features: list[Annotated[str, Field(min_length=1, max_length=160)]]
    source: SourceSpec


class MainboardSearchArguments(BaseModel):
    """Strict function-call arguments; optional values are required as JSON null."""

    model_config = ConfigDict(extra="forbid")

    query: Annotated[str, Field(min_length=1, max_length=200)]
    brand: Annotated[str, Field(max_length=64)] | None
    limit: Annotated[int, Field(ge=1, le=20)] | None

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        value = value.strip()
        if not normalize_search_text(value):
            raise ValueError("query must contain searchable letters or digits")
        return value

    @field_validator("brand")
    @classmethod
    def validate_brand(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not normalize_search_text(value):
            raise ValueError("brand must contain searchable letters or digits")
        return value


class MainboardDatasetError(ValueError):
    """Raised when a catalogue cannot be read or does not match its schema."""


_CATALOG_ADAPTER = TypeAdapter(list[MainboardRecord])


class MainboardCatalog:
    """An immutable in-memory catalogue loaded and validated once at startup."""

    def __init__(self, records: list[MainboardRecord]) -> None:
        ids = [record.id for record in records]
        if len(ids) != len(set(ids)):
            raise MainboardDatasetError("Motherboard dataset contains duplicate ids")
        self._records = tuple(records)

    @classmethod
    def from_path(cls, path: Path) -> "MainboardCatalog":
        try:
            raw_data = path.read_bytes()
        except OSError as error:
            raise MainboardDatasetError(f"Cannot read motherboard dataset: {path}") from error

        try:
            records = _CATALOG_ADAPTER.validate_json(raw_data)
        except ValidationError as error:
            raise MainboardDatasetError(
                f"Invalid motherboard dataset: {path} ({error.error_count()} validation errors)"
            ) from error
        return cls(records)

    def search(
        self,
        query: str,
        *,
        brand: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, object]]:
        query_text = normalize_search_text(query)
        query_tokens = query_text.split()
        brand_filter = normalize_search_text(brand) if brand is not None else None
        matches: list[tuple[int, str, str, str, MainboardRecord]] = []

        for record in self._records:
            if brand_filter is not None and normalize_search_text(record.brand) != brand_filter:
                continue
            score = _score_record(record, query_text, query_tokens)
            if score is None:
                continue
            matches.append(
                (
                    score,
                    normalize_search_text(record.brand),
                    normalize_search_text(record.model),
                    record.id,
                    record,
                )
            )

        matches.sort(key=lambda match: (-match[0], match[1], match[2], match[3]))
        return [
            {"match_score": score, **record.model_dump(mode="json")}
            for score, _, _, _, record in matches[:limit]
        ]


def _score_record(
    record: MainboardRecord,
    query_text: str,
    query_tokens: list[str],
) -> int | None:
    aliases = [normalize_search_text(alias) for alias in record.aliases]
    searchable_fields: list[tuple[int, list[str]]] = [
        (120, [normalize_search_text(record.model), *aliases]),
        (90, [normalize_search_text(record.brand)]),
        (75, [normalize_search_text(record.chipset)]),
        (70, [normalize_search_text(record.socket)]),
        (65, [normalize_search_text(record.form_factor)]),
        (
            60,
            [
                normalize_search_text(record.memory.type),
                normalize_search_text(f"{record.memory.slots} DIMM slots"),
                normalize_search_text(f"{record.memory.max_capacity_gb or ''} GB"),
                normalize_search_text(f"{record.memory.max_speed_mt_s or ''} MT/s"),
                normalize_search_text(record.memory.ecc_support or ""),
            ],
        ),
        (45, [normalize_search_text(item) for item in record.cpu_support]),
        (
            40,
            [
                normalize_search_text(record.network.ethernet or ""),
                normalize_search_text(record.network.wifi or ""),
                normalize_search_text(record.network.bluetooth or ""),
            ],
        ),
        (35, [normalize_search_text(item) for item in record.features]),
        (
            30,
            [
                normalize_search_text(f"{record.expansion.pcie_x16_slots} PCIe x16 slots"),
                normalize_search_text(f"{record.expansion.m2_slots} M.2 slots"),
                normalize_search_text(f"{record.expansion.sata_ports} SATA ports"),
            ],
        ),
        (20, [normalize_search_text(record.audio or "")]),
    ]

    exact_identifiers = {
        normalize_search_text(record.id),
        normalize_search_text(record.model),
        *aliases,
    }
    score = 1000 if query_text in exact_identifiers else 0
    if query_text == normalize_search_text(record.brand):
        score += 700
    if any(query_text in value for value in searchable_fields[0][1]):
        score += 350

    for token in query_tokens:
        token_score = max(
            (
                weight
                for weight, values in searchable_fields
                if any(token in value for value in values)
            ),
            default=0,
        )
        if token_score == 0:
            return None
        score += token_score
    return score


def create_mainboard_search_tool(
    dataset_path: Path,
    *,
    timeout_seconds: float = 10.0,
) -> ToolDefinition[MainboardSearchArguments]:
    """Load a catalogue once and create the registry-compatible search tool."""

    catalog = MainboardCatalog.from_path(dataset_path)

    def search_mainboards(arguments: MainboardSearchArguments) -> dict[str, object]:
        limit = arguments.limit if arguments.limit is not None else 5
        results = catalog.search(arguments.query, brand=arguments.brand, limit=limit)
        return {
            "query": arguments.query,
            "brand": arguments.brand,
            "count": len(results),
            "results": results,
        }

    return ToolDefinition(
        name="search",
        description=(
            "Search the local motherboard catalogue by model, brand, chipset, socket, form "
            "factor, memory, networking, or feature. Use it for motherboard specifications."
        ),
        args_model=MainboardSearchArguments,
        handler=search_mainboards,
        timeout_seconds=timeout_seconds,
        side_effect=ToolSideEffect.READ,
    )
