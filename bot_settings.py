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


# Runtime optional layers loader (Phase 3C)
# - Must not alter legacy load_settings() behavior
# - Must not wire layers into execution order yet

def _as_non_empty_str(value: Any, *, key: str, max_len: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Config key '{key}' must be a string; got: {type(value).__name__}")
    s = value.strip()
    if not s:
        raise ValueError(f"Config key '{key}' must be a non-empty string")
    if len(s) > max_len:
        raise ValueError(f"Config key '{key}' must be at most {max_len} characters")
    return s


def _as_str_bool(value: Any, *, key: str) -> bool:
    # Strict bool only; do not accept 0/1 strings etc here.
    return _as_bool(value, key=key)


def _as_positive_number(value: Any, *, key: str) -> float:
    num = _as_number(value, key=key)
    if not (num > 0):
        raise ValueError(f"Config key '{key}' must be > 0; got: {num}")
    return num


def _as_int_ge(value: Any, *, key: str, min_value: int) -> int:
    i = _as_int(value, key=key)
    if i < min_value:
        raise ValueError(f"Config key '{key}' must be >= {min_value}; got: {i}")
    return i


def load_runtime_layers(path: str | Path | None = None) -> list[dict[str, Any]] | None:
    """Load and validate optional top-level runtime `layers` from bot_config.json.

    Returns:
        - None: if top-level `layers` key is missing (backward compatible)
        - []: if `layers` exists but is an empty list
        - list[dict]: normalized validated layers

    Notes:
        - This helper intentionally does NOT affect legacy load_settings().
        - Runtime order execution is not wired to layers yet.
    """

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

    if "layers" not in data:
        return None

    raw_layers = data["layers"]
    if not isinstance(raw_layers, list):
        # Must be "same style" of config error (ValueError with message, like load_settings)
        raise ValueError(f"Config key 'layers' must be a list; got: {type(raw_layers).__name__}")

    if len(raw_layers) == 0:
        return []

    # Validate at most 10 layers
    if len(raw_layers) > 10:
        raise ValueError(f"Config key 'layers' must contain at most 10 items; got: {len(raw_layers)}")

    validated: list[dict[str, Any]] = []
    for idx, item in enumerate(raw_layers):
        if not isinstance(item, dict):
            raise ValueError(f"layers[{idx}] must be an object; got: {type(item).__name__}")

        # required fields
        enabled = _as_str_bool(_require_key(item, "enabled"), key="layers[].enabled")
        name = _as_non_empty_str(_require_key(item, "name"), key="layers[].name", max_len=40)
        lot = _as_positive_number(_require_key(item, "lot"), key="layers[].lot")
        tp_enabled = _as_str_bool(_require_key(item, "tp_enabled"), key="layers[].tp_enabled")
        tp_pips = _as_int_ge(_require_key(item, "tp_pips"), key="layers[].tp_pips", min_value=0)
        if tp_enabled and tp_pips <= 0:
            raise ValueError(
                "layers[].tp_pips must be > 0 when tp_enabled is true"
            )
        be_enabled = _as_str_bool(_require_key(item, "be_enabled"), key="layers[].be_enabled")
        be_trigger_pips = _as_int_ge(
            _require_key(item, "be_trigger_pips"), key="layers[].be_trigger_pips", min_value=0
        )
        be_offset_pips = _as_int_ge(
            _require_key(item, "be_offset_pips"), key="layers[].be_offset_pips", min_value=0
        )
        comment = _as_non_empty_str(_require_key(item, "comment"), key="layers[].comment", max_len=40)

        validated.append(
            {
                "name": name,
                "enabled": enabled,
                "lot": lot,
                "tp_enabled": tp_enabled,
                "tp_pips": tp_pips,
                "be_enabled": be_enabled,
                "be_trigger_pips": be_trigger_pips,
                "be_offset_pips": be_offset_pips,
                "comment": comment,
            }
        )

    return validated






def _main() -> None:
    settings = load_settings()
    print(settings_to_dict(settings))


if __name__ == "__main__":
    _main()

