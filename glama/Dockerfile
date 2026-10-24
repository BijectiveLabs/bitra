# The MCP server as Glama builds it: the package from this checkout with the [mcp] extra only,
# served over stdio by a non-root user. The tools read under /work and never trade.
#
# Glama does not read this file; it builds from the spec on the server's admin page
# (glama.ai/mcp/servers/BijectiveLabs/trading-research-agents/admin/dockerfile). That spec is:
#   Python version  3.12
#   Build steps     ["uv sync --locked --no-dev --extra mcp"]
#   CMD arguments   ["mcp-proxy", "--", "uv", "run", "--no-sync", "trading-research-agents",
#                    "mcp", "serve"]
# CMD is one array element per argument. The MCP SDK lives in the [mcp] extra, so a plain
# `uv sync` leaves it out and the server exits without listing a tool. Python 3.15 has no wheels
# yet for pydantic-core, which the SDK needs, so a build on it has to compile that from Rust
# source. This file is the same build, to check locally:
#   docker build -f glama/Dockerfile -t tra-mcp . && docker run -i --rm tra-mcp

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
CMD ["tra", "mcp", "serve"]
