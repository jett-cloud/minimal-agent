from pathlib import Path

import httpx
import pytest

from minimal_agent.config import Settings
from minimal_agent.llm import FakeLLM
from minimal_agent.main import create_app
from minimal_agent.tools import ToolRegistry

_ROOT = Path(__file__).resolve().parents[2]


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_path=tmp_path / "orders-api.db",
        system_prompt_path=_ROOT / "prompts/system.md",
        summarize_prompt_path=_ROOT / "prompts/summarize.md",
        injected_llm_context_window_tokens=10_000,
        agent_shutdown_grace_seconds=0,
    )


@pytest.mark.asyncio
async def test_orders_api_crud_idempotency_and_version_conflicts(tmp_path: Path) -> None:
    # 验证订单 API CRUD、幂等重放和版本冲突。
    app = create_app(settings=_settings(tmp_path), llm=FakeLLM([]), tools=ToolRegistry())
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        payload = {
            "customer_name": "Alice",
            "product_name": "Keyboard",
            "quantity": 2,
            "unit_price": "199.90",
        }
        headers = {"Idempotency-Key": "api-create-0001"}
        created_response = await client.post("/v1/orders", json=payload, headers=headers)
        replay_response = await client.post("/v1/orders", json=payload, headers=headers)
        assert created_response.status_code == 201
        assert replay_response.status_code == 201
        created = created_response.json()
        assert replay_response.json() == created

        listed = await client.get("/v1/orders", params={"status": "pending"})
        fetched = await client.get(f"/v1/orders/{created['id']}")
        assert listed.json() == [created]
        assert fetched.json() == created

        updated_response = await client.patch(
            f"/v1/orders/{created['id']}",
            json={"expected_version": 1, "quantity": 3, "status": "confirmed"},
            headers={"Idempotency-Key": "api-update-0001"},
        )
        assert updated_response.status_code == 200
        updated = updated_response.json()
        assert updated["version"] == 2

        stale = await client.patch(
            f"/v1/orders/{created['id']}",
            json={"expected_version": 1, "quantity": 4},
            headers={"Idempotency-Key": "api-update-0002"},
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "ORDER_VERSION_CONFLICT"

        deleted = await client.delete(
            f"/v1/orders/{created['id']}",
            params={"expected_version": 2},
            headers={"Idempotency-Key": "api-delete-0001"},
        )
        replayed_delete = await client.delete(
            f"/v1/orders/{created['id']}",
            params={"expected_version": 2},
            headers={"Idempotency-Key": "api-delete-0001"},
        )
        assert deleted.status_code == 200
        assert replayed_delete.json() == deleted.json()
        assert (await client.get(f"/v1/orders/{created['id']}")).status_code == 404


@pytest.mark.asyncio
async def test_orders_api_validates_idempotency_key_and_key_reuse(tmp_path: Path) -> None:
    # 验证订单 API 校验幂等键及其复用冲突。
    app = create_app(settings=_settings(tmp_path), llm=FakeLLM([]), tools=ToolRegistry())
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        payload = {
            "customer_name": "Alice",
            "product_name": "Keyboard",
            "quantity": 1,
            "unit_price": "10.00",
        }
        assert (await client.post("/v1/orders", json=payload)).status_code == 422
        headers = {"Idempotency-Key": "api-conflict-0001"}
        assert (await client.post("/v1/orders", json=payload, headers=headers)).status_code == 201
        payload["quantity"] = 2
        conflict = await client.post("/v1/orders", json=payload, headers=headers)
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
