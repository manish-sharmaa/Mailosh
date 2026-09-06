"""`mailosh.web.deps.viewer_now`: the reader's clock, not the server's.

`format_date` has always done its "today"/"yesterday"/clock-time arithmetic
in `now.tzinfo`. Every page route passed `datetime.now(UTC)`, so every time
the UI ever showed was a UTC time. These pin the cookie that fixes it.
"""

from __future__ import annotations

from datetime import UTC
from zoneinfo import ZoneInfo

from fastapi import Request

from mailosh.web.deps import viewer_now


def _request(cookie: str | None) -> Request:
    headers = [] if cookie is None else [(b"cookie", f"tz={cookie}".encode())]
    return Request({"type": "http", "headers": headers, "method": "GET", "path": "/"})


def test_a_valid_zone_cookie_sets_the_clock():
    now = viewer_now(_request("Asia/Kolkata"))
    assert now.tzinfo == ZoneInfo("Asia/Kolkata")
    assert now.utcoffset().total_seconds() == 5.5 * 3600


def test_no_cookie_means_utc():
    assert viewer_now(_request(None)).tzinfo == UTC


def test_an_unknown_zone_falls_back_to_utc_rather_than_failing_the_page():
    assert viewer_now(_request("Mars/Olympus_Mons")).tzinfo == UTC
    assert viewer_now(_request("../../etc/passwd")).tzinfo == UTC
    assert viewer_now(_request("x" * 200)).tzinfo == UTC
