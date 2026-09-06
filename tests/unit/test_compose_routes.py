"""Unit tests for the compose routes and their markup (Phase 1C, design
spec §8): `mailosh.web.compose`'s seven routes, the form-body parsing they
share, and the properties of the rendered dock that the whole design rests
on.

`compose.router` is mounted on a bare `FastAPI()` — never `create_app` —
the same shape `test_prefs.py` uses for `mailosh.web.prefs` and
`test_actions.py` for `mailosh.web.actions`. `deps.csrf_protect` is left
*real*, so every mutating route below is exercised through the actual CSRF
check; `require_session`/`current_user`/`prefs_for`/`client_for` are
overridden with stand-ins carrying only the attributes the router's own
dependency chain reads.

**The service is faked, the templates are not.** `mailosh.services.compose`
has its own exhaustive suite; what these tests own is the seam — that a
form body becomes the right `DraftInput`, that a `ComposeError` becomes an
inline message rather than a 500, and that what comes back is markup with
the right hooks in it. So the Jinja environment here is the app's real one
(`mailosh.ui.env.build_env`) rendering the real
`mailosh/web/templates/compose/*.html`: a template that stops carrying
`data-compose`, or starts carrying a `<dialog>`, fails here rather than in
a browser.

The service functions are patched **on `mailosh.web.compose`**, not on
`mailosh.services.compose`, because that module imports them by name — the
route's own reference is what a test has to replace.
"""

from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass, replace
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from mailosh.jmap.models import BodyPart, EmailBody
from mailosh.services.compose import (
    AttachmentRef,
    DraftInput,
    InvalidAddress,
    NoRecipients,
    Recipient,
    SendResult,
)
from mailosh.ui.env import build_env
from mailosh.web import compose as compose_module
from mailosh.web import deps
from mailosh.web.compose import router as compose_router

CSRF = "csrf-token-for-tests"

#: What every recorded call lands in, so a test can assert on the
#: `DraftInput` the route actually built rather than only on the response.
#: A module-level list would leak between tests; this is rebuilt per
#: fixture.
_STATIC_DIR = pathlib.Path("mailosh/web/static")


@dataclass
class FakeClient:
    """The two `JmapClient` methods these routes call, and nothing else.

    `get_thread` is what `GET /compose/reply/{id}` and `GET
    /compose/{draft_id}` read; `upload` is `POST /attachments`'s whole
    Stalwart half. Recording the arguments matters as much as the return
    values here — passing `build_reply` the *whole* thread rather than the
    one message being answered is load-bearing (it is how a reply's
    `References` chain is rebuilt when the original dropped the header),
    and that is only observable from the call.
    """

    thread: list[EmailBody]
    uploaded: list[tuple[bytes, str]]
    thread_calls: list[str]
    account_id: str = "acct"

    async def get_thread(self, thread_id: str) -> list[EmailBody]:
        self.thread_calls.append(thread_id)
        return self.thread

    async def upload(self, data: bytes, content_type: str) -> str:
        self.uploaded.append((data, content_type))
        return "blob-" + str(len(self.uploaded))


def _message(**overrides) -> EmailBody:
    fields = {
        "id": "m1",
        "threadId": "t1",
        "receivedAt": "2026-09-01T20:41:00Z",
        "preview": "hello",
        "hasAttachment": False,
        "subject": "Quarterly numbers",
        "from": [{"name": "Daniel Okafor", "email": "daniel@partner.test"}],
        "to": [{"name": "Demo", "email": "demo@mailosh.test"}],
    }
    fields.update(overrides)
    return EmailBody.model_validate(fields)


@pytest.fixture
def env():
    """`(app, client, calls)`: the router on a bare app, the fake JMAP
    client behind it, and the recorder every patched service function
    writes into.
    """
    jmap = FakeClient(thread=[_message()], uploaded=[], thread_calls=[])
    calls: dict[str, list] = {"save": [], "send": [], "discard": [], "reply": [], "identities": 0}

    app = FastAPI()
    app.include_router(compose_router)
    app.state.templates = Jinja2Templates(env=build_env(_STATIC_DIR))

    app.dependency_overrides[deps.require_session] = lambda: SimpleNamespace(
        csrf_token=CSRF, user_id=1
    )
    app.dependency_overrides[deps.current_user] = lambda: SimpleNamespace(
        id=1, email="demo@mailosh.test"
    )
    app.dependency_overrides[deps.prefs_for] = lambda: SimpleNamespace(
        theme="light",
        density="comfortable",
        shortcuts=True,
        undo_send_seconds=10,
        default_reply="reply",
    )
    app.dependency_overrides[deps.client_for] = lambda: jmap
    # The two open-a-composer routes read this user's saved signature
    # (`repo.signature_map`); `signatures` below is what that returns here.
    app.dependency_overrides[deps.get_db] = lambda: None
    return SimpleNamespace(app=app, jmap=jmap, calls=calls, signatures={})


