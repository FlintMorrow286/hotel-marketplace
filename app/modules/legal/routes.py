"""Public legal info: the version of the documents a guest has to accept to book."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.config import legal

router = APIRouter(prefix="/v1/legal", tags=["legal"])


class LegalInfo(BaseModel):
    docs_version: str


@router.get("", response_model=LegalInfo)
async def get_legal() -> LegalInfo:
    """The current `LEGAL_DOCS_VERSION`.

    The checkout shows it next to the documents and sends it back with the hold; the hold
    route refuses any other version, so a guest never accepts text that has since changed.
    """
    return LegalInfo(docs_version=legal.LEGAL_DOCS_VERSION)
