"""Photo upload tests: storage, serving and the ownership rules.

Route-level, like the channel read tests: the multipart boundary, the byte cap
and the static mount only behave end to end through the real app. Images are
synthesised with Pillow, so nothing binary lives in the repo.
"""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import asyncpg
import pytest
from PIL import Image
from starlette.testclient import TestClient

from app.config.settings import settings
from app.modules.auth import service as auth_service
from app.modules.auth.jwt import create_access_token
from app.modules.auth.schemas import PartnerRegisterRequest
from app.modules.property import service as property_service
from app.modules.property.schemas import PropertyCreate, PropertyUpdate

_CLEANUP = (
    "DELETE FROM booking_line; DELETE FROM payment; DELETE FROM booking; "
    "DELETE FROM inventory_day; DELETE FROM unit_type; "
    "DELETE FROM property; DELETE FROM partner;"
)

_MEDIA_ORIG = settings.media_dir


def _png(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (120, 150, 180)).save(buf, format="PNG")
    return buf.getvalue()


def _jpeg(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (180, 140, 120)).save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture(scope="module")
def media_tmp(tmp_path_factory) -> Iterator:
    """Uploads must never land in the repo; the whole slice writes to a tmp dir."""
    import pathlib

    settings.media_dir = pathlib.Path(tmp_path_factory.mktemp("media"))
    try:
        yield settings.media_dir
    finally:
        settings.media_dir = _MEDIA_ORIG


@pytest.fixture(scope="module")
async def world(media_tmp, _test_db) -> AsyncIterator[dict]:
    """Two partners, one property each, and tokens for both."""
    pool = await asyncpg.create_pool(dsn=settings.test_dsn, min_size=1, max_size=5)
    conn = await pool.acquire()
    try:
        partner_a = await auth_service.register_partner(
            conn,
            PartnerRegisterRequest(email="photo-a@example.com", password="secret123", name="Alpha"),
        )
        partner_b = await auth_service.register_partner(
            conn,
            PartnerRegisterRequest(email="photo-b@example.com", password="secret123", name="Bravo"),
        )
        prop_a = await property_service.create_property(
            conn,
            partner_a,
            PropertyCreate(name="Дом у моря", property_type="house", timezone="Europe/Simferopol"),
        )
        prop_b = await property_service.create_property(
            conn,
            partner_b,
            PropertyCreate(name="Чужой дом", property_type="house", timezone="Europe/Simferopol"),
        )
        # The catalog only renders published properties.
        await property_service.update_property(
            conn, prop_a.id, partner_a, PropertyUpdate(status="published")
        )
        yield {
            "property_id": prop_a.id,
            "foreign_property_id": prop_b.id,
            "token_a": _login(conn, partner_a),
            "token_b": _login(conn, partner_b),
        }
    finally:
        await conn.execute(_CLEANUP)
        await pool.release(conn)
        await pool.close()


def _login(conn, partner_id: str) -> str:
    """A token straight from the JWT helper — no password round-trip needed."""
    return create_access_token(str(partner_id), "partner")


@pytest.fixture(scope="module")
def client(world: dict) -> Iterator[TestClient]:
    from app.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _local(media_root, url: str):
    """A served URL back to its path on disk (strip the mount prefix)."""
    from pathlib import Path

    return Path(media_root) / url.removeprefix(settings.media_url_prefix).lstrip("/")


# ---------------------------------------------------------------- upload + list


def test_upload_returns_list_and_writes_webp(client: TestClient, world: dict, media_tmp) -> None:
    resp = client.post(
        f"/v1/partner/properties/{world['property_id']}/photos",
        headers=_auth(world["token_a"]),
        files={"file": ("cottage.png", _png(2400, 1600), "image/png")},
    )
    assert resp.status_code == 201, resp.text
    photos = resp.json()
    assert len(photos) == 1
    (photo,) = photos
    assert photo["full"] != photo["thumb"]
    # The URL prefix is what the static mount serves.
    assert photo["full"].startswith(f"{settings.media_url_prefix}/properties/")
    assert photo["thumb"].endswith(".thumb.webp")

    # Both files exist and are real WebPs at the capped sizes.
    for key, max_edge in (
        ("full", settings.photo_max_dimension),
        ("thumb", settings.photo_thumb_width),
    ):
        path = _local(media_tmp, photo[key])
        assert path.is_file(), path
        with Image.open(path) as img:
            assert img.format == "WEBP"
            assert max(img.size) <= max_edge
    # The client's filename never reached the filesystem: every stored file is
    # a server-generated id, not what the partner's browser sent.
    prop_dir = _local(media_tmp, photo["full"]).parent
    names = sorted(p.name for p in prop_dir.iterdir())
    assert names == [f"{photo['id']}.thumb.webp", f"{photo['id']}.webp"]


