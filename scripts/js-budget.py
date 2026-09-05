"""Report what a production image would actually serve, in gzipped bytes.

Measures against the design spec's §11 budget (total JS <= 90 KB gz). Two
things make this worth a script rather than a one-liner:

- It measures the **minified** size of this app's own JavaScript without
  minifying anything on disk. `scripts/minify-js.py` writes in place, which
  is right inside a Docker build and wrong in a checkout.
- It follows what the layout actually loads -- the `<script>` tags and their
  transitive imports -- rather than summing the directory. `auth.js` is only
  on the sign-in page, and a module nothing imports costs a reader nothing.
"""

from __future__ import annotations

import gzip
import pathlib
import re
import sys

BUDGET = 90_000
ROOT = pathlib.Path(__file__).resolve().parents[1]
JS = ROOT / "mailosh/web/static/js"
VENDOR = ROOT / "mailosh/web/static/vendor"
LAYOUT = ROOT / "mailosh/web/templates/layouts/app.html"


def gz(data: bytes) -> int:
    return len(gzip.compress(data, 9))


def shell_modules(layout: str) -> set[str]:
    """Our modules the app shell loads: every `<script>`-tagged one, plus
    everything they import, transitively."""
    seen: set[str] = set()
    stack = list(
        re.findall(r"<script src=\"\{\{ static\('js/([A-Za-z0-9_.-]+\.js)'\) \}\}\"", layout)
    )
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        path = JS / name
        if path.exists():
            stack += re.findall(r'from "\./([A-Za-z0-9_.-]+\.js)"', path.read_text())
    return seen


def main() -> int:
    try:
        import rjsmin
    except ImportError:
        print("js-budget: needs rjsmin (pip install rjsmin)", file=sys.stderr)
        return 1

    layout = LAYOUT.read_text()
    ours = shell_modules(layout)
    # Only real `<script src=...>` tags. Matching `vendor/...` anywhere in
    # the file counted the prose explaining why Squire is *not* tagged, and
    # the two `data-compose-*` attributes carrying its URL for the loader --
    # which reported the deferred bytes as if they were still shipped.
    vendor = sorted(
        set(
            re.findall(
                r"<script src=\"\{\{ static\('vendor/([A-Za-z0-9_.-]+\.js)'\) \}\}\"", layout
            )
        )
    )

    src_gz = min_gz = 0
    for name in sorted(ours):
        path = JS / name
        if not path.exists():
            continue
        source = path.read_text()
        src_gz += gz(source.encode())
        min_gz += gz(rjsmin.jsmin(source).encode())

    vendor_gz = sum(gz((VENDOR / name).read_bytes()) for name in vendor if (VENDOR / name).exists())

    print(f"  ours, as written   {src_gz:>7} B gz  ({len(ours)} modules on the shell)")
    print(f"  ours, minified     {min_gz:>7} B gz  <- what the image serves")
    print(
        f"  vendor             {vendor_gz:>7} B gz  ({len(vendor)} files, SHA-pinned, not minified)"
    )
    total = min_gz + vendor_gz
    verdict = "OK" if total <= BUDGET else f"OVER by {total - BUDGET:,}"
    print(f"  shell total        {total:>7} B gz  against {BUDGET:,}  {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