@pytest.fixture
def stub(env, monkeypatch):
    """Patch the four service entry points the routes call, recording every
    argument. Each returns something plausible; individual tests replace
    one of them to raise instead.
    """

    async def save_draft(client, draft):
        env.calls["save"].append(draft)
        return "draft-" + str(len(env.calls["save"]))

    async def send_draft(client, draft):
        env.calls["send"].append(draft)
        return SendResult(submission_id="sub-1", email_id="sent-1")

    async def discard_draft(client, draft_id):
        env.calls["discard"].append(draft_id)

    async def list_identities(client):
        env.calls["identities"] += 1
        return [SimpleNamespace(id="i1", email="demo@mailosh.test", name="Demo")]

    def build_reply(thread, reply_to_id, mode, me):
        env.calls["reply"].append((thread, reply_to_id, mode, me))
        return DraftInput(
            to=(Recipient(email="daniel@partner.test", name="Daniel Okafor"),),
            subject="Re: Quarterly numbers",
            html="<blockquote>original</blockquote>",
            text="> original",
            in_reply_to="root@partner.test",
            references=("root@partner.test",),
        )

    monkeypatch.setattr(compose_module, "save_draft", save_draft)
    monkeypatch.setattr(compose_module, "send_draft", send_draft)
    monkeypatch.setattr(compose_module, "discard_draft", discard_draft)
    monkeypatch.setattr(compose_module, "list_identities", list_identities)

    async def signature_map(db, user_id, account_id):
        return dict(env.signatures)

    monkeypatch.setattr(compose_module, "build_reply", build_reply)
    monkeypatch.setattr(compose_module.repo, "signature_map", signature_map)
    return env


def _client(app) -> TestClient:
    return TestClient(app)


def _post(app, url, data, *, token=CSRF, files=None):
    headers = {} if token is None else {"X-CSRF-Token": token}
    return _client(app).post(url, data=data, files=files, headers=headers)


def _dom_id(body: str) -> str:
    found = re.search(r'<section class="compose[^"]*" id="([^"]+)"', body)
    assert found is not None, body[:400]
    return found.group(1)


# ---------------------------------------------------------------------------
# CSRF — the router-level dependency, exactly as `prefs.router` carries it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url", ["/compose/draft", "/compose/send", "/compose/discard", "/attachments"]
)
def test_every_mutating_route_refuses_a_request_with_no_csrf_token(stub, url):
    assert _post(stub.app, url, {}, token=None).status_code == 403


@pytest.mark.parametrize(
    "url", ["/compose/draft", "/compose/send", "/compose/discard", "/attachments"]
)
def test_every_mutating_route_refuses_a_wrong_csrf_token(stub, url):
    assert _post(stub.app, url, {}, token="not-the-token").status_code == 403


def test_the_csrf_gate_runs_before_anything_is_saved(stub):
    """403 with zero downstream work — no `DraftInput` built, no service
    call, nothing written. That is the whole reason the dependency is
    router-level rather than a check inside each handler."""
    assert _post(stub.app, "/compose/draft", {"to": "ada@example.test"}, token=None).status_code
    assert stub.calls["save"] == []


def test_the_read_routes_are_exempt_by_method_not_by_a_second_dependency(stub):
    """`mailosh.security.csrf.validate` returns early for GET/HEAD/OPTIONS,
    so the four read routes sit under the same router-level dependency and
    pay only for the session lookup they need anyway. A GET that 403'd
    would mean the dock could never be opened."""
    assert _client(stub.app).get("/compose").status_code == 200


# ---------------------------------------------------------------------------
# GET /compose — the dock, and the properties the design rests on
# ---------------------------------------------------------------------------


def test_a_fresh_dock_renders_the_component_with_its_hooks(stub):
    body = _client(stub.app).get("/compose").text
    assert "data-compose" in body
    assert 'data-surface="dock"' in body
    assert 'x-data="compose"' in body
    assert 'data-state="open"' in body
    # The Squire root, the two body fields it syncs, and the file input.
    assert "data-editor" in body
    assert "data-html" in body
    assert "data-plain" in body
    assert "data-file" in body


