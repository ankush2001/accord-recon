# syntax=docker/dockerfile:1

FROM python:3.12-slim AS runtime

# uv resolves and installs an order of magnitude faster than pip, which matters
# most on a free-tier builder where the whole image is rebuilt on every deploy.
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies install in their own layer, keyed on the project metadata alone.
# Editing application code -- which is nearly every build -- reuses it.
COPY pyproject.toml README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv && uv pip install --no-cache .

COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./
RUN --mount=type=cache,target=/root/.cache/uv uv pip install --no-cache --no-deps -e .

# Never root.
RUN useradd --create-home --uid 10001 accord && chown -R accord:accord /app
USER accord

EXPOSE 8000

# Migrations run at startup rather than as a separate deploy step. On a
# single-instance free tier that is simply correct; with several replicas it
# would need to move to a release command, because concurrent `alembic upgrade`
# processes race for the version table.
COPY --chown=accord:accord docker-entrypoint.sh /app/
ENTRYPOINT ["/app/docker-entrypoint.sh"]
