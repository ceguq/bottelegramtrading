r"""
MT5 execution module for the XAUUSD Telegram signal bot.

MT5 account settings are loaded from bot_config.json. This module is called by
telegram_listener.py to place pending orders from one parsed signal.

Valetax terminal path (required):
If you have both MetaQuotes MT5 and the Valetax MT5 installed, you must
connect using the Valetax terminal executable explicitly.

To find the correct executable path:
1) Right-click the Valetax MT5 desktop shortcut.
2) Select Properties.
3) Open the Shortcut tab.
4) Copy the value from Target.
5) Use only the path pointing to terminal64.exe.
6) Remove surrounding quotation marks and any extra shortcut arguments
   (keep only something like the examples below).

Examples:
C:\Program Files\Valetax\terminal64.exe
C:\Program Files (x86)\Valetax\terminal64.exe

Actual path depends on where Valetax MT5 was installed.
"""

import logging
import time
from functools import wraps
from pathlib import Path

import MetaTrader5 as mt5

from mt5_lock import mt5_process_lock

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

SYMBOL = "XAUUSD.vxc"  # Broker symbol to trade

# Operational and MT5 account settings loaded from bot_config.json (via bot_settings.py).
try:
    from bot_settings import load_settings

    _settings = load_settings()
    MT5_LOGIN = _settings.mt5_login
    MT5_PASSWORD = _settings.mt5_password
    MT5_SERVER = _settings.mt5_server
    MT5_PATH = _settings.mt5_path
    LOT = _settings.lot  # Lot size for each pending order
    PIP = _settings.pip  # 1 pip = configured for XAUUSD
    TP1_PIPS = _settings.tp1_pips  # Pips for Order 1 take profit
    TP2_PIPS = _settings.tp2_pips  # Pips for Order 2 take profit
    SL_BUFFER = _settings.sl_buffer  # Extra pips added to the raw signal SL
except Exception as _cfg_exc:
    # Fail-fast at import time: wrong trading config should not result in silent defaults.
    raise

MAGIC = 20250611  # Magic number to identify orders from this bot
SLIPPAGE = 20  # Maximum allowed slippage/deviation in points
MONITOR_INTERVAL = 5  # Seconds between each breakeven monitor check
MT5_IPC_RETRIES = 3  # Number of retries when MT5 returns an IPC timeout
MT5_IPC_RETRY_DELAY = 5  # Seconds to wait between IPC timeout retries
SL_UPDATE_COMMENT = "TG-SL-UPDATE"
TP3_COMMENT = "TG-NO-TP"
MAX_RUNTIME_LAYERS = 10