def test_the_dock_is_never_modal_and_never_reaches_outside_itself(stub):
    """The single property the whole compose design rests on: an open dock
    leaves the inbox behind it fully interactive.

    Three things in the markup would break it, and none of them may appear
    — a `<dialog>` (the platform would take the keyboard, and
    `static/js/keys.js` stops dispatching entirely while `dialog[open]`
    matches anything), a swap aimed at `#main` or `#list` (the dock would
    replace the mail it is supposed to sit beside), and a pushed URL (a
    draft is not a place, and Back would then land on one).
    """
    body = _client(stub.app).get("/compose").text
    assert "<dialog" not in body
    assert "showModal" not in body
    assert 'hx-target="#main"' not in body
    assert 'hx-target="#list"' not in body
    assert 'hx-push-url="true"' not in body
    # ...and the one target it does name is its own state chip.
    dom_id = _dom_id(body)
    assert f'hx-target="#{dom_id}-state"' in body
    assert 'hx-push-url="false"' in body


def test_autosave_is_declared_exactly_as_the_spec_writes_it(stub):
    """Spec §8: "autosave 2 s after the last edit (`hx-trigger="input
    delay:2s"`, `hx-sync="this:replace"`)". Both halves matter — the delay
    restarts on each keystroke, and the sync keeps two saves from racing
    each other into two drafts."""
    body = _client(stub.app).get("/compose").text
    assert 'hx-post="/compose/draft"' in body
    assert 'hx-trigger="input delay:2s"' in body
    assert 'hx-sync="this:replace"' in body


def test_every_id_in_a_dock_is_namespaced_so_three_can_coexist(stub):
    """Spec §8 allows three docks at once, and they share a document. Two
    fetches must not produce the same ids."""
    first = _client(stub.app).get("/compose").text
    second = _client(stub.app).get("/compose").text
    assert _dom_id(first) != _dom_id(second)
    # The form posts its own id back, so a response can only land in the
    # dock that asked for it.
    dom_id = _dom_id(first)
    assert f'<input type="hidden" name="dom_id" value="{dom_id}">' in first


def test_the_from_picker_is_absent_for_a_single_identity(stub):
    """Spec §8: the identity picker exists only when there is more than one
    thing to pick. A select with one option is a control promising a
    choice that does not exist."""
    body = _client(stub.app).get("/compose").text
    assert 'name="identity_id"' not in body


def test_the_from_picker_appears_once_the_account_has_two_identities(stub, monkeypatch):
    async def two(client):
        return [
            SimpleNamespace(id="i1", email="demo@mailosh.test", name="Demo"),
            SimpleNamespace(id="i2", email="sales@mailosh.test", name=None),
        ]

    monkeypatch.setattr(compose_module, "list_identities", two)
    body = _client(stub.app).get("/compose").text
    assert 'name="identity_id"' in body
    assert 'value="i2"' in body
    assert "sales@mailosh.test" in body


def test_an_identity_lookup_failure_costs_the_picker_and_nothing_else(stub, monkeypatch):
    """A compose window whose `Identity/get` failed still has exactly one
    useful thing in it, which is somewhere to write."""

    async def boom(client):
        raise RuntimeError("no identities today")

    monkeypatch.setattr(compose_module, "list_identities", boom)
    response = _client(stub.app).get("/compose")
    assert response.status_code == 200
    assert 'name="identity_id"' not in response.text


# ---------------------------------------------------------------------------
# Recipient parsing — the form body -> `DraftInput` seam
# ---------------------------------------------------------------------------


def test_a_display_name_survives_the_round_trip(stub):
    _post(stub.app, "/compose/draft", {"to": "Ada Lovelace <ada@example.test>"})
    draft = stub.calls["save"][0]
    assert draft.to == (Recipient(email="ada@example.test", name="Ada Lovelace"),)


def test_one_field_may_carry_a_whole_pasted_list(stub):
    _post(stub.app, "/compose/draft", {"to": 'a@x.test, "B, Jr" <b@x.test>; c@x.test'})
    draft = stub.calls["save"][0]
    assert [person.email for person in draft.to] == ["a@x.test", "b@x.test", "c@x.test"]
    assert draft.to[1].name == "B, Jr"


def test_duplicates_collapse_case_insensitively_keeping_the_first_spelling(stub):
    _post(stub.app, "/compose/draft", {"to": ["Ada <ada@x.test>", "ADA@X.TEST"]})
    draft = stub.calls["save"][0]
    assert draft.to == (Recipient(email="ada@x.test", name="Ada"),)


