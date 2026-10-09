"""A partner's own stop-sell must not be undone by the calendar feed.

`closed_source` tells imported closures from manual ones, and the importer
reopens only the imported ones when a date leaves the feed. Two orders used to
lose a manual closure: the feed blocking a day the partner had already closed
(the importer re-labelled it as its own), and the partner closing a day the feed
had already closed (the label stayed 'import'). Either way the day reopened when
the feed dropped it, and the room became bookable against the partner's wish.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.modules.inventory import service as inventory_service
from app.modules.sync import ical_import
from tests.test_ical_import import _seed

TODAY = dt.date.today()
DAY = TODAY + dt.timedelta(days=10)
NEXT = DAY + dt.timedelta(days=1)


async def _state(conn, unit_type_id: str, day: dt.date) -> tuple[bool, str]:
    row = await conn.fetchrow(
        "SELECT closed, closed_source FROM inventory_day WHERE unit_type_id = $1 AND date = $2",
        unit_type_id,
        day,
    )
    return row["closed"], row["closed_source"]


@pytest.mark.asyncio
async def test_the_feed_does_not_take_over_a_day_closed_by_hand(db_conn) -> None:
    ut = (await _seed(db_conn, "mc1@example.com"))["unit_type_id"]
    await inventory_service.close_range(db_conn, ut, DAY, DAY + dt.timedelta(days=1))

    await ical_import.apply_import(db_conn, ut, {DAY, NEXT})

    assert await _state(db_conn, ut, DAY) == (True, "manual")
    assert await _state(db_conn, ut, NEXT) == (True, "import")

    # The feed drops both days: only the imported one reopens.
    await ical_import.apply_import(db_conn, ut, set())

    assert await _state(db_conn, ut, DAY) == (True, "manual")
    assert await _state(db_conn, ut, NEXT) == (False, "manual")


@pytest.mark.asyncio
async def test_closing_by_hand_claims_a_day_the_feed_closed(db_conn) -> None:
    ut = (await _seed(db_conn, "mc2@example.com"))["unit_type_id"]
    await ical_import.apply_import(db_conn, ut, {DAY})
    assert await _state(db_conn, ut, DAY) == (True, "import")

    await inventory_service.close_range(db_conn, ut, DAY, DAY + dt.timedelta(days=1))
    assert await _state(db_conn, ut, DAY) == (True, "manual")

    await ical_import.apply_import(db_conn, ut, set())
    assert await _state(db_conn, ut, DAY) == (True, "manual")


@pytest.mark.asyncio
async def test_reopening_by_hand_leaves_the_source_alone(db_conn) -> None:
    """Opening is not claiming: the feed still blocks the day, so the next sync closes it again."""
    ut = (await _seed(db_conn, "mc3@example.com"))["unit_type_id"]
    await ical_import.apply_import(db_conn, ut, {DAY})

    await inventory_service.close_range(db_conn, ut, DAY, DAY + dt.timedelta(days=1), closed=False)
    assert await _state(db_conn, ut, DAY) == (False, "import")

    await ical_import.apply_import(db_conn, ut, {DAY})
    assert await _state(db_conn, ut, DAY) == (True, "import")
