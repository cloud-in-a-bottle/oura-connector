import asyncio

import pytest
from litestar.testing import TestClient

from openhost_oura import app as app_module
from openhost_oura import db, oura


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    exchanged = []

    async def fake_exchange(code, redirect_uri, client_id, client_secret):
        exchanged.append(code)
        return {"access_token": "tok", "refresh_token": "ref"}

    async def no_sync():
        pass

    monkeypatch.setattr(oura, "exchange_code", fake_exchange)
    monkeypatch.setattr(app_module, "_background_sync", no_sync)
    monkeypatch.setattr(app_module, "_periodic_sync", no_sync)

    async def seed():
        await db.init_db()
        await db.set_config("oura_client_id", "cid")
        await db.set_config("oura_client_secret", "secret")

    asyncio.run(seed())
    with TestClient(app=app_module.app) as c:
        c.exchanged = exchanged
        yield c


def _config(key):
    return asyncio.run(db.get_config(key))


def _start(client):
    r = client.post(
        "/setup/oauth",
        content="client_id=cid&client_secret=secret",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303, 307)
    return _config("oauth_state")


def test_callback_with_matching_state_connects(client):
    state = _start(client)
    r = client.get(f"/oauth/callback?code=c1&state={state}", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert client.exchanged == ["c1"]
    assert _config("oura_access_token") == "tok"
    assert _config("oauth_state") is None


@pytest.mark.parametrize("query", ["code=c1", "code=c1&state=", "code=c1&state=wrong"])
def test_callback_with_bad_state_is_refused(client, query):
    _start(client)
    r = client.get(f"/oauth/callback?{query}", follow_redirects=False)
    assert r.status_code == 400
    assert client.exchanged == []
    assert _config("oura_access_token") is None


@pytest.mark.parametrize("query", ["code=c1", "code=c1&state="])
def test_callback_without_a_connect_in_progress_is_refused(client, query):
    # No /setup/oauth first, so no state is stored: a missing state must not match it.
    r = client.get(f"/oauth/callback?{query}", follow_redirects=False)
    assert r.status_code == 400
    assert client.exchanged == []
    assert _config("oura_access_token") is None


def test_state_is_single_use(client):
    state = _start(client)
    client.get(f"/oauth/callback?code=c1&state={state}", follow_redirects=False)
    r = client.get(f"/oauth/callback?code=c2&state={state}", follow_redirects=False)
    assert r.status_code == 400
    assert client.exchanged == ["c1"]