def test_a_value_that_is_not_an_address_reaches_the_service_rather_than_vanishing(stub):
    """The route parses; it does not judge.

    `mailosh.services.compose` raises `InvalidAddress` naming the offender,
    and the dock marks that chip — so a typo becomes a correction. Dropping
    it here instead would produce a message that looks addressed to
    everyone the reader typed and quietly reaches fewer people, which is
    the failure the service's own docstring exists to forbid.
    """
    _post(stub.app, "/compose/draft", {"to": ["nonsense", "ok@x.test"]})
    draft = stub.calls["save"][0]
    assert [person.email for person in draft.to] == ["nonsense", "ok@x.test"]


def test_a_field_whose_first_fragment_is_unparsable_keeps_the_rest(stub):
    """Python 3.13 hardened `email.utils.getaddresses` to answer
    `[("", "")]` for a malformed *anything*, which would have taken the
    whole field down with one typo — five good addresses and one bad one
    becoming zero recipients. `_split_addresses` is why that cannot
    happen here."""
    _post(stub.app, "/compose/draft", {"to": "broken@, good@x.test; also@x.test"})
    draft = stub.calls["save"][0]
    assert [person.email for person in draft.to] == ["broken@", "good@x.test", "also@x.test"]


def test_cc_and_bcc_are_parsed_the_same_way_and_kept_apart(stub):
    _post(
        stub.app,
        "/compose/draft",
        {"to": "a@x.test", "cc": "b@x.test", "bcc": "c@x.test"},
    )
    draft = stub.calls["save"][0]
    assert (draft.to[0].email, draft.cc[0].email, draft.bcc[0].email) == (
        "a@x.test",
        "b@x.test",
        "c@x.test",
    )


def test_an_untouched_identity_picker_means_no_identity_not_the_empty_string(stub):
    """Every `str | None` field on `DraftInput` means "absent" by `None`,
    and an untouched `<input>` posts `""`."""
    _post(stub.app, "/compose/draft", {"to": "a@x.test", "identity_id": "", "in_reply_to": ""})
    draft = stub.calls["save"][0]
    assert draft.identity_id is None
    assert draft.in_reply_to is None


def test_the_text_part_is_derived_from_the_html_when_the_browser_sent_none(stub):
    """A message that is HTML-only is one a plain-text reader receives as
    nothing at all, so the route falls back to
    `mailosh.web.app.html_to_text` rather than sending an empty part."""
    _post(
        stub.app,
        "/compose/draft",
        {"to": "a@x.test", "html": "<div>first</div><div>second</div>", "text": ""},
    )
    draft = stub.calls["save"][0]
    # `html_to_text`'s own documented shape: a block boundary becomes a
    # paragraph break, and runs of blank lines collapse to one.
    assert draft.text == "first\n\nsecond"


def test_a_text_body_the_browser_did_send_is_left_alone(stub):
    _post(stub.app, "/compose/draft", {"to": "a@x.test", "html": "<b>hi</b>", "text": "hi there"})
    assert stub.calls["save"][0].text == "hi there"


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------


def test_an_upload_reaches_stalwart_and_comes_back_as_a_chip(stub):
    response = _post(
        stub.app,
        "/attachments",
        {"dom_id": "cabc"},
        files={"file": ("report.pdf", b"%PDF-1.7 body", "application/pdf")},
    )
    assert response.status_code == 200
    assert stub.jmap.uploaded == [(b"%PDF-1.7 body", "application/pdf")]
    assert "report.pdf" in response.text
    assert 'name="attachment"' in response.text
    assert "data-attachment" in response.text


def test_the_chip_carries_the_exact_json_the_form_parses_back(stub):
    """The hidden input and `_attachments` are one contract. A chip whose
    value the parser rejects is an attachment that vanishes from a message
    that looked complete."""
    response = _post(
        stub.app,
        "/attachments",
        {},
        files={"file": ("a.txt", b"1234567890", "text/plain")},
    )
    value = re.search(r'name="attachment" value="([^"]*)"', response.text)
    assert value is not None, response.text
    record = json.loads(value.group(1).replace("&#34;", '"').replace("&quot;", '"'))
    assert record == {"blob_id": "blob-1", "name": "a.txt", "size": 10, "type": "text/plain"}

    # ...and it round-trips: posting that value back produces the same ref.
    _post(stub.app, "/compose/draft", {"to": "a@x.test", "attachment": json.dumps(record)})
    assert stub.calls["save"][0].attachments == (
        AttachmentRef(blob_id="blob-1", name="a.txt", type="text/plain", size=10),
    )


