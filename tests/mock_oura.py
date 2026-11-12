"""A stand-in for Oura's OAuth and API hosts, for the connect-flow harness test.

It runs on the test host, reachable two ways: the browser visits it as http://127.0.0.1:<port>,
which is cross-site to the harness's *.localhost apps (as cloud.ouraring.com is to a real
instance), and the app container calls it as http://host.containers.internal:<port>.
"""

import json
import secrets
import threading
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

CLIENT_ID = "mock-client-id"
CLIENT_SECRET = "mock-client-secret"
ACCESS_TOKEN = "mock-access-token"


class MockOura:
    def __init__(self) -> None:
        self.issued_codes: dict[str, str] = {}  # code -> the redirect_uri it was issued for
        self.token_requests: list[dict[str, str]] = []
        self.api_paths: list[str] = []
        self._server = ThreadingHTTPServer(("0.0.0.0", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def browser_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def container_url(self) -> str:
        return f"http://host.containers.internal:{self.port}"

    def __enter__(self) -> "MockOura":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def _send(self, status: int, body: str, content_type: str, headers: dict[str, str] | None = None) -> None:
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def _json(self, status: int, payload: object) -> None:
                self._send(status, json.dumps(payload), "application/json")

            def _form(self) -> dict[str, str]:
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
                return {k: v[0] for k, v in parse_qs(raw).items()}

            def do_GET(self) -> None:
                url = urlsplit(self.path)
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                if url.path == "/oauth/authorize":
                    if query.get("client_id") != CLIENT_ID or query.get("response_type") != "code":
                        return self._send(400, "bad authorize request", "text/plain")
                    # Oura's consent page: approving POSTs back here, then redirects to the app.
                    hidden = "".join(
                        f'<input type="hidden" name="{k}" value="{escape(query.get(k, ""))}">'
                        for k in ("redirect_uri", "state")
                    )
                    page = f'<form method="post" action="/oauth/approve">{hidden}<button id="approve">Allow</button></form>'
                    return self._send(200, f"<!doctype html><title>Mock Oura</title>{page}", "text/html")
                if url.path.startswith("/v2/usercollection/"):
                    if self.headers.get("Authorization") != f"Bearer {ACCESS_TOKEN}":
                        return self._json(401, {"detail": "bad token"})
                    mock.api_paths.append(url.path)
                    return self._json(200, {"data": [], "next_token": None})
                self._send(404, "not found", "text/plain")

            def do_POST(self) -> None:
                path = urlsplit(self.path).path
                form = self._form()
                if path == "/oauth/approve":
                    code = secrets.token_urlsafe(8)
                    mock.issued_codes[code] = form["redirect_uri"]
                    location = f"{form['redirect_uri']}?{urlencode({'code': code, 'state': form['state']})}"
                    return self._send(302, "", "text/plain", {"Location": location})
                if path == "/oauth/token":
                    mock.token_requests.append(form)
                    ok = (
                        form.get("grant_type") == "authorization_code"
                        and form.get("client_id") == CLIENT_ID
                        and form.get("client_secret") == CLIENT_SECRET
                        and mock.issued_codes.pop(form.get("code", ""), None) == form.get("redirect_uri")
                    )
                    if not ok:
                        return self._json(400, {"error": "invalid_grant"})
                    return self._json(
                        200,
                        {"access_token": ACCESS_TOKEN, "refresh_token": "mock-refresh-token", "token_type": "bearer"},
                    )
                self._send(404, "not found", "text/plain")

        return Handler
