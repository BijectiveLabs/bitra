"""Which files a tool may open when a model, or a host, chose the path.

The read tools take paths, and on the surface those paths are written by whoever is calling: an
auditor in a tool loop, or an MCP host. A reader that opens whatever it is handed is a way for a
model to send the contents of any file on the machine to its vendor. So a path argument has to
resolve inside a declared root, and nothing hidden - `.env`, `.tra-history`, a `.git` directory -
is readable at all, wherever it sits.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])
PATH_ARGUMENTS = ("_path", "_dir")


class OutsideRootError(ValueError):
    """A tool was asked to open a file it has no business opening."""


def roots_of(paths: Iterable[Path | str]) -> tuple[Path, ...]:
    return tuple(dict.fromkeys(Path(p).expanduser().resolve() for p in paths))


def inside(value: str, roots: tuple[Path, ...], argument: str = "path") -> Path:
    """The resolved path, refused when it escapes every root or names something hidden."""
    path = Path(value).expanduser().resolve()
    for root in roots:
        if path.is_relative_to(root):
            hidden = [part for part in path.relative_to(root).parts if part.startswith(".")]
            if hidden:
                raise OutsideRootError(f"{argument} names a hidden file or directory ({hidden[0]})")
            return path
    raise OutsideRootError(
        f"{argument} {value!r} is outside what this tool may read: {[str(r) for r in roots]}"
    )


def confined(fn: F, roots: tuple[Path, ...]) -> F:
    """The same tool, refusing any path argument that does not resolve inside `roots`."""
    signature = inspect.signature(fn)

    @functools.wraps(fn)
    def guarded(*args: Any, **kwargs: Any) -> Any:
        bound = signature.bind_partial(*args, **kwargs)
        for name, value in bound.arguments.items():
            if name.endswith(PATH_ARGUMENTS) and value is not None:
                inside(str(value), roots, name)
        return fn(*args, **kwargs)

    return guarded  # type: ignore[return-value]


def confine_all(
    tools: Mapping[str, Callable[..., Any]], roots: tuple[Path, ...]
) -> dict[str, Callable[..., Any]]:
    return {name: confined(fn, roots) for name, fn in tools.items()}