def test_a_file_over_the_ceiling_is_refused_while_it_is_still_arriving(stub, monkeypatch):
    monkeypatch.setattr(compose_module, "MAX_ATTACHMENT_BYTES", 32)
    response = _post(
        stub.app,
        "/attachments",
        {},
        files={"file": ("big.bin", b"x" * 4096, "application/octet-stream")},
    )
    assert response.status_code == 413
    assert stub.jmap.uploaded == []


def test_a_malformed_attachment_field_is_refused_not_silently_dropped(stub):
    """Nothing a reader can do produces one — the only writer of these
    fields is the fragment this app rendered — so it means tampering, and
    the failure that must not exist is a message that sends looking
    complete with a file quietly missing."""
    for value in ["{not json", '"a string"', '{"name": "x"}']:
        response = _post(stub.app, "/compose/draft", {"to": "a@x.test", "attachment": value})
        assert response.status_code == 400, value
    assert stub.calls["save"] == []


# ---------------------------------------------------------------------------
# POST /compose/draft
# ---------------------------------------------------------------------------


def test_an_untouched_form_writes_nothing_at_all(stub):
    """A blank draft in the Drafts folder is litter the reader never asked
    for, and — unlike every other save — there is nothing in it to lose."""
    response = _post(stub.app, "/compose/draft", {"dom_id": "cabc"})
    assert response.status_code == 200
    assert stub.calls["save"] == []
    assert 'data-state="idle"' in response.text


@pytest.mark.parametrize("body", ["\u200b", "\u200b\n\n\n", "  \u00a0 ", "\ufeff"])
def test_a_body_made_only_of_invisible_characters_is_still_empty(stub, body):
    """Squire drops a U+200B into an empty document the moment a block
    command runs on it — make a list, quote it, clear the formatting — and
    `str.strip()` reports that as typed. It produced a real draft in the
    Drafts folder whose entire body was one invisible character."""
    _post(stub.app, "/compose/draft", {"text": body, "html": "<div>" + body + "</div>"})
    assert stub.calls["save"] == []


def test_a_body_that_is_only_an_image_still_counts_as_content(stub):
    """The one shape that renders as no text here and as something to the
    person receiving it."""
    _post(stub.app, "/compose/draft", {"html": '<div><img src="cid:x"></div>'})
    assert len(stub.calls["save"]) == 1


def test_a_reply_card_that_was_never_typed_into_is_still_empty(stub):
    """`in_reply_to`/`references` arrive with an untouched reply card and
    must not count as content."""
    _post(
        stub.app,
        "/compose/draft",
        {"in_reply_to": "root@x.test", "references": "root@x.test"},
    )
    assert stub.calls["save"] == []


def test_a_save_hands_the_new_draft_id_back_twice(stub):
    """JMAP bodies are immutable, so every save creates a new draft and
    destroys the last — the form has to be carrying the new id before the
    next save fires, or the Drafts folder fills with one copy per pause.

    Both halves of that round trip are markup: the state fragment carries
    it as `data-draft-id`, and the out-of-band input replaces the form's
    own hidden field. Neither needs a line of JavaScript."""
    response = _post(stub.app, "/compose/draft", {"dom_id": "cabc", "to": "a@x.test"})
    assert response.status_code == 200
    assert 'data-state="saved"' in response.text
    assert 'data-draft-id="draft-1"' in response.text
    assert 'id="cabc-draft"' in response.text
    assert 'name="draft_id" value="draft-1"' in response.text
    assert 'hx-swap-oob="true"' in response.text


def test_the_previous_draft_id_travels_with_the_next_save(stub):
    _post(stub.app, "/compose/draft", {"to": "a@x.test", "draft_id": "draft-9"})
    assert stub.calls["save"][0].draft_id == "draft-9"


def test_a_bad_address_is_an_inline_message_and_never_a_500(stub, monkeypatch):
    """Autosave fires every two seconds, so a half-typed address is the
    *normal* condition of a To field. `InvalidAddress` therefore has to
    read as a sentence in the toolbar, at `200` so htmx swaps it — and it
    carries the field and address so the client can mark the one chip that
    is wrong."""

    async def refuse(client, draft):
        raise InvalidAddress("ada@", "to", "not a local@domain address")

    monkeypatch.setattr(compose_module, "save_draft", refuse)
    response = _post(stub.app, "/compose/draft", {"dom_id": "cabc", "to": "ada@"})
    assert response.status_code == 200
    assert 'data-state="error"' in response.text
    assert 'data-error-field="to"' in response.text
    assert 'data-error-address="ada@"' in response.text
    # The reader's words, not the exception's: no talk of local@domain.
    assert "local@domain" not in response.text


