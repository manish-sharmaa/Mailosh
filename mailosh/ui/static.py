"""Content-versioned `/static/...` URLs, so a browser (or an intermediary
cache/CDN) can be told to cache every asset forever — the URL itself
changes the moment the file's bytes do, rather than the app needing
`Cache-Control` revalidation round-trips or a manually-bumped version
number someone has to remember to update.

A short (8 hex char, 32-bit) prefix of the file's own SHA-256 is the
version, not a build timestamp or a monotonic counter: content-addressed,
so two builds that happen to produce byte-identical output (e.g. `make
css` run twice with no source change) get the same URL rather than
needlessly invalidating every cache that already has it.
"""

from __future__ import annotations

import functools
import hashlib
from collections.abc import Callable
from pathlib import Path

#: `sha256[:8]` is 8 hex characters = 4 bytes = 32 bits of the digest --
#: 1-in-4-billion odds of two *different* files colliding, vastly more than
#: enough for a cache-busting token (not a security/integrity check).
_HASH_PREFIX_LEN = 8


def make_static(static_dir: Path | str) -> Callable[[str], str]:
    """Build a `static(path) -> "/static/{path}?v={sha256[:8]}"` closure
    bound to `static_dir` (`mailosh/web/static` in production; the same
    directory `StaticFiles` in `mailosh.web.app` serves from).

    `path` is relative to `static_dir` and must name a real file — e.g.
    `"app.css"`, `"fonts/inter-latin.woff2"`, `"js/app.js"` — so the tag it
    produces never silently points at nothing; a typo'd or not-yet-built
    path raises `FileNotFoundError` immediately (at template-render time,
    i.e. effectively at build/test time for every path this app's own
    templates reference), the same "fail loud before it ships" contract
    `macros.make_icon` explicitly does NOT give unknown icon names (see
    that module's docstring for why the two helpers differ here).

    Each `path`'s hash is computed at most once per returned closure
    (`functools.lru_cache`) — "hashes file bytes at first call" — not once
    per process: `build_env` (and so this factory) runs again for every
    test, and in production every worker process builds its own `Environment`
    at startup, so the cache's lifetime is exactly one `Environment`'s.
    """
    static_dir = Path(static_dir)

    @functools.lru_cache(maxsize=None)
    def _content_hash(path: str) -> str:
        data = (static_dir / path).read_bytes()
        return hashlib.sha256(data).hexdigest()[:_HASH_PREFIX_LEN]

    def static(path: str) -> str:
        return f"/static/{path}?v={_content_hash(path)}"

    return static