def _with_mt5_lock(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        with mt5_process_lock(timeout=30):
            return func(*args, **kwargs)

    return wrapper


def _default_layer_comment(index: int) -> str:
    defaults = ("TG-TP1", "TG-TP2", TP3_COMMENT)
    if index < len(defaults):
        return defaults[index]
    return f"TG-L{index + 1}"


def _runtime_layer_count(*values, legacy_count=3) -> int:
    lengths = [len(value) for value in values if value is not None]
    layer_count = max(lengths) if lengths else legacy_count
    if layer_count > MAX_RUNTIME_LAYERS:
        raise ValueError(
            f"too_many_enabled_layers: maximum {MAX_RUNTIME_LAYERS}, got {layer_count}"
        )
    return layer_count


def _normalize_layer_override_list(name, values, pad_value, size=None):
    if values is None:
        return None
    if not isinstance(values, list):
        raise ValueError(f"{name} must be a list; got: {values}")
    if len(values) > MAX_RUNTIME_LAYERS:
        raise ValueError(
            f"too_many_enabled_layers: maximum {MAX_RUNTIME_LAYERS}, got {len(values)}"
        )
    normalized = list(values)
    if size is not None and len(normalized) > size:
        raise ValueError(f"{name} has more items than configured layers: {values}")
    if size is not None and len(normalized) < size:
        normalized.extend([pad_value] * (size - len(normalized)))
    return normalized


def _legacy_entry_values(entry_first, entry_second, entry_third, layer_count):
    third_entry = entry_third if entry_third is not None else entry_second
    entries = [entry_first, entry_second, third_entry]
    while len(entries) < layer_count:
        entries.append(third_entry)
    return entries[:layer_count]


def _prepare_layer_plan(
    *,
    lot_override,
    lot_overrides,
    order_enabled,
    tp_enabled_overrides,
    tp_pips_overrides,
    tp_price_overrides=None,
    order_entries=None,
    order_comments=None,
):
    layer_count = _runtime_layer_count(
        lot_overrides,
        order_enabled,
        tp_enabled_overrides,
        tp_pips_overrides,
        tp_price_overrides,
        order_entries,
        order_comments,
    )

    lot_overrides = _normalize_layer_override_list(
        "lot_overrides", lot_overrides, None, layer_count
    )
    order_enabled = _normalize_layer_override_list(
        "order_enabled", order_enabled, False, layer_count
    )
    tp_enabled_overrides = _normalize_layer_override_list(
        "tp_enabled_overrides", tp_enabled_overrides, None, layer_count
    )
    tp_pips_overrides = _normalize_layer_override_list(
        "tp_pips_overrides", tp_pips_overrides, None, layer_count
    )
    tp_price_overrides = _normalize_layer_override_list(
        "tp_price_overrides", tp_price_overrides, None, layer_count
    )
    order_entries = _normalize_layer_override_list(
        "order_entries", order_entries, None, layer_count
    )
    order_comments = _normalize_layer_override_list(
        "order_comments", order_comments, None, layer_count
    )

    if order_enabled is not None:
        enabled_list = order_enabled
    else:
        enabled_list = [True] * layer_count

    enabled_count = sum(1 for enabled in enabled_list if enabled is True)
    if enabled_count > MAX_RUNTIME_LAYERS:
        raise ValueError(
            f"too_many_enabled_layers: maximum {MAX_RUNTIME_LAYERS}, got {enabled_count}"
        )

    if lot_overrides is not None:
        effective_lots = [LOT if lot is None else float(lot) for lot in lot_overrides]
    elif lot_override is not None:
        effective_lot = float(lot_override)
        if not (effective_lot > 0):
            raise ValueError(f"lot_override must be > 0; got: {effective_lot}")
        effective_lots = [effective_lot] * layer_count
    else:
        effective_lots = [LOT] * layer_count

    for i, lot in enumerate(effective_lots):
        if not (lot > 0):
            raise ValueError(f"effective_lots[{i}] must be > 0; got: {lot}")

    legacy_tp_enabled = [True, True, False]
    legacy_tp_pips = [TP1_PIPS, TP2_PIPS, None]
    tp_enabled_list = []
    tp_pips_list = []

    for i in range(layer_count):
        raw_enabled = tp_enabled_overrides[i] if tp_enabled_overrides is not None else None
        raw_pips = tp_pips_overrides[i] if tp_pips_overrides is not None else None

        if raw_enabled is None:
            tp_enabled_list.append(legacy_tp_enabled[i] if i < len(legacy_tp_enabled) else False)
            tp_pips_list.append(legacy_tp_pips[i] if i < len(legacy_tp_pips) else None)
        elif raw_enabled is True:
            tp_enabled_list.append(True)
            tp_pips_list.append(raw_pips)
        elif raw_enabled is False:
            tp_enabled_list.append(False)
            tp_pips_list.append(None)
        else:
            raise ValueError(
                f"tp_enabled_overrides[{i}] must be True, False, or None; got: {raw_enabled}"
            )

    for i in range(layer_count):
        if enabled_list[i] is not True or tp_enabled_list[i] is not True:
            continue
        price_override = tp_price_overrides[i] if tp_price_overrides is not None else None
        if price_override is not None:
            price_num = float(price_override)
            if price_num <= 0:
                raise ValueError(f"tp_price_overrides[{i}] must be > 0; got: {price_num}")
            continue
        pips = tp_pips_list[i]
        if pips is None:
            raise ValueError(
                f"tp_pips_list[{i}] must not be None when tp_enabled is True"
            )
        pips_num = float(pips)
        if pips_num <= 0:
            raise ValueError(f"tp_pips_list[{i}] must be > 0; got: {pips_num}")

    comments = [
        str(order_comments[i]) if order_comments is not None and order_comments[i] is not None
        else _default_layer_comment(i)
        for i in range(layer_count)
    ]

    return {
        "layer_count": layer_count,
        "configured_layers_count": layer_count,
        "enabled_layers_count": enabled_count,
        "lot_overrides": lot_overrides,
        "enabled_list": enabled_list,
        "effective_lots": effective_lots,
        "tp_enabled_list": tp_enabled_list,
        "tp_pips_list": tp_pips_list,
        "tp_price_overrides": tp_price_overrides,
        "order_entries": order_entries,
        "comments": comments,
    }


def _is_ipc_timeout(error):
    message = str(error).lower()
    return "ipc" in message and "timeout" in message


def connect():
    """Initialize MT5, log in, and log account name and balance."""
    mt5_executable = Path(MT5_PATH)
    if not mt5_executable.is_file():
        logger.error("MT5 executable not found: %s", MT5_PATH)
        return False

    for attempt in range(MT5_IPC_RETRIES + 1):
        if not mt5.initialize(
            path=MT5_PATH,
            login=MT5_LOGIN,
            password=MT5_PASSWORD,
            server=MT5_SERVER,
        ):
            error = mt5.last_error()
            mt5.shutdown()
            if _is_ipc_timeout(error) and attempt < MT5_IPC_RETRIES:
                logger.warning(
                    "MT5 initialize IPC timeout: %s. Retry attempt %s/%s in %s seconds.",
                    error,
                    attempt + 1,
                    MT5_IPC_RETRIES,
                    MT5_IPC_RETRY_DELAY,
                )
                time.sleep(MT5_IPC_RETRY_DELAY)
                continue

            if _is_ipc_timeout(error):
                raise RuntimeError(
                    f"MT5 initialize failed after {MT5_IPC_RETRIES} retries: {error}"
                )

            raise RuntimeError(f"MT5 initialize failed: {error}")

        terminal = mt5.terminal_info()
        if terminal is None:
            logger.error("Could not read MT5 terminal info: %s", mt5.last_error())
            mt5.shutdown()
            return False

        logger.info(
            "MT5 terminal connected: company=%s path=%s",
            getattr(terminal, "company", ""),
            getattr(terminal, "path", ""),
        )

        if "metaquotes" in str(getattr(terminal, "company", "")).lower():
            logger.warning(
                "Using generic MetaQuotes MT5 terminal. Account server validation will decide safety. company=%s path=%s",
                getattr(terminal, "company", ""),
                getattr(terminal, "path", ""),
            )

        if mt5.login(MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER):
            break

        error = mt5.last_error()
        mt5.shutdown()
        if _is_ipc_timeout(error) and attempt < MT5_IPC_RETRIES:
            logger.warning(
                "MT5 login IPC timeout: %s. Retry attempt %s/%s in %s seconds.",
                error,
                attempt + 1,
                MT5_IPC_RETRIES,
                MT5_IPC_RETRY_DELAY,
            )
            time.sleep(MT5_IPC_RETRY_DELAY)
            continue

        if _is_ipc_timeout(error):
            raise RuntimeError(f"MT5 login failed after {MT5_IPC_RETRIES} retries: {error}")

        raise RuntimeError(f"MT5 login failed: {error}")

    terminal = mt5.terminal_info()
    account_info = mt5.account_info()

    if terminal is None:
        logger.error("Could not read MT5 terminal info after login: %s", mt5.last_error())
        mt5.shutdown()
        return False

    if account_info is None:
        logger.error("Could not read MT5 account info: %s", mt5.last_error())
        mt5.shutdown()
        return False

    logger.info(
        "Connected MT5 terminal company=%s trade_allowed=%s | account login=%s server=%s",
        getattr(terminal, "company", ""),
        getattr(terminal, "trade_allowed", None),
        getattr(account_info, "login", None),
        getattr(account_info, "server", ""),
    )

    if getattr(account_info, "login", None) != MT5_LOGIN:
        logger.error(
            "MT5 account login mismatch: expected=%s actual=%s",
            MT5_LOGIN,
            getattr(account_info, "login", None),
        )
        mt5.shutdown()
        return False

    if getattr(account_info, "server", None) != MT5_SERVER:
        logger.error(
            "MT5 account server mismatch: expected=%s actual=%s",
            MT5_SERVER,
            getattr(account_info, "server", None),
        )
        mt5.shutdown()
        return False

    if getattr(terminal, "trade_allowed", None) is False:
        logger.warning("trade_allowed=False — check MT5 login or Algo Trading setting")
        mt5.shutdown()
        return False

    logger.info(
        "Connected to MT5 account=%s name=%s balance=%.2f",
        account_info.login,
        getattr(account_info, "name", ""),
        account_info.balance,
    )

    return True


@_with_mt5_lock
def get_current_reference_price() -> float:
    """Return the latest midpoint price for the configured MT5 symbol."""

    if connect() is not True:
        raise RuntimeError("MT5 connection failed")

    try:
        if not mt5.symbol_select(SYMBOL, True):
            raise RuntimeError(
                f"Failed to select MT5 symbol {SYMBOL}: {mt5.last_error()}"
            )

        tick = mt5.symbol_info_tick(SYMBOL)

        if tick is None:
            raise RuntimeError(
                f"Failed to retrieve tick for {SYMBOL}: {mt5.last_error()}"
            )

        bid = float(tick.bid)
        ask = float(tick.ask)

        if bid <= 0 or ask <= 0:
            raise RuntimeError(
                f"Invalid tick prices for {SYMBOL}: bid={bid}, ask={ask}"
            )

        return (bid + ask) / 2.0

    finally:
        disconnect()


@_with_mt5_lock
def check_orders(
    direction: str,
    entry_first: float,
    entry_second: float,
    sl_raw: float,
    lot_override: float | None = None,
    lot_overrides: list[float | None] | None = None,
    order_enabled: list[bool] | None = None,
    tp_enabled_overrides: list[bool | None] | None = None,
    tp_pips_overrides: list[float | None] | None = None,
    entry_third: float | None = None,
    tp_price_overrides: list[float | None] | None = None,
    order_entries: list[float | None] | None = None,
    order_comments: list[str | None] | None = None,
) -> dict:
    plan = _prepare_layer_plan(
        lot_override=lot_override,
        lot_overrides=lot_overrides,
        order_enabled=order_enabled,
        tp_enabled_overrides=tp_enabled_overrides,
        tp_pips_overrides=tp_pips_overrides,
        tp_price_overrides=tp_price_overrides,
        order_entries=order_entries,
        order_comments=order_comments,
    )

    layer_count = plan["layer_count"]
    enabled_list = plan["enabled_list"]
    effective_lots = plan["effective_lots"]
    tp_enabled_list = plan["tp_enabled_list"]
    tp_pips_list = plan["tp_pips_list"]
    tp_price_overrides = plan["tp_price_overrides"]
    order_entries = plan["order_entries"]
    comments = plan["comments"]
    effective_lot = effective_lots[0] if effective_lots else LOT


    # Compute per-order lot summary (disabled layers excluded)
    lots_per_order = effective_lots[:]  # effective lots always represent requested layer lots
    total_planned_lot = sum(
        float(lots_per_order[i]) for i in range(layer_count) if enabled_list[i] is True
    )

    result_data = {
        "ok": False,
        "symbol": SYMBOL,
        "direction": str(direction).lower(),
        "entry": entry_first,
        "sl": None,
        "entry_first": entry_first,
        "entry_second": entry_second,
        "entry_third": entry_third if entry_third is not None else entry_second,
        "current_bid": None,
        "current_ask": None,
        "volume": effective_lot,
        "total_volume": total_planned_lot,
        # New summary fields (used by TEST MODE console)
        "lots_per_order": lots_per_order,
        "enabled_orders": enabled_list,
        "total_planned_lot": total_planned_lot,
        "configured_layers_count": plan["configured_layers_count"],
        "enabled_layers_count": plan["enabled_layers_count"],
        "broker": None,
        "orders": [],
        "error": None,
    }

    if not any(enabled_list):
        result_data["error"] = "no_enabled_layers_configured"
        logger.info("no_enabled_layers_configured; skipping order check")
        return result_data

    logger.info(
        "configured_layers_count=%s enabled_layers_count=%s",
        plan["configured_layers_count"],
        plan["enabled_layers_count"],
    )

    try:
        if connect() is not True:
            result_data["error"] = "MT5 connection failed"
            return result_data

        direction_lower = str(direction).lower()
        if direction_lower not in {"buy", "sell"}:
            result_data["error"] = f"Invalid direction: {direction}"
            return result_data

        if not mt5.symbol_select(SYMBOL, True):
            result_data["error"] = f"Could not select symbol {SYMBOL}: {mt5.last_error()}"
            return result_data

        symbol_info = mt5.symbol_info(SYMBOL)
        tick = mt5.symbol_info_tick(SYMBOL)

        if symbol_info is None or tick is None:
            result_data["error"] = f"Could not get symbol/tick info: {mt5.last_error()}"
            return result_data

        digits = symbol_info.digits
        result_data["current_bid"] = getattr(tick, "bid", None)
        result_data["current_ask"] = getattr(tick, "ask", None)

        trade_stops_level = getattr(symbol_info, "trade_stops_level", None)
        point = getattr(symbol_info, "point", None)

        volume_min = getattr(symbol_info, "volume_min", None)
        volume_max = getattr(symbol_info, "volume_max", None)
        volume_step = getattr(symbol_info, "volume_step", None)

        result_data["broker"] = {
            "digits": digits,
            "point": point,
            "trade_stops_level": trade_stops_level,
            "minimum_distance": None
            if trade_stops_level is None or point is None
            else trade_stops_level * point,
            "volume_min": volume_min,
            "volume_max": volume_max,
            "volume_step": volume_step,
        }

        # Validate layer lots against broker rules.
        for order_idx, lot in enumerate(effective_lots):
            if enabled_list[order_idx] is not True:
                continue
            if volume_min is not None and lot < volume_min:
                result_data["error"] = f"Invalid lot L{order_idx + 1}={lot}; volume_min={volume_min}"
                return result_data
            if volume_max is not None and lot > volume_max:
                result_data["error"] = f"Invalid lot L{order_idx + 1}={lot}; volume_max={volume_max}"
                return result_data
            if volume_step is not None and volume_step > 0:
                steps = (lot - (volume_min if volume_min is not None else 0.0)) / volume_step
                if abs(round(steps) - steps) > 1e-6:
                    result_data["error"] = (
                        f"Invalid lot L{order_idx + 1}={lot}; volume_step={volume_step}"
                        + (f", volume_min={volume_min}" if volume_min is not None else "")
                        + (f", volume_max={volume_max}" if volume_max is not None else "")
                    )
                    return result_data

        legacy_entries = _legacy_entry_values(entry_first, entry_second, entry_third, layer_count)
        entries = [
            _normalize_price(
                order_entries[i] if order_entries is not None and order_entries[i] is not None else legacy_entries[i],
                digits,
            )
            for i in range(layer_count)
        ]
        sl_actual = _normalize_price(sl_raw, digits)
        result_data["sl"] = sl_actual
        result_data["entry"] = entries[0] if entries else entry_first
        result_data["entry_first"] = entries[0] if len(entries) > 0 else None
        result_data["entry_second"] = entries[1] if len(entries) > 1 else result_data["entry_first"]
        result_data["entry_third"] = entries[2] if len(entries) > 2 else result_data["entry_second"]

        current_price = (
            getattr(tick, "bid", None) if direction_lower == "sell" else getattr(tick, "ask", None)
        )

        if current_price is None:
            result_data["error"] = f"Could not determine current price: {mt5.last_error()}"
            return result_data

        tp_levels = []
        tp_sources = []
        for order_idx, entry_value in enumerate(entries):
            if tp_enabled_list[order_idx] is False:
                tp_levels.append(None)
                tp_sources.append("disabled")
            elif tp_price_overrides is not None and tp_price_overrides[order_idx] is not None:
                tp_val = _normalize_price(float(tp_price_overrides[order_idx]), digits)
                tp_levels.append(tp_val)
                tp_sources.append("price_override")
            else:
                pips = tp_pips_list[order_idx]
                if pips is None:
                    tp_levels.append(None)
                    tp_sources.append("none")
                else:
                    pips_num = float(pips)
                    tp_val = (
                        _normalize_price(entry_value - pips_num * PIP, digits)
                        if direction_lower == "sell"
                        else _normalize_price(entry_value + pips_num * PIP, digits)
                    )
                    tp_levels.append(tp_val)
                    tp_sources.append("pips")

        errors = []
        for order_idx, (entry_value, tp_level) in enumerate(zip(entries, tp_levels)):
            if enabled_list[order_idx] is not True:
                continue
            comment = comments[order_idx]
            if direction_lower == "buy":
                if tp_level is not None and not (sl_actual < entry_value < tp_level):
                    errors.append(
                        f"{comment} level invalid: sl_actual={sl_actual}, entry={entry_value}, tp={tp_level}"
                    )
                elif tp_level is None and not (sl_actual < entry_value):
                    errors.append(
                        f"{comment} level invalid: sl_actual={sl_actual}, entry={entry_value}"
                    )
            else:
                if tp_level is not None and not (tp_level < entry_value < sl_actual):
                    errors.append(
                        f"{comment} level invalid: tp={tp_level}, entry={entry_value}, sl_actual={sl_actual}"
                    )
                elif tp_level is None and not (entry_value < sl_actual):
                    errors.append(
                        f"{comment} level invalid: entry={entry_value}, sl_actual={sl_actual}"
                    )

        if trade_stops_level is not None and point is not None and trade_stops_level > 0:
            minimum_distance = trade_stops_level * point
            minimum_distance_norm = _normalize_price(minimum_distance, digits)

            def _dist(a, b):
                return abs(a - b)

            for order_idx, entry_value in enumerate(entries):
                if enabled_list[order_idx] is not True:
                    continue
                layer_num = order_idx + 1
                level_checks = [
                    (f"entry{layer_num}-current_price", _dist(entry_value, current_price), minimum_distance_norm),
                    (f"entry{layer_num}-sl", _dist(entry_value, sl_actual), minimum_distance_norm),
                ]
                if tp_levels[order_idx] is not None:
                    level_checks.append(
                        (f"entry{layer_num}-tp{layer_num}", _dist(entry_value, tp_levels[order_idx]), minimum_distance_norm)
                    )
                for label, actual, minimum in level_checks:
                    if actual < minimum:
                        result_data["error"] = f"{label} too close: actual={actual}, minimum={minimum}"
                        return result_data

        if errors:
            result_data["error"] = "; ".join(errors)
            return result_data

        orders = [
            (
                comments[order_idx],
                entries[order_idx],
                tp_levels[order_idx],
                _order_type(direction_lower, entries[order_idx], current_price),
                tp_sources[order_idx],
            )
            for order_idx in range(layer_count)
        ]

        for order_idx, (comment, price_level, tp_level, pending_type, tp_source) in enumerate(orders):
            # Skip disabled orders
            if not enabled_list[order_idx]:
                # Log skipped order
                skipped_record = {
                    "comment": comment,
                    "ok": True,
                    "checked": False,
                    "skipped": True,
                    "reason": "order disabled by layer config",
                    "tp_source": tp_source,
                    "request": None,
                    "retcode": None,
                    "comment_result": None,
                    "error": "Skipped by layer config",
                }
                result_data["orders"].append(skipped_record)
                continue
            
            request = {
                "action": mt5.TRADE_ACTION_PENDING,
                "symbol": SYMBOL,
                "volume": effective_lots[order_idx],
                "type": pending_type,
                "price": price_level,
                "sl": sl_actual,
                "deviation": SLIPPAGE,
                "magic": MAGIC,
                "comment": comment,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_RETURN,
            }
            request["tp"] = 0.0 if tp_level is None else tp_level

            check_result = mt5.order_check(request)
            ok = False
            retcode_result = None
            comment_result = None
            err_msg = None

            if check_result is None or getattr(check_result, "retcode", None) != 0:
                retcode_result = getattr(check_result, "retcode", None)
                comment_result = getattr(check_result, "comment", None)
                err_msg = (
                    f"order_check failed for comment={comment} "
                    f"retcode={retcode_result} comment_result={comment_result} "
                    f"last_error={mt5.last_error()} request={request}"
                )
                logger.error(
                    "Order check failed (comment=%s retcode=%s comment_result=%s last_error=%s) request=%s",
                    comment,
                    retcode_result,
                    comment_result,
                    mt5.last_error(),
                    request,
                )
                order_record = {
                    "label": comment,
                    "request": request,
                    "checked": True,
                    "ok": False,
                    "tp_source": tp_source,
                    "retcode": retcode_result,
                    "comment": comment_result,
                    "error": err_msg,
                }
                result_data["orders"].append(order_record)
                if result_data["error"] is None:
                    result_data["error"] = err_msg
                continue

            ok = True

            order_record = {
                "label": comment,
                "request": request,
                "checked": True,
                "ok": ok,
                "tp_source": tp_source,
                "retcode": getattr(check_result, "retcode", None),
                "comment": getattr(check_result, "comment", None),
                "error": None,
            }
            result_data["orders"].append(order_record)


        result_data["ok"] = all(o.get("ok") is True for o in result_data["orders"])
        return result_data

    finally:
        disconnect()


def disconnect():
    """Disconnect from MT5."""
    mt5.shutdown()


def _order_type(direction, entry, current_price):
    """Return the correct MT5 pending order type."""
    direction = direction.lower()

    if direction == "sell":
        if entry > current_price:
            return mt5.ORDER_TYPE_SELL_LIMIT
        return mt5.ORDER_TYPE_SELL_STOP

    if direction == "buy":
        if entry < current_price:
            return mt5.ORDER_TYPE_BUY_LIMIT
        return mt5.ORDER_TYPE_BUY_STOP

    raise ValueError(f"Unsupported direction: {direction}")


def _normalize_price(price, digits):
    return round(float(price), digits)


def _success_retcode(retcode):
    return retcode in {mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED}


def _build_levels(direction, entry_first, entry_second, sl_raw, digits):
    if direction == "sell":
        sl_actual = sl_raw
        tp1 = entry_first - TP1_PIPS * PIP
        tp2 = entry_second - TP2_PIPS * PIP
    elif direction == "buy":
        sl_actual = sl_raw
        tp1 = entry_first + TP1_PIPS * PIP
        tp2 = entry_second + TP2_PIPS * PIP
    else:
        raise ValueError(f"Unsupported direction: {direction}")

    return (
        _normalize_price(sl_actual, digits),
        _normalize_price(tp1, digits),
        _normalize_price(tp2, digits),
    )


def _to_int_or_none(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _matches_bot_trade(obj) -> bool:
    return (
        getattr(obj, "symbol", None) == SYMBOL
        and getattr(obj, "magic", None) == MAGIC
    )


def _tp_for_sl_update(label: str, trade_obj) -> float:
    if label == TP3_COMMENT:
        return 0.0

    existing_tp = getattr(trade_obj, "tp", 0.0)
    return 0.0 if existing_tp is None else existing_tp


def _get_pending_order_by_ticket(ticket):
    ticket_int = _to_int_or_none(ticket)
    if ticket_int is None:
        return None

    orders = mt5.orders_get(ticket=ticket_int)
    if not orders:
        orders = mt5.orders_get(symbol=SYMBOL)

    if not orders:
        return None

    for order in orders:
        if getattr(order, "ticket", None) == ticket_int and _matches_bot_trade(order):
            return order

    return None


def _get_history_position_ids_from_order(ticket) -> set[int]:
    ticket_int = _to_int_or_none(ticket)
    if ticket_int is None:
        return set()

    position_ids = set()
    history_orders = mt5.history_orders_get(ticket=ticket_int)
    if not history_orders:
        return position_ids

    for order in history_orders:
        for field_name in ("position_id", "position"):
            position_id = _to_int_or_none(getattr(order, field_name, None))
            if position_id is not None:
                position_ids.add(position_id)

    return position_ids


def _find_position_for_order_ticket(ticket):
    ticket_int = _to_int_or_none(ticket)
    if ticket_int is None:
        return None

    candidate_ids = {ticket_int}
    candidate_ids.update(_get_history_position_ids_from_order(ticket_int))

    positions = []
    direct_positions = mt5.positions_get(ticket=ticket_int)
    if direct_positions:
        positions.extend(direct_positions)

    symbol_positions = mt5.positions_get(symbol=SYMBOL)
    if symbol_positions:
        positions.extend(symbol_positions)

    seen_tickets = set()
    for position in positions:
        position_ticket = _to_int_or_none(getattr(position, "ticket", None))
        if position_ticket in seen_tickets:
            continue
        if position_ticket is not None:
            seen_tickets.add(position_ticket)

        if not _matches_bot_trade(position):
            continue

        position_ids = {
            _to_int_or_none(getattr(position, "ticket", None)),
            _to_int_or_none(getattr(position, "identifier", None)),
            _to_int_or_none(getattr(position, "position_id", None)),
            _to_int_or_none(getattr(position, "position", None)),
        }
        position_ids.discard(None)

        if candidate_ids.intersection(position_ids):
            return position

    return None


def _send_sl_update_request(request: dict) -> dict:
    check_result = mt5.order_check(request)
    if check_result is None:
        return {
            "ok": False,
            "status": "check_failed",
            "retcode": None,
            "comment": None,
            "error": f"order_check returned None: {mt5.last_error()}",
        }

    check_retcode = getattr(check_result, "retcode", None)
    check_comment = getattr(check_result, "comment", None)
    if check_retcode != 0:
        return {
            "ok": False,
            "status": "check_failed",
            "retcode": check_retcode,
            "comment": check_comment,
            "error": f"order_check failed: {mt5.last_error()}",
        }

    send_result = mt5.order_send(request)
    if send_result is None:
        return {
            "ok": False,
            "status": "send_failed",
            "retcode": None,
            "comment": None,
            "error": f"order_send returned None: {mt5.last_error()}",
        }

    send_retcode = getattr(send_result, "retcode", None)
    send_comment = getattr(send_result, "comment", None)
    ok = _success_retcode(send_retcode)

    return {
        "ok": ok,
        "status": "updated" if ok else "send_failed",
        "retcode": send_retcode,
        "comment": send_comment,
        "error": None if ok else f"order_send failed: {mt5.last_error()}",
    }


def _cancel_result_template(label, ticket, reason=None) -> dict:
    return {
        "label": label,
        "target": "pending_order",
        "db_ticket": ticket,
        "mt5_ticket": None,
        "reason": reason,
        "ok": False,
        "status": "not_sent",
        "retcode": None,
        "comment": None,
        "error": None,
    }


def _cancel_pending_order_with_active_connection(ticket, reason=None, label=None) -> dict:
    result = _cancel_result_template(label, ticket, reason=reason)

    ticket_int = _to_int_or_none(ticket)
    if ticket_int is None:
        result["status"] = "missing_ticket"
        result["error"] = "DB ticket is empty or invalid"
        return result

    result["db_ticket"] = ticket_int

    active_position = _find_position_for_order_ticket(ticket_int)
    if active_position is not None:
        result["target"] = "position"
        result["mt5_ticket"] = getattr(active_position, "ticket", None)
        result["status"] = "active_position_skip"
        result["error"] = "Ticket has an active position; pending cancellation skipped"
        logger.warning(
            "Pending cancel skipped: ticket=%s reason=%s active_position_ticket=%s",
            ticket_int,
            reason,
            result["mt5_ticket"],
        )
        return result

    orders = mt5.orders_get(ticket=ticket_int)
    if orders is None:
        result["status"] = "orders_get_failed"
        result["error"] = f"orders_get failed: {mt5.last_error()}"
        logger.error("Pending cancel lookup failed ticket=%s error=%s", ticket_int, result["error"])
        return result

    if not orders:
        result["ok"] = True
        result["status"] = "already_gone"
        logger.info("Pending cancel skipped: ticket=%s already gone reason=%s", ticket_int, reason)
        return result

    pending_order = None
    for order in orders:
        if getattr(order, "ticket", None) == ticket_int:
            pending_order = order
            break

    if pending_order is None:
        result["ok"] = True
        result["status"] = "already_gone"
        logger.info("Pending cancel skipped: ticket=%s no exact order match reason=%s", ticket_int, reason)
        return result

    result["mt5_ticket"] = ticket_int

    if not _matches_bot_trade(pending_order):
        result["status"] = "not_bot_order"
        result["error"] = (
            f"Order does not match bot symbol/magic: "
            f"symbol={getattr(pending_order, 'symbol', None)} "
            f"magic={getattr(pending_order, 'magic', None)}"
        )
        logger.warning(
            "Pending cancel skipped: ticket=%s reason=%s %s",
            ticket_int,
            reason,
            result["error"],
        )
        return result

    request = {
        "action": mt5.TRADE_ACTION_REMOVE,
        "order": ticket_int,
        "symbol": SYMBOL,
        "magic": MAGIC,
        "deviation": SLIPPAGE,
    }

    send_result = mt5.order_send(request)
    if send_result is None:
        result["status"] = "send_failed"
        result["error"] = f"order_send returned None: {mt5.last_error()}"
        logger.error(
            "Pending cancel failed: ticket=%s reason=%s error=%s request=%s",
            ticket_int,
            reason,
            result["error"],
            request,
        )
        return result

    retcode = getattr(send_result, "retcode", None)
    comment = getattr(send_result, "comment", None)
    ok = _success_retcode(retcode)

    result.update(
        {
            "ok": ok,
            "status": "cancelled" if ok else "send_failed",
            "retcode": retcode,
            "comment": comment,
            "error": None if ok else f"order_send failed: {mt5.last_error()}",
        }
    )

    if ok:
        logger.info("Pending order cancelled: ticket=%s reason=%s retcode=%s", ticket_int, reason, retcode)
    else:
        logger.error(
            "Pending cancel failed: ticket=%s reason=%s retcode=%s comment=%s error=%s request=%s",
            ticket_int,
            reason,
            retcode,
            comment,
            result["error"],
            request,
        )

    return result


@_with_mt5_lock
def cancel_pending_order(ticket, reason=None) -> dict:
    """Cancel one pending order ticket without touching active positions."""
    result = _cancel_result_template(None, ticket, reason=reason)

    try:
        if connect() is not True:
            result["status"] = "connect_failed"
            result["error"] = "MT5 connection failed"
            return result

        if not mt5.symbol_select(SYMBOL, True):
            result["status"] = "symbol_select_failed"
            result["error"] = f"Could not select symbol {SYMBOL}: {mt5.last_error()}"
            return result

        return _cancel_pending_order_with_active_connection(ticket, reason=reason)

    except Exception as exc:
        result["status"] = "exception"
        result["error"] = str(exc)
        logger.exception("Pending cancel exception ticket=%s reason=%s", ticket, reason)
        return result

    finally:
        disconnect()


@_with_mt5_lock
def cancel_pending_order_group(order_row, reason=None) -> dict:
    """Cancel all pending tickets in a DB order group without touching positions."""
    order_group_id = order_row.get("id") if isinstance(order_row, dict) else None
    result = {
        "ok": False,
        "order_group_id": order_group_id,
        "symbol": SYMBOL,
        "magic": MAGIC,
        "reason": reason,
        "cancellations": [],
        "error": None,
    }

    try:
        if connect() is not True:
            result["error"] = "MT5 connection failed"
            return result

        if not mt5.symbol_select(SYMBOL, True):
            result["error"] = f"Could not select symbol {SYMBOL}: {mt5.last_error()}"
            return result

        ticket_targets = (
            ("TG-TP1", "ticket_tp1"),
            ("TG-TP2", "ticket_tp2"),
            (TP3_COMMENT, "ticket_tp3"),
        )
        for label, ticket_key in ticket_targets:
            ticket = order_row.get(ticket_key) if isinstance(order_row, dict) else None
            if ticket is None:
                result["cancellations"].append(
                    {
                        "label": label,
                        "target": "pending_order",
                        "db_ticket": None,
                        "mt5_ticket": None,
                        "reason": reason,
                        "ok": True,
                        "status": "skipped_no_ticket",
                        "retcode": None,
                        "comment": None,
                        "error": None,
                    }
                )
                continue

            cancellation = _cancel_pending_order_with_active_connection(
                ticket,
                reason=reason,
                label=label,
            )
            result["cancellations"].append(cancellation)

        result["ok"] = all(
            cancellation.get("ok") is True
            for cancellation in result["cancellations"]
        )

        logger.info(
            "Pending order group cancel complete order_group_id=%s ok=%s reason=%s cancellations=%s",
            order_group_id,
            result["ok"],
            reason,
            result["cancellations"],
        )
        return result

    except Exception as exc:
        result["error"] = str(exc)
        logger.exception(
            "Pending order group cancel exception order_group_id=%s reason=%s",
            order_group_id,
            reason,
        )
        return result

    finally:
        disconnect()


def _update_pending_order_sl(label: str, order, new_sl: float) -> dict:
    mt5_ticket = getattr(order, "ticket", None)
    existing_tp = _tp_for_sl_update(label, order)
    existing_price = getattr(order, "price_open", None)

    result = {
        "label": label,
        "target": "pending_order",
        "db_ticket": mt5_ticket,
        "mt5_ticket": mt5_ticket,
        "previous_sl": getattr(order, "sl", None),
        "new_sl": new_sl,
        "tp": existing_tp,
        "ok": False,
        "status": "not_sent",
        "retcode": None,
        "comment": None,
        "error": None,
    }

    if existing_price is None:
        result["status"] = "missing_price"
        result["error"] = "Pending order has no price_open"
        return result

    request = {
        "action": mt5.TRADE_ACTION_MODIFY,
        "order": mt5_ticket,
        "symbol": SYMBOL,
        "price": existing_price,
        "sl": new_sl,
        "tp": existing_tp,
        "magic": MAGIC,
        "comment": SL_UPDATE_COMMENT,
        "deviation": SLIPPAGE,
    }

    type_time = getattr(order, "type_time", None)
    if type_time is not None:
        request["type_time"] = type_time

    expiration = getattr(order, "time_expiration", None)
    if expiration not in (None, 0):
        request["expiration"] = expiration

    result.update(_send_sl_update_request(request))

    if result["ok"]:
        logger.info(
            "SL follow-up updated pending order label=%s ticket=%s new_sl=%s preserved_tp=%s",
            label,
            mt5_ticket,
            new_sl,
            existing_tp,
        )
    else:
        logger.error(
            "SL follow-up failed for pending order label=%s ticket=%s new_sl=%s status=%s error=%s",
            label,
            mt5_ticket,
            new_sl,
            result.get("status"),
            result.get("error"),
        )

    return result


def _update_position_sl(label: str, db_ticket, position, new_sl: float) -> dict:
    mt5_ticket = getattr(position, "ticket", None)
    existing_tp = _tp_for_sl_update(label, position)

    result = {
        "label": label,
        "target": "position",
        "db_ticket": db_ticket,
        "mt5_ticket": mt5_ticket,
        "previous_sl": getattr(position, "sl", None),
        "new_sl": new_sl,
        "tp": existing_tp,
        "ok": False,
        "status": "not_sent",
        "retcode": None,
        "comment": None,
        "error": None,
    }

    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": SYMBOL,
        "position": mt5_ticket,
        "sl": new_sl,
        "tp": existing_tp,
        "magic": MAGIC,
        "comment": SL_UPDATE_COMMENT,
        "deviation": SLIPPAGE,
    }

    result.update(_send_sl_update_request(request))

    if result["ok"]:
        logger.info(
            "SL follow-up updated position label=%s db_ticket=%s position_ticket=%s new_sl=%s preserved_tp=%s",
            label,
            db_ticket,
            mt5_ticket,
            new_sl,
            existing_tp,
        )
    else:
        logger.error(
            "SL follow-up failed for position label=%s db_ticket=%s position_ticket=%s new_sl=%s status=%s error=%s",
            label,
            db_ticket,
            mt5_ticket,
            new_sl,
            result.get("status"),
            result.get("error"),
        )

    return result


def _update_sl_for_ticket(label: str, ticket, new_sl: float) -> dict:
    result = {
        "label": label,
        "target": None,
        "db_ticket": ticket,
        "mt5_ticket": None,
        "previous_sl": None,
        "new_sl": new_sl,
        "tp": None,
        "ok": False,
        "status": "not_found",
        "retcode": None,
        "comment": None,
        "error": None,
    }

    ticket_int = _to_int_or_none(ticket)
    if ticket_int is None:
        result["status"] = "missing_ticket"
        result["error"] = "DB ticket is empty or invalid"
        return result

    pending_order = _get_pending_order_by_ticket(ticket_int)
    if pending_order is not None:
        return _update_pending_order_sl(label, pending_order, new_sl)

    position = _find_position_for_order_ticket(ticket_int)
    if position is not None:
        return _update_position_sl(label, ticket_int, position, new_sl)

    logger.warning(
        "SL follow-up target not found label=%s db_ticket=%s symbol=%s magic=%s",
        label,
        ticket_int,
        SYMBOL,
        MAGIC,
    )
    return result


@_with_mt5_lock
def update_sl_for_order_group(order_group: dict, new_sl: float) -> dict:
    """Update SL for existing TP1, TP2, and TP3 tickets from a DB order group."""
    result = {
        "ok": False,
        "order_group_id": order_group.get("id") if isinstance(order_group, dict) else None,
        "symbol": SYMBOL,
        "magic": MAGIC,
        "new_sl": None,
        "updates": [],
        "error": None,
    }

    try:
        if connect() is not True:
            result["error"] = "MT5 connection failed"
            return result

        if not mt5.symbol_select(SYMBOL, True):
            result["error"] = f"Could not select symbol {SYMBOL}: {mt5.last_error()}"
            return result

        symbol_info = mt5.symbol_info(SYMBOL)
        if symbol_info is None:
            result["error"] = f"Could not read symbol info for {SYMBOL}: {mt5.last_error()}"
            return result

        new_sl_norm = _normalize_price(new_sl, symbol_info.digits)
        result["new_sl"] = new_sl_norm

        logger.info(
            "SL follow-up update start order_group_id=%s ticket_tp1=%s ticket_tp2=%s ticket_tp3=%s new_sl=%s",
            result["order_group_id"],
            order_group.get("ticket_tp1") if isinstance(order_group, dict) else None,
            order_group.get("ticket_tp2") if isinstance(order_group, dict) else None,
            order_group.get("ticket_tp3") if isinstance(order_group, dict) else None,
            new_sl_norm,
        )

        ticket_targets = (
            ("TG-TP1", "ticket_tp1"),
            ("TG-TP2", "ticket_tp2"),
            (TP3_COMMENT, "ticket_tp3"),
        )
        for label, ticket_key in ticket_targets:
            ticket = order_group.get(ticket_key) if isinstance(order_group, dict) else None
            if ticket is None:
                result["updates"].append(
                    {
                        "label": label,
                        "target": None,
                        "db_ticket": None,
                        "mt5_ticket": None,
                        "previous_sl": None,
                        "new_sl": new_sl_norm,
                        "tp": 0.0 if label == TP3_COMMENT else None,
                        "ok": True,
                        "status": "skipped_no_ticket",
                        "retcode": None,
                        "comment": None,
                        "error": None,
                    }
                )
                continue

            update = _update_sl_for_ticket(label, ticket, new_sl_norm)
            result["updates"].append(update)

        result["ok"] = all(update.get("ok") is True for update in result["updates"])

        logger.info(
            "SL follow-up update complete order_group_id=%s ok=%s updates=%s",
            result["order_group_id"],
            result["ok"],
            result["updates"],
        )
        return result

    except Exception as exc:
        result["error"] = str(exc)
        logger.exception("SL follow-up update failed")
        return result

    finally:
        disconnect()


@_with_mt5_lock
def place_orders(
    direction,
    entry_first,
    entry_second,
    sl_raw,
    lot_override: float | None = None,
    lot_overrides: list[float | None] | None = None,
    order_enabled: list[bool] | None = None,
    tp_enabled_overrides: list[bool | None] | None = None,
    tp_pips_overrides: list[float | int | None] | None = None,
    entry_third: float | None = None,
    tp_price_overrides: list[float | None] | None = None,
    order_entries: list[float | None] | None = None,
    order_comments: list[str | None] | None = None,
):

    """
    Place three pending orders: two TP layers and one no-TP layer.

    Returns successful ticket numbers in order: [ticket_tp1, ticket_tp2, ticket_tp3].
    """
    plan = _prepare_layer_plan(
        lot_override=lot_override,
        lot_overrides=lot_overrides,
        order_enabled=order_enabled,
        tp_enabled_overrides=tp_enabled_overrides,
        tp_pips_overrides=tp_pips_overrides,
        tp_price_overrides=tp_price_overrides,
        order_entries=order_entries,
        order_comments=order_comments,
    )

    layer_count = plan["layer_count"]
    enabled_list = plan["enabled_list"]
    effective_lots = plan["effective_lots"]
    tp_enabled_list = plan["tp_enabled_list"]
    tp_pips_list = plan["tp_pips_list"]
    tp_price_overrides = plan["tp_price_overrides"]
    order_entries = plan["order_entries"]
    comments = plan["comments"]

    if not any(enabled_list):
        logger.info("no_enabled_layers_configured; skipping order placement")
        return []

    logger.info(
        "configured_layers_count=%s enabled_layers_count=%s",
        plan["configured_layers_count"],
        plan["enabled_layers_count"],
    )

    tickets = []


    try:
        if connect() is not True:
            raise RuntimeError("MT5 connection failed")

        direction = direction.lower()

        if not mt5.symbol_select(SYMBOL, True):
            raise RuntimeError(f"Could not select symbol {SYMBOL}: {mt5.last_error()}")

        symbol_info = mt5.symbol_info(SYMBOL)
        if symbol_info is None:
            raise RuntimeError(f"Could not read symbol info for {SYMBOL}: {mt5.last_error()}")

        tick = mt5.symbol_info_tick(SYMBOL)
        if tick is None:
            raise RuntimeError(f"Could not read tick for {SYMBOL}: {mt5.last_error()}")

        digits = symbol_info.digits
        direction_lower = direction.lower()
        legacy_entries = _legacy_entry_values(entry_first, entry_second, entry_third, layer_count)
        entries = [
            _normalize_price(
                order_entries[i] if order_entries is not None and order_entries[i] is not None else legacy_entries[i],
                digits,
            )
            for i in range(layer_count)
        ]
        sl_actual = _normalize_price(sl_raw, digits)
        
        tp_levels = []
        tp_sources = []
        for order_idx, entry_value in enumerate(entries):
            if tp_enabled_list[order_idx] is False:
                tp_levels.append(None)
                tp_sources.append("disabled")
            elif tp_price_overrides is not None and tp_price_overrides[order_idx] is not None:
                tp_val = _normalize_price(float(tp_price_overrides[order_idx]), digits)
                tp_levels.append(tp_val)
                tp_sources.append("price_override")
            else:
                pips = tp_pips_list[order_idx]
                if pips is None:
                    tp_levels.append(None)
                    tp_sources.append("none")
                else:
                    pips_num = float(pips)
                    tp_val = (
                        _normalize_price(entry_value - pips_num * PIP, digits)
                        if direction_lower == "sell"
                        else _normalize_price(entry_value + pips_num * PIP, digits)
                    )
                    tp_levels.append(tp_val)
                    tp_sources.append("pips")

            logger.info(
                "L%s enabled=%s lot=%s entry_price=%s tp_source=%s tp=%s comment=%s",
                order_idx + 1,
                enabled_list[order_idx],
                effective_lots[order_idx],
                entry_value,
                tp_sources[order_idx],
                tp_levels[order_idx],
                comments[order_idx],
            )
        
        current_price = tick.bid if direction == "sell" else tick.ask

        orders = [
            (
                comments[order_idx],
                entries[order_idx],
                tp_levels[order_idx],
                _order_type(direction_lower, entries[order_idx], current_price),
            )
            for order_idx in range(layer_count)
        ]

        for order_idx, (comment, order_entry, tp, order_type) in enumerate(orders):
            # Skip disabled orders
            if not enabled_list[order_idx]:
                logger.info("Skipping %s (layer disabled)", comment)
                continue
            
            request = {
                "action": mt5.TRADE_ACTION_PENDING,
                "symbol": SYMBOL,
                "volume": effective_lots[order_idx],
                "type": order_type,
                "price": order_entry,
                "sl": sl_actual,
                "deviation": SLIPPAGE,
                "magic": MAGIC,
                "comment": comment,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_RETURN,
            }
            request["tp"] = 0.0 if tp is None else tp

            check_result = mt5.order_check(request)
            if check_result is None or getattr(check_result, "retcode", None) != 0:
                logger.error(
                    "Order check failed (comment=%s retcode=%s comment_result=%s last_error=%s) comment=%s entry=%s sl=%s tp=%s type=%s volume=%s",
                    getattr(check_result, "comment", None),
                    getattr(check_result, "retcode", None),
                    getattr(check_result, "comment", None),
                    mt5.last_error(),
                    comment,
                    order_entry,
                    sl_actual,
                    tp,
                    order_type,
                    effective_lots[order_idx],
                )
                continue

            result = mt5.order_send(request)
            if result is None:
                logger.error(
                    "%s failed entry=%s sl=%s tp=%s error=%s",
                    comment,
                    order_entry,
                    sl_actual,
                    tp,
                    mt5.last_error(),
                )
                continue

            if _success_retcode(result.retcode):
                ticket = result.order
                tickets.append(ticket)
                logger.info(
                    "%s placed ticket=%s entry=%s sl=%s tp=%s",
                    comment,
                    ticket,
                    order_entry,
                    sl_actual,
                    tp,
                )
            else:
                logger.error(
                    "%s failed retcode=%s comment=%s entry=%s sl=%s tp=%s",
                    comment,
                    result.retcode,
                    result.comment,
                    order_entry,
                    sl_actual,
                    tp,
                )

        return tickets
    finally:
        disconnect()
