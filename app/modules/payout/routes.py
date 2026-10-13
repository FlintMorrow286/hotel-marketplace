"""Payouts to partners: the operator's side (admin scope) and the partner's own statement."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, field_validator

from app.db.pool import get_pool
from app.modules.auth.deps import require_scope
from app.modules.auth.jwt import TokenData
from app.modules.payout import service

admin_router = APIRouter(prefix="/v1/admin/payouts", tags=["payouts"])
partner_router = APIRouter(prefix="/v1/partner/payouts", tags=["payouts"])

_STATUS = "^(draft|approved|paid|cancelled)$"


class GenerateRequest(BaseModel):
    """`partner_id` omitted: every partner with something payable (and every open draft)."""

    partner_id: uuid.UUID | None = None


class CancelRequest(BaseModel):
    """What the caller believes it cancels: a draft (deleted) or an approved payout (cancelled)."""

    expect: Literal["draft", "approved"] | None = None


class PaidRequest(BaseModel):
    """The bank's payment reference: how the transfer is found in the bank's statement."""

    reference: str = Field(min_length=1, max_length=200)

    @field_validator("reference")
    @classmethod
    def _one_line(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("the payment reference is blank")
        return value


def _http(exc: service.PayoutError) -> HTTPException:
    if isinstance(exc, service.NotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, service.Conflict):
        return HTTPException(status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))


def _csv_response(detail: dict) -> Response:
    number = detail["number"]
    return Response(
        content=service.payout_csv(detail),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="payout-{number}.csv"',
            "Cache-Control": "no-store",
        },
    )


# ---------------------------------------------------------------- admin


@admin_router.get("/balances")
async def admin_balances(
    token: Annotated[TokenData, Depends(require_scope("admin"))] = None,
) -> dict:
    """What every partner is owed, and the rule that decides when."""
    try:
        async with get_pool().acquire() as conn:
            rows = await service.balances(conn)
        return {"rule": service.rule_info(service.current_rule()), "balances": rows}
    except service.PayoutError as exc:
        raise _http(exc) from exc


@admin_router.post("/generate")
async def admin_generate(
    data: GenerateRequest,
    token: Annotated[TokenData, Depends(require_scope("admin"))] = None,
) -> dict:
    """Build the draft payouts: for one partner, or for all of them."""
    try:
        async with get_pool().acquire() as conn:
            ids = (
                await service.generate(conn, str(data.partner_id))
                if data.partner_id is not None
                else await service.generate_all(conn)
            )
            return {"payouts": await service.payout_summaries(conn, ids)}
    except service.PayoutError as exc:
        raise _http(exc) from exc


@admin_router.get("")
async def admin_list(
    status_filter: Annotated[str | None, Query(alias="status", pattern=_STATUS)] = None,
    partner_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    token: Annotated[TokenData, Depends(require_scope("admin"))] = None,
) -> list[dict]:
    async with get_pool().acquire() as conn:
        return await service.list_payouts(
            conn, partner_id=partner_id, status=status_filter, limit=limit
        )


@admin_router.get("/{payout_id}")
async def admin_detail(
    payout_id: str,
    token: Annotated[TokenData, Depends(require_scope("admin"))] = None,
) -> dict:
    try:
        async with get_pool().acquire() as conn:
            return await service.payout_detail(conn, payout_id)
    except service.PayoutError as exc:
        raise _http(exc) from exc


@admin_router.get("/{payout_id}/export.csv")
async def admin_export(
    payout_id: str,
    token: Annotated[TokenData, Depends(require_scope("admin"))] = None,
) -> Response:
    try:
        async with get_pool().acquire() as conn:
            return _csv_response(await service.payout_detail(conn, payout_id))
    except service.PayoutError as exc:
        raise _http(exc) from exc


@admin_router.post("/{payout_id}/approve")
async def admin_approve(
    payout_id: str,
    token: Annotated[TokenData, Depends(require_scope("admin"))] = None,
) -> dict:
    """Freeze a draft. 409 when bookings changed since it was built: rebuild it first."""
    try:
        async with get_pool().acquire() as conn:
            await service.approve(conn, payout_id, token.sub)
            return await service.payout_summary(conn, payout_id)
    except service.PayoutError as exc:
        raise _http(exc) from exc


@admin_router.post("/{payout_id}/paid")
async def admin_paid(
    payout_id: str,
    data: PaidRequest,
    token: Annotated[TokenData, Depends(require_scope("admin"))] = None,
) -> dict:
    """Record that the transfer was made, with the bank's payment reference."""
    try:
        async with get_pool().acquire() as conn:
            await service.mark_paid(conn, payout_id, data.reference, token.sub)
            return await service.payout_summary(conn, payout_id)
    except service.PayoutError as exc:
        raise _http(exc) from exc


