"""Shared test helper: the consent a web guest sends with every hold."""

from __future__ import annotations

from app.config import legal


def consent() -> dict:
    """Both boxes ticked and the current documents version, as the checkout sends them."""
    return {"docs_version": legal.LEGAL_DOCS_VERSION, "offer": True, "personal_data": True}
