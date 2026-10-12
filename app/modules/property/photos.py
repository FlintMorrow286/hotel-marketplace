"""Property photo storage.

Photos arrive as multipart uploads from the partner cabinet and leave as two
WebP files: a full size (long edge capped) and a card thumb. The DB keeps the
order (first photo = the listing's cover) as a jsonb array of
``{"id", "full", "thumb"}`` on ``property.photos``.

Security rules this module owns:

- The filename is **never** taken from the client: the stored name is a server
  generated id, so nothing the partner sends can reach the filesystem as a path
  component.
- The type is sniffed from magic bytes, not the declared ``Content-Type`` — a
  ``.jpg`` rename does not make a file a JPEG.
- The image is re-encoded with Pillow: a file that does not decode is rejected,
  and the bytes that hit disk are ours, not the partner's.
- Ownership is checked on every write the way the rest of the cabinet does it
  (``WHERE partner_id = $2``) — another partner's property answers 404.
- The count limit is enforced inside the same UPDATE that appends, so two
  concurrent uploads cannot both slip past it.
"""

from __future__ import annotations

import asyncio
import json
import uuid
import weakref
from dataclasses import dataclass
from pathlib import Path

import asyncpg

from app.config.settings import settings
from app.utils.logger import get_logger

log = get_logger(__name__)

# Magic bytes of the formats we accept. Anything else is a 415, whatever the
# browser claimed the file was.
_MAGIC: tuple[bytes, str] = (
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    # RIFF is also WAV/AVI; a WebP carries the format tag at offset 8.
    (b"RIFF", "webp"),
)

ACCEPTED_TYPES = tuple(sorted({t for _, t in _MAGIC}))


# A decode takes a few hundred MiB at the pixel limit: one at a time per worker, so a burst of
# uploads queues instead of adding up. (A semaphore belongs to one event loop, hence the map.)
_slots: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


def _decode_slot() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    slot = _slots.get(loop)
    if slot is None:
        slot = _slots[loop] = asyncio.Semaphore(1)
    return slot


class PhotoError(Exception):
    """A rejected upload, with the status code the route should answer."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class Photo:
    id: str
    full: str
    thumb: str


def _photo_dir(property_id: str) -> Path:
    return settings.media_dir / "properties" / property_id


def _urls(property_id: str, photo_id: str) -> tuple[Path, Path, str, str]:
    """(full path, thumb path, full url, thumb url) for one photo id."""
    base = _photo_dir(property_id)
    return (
        base / f"{photo_id}.webp",
        base / f"{photo_id}.thumb.webp",
        f"{settings.media_url_prefix}/properties/{property_id}/{photo_id}.webp",
        f"{settings.media_url_prefix}/properties/{property_id}/{photo_id}.thumb.webp",
    )


def _to_photos(raw) -> list[Photo]:
    if isinstance(raw, str):
        raw = json.loads(raw)
    return [
        Photo(id=p["id"], full=p["full"], thumb=p["thumb"])
        for p in (raw or [])
        if isinstance(p, dict) and {"id", "full", "thumb"} <= p.keys()
    ]


async def _property_owned(conn: asyncpg.Connection, property_id: str, partner_id: str) -> bool:
    """Cheap ownership probe — a photo route answers 404 for a foreign id."""
    return bool(
        await conn.fetchval(
            "SELECT 1 FROM property WHERE id = $1 AND partner_id = $2",
            property_id,
            partner_id,
        )
    )


async def list_photos(
    conn: asyncpg.Connection, property_id: str, partner_id: str
) -> list[Photo] | None:
    """None when the property is not this partner's."""
    raw = await conn.fetchval(
        "SELECT photos::jsonb FROM property WHERE id = $1 AND partner_id = $2",
        property_id,
        partner_id,
    )
    return None if raw is None else _to_photos(raw)


