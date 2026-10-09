"""Telegram bot: the partner's notification channel.

The partner copies a one-time code from the cabinet and sends /start <code> to
the bot. The bot binds the chat the message came from to that partner, and
notify_booking_confirmed starts delivering there.

Why a code and not the chat id in deep-link: the chat id is not a secret the
partner can type, and a deep link to the bot carries its payload in the open.
The code rotates after every successful link, so a stale screenshot of the
cabinet cannot bind a stranger's chat to someone's bookings.

Polling, not webhooks: one process, no public callback URL to provision, and
the bot is a notification sink that does not need webhook's latency.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.db.pool import get_pool
from app.utils.logger import get_logger

if TYPE_CHECKING:
    # aiogram is the optional `bot` extra and the bot runs only when a token is
    # set, so importing this module must not need it.
    from aiogram import Dispatcher
    from aiogram.filters import CommandObject
    from aiogram.types import Message

log = get_logger(__name__)


async def _link(conn, code: str, chat_id: str) -> str | None:
    """Bind a chat to the partner owning the code. Returns the partner's name.

    A chat belongs to one partner at a time. Before the bind, any other partner
    holding this chat is detached: /unlink matches on the chat alone, so a chat
    shared by two partners would let one /unlink the other's binding, and both
    would receive the bookings of whichever stayed. Detaching first keeps the
    UNIQUE index satisfiable when the chat moves between partners.
    """
    await conn.execute(
        """
        UPDATE partner SET telegram_chat_id = NULL, telegram_linked_at = NULL
        WHERE telegram_chat_id = $1 AND telegram_link_code != $2
        """,
        str(chat_id),
        code,
    )
    row = await conn.fetchrow(
        """
        UPDATE partner
        SET telegram_chat_id = $1,
            telegram_linked_at = now(),
            telegram_link_code = gen_random_uuid()::text
        WHERE telegram_link_code = $2
        RETURNING name::text
        """,
        str(chat_id),
        code,
    )
    return row["name"] if row else None


async def _unlink(conn, chat_id: str) -> str | None:
    """Forget the chat. /unlink stops notifications without rotating the code."""
    row = await conn.fetchrow(
        """
        UPDATE partner SET telegram_chat_id = NULL, telegram_linked_at = NULL
        WHERE telegram_chat_id = $1
        RETURNING name::text
        """,
        str(chat_id),
    )
    return row["name"] if row else None


async def _handle_start(message: Message, command: CommandObject) -> None:
    """Bind the chat to the partner whose code was passed as /start <code>."""
    raw = (command.args or "").strip()
    if not raw:
        await message.answer(
            "Привет! Это бот уведомлений о бронях.\n\n"
            "Чтобы привязать кабинет, скопируйте код из раздела «Telegram» "
            "и отправьте сюда командой:\n"
            "<code>/start ВАШ_КОД</code>",
            parse_mode="HTML",
        )
        return

    # The code is a uuid; aiogram's arg parser stops at the first dash group.
    code = raw.replace(" ", "")
    pool = get_pool()
    conn = await pool.acquire()
    try:
        name = await _link(conn, code, message.chat.id)
    finally:
        await pool.release(conn)

    if name is None:
        await message.answer(
            "Код не найден или уже использован. Скопируйте свежий код из "
            "раздела «Telegram» в кабинете и отправьте <code>/start КОД</code>.",
            parse_mode="HTML",
        )
        return

    await message.answer(
        f"Кабинет «{name}» привязан.\n\n"
        "Сюда будут приходить уведомления о подтверждённых бронях: код, гость, "
        "даты и сумма с комиссией.\n\n"
        "/help — список команд\n"
        "/unlink — отключить уведомления",
    )
    log.info("telegram-linked", partner=name, chat_id=message.chat.id)


async def _handle_help(message: Message) -> None:
    await message.answer(
        "Уведомления о бронях приходят сюда автоматически.\n\n"
        "/help — эта справка\n"
        "/unlink — отключить уведомления",
    )


async def _handle_unlink(message: Message) -> None:
    pool = get_pool()
    conn = await pool.acquire()
    try:
        name = await _unlink(conn, message.chat.id)
    finally:
        await pool.release(conn)

    if name is None:
        await message.answer("Этот чат не привязан к кабинету.")
        return

    await message.answer(f"Кабинет «{name}» отключён. Уведомления больше не придут.")
    log.info("telegram-unlinked", partner=name, chat_id=message.chat.id)


def build_dispatcher() -> Dispatcher:
    """Wire the handlers once; the polling loop reuses this."""
    from aiogram import Dispatcher
    from aiogram.filters import Command

    dp = Dispatcher()
    dp.message.register(_handle_start, Command("start"))
    dp.message.register(_handle_help, Command("help"))
    dp.message.register(_handle_unlink, Command("unlink"))
    return dp


async def bot_loop(token: str) -> None:
    """Poll the bot until the process stops. Runs as a background task.

    A network blip must not kill the channel: aiohttp's backoff retries, and
    a failure here is logged, not raised — the booking flow does not depend on
    the bot, only on notifications being best-effort.
    """
    from aiogram import Bot

    bot = Bot(token=token)
    dp = build_dispatcher()
    log.info("bot-started")
    try:
        await dp.start_polling(bot, allowed_updates=[])
    except Exception as exc:
        log.error("bot-polling-failed", error=str(exc))
    finally:
        await bot.session.close()
