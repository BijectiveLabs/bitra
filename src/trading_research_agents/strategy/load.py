"""A decision core from a Python file the operator names, outside the package.

The package ships no strategy, so a core is always loaded from the operator's own file: by
`strategy.toml` for a run, and by the arena when a row is named `path/to/file.py:Class`.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


class CoreLoadError(ValueError):
    """The file does not exist, does not load, or does not define the class."""


def load_class(path: Path, class_name: str) -> Any:
    """The class `class_name` defined in the file at `path`."""
    if not path.is_file():
        raise CoreLoadError(f"strategy file {str(path)!r} does not exist")
    spec = importlib.util.spec_from_file_location(f"strategy_file_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise CoreLoadError(f"cannot load strategy file {str(path)!r}")
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: a dataclass in the file looks itself up in sys.modules.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(spec.name, None)
        raise CoreLoadError(f"strategy file {str(path)!r} failed to load: {exc}") from exc
    if not hasattr(module, class_name):
        raise CoreLoadError(f"{str(path)!r} has no class {class_name!r}")
    return getattr(module, class_name)