async def add_photo(
    conn: asyncpg.Connection,
    property_id: str,
    partner_id: str,
    data: bytes,
) -> list[Photo]:
    """Validate, store and record one upload. Returns the new photo list.

    Raises PhotoError (4xx). Nothing is written to disk before the bytes are
    known good, and nothing stays on disk if the DB write fails.
    """
    if not await _property_owned(conn, property_id, partner_id):
        # Same answer as any other partner endpoint: a foreign id is "not found".
        raise PhotoError(404, "property not found")

    if _sniff(data) is None:
        raise PhotoError(
            415,
            "unsupported file type; accept jpeg, png, webp",
        )

    photo_id = uuid.uuid4().hex
    full_path, thumb_path, full_url, thumb_url = _urls(property_id, photo_id)

    try:
        async with _decode_slot():
            await asyncio.to_thread(_store, data, full_path, thumb_path)
    except PhotoError:
        raise
    except Exception as exc:  # PIL: corrupt or truncated image
        log.warning("photo-decode-failed", property_id=property_id, error=str(exc))
        raise PhotoError(415, "could not decode the image; the file is corrupt") from exc

    # The count guard runs inside the UPDATE: two concurrent uploads both pass a
    # pre-check, but only one gets the row.
    raw = await conn.fetchval(
        """
        UPDATE property
        SET photos = photos || $2::jsonb
        WHERE id = $1
          AND partner_id = $3
          AND jsonb_array_length(photos) < $4
        RETURNING photos::jsonb
        """,
        property_id,
        json.dumps([{"id": photo_id, "full": full_url, "thumb": thumb_url}]),
        partner_id,
        settings.photo_max_per_property,
    )
    if raw is None:
        # Lost the race (or the list was already full): files were written, the
        # property never accepted them — take them back off disk.
        _unlink_pair(full_path, thumb_path)
        raise PhotoError(
            409,
            f"a property holds at most {settings.photo_max_per_property} photos",
        )

    log.info(
        "photo-added",
        property_id=property_id,
        photo_id=photo_id,
        bytes_in=len(data),
    )
    return _to_photos(raw)


async def remove_photo(
    conn: asyncpg.Connection, property_id: str, partner_id: str, photo_id: str
) -> list[Photo]:
    """Drop one photo. A filesystem hiccup never fails the request."""
    if not await _property_owned(conn, property_id, partner_id):
        raise PhotoError(404, "property not found")

    row = await conn.fetchrow(
        """
        UPDATE property
        SET photos = COALESCE(
              (SELECT jsonb_agg(elem)
                 FROM jsonb_array_elements(photos) AS elem
                WHERE elem->>'id' <> $2),
              '[]'::jsonb)
        WHERE id = $1
          AND partner_id = $3
          AND photos @> $4::jsonb
        RETURNING photos::jsonb
        """,
        property_id,
        photo_id,
        partner_id,
        json.dumps([{"id": photo_id}]),
    )
    if row is None:
        raise PhotoError(404, "photo not found")

    full_path, thumb_path, _, _ = _urls(property_id, photo_id)
    _unlink_pair(full_path, thumb_path)
    log.info("photo-removed", property_id=property_id, photo_id=photo_id)
    return _to_photos(row["photos"])


def _sniff(data: bytes) -> str | None:
    for magic, kind in _MAGIC:
        if data.startswith(magic):
            if kind == "webp" and data[8:12] != b"WEBP":
                return None
            return kind
    return None


def _cap_long_edge(img, max_edge: int):
    """Scale down so the longer side is at most max_edge; copies when in range."""
    from PIL import Image

    long_edge = max(img.size)
    if long_edge <= max_edge:
        return img.copy()
    scale = max_edge / long_edge
    return img.resize(
        (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
        Image.LANCZOS,
    )


def _store(data: bytes, full_path: Path, thumb_path: Path) -> None:
    """Decode once, write the full size and the thumb. Runs in a threadpool."""
    from io import BytesIO

    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        with Image.open(BytesIO(data)) as img:
            # Open is lazy: the size is known from the header, before a pixel is decoded.
            if img.width * img.height > settings.photo_max_pixels:
                raise PhotoError(413, _too_many_pixels())
            # EXIF orientation matters for phone shots: apply it before the
            # resize or thumbs come out sideways.
            img = ImageOps.exif_transpose(img).convert("RGB")

            full_path.parent.mkdir(parents=True, exist_ok=True)
            _cap_long_edge(img, settings.photo_max_dimension).save(
                full_path, format="WEBP", quality=82, method=4
            )
            _cap_long_edge(img, settings.photo_thumb_width).save(
                thumb_path, format="WEBP", quality=78, method=4
            )
    except UnidentifiedImageError as exc:
        raise PhotoError(415, "could not decode the image; the file is corrupt") from exc
    except Image.DecompressionBombError as exc:
        # Pillow's own guard, for a header that claims far more than the limit above.
        raise PhotoError(413, _too_many_pixels()) from exc


def _too_many_pixels() -> str:
    return f"image too large: at most {settings.photo_max_pixels // 1_000_000} megapixels"


def _unlink_pair(full_path: Path, thumb_path: Path) -> None:
    """Best-effort: the DB row is the source of truth, not the file system."""
    for path in (full_path, thumb_path):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("photo-file-not-removed", path=str(path), error=str(exc))
