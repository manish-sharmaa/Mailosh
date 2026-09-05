FROM python:3.12-slim

WORKDIR /app
COPY . /app

# `.[build]` rather than `.`: it adds rjsmin, which the next line needs and
# nothing else does. Minifying happens here, against the copy inside the
# image, so the source tree on the host and in git keeps its comments --
# they are a large part of how this codebase explains itself, and they have
# no business on the wire. Measured at the time of writing: 92,013 -> 30,588
# bytes gzipped across this app's own modules.
#
# One layer, so a failed minify fails the build rather than leaving an image
# with a half-processed static directory. `vendor/` is untouched: those files
# are SHA-pinned against the URLs they came from.
RUN pip install --no-cache-dir -e ".[build]" \
 && python scripts/minify-js.py

CMD ["uvicorn", "mailosh.web.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