def test_a_refused_save_leaves_the_form_pointing_at_the_stored_draft(stub, monkeypatch):
    async def refuse(client, draft):
        raise InvalidAddress("ada@", "to", "not a local@domain address")

    monkeypatch.setattr(compose_module, "save_draft", refuse)
    response = _post(
        stub.app, "/compose/draft", {"dom_id": "cabc", "to": "ada@", "draft_id": "draft-7"}
    )
    assert 'name="draft_id" value="draft-7"' in response.text


# ---------------------------------------------------------------------------
# POST /compose/send and /compose/discard
# ---------------------------------------------------------------------------


def test_send_answers_204_and_names_the_two_ids_it_produced(stub):
    response = _post(stub.app, "/compose/send", {"to": "a@x.test", "text": "hi"})
    assert response.status_code == 204
    assert response.content == b""
    trigger = json.loads(response.headers["HX-Trigger"])
    assert trigger == {"om:sent": {"submission_id": "sub-1", "email_id": "sent-1"}}
    assert stub.calls["send"][0].to[0].email == "a@x.test"


def test_a_send_with_nobody_to_send_to_is_a_400_with_copy_that_helps(stub, monkeypatch):
    """`NoRecipients` is the service's, checked before anything is created
    — Stalwart accepts the draft and only refuses the submission, leaving
    an orphan behind per attempt."""

    async def refuse(client, draft):
        raise NoRecipients()

    monkeypatch.setattr(compose_module, "send_draft", refuse)
    response = _post(stub.app, "/compose/send", {"text": "hi"})
    assert response.status_code == 400
    trigger = json.loads(response.headers["HX-Trigger"])
    assert trigger["om:error"]["toast"] == "Add a recipient first"
    # Not transient: re-posting an address the server just refused would
    # only fail again, unlike the `JmapError` case.
    assert trigger["om:error"]["retry"] is False


def test_discard_removes_the_stored_draft(stub):
    response = _post(stub.app, "/compose/discard", {"draft_id": "draft-3"})
    assert response.status_code == 204
    assert stub.calls["discard"] == ["draft-3"]
    assert json.loads(response.headers["HX-Trigger"]) == {"om:discarded": {}}


def test_discarding_a_never_saved_draft_is_still_a_204(stub):
    """ "There was nothing stored to delete" and "the stored copy is gone"
    are the same fact from the reader's chair."""
    response = _post(stub.app, "/compose/discard", {})
    assert response.status_code == 204
    assert stub.calls["discard"] == []


# ---------------------------------------------------------------------------
# GET /compose/reply/{email_id}
# ---------------------------------------------------------------------------


def test_a_reply_is_handed_the_whole_thread_not_just_the_message(stub):
    """Load-bearing, and invisible from this end: when the original
    carries no `References` header, `build_reply` rebuilds the chain from
    the message-ids of every older message in the thread. Passing one
    message re-roots the conversation, and the symptom only shows in other
    people's mailboxes."""
    stub.jmap.thread = [_message(id="m0"), _message(id="m1"), _message(id="m2")]
    response = _client(stub.app).get("/compose/reply/m1?thread=t1&mode=reply_all")
    assert response.status_code == 200
    thread, reply_to_id, mode, me = stub.calls["reply"][0]
    assert [message.id for message in thread] == ["m0", "m1", "m2"]
    assert (reply_to_id, mode, me) == ("m1", "reply_all", "demo@mailosh.test")
    assert stub.jmap.thread_calls == ["t1"]


def test_a_reply_renders_the_inline_card_by_default(stub):
    body = _client(stub.app).get("/compose/reply/m1?thread=t1").text
    assert 'data-surface="inline"' in body
    assert "compose-inline" in body
    # Pop-out is the one control a card has that a dock does not.
    assert 'data-role="popout"' in body
    assert "<dialog" not in body


def test_a_reply_can_be_asked_for_as_a_dock(stub):
    body = _client(stub.app).get("/compose/reply/m1?thread=t1&surface=dock").text
    assert 'data-surface="dock"' in body
    assert 'data-role="minimize"' in body


def test_the_reply_card_carries_its_threading_headers_as_fields(stub):
    body = _client(stub.app).get("/compose/reply/m1?thread=t1").text
    assert '<input type="hidden" name="in_reply_to" value="root@partner.test">' in body
    assert '<input type="hidden" name="references" value="root@partner.test">' in body


