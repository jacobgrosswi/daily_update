"""Tests for scripts/check_token_health.py — exits 0 on success, 1 on failure,
persists rotated refresh tokens to REFRESH_TOKEN_OUT_PATH."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_token_health.py"


@pytest.fixture
def health_mod():
    """Load the script as a module so we can patch its symbols directly."""
    spec = importlib.util.spec_from_file_location("check_token_health", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ok_response():
    r = MagicMock(spec=httpx.Response)
    r.status_code = 200
    r.is_success = True
    r.raise_for_status.return_value = None
    r.json.return_value = {"id": "u1", "userPrincipalName": "me@example.com"}
    return r


def _ok_email_client(rotated_rt: str | None = None):
    """A MagicMock that mimics the bits of EmailClient the script touches."""
    c = MagicMock()
    c._acquire_access_token.return_value = "access-token"
    c.current_refresh_token = rotated_rt or "rt-original"
    return c


def _patch_httpx(monkeypatch, response):
    fake_http = MagicMock()
    fake_http.__enter__ = lambda self: self
    fake_http.__exit__ = lambda self, *a: None
    fake_http.get.return_value = response
    monkeypatch.setattr(httpx, "Client", lambda **kw: fake_http)
    return fake_http


def test_returns_zero_on_success(monkeypatch, health_mod, capsys):
    monkeypatch.setattr(health_mod, "EmailClient", lambda: _ok_email_client())
    _patch_httpx(monkeypatch, _ok_response())
    assert health_mod.main() == 0
    out = capsys.readouterr().out
    assert "TOKEN HEALTH OK" in out


def test_returns_one_when_msal_fails(monkeypatch, health_mod, capsys):
    bad = MagicMock()
    bad._acquire_access_token.side_effect = RuntimeError(
        "Token refresh failed: invalid_grant - expired"
    )
    monkeypatch.setattr(health_mod, "EmailClient", lambda: bad)
    assert health_mod.main() == 1
    err = capsys.readouterr().err
    assert "TOKEN HEALTH FAIL" in err
    assert "invalid_grant" in err


def test_returns_one_when_graph_rejects_token(monkeypatch, health_mod, capsys):
    monkeypatch.setattr(health_mod, "EmailClient", lambda: _ok_email_client())
    bad = MagicMock(spec=httpx.Response)
    bad.raise_for_status.side_effect = httpx.HTTPStatusError(
        "401 Unauthorized", request=MagicMock(), response=MagicMock()
    )
    _patch_httpx(monkeypatch, bad)
    assert health_mod.main() == 1
    err = capsys.readouterr().err
    assert "TOKEN HEALTH FAIL" in err


def test_persists_rotated_refresh_token(monkeypatch, tmp_path, health_mod):
    out_path = tmp_path / "ms_refresh_token.txt"
    monkeypatch.setenv("REFRESH_TOKEN_OUT_PATH", str(out_path))
    monkeypatch.setenv("MS_REFRESH_TOKEN", "rt-original")
    monkeypatch.setattr(
        health_mod, "EmailClient", lambda: _ok_email_client(rotated_rt="rt-new")
    )
    _patch_httpx(monkeypatch, _ok_response())
    assert health_mod.main() == 0
    assert out_path.read_text() == "rt-new"


def test_does_not_write_when_token_unchanged(monkeypatch, tmp_path, health_mod):
    out_path = tmp_path / "ms_refresh_token.txt"
    monkeypatch.setenv("REFRESH_TOKEN_OUT_PATH", str(out_path))
    monkeypatch.setenv("MS_REFRESH_TOKEN", "rt-original")
    monkeypatch.setattr(
        health_mod, "EmailClient", lambda: _ok_email_client(rotated_rt="rt-original")
    )
    _patch_httpx(monkeypatch, _ok_response())
    assert health_mod.main() == 0
    assert not out_path.exists()


def test_no_persist_when_env_var_unset(monkeypatch, health_mod):
    monkeypatch.delenv("REFRESH_TOKEN_OUT_PATH", raising=False)
    monkeypatch.setattr(
        health_mod, "EmailClient", lambda: _ok_email_client(rotated_rt="rt-new")
    )
    _patch_httpx(monkeypatch, _ok_response())
    # Just shouldn't raise — nothing to assert beyond exit 0.
    assert health_mod.main() == 0
