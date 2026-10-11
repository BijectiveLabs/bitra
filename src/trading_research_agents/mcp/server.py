"""The read surface registered as MCP tools. Nothing here can place an order or move a tier.

Built against the MCP Python SDK v2 (`mcp>=2,<3`, read 2026-09-13): `MCPServer`, `@tool` with
`ToolAnnotations`, `ToolError` for a message the model should read, and a return type
annotation that becomes the tool's output schema. `serve()` blocks on stdio, which is what
`mcp dev`, `mcp run` and every desktop host expect from a local server.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

from trading_research_agents.surface import READ_TOOLS
from trading_research_agents.surface.confine import confined, roots_of

P = ParamSpec("P")
R = TypeVar("R")
SERVER_NAME = "trading-research-agents"
INSTRUCTIONS = (
    "These tools read the gates of a trading research record: the cost floor, a sealed "
    "pre-registration, a dataset audit, a layer-5 validation, the kill ledger, the promotion "
    "ladder and a run report. They measure; they never trade, promote or record. Every number "
    "in basis points is a Decimal string; every time is UTC."
)


def _sdk() -> tuple[Any, Any, Any]:
    try:
        from mcp.server import MCPServer
        from mcp.server.mcpserver.exceptions import ToolError
        from mcp.types import ToolAnnotations
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise ImportError(
            "the MCP server needs the `mcp` extra: pip install 'trading-research-agents[mcp]'"
        ) from exc
    return MCPServer, ToolAnnotations, ToolError


def build_server(roots: Sequence[Path] | None = None) -> Any:
    """The server with its tools registered. Kept separate from `serve()` so tests can call it.

    A host chooses every path a tool opens, so each path has to resolve under `roots` - the
    working directory unless the operator names others - and never into a hidden file.
    """
    server_cls, annotations_cls, tool_error = _sdk()
    allowed = roots_of(roots or (Path.cwd(),))
    mcp = server_cls(SERVER_NAME, instructions=INSTRUCTIONS)
    reads = annotations_cls(read_only_hint=True, open_world_hint=False)

    def guarded(fn: Callable[P, R]) -> Callable[P, R]:
        # Every refusal the gates raise becomes a message the model reads, never a crash.
        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                return fn(*args, **kwargs)
            except (ValueError, KeyError, OSError, RuntimeError) as exc:
                raise tool_error(str(exc)) from None

        return wrapper

    for name, title, fn in READ_TOOLS:
        mcp.tool(name=name, title=title, annotations=reads)(guarded(confined(fn, allowed)))
    return mcp


def serve(roots: Sequence[Path] | None = None) -> None:
    """Block on stdio. Transport options belong to run(), never to the constructor."""
    build_server(roots).run()
