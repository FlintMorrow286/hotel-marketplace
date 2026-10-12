"""Body limits, absurd nesting and security headers, on the real app."""

from __future__ import annotations

import io
import json
import uuid

import pytest
from fastapi import FastAPI
from PIL import Image
from starlette.testclient import TestClient

from app.config.settings import settings
from app.main import create_app
from app.modules.auth.jwt import create_access_token
from app.utils import http_guard
from app.utils.http_guard import BodyLimitMiddleware, body_limit, install_http_guards

pytestmark = pytest.mark.usefixtures("_test_db")


def _tiny_app(seen: list) -> FastAPI:
    """Just the guards and one route, so a test can see whether the route ran."""
    app = FastAPI()

    @app.post("/echo")
    async def echo(payload: dict) -> dict:
        seen.append(payload)
        return {"ok": True}

    install_http_guards(app)
    return app


# ---------------------------------------------------------------- body limit


def test_a_declared_length_over_the_limit_is_refused_before_the_route_runs(monkeypatch) -> None:
    monkeypatch.setattr(settings, "max_body_bytes", 100)
    seen: list = []

    with TestClient(_tiny_app(seen)) as client:
        big = client.post("/echo", json={"pad": "x" * 500})
        small = client.post("/echo", json={"pad": "x"})

    assert big.status_code == 413 and big.json() == {"detail": "request body too large"}
    assert small.status_code == 200
    assert seen == [{"pad": "x"}], "the oversized body must never reach the route"


def test_a_chunked_body_without_a_length_is_counted_as_it_arrives(monkeypatch) -> None:
    monkeypatch.setattr(settings, "max_body_bytes", 100)
    seen: list = []

    def chunks():
        yield b'{"pad": "'
        for _ in range(10):
            yield b"x" * 50
        yield b'"}'

    with TestClient(_tiny_app(seen)) as client:
        response = client.post(
            "/echo", content=chunks(), headers={"content-type": "application/json"}
        )

    assert response.status_code == 413
    assert seen == []


def test_reads_are_not_limited(monkeypatch) -> None:
    monkeypatch.setattr(settings, "max_body_bytes", 1)

    with TestClient(_tiny_app([])) as client:
        assert client.get("/nowhere").status_code == 404  # not 413


def test_only_the_photo_upload_gets_a_bigger_budget() -> None:
    assert body_limit("/v1/auth/login") == settings.max_body_bytes
    assert body_limit("/v1/bookings/hold") == settings.max_body_bytes
    upload = body_limit(f"/v1/partner/properties/{uuid.uuid4()}/photos")
    assert settings.photo_max_bytes < upload < settings.photo_max_bytes + 1024 * 1024
    assert body_limit("/v1/partner/properties/x/photos/../../other") == settings.max_body_bytes


def test_a_real_upload_within_the_photo_limit_still_passes(monkeypatch) -> None:
    """The multipart framing around a file at the limit must fit inside the budget."""
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (1, 2, 3)).save(buf, format="PNG")
    token = create_access_token(str(uuid.uuid4()), "partner")
    monkeypatch.setattr(settings, "photo_max_bytes", len(buf.getvalue()))

    with TestClient(create_app()) as client:
        response = client.post(
            f"/v1/partner/properties/{uuid.uuid4()}/photos",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("x.png", buf.getvalue(), "image/png")},
        )

    assert response.status_code == 404  # reached the route: the property is not this partner's


def test_a_huge_upload_is_refused_without_being_spooled(monkeypatch) -> None:
    token = create_access_token(str(uuid.uuid4()), "partner")
    monkeypatch.setattr(settings, "photo_max_bytes", 1024)
    spooled: list[int] = []
    real = http_guard.BodyLimitMiddleware.__call__

    async def watching(self, scope, receive, send):
        async def counting():
            message = await receive()
            spooled.append(len(message.get("body", b"")))
            return message

        await real(self, scope, counting, send)

    monkeypatch.setattr(http_guard.BodyLimitMiddleware, "__call__", watching)

    with TestClient(create_app()) as client:
        response = client.post(
            f"/v1/partner/properties/{uuid.uuid4()}/photos",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("x.png", b"\x89PNG" + b"0" * 3_000_000, "image/png")},
        )

    assert response.status_code == 413
    assert sum(spooled) == 0, "not one byte of the body may be read for a declared oversize"


