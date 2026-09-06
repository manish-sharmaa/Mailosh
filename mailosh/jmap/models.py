"""Pydantic models for JMAP objects (RFC 8620 core + RFC 8621 mail).

Every model uses camelCase aliases matching the JMAP wire format, with
``populate_by_name=True`` so callers can also build them with idiomatic
snake_case keyword arguments. Field names/optionality are cross-checked
against ``.reference/ihasmail/web/src/jmap/types.ts``.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel


class JmapModel(BaseModel):
    """Shared config: camelCase on the wire, snake_case in Python."""

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)


def _rebase_url(url: str, base_url: str) -> str:
    """Swap ``url``'s scheme+host for ``base_url``'s; keep path/query/fragment."""
    base = urlsplit(base_url)
    parts = urlsplit(url)
    return urlunsplit((base.scheme, base.netloc, parts.path, parts.query, parts.fragment))


class Session(JmapModel):
    """A resolved JMAP session (RFC 8620 §2), reduced to what this client needs."""

    api_url: str
    upload_url: str
    download_url: str
    event_source_url: str
    primary_account_id: str

    @classmethod
    def from_jmap(cls, raw: dict) -> Session:
        """Parse a raw ``GET /.well-known/jmap`` response body.

        The mail account id isn't a top-level field: it lives under
        ``primaryAccounts["urn:ietf:params:jmap:mail"]``, so this can't be a
        plain ``Session.model_validate(raw)``.
        """
        primary_account_id = raw.get("primaryAccounts", {}).get("urn:ietf:params:jmap:mail")
        return cls(
            api_url=raw.get("apiUrl"),
            upload_url=raw.get("uploadUrl"),
            download_url=raw.get("downloadUrl"),
            event_source_url=raw.get("eventSourceUrl"),
            primary_account_id=primary_account_id,
        )

    def rebase(self, base_url: str) -> Session:
        """Return a copy with apiUrl/uploadUrl/downloadUrl/eventSourceUrl
        rehosted onto ``base_url``.

        Stalwart advertises these URLs against its own configured
        ``serverHostname`` (e.g. ``https://mail.mailosh.test/...``), which is
        typically unreachable from outside its Docker network/TLS setup. The
        app must always talk back to the ``base_url`` it was configured with,
        not whatever host the server claims for itself.
        """
        return self.model_copy(
            update={
                "api_url": _rebase_url(self.api_url, base_url),
                "upload_url": _rebase_url(self.upload_url, base_url),
                "download_url": _rebase_url(self.download_url, base_url),
                "event_source_url": _rebase_url(self.event_source_url, base_url),
            }
        )


class Address(JmapModel):
    """A single JMAP EmailAddress (RFC 8621 §4.1.2.3)."""

    name: str | None = None
    email: str


class Identity(JmapModel):
    """A JMAP Identity (RFC 8621 §6.1): one of the account's "send as"
    addresses, reduced to what `JmapClient.send` needs to populate an
    outgoing message's `from` and `EmailSubmission`'s `identityId`. RFC 8621
    defines several more properties (`replyTo`, `bccTo`, `textSignature`,
    `htmlSignature`, `mayDelete`); none are needed yet, so — like
    `Mailbox.role` staying a plain `str` rather than an enum — they're left
    unmodeled rather than added speculatively.
    """

    id: str
    email: str
    name: str | None = None


def _flag_map_to_set(v: object) -> object:
    """JMAP represents id/keyword sets on the wire as ``{"id": true, ...}``."""
    if v is None:
        return set()
    if isinstance(v, dict):
        return {k for k, flag in v.items() if flag}
    return v


def _none_to_list(v: object) -> object:
    return [] if v is None else v


class Mailbox(JmapModel):
    """A JMAP Mailbox (RFC 8621 §2).

    ``sort_order``/``total_emails``/``unread_emails`` are required (no
    default): RFC 8621 marks them always-server-set, so a response missing
    one is a bug worth failing loudly on rather than silently reading as 0.
    ``parent_id``/``role`` stay optional — the spec allows both to be null.
    """

    id: str
    name: str
    parent_id: str | None = None
    role: str | None = None
    sort_order: int
    total_emails: int
    unread_emails: int


class EmailHeader(JmapModel):
    """The subset of a JMAP Email's metadata needed for an inbox row (RFC 8621 §4.1).

    ``has_attachment`` is required (no default), matching ``sort_order``/etc.
    on ``Mailbox``: a response that omits it fails loudly instead of silently
    reading as false. ``from_``/``subject`` stay optional/nullable, since
    RFC 8621 allows both to be absent or null (a message can legitimately
    have no ``From``).

    ``preview`` was required for the same reason and is no longer, because
    the assumption behind it turned out to be wrong. Stalwart omits the key
    entirely for a message with no body, which is not a malformed response —
    a body-less message has nothing to preview. That state was unreachable
    until Phase 1C, since nothing created drafts; now the compose dock
    autosaves one within seconds of the first keystroke, and "fail loudly"
    meant a `ValidationError` that took `/mail/drafts` and `/compose/{id}`
    down with a 500 for as long as that draft existed. Defaulting to `""` is
    the honest reading: absent means nothing to show, and every consumer
    already renders an empty preview as an empty row.
    """

    id: str
    thread_id: str
    mailbox_ids: set[str] = Field(default_factory=set)
    keywords: set[str] = Field(default_factory=set)
    from_: list[Address] = Field(default_factory=list, alias="from")
    subject: str | None = None
    received_at: datetime
    preview: str = ""
    has_attachment: bool

    @field_validator("mailbox_ids", "keywords", mode="before")
    @classmethod
    def _coerce_flag_maps(cls, v: object) -> object:
        return _flag_map_to_set(v)

    @field_validator("from_", mode="before")
    @classmethod
    def _coerce_from(cls, v: object) -> object:
        return _none_to_list(v)


class BodyPart(JmapModel):
    """One MIME body part reference (RFC 8621 §4.1.4 ``EmailBodyPart``),
    reduced to what an attachment listing / ``cid:`` rewrite needs — no
    ``charset``, ``headers`` or ``subParts``, none of which any current
    caller uses. Used both for ``EmailBody.attachments`` and, in principle,
    for a raw ``textBody``/``htmlBody`` entry (though this client only ever
    reads ``partId`` back out of those two before collapsing them to a flat
    string — see ``_resolve_body_part`` below).
    """

    part_id: str | None = None
    blob_id: str | None = None
    size: int = 0
    type: str = "application/octet-stream"
    name: str | None = None
    cid: str | None = None
    disposition: str | None = None


def _resolve_body_part(data: dict, *, list_key: str, truncated_key: str) -> dict:
    """Shared resolution behind ``EmailBody``'s ``_resolve_text_body``/
    ``_resolve_html_body`` validators: collapse a wire-shaped body-part list
    (``textBody``/``htmlBody`` — a list of ``EmailBodyPart`` refs) plus
    ``bodyValues`` into a flat string under that same key, and record
    whether that part's value was truncated under ``truncated_key``.

    Returns ``data`` unchanged whenever ``list_key`` isn't a list at all —
    the common shape of a real ``Email/get`` response when
    ``fetchTextBodyValues``/``fetchHTMLBodyValues`` was false (RFC 8621
    defaults both to false), or a flat-construction caller that never had a
    wire-shaped key here in the first place (e.g. built by hand from
    ``text_body=...``) — so resolution only ever runs when there is
    something wire-shaped to resolve. An absent or non-dict ``bodyValues``
    is treated the same as an empty one rather than an error, for the same
    reason. The result is ``None``/``False`` whenever there's no part, or
    its value isn't available (``bodyValues`` missing/empty, or missing
    that ``partId``).
    """
    body_part_list = data.get(list_key)
    if not isinstance(body_part_list, list):
        return data
    body_values = data.get("bodyValues")
    if not isinstance(body_values, dict):
        body_values = {}
    resolved = None
    truncated = False
    if body_part_list and isinstance(body_part_list[0], dict):
        part_id = body_part_list[0].get("partId")
        entry = body_values.get(part_id) if part_id is not None else None
        if isinstance(entry, dict):
            resolved = entry.get("value")
            truncated = bool(entry.get("isTruncated", False))
    return {**data, list_key: resolved, truncated_key: truncated}


class EmailBody(EmailHeader):
    """A full message body (RFC 8621 §4.1), as returned from a thread fetch.

    ``text_body``/``html_body`` are resolved flat strings, not the raw
    wire-format ``textBody``/``htmlBody`` (each a list of ``EmailBodyPart``
    refs into ``bodyValues``) — that resolution is what later tasks actually
    want to consume. Because ``to_camel("text_body") == "textBody"`` (and
    likewise for ``html_body``/``htmlBody``), each alias collides in *name*
    with its differently-shaped real JMAP property; ``_resolve_text_body``/
    ``_resolve_html_body`` below turn that collision into a feature (via the
    shared ``_resolve_body_part`` helper above), so
    ``EmailBody.model_validate(<raw Email/get response>)`` works directly.
    The two validators are deliberately kept separate rather than merged
    into one pass over both keys: each only ever reads/writes its own two
    wire keys (``textBody``/``textTruncated`` vs. ``htmlBody``/
    ``htmlTruncated``), so a malformed or absent ``textBody`` can never
    suppress the html resolution, and vice versa. A caller that already has
    flat strings (e.g. built by hand from ``text_body=...``) never has a
    wire-shaped ``textBody``/``htmlBody`` key at all, so both are a no-op
    for that path.

    ``return_path``/``auth_results`` read RFC 8621 §4.1.7 header
    pseudo-properties (``header:Return-Path:asText``/
    ``header:Authentication-Results:asText``) via an explicit ``Field``
    alias — ``to_camel`` would otherwise mangle those colon-separated wire
    names. Every other new field here (``bcc``, ``reply_to``, ``sent_at``,
    ``blob_id``, ``attachments``) is a plain passthrough with no collision
    to resolve, and every new field is defaulted so 1A's existing
    ``EmailBody(...)`` constructions keep validating unchanged.

    ``message_id``/``in_reply_to``/``references`` are RFC 8621 §4.1.3's
    three convenience properties over the RFC 5322 ``Message-ID``/
    ``In-Reply-To``/``References`` header fields. Each is a **list of
    strings with the angle brackets already removed** — that is what
    §4.1.2.4's ``asMessageIds`` form means, and it is symmetric: a value
    written back in an ``Email/set`` create is re-wrapped in ``<...>`` by
    the server (verified live against Stalwart: a create carrying
    ``references: ["root@x", "orig@x"]`` produces the header
    ``References: <root@x> <orig@x>``). ``message_id`` is a list rather
    than a scalar because the RFC models it that way; a well-formed
    message carries exactly one entry.
    ``mailosh.services.compose.build_reply`` reads all three to build a
    reply's own threading headers, which is the only reason they are
    modelled at all.
    """

    to: list[Address] = Field(default_factory=list)
    cc: list[Address] = Field(default_factory=list)
    bcc: list[Address] = Field(default_factory=list)
    reply_to: list[Address] = Field(default_factory=list)
    sent_at: datetime | None = None
    blob_id: str | None = None
    text_body: str | None = None
    html_body: str | None = None
    text_truncated: bool = False
    html_truncated: bool = False
    attachments: list[BodyPart] = Field(default_factory=list)
    message_id: list[str] = Field(default_factory=list)
    in_reply_to: list[str] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)
    return_path: str | None = Field(default=None, alias="header:Return-Path:asText")
    auth_results: str | None = Field(default=None, alias="header:Authentication-Results:asText")

    @field_validator(
        "to",
        "cc",
        "bcc",
        "reply_to",
        "attachments",
        "message_id",
        "in_reply_to",
        "references",
        mode="before",
    )
    @classmethod
    def _coerce_none_lists(cls, v: object) -> object:
        return _none_to_list(v)

    @model_validator(mode="before")
    @classmethod
    def _resolve_text_body(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        return _resolve_body_part(data, list_key="textBody", truncated_key="textTruncated")

    @model_validator(mode="before")
    @classmethod
    def _resolve_html_body(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        return _resolve_body_part(data, list_key="htmlBody", truncated_key="htmlTruncated")


class StateChange(JmapModel):
    """A JMAP StateChange push object (RFC 8620 §7.2), as delivered over SSE/EventSource."""

    changed: dict[str, dict[str, str]]


class DeliveryStatus(JmapModel):
    """One recipient's entry in ``EmailSubmission.deliveryStatus`` (RFC 8621
    §7, ``DeliveryStatus``).

    ``delivered`` is one of ``queued``/``yes``/``no``/``unknown`` and
    ``displayed`` one of ``unknown``/``yes``; both are kept as plain ``str``
    rather than enums, like ``Mailbox.role``, so a server extension value
    degrades to "unknown" in `mailosh.services.outbound.classify` instead of
    failing the whole ``EmailSubmission/get`` response. ``smtp_reply`` is
    the last SMTP reply the server has for that recipient — verified live
    against Stalwart 0.16, it starts out as ``"250 2.1.5 Queued"`` for every
    recipient the instant a message is accepted into the outbound queue,
    and only changes once a DSN (bounce or delay notice) is processed.
    """

    smtp_reply: str = ""
    delivered: str = "unknown"
    displayed: str = "unknown"


class EmailSubmission(JmapModel):
    """A JMAP EmailSubmission (RFC 8621 §7), reduced to what outbound
    delivery tracking reads back after `JmapClient.send_message` created it.

    Shape verified live against Stalwart 0.16 (``EmailSubmission/get``):
    ``id``, ``emailId``, ``threadId``, ``identityId``, ``envelope``,
    ``sendAt``, ``undoStatus`` (``pending``/``final``/``canceled``),
    ``deliveryStatus`` (a map keyed by recipient address, or ``null``),
    ``dsnBlobIds``, ``mdnBlobIds``. ``envelope`` and ``identityId`` are not
    modelled — nothing here reads them — and ``delivery_status`` defaults
    to an empty map for the ``null`` case so a caller can iterate it
    without a None check.
    """

    id: str
    email_id: str
    thread_id: str | None = None
    undo_status: str = "final"
    send_at: datetime | None = None
    delivery_status: dict[str, DeliveryStatus] = Field(default_factory=dict)
    dsn_blob_ids: list[str] = Field(default_factory=list)

    @field_validator("delivery_status", mode="before")
    @classmethod
    def _coerce_null_status(cls, v: object) -> object:
        return {} if v is None else v

    @field_validator("dsn_blob_ids", mode="before")
    @classmethod
    def _coerce_null_dsns(cls, v: object) -> object:
        return _none_to_list(v)
