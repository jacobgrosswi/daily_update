"""Weekly Microsoft Graph refresh-token health check.

Exercises the refresh token end-to-end: acquires an access token via MSAL and
makes a small Graph call (GET /me) to confirm scopes still work. If MSAL hands
back a rotated refresh token, writes it to REFRESH_TOKEN_OUT_PATH so the
calling workflow can update the MS_REFRESH_TOKEN secret.

Exit codes:
  0 — refresh token works and Graph responded.
  1 — refresh failed or Graph rejected the access token. The GitHub Actions
      run will fail; GitHub emails the workflow owner by default.

Run from repo root:
    uv run python scripts/check_token_health.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

# Allow running as a plain script (without `-m`) by adding repo root to path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.email_client import GRAPH_BASE, EmailClient  # noqa: E402
from src.utils import configure_logging, get_logger  # noqa: E402

log = get_logger(__name__)


def _persist_rotated_refresh_token(client: EmailClient) -> None:
    """Mirror of src/main.py — if MSAL rotated the token, surface it to the
    workflow via REFRESH_TOKEN_OUT_PATH so MS_REFRESH_TOKEN gets updated."""
    out_path = os.environ.get("REFRESH_TOKEN_OUT_PATH")
    if not out_path:
        return
    current = client.current_refresh_token
    original = os.environ.get("MS_REFRESH_TOKEN")
    if current and current != original:
        Path(out_path).write_text(current)
        log.info("Refresh token rotated; wrote new value to %s.", out_path)


def main() -> int:
    configure_logging()
    try:
        client = EmailClient()
        # Acquire access token. Raises RuntimeError on invalid_grant / etc.
        token = client._acquire_access_token()  # noqa: SLF001
        # Sanity ping: confirm the token actually works against Graph.
        with httpx.Client(timeout=15.0) as http:
            r = http.get(
                f"{GRAPH_BASE}/me",
                headers={"Authorization": f"Bearer {token}"},
                params={"$select": "id,userPrincipalName"},
            )
            r.raise_for_status()
        _persist_rotated_refresh_token(client)
    except Exception as e:  # noqa: BLE001 — top-level reporter
        print(f"TOKEN HEALTH FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print("TOKEN HEALTH OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
