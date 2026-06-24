from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


CONFIG_FILENAME = "bot_config.json"
MAX_LAYER_LOT = 0.01
REQUIRED_LAYER_COUNT = 3


def _project_root() -> Path:
    # This script lives in project root.
    return Path(__file__).resolve().parent


def _read_json(path: Path) -> Any:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise
    except OSError as e:
        raise ValueError(f"Failed reading config: {e}") from e

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON: {e}") from e


def _safe_bool_repr(v: Any) -> str:
    if v is True:
        return "True"
    if v is False:
        return "False"
    if v is None:
        return "None"
    return str(v)


def _truthy_is_true(v: Any) -> bool:
    # Safety rule: allow_real_order must NOT be true.
    return v is True


def _to_optional_bool(v: Any) -> Any:
    # Preserve None/boolean; for anything else return as-is.
    return v if v is None or isinstance(v, bool) else v


def _get_layers(layer_data: Any) -> list[dict[str, Any]] | None:
    if not isinstance(layer_data, list):
        return None
    out: list[dict[str, Any]] = []
    for i, item in enumerate(layer_data):
        if not isinstance(item, dict):
            return None
        out.append(item)
    return out


def main() -> int:
    config_path = _project_root() / CONFIG_FILENAME

    print("CONFIG SAFETY CHECK")

    try:
        if not config_path.exists():
            raise FileNotFoundError(str(config_path))

        data = _read_json(config_path)
        if not isinstance(data, dict):
            raise ValueError("Config root must be a JSON object")

        telegram_test_mode = data.get("telegram_test_mode")
        allow_real_order = data.get("allow_real_order", None)
        layers_raw = data.get("layers", None)

        layers_list = _get_layers(layers_raw)

        layers_count = len(layers_list) if layers_list is not None else (len(layers_raw) if isinstance(layers_raw, list) else "missing/invalid")

        print(f"telegram_test_mode: {_safe_bool_repr(telegram_test_mode)}")
        print(f"allow_real_order: {_safe_bool_repr(_to_optional_bool(allow_real_order))}")
        print(f"layers: {layers_count if isinstance(layers_count, int) else layers_count}")
        print()

        # Print layer rows (best-effort) only when layers_list is a list.
        if layers_list is not None:
            for idx, layer in enumerate(layers_list[:REQUIRED_LAYER_COUNT], start=1):
                enabled = layer.get("enabled")
                lot = layer.get("lot")
                tp_enabled = layer.get("tp_enabled")
                tp_pips = layer.get("tp_pips")
                be_enabled = layer.get("be_enabled")
                comment = layer.get("comment")

                def _fmt(x: Any) -> str:
                    return str(x)

                print(
                    f"Layer {idx}: "
                    f"enabled={_fmt(enabled)} lot={_fmt(lot)} "
                    f"tp={_fmt(tp_enabled)} tp_pips={_fmt(tp_pips)} "
                    f"be={_fmt(be_enabled)} comment={_fmt(comment)}"
                )
        else:
            # Missing/invalid layers, do not attempt per-layer print.
            pass

        # Verdict computation (strict, per requested rules)
        is_test_mode = telegram_test_mode is True
        allow_real_order_not_true = allow_real_order is None or allow_real_order is False or allow_real_order_not_true_is_missing(allow_real_order)

        # helper for "missing": in our extraction we used .get(..., None). So missing key becomes None.
        # But the requirement says "missing, false, or None" => treat missing and None the same.
        # So above rule reduces to allow_real_order is not True.
        allow_real_order_not_true = not _truthy_is_true(allow_real_order)

        is_layers_exact_3 = layers_list is not None and len(layers_list) == REQUIRED_LAYER_COUNT

        max_lot_ok = True
        if layers_list is None:
            max_lot_ok = False
        else:
            for layer in layers_list:
                # If lot missing or not numeric, this fails WARNING (i.e., not SAFE).
                lot_val = layer.get("lot")
                try:
                    if isinstance(lot_val, bool):
                        raise TypeError("bool is not numeric")
                    lot_num = float(lot_val)
                except Exception:
                    max_lot_ok = False
                    break
                if lot_num > MAX_LAYER_LOT:
                    max_lot_ok = False
                    break

        is_safe = is_test_mode and allow_real_order_not_true and is_layers_exact_3 and max_lot_ok

        if is_safe:
            # Provide a succinct verdict line matching the spirit of the example.
            max_lot_str = "<= 0.01"
            verdict_detail = "TEST MODE active, real order locked, max lot <= 0.01"
            print(f"VERDICT: SAFE - {verdict_detail}")
            return 0

        print(
            "VERDICT: WARNING - "
            f"(SAFE requires telegram_test_mode=True, allow_real_order not True/missing/false/None, exactly {REQUIRED_LAYER_COUNT} layers, and all layer lots <= {MAX_LAYER_LOT})"
        )
        return 2

    except FileNotFoundError:
        print(f"VERDICT: WARNING - bot_config.json missing at: {config_path}")
        return 1
    except Exception as e:
        print(f"VERDICT: WARNING - bot_config.json invalid/unreadable: {e}")
        return 1


# NOTE: kept for readability; missing-key handling is unified into allow_real_order is None.
def allow_real_order_not_true_is_missing(allow_real_order: Any) -> bool:
    return allow_real_order is None


if __name__ == "__main__":
    raise SystemExit(main())

