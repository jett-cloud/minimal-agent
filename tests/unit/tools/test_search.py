from pathlib import Path

import pytest

from minimal_agent.tools import (
    MainboardCatalog,
    MainboardDatasetError,
    ToolRegistry,
    create_mainboard_search_tool,
)

FIXTURE_PATH = Path(__file__).parents[2] / "fixtures" / "mainboards.json"
RUNTIME_DATA_PATH = Path(__file__).parents[3] / "data" / "mainboards.json"


@pytest.fixture
def catalog() -> MainboardCatalog:
    return MainboardCatalog.from_path(FIXTURE_PATH)


def test_runtime_dataset_is_valid_and_has_balanced_coverage() -> None:
    catalog = MainboardCatalog.from_path(RUNTIME_DATA_PATH)

    assert len(catalog.search("ASUS", limit=20)) == 4
    assert len(catalog.search("MSI", limit=20)) == 4
    assert len(catalog.search("Gigabyte", limit=20)) == 4
    assert len(catalog.search("ASRock", limit=20)) == 4


def test_exact_model_and_alias_queries_rank_deterministically(
    catalog: MainboardCatalog,
) -> None:
    exact = catalog.search("ROG Alpha B650E-I", limit=5)
    alias = catalog.search("Alpha ITX", limit=5)

    assert exact[0]["id"] == "asus-rog-alpha-b650e-i"
    assert alias[0]["id"] == "asus-rog-alpha-b650e-i"
    assert exact[0]["match_score"] >= 1000
    assert catalog.search("Prime Alpha", limit=5)[0]["id"] == "asus-prime-alpha-b650"
    assert catalog.search("wifi", limit=20) == catalog.search("wifi", limit=20)


def test_brand_filter_and_spec_queries(catalog: MainboardCatalog) -> None:
    asus_b650 = catalog.search("B650", brand="asus", limit=10)
    ddr4_micro_atx = catalog.search("DDR4 Micro-ATX", limit=10)
    wifi_6e = catalog.search("Wi-Fi 6E", limit=10)

    assert [result["brand"] for result in asus_b650] == ["ASUS", "ASUS"]
    assert [result["id"] for result in ddr4_micro_atx] == ["asrock-pro-delta-b760m-d4-wifi"]
    assert {result["id"] for result in wifi_6e} == {
        "asus-rog-alpha-b650e-i",
        "gigabyte-aorus-gamma-z790",
        "msi-mag-beta-b650m-wifi",
    }


def test_limit_and_empty_results(catalog: MainboardCatalog) -> None:
    assert len(catalog.search("wifi", limit=2)) == 2
    assert catalog.search("TRX50 workstation", limit=5) == []
    assert catalog.search("B650", brand="Gigabyte", limit=5) == []


@pytest.mark.asyncio
async def test_search_tool_exports_strict_schema_and_returns_full_records() -> None:
    tool = create_mainboard_search_tool(FIXTURE_PATH, timeout_seconds=1)
    registry = ToolRegistry([tool])

    schema = registry.openai_tools()[0]
    result = await registry.execute(
        "search",
        {"query": "B650 WiFi", "brand": "MSI", "limit": 1},
    )

    assert schema["parameters"]["required"] == ["query", "brand", "limit"]
    assert result.ok is True
    assert result.data["count"] == 1
    assert result.data["results"][0]["id"] == "msi-mag-beta-b650m-wifi"
    assert result.data["results"][0]["source"]["url"].startswith("https://")


@pytest.mark.asyncio
async def test_search_tool_validates_limit_and_blank_filters() -> None:
    registry = ToolRegistry([create_mainboard_search_tool(FIXTURE_PATH)])

    invalid_limit = await registry.execute(
        "search",
        {"query": "B650", "brand": None, "limit": 0},
    )
    blank_brand = await registry.execute(
        "search",
        {"query": "B650", "brand": "---", "limit": None},
    )

    assert invalid_limit.error is not None
    assert invalid_limit.error.code == "INVALID_ARGUMENTS"
    assert blank_brand.error is not None
    assert blank_brand.error.code == "INVALID_ARGUMENTS"


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ("{not-json", "Invalid motherboard dataset"),
        ("[]", None),
        ('[{"id":"incomplete"}]', "Invalid motherboard dataset"),
    ],
)
def test_dataset_validation(
    tmp_path: Path,
    contents: str,
    message: str | None,
) -> None:
    path = tmp_path / "mainboards.json"
    path.write_text(contents, encoding="utf-8")

    if message is None:
        assert MainboardCatalog.from_path(path).search("anything") == []
    else:
        with pytest.raises(MainboardDatasetError, match=message):
            MainboardCatalog.from_path(path)


def test_dataset_rejects_duplicate_ids(tmp_path: Path) -> None:
    contents = FIXTURE_PATH.read_text(encoding="utf-8")
    first_record = contents[contents.index("{") : contents.index("},\n  {") + 1]
    path = tmp_path / "duplicates.json"
    path.write_text(f"[{first_record},{first_record}]", encoding="utf-8")

    with pytest.raises(MainboardDatasetError, match="duplicate ids"):
        MainboardCatalog.from_path(path)
