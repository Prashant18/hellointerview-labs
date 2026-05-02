# syntax=docker/dockerfile:1.7
#
# Gateway image — uses the official uv base image so we can `uv sync --frozen`
# from the committed lockfile and get bit-identical environments locally and
# in CI. The local IDE points at gateway/.venv (created by `uv sync` on the
# host); this Dockerfile builds its own venv inside the container at /srv/.venv.

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /srv

# Install dependencies first (layer-cached) before copying the app code.
COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY app ./app

ENV PATH="/srv/.venv/bin:${PATH}"

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--log-level", "warning"]
