"""One-shot helper: complete Microsoft Graph consent and print a refresh token.

Run this locally exactly once after the Azure App Registration is set up.

Usage:
    export MS_CLIENT_ID=...    # from Azure portal "Application (client) ID"
    export MS_CLIENT_SECRET=...  # from Azure portal "Certificates & secrets"
    uv run python scripts/get_refresh_token.py

The script:
  1. Opens your browser to the Microsoft consent page.
  2. Spins up a tiny localhost server on port 8000 to catch the redirect.
  3. Exchanges the auth code for tokens.
  4. Prints the refresh token. Copy it into GitHub Secrets as MS_REFRESH_TOKEN
     (and into your local .env if you want to test the daily run).

Azure prerequisites (one-time):
  - App registration with redirect URI http://localhost:8000/callback (Web).
  - Delegated Microsoft Graph permissions: Mail.Read, Mail.Send, offline_access.
  - Supported account types: "Personal Microsoft accounts only".
"""
from __future__ import annotations

import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import msal

REDIRECT_URI = "http://localhost:8000/callback"
AUTHORITY = "https://login.microsoftonline.com/consumers"
SCOPES = ["Mail.Read", "Mail.Send"]  # offline_access is implicit; MSAL adds it


class _CallbackHandler(BaseHTTPRequestHandler):
    captured: dict = {}

    def do_GET(self):  # noqa: N802 — name fixed by BaseHTTPRequestHandler
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        _CallbackHandler.captured.update(params)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = "Auth code received. You can close this tab."
        if "error" in params:
            msg = f"Error: {params.get('error')} - {params.get('error_description', '')}"
        self.wfile.write(f"<html><body><h2>{msg}</h2></body></html>".encode())

    def log_message(self, *_):  # silence the per-request stderr line
        return


def main() -> int:
    client_id = os.environ.get("MS_CLIENT_ID")
    client_secret = os.environ.get("MS_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("ERROR: Set MS_CLIENT_ID and MS_CLIENT_SECRET in your environment first.",
              file=sys.stderr)
        return 1

    app = msal.ConfidentialClientApplication(
        client_id=client_id,
        client_credential=client_secret,
        authority=AUTHORITY,
    )

    auth_url = app.get_authorization_request_url(
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
        prompt="consent",  # force the consent screen so refresh_token is granted
    )

    server = HTTPServer(("localhost", 8000), _CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print(f"Opening browser for consent: {auth_url}")
    webbrowser.open(auth_url)
    print("Waiting for the redirect...")

    while "code" not in _CallbackHandler.captured and "error" not in _CallbackHandler.captured:
        pass  # busy-wait is fine for a 1-time interactive script

    server.shutdown()

    if "error" in _CallbackHandler.captured:
        print(f"Consent failed: {_CallbackHandler.captured}", file=sys.stderr)
        return 1

    code = _CallbackHandler.captured["code"]
    result = app.acquire_token_by_authorization_code(
        code=code,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )

    if "refresh_token" not in result:
        print(f"Token exchange failed: {result}", file=sys.stderr)
        return 1

    print()
    print("=" * 60)
    print("MS_REFRESH_TOKEN (copy this into GitHub Secrets and .env):")
    print("=" * 60)
    print(result["refresh_token"])
    print("=" * 60)
    print(f"Granted scopes: {result.get('scope', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
