import pytest
from fastapi import HTTPException
from starlette.requests import Request

from mailosh.security import csrf


def _req(method="POST", headers=None):
    scope = {
        "type": "http",
        "method": method,
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "path": "/",
        "query_string": b"",
    }
    return Request(scope)


def test_get_is_exempt():
    csrf.validate(_req("GET"), "t")


def test_header_token_matches():
    csrf.validate(_req(headers={"X-CSRF-Token": "t", "Sec-Fetch-Site": "same-origin"}), "t")


def test_missing_or_wrong_token_403():
    with pytest.raises(HTTPException) as e:
        csrf.validate(_req(headers={"X-CSRF-Token": "nope"}), "t")
    assert e.value.status_code == 403


def test_cross_site_rejected_even_with_token():
    with pytest.raises(HTTPException):
        csrf.validate(_req(headers={"X-CSRF-Token": "t", "Sec-Fetch-Site": "cross-site"}), "t")


# ---------------------------------------------------------------------------
# Coverage Task 3 left as hand-verified-but-untested (progress ledger: "HEAD/
# OPTIONS CSRF exemption and the form_token fallback verified by hand but not
# covered by committed tests — Task 5 relies on the form path; add coverage
# there") — Task 5's POST /login->/logout flow depends on both.
# ---------------------------------------------------------------------------


def test_head_is_exempt():
    csrf.validate(_req("HEAD"), "t")


def test_options_is_exempt():
    csrf.validate(_req("OPTIONS"), "t")


def test_form_token_fallback_used_when_no_header_present():
    # mailosh.web.auth's login form has no session yet to carry a header
    # token; every other plain-<form> POST (Task 5's /logout, /logout/all)
    # falls back to this same form_token path when no X-CSRF-Token header
    # is present.
    csrf.validate(_req(headers={"Sec-Fetch-Site": "same-origin"}), "t", form_token="t")


def test_form_token_wrong_value_still_403s():
    with pytest.raises(HTTPException) as e:
        csrf.validate(_req(headers={"Sec-Fetch-Site": "same-origin"}), "t", form_token="nope")
    assert e.value.status_code == 403


def test_missing_form_token_403s_the_same_as_missing_header():
    with pytest.raises(HTTPException) as e:
        csrf.validate(_req(headers={"Sec-Fetch-Site": "same-origin"}), "t", form_token=None)
    assert e.value.status_code == 403


def test_header_token_wins_even_when_form_token_is_wrong():
    # The header is checked first (`token = header or form_token`) — a
    # correct header must not be second-guessed by an incidental/absent
    # form_token on the same request.
    csrf.validate(
        _req(headers={"X-CSRF-Token": "t", "Sec-Fetch-Site": "same-origin"}), "t", form_token="nope"
    )


def test_is_cross_site_true_only_for_the_cross_site_value():
    assert csrf.is_cross_site(_req(headers={"Sec-Fetch-Site": "cross-site"})) is True


def test_is_cross_site_false_when_absent_or_same_origin():
    assert csrf.is_cross_site(_req()) is False
    assert csrf.is_cross_site(_req(headers={"Sec-Fetch-Site": "same-origin"})) is False
    assert csrf.is_cross_site(_req(headers={"Sec-Fetch-Site": "same-site"})) is False