def test_first_photo_is_the_cover(client: TestClient, world: dict) -> None:
    """Upload order is the catalog order; the first photo is the cover."""
    for name in ("second.jpg", "third.png"):
        kind = _jpeg(800, 600) if name.endswith(".jpg") else _png(800, 600)
        client.post(
            f"/v1/partner/properties/{world['property_id']}/photos",
            headers=_auth(world["token_a"]),
            files={"file": (name, kind, "image/jpeg")},
        )
    resp = client.get(
        f"/v1/partner/properties/{world['property_id']}/photos",
        headers=_auth(world["token_a"]),
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 3
    # The very first upload (the 2400x1600 PNG from the test above) stays first.
    assert resp.json()[0]["thumb"].endswith(".thumb.webp")


def test_media_is_served(client: TestClient, world: dict) -> None:
    listing = client.get(
        f"/v1/partner/properties/{world['property_id']}/photos",
        headers=_auth(world["token_a"]),
    ).json()
    resp = client.get(listing[0]["thumb"])
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/webp"


def test_catalog_exposes_photos(client: TestClient, world: dict) -> None:
    resp = client.get(f"/v1/properties/{world['property_id']}")
    assert resp.status_code == 200, resp.text
    photos = resp.json()["photos"]
    assert len(photos) == 3
    assert {"id", "full", "thumb"} == set(photos[0])


# ---------------------------------------------------------------- rejections


def test_non_image_is_415(client: TestClient, world: dict) -> None:
    resp = client.post(
        f"/v1/partner/properties/{world['property_id']}/photos",
        headers=_auth(world["token_a"]),
        files={"file": ("not-a-photo.png", b"\x89PNG\r\n\x1a\n" + b"junk" * 100, "image/png")},
    )
    assert resp.status_code == 415, resp.text


def test_wrong_magic_is_415(client: TestClient, world: dict) -> None:
    """A .png extension on a text file does not make it an image."""
    resp = client.post(
        f"/v1/partner/properties/{world['property_id']}/photos",
        headers=_auth(world["token_a"]),
        files={"file": ("lies.png", b"plain text, not an image", "image/png")},
    )
    assert resp.status_code == 415, resp.text


def test_oversized_is_413(client: TestClient, world: dict, monkeypatch) -> None:
    monkeypatch.setattr(settings, "photo_max_bytes", 1024)
    resp = client.post(
        f"/v1/partner/properties/{world['property_id']}/photos",
        headers=_auth(world["token_a"]),
        files={"file": ("big.png", _png(2000, 2000), "image/png")},
    )
    assert resp.status_code == 413, resp.text


def test_a_small_file_that_decodes_to_too_many_pixels_is_413(
    client: TestClient, world: dict, monkeypatch, media_tmp
) -> None:
    """A decompression bomb: tiny on the wire (a flat colour compresses to nothing), huge decoded."""
    monkeypatch.setattr(settings, "photo_max_pixels", 1_000_000)
    bomb = _png(2000, 1000)  # 2 megapixels
    assert len(bomb) < 20_000, "the point is that the file itself is small"
    before = sorted(p.name for p in media_tmp.rglob("*") if p.is_file())

    resp = client.post(
        f"/v1/partner/properties/{world['property_id']}/photos",
        headers=_auth(world["token_a"]),
        files={"file": ("bomb.png", bomb, "image/png")},
    )

    assert resp.status_code == 413, resp.text
    assert "at most 1 megapixels" in resp.json()["detail"]
    assert sorted(p.name for p in media_tmp.rglob("*") if p.is_file()) == before


def test_the_pixel_limit_is_checked_before_the_image_is_decoded(monkeypatch) -> None:
    """The size comes from the header: nothing may be converted or resized for a refused image."""
    from PIL import Image

    from app.modules.property import photos

    monkeypatch.setattr(settings, "photo_max_pixels", 1_000_000)
    bomb = _png(2000, 1000)  # built before the patches: saving it decodes

    def never(*args, **kwargs):
        raise AssertionError("the image must not be decoded")

    monkeypatch.setattr(Image.Image, "convert", never)
    monkeypatch.setattr(Image.Image, "load", never)

    with pytest.raises(photos.PhotoError) as raised:
        photos._store(bomb, Path("/nonexistent/full.webp"), Path("/nonexistent/t.webp"))

    assert raised.value.status_code == 413


def test_an_image_at_the_limit_is_accepted(client: TestClient, world: dict, monkeypatch) -> None:
    monkeypatch.setattr(settings, "photo_max_pixels", 2_000_000)

    resp = client.post(
        f"/v1/partner/properties/{world['property_id']}/photos",
        headers=_auth(world["token_a"]),
        files={"file": ("edge.png", _png(2000, 1000), "image/png")},
    )

    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_uploads_are_decoded_one_at_a_time(monkeypatch) -> None:
    """A burst of large uploads queues; it must not add their memory up."""
    import threading
    import time

    from app.modules.property import photos

    running = peak = 0
    lock = threading.Lock()

    def slow_store(data, full_path, thumb_path) -> None:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.05)
        with lock:
            running -= 1

    async def decode() -> None:
        async with photos._decode_slot():
            await asyncio.to_thread(slow_store, b"", None, None)

    await asyncio.gather(*(decode() for _ in range(5)))

    assert peak == 1


