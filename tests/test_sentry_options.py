"""What the application lets Sentry see (app/utils/sentry.py).

The SDK's defaults attach, to every error, the local variables of every frame, the request body and headers and the
log lines before it. Here that is a failing login's password, a guest's e-mail and phone, the booking token, a
channel's API key and the caller's JWT, as text in a third party's database. These tests fail real FastAPI routes the
way a database outage does and read what the SDK would have sent.
"""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path
from typing import Annotated

import pytest
import sentry_sdk
from fastapi import FastAPI, Header
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sentry_sdk.transport import Transport

from app.utils.sentry import init_sentry

ROOT = Path(__file__).resolve().parent.parent
DSN = "https://public@o0.ingest.example.invalid/1"
SECRETS = {
    "password": "hunter2-very-secret-pw",
    "guest e-mail": "guest.private@example.com",
    "guest phone": "+79991234567",
    "booking token": "bk-token-0123456789abcdef0123456789abcdef",
    "channel API key": "ck-live-9f8e7d6c5b4a39281706f5e4d3c2b1a0",
    "JWT": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJwYXJ0bmVyIn0.signaturesignature",
}
LOG = logging.getLogger("app.test_sentry")


class _LoginBody(BaseModel):
    email: str
    password: str


class _HoldBody(BaseModel):
    guest_email: str
    guest_phone: str


def _app() -> FastAPI:
    app = FastAPI()

    @app.post("/login")
    async def login(data: _LoginBody):
        # The notification stub logs the guest's address at INFO: it becomes a breadcrumb.
        LOG.info("notify-email to=%s", SECRETS["guest e-mail"])
        raise RuntimeError("database went away")

    @app.post("/hold")
    async def hold(data: _HoldBody, x_booking_token: Annotated[str, Header()]):
        LOG.info("notify-email to=%s", data.guest_email)
        raise RuntimeError("database went away")

    @app.post("/channel")
    async def channel(x_api_key: Annotated[str, Header()]):
        raise RuntimeError("database went away")

    return app


@contextlib.contextmanager
def _sentry(events: list[dict], **overrides):
    class Capture(Transport):
        def capture_envelope(self, envelope):
            for item in envelope.items:
                if item.headers.get("type") == "event":
                    events.append(item.payload.json)

    init_sentry(DSN, "production", transport=Capture, **overrides)
    try:
        yield
    finally:
        sentry_sdk.get_client().close()
        sentry_sdk.get_global_scope().set_client(None)


def _fail_three_requests(app: FastAPI) -> None:
    jwt = {"Authorization": f"Bearer {SECRETS['JWT']}"}
    with TestClient(app, raise_server_exceptions=False) as client:
        client.post(
            "/login",
            json={"email": SECRETS["guest e-mail"], "password": SECRETS["password"]},
            headers=jwt,
        )
        client.post(
            "/hold",
            json={"guest_email": SECRETS["guest e-mail"], "guest_phone": SECRETS["guest phone"]},
            headers={**jwt, "X-Booking-Token": SECRETS["booking token"]},
        )
        client.post("/channel", headers={"X-API-Key": SECRETS["channel API key"]})
    sentry_sdk.flush()


@pytest.mark.parametrize(
    "app_first", [True, False], ids=["app built before init", "app built after init"]
)
def test_no_secret_or_personal_data_reaches_sentry(caplog, app_first: bool) -> None:
    """The application starts Sentry in its lifespan, after the routes exist; the other order too."""
    caplog.set_level(logging.INFO, logger=LOG.name)
    events: list[dict] = []
    app = _app() if app_first else None
    with _sentry(events):
        _fail_three_requests(app or _app())

    assert len(events) == 3, "the probe must capture the three failures, or it proves nothing"
    blob = json.dumps(events)
    leaked = [name for name, value in SECRETS.items() if value in blob]
    assert not leaked, f"reached Sentry: {leaked}"


def test_the_error_itself_still_reaches_sentry(caplog) -> None:
    """Closing the side channels must not blind it: type, message, stack trace and request line stay."""
    caplog.set_level(logging.INFO, logger=LOG.name)
    events: list[dict] = []
    with _sentry(events):
        _fail_three_requests(_app())

    first = events[0]
    exc = first["exception"]["values"][0]
    assert exc["type"] == "RuntimeError" and exc["value"] == "database went away"
    assert any(fr["function"] == "login" for fr in exc["stacktrace"]["frames"])
    assert first["request"]["url"].endswith("/login") and first["request"]["method"] == "POST"
    assert first["environment"] == "production"


def test_sentry_is_started_only_through_init_sentry() -> None:
    """A bare sentry_sdk.init(dsn=...) would bring all the defaults back."""
    direct = [
        str(path.relative_to(ROOT))
        for path in (ROOT / "app").rglob("*.py")
        if path.name != "sentry.py" and "sentry_sdk.init(" in path.read_text()
    ]
    assert not direct, f"start Sentry with app.utils.sentry.init_sentry: {direct}"
    assert "init_sentry(" in (ROOT / "app" / "main.py").read_text()
