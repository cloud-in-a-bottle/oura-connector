import asyncio

import httpx
from litestar import Litestar
from litestar.testing import TestClient

from openhost_oura import db, oura
from openhost_oura.service_api import service_routes

from test_backfill import FakeClient, FakeResp

RIDE = {
    "id": "w-ride",
    "activity": "cycling",
    "calories": 420.5,
    "day": "2025-03-01",
    "distance": 20000.0,
    "start_datetime": "2025-03-01T08:00:00-05:00",
    "end_datetime": "2025-03-01T09:00:00-05:00",
    "intensity": "moderate",
    "label": None,
    "source": "confirmed",
}
RUN = {
    "id": "w-run",
    "activity": "running",
    "calories": 300.0,
    "day": "2025-03-02",
    "distance": 5000.0,
    "start_datetime": "2025-03-02T07:00:00+00:00",
    "end_datetime": "2025-03-02T07:25:00+00:00",
    "intensity": "hard",
    "label": None,
    "source": "autodetected",
}
LIFT = {
    "id": "w-lift",
    "activity": "strengthTraining",
    "calories": None,
    "day": "2025-03-03",
    "distance": None,
    "start_datetime": "2025-03-03T18:00:00+00:00",
    "end_datetime": "2025-03-03T18:45:00+00:00",
    "intensity": "moderate",
    "label": "Gym",
    "source": "manual",
}


def _ms(iso):
    from datetime import datetime
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def _seed(monkeypatch, tmp_path, records):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))

    async def run():
        await db.init_db()
        await oura._sync_workouts(FakeClient([FakeResp(200, data=records)]), {}, "2025-03-01", "2025-04-01")
        async with db.connect() as conn:
            # Ride HR, plus a sleep-session sample in the same window that must be ignored.
            await conn.execute("INSERT INTO sleep_sessions (source_id, start_ts, end_ts) VALUES ('s', 'a', 'b')")
            await conn.executemany(
                "INSERT INTO samples (metric, timestamp_unix, end_unix, value, sleep_session_id) VALUES ('heart_rate', ?, ?, ?, ?)",
                [
                    (_ms("2025-03-01T08:10:00-05:00"), None, 120.0, None),
                    (_ms("2025-03-01T08:20:00-05:00"), None, 150.0, None),
                    (_ms("2025-03-01T08:30:00-05:00"), None, 135.0, None),
                    (_ms("2025-03-01T08:40:00-05:00"), _ms("2025-03-01T08:45:00-05:00"), 50.0, 1),
                    (_ms("2025-03-01T10:00:00-05:00"), None, 70.0, None),  # after the ride
                ],
            )
            await conn.commit()

    asyncio.run(run())
    return TestClient(Litestar(route_handlers=service_routes))


def test_workout_type_mapping():
    assert oura.workout_type("cycling") == "cycling"
    assert oura.workout_type("indoorCycling") == "cycling"
    assert oura.workout_type("strengthTraining") == "strength"
    assert oura.workout_type("Outdoor Run") == "running"
    assert oura.workout_type("elliptical") == "elliptical"
    assert oura.workout_type("") == "other"


def test_sync_workouts_upserts(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path, [RIDE, RUN])

    async def resync():
        edited = dict(RIDE, calories=500.0)
        await oura._sync_workouts(FakeClient([FakeResp(200, data=[edited])]), {}, "2025-03-01", "2025-04-01")
        async with db.connect() as conn:
            return await (await conn.execute("SELECT source_id, calories FROM workouts ORDER BY source_id")).fetchall()

    rows = [tuple(r) for r in asyncio.run(resync())]
    assert rows == [("w-ride", 500.0), ("w-run", 300.0)]