# ---------------------------------------------------------------- nesting


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/v1/auth/login"),
        ("post", "/v1/auth/register"),
        ("post", "/v1/bookings/hold"),
        ("post", "/v1/bookings/lookup"),
        ("post", "/v1/admin/login"),
    ],
)
def test_json_nested_a_thousand_levels_deep_is_400_not_500(method, path) -> None:
    nested = "[" * 1500 + "]" * 1500

    with TestClient(create_app()) as client:
        response = getattr(client, method)(
            path, content=nested, headers={"content-type": "application/json"}
        )

    assert response.status_code == 400, response.text
    assert response.json() == {"detail": "request body is nested too deeply"}


def test_ordinary_nested_json_still_validates() -> None:
    body = json.dumps({"email": "a@b.example", "password": "x" * 12})

    with TestClient(create_app()) as client:
        response = client.post(
            "/v1/auth/login", content=body, headers={"content-type": "application/json"}
        )

    assert response.status_code in (401, 422)


# ---------------------------------------------------------------- headers


def _https(client: TestClient) -> TestClient:
    return TestClient(client.app, base_url="https://testserver")


def test_every_answer_carries_the_headers_even_a_refusal(monkeypatch) -> None:
    monkeypatch.setattr(settings, "max_body_bytes", 10)

    with TestClient(create_app()) as client:
        api = client.get("/healthz")
        missing = client.get("/v1/nothing-here")
        refused = client.post("/v1/auth/login", json={"pad": "x" * 100})

    for response in (api, missing, refused):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
        assert "geolocation=()" in response.headers["permissions-policy"]
    assert refused.status_code == 413


def test_the_policy_allows_this_origin_and_google_fonts_and_nothing_else() -> None:
    with TestClient(create_app()) as client:
        policy = client.get("/healthz").headers["content-security-policy"]

    directives = dict(part.strip().split(" ", 1) for part in policy.split(";"))
    assert directives["default-src"] == "'self'"
    assert directives["script-src"] == "'self'"  # no inline script, no eval
    assert directives["frame-ancestors"] == "'none'"
    assert directives["object-src"] == "'none'"
    assert directives["connect-src"] == "'self'"
    assert directives["font-src"] == "'self' https://fonts.gstatic.com"
    assert "https://fonts.googleapis.com" in directives["style-src"]
    assert "*" not in policy and "unsafe-eval" not in policy


def test_the_docs_page_is_left_without_the_policy_it_would_break_on() -> None:
    with TestClient(create_app()) as client:
        docs = client.get("/docs")

    assert docs.status_code == 200
    assert "content-security-policy" not in docs.headers
    assert docs.headers["x-content-type-options"] == "nosniff"


def test_hsts_only_over_https() -> None:
    with TestClient(create_app()) as client:
        plain = client.get("/healthz")
        secure = _https(client).get("/healthz")

    assert "strict-transport-security" not in plain.headers
    assert secure.headers["strict-transport-security"].startswith("max-age=")


def test_a_header_set_by_the_route_is_not_overwritten() -> None:
    app = FastAPI()

    @app.get("/x")
    async def x():
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse("x", headers={"Referrer-Policy": "no-referrer"})

    app.add_middleware(http_guard.SecurityHeadersMiddleware)

    with TestClient(app) as client:
        assert client.get("/x").headers["referrer-policy"] == "no-referrer"


def test_the_limit_middleware_is_what_the_app_installs() -> None:
    names = [m.cls.__name__ for m in create_app().user_middleware]
    assert names.index("SecurityHeadersMiddleware") < names.index("BodyLimitMiddleware")
    assert BodyLimitMiddleware.__name__ in names
