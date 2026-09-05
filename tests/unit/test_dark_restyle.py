"""Unit tests for `mailosh.render.dark` (Task 12 brief, Step 1) plus the
persistence half it depends on but never touches itself: `SenderPref` /
`mailosh.db.repo.sender_pref` / `set_sender_restyle`.

Every fixture here is local to this module, except `db`
(`tests/conftest.py`'s in-memory aiosqlite session), the same shared,
long-standing fixture `test_db_models.py`/`test_prefs.py` already use for
every other repo-level test -- nothing new is added to `conftest.py`
itself.

What this module deliberately does NOT cover, because it belongs to other
Task 12 work in flight elsewhere and not to this task's file list:
`mailosh.render.frame_document.render_frame` actually inlining
`dark.INVERT_CSS`/`dark.COLOR_SCHEME_CSS` into a rendered frame, the
`POST /m/{id}/restyle` route, and that route's CSRF/foreign-sender checks.
The tests below pin the same two behaviours those would exercise --
"invert emits exactly two counter-inverting rules" and "the per-sender
choice is remembered and scoped" -- entirely at the `dark`/`repo` layer
this task owns.
"""

from __future__ import annotations

import pathlib

import pytest
from sqlalchemy import func, select

from mailosh.db import repo
from mailosh.db.models import SenderPref
from mailosh.render.dark import (
    COLOR_SCHEME_CSS,
    INVERT_CSS,
    background_is_light,
    declares_color_scheme,
    restyle_mode,
)

# ---------------------------------------------------------------------------
# restyle_mode: the decision table, verbatim from the Task 12 brief.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "theme,enabled,declares,light,expected",
    [
        ("light", True, False, True, "none"),
        ("dark", False, False, True, "none"),
        ("dark", True, True, True, "color-scheme"),
        ("dark", True, False, True, "invert"),
        ("dark", True, False, False, "none"),
    ],
)
def test_the_decision_table_is_exactly_the_spec(theme, enabled, declares, light, expected):
    assert restyle_mode(theme=theme, enabled=enabled, declares=declares, light=light) == expected


@pytest.mark.parametrize("theme", ["system", "dark", "anything-not-literally-light"])
def test_every_non_light_theme_name_reaches_the_same_branches(theme):
    """The brief's migration note: `prefs.theme == "system"` "computes the
    same mode" as `"dark"`. Resolved here, inside `restyle_mode` itself
    (`theme == "light"` is the only early exit), rather than requiring
    every caller to remap `"system"` to `"dark"` before calling -- so the
    contract is exactly "a light theme never restyles", independent of how
    many non-light theme names exist.
    """
    assert restyle_mode(theme=theme, enabled=True, declares=True, light=True) == "color-scheme"
    assert restyle_mode(theme=theme, enabled=True, declares=False, light=True) == "invert"
    assert restyle_mode(theme=theme, enabled=True, declares=False, light=False) == "none"
    assert restyle_mode(theme=theme, enabled=False, declares=True, light=True) == "none"


# ---------------------------------------------------------------------------
# declares_color_scheme
# ---------------------------------------------------------------------------


def test_color_scheme_is_detected_from_meta_or_css():
    assert declares_color_scheme('<meta name="color-scheme" content="dark light">', "")
    assert declares_color_scheme("", ":root{color-scheme:light dark}")
    assert not declares_color_scheme("<p>x</p>", "p{color:red}")


def test_color_scheme_detection_ignores_the_declared_value():
    """Presence only: spec §7 trusts *any* declared value unconditionally,
    so a dark-only or light-only declaration must be detected exactly like
    the "light dark" case above.
    """
    assert declares_color_scheme('<meta name="color-scheme" content="dark">', "")
    assert declares_color_scheme("", ":root{color-scheme:light}")


def test_color_scheme_detection_is_case_insensitive_and_survives_self_closing_meta():
    assert declares_color_scheme('<META NAME="COLOR-SCHEME" CONTENT="dark">', "")
    assert declares_color_scheme('<meta name="color-scheme" content="dark"/>', "")


def test_color_scheme_in_css_is_found_inside_a_nested_at_rule():
    """Mirrors `css_sanitize`'s own "`@media` can nest" allowance: the scan
    must not stop at the top level only.
    """
    css = "@media (prefers-color-scheme: dark) { :root { color-scheme: dark; } }"
    assert declares_color_scheme("", css)


def test_color_scheme_is_not_detected_from_an_unrelated_meta_or_property_value():
    assert not declares_color_scheme('<meta name="viewport" content="color-scheme">', "")
    assert not declares_color_scheme("", "body{background-color:color-scheme}")


# ---------------------------------------------------------------------------
# background_is_light
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "html,css,light",
    [
        ('<body bgcolor="#ffffff">', "", True),
        ('<body bgcolor="#111111">', "", False),
        ('<body bgcolor="black">', "", False),
        ("", "body{background-color:#fff}", True),
        ("", "body{background-color:#0d1015}", False),
        ("", "table{background-color:#eee}", True),
        ("<p>no background anywhere</p>", "", True),  # mail defaults to white paper
    ],
)
def test_background_luminance(html, css, light):
    assert background_is_light(html, css) is light