def test_service_workouts_list(monkeypatch, tmp_path):
    with _seed(monkeypatch, tmp_path, [RIDE, RUN, LIFT]) as client:
        body = client.get("/api/v1/workouts").json()
    assert body["count"] == 3
    ride, run, lift = body["data"]  # chronological

    assert ride["id"] == "w-ride"
    assert ride["workout_type"] == "cycling"
    assert ride["start"] == "2025-03-01T13:00:00+00:00"
    assert ride["duration"]["value"] == 60.0 and ride["duration"]["unit"] == "min"
    assert ride["calories"]["value"] == 420.5
    assert ride["distance"] == {"metric_id": "distance", "display_name": "Distance", "unit": "m", "value": 20000.0, "source": "oura"}
    assert abs(ride["average_speed"]["value"] - 20000 / 3600) < 1e-9
    assert "average_pace" not in ride  # cycling reports speed, not pace
    assert ride["average_heart_rate"]["value"] == 135.0
    assert ride["max_heart_rate"]["value"] == 150.0
    assert ride["lowest_heart_rate"]["value"] == 120.0
    assert "heart_rate" not in ride  # trace is detail-only

    assert run["average_pace"]["value"] == 300.0  # 25 min / 5 km
    assert "average_heart_rate" not in run

    assert lift["workout_type"] == "strength"
    assert "calories" not in lift and "distance" not in lift


def test_service_workouts_filters(monkeypatch, tmp_path):
    with _seed(monkeypatch, tmp_path, [RIDE, RUN, LIFT]) as client:
        by_type = client.get("/api/v1/workouts", params={"workout_type": "running"}).json()
        by_range = client.get("/api/v1/workouts", params={"start": "2025-03-02T00:00:00Z", "end": "2025-03-02 23:59:59+00:00"}).json()
        limited = client.get("/api/v1/workouts", params={"limit": 1}).json()
    assert [w["id"] for w in by_type["data"]] == ["w-run"]
    assert [w["id"] for w in by_range["data"]] == ["w-run"]
    assert [w["id"] for w in limited["data"]] == ["w-lift"]  # most recent


def test_service_workout_detail(monkeypatch, tmp_path):
    with _seed(monkeypatch, tmp_path, [RIDE]) as client:
        r = client.get("/api/v1/workouts/w-ride")
        missing = client.get("/api/v1/workouts/nope")
    assert r.status_code == 200
    w = r.json()
    assert [s["value"] for s in w["heart_rate"]["samples"]] == [120.0, 150.0, 135.0]
    assert w["heart_rate"]["unit"] == "bpm"
    assert missing.status_code == 404


def test_service_workouts_deserialize_with_spec_client(monkeypatch, tmp_path):
    from health_data_service import CyclingWorkout, RunningWorkout
    from health_data_service.client import _structure_workouts

    with _seed(monkeypatch, tmp_path, [RIDE, RUN]) as client:
        data = client.get("/api/v1/workouts").json()["data"]
    ride, run = _structure_workouts(data)
    assert isinstance(ride, CyclingWorkout) and ride.distance.value == 20000.0
    assert isinstance(run, RunningWorkout) and run.average_pace.value == 300.0


def test_sync_workouts_missing_scope_is_skipped(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))

    async def no_refresh():
        return None

    monkeypatch.setattr(oura, "refresh_access_token", no_refresh)

    async def run():
        await db.init_db()
        client = FakeClient([FakeResp(401)])
        await oura._sync_workouts(client, {"Authorization": "Bearer t"}, "2025-03-01", "2025-04-01")
        # Second window doesn't hit the API again.
        await oura._sync_workouts(client, {"Authorization": "Bearer t"}, "2025-04-01", "2025-05-01")
        return len(client.calls), await oura._workouts_scope_warning()

    calls, warning = asyncio.run(run())
    assert calls == 1
    assert "reconnect" in warning.lower()


def test_sync_workouts_other_errors_propagate(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))

    async def run():
        await db.init_db()
        await oura._sync_workouts(FakeClient([FakeResp(500)]), {}, "2025-03-01", "2025-04-01")

    try:
        asyncio.run(run())
    except httpx.HTTPStatusError:
        pass
    else:
        raise AssertionError("expected HTTPStatusError")


def test_backfill_restarts_after_done(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    windows = []

    async def fake_window(client, headers, start, end):
        windows.append((start, end))

    monkeypatch.setattr(oura, "_sync_window", fake_window)

    async def run():
        await db.init_db()
        await db.set_config("oura_access_token", "tok")
        await db.set_config("backfill_start", "2025-01-01")
        await db.set_config("backfill_cursor", "2099-01-01")
        await db.set_config("backfill_state", "done")
        await oura.backfill()

    asyncio.run(run())
    # A finished backfill re-walks history from the start instead of no-op'ing.
    assert windows[0] == ("2025-01-01", "2025-02-01")
