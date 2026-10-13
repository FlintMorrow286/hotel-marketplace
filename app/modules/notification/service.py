"""Notifications.

- email: a durable queue written with the booking change, delivered by a job
  (app/modules/notification/email.py).
- Telegram: aiogram 3 bot to the partner, when TELEGRAM_BOT_TOKEN is set. Best-effort, after
  the commit: a notification failure must never break a booking.
"""

from __future__ import annotations

import asyncpg

from app.config.settings import settings
from app.utils.logger import get_logger

log = get_logger(__name__)

_bot = None


async def _telegram_bot():
    """Lazily build the aiogram bot; None if no token is configured."""
    global _bot
    if not settings.telegram_bot_token:
        return None
    if _bot is None:
        try:
            from aiogram import Bot

            _bot = Bot(token=settings.telegram_bot_token)
        except Exception as exc:
            log.warning("telegram-bot-init-failed", error=str(exc))
            return None
    return _bot


async def _partner_chat_id(conn: asyncpg.Connection, partner_id: str) -> str | None:
    """Partner's Telegram chat id (bound later via /start in the bot)."""
    return await conn.fetchval("SELECT telegram_chat_id FROM partner WHERE id = $1", partner_id)


async def notify_booking_confirmed(conn: asyncpg.Connection, booking: dict) -> None:
    """Tell the partner a booking was confirmed and paid."""
    partner_id = booking.get("partner_id")
    text = (
        "✅ Новая бронь {code}\n"
        "Гость: {guest}\n"
        "Заезд: {checkin}, выезд: {checkout}\n"
        "Сумма: {total} ₽ (комиссия {commission} ₽)"
    ).format(
        code=booking.get("code"),
        guest=booking.get("guest_name"),
        checkin=booking.get("checkin_date"),
        checkout=booking.get("checkout_date"),
        total=booking.get("total_amount"),
        commission=booking.get("commission_amt"),
    )

    # telegram
    if partner_id is None:
        return
    chat_id = await _partner_chat_id(conn, str(partner_id))
    bot = await _telegram_bot()
    if bot and chat_id:
        try:
            await bot.send_message(chat_id, text)
            log.info("notify-telegram-sent", chat_id=chat_id, code=booking.get("code"))
        except Exception as exc:
            log.warning("notify-telegram-failed", error=str(exc))