@pytest.mark.parametrize(
    "html,css,light",
    [
        ('<body bgcolor="#fff">', "", True),  # 3-digit hex shorthand
        ('<body bgcolor="#000">', "", False),
        ('<body BGCOLOR="WHITE">', "", True),  # uppercase attribute name + value
        ('<BODY bgcolor="navy">', "", False),  # uppercase tag name
        ('<table bgcolor="#ffffff"></table>', "", True),  # no body at all -> first table
        (
            '<body></body><table bgcolor="#ffffff"></table>',
            "",
            True,
        ),  # body present but bgcolor-less -> falls through to first table
        (
            '<body bgcolor="not-a-real-color"></body><table bgcolor="#ffffff"></table>',
            "",
            True,
        ),  # an unparseable body bgcolor is "no signal", not "dark" or "light"
        (
            "",
            "body{background-color:#000} table{background-color:#fff}",
            False,
        ),  # body's declaration outranks table's
        ("", "body, table{background-color:#111111}", False),  # comma-separated selector list
        ("", "table{background-color:not-a-color}", True),  # unparseable CSS value -> default
    ],
)
def test_background_luminance_edge_cases(html, css, light):
    assert background_is_light(html, css) is light


@pytest.mark.parametrize(
    "name,light",
    [
        ("black", False),
        ("silver", True),
        ("gray", False),
        ("white", True),
        ("maroon", False),
        ("red", False),
        ("purple", False),
        ("fuchsia", False),
        ("green", False),
        ("lime", True),
        ("olive", False),
        ("yellow", True),
        ("navy", False),
        ("blue", False),
        ("teal", False),
        ("aqua", True),
    ],
)
def test_all_sixteen_html_colour_names_parse(name, light):
    assert background_is_light(f'<body bgcolor="{name}">', "") is light


def test_the_colour_name_table_is_exactly_sixteen_entries():
    """Cardinality pin on "the sixteen HTML colour names" (module
    docstring): the sixteen cases above are exhaustive, not a sample of a
    larger table -- this fails the moment anyone quietly grows it towards
    the much larger CSS3 colour-name list.
    """
    from mailosh.render.dark import _HTML_COLOR_NAMES

    assert len(_HTML_COLOR_NAMES) == 16
    assert set(_HTML_COLOR_NAMES) == {
        "black",
        "silver",
        "gray",
        "white",
        "maroon",
        "red",
        "purple",
        "fuchsia",
        "green",
        "lime",
        "olive",
        "yellow",
        "navy",
        "blue",
        "teal",
        "aqua",
    }


def test_a_declared_dark_only_color_scheme_wins_over_no_bgcolor_at_all():
    """The "interesting case" this task calls out by name: a message that
    already ships a dark palette must not be inverted into a light one.
    With no `bgcolor`/`background-color` anywhere, a `color-scheme: dark`
    hint (declared without `light` alongside it) must itself be enough to
    answer "not light" -- this must NOT fall through to the "no signal ->
    light" default.
    """
    assert background_is_light("", ":root{color-scheme:dark}") is False


def test_a_declared_light_only_color_scheme_hints_light():
    assert background_is_light("", ":root{color-scheme:light}") is True


def test_light_dark_together_is_an_ambiguous_hint_and_falls_through_to_the_default():
    assert background_is_light("", ":root{color-scheme:light dark}") is True


def test_malformed_html_and_css_do_not_raise():
    assert background_is_light("<<<not html at all", "not valid css {{{") is True
    assert declares_color_scheme("<<<not html at all", "not valid css {{{") is False


# ---------------------------------------------------------------------------
# The two CSS fragments a caller (frame_document, owned elsewhere) inlines.
# ---------------------------------------------------------------------------


def test_invert_css_carries_exactly_two_spec_rules():
    """The interesting failure mode this task calls out by name: a lone
    `html{filter:invert(...)}` rule would turn every photograph into a
    negative. `INVERT_CSS` must carry *both* the inversion rule and the
    counter-inversion rule for images/background-images -- pinned by
    cardinality and by the two exact rule strings, not a loose substring
    match.
    """
    assert INVERT_CSS.count("invert(1) hue-rotate(180deg)") == 2
    assert INVERT_CSS == (
        "html{filter:invert(1) hue-rotate(180deg)}"
        'img,[style*="background-image"]{filter:invert(1) hue-rotate(180deg)}'
    )


def test_color_scheme_css_is_the_light_dark_override_not_a_specific_side():
    assert COLOR_SCHEME_CSS == "html{color-scheme:light dark}"


# ---------------------------------------------------------------------------
# SenderPref: the model + repo helpers persisting "Show original".
# ---------------------------------------------------------------------------


