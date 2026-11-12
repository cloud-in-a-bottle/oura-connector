"""Connecting to Oura end to end: a real browser and router, with mock_oura playing Oura.

Covers cloud-in-a-bottle/oura-connector#14, where Oura's redirect back to /oauth/callback is a
cross-site navigation that the router answered with an empty 404 instead of passing it to the app.
"""

import subprocess
import time
from pathlib import Path

import pytest
from openhost_test_harness import OpenhostStack
from playwright.sync_api import sync_playwright

from mock_oura import ACCESS_TOKEN, CLIENT_ID, CLIENT_SECRET, MockOura


REPO = Path(__file__).resolve().parent.parent
OURA_MODULE = Path("openhost_oura/src/openhost_oura/oura.py")


@pytest.fixture(scope="module")
def mock_oura():
    with MockOura() as m:
        yield m


def _app_pointed_at(mock: MockOura, dest: Path) -> Path:
    """A copy of the app (git-listed files) with Oura's hosts swapped for the mock's."""
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=REPO, capture_output=True, check=True,
    ).stdout.decode().split("\0")
    for rel in filter(None, listed):
        if (REPO / rel).is_file():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            (dest / rel).write_bytes((REPO / rel).read_bytes())
    module = dest / OURA_MODULE
    source = module.read_text()
    # The browser visits the auth host; the app container calls the API host (tokens and data).
    for real, fake in [("https://cloud.ouraring.com", mock.browser_url), ("https://api.ouraring.com", mock.container_url)]:
        assert real in source, f"{real} no longer in {OURA_MODULE}; update this test"
        source = source.replace(real, fake)
    module.write_text(source)
    return dest


@pytest.fixture(scope="module")
def stack(mock_oura, tmp_path_factory):
    # Its own stack: connecting leaves the app connected, which the shared stack's tests don't expect. Its
    # own app name too: the harness names containers after the app, and tearing this stack down would
    # otherwise remove the shared stack's container.
    app_dir = _app_pointed_at(mock_oura, tmp_path_factory.mktemp("app"))
    with OpenhostStack(app_dir=app_dir, app_name="oura-connect") as s:
        yield s


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


def test_connect_to_oura(stack, mock_oura, browser_page):
    page = browser_page
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
