#!/bin/sh
# Entrypoint for the `mailosh` compose service (docker-compose.yml's
# `command: ["/app/docker/entrypoint.sh"]`, overriding mailosh.Dockerfile's
# own CMD): apply any pending Alembic migrations against
# MAILOSH_DATABASE_URL, then hand off to uvicorn.
#
# `set -eu`: abort on the first failing command (a migration error must
# stop the container, not fall through into serving traffic against a
# stale/half-migrated schema) and treat an unset variable as an error.
#
# `exec` for the final command: replaces this shell process with uvicorn
# (PID 1 becomes uvicorn itself) so it receives SIGTERM directly from
# `docker compose stop`/`down` for a clean shutdown, rather than a signal
# sent to this wrapper script that uvicorn underneath never sees.
#
# MAILOSH_RELOAD=1 adds uvicorn's `--reload` and is set only by
# docker-compose.dev.yml, which also bind-mounts the working tree over
# /app. Without that mount the flag would be pointless: the default stack
# bakes source into the image (mailosh.Dockerfile's `COPY . /app`), so
# there is nothing on disk for the reloader to notice changing. Watching
# is scoped to /app/mailosh so that editing a test, a migration or a
# vendored asset does not bounce the server.
#
# The --reload-include lines below are not optional polish. uvicorn's
# reloader watches *.py and nothing else, so without them a template,
# stylesheet or script edit did not restart the app -- and because
# `static()` computes its cache-busting hashes once per Jinja Environment
# at app start, the browser then kept serving stale bytes under an
# unchanged `?v=`. The symptom was an edit that appeared to do nothing at
# all, which cost real debugging time before it was diagnosed. Restarting
# the app is what recomputes those hashes, so watching these three
# extensions is what makes the dev loop honest for frontend work.
set -eu

alembic upgrade head

if [ "${MAILOSH_RELOAD:-0}" = "1" ]; then
	exec uvicorn mailosh.web.app:create_app --factory --host 0.0.0.0 --port 8000 \
		--reload --reload-dir /app/mailosh \
		--reload-include '*.html' \
		--reload-include '*.css' \
		--reload-include '*.js'
fi

exec uvicorn mailosh.web.app:create_app --factory --host 0.0.0.0 --port 8000
