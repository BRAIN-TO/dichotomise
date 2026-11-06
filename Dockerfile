# syntax=docker/dockerfile:1
FROM python:3.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY . .
RUN uv sync --locked --no-dev

FROM python:3.13-slim

# pigz writes the archives with every core; without it the run falls back to single-threaded gzip.
RUN apt-get update \
    && apt-get install -y --no-install-recommends pigz \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app /app
ENV PATH="/app/.venv/bin:$PATH"

WORKDIR /data
ENTRYPOINT ["dichotomise"]
