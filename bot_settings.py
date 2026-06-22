from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BotSettings:
    lot: float
    pip: float
    tp1_pips: int
    tp2_pips: int
    sl_buffer: float
    emergency_sl_pips: int
    monitor_interval: float
    telegram_test_mode: bool
    source_chat_id: int


_DEFAULT_FILENAME = "bot_config.json"


def _require_key(obj: dict[str, Any], key: str) -> Any:
    if key not in obj:
        raise ValueError(f"Missing required config key: {key}")
    return obj[key]


def _as_number(value: Any, *, key: str) -> float:
    try:
        # Reject bool (bool is a subclass of int)
        if isinstance(value, bool):
            raise TypeError
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"Config key '{key}' must be numeric; got: {value!r}")


def _as_int(value: Any, *, key: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"Config key '{key}' must be an integer; got bool")
    try:
        # Allow JSON numbers that are whole-number floats
        if isinstance(value, float) and not value.is_integer():
            raise ValueError
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"Config key '{key}' must be an integer; got: {value!r}")


def _as_bool(value: Any, *, key: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"Config key '{key}' must be boolean; got: {value!r}")
    return value


def load_settings(path: str | Path | None = None) -> BotSettings:
    base_dir = Path(__file__).resolve().parent
    config_path = Path(path) if path is not None else (base_dir / _DEFAULT_FILENAME)

    if not config_path.exists():
        raise FileNotFoundError(f"bot_config.json not found at: {config_path}")

    try:
        raw = config_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in config file {config_path}: {e}") from e

    if not isinstance(data, dict):
        raise ValueError(f"Config root must be a JSON object; got: {type(data).__name__}")

    # Strict parsing: do not silently invent defaults.
    lot = _as_number(_require_key(data, "lot"), key="lot")
    pip = _as_number(_require_key(data, "pip"), key="pip")
    tp1_pips = _as_int(_require_key(data, "tp1_pips"), key="tp1_pips")
    tp2_pips = _as_int(_require_key(data, "tp2_pips"), key="tp2_pips")
    sl_buffer = _as_number(_require_key(data, "sl_buffer"), key="sl_buffer")
    emergency_sl_pips = _as_int(_require_key(data, "emergency_sl_pips"), key="emergency_sl_pips")
    monitor_interval = _as_number(_require_key(data, "monitor_interval"), key="monitor_interval")
    telegram_test_mode = _as_bool(
        _require_key(data, "telegram_test_mode"), key="telegram_test_mode"
    )
    source_chat_id = _as_int(_require_key(data, "source_chat_id"), key="source_chat_id")

    # Validation rules
    if not (lot > 0):
        raise ValueError(f"Config key 'lot' must be > 0; got: {lot}")
    if not (pip > 0):
        raise ValueError(f"Config key 'pip' must be > 0; got: {pip}")
    if not (tp1_pips > 0):
        raise ValueError(f"Config key 'tp1_pips' must be > 0; got: {tp1_pips}")
    if not (tp2_pips > 0):
        raise ValueError(f"Config key 'tp2_pips' must be > 0; got: {tp2_pips}")
    if tp2_pips < tp1_pips:
        raise ValueError(
            f"Config keys must satisfy tp2_pips >= tp1_pips; got tp1_pips={tp1_pips}, tp2_pips={tp2_pips}"
        )
    if sl_buffer < 0:
        raise ValueError(f"Config key 'sl_buffer' must be >= 0; got: {sl_buffer}")
    if emergency_sl_pips <= 0:
        raise ValueError(f"Config key 'emergency_sl_pips' must be > 0; got: {emergency_sl_pips}")
    if monitor_interval < 1:
        raise ValueError(f"Config key 'monitor_interval' must be >= 1; got: {monitor_interval}")

    # telegram_test_mode + source_chat_id already validated by type conversions above.
    return BotSettings(
        lot=lot,
        pip=pip,
        tp1_pips=tp1_pips,
        tp2_pips=tp2_pips,
        sl_buffer=sl_buffer,
        emergency_sl_pips=emergency_sl_pips,
        monitor_interval=monitor_interval,
        telegram_test_mode=telegram_test_mode,
        source_chat_id=source_chat_id,
    )


def settings_to_dict(settings: BotSettings) -> dict[str, Any]:
    return {
        "lot": settings.lot,
        "pip": settings.pip,
        "tp1_pips": settings.tp1_pips,
        "tp2_pips": settings.tp2_pips,
        "sl_buffer": settings.sl_buffer,
        "emergency_sl_pips": settings.emergency_sl_pips,
        "monitor_interval": settings.monitor_interval,
        "telegram_test_mode": settings.telegram_test_mode,
        "source_chat_id": settings.source_chat_id,
    }


def _main() -> None:
    settings = load_settings()
    print(settings_to_dict(settings))


if __name__ == "__main__":
    _main()