def test_the_quoted_body_is_an_attribute_value_never_markup_in_the_page(stub):
    """A reply quotes a message a stranger wrote. Rendering that HTML into
    this document even once — before Squire and DOMPurify have seen it —
    is the one thing a mail client may never do, so it travels as the
    value of a hidden input and the editor is served empty."""
    body = _client(stub.app).get("/compose/reply/m1?thread=t1").text
    assert "<blockquote>original</blockquote>" not in body
    assert "&lt;blockquote&gt;original&lt;/blockquote&gt;" in body
    assert re.search(r'<div class="compose-editor"[^>]*>\s*</div>', body)


def test_a_message_that_is_not_in_that_thread_is_a_404(stub):
    assert _client(stub.app).get("/compose/reply/nope?thread=t1").status_code == 404
    assert stub.calls["reply"] == []


def test_an_unknown_reply_mode_is_rejected_before_the_service_runs(stub):
    response = _client(stub.app).get("/compose/reply/m1?thread=t1&mode=sideways")
    assert response.status_code == 422
    assert stub.calls["reply"] == []


def test_a_reply_needs_the_thread_the_conversation_view_already_knows(stub):
    """`thread` is required for the same reason `GET /m/{id}/frame` takes
    one: the conversation already has the id, and asking Stalwart which
    thread a message is in before fetching that thread is a round trip
    nobody has to make."""
    assert _client(stub.app).get("/compose/reply/m1").status_code == 422


# ---------------------------------------------------------------------------
# GET /compose/{draft_id}
# ---------------------------------------------------------------------------


def test_a_saved_draft_reopens_with_its_fields_filled_in(stub):
    stub.jmap.thread = [
        _message(
            id="d1",
            subject="Half-written",
            to=[{"name": "Ada Lovelace", "email": "ada@example.test"}],
            cc=[{"email": "cc@example.test"}],
            textBody="what I had so far",
            htmlBody="<div>what I had so far</div>",
            inReplyTo=["root@x.test"],
            references=["root@x.test"],
        )
    ]
    body = _client(stub.app).get("/compose/d1").text
    assert 'data-surface="dock"' in body
    assert 'value="Half-written"' in body
    assert 'name="draft_id" value="d1"' in body
    assert 'value="Ada Lovelace &lt;ada@example.test&gt;"' in body
    assert 'value="cc@example.test"' in body
    # The Cc row is revealed, because it has something in it.
    assert re.search(r'data-field="cc"(?! hidden)', body)
    assert '<input type="hidden" name="in_reply_to" value="root@x.test">' in body


def test_a_reopened_draft_brings_its_attachments_back(stub):
    stub.jmap.thread = [
        _message(
            id="d1",
            attachments=[
                BodyPart(
                    blobId="b1", name="deck.pdf", type="application/pdf", size=2048
                ).model_dump(by_alias=True)
            ],
        )
    ]
    body = _client(stub.app).get("/compose/d1").text
    assert "deck.pdf" in body
    assert "2 KB" in body
    assert '"blob_id":"b1"' in body.replace("&#34;", '"').replace("&quot;", '"')


def test_a_draft_that_is_gone_is_a_404(stub):
    stub.jmap.thread = []
    assert _client(stub.app).get("/compose/nope").status_code == 404


# ---------------------------------------------------------------------------
# The external-domain hint (spec §8)
# ---------------------------------------------------------------------------


def test_an_address_outside_the_senders_domain_is_marked_and_one_inside_is_not(stub, monkeypatch):
    def build(thread, reply_to_id, mode, me):
        return DraftInput(
            to=(Recipient(email="outside@partner.test"),),
            cc=(Recipient(email="colleague@mailosh.test"),),
        )

    monkeypatch.setattr(compose_module, "build_reply", build)
    body = _client(stub.app).get("/compose/reply/m1?thread=t1").text

    def classes(address):
        found = re.search(r'<span class="recipient([^"]*)"[^>]*data-email="' + address + '"', body)
        assert found is not None, address
        return found.group(1)

    outside = classes("outside@partner.test")
    inside = classes("colleague@mailosh.test")
    assert "is-external" in outside
    assert "is-external" not in inside
    # The form carries the domain the client half compares against, so a
    # chip typed into the dock lands on the same answer.
    assert 'data-domain="mailosh.test"' in body


# ---------------------------------------------------------------------------
# The layout's half of the contract
# ---------------------------------------------------------------------------


REPO = pathlib.Path(__file__).resolve().parents[2]
APP_LAYOUT = REPO / "mailosh/web/templates/layouts/app.html"


