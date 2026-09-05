"""Small, focused helpers over `mailosh.db.models`, each taking an
already-open `AsyncSession` (`db`) — this module never opens its own
session or engine (`mailosh.db.session` owns that). Kept deliberately thin
per the Task 1 brief: no session/auth logic lives here (that's Task 3's
`mailosh.security.sessions`), just the plain read/create/update helpers
the brief's "Produces" list names.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mailosh.db.models import AppUser, AuditLog, LabelMeta, SenderPref, UiPref, Visibility


async def get_or_create_user(db: AsyncSession, username: str, email: str) -> AppUser:
    """Return the `AppUser` for `username`, creating it (with `email`) if new.

    Looked up by `stalwart_username` — the one identity a login flow
    (Task 3) actually has: a repeat call for an already-known username
    returns the existing row as-is, it does not refresh `email` from the
    argument given this time (a lookup-or-create, not an upsert).

    Race-safe for a brand-new username (Task 1's own deferred note,
    "revisit in T5 concurrent login"): two requests for the same never-
    before-seen `username` — e.g. two browser tabs both completing a first-
    ever login at once — can both run the SELECT above and both see
    nothing, before either commits its INSERT. `stalwart_username`'s own
    UNIQUE constraint (design spec §12 / the Task 1 migration) then rejects
    whichever commit lands second with `IntegrityError` — caught here,
    rather than left to surface as an unhandled 500 on an otherwise-
    successful login: the failed insert is rolled back and the row the
    other request just created is re-selected and returned instead, so
    both callers converge on the exact same `AppUser` row either way.
    """
    result = await db.execute(select(AppUser).where(AppUser.stalwart_username == username))
    user = result.scalar_one_or_none()
    if user is not None:
        return user
    user = AppUser(stalwart_username=username, email=email)
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        result = await db.execute(select(AppUser).where(AppUser.stalwart_username == username))
        return result.scalar_one()
    await db.refresh(user)
    return user


async def get_prefs(db: AsyncSession, user_id: int) -> UiPref:
    """Return `user_id`'s `UiPref` row, creating an all-defaults row first if
    this user has never had one.
    """
    result = await db.execute(select(UiPref).where(UiPref.user_id == user_id))
    prefs = result.scalar_one_or_none()
    if prefs is not None:
        return prefs
    prefs = UiPref(user_id=user_id)
    db.add(prefs)
    await db.commit()
    await db.refresh(prefs)
    return prefs


async def set_prefs(db: AsyncSession, user_id: int, **fields: Any) -> None:
    """Update one or more columns on `user_id`'s `UiPref` row (created first,
    at its defaults, if this user never had one — same as `get_prefs`).
    """
    prefs = await get_prefs(db, user_id)
    for key, value in fields.items():
        setattr(prefs, key, value)
    await db.commit()


async def label_meta_map(db: AsyncSession, user_id: int, account_id: str) -> dict[str, LabelMeta]:
    """`{mailbox_id: LabelMeta}` for every label `user_id` has metadata for in
    `account_id`. A mailbox with no `LabelMeta` row simply has no entry here
    — callers fall back to defaults (system role/no color/`"show"`) rather
    than this helper inventing one.
    """
    result = await db.execute(
        select(LabelMeta).where(LabelMeta.user_id == user_id, LabelMeta.account_id == account_id)
    )
    return {row.mailbox_id: row for row in result.scalars()}


#: `set_label_meta`'s "leave this column exactly as it is" marker.
#:
#: `None` cannot do that job here: `LabelMeta.color` is nullable and `None`
#: is a *meaningful* value for it — "this label has no colour of its own,
#: fall back to the hashed one" (`mailosh.ui.format.label_color`). A caller
#: clearing a colour and a caller only changing visibility would otherwise
#: be indistinguishable, and one of the two would silently do the other's
#: job. Same reasoning for `sort_order`.
_KEEP: Any = object()


async def label_meta(
    db: AsyncSession, user_id: int, account_id: str, mailbox_id: str
) -> LabelMeta | None:
    """`user_id`'s `LabelMeta` row for one mailbox, or `None` if this label
    has no display metadata yet.

    Shaped like `sender_pref` rather than `get_prefs`: a `LabelMeta` row
    only ever comes into being through an explicit colour/visibility choice
    (`set_label_meta`), so "no row" is the ordinary state of most labels and
    inventing one on a read would be a write nobody asked for on every
    label the reader has never customised.
    """
    result = await db.execute(
        select(LabelMeta).where(
            LabelMeta.user_id == user_id,
            LabelMeta.account_id == account_id,
            LabelMeta.mailbox_id == mailbox_id,
        )
    )
    return result.scalar_one_or_none()


async def set_label_meta(
    db: AsyncSession,
    user_id: int,
    account_id: str,
    mailbox_id: str,
    *,
    color: str | None = _KEEP,
    visibility: str = _KEEP,
    sort_order: int | None = _KEEP,
) -> LabelMeta:
    """Create-or-update the display metadata for one label, touching only
    the columns actually named (see `_KEEP`).

    **This never writes the label's name or its nesting**, and it never can:
    those are `Mailbox` properties, live only in JMAP, and are changed
    through `Mailbox/set` (`mailosh.services.labels`). The row here is
    display metadata *about* a mailbox — design spec §10's "colour,
    visibility, pinned order in Postgres `label_meta`" — never a second copy
    of the label itself, which would then have to be kept in step with a
    server other clients also write to.

    A freshly-created row takes `Visibility.SHOW` for `visibility` when the
    caller did not name one, matching both the column default and
    `Visibility.parse(None)`, so a label given only a colour reads back
    exactly as it did before it had a row at all.
    """
    row = await label_meta(db, user_id, account_id, mailbox_id)
    if row is None:
        row = LabelMeta(
            user_id=user_id,
            account_id=account_id,
            mailbox_id=mailbox_id,
            color=None if color is _KEEP else color,
            visibility=Visibility.SHOW.value if visibility is _KEEP else visibility,
            sort_order=None if sort_order is _KEEP else sort_order,
        )
        db.add(row)
    else:
        if color is not _KEEP:
            row.color = color
        if visibility is not _KEEP:
            row.visibility = visibility
        if sort_order is not _KEEP:
            row.sort_order = sort_order
    await db.commit()
    await db.refresh(row)
    return row


async def forget_label_meta(
    db: AsyncSession, user_id: int, account_id: str, mailbox_id: str
) -> None:
    """Drop one label's display metadata. Deleting a row that was never
    there is a no-op, not an error — the caller (a label being deleted) has
    no way to know whether the reader ever gave it a colour.
    """
    await db.execute(
        delete(LabelMeta).where(
            LabelMeta.user_id == user_id,
            LabelMeta.account_id == account_id,
            LabelMeta.mailbox_id == mailbox_id,
        )
    )
    await db.commit()


async def prune_label_meta(
    db: AsyncSession, user_id: int, account_id: str, live_mailbox_ids: set[str]
) -> list[str]:
    """Delete this user's `LabelMeta` rows for mailboxes that no longer
    exist in `account_id`, returning the ids dropped.

    **`live_mailbox_ids` must be a complete `Mailbox/get` for the account**,
    not a filtered subset: this deletes by absence, so handing it "the
    labels I happen to be showing" would throw away the colour of every
    hidden label. The one caller (`mailosh.web.labels`) passes every mailbox
    the account has, role mailboxes included.

    Why prune at all, when nothing reads an orphan row anyway
    (`mailosh.services.mailbox_tree.build_nav` walks *mailboxes* and looks
    metadata up by id, so a row whose mailbox is gone is simply never
    consulted and can never render a ghost label): ids are the server's to
    reuse. A label deleted in Thunderbird leaves a row keyed on an id
    Stalwart is free to hand to the *next* mailbox created, at which point a
    brand-new label would silently inherit a colour — or worse, a
    `visibility` of `hide` — from a label the reader deleted months ago.
    Cleaning up on a path that already holds the full mailbox list costs one
    statement, and only when there is genuinely something to drop.

    Returns `[]` without issuing a DELETE when nothing is orphaned, which is
    the overwhelmingly common case.
    """
    rows = await label_meta_map(db, user_id, account_id)
    orphans = sorted(set(rows) - live_mailbox_ids)
    if not orphans:
        return []
    await db.execute(
        delete(LabelMeta).where(
            LabelMeta.user_id == user_id,
            LabelMeta.account_id == account_id,
            LabelMeta.mailbox_id.in_(orphans),
        )
    )
    await db.commit()
    return orphans


async def sender_pref(db: AsyncSession, user_id: int, sender_email: str) -> SenderPref | None:
    """`user_id`'s `SenderPref` row for `sender_email`, or `None` if this
    sender has no override yet.

    Deliberately not `get_prefs`-shaped: that helper auto-creates an
    all-defaults row on first lookup because `UiPref` is meant to always
    exist once a user does. A `SenderPref` row instead only ever comes into
    being through an explicit "Show original" click
    (`set_sender_restyle`), so a bare `SELECT` is both correct and enough —
    inventing a row here would be a write no caller asked for, on every
    single message render from a sender nobody has ever overridden.
    """
    result = await db.execute(
        select(SenderPref).where(
            SenderPref.user_id == user_id, SenderPref.sender_email == sender_email
        )
    )
    return result.scalar_one_or_none()


async def set_sender_restyle(
    db: AsyncSession, user_id: int, sender_email: str, value: bool | None
) -> SenderPref:
    """Create-or-update `user_id`'s `SenderPref` row for `sender_email`,
    setting `dark_restyle = value`.

    Today's one caller — the "Show original" route — always passes
    `False`; this stays a plain setter (not hardcoded to it) so a later
    "always restyle this sender" control, or a "forget this override"
    reset back to `None`, is a call site away rather than a schema or repo
    change.
    """
    pref = await sender_pref(db, user_id, sender_email)
    if pref is None:
        pref = SenderPref(user_id=user_id, sender_email=sender_email, dark_restyle=value)
        db.add(pref)
    else:
        pref.dark_restyle = value
    await db.commit()
    await db.refresh(pref)
    return pref


async def audit(
    db: AsyncSession,
    user_id: int | None,
    action: str,
    detail: dict[str, Any] | None,
    ip: str | None,
) -> None:
    """Append one `audit_log` row (design spec §9: login success/failure,
    logout, ...). Always commits — a caller that wants audit logging to
    never break the request it's part of wraps this call itself, rather
    than this helper silently swallowing a write failure.
    """
    db.add(AuditLog(user_id=user_id, action=action, detail=detail, ip=ip))
    await db.commit()
