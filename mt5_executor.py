r"""
MT5 execution module for the XAUUSD Telegram signal bot.

Fill in the MT5 account settings below. This module is called by
telegram_listener.py to place two pending orders from one parsed signal.

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
from pathlib import Path

import MetaTrader5 as mt5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

MT5_LOGIN = 371836460  # Your MT5 account login number from your broker
MT5_PASSWORD = "sw34LOG2311@"  # Your MT5 account password
MT5_SERVER = "ValetaxIntl-Live2"  # Your MT5 broker server name exactly as shown in MT5

# IMPORTANT: set to your Valetax terminal64.exe path.
# This is a placeholder; update it to match your local installation.
MT5_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"

SYMBOL = "XAUUSD.vx"  # Broker symbol to trade
LOT = 0.01  # Lot size for each pending order
PIP = 0.1  # 1 pip = 0.1 for XAUUSD
TP1_PIPS = 50  # Pips for Order 1 take profit
TP2_PIPS = 100  # Pips for Order 2 take profit
SL_BUFFER = 10  # Extra pips added to the raw signal SL
MAGIC = 20250611  # Magic number to identify orders from this bot
SLIPPAGE = 20  # Maximum allowed slippage/deviation in points
MONITOR_INTERVAL = 5  # Seconds between each breakeven monitor check
MT5_IPC_RETRIES = 3  # Number of retries when MT5 returns an IPC timeout
MT5_IPC_RETRY_DELAY = 5  # Seconds to wait between IPC timeout retries


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
        if not mt5.initialize(path=MT5_PATH):
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
                "Connected to MetaQuotes terminal; expected Valetax terminal. company=%s path=%s",
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


def check_orders(
    direction: str,
    entry_first: float,
    entry_second: float,
    sl_raw: float,
) -> dict:
    result_data = {
        "ok": False,
        "symbol": SYMBOL,
        "direction": str(direction).lower(),
        "entry": entry_first,
        "sl": None,
        "entry_first": entry_first,
        "entry_second": entry_second,
        "current_bid": None,
        "current_ask": None,
        "volume": LOT,
        "total_volume": LOT * 2,
        "broker": None,
        "orders": [],
        "error": None,
    }

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

        # Validate LOT against broker rules.
        if volume_min is not None and LOT < volume_min:
            result_data["error"] = f"Invalid LOT={LOT}; volume_min={volume_min}"
            return result_data
        if volume_max is not None and LOT > volume_max:
            result_data["error"] = f"Invalid LOT={LOT}; volume_max={volume_max}"
            return result_data
        if volume_step is not None and volume_step > 0:
            steps = (LOT - (volume_min if volume_min is not None else 0.0)) / volume_step
            if abs(round(steps) - steps) > 1e-6:
                result_data["error"] = (
                    f"Invalid LOT={LOT}; volume_step={volume_step}"
                    + (f", volume_min={volume_min}" if volume_min is not None else "")
                    + (f", volume_max={volume_max}" if volume_max is not None else "")
                )
                return result_data

        entry_first_norm = _normalize_price(entry_first, digits)
        entry_second_norm = _normalize_price(entry_second, digits)
        sl_raw_norm = _normalize_price(sl_raw, digits)

        sl_actual = (
            _normalize_price(sl_raw_norm + SL_BUFFER * PIP, digits)
            if direction_lower == "sell"
            else _normalize_price(sl_raw_norm - SL_BUFFER * PIP, digits)
        )

        tp1 = (
            _normalize_price(entry_first_norm - TP1_PIPS * PIP, digits)
            if direction_lower == "sell"
            else _normalize_price(entry_first_norm + TP1_PIPS * PIP, digits)
        )
        tp2 = (
            _normalize_price(entry_second_norm - TP2_PIPS * PIP, digits)
            if direction_lower == "sell"
            else _normalize_price(entry_second_norm + TP2_PIPS * PIP, digits)
        )
        result_data["sl"] = sl_actual

        current_price = (
            getattr(tick, "bid", None) if direction_lower == "sell" else getattr(tick, "ask", None)
        )

        if current_price is None:
            result_data["error"] = f"Could not determine current price: {mt5.last_error()}"
            return result_data

        order_type_tp1 = _order_type(direction_lower, entry_first_norm, current_price)
        order_type_tp2 = _order_type(direction_lower, entry_second_norm, current_price)


        # Validate SL/TP orientation per-order entry.
        errors = []
        if direction_lower == "buy":
            # TP BUY: SL < entry_first < TP1 and SL < entry_second < TP2
            if not (sl_actual < entry_first_norm < tp1):
                errors.append(
                    f"TG-TP1 level invalid: sl_actual={sl_actual}, entry_first={entry_first_norm}, tp1={tp1}"
                )
            if not (sl_actual < entry_second_norm < tp2):
                errors.append(
                    f"TG-TP2 level invalid: sl_actual={sl_actual}, entry_second={entry_second_norm}, tp2={tp2}"
                )
        else:
            # SELL: TP BUY means order direction is sell, so TP1 < entry_first < SL and TP2 < entry_second < SL
            if not (tp1 < entry_first_norm < sl_actual):
                errors.append(
                    f"TG-TP1 level invalid: tp1={tp1}, entry_first={entry_first_norm}, sl_actual={sl_actual}"
                )
            if not (tp2 < entry_second_norm < sl_actual):
                errors.append(
                    f"TG-TP2 level invalid: tp2={tp2}, entry_second={entry_second_norm}, sl_actual={sl_actual}"
                )


        # Minimum distance validation.
        if trade_stops_level is not None and point is not None and trade_stops_level > 0:
            minimum_distance = trade_stops_level * point
            minimum_distance_norm = _normalize_price(minimum_distance, digits)

            def _dist(a, b):
                return abs(a - b)

            level_checks = [
                ("entry1-current_price", _dist(entry_first_norm, current_price), minimum_distance_norm),
                ("entry2-current_price", _dist(entry_second_norm, current_price), minimum_distance_norm),
                ("entry1-sl", _dist(entry_first_norm, sl_actual), minimum_distance_norm),
                ("entry2-sl", _dist(entry_second_norm, sl_actual), minimum_distance_norm),
                ("entry1-tp1", _dist(entry_first_norm, tp1), minimum_distance_norm),
                ("entry2-tp2", _dist(entry_second_norm, tp2), minimum_distance_norm),
            ]

            for label, actual, minimum in level_checks:
                if actual < minimum:
                    result_data["error"] = f"{label} too close: actual={actual}, minimum={minimum}"
                    return result_data

        if errors:
            result_data["error"] = "; ".join(errors)
            return result_data


        orders = (
            ("TG-TP1", entry_first_norm, tp1, order_type_tp1),
            ("TG-TP2", entry_second_norm, tp2, order_type_tp2),
        )

        for comment, price_level, tp_level, pending_type in orders:
            request = {
                "action": mt5.TRADE_ACTION_PENDING,
                "symbol": SYMBOL,
                "volume": LOT,
                "type": pending_type,
                "price": price_level,
                "sl": sl_actual,
                "tp": tp_level,

                "deviation": SLIPPAGE,
                "magic": MAGIC,
                "comment": comment,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_RETURN,
            }

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
        sl_actual = sl_raw + SL_BUFFER * PIP
        tp1 = entry_first - TP1_PIPS * PIP
        tp2 = entry_second - TP2_PIPS * PIP
    elif direction == "buy":
        sl_actual = sl_raw - SL_BUFFER * PIP
        tp1 = entry_first + TP1_PIPS * PIP
        tp2 = entry_second + TP2_PIPS * PIP
    else:
        raise ValueError(f"Unsupported direction: {direction}")

    return (
        _normalize_price(sl_actual, digits),
        _normalize_price(tp1, digits),
        _normalize_price(tp2, digits),
    )


def place_orders(direction, entry_first, entry_second, sl_raw):
    """
    Place two pending orders at separate entries with different TP targets.

    Returns a list of successful ticket numbers: [ticket_tp1, ticket_tp2].
    """
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
        entry_first = _normalize_price(entry_first, digits)
        entry_second = _normalize_price(entry_second, digits)
        sl_raw = _normalize_price(sl_raw, digits)
        sl_actual, tp1, tp2 = _build_levels(direction, entry_first, entry_second, sl_raw, digits)
        current_price = tick.bid if direction == "sell" else tick.ask
        order_type_tp1 = _order_type(direction, entry_first, current_price)
        order_type_tp2 = _order_type(direction, entry_second, current_price)

        orders = (
            ("TG-TP1", entry_first, tp1, order_type_tp1),
            ("TG-TP2", entry_second, tp2, order_type_tp2),
        )

        for comment, order_entry, tp, order_type in orders:
            request = {
                "action": mt5.TRADE_ACTION_PENDING,
                "symbol": SYMBOL,
                "volume": LOT,
                "type": order_type,
                "price": order_entry,
                "sl": sl_actual,
                "tp": tp,
                "deviation": SLIPPAGE,
                "magic": MAGIC,
                "comment": comment,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_RETURN,
            }

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
                    LOT,
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
