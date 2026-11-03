"""Connecting to Oura end to end: a real browser and router, with mock_oura playing Oura.

Covers cloud-in-a-bottle/oura-connector#14, where Oura's redirect back to /oauth/callback is a
cross-site navigation that the router answered with an empty 404 instead of passing it to the app.
"""

import sqlite3
import time
from pathlib import Path

import pytest
from openhost_test_harness import OpenhostStack
from playwright.sync_api import sync_playwright

from mock_oura import ACCESS_TOKEN, CLIENT_ID, CLIENT_SECRET, MockOura


@pytest.fixture(scope="module")
def stack():
    # Its own stack: connecting leaves the app connected, which the shared stack's tests don't expect. Its
    # own app name too: the harness names containers after the app, and tearing this stack down would
    # otherwise remove the shared stack's container.
    with OpenhostStack(app_dir=Path(__file__).resolve().parent.parent, app_name="oura-connect") as s:
        yield s


@pytest.fixture
def mock_oura():
    with MockOura() as m:
        yield m


@pytest.fixture
def browser_page():
    # Not pytest-playwright's `page`: its session-wide browser keeps an event loop running on this thread,
    # which breaks the later tests that call asyncio.run.
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            yield browser.new_page()
        finally:
            browser.close()


def _point_app_at(stack: OpenhostStack, mock: MockOura) -> None:
    db_path = Path(stack.local_stack.config.persistent_data_dir) / "app_data" / stack.app_name / "sqlite" / "health.db"
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
            [("oura_auth_base_override", mock.browser_url), ("oura_api_base_override", mock.container_url)],
        )


def test_connect_to_oura(stack, mock_oura, browser_page):
    page = browser_page
    _point_app_at(stack, mock_oura)
    callback = f"{stack.url}/oauth/callback"

    stack.playwright_login(page)
    page.goto(f"{stack.url}/setup")
    page.fill("input[name=client_id]", CLIENT_ID)
    page.fill("input[name=client_secret]", CLIENT_SECRET)
    page.click("button:has-text('Connect to Oura')")

    page.wait_for_url(f"{mock_oura.browser_url}/oauth/authorize?*")
    with page.expect_response(lambda r: r.url.startswith(callback)) as returned:
        page.click("#approve")
    # The browser comes back from Oura's site: the router has to pass that cross-site navigation to the
    # app with the owner's session, not answer it with the empty 404 a private app gives strangers.
    assert returned.value.request.all_headers().get("sec-fetch-site") == "cross-site"
    assert returned.value.status == 302, f"callback answered {returned.value.status}, not the app's redirect"
    page.wait_for_url(f"{stack.url}/")

    # The app exchanged the code server-side, with the redirect_uri Oura issued it for.
    assert len(mock_oura.token_requests) == 1
    assert mock_oura.token_requests[0]["redirect_uri"] == callback
    assert mock_oura.issued_codes == {}  # the code was redeemed

    # The initial sync runs with the new token.
    deadline = time.monotonic() + 30
    while not mock_oura.api_paths and time.monotonic() < deadline:
        time.sleep(0.2)
    assert mock_oura.api_paths, "the app never called the Oura API after connecting"

    setup = stack.owner_session.get(f"{stack.url}/setup")
    assert "not connected" not in setup.text
    assert "connected" in setup.text
    assert ACCESS_TOKEN not in setup.text
