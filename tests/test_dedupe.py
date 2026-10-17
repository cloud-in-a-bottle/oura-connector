import asyncio
import json

import httpx
from litestar import Litestar
from litestar.testing import TestClient

from openhost_oura import db, dedupe, oura
from openhost_oura.service_api import service_routes

from test_backfill import FakeClient, FakeResp
from test_workouts import LIFT, RIDE, RUN

OWN_ID = "oura-app-id"
PROVIDERS = [
    {"app_id": OWN_ID, "app_name": "oura", "status": "running"},
    {"app_id": "apple-id", "app_name": "apple-health", "status": "running"},
    {"app_id": "stopped-id", "app_name": "stopped", "status": "stopped"},
]
# Overlaps the ride (13:00-14:00Z) by its last 15 minutes.
APPLE_RIDE = {"id": "a-1", "start": "2025-03-01T13:45:00+00:00", "end": "2025-03-01T14:30:00+00:00"}
# Ends exactly when the run (07:00-07:25Z) starts: touching isn't overlapping.
APPLE_WALK = {"id": "a-2", "start": "2025-03-02T06:30:00+00:00", "end": "2025-03-02T07:00:00+00:00"}


def _router(apple_workouts, calls, fail_with=None):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer app-token"
        if request.url.path == "/api/services/v2/providers":
            return httpx.Response(200, json=PROVIDERS)  # the router returns a bare list
        assert request.url.path == "/api/services/v2/call/health/v1/workouts"
        assert request.headers["X-OpenHost-Provider"] == "apple-id"
        if fail_with:
            return httpx.Response(fail_with, json={"error": "nope"})
        return httpx.Response(200, json={"count": len(apple_workouts), "data": apple_workouts})
    return httpx.MockTransport(handler)


def _setup(monkeypatch, tmp_path, transport, enabled=True):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("OPENHOST_ROUTER_URL", "http://router")
    monkeypatch.setenv("OPENHOST_APP_TOKEN", "app-token")
    monkeypatch.setenv("OPENHOST_APP_ID", OWN_ID)
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))

    async def run():
        await db.init_db()
        await db.set_config("dedupe_workouts", "1" if enabled else "")
        await oura._sync_workouts(FakeClient([FakeResp(200, data=[RIDE, RUN, LIFT])]), {}, "2025-03-01", "2025-04-01")
    asyncio.run(run())


def _suppressed():
    async def run():
        async with db.connect() as conn:
            rows = await (await conn.execute("SELECT source_id, suppressed_by FROM workouts ORDER BY source_id")).fetchall()
        return {r[0]: r[1] for r in rows}
    return asyncio.run(run())


def _listed_ids():
    with TestClient(Litestar(route_handlers=service_routes)) as client:
        return [w["id"] for w in client.get("/api/v1/workouts").json()["data"]], client.get("/api/v1/workouts/w-ride").status_code


def test_refresh_suppresses_overlaps_only(monkeypatch, tmp_path):
    calls = []
    _setup(monkeypatch, tmp_path, _router([APPLE_RIDE, APPLE_WALK], calls))
    asyncio.run(dedupe.refresh_all())

    assert _suppressed() == {"w-ride": "apple-health:a-1", "w-run": None, "w-lift": None}
    # Only the running, non-self provider is queried for workouts.
    workout_calls = [c for c in calls if c.url.path.endswith("/workouts")]
    assert workout_calls and all(c.headers["X-OpenHost-Provider"] == "apple-id" for c in workout_calls)
    assert asyncio.run(db.get_config("dedupe_error")) == ""


def test_service_hides_suppressed_only_while_enabled(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, _router([APPLE_RIDE], []))
    asyncio.run(dedupe.refresh_all())

    assert _listed_ids() == (["w-run", "w-lift"], 404)
    asyncio.run(db.set_config("dedupe_workouts", ""))
    assert _listed_ids() == (["w-ride", "w-run", "w-lift"], 200)


def test_refresh_clears_stale_suppression(monkeypatch, tmp_path):
    workouts = [APPLE_RIDE]
    _setup(monkeypatch, tmp_path, _router(workouts, []))
    asyncio.run(dedupe.refresh_all())
    assert _suppressed()["w-ride"] == "apple-health:a-1"

    workouts.clear()  # the other provider deleted it
    asyncio.run(dedupe.refresh_all())
    assert _suppressed()["w-ride"] is None


def test_refresh_failure_keeps_existing_suppression(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, _router([APPLE_RIDE], []))
    asyncio.run(dedupe.refresh_all())

    monkeypatch.undo()
    _setup(monkeypatch, tmp_path, _router([], [], fail_with=403))
    # _setup re-ingests (upsert), which must not reset suppression.
    assert _suppressed()["w-ride"] == "apple-health:a-1"
    asyncio.run(dedupe.refresh_all())

    assert _suppressed()["w-ride"] == "apple-health:a-1"
    assert "denied" in asyncio.run(db.get_config("dedupe_error"))


def test_refresh_noop_when_disabled(monkeypatch, tmp_path):
    calls = []
    _setup(monkeypatch, tmp_path, _router([APPLE_RIDE], calls), enabled=False)
    asyncio.run(dedupe.refresh_all())
    assert calls == []
    assert _suppressed()["w-ride"] is None


def test_refresh_fetches_in_windows(monkeypatch, tmp_path):
    calls = []
    _setup(monkeypatch, tmp_path, _router([], calls))
    from datetime import datetime, timezone
    asyncio.run(dedupe.refresh(datetime(2020, 1, 1, tzinfo=timezone.utc), datetime(2022, 6, 1, tzinfo=timezone.utc)))
    windows = [(c.url.params["start"], c.url.params["end"]) for c in calls if c.url.path.endswith("/workouts")]
    assert len(windows) == 3
    assert windows[0][0].startswith("2019-12-31")  # one-day margin before the range
    assert all(windows[i][1] == windows[i + 1][0] for i in range(len(windows) - 1))
    assert windows[-1][1].startswith("2022-06-01")
