"""Minify this app's own JavaScript, in place, for a production image.

Run during the Docker build, never in development. The source in git keeps
its comments -- they are a large part of how this codebase explains itself,
and several of them are the only record of why a line is the way it is --
while the bytes a browser downloads do not carry them. Measured: comments
and formatting are about two thirds of our gzipped JavaScript.

`vendor/` is deliberately untouched. Those files are pinned by SHA against
the URL they were fetched from (see the Makefile), and rewriting them would
break that pin and the NOTICE that depends on it. Squire ships unminified
and is the largest of them; it is handled by loading it only when someone
actually composes, not by rewriting it here.

rjsmin removes comments and redundant whitespace. It does not rename
anything, does not reorder, and does not parse the program -- which is
exactly why it is safe on code that is loaded as ES modules, uses regular
expressions, and is scanned by tests that read the source rather than run
it. Renaming minifiers buy perhaps another 20% and can change behaviour
around `Function.prototype.name`, `arguments`, and getters; that trade is
not worth making for a self-hosted app.
"""

from __future__ import annotations

import gzip
import pathlib
import sys

import rjsmin

JS_DIR = pathlib.Path(__file__).resolve().parents[1] / "mailosh/web/static/js"


def main() -> int:
    files = sorted(JS_DIR.glob("*.js"))
    if not files:
        print(f"minify-js: no .js under {JS_DIR}", file=sys.stderr)
        return 1

    before_gz = after_gz = 0
    for path in files:
        source = path.read_text(encoding="utf-8")
        minified = rjsmin.jsmin(source)
        if not minified.strip():
            # A file that minifies to nothing means rjsmin misread it. Fail
            # the build rather than ship an empty module that would break
            # the page only once someone loaded it.
            print(f"minify-js: {path.name} minified to nothing", file=sys.stderr)
            return 1
        before = len(gzip.compress(source.encode(), 9))
        after = len(gzip.compress(minified.encode(), 9))
        before_gz += before
        after_gz += after
        path.write_text(minified, encoding="utf-8")
        print(f"  {path.name:16} {before:7} -> {after:7} B gz")

    saved = before_gz - after_gz
    print(f"  {'TOTAL':16} {before_gz:7} -> {after_gz:7} B gz  ({saved:,} B saved)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
