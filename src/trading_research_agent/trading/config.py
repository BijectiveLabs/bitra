"""`strategy.toml`: everything a run needs, declared once, none of it in the logic."""

from __future__ import annotations

import importlib
import importlib.util
import sys
import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_research_agent.strategy.core import Strategy
from trading_research_agent.strategy.exits import ExitRule
from trading_research_agent.trading.controls import Limits


class ConfigError(ValueError):
    """The run configuration is incomplete or contradicts itself."""


@dataclass(frozen=True)
class RunSettings:
    instrument: str
    venue_key: str
    bar_interval_s: int
    size: Decimal
    spread_cost_bps: Decimal
    equity: Decimal | None
    latency_ms: Decimal
    slippage_bps: Decimal
    settlement_interval_s: int


@dataclass(frozen=True)
class StrategyConfig:
    strategy: Strategy
    exit_rule: ExitRule
    limits: Limits
    kill_switch: Path | None
    run: RunSettings
    registration_seal: str | None


def load_strategy_config(path: Path) -> StrategyConfig:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: not valid TOML: {exc}") from exc
    return parse_strategy_config(raw, base=path.resolve().parent)


def parse_strategy_config(raw: dict[str, Any], *, base: Path | None = None) -> StrategyConfig:
    strategy_raw = _table(raw, "strategy")
    exit_raw = _table(raw, "exit")
    run_raw = _table(raw, "run")
    controls_raw = _table(raw, "controls")

    strategy = _build_strategy(strategy_raw, base)
    exit_rule = ExitRule(
        stop_sigma_frac=_dec(exit_raw, "stop_sigma_frac"),
        timeout_bars=_int(exit_raw, "timeout_bars"),
        mechanism_bound=str(exit_raw.get("mechanism_bound", "")),
        take_profit_sigma_frac=(
            _dec(exit_raw, "take_profit_sigma_frac")
            if "take_profit_sigma_frac" in exit_raw
            else None
        ),
        slip_bps=_dec(exit_raw, "slip_bps") if "slip_bps" in exit_raw else Decimal("5.0"),
    )
    limits = Limits(
        max_risk_per_trade_bps=_dec(controls_raw, "max_risk_per_trade_bps"),
        max_position=_dec(controls_raw, "max_position"),
        max_orders_per_minute=_int(controls_raw, "max_orders_per_minute"),
        daily_loss_limit_bps=_dec(controls_raw, "daily_loss_limit_bps"),
    )
    kill = controls_raw.get("kill_switch")
    run = RunSettings(
        instrument=str(run_raw.get("instrument", "")),
        venue_key=str(run_raw.get("venue_key", "")),
        bar_interval_s=_int(run_raw, "bar_interval_s"),
        size=_dec(run_raw, "size"),
        spread_cost_bps=_dec(run_raw, "spread_cost_bps"),
        equity=_dec(run_raw, "equity") if "equity" in run_raw else None,
        latency_ms=_dec(run_raw, "latency_ms"),
        slippage_bps=_dec(run_raw, "slippage_bps"),
        settlement_interval_s=_int(run_raw, "settlement_interval_s"),
    )
    if not run.instrument or not run.venue_key:
        raise ConfigError("[run] names the instrument and the venue_key (venue/channel)")
    seal = strategy_raw.get("registration_seal")
    if seal is not None and (not isinstance(seal, str) or len(seal) != 64):
        raise ConfigError("[strategy].registration_seal is the 64-character pre-registration seal")
    return StrategyConfig(
        strategy=strategy,
        exit_rule=exit_rule,
        limits=limits,
        kill_switch=Path(str(kill)) if kill else None,
        run=run,
        registration_seal=seal,
    )


def _build_strategy(raw: dict[str, Any], base: Path | None) -> Strategy:
    """A decision core named by `import = "pkg.mod:Class"` or `file = "x.py"` + `class`.

    The package ships no strategy of its own. `examples/strategies/` holds two that a run can
    point at by file; they are examples, not recommendations.
    """
    params = raw.get("params", {})
    if not isinstance(params, dict):
        raise ConfigError("[strategy.params] must be a table")
    target = raw.get("import")
    file = raw.get("file")
    if isinstance(target, str) and target:
        if ":" not in target:
            raise ConfigError("[strategy].import is 'package.module:Class'")
        module_name, _, class_name = target.partition(":")
        try:
            cls = getattr(importlib.import_module(module_name), class_name)
        except (ImportError, AttributeError) as exc:
            raise ConfigError(f"cannot import strategy {target!r}: {exc}") from exc
        label = target
    elif isinstance(file, str) and file:
        class_name = str(raw.get("class", ""))
        if not class_name:
            raise ConfigError("[strategy].file needs [strategy].class naming the core")
        cls = _class_from_file(_resolve(Path(file), base), class_name)
        label = f"{file}:{class_name}"
    else:
        raise ConfigError(
            "[strategy] names the core: import = 'package.module:Class', or "
            "file = 'path/to/core.py' with class = 'Class'"
        )
    try:
        instance = cls(**params)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"strategy {label!r}: {exc}") from exc
    if not hasattr(instance, "on_bar") or not hasattr(instance, "name"):
        raise ConfigError(f"{label!r} is not a Strategy: it needs `name` and `on_bar`")
    return instance  # type: ignore[no-any-return]


def _resolve(file: Path, base: Path | None) -> Path:
    # Relative to the config file first, so a run directory carries its own core.
    if file.is_absolute() or base is None:
        return file
    beside = base / file
    return beside if beside.is_file() else file


def _class_from_file(path: Path, class_name: str) -> Any:
    if not path.is_file():
        raise ConfigError(f"strategy file {str(path)!r} does not exist")
    spec = importlib.util.spec_from_file_location(f"strategy_file_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ConfigError(f"cannot load strategy file {str(path)!r}")
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: a dataclass in the file looks itself up in sys.modules.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(spec.name, None)
        raise ConfigError(f"strategy file {str(path)!r} failed to load: {exc}") from exc
    if not hasattr(module, class_name):
        raise ConfigError(f"{str(path)!r} has no class {class_name!r}")
    return getattr(module, class_name)


def _table(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] table is required")
    return value


def _dec(table: dict[str, Any], key: str) -> Decimal:
    if key not in table:
        raise ConfigError(f"missing {key}")
    value = table[key]
    if isinstance(value, bool):
        raise ConfigError(f"{key} must be a number")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | str):
        try:
            return Decimal(str(value))
        except ArithmeticError as exc:
            raise ConfigError(f"{key} is not a number: {value!r}") from exc
    raise ConfigError(f"{key} must be a number")


def _int(table: dict[str, Any], key: str) -> int:
    if key not in table:
        raise ConfigError(f"missing {key}")
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} must be an integer")
    return value
