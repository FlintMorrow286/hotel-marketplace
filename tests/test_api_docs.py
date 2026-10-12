"""Swagger UI, ReDoc and the schema are on while developing and off in production."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.config.settings import Settings, settings
from app.main import create_app

# The app is only built, never started: its lifespan would refuse the production config the
# tests set up here, and nothing below needs it.


@pytest.mark.parametrize(
    ("env", "api_docs", "expected"),
    [
        ("local", None, True),
        ("test", None, True),
        ("production", None, False),
        ("staging", None, False),
        ("production", True, True),
        ("local", False, False),
    ],
)
def test_docs_follow_the_environment_unless_set(env, api_docs, expected) -> None:
    assert Settings(_env_file=None, app_env=env, api_docs=api_docs).docs_enabled is expected


@pytest.mark.parametrize("raw", ["", "  "])
def test_a_blank_api_docs_is_unset(monkeypatch, raw) -> None:
    """Compose forwards API_DOCS even when the operator never set it."""
    monkeypatch.setenv("API_DOCS", raw)

    assert Settings(_env_file=None, app_env="production").docs_enabled is False
    assert Settings(_env_file=None, app_env="local").docs_enabled is True


@pytest.mark.parametrize("raw", ["true", "1"])
def test_api_docs_can_be_switched_on_in_production(monkeypatch, raw) -> None:
    monkeypatch.setenv("API_DOCS", raw)

    assert Settings(_env_file=None, app_env="production").docs_enabled is True


def test_production_serves_neither_the_pages_nor_the_schema(monkeypatch) -> None:
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "api_docs", None)
    app = create_app()
    client = TestClient(app)

    assert (app.docs_url, app.redoc_url, app.openapi_url) == (None, None, None)
    assert "swagger" not in client.get("/docs").text.lower()
    assert "redoc" not in client.get("/redoc").text.lower()
    assert "openapi" not in client.get("/openapi.json").text.lower()


def test_production_with_the_switch_on_serves_them(monkeypatch) -> None:
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "api_docs", True)
    client = TestClient(create_app())

    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").json()["info"]["title"] == "Hotel Marketplace API"


def test_the_policy_comes_back_on_the_paths_the_docs_no_longer_use(monkeypatch) -> None:
    """With the docs off, /docs is the SPA's fallback page and gets the policy like any page."""
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "api_docs", None)
    client = TestClient(create_app())

    assert "content-security-policy" in client.get("/docs").headers
    assert "content-security-policy" in client.get("/openapi.json").headers
