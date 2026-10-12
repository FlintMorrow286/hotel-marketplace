"""Photo upload routes (partner scope).

Photos are multipart, so this router does not go through the JSON client of
the rest of the cabinet. Everything else is the same contract: a property the
partner does not own is a 404, never a 403 (no oracle about other partners'
ids), and every answer is the property's full photo list — the UI never holds
a stale order.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from app.config.settings import settings
from app.db.pool import get_pool
from app.modules.auth.deps import require_scope
from app.modules.auth.jwt import TokenData
from app.modules.property import photos
from app.modules.property.photos import PhotoError
from app.modules.property.schemas import PhotoOut
from app.utils.ratelimit_http import throttle

router = APIRouter(prefix="/v1", tags=["property-photos"])


async def _read_capped(upload: UploadFile) -> bytes:
    """Stream the upload with a hard byte cap.

    Reading in one go would let a hostile client post gigabytes straight into
    memory; the cap is checked while the bytes flow, so an oversized file is
    cut off, not buffered first.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > settings.photo_max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=(
                    f"photo too large: the limit is {settings.photo_max_bytes // (1024 * 1024)} MB"
                ),
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.get(
    "/partner/properties/{property_id}/photos",
    response_model=list[PhotoOut],
)
async def list_property_photos(
    property_id: str,
    token: Annotated[TokenData, Depends(require_scope("partner"))],
) -> list[PhotoOut]:
    conn = await get_pool().acquire()
    try:
        result = await photos.list_photos(conn, property_id, token.sub)
        if result is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="property not found")
        return [PhotoOut(id=p.id, full=p.full, thumb=p.thumb) for p in result]
    finally:
        await get_pool().release(conn)


@router.post(
    "/partner/properties/{property_id}/photos",
    response_model=list[PhotoOut],
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(throttle("photo-upload", "photo_upload_limit_per_min"))],
)
async def upload_property_photo(
    property_id: str,
    token: Annotated[TokenData, Depends(require_scope("partner"))],
    file: Annotated[UploadFile, File(description="One photo: jpeg, png or webp")],
) -> list[PhotoOut]:
    conn = await get_pool().acquire()
    try:
        data = await _read_capped(file)
        if not data:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="empty upload",
            )
        try:
            result = await photos.add_photo(conn, property_id, token.sub, data)
        except PhotoError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        return [PhotoOut(id=p.id, full=p.full, thumb=p.thumb) for p in result]
    finally:
        await file.close()
        await get_pool().release(conn)


@router.delete(
    "/partner/properties/{property_id}/photos/{photo_id}",
    response_model=list[PhotoOut],
)
async def delete_property_photo(
    property_id: str,
    photo_id: str,
    token: Annotated[TokenData, Depends(require_scope("partner"))],
) -> list[PhotoOut]:
    conn = await get_pool().acquire()
    try:
        try:
            result = await photos.remove_photo(conn, property_id, token.sub, photo_id)
        except PhotoError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        return [PhotoOut(id=p.id, full=p.full, thumb=p.thumb) for p in result]
    finally:
        await get_pool().release(conn)