def test_upload_to_foreign_property_is_404(client: TestClient, world: dict) -> None:
    resp = client.post(
        f"/v1/partner/properties/{world['foreign_property_id']}/photos",
        headers=_auth(world["token_a"]),
        files={"file": ("x.png", _png(100, 100), "image/png")},
    )
    assert resp.status_code == 404, resp.text


def test_list_foreign_property_is_404(client: TestClient, world: dict) -> None:
    resp = client.get(
        f"/v1/partner/properties/{world['foreign_property_id']}/photos",
        headers=_auth(world["token_a"]),
    )
    assert resp.status_code == 404, resp.text


def test_photo_limit_is_enforced(client: TestClient, world: dict, monkeypatch) -> None:
    """The cap is checked inside the UPDATE, so it holds under concurrency too."""
    listing = client.get(
        f"/v1/partner/properties/{world['property_id']}/photos",
        headers=_auth(world["token_a"]),
    ).json()
    # Set the cap exactly at the current count: one more must not fit.
    monkeypatch.setattr(settings, "photo_max_per_property", len(listing))
    resp = client.post(
        f"/v1/partner/properties/{world['property_id']}/photos",
        headers=_auth(world["token_a"]),
        files={"file": ("over-the-limit.png", _png(100, 100), "image/png")},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]
    # And the property did not gain a photo.
    after = client.get(
        f"/v1/partner/properties/{world['property_id']}/photos",
        headers=_auth(world["token_a"]),
    ).json()
    assert len(after) == len(listing)


# ---------------------------------------------------------------- delete


def test_delete_removes_photo_and_files(client: TestClient, world: dict, media_tmp) -> None:
    listing = client.get(
        f"/v1/partner/properties/{world['property_id']}/photos",
        headers=_auth(world["token_a"]),
    ).json()
    photo = listing[-1]
    full_path = _local(media_tmp, photo["full"])
    thumb_path = _local(media_tmp, photo["thumb"])
    assert full_path.is_file()

    resp = client.delete(
        f"/v1/partner/properties/{world['property_id']}/photos/{photo['id']}",
        headers=_auth(world["token_a"]),
    )
    assert resp.status_code == 200, resp.text
    assert all(p["id"] != photo["id"] for p in resp.json())
    assert not full_path.exists()
    assert not thumb_path.exists()


def test_delete_twice_is_404(client: TestClient, world: dict) -> None:
    listing = client.get(
        f"/v1/partner/properties/{world['property_id']}/photos",
        headers=_auth(world["token_a"]),
    ).json()
    photo_id = listing[-1]["id"]
    client.delete(
        f"/v1/partner/properties/{world['property_id']}/photos/{photo_id}",
        headers=_auth(world["token_a"]),
    )
    resp = client.delete(
        f"/v1/partner/properties/{world['property_id']}/photos/{photo_id}",
        headers=_auth(world["token_a"]),
    )
    assert resp.status_code == 404, resp.text


def test_delete_on_foreign_property_is_404(client: TestClient, world: dict) -> None:
    resp = client.delete(
        f"/v1/partner/properties/{world['property_id']}/photos/{'0' * 32}",
        headers=_auth(world["token_b"]),
    )
    assert resp.status_code == 404, resp.text


def test_unauthenticated_is_401(client: TestClient, world: dict) -> None:
    resp = client.post(
        f"/v1/partner/properties/{world['property_id']}/photos",
        files={"file": ("x.png", _png(100, 100), "image/png")},
    )
    assert resp.status_code in (401, 403), resp.text


async def test_jsonb_shape_matches_service(client: TestClient, world: dict, _test_db) -> None:
    """The stored jsonb is the shape the catalog payload documents."""
    conn = await asyncpg.connect(dsn=settings.test_dsn)
    try:
        row = await conn.fetchval(
            "SELECT photos::jsonb FROM property WHERE id = $1",
            world["property_id"],
        )
    finally:
        await conn.close()
    assert row is not None
    stored = json.loads(row) if isinstance(row, str) else row
    assert isinstance(stored, list)
    assert all(set(p) == {"id", "full", "thumb"} for p in stored)
