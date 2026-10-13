"""The mail sender: delivers the letters the booking code queued (app/modules/notification/email.py).

Several processes may run it at once; the queue's row locks keep a letter with one of them.
"""

from __future__ import annotations

import asyncio
import time

from app.config.settings import settings
from app.db.pool import get_pool
from app.modules.notification import email
from app.utils.logger import get_logger

log = get_logger(__name__)

PURGE_EVERY_SEC = 3600.0


async def mail_loop() -> None:
    """Send due letters on a fixed cadence until cancelled; once an hour, drop the old ones."""
    log.info(
        "mail-started", backend=settings.mail_backend, interval_sec=settings.mail_poll_interval_sec
    )
    last_purge = 0.0
    while True:
        # Wait first: a process that has only just started has nothing due, and a test that
        # boots the app must not have its letters taken from under it.
        await asyncio.sleep(settings.mail_poll_interval_sec)
        try:
            # A full batch means more is waiting: go on without the pause.
            while await email.send_due() >= email.BATCH:
                pass
            if time.monotonic() - last_purge >= PURGE_EVERY_SEC:
                last_purge = time.monotonic()
                pool = get_pool()
                conn = await pool.acquire()
                try:
                    purged = await email.purge_old(conn)
                finally:
                    await pool.release(conn)
                if purged:
                    log.info("mail-purged", rows=purged)
        except Exception as exc:
            log.error("mail-loop-error", error=repr(exc))
