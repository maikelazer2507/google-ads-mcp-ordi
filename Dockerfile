# syntax=docker/dockerfile:1.7

# The defaults are immutable version tags for local development. Production
# builds MUST override both arguments with publisher-verified digest references;
# deploy/build-release.sh enforces that policy.
ARG PYTHON_IMAGE=python:3.11.13-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.33

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

WORKDIR /app

COPY --from=uv /uv /uvx /usr/local/bin/
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY ads_mcp ./ads_mcp

# Seed the locked build backend, then build without PEP 517 network isolation.
# The final sync drops the build-only extra from the runtime environment.
RUN uv sync --frozen --no-install-project --no-dev \
        --extra firestore --extra build \
    && uv sync --frozen --no-dev --extra firestore --no-editable \
        --no-build-isolation

FROM ${PYTHON_IMAGE} AS runtime

ENV HOME=/home/app \
    PATH=/app/.venv/bin:$PATH \
    FASTMCP_CHECK_FOR_UPDATES=off \
    FASTMCP_SHOW_SERVER_BANNER=false \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --system --gid 65532 app \
    && useradd --system --uid 65532 --gid 65532 \
        --create-home --home-dir /home/app app

WORKDIR /app

COPY --from=builder --chown=65532:65532 /app/.venv /app/.venv

USER 65532:65532

EXPOSE 8080
STOPSIGNAL SIGTERM

CMD ["google-ads-mcp"]
