"""The design system: a standalone Jinja2 environment (`env.py`) wired with
template-rendering helpers most templates need on every page — inlined
Lucide icons and a `<kbd>` renderer (`macros.py`), content-versioned
`/static/...` URLs (`static.py`), and avatar-related view helpers
(`format.py`; `format_date`/`format_senders` land in Task 6).

Kept separate from `mailosh.web.app`'s own `Jinja2Templates` (FastAPI's
thin wrapper, used for the actual request/response cycle) specifically so
`build_env` can be unit-tested — rendered, inspected — with no FastAPI
app, no HTTP request, and no route wiring at all; Task 5/6 point
`mailosh.web.app`'s template loader at the same `mailosh/web/templates`
tree these tests already exercise.
"""
