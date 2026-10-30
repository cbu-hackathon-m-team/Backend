import asyncio

import httpx

from ossguard.api import create_app


async def test_api_accepts_polls_and_rejects_unconfirmed(repository, snapshot, fake):
    app = create_app(repository, {"node-red": fake})
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:
            assert (await client.get("/health/ready")).status_code == 200
            body = snapshot.model_dump(mode="json")
            body["confirmed"] = False
            assert (await client.post("/api/validations", json=body)).status_code == 422
            body["confirmed"] = True
            accepted = await client.post("/api/validations", json=body)
            assert accepted.status_code == 202
            run_id = accepted.json()["id"]
            for _ in range(100):
                response = await client.get(f"/api/validations/{run_id}")
                if response.json()["status"] == "COMPLETED":
                    break
                await asyncio.sleep(0.02)
            assert response.json()["report"]["comparisons"][0]["verdict"] == "REGRESSION_SUSPECTED"
            assert (await client.get("/api/validations/not-a-uuid")).status_code == 422