async def test_sender_pref_is_none_until_a_preference_is_set(db):
    user = await repo.get_or_create_user(db, "reader@x", "reader@x")
    assert await repo.sender_pref(db, user.id, "news@t.test") is None


async def test_set_sender_restyle_is_remembered_and_scoped_to_one_sender_and_user(db):
    user = await repo.get_or_create_user(db, "reader@x", "reader@x")
    pref = await repo.set_sender_restyle(db, user.id, "news@t.test", False)
    assert pref.dark_restyle is False

    again = await repo.sender_pref(db, user.id, "news@t.test")
    assert again is not None
    assert again.dark_restyle is False

    # a different sender for the same user, and the same sender for a
    # different user, are each untouched by the write above.
    assert await repo.sender_pref(db, user.id, "other@t.test") is None
    other_user = await repo.get_or_create_user(db, "reader2@x", "reader2@x")
    assert await repo.sender_pref(db, other_user.id, "news@t.test") is None


async def test_set_sender_restyle_updates_in_place_rather_than_duplicating(db):
    user = await repo.get_or_create_user(db, "reader@x", "reader@x")
    await repo.set_sender_restyle(db, user.id, "news@t.test", False)
    await repo.set_sender_restyle(db, user.id, "news@t.test", True)

    pref = await repo.sender_pref(db, user.id, "news@t.test")
    assert pref.dark_restyle is True

    result = await db.execute(
        select(func.count()).select_from(SenderPref).where(SenderPref.user_id == user.id)
    )
    assert result.scalar_one() == 1


async def test_set_sender_restyle_can_clear_an_override_back_to_none(db):
    user = await repo.get_or_create_user(db, "reader@x", "reader@x")
    await repo.set_sender_restyle(db, user.id, "news@t.test", False)
    pref = await repo.set_sender_restyle(db, user.id, "news@t.test", None)
    assert pref.dark_restyle is None


def test_sender_pref_primary_key_is_user_id_and_sender_email():
    """Schema-level pin, independent of the repo helpers above: the model
    itself must declare exactly this composite key and a nullable
    `dark_restyle`, matching `migrations/versions/0002_reading.py`'s
    `create_table` column-for-column.
    """
    pk_columns = {column.name for column in SenderPref.__table__.primary_key.columns}
    assert pk_columns == {"user_id", "sender_email"}
    assert SenderPref.__table__.columns["dark_restyle"].nullable is True


# ---------------------------------------------------------------------------
# Depth bounding (security review, Critical 1)
# ---------------------------------------------------------------------------


def test_a_deeply_nested_prelude_does_not_crash_the_reading_route():
    """A ~1.2 KB message must not make itself permanently unopenable.

    `tinycss2.serialize` recurses once per nested block, so `a((((…))))`
    raises `RecursionError` from about a kilobyte of input. `css_sanitize`
    has always bounded this before serialising; `dark` carried its own copy
    of the same walk with no such bound, and `frames.py` feeds it the *raw*
    `<style>` blocks by design -- it needs the unsanitised text to see a
    colour-scheme declaration the sanitiser would have dropped.

    The result was a 500 on `GET /m/{id}/html` for that message, forever,
    on the default preference set, with no way for the reader to open it and
    no indication the mail was hostile.
    """
    payload = "a" + "(" * 600 + ")" * 600 + "{color:red}"
    html = f"<style>{payload}</style><p>hi</p>"

    assert declares_color_scheme(html, payload) is False
    assert isinstance(background_is_light(html, payload), bool)


def test_a_deeply_nested_declaration_value_does_not_crash_either():
    """The prelude is one of three `serialize` sites; a value is another."""
    css = "body{background-color:" + "calc(" * 600 + "1px" + ")" * 600 + "}"

    assert isinstance(background_is_light(f"<style>{css}</style>", css), bool)


def test_bounding_the_depth_did_not_blind_the_ordinary_cases():
    """The guard skips only what it cannot read. Everything a real message
    declares must still be seen, or dark restyle silently stops working.
    """
    light = "body{background-color:#ffffff}"
    assert background_is_light(f"<style>{light}</style>", light) is True

    declared = ":root{color-scheme:dark}"
    assert declares_color_scheme(f"<style>{declared}</style>", declared) is True


def test_dark_never_calls_tinycss2_serialize_directly():
    """One bound, one place.

    This bug *was* the second copy of a walk drifting from the first, so the
    rule is structural rather than a matter of remembering: every serialise
    of attacker-supplied component values goes through
    `css_sanitize.serialize_bounded`. A new `tinycss2.serialize(` here would
    reintroduce exactly this crash on whichever site it was added to.
    """
    source = (pathlib.Path(__file__).resolve().parents[2] / "mailosh/render/dark.py").read_text()
    body = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith(("#", "*", '"'))
    )
    assert "tinycss2.serialize(" not in body
    assert "serialize_bounded(" in body
