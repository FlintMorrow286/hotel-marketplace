"""What the application lets Sentry see.

The SDK's defaults attach, to every error, the local variables of every frame, the request body, the request
headers and the log lines before it. Here that is the password of a failing login, a guest's e-mail and phone, the
booking token, a channel's API key and the caller's JWT, as text in a third party's database (measured on the
Compose stack: all of them reached a stand-in Sentry). What goes is the exception, its stack trace and the request
line, which is what finding the fault needs.

The request line holds the URL, and a calendar client cannot send a header, so the iCal feed's only credential is
in its path. The SDK's scrubber does not read the URL; `before_send` blanks it.
"""

from __future__ import annotations

import re
from typing import Any

import sentry_sdk
from sentry_sdk.scrubber import DEFAULT_DENYLIST, EventScrubber

# The SDK's own list of header and field names to blank does not know this header; names are matched lower-case.
_ALSO_BLANKED = ["x-booking-token"]

# Each pattern keeps the route and blanks the credential in it. tests/test_sentry_options.py fails for a route with a
# credential-named path parameter that no pattern covers.
URL_CREDENTIALS = (re.compile(r"(/v1/ical/)[^/?#]+(\.ics)"),)


def redact_credentials(url: str) -> str:
    for pattern in URL_CREDENTIALS:
        url = pattern.sub(r"\1[Filtered]\2", url)
    return url


def _before_send(event: Any, hint: Any) -> Any:
    request = event.get("request")
    if isinstance(request, dict) and isinstance(request.get("url"), str):
        request["url"] = redact_credentials(request["url"])
    return event


def sentry_options(dsn: str, environment: str) -> dict[str, Any]:
    return {
        "dsn": dsn,
        "environment": environment,
        "include_local_variables": False,
        "max_request_body_size": "never",
        "max_breadcrumbs": 0,
        "event_scrubber": EventScrubber(denylist=[*DEFAULT_DENYLIST, *_ALSO_BLANKED]),
        "before_send": _before_send,
    }


def init_sentry(dsn: str, environment: str, **overrides: Any):
    """sentry_sdk.init with the options above; `overrides` is for tests (a transport)."""
    return sentry_sdk.init(**{**sentry_options(dsn, environment), **overrides})
