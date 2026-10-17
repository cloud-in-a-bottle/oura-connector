import httpx


def test_health(stack):
    r = httpx.get(f"{stack.app_url}/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_index_redirects_to_setup(stack):
    r = stack.owner_session.get(f"{stack.url}/", allow_redirects=False)
    assert r.status_code in (301, 302, 307)
    assert "/setup" in r.headers.get("location", "")


def test_setup_page(stack):
    r = stack.owner_session.get(f"{stack.url}/setup")
    assert r.status_code == 200
    assert "Oura" in r.text


def test_api_metrics_empty(stack):
    r = stack.owner_session.get(f"{stack.url}/api/v1/metrics")
    assert r.status_code == 200
    assert "metrics" in r.json()


def test_api_time_series_requires_metric(stack):
    r = stack.owner_session.get(f"{stack.url}/api/v1/time-series")
    assert r.status_code == 200
    body = r.json()
    assert "error" in body


def test_api_sleep_sessions_empty(stack):
    r = stack.owner_session.get(f"{stack.url}/api/v1/sleep-sessions")
    assert r.status_code == 200
    assert r.json()["data"] == []


def test_api_workouts_empty(stack):
    r = stack.owner_session.get(f"{stack.url}/api/v1/workouts")
    assert r.status_code == 200
    assert r.json()["data"] == []


def test_api_workout_detail_404(stack):
    r = stack.owner_session.get(f"{stack.url}/api/v1/workouts/does-not-exist")
    assert r.status_code == 404


def test_api_sleep_sessions_no_series(stack):
    r = stack.owner_session.get(f"{stack.url}/api/v1/sleep-sessions?limit=60&include_series=false")
    assert r.status_code == 200
    assert r.json()["data"] == []


def test_backfill_requires_token(stack):
    r = stack.owner_session.post(f"{stack.url}/backfill")
    assert r.status_code == 201
    assert r.json().get("error") == "Not configured"


def test_backfill_status_idle(stack):
    r = stack.owner_session.get(f"{stack.url}/api/backfill-status")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "idle"
    assert body["progress"] == 0.0


def test_dedupe_toggle(stack):
    r = stack.owner_session.post(f"{stack.url}/setup/dedupe", data={"dedupe_workouts": "1"})
    assert r.status_code == 200
    assert 'name="dedupe_workouts" value="1" checked' in r.text
    assert "hidden as duplicates" in r.text

    r = stack.owner_session.post(f"{stack.url}/setup/dedupe", data={})
    assert 'name="dedupe_workouts" value="1" checked' not in r.text
