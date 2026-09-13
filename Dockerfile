# The gates in a container: one non-root user, the package installed from this checkout,
# runs and data on a volume, keys from the environment only.

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /build
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY src ./src
RUN uv sync --locked --no-dev --all-extras --no-editable

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
RUN useradd --create-home --uid 1000 tra
COPY --from=builder --chown=tra:tra /build/.venv /app/.venv
USER tra
WORKDIR /work
VOLUME ["/work"]
ENTRYPOINT ["tra"]
CMD ["--help"]