def test_the_editor_is_not_on_the_app_shell():
    """Squire, DOMPurify and `compose.js` are 36 KB gz — 40% of spec §11's
    whole 90 KB shell budget — and none of it does anything until a reader
    opens a dock.

    They are loaded by `js/compose-boot.js` on the first compose action.
    This fails if any of the three is tagged again, which is the easy
    mistake: a script tag added next to a feature looks harmless and costs
    every page view.
    """
    markup = APP_LAYOUT.read_text()
    tagged = re.findall(r"<script src=\"\{\{ static\('([^']+)'\) \}\}\"", markup)
    for name in ("vendor/purify.min.js", "vendor/squire.js", "js/compose.js"):
        assert name not in tagged, (name, tagged)
    assert "js/compose-boot.js" in tagged


def test_purify_still_loads_before_squire_and_both_before_compose():
    """The guarantee did not go away with the tags — it moved.

    Squire's default config calls a **global** `DOMPurify` from its own
    `sanitizeToDOMFragment`, so a Squire instance built before purify has
    executed throws the first time anything is parsed: a paste, or simply
    seeding a reply with its quoted body. That used to be enforced by the
    order of two script tags. It is now enforced by the order of two awaited
    loads, and this test follows it there rather than being deleted with the
    thing it used to watch.

    The URLs travel on `<body>` because the loader cannot call `static()`
    and the CSP forbids the inline script that would otherwise carry them.
    """
    boot = (REPO / "mailosh/web/static/js/compose-boot.js").read_text()
    # The *load calls*, not the first mention of each name. Both appear in
    # the destructuring line above them whatever the order, so asserting on
    # `index("composePurify")` passed happily with the loads reversed —
    # caught by mutating the file and watching this stay green.
    purify = boot.index("loadClassic(composePurify)")
    squire = boot.index("loadClassic(composeSquire)")
    compose = boot.index('import("./compose.js")')
    assert purify < squire < compose, (purify, squire, compose)
    # Awaited in sequence, not started together: `Promise.all` here would
    # race Squire against the global it needs.
    assert "Promise.all" not in boot

    markup = APP_LAYOUT.read_text()
    assert "data-compose-purify=\"{{ static('vendor/purify.min.js') }}\"" in markup
    assert "data-compose-squire=\"{{ static('vendor/squire.js') }}\"" in markup


def test_the_dock_mounts_outside_main_and_outside_history():
    """`#compose-dock` is a sibling of the grid, not a child of `#main` —
    which is what leaves the inbox behind an open dock scrolling, clickable
    and live. `hx-history="false"` keeps a draft in progress out of the
    back button."""
    markup = APP_LAYOUT.read_text()
    mount = re.search(r'<div id="compose-dock"([^>]*)>', markup)
    assert mount is not None
    assert 'hx-history="false"' in mount.group(1)
    # It is declared after `</main>`'s enclosing grid closes, so no swap
    # that targets `#main` can ever reach it.
    assert markup.index('id="main"') < markup.index('id="compose-dock"')
    assert markup.index("</div>", markup.index("</main>")) < markup.index('id="compose-dock"')


def test_no_compose_template_carries_an_inline_expression_the_csp_build_rejects():
    """`script-src 'self'` with no `'unsafe-eval'`, and a CSP-build Alpine.
    The only directive allowed in these templates is a bare
    `x-data="compose"` naming a component registered with `Alpine.data()`;
    every other form compiles a string at runtime and dies silently."""
    for path in sorted((REPO / "mailosh/web/templates/compose").glob("*.html")):
        markup = re.sub(r"\{#.*?#\}", "", path.read_text(), flags=re.S)
        assert "hx-on" not in markup, path
        assert "js:" not in markup, path
        for directive in ("x-on:", "@click", "x-show", "x-bind", "x-init", "x-effect"):
            assert directive not in markup, (path, directive)
        for found in re.findall(r'x-data="([^"]*)"', markup):
            assert found == "compose", (path, found)


def test_the_dataclass_field_a_route_reads_is_the_one_the_service_ships():
    """`AttachmentRef.size` is never sent to Stalwart — the server sets it
    per RFC 8621 — and exists for the file chip and spec §8's 25 MB soft
    warning. This route renders it, so it stays."""
    ref = AttachmentRef(blob_id="b", name="n", type="text/plain", size=1536)
    assert compose_module._chip(ref)["size_display"] == "2 KB"
    assert compose_module._chip(replace(ref, size=0))["size_display"] == "0 B"
