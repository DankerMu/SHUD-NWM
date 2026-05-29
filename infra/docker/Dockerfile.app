# syntax=docker/dockerfile:1.7

FROM node:22-bookworm-slim AS frontend
WORKDIR /src/apps/frontend

COPY apps/frontend/package.json apps/frontend/pnpm-lock.yaml ./
RUN corepack enable \
    && corepack prepare pnpm@10.11.0 --activate \
    && pnpm install --frozen-lockfile

COPY apps/frontend/ ./
RUN pnpm build

FROM python:3.12-slim-bookworm AS app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_SYNC=1 \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /uvx /usr/local/bin/

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock README.md ./
RUN UV_NO_SYNC=0 uv sync --frozen --no-dev --no-install-project

COPY apps apps
COPY packages packages
COPY services services
COPY workers workers
COPY db db
COPY config config
COPY schemas schemas
COPY openapi openapi
COPY infra/docker infra/docker
COPY infra/sbatch infra/sbatch
COPY --from=frontend /src/apps/frontend/dist apps/frontend/dist

RUN UV_NO_SYNC=0 uv sync --frozen --no-dev \
    && chmod 0755 infra/docker/entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["infra/docker/entrypoint.sh"]
CMD ["uv", "run", "python", "-m", "uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
