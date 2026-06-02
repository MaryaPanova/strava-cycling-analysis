"""One-time OAuth2 setup for the Strava API.

Run once:

    python auth.py

It opens your browser to Strava's authorization page, captures the
redirect on a tiny local web server, exchanges the code for tokens, and
saves them to `strava_tokens.json`. After that, the other scripts refresh
the access token automatically.
"""

from __future__ import annotations

import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from strava_client import StravaClient, StravaAuthError

# Strava requires the redirect host to match the app's "Authorization
# Callback Domain". Set that to `localhost` in your Strava app settings.
CALLBACK_HOST = "localhost"
CALLBACK_PORT = 8721
REDIRECT_URI = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}/callback"

_SUCCESS_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>Strava connected</title></head>
<body style="font-family:system-ui;text-align:center;margin-top:18%">
<h2 style="color:#FC4C02">✅ Strava connected</h2>
<p>You can close this tab and return to the terminal.</p>
</body></html>"""

_ERROR_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>Authorization failed</title></head>
<body style="font-family:system-ui;text-align:center;margin-top:18%">
<h2 style="color:#b00">Authorization failed</h2>
<p>{message}</p></body></html>"""


class _CallbackHandler(BaseHTTPRequestHandler):
    # Filled in by the server loop.
    auth_code: str | None = None
    auth_error: str | None = None

    def do_GET(self):  # noqa: N802 (http.server API)
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = parse_qs(parsed.query)
        if "error" in params:
            _CallbackHandler.auth_error = params["error"][0]
            self._respond(400, _ERROR_HTML.format(message=_CallbackHandler.auth_error))
        elif "code" in params:
            _CallbackHandler.auth_code = params["code"][0]
            self._respond(200, _SUCCESS_HTML)
        else:
            self._respond(400, _ERROR_HTML.format(message="No code returned."))

    def _respond(self, status: int, html: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def log_message(self, *args):  # silence default request logging
        pass


def run_oauth_flow() -> None:
    try:
        client = StravaClient()
    except StravaAuthError as exc:
        sys.exit(f"✗ {exc}")

    auth_url = client.authorization_url(REDIRECT_URI)
    server = HTTPServer((CALLBACK_HOST, CALLBACK_PORT), _CallbackHandler)

    print("Opening your browser to authorize this app with Strava…")
    print(f"If it doesn't open, visit:\n  {auth_url}\n")
    webbrowser.open(auth_url)

    # Serve requests on a background thread until we get a code or error.
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print(f"Waiting for the Strava redirect on {REDIRECT_URI} …")
    try:
        while _CallbackHandler.auth_code is None and _CallbackHandler.auth_error is None:
            time.sleep(0.3)
    except KeyboardInterrupt:
        sys.exit("\n✗ Cancelled.")
    finally:
        server.shutdown()

    if _CallbackHandler.auth_error:
        sys.exit(f"✗ Strava returned an error: {_CallbackHandler.auth_error}")

    print("Exchanging authorization code for tokens…")
    tokens = client.exchange_code(_CallbackHandler.auth_code)
    athlete = tokens.get("athlete") or {}
    name = " ".join(filter(None, [athlete.get("firstname"), athlete.get("lastname")]))
    print(f"✓ Authorized{f' as {name}' if name else ''}.")
    print(f"✓ Tokens saved to {client.tokens_path.name} (auto-refreshes from here on).")


if __name__ == "__main__":
    run_oauth_flow()