@admin_router.post("/{payout_id}/cancel")
async def admin_cancel(
    payout_id: str,
    data: CancelRequest | None = None,
    token: Annotated[TokenData, Depends(require_scope("admin"))] = None,
) -> dict:
    """Discard a draft, or cancel an approved payout that was not paid (its bookings return).

    Send `{"expect": "draft"}` or `{"expect": "approved"}` from a screen that shows the payout:
    if another operator moved it on, the answer is 409 and nothing changes.
    """
    try:
        async with get_pool().acquire() as conn:
            outcome = await service.cancel(
                conn, payout_id, token.sub, data.expect if data is not None else None
            )
        return {"id": payout_id, "outcome": outcome}
    except service.PayoutError as exc:
        raise _http(exc) from exc


# ---------------------------------------------------------------- the partner's own


@partner_router.get("/summary")
async def partner_summary(
    token: Annotated[TokenData, Depends(require_scope("partner"))] = None,
) -> dict:
    """What the platform owes this partner, and the rule that decides when."""
    try:
        async with get_pool().acquire() as conn:
            rows = await service.balances(conn, token.sub)
        for r in rows:
            r.pop("draft")  # the operator's workspace
        return {"rule": service.rule_info(service.current_rule()), "balances": rows}
    except service.PayoutError as exc:
        raise _http(exc) from exc


@partner_router.get("/pending")
async def partner_pending(
    limit: Annotated[int, Query(ge=1, le=2000)] = 200,
    token: Annotated[TokenData, Depends(require_scope("partner"))] = None,
) -> dict:
    """The bookings behind the balance: payable now, waiting for their date, or corrected.

    `{items, total}`: the first `limit` of `total`, so that a long list can be asked for in full.
    """
    try:
        async with get_pool().acquire() as conn:
            return await service.pending_bookings(conn, token.sub, limit)
    except service.PayoutError as exc:
        raise _http(exc) from exc


@partner_router.get("")
async def partner_list(
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    token: Annotated[TokenData, Depends(require_scope("partner"))] = None,
) -> list[dict]:
    """This partner's payouts, newest first; drafts are not shown."""
    async with get_pool().acquire() as conn:
        return await service.list_payouts(conn, partner_id=token.sub, as_partner=True, limit=limit)


@partner_router.get("/{payout_id}")
async def partner_detail(
    payout_id: str,
    token: Annotated[TokenData, Depends(require_scope("partner"))] = None,
) -> dict:
    """One of this partner's payouts with its lines; anyone else's is a 404."""
    try:
        async with get_pool().acquire() as conn:
            return await service.payout_detail(
                conn, payout_id, partner_id=token.sub, as_partner=True
            )
    except service.PayoutError as exc:
        raise _http(exc) from exc


@partner_router.get("/{payout_id}/export.csv")
async def partner_export(
    payout_id: str,
    token: Annotated[TokenData, Depends(require_scope("partner"))] = None,
) -> Response:
    try:
        async with get_pool().acquire() as conn:
            return _csv_response(
                await service.payout_detail(conn, payout_id, partner_id=token.sub, as_partner=True)
            )
    except service.PayoutError as exc:
        raise _http(exc) from exc
