# The MCP server for Glama's build: the package from this checkout with the [mcp] extra only,
# served over stdio by a non-root user. The tools read under /work and never trade.

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0 UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /build
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY src ./src
RUN uv sync --locked --no-dev --extra mcp --no-editable

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
RUN useradd --create-home --uid 1000 tra
COPY --from=builder --chown=tra:tra /app/.venv /app/.venv
USER tra
WORKDIR /work
CMD ["trading-research-agents", "mcp", "serve"]
