import httpx
import pytest

from minimal_agent.main import app


@pytest.mark.asyncio
async def test_health_returns_service_metadata() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "minimal-agent",
        "version": "0.1.0",
        "environment": "local",
    }
