"""
Telegram listener for the XAUUSD MT5 auto-trading bot.

Fill in the Telegram settings below, set SOURCE_CHAT_ID using cari_chat_id.py,
then run this file to listen for valid signals and submit MT5 pending orders.
New order state is saved to SQLite so be_monitor.py can read it from a
separate process.
"""

# Install dependencies:
# pip install telethon MetaTrader5

import asyncio
import logging
import re
import sys
from pathlib import Path

# Safety guard (defense-in-depth): refuse to run unless started with the project venv Python.
expected_venv_python = (Path(__file__).resolve().parent / ".venv" / "Scripts" / "python.exe").resolve()
current_python = Path(sys.executable).resolve()
if current_python != expected_venv_python:
    print(
        "telegram_listener.py must be started with the project venv Python.\n"
        "Use: .\\.venv\\Scripts\\python.exe .\\run_bot.py",
        flush=True,
    )
    raise SystemExit(1)


from telethon import TelegramClient, events

from db import get_latest_active_order, init_db, insert_order
from mt5_executor import (
    SYMBOL as MT5_SYMBOL,
    check_orders,
    place_orders,
    get_current_reference_price,
    update_sl_for_order_group,
)

from bot_settings import load_settings, load_runtime_layers, load_allow_real_order


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

API_ID = 37673990  # API ID from https://my.telegram.org
API_HASH = "a9a7c7a933318f577f7d16aeb05a63db"  # API hash from https://my.telegram.org
PHONE = "+6281229995423"  # Telegram phone number with country code
SOURCE_CHAT_ID = -1003511779760  # Signal channel/group/chat ID from cari_chat_id.py

# Load safe operational values from bot_config.json via bot_settings.py.
# IMPORTANT: SOURCE_CHAT_ID must be defined before the @client.on decorator.
settings = load_settings()

# --- Startup safety banner (config-driven) ---
# Must be printed after config/settings are loaded and before listening for signals.
startup_test_mode = bool(getattr(settings, "telegram_test_mode", True))

try:
    startup_allow_real_order = load_allow_real_order()
except Exception:
    # Fail safe: treat as not allowed; banner indicates blocked.
    startup_allow_real_order = None

if startup_test_mode is True:

    print(
        "\n====== BOT SAFETY MODE ======\n"
        "BOT MODE: TEST MODE\n"
        "REAL ORDER: LOCKED\n"
        "MT5 order_send disabled; order_check only.\n"
        "==============================\n",
        flush=True,
    )
elif startup_allow_real_order is not True:
    print(
        "\n====== BOT SAFETY MODE ======\n"
        "BOT MODE: REAL REQUESTED BUT BLOCKED\n"
        "REAL ORDER: LOCKED\n"
        "allow_real_order is not true.\n"
        "==============================\n",
        flush=True,
    )
else:
    print(
        "\n====== BOT SAFETY MODE ======\n"
        "BOT MODE: REAL\n"
        "REAL ORDER: ENABLED\n"
        "WARNING: order_send can place real MT5 orders.\n"
        "==============================\n",
        flush=True,
    )



LOT = settings.lot  # Lot size for each pending order
PIP = settings.pip  # 1 pip = 0.1 for XAUUSD
TP1_PIPS = settings.tp1_pips  # Pips for Order 1 take profit
TP2_PIPS = settings.tp2_pips  # Pips for Order 2 take profit
SL_BUFFER = settings.sl_buffer  # Extra pips added to the raw signal SL
EMERGENCY_SL_PIPS = settings.emergency_sl_pips  # Final fallback SL distance when signal has no SL

# False = aktifkan pengiriman pending order nyata ke MT5 (real execution)
TELEGRAM_TEST_MODE = settings.telegram_test_mode

SOURCE_CHAT_ID = settings.source_chat_id  # Signal channel/group/chat ID from cari_chat_id.py

MAGIC = 20250611  # Magic number to identify orders from this bot
SLIPPAGE = 20  # Maximum allowed slippage/deviation in points
MONITOR_INTERVAL = 5  # Seconds between each breakeven monitor check


SESSION_NAME = "xauusd_signal_session"  # Local Telethon session file name

client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

PRICE_RANGE_PATTERN = re.compile(
    r"(?P<price_a>\d+(?:\.\d+)?)\s*(?:-|\u2013)\s*(?P<price_b>\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
DIRECTION_PATTERN = re.compile(r"\b(?P<direction>buy|sell|sel)\b", re.IGNORECASE)
SL_PATTERN = re.compile(r"\bsl\b\s*[:=]?\s*(?P<sl>\d+(?:\.\d+)?)", re.IGNORECASE)
SIGNAL_STYLE_PATTERN = re.compile(r"\b(?P<style>intraday|swing)\b", re.IGNORECASE)
CHAT_TP_PATTERN = re.compile(
    r"^\s*tp\s*(?P<index>[123])\s*[:=\-]?\s*(?P<pips>\d+(?:\.\d+)?)\s*(?:pips?\b)?",
    re.IGNORECASE | re.MULTILINE,
)

# In-memory dedup store for Telegram messages: chat_id + message_id
_dedup_store: dict[str, bool] = {}
_MAX_DEDUP_KEYS = 1000


def _expand_short_price(token: str, reference_price: float) -> float:
    import math

    token_text = str(token).strip()
    if not token_text:
        raise ValueError("empty token")

    integer_part = token_text.split(".", 1)[0].lstrip("+-")
    try:
        short_value = float(token_text)
    except ValueError as e:
        raise ValueError(f"invalid price token: {token_text}") from e

    if len(integer_part) > 2:
        return short_value

    current_block = math.floor(reference_price / 100.0) * 100.0
    current_suffix = reference_price - current_block

    expanded_price = current_block + short_value

    if current_suffix >= 75.0 and short_value <= 25.0:
        expanded_price += 100.0
    elif current_suffix <= 25.0 and short_value >= 75.0:
        expanded_price -= 100.0

    return expanded_price


def _normalize_direction(direction: str) -> str:
    direction = str(direction).strip().lower()
    if direction == "sel":
        return "sell"
    return direction


def _detect_signal_style(text: str) -> str:
    style_match = SIGNAL_STYLE_PATTERN.search(text or "")
    if style_match is None:
        return "scalping/default"
    return style_match.group("style").lower()


def _parse_chat_tp_pips(text: str) -> list[float | None]:
    parsed: list[float | None] = [None, None, None]
    if not text:
        return parsed

    for match in CHAT_TP_PATTERN.finditer(text):
        order_idx = int(match.group("index")) - 1
        try:
            pips_num = float(match.group("pips"))
        except (TypeError, ValueError):
            continue
        if pips_num <= 0:
            continue
        parsed[order_idx] = int(pips_num) if pips_num.is_integer() else pips_num

    return parsed


def _find_signal_entry(text: str):
    direction_match = DIRECTION_PATTERN.search(text)
    if direction_match is None:
        return None, None

    for line in text.splitlines():
        line_direction_match = DIRECTION_PATTERN.search(line)
        line_range_match = PRICE_RANGE_PATTERN.search(line)
        if line_direction_match is not None and line_range_match is not None:
            return line_direction_match, line_range_match

    for line in text.splitlines():
        if line.lstrip().lower().startswith("tp"):
            continue
        line_range_match = PRICE_RANGE_PATTERN.search(line)
        if line_range_match is not None:
            return direction_match, line_range_match

    return direction_match, None


def _is_short_integer(token: str | None) -> bool:
    if token is None:
        return False

    token = str(token).strip()
    if not token:
        return False

    token_int = token.split(".", 1)[0].lstrip("+-")
    return len(token_int.lstrip("0")) <= 2


def _expand_if_needed(raw_token: str, reference_price: float | None) -> float:
    if raw_token is None:
        raise ValueError("raw_token is None")
    if _is_short_integer(raw_token) and reference_price is not None:
        return _expand_short_price(raw_token, reference_price)
    return float(raw_token)


def _apply_chat_sl_buffer(direction: str, chat_sl: float) -> float:
    if direction == "buy":
        return chat_sl - SL_BUFFER * PIP
    if direction == "sell":
        return chat_sl + SL_BUFFER * PIP
    raise ValueError(f"Unsupported direction for SL buffer: {direction}")


def _apply_emergency_sl(direction: str, entry: float) -> float:
    if direction == "buy":
        return entry - EMERGENCY_SL_PIPS * PIP
    if direction == "sell":
        return entry + EMERGENCY_SL_PIPS * PIP
    raise ValueError(f"Unsupported direction for emergency SL: {direction}")


def _validate_final_sl(direction: str, final_sl: float, entry: float) -> None:
    if direction == "buy" and final_sl < entry:
        return
    if direction == "sell" and final_sl > entry:
        return
    raise ValueError(
        f"Final SL invalid for {direction}: final_sl={final_sl}, entry={entry}"
    )


def parse_signal(text: str) -> dict | None:
    """Parse a signal into direction and raw (possibly short) entry range + SL.

    Output keys intentionally do NOT include expanded prices; expansion happens
    in handle_signal() once reference_price is fetched.
    """

    if not text:

        return None

    direction_match, range_match = _find_signal_entry(text)
    sl_match = SL_PATTERN.search(text)
    if direction_match is None or range_match is None:
        return None

    # Token strings; final per-order entries are resolved later.
    token_a = range_match.group("price_a")
    token_b = range_match.group("price_b")
    signal_style = _detect_signal_style(text)
    if signal_style in ("intraday", "swing"):
        chat_tp_pips = _parse_chat_tp_pips(text)
    else:
        chat_tp_pips = [None, None, None]

    return {
        "direction": _normalize_direction(direction_match.group("direction")),
        "raw_entry_first": token_a,
        "raw_entry_second": token_b,
        "raw_range": f"{token_a}-{token_b}",
        "raw_sl": sl_match.group("sl") if sl_match is not None else None,
        "signal_style": signal_style,
        "chat_tp_pips": chat_tp_pips,
    }


def parse_sl_update(text: str) -> dict | None:
    """Parse an SL-only follow-up message without creating a new signal."""
    if not text:
        return None

    if DIRECTION_PATTERN.search(text) is not None:
        return None

    if PRICE_RANGE_PATTERN.search(text) is not None:
        return None

    sl_match = SL_PATTERN.search(text)
    if sl_match is None:
        return None

    return {"raw_sl": sl_match.group("sl")}


def _build_order_plan(signal: dict, reference_price: float | None) -> dict:
    raw_first = signal.get("raw_entry_first")
    raw_second = signal.get("raw_entry_second")
    raw_sl = signal.get("raw_sl")
    direction = signal["direction"]

    expanded_first = _expand_if_needed(raw_first, reference_price)
    expanded_second = _expand_if_needed(raw_second, reference_price)
    expanded_low = min(expanded_first, expanded_second)
    expanded_high = max(expanded_first, expanded_second)

    if direction == "sell":
        selected_entry = expanded_low
        selected_entry_mode = "SELL/SEL uses lower range"
    else:
        selected_entry = expanded_high
        selected_entry_mode = "BUY uses higher range"

    expanded_sl = None
    sl_source = "chat_sl_buffered" if raw_sl is not None else "emergency_60_pips"
    if raw_sl is not None:
        expanded_sl = _expand_if_needed(raw_sl, reference_price)
        final_sl = _apply_chat_sl_buffer(direction, expanded_sl)
    else:
        final_sl = _apply_emergency_sl(direction, selected_entry)

    _validate_final_sl(direction, final_sl, selected_entry)

    tp1_target = (
        selected_entry + TP1_PIPS * PIP
        if direction == "buy"
        else selected_entry - TP1_PIPS * PIP
    )
    tp2_target = (
        selected_entry + TP2_PIPS * PIP
        if direction == "buy"
        else selected_entry - TP2_PIPS * PIP
    )

    return {
        "expanded_entry_first": expanded_first,
        "expanded_entry_second": expanded_second,
        "expanded_range": f"{expanded_first}-{expanded_second}",
        "selected_entry_mode": selected_entry_mode,
        "entry_first": selected_entry,
        "entry_second": selected_entry,
        "entry_third": selected_entry,
        "sl": final_sl,
        "sl_source": sl_source,
        "expanded_sl": expanded_sl,
        "tp1_target": tp1_target,
        "tp2_target": tp2_target,
    }


def _is_duplicate_event(event) -> bool:
    chat_id = getattr(event, "chat_id", None)
    message_id = getattr(event, "id", None)
    if message_id is None:
        message_obj = getattr(event, "message", None)
        message_id = getattr(message_obj, "id", None)

    message_key = (chat_id, message_id)
    if chat_id is not None and message_id is not None:
        if message_key in _dedup_store:
            print("Duplicate Telegram message ignored")
            return True

        _dedup_store[message_key] = True
        if len(_dedup_store) > _MAX_DEDUP_KEYS:
            # drop oldest keys
            for k in list(_dedup_store.keys())[:200]:
                _dedup_store.pop(k, None)

    return False


async def _handle_sl_update(sl_update: dict):
    loop = asyncio.get_running_loop()
    raw_sl = sl_update["raw_sl"]
    order_group = get_latest_active_order()
    reference_price = None

    if order_group is None:
        logger.warning("No active order group found for SL follow-up update.")
        print("[SL FOLLOW-UP UPDATE]")
        print("SL source: chat_sl_buffered")
        print("Raw SL:", raw_sl)
        print("No active order group found; no MT5 update sent.")
        return

    direction = str(order_group.get("direction", "")).lower()
    if direction not in {"buy", "sell"}:
        logger.error("Invalid active order direction for SL follow-up: %s", direction)
        print("[SL FOLLOW-UP UPDATE]")
        print("SL source: chat_sl_buffered")
        print("Raw SL:", raw_sl)
        print("Invalid active order direction; no MT5 update sent.")
        return

    if _is_short_integer(raw_sl):
        reference_price = await loop.run_in_executor(None, get_current_reference_price)

    expanded_sl = _expand_if_needed(raw_sl, reference_price)
    final_sl = _apply_chat_sl_buffer(direction, expanded_sl)
    entry_reference = float(
        order_group.get("entry_tp1")
        if order_group.get("entry_tp1") is not None
        else order_group.get("entry")
    )
    try:
        _validate_final_sl(direction, final_sl, entry_reference)
    except ValueError as exc:
        logger.error("SL follow-up rejected: %s", exc)
        print("[SL FOLLOW-UP UPDATE]")
        print("SL source: chat_sl_buffered")
        print("Raw SL:", raw_sl)
        print("Expanded raw SL:", expanded_sl)
        print("Final SL sent to MT5:", final_sl)
        print("SL update rejected:", exc)
        return

    logger.info(
        "SL follow-up parsed raw_sl=%s expanded_sl=%s reference_price=%s sl_source=chat_sl_buffered final_sl=%s",
        raw_sl,
        expanded_sl,
        reference_price,
        final_sl,
    )

    print("[SL FOLLOW-UP UPDATE]")
    print("SL source: chat_sl_buffered")
    print("Reference price:", reference_price)
    print("Raw SL:", raw_sl)
    print("Expanded raw SL:", expanded_sl)
    print("Final SL sent to MT5:", final_sl)

    print("Latest active order group id:", order_group.get("id"))
    print("TP1 ticket:", order_group.get("ticket_tp1"))
    print("TP2 ticket:", order_group.get("ticket_tp2"))

    if TELEGRAM_TEST_MODE is True:
        print("[TELEGRAM TEST MODE]")
        print("No MT5 SL will be modified")
        print(
            "Would update SL for latest active order group:",
            {
                "id": order_group.get("id"),
                "ticket_tp1": order_group.get("ticket_tp1"),
                "ticket_tp2": order_group.get("ticket_tp2"),
                "new_sl": final_sl,
            },
        )
        return

    try:
        update_result = await loop.run_in_executor(
            None,
            update_sl_for_order_group,
            order_group,
            final_sl,
        )
    except Exception:
        logger.exception("Failed to update SL follow-up in MT5.")
        return

    print("SL follow-up update result:", update_result)
    for update in update_result.get("updates", []):
        logger.info(
            "SL follow-up update target=%s db_ticket=%s status=%s mt5_ticket=%s new_sl=%s",
            update.get("label"),
            update.get("db_ticket"),
            update.get("status"),
            update.get("mt5_ticket"),
            update.get("new_sl"),
        )


PARSER_MANUAL_CHECKS = (
    "SELL 4566-4569 SL 4574 -> sell, entries 4566/4566, buffered SL 4575",
    "SEL 4566-4569 SL 4574 -> sell, entries 4566/4566, buffered SL 4575",
    "BUY 4566-4569 SL 4563 -> buy, entries 4569/4569, buffered SL 4562",
    "recov sel 56-58 sl 60 with reference 4655 -> sell, entries 4656/4656, buffered SL 4661",
    "BUY 4566-4569 -> buy, entries 4569/4569, emergency SL 4563",
    "SELL 4566-4569 -> sell, entries 4566/4566, emergency SL 4572",
    "SL 4574 -> follow-up update only",
    "SL 60 with reference 4655 -> follow-up update only, buffered by active order direction",
)




@client.on(events.NewMessage(chats=SOURCE_CHAT_ID))
async def handle_signal(event):
    """Handle a new Telegram message from the configured signal chat."""
    logger.info("Telegram message received")
    raw_text = event.raw_text or ""
    logger.info("Raw Telegram text: %s", raw_text)
    print("Raw Telegram text:", raw_text)

    signal = parse_signal(raw_text)
    if signal is None:
        sl_update = parse_sl_update(raw_text)
        if sl_update is None:
            logger.info("Bukan format sinyal, dilewati.")
            return
        if _is_duplicate_event(event):
            return
        await _handle_sl_update(sl_update)
        return

    if _is_duplicate_event(event):
        return


    loop = asyncio.get_running_loop()
    reference_price = None
    raw_first = signal.get("raw_entry_first")
    raw_second = signal.get("raw_entry_second")
    raw_sl = signal.get("raw_sl")

    # Decide whether expansion is needed
    need_reference = (
        (raw_first is not None and _is_short_integer(raw_first))
        or (raw_second is not None and _is_short_integer(raw_second))
        or (raw_sl is not None and _is_short_integer(raw_sl))
    )

    if need_reference:
        reference_price = await loop.run_in_executor(None, get_current_reference_price)

    try:
        order_plan = _build_order_plan(signal, reference_price)
    except ValueError as exc:
        logger.error("Signal rejected: %s", exc)
        print("Signal rejected:", exc)
        return
    signal.update(order_plan)
    signal_style = signal.get("signal_style", "scalping/default")
    chat_tp_pips = signal.get("chat_tp_pips")
    if not isinstance(chat_tp_pips, list) or len(chat_tp_pips) != 3:
        chat_tp_pips = [None, None, None]
    tp_source = "chat_message" if signal_style in ("intraday", "swing") else "layer_config"

    logger.info(
        "Signal valid direction=%s raw_range=%s expanded_range=%s mode=%s entry_first=%s entry_second=%s entry_third=%s raw_sl=%s expanded_sl=%s sl_source=%s final_sl=%s",
        signal["direction"],
        signal.get("raw_range"),
        signal["expanded_range"],
        signal["selected_entry_mode"],
        signal["entry_first"],
        signal["entry_second"],
        signal["entry_third"],
        raw_sl,
        signal.get("expanded_sl"),
        signal["sl_source"],
        signal["sl"],
    )
    logger.info("Signal style: %s", signal_style)
    logger.info("TP source: %s", tp_source)
    logger.info(
        "Parsed chat TP pips: tp1=%s, tp2=%s, tp3=%s",
        chat_tp_pips[0],
        chat_tp_pips[1],
        chat_tp_pips[2],
    )

    print("Direction:", signal["direction"].upper())
    print("Signal style:", signal_style)
    print("TP source:", tp_source)
    print(
        "Parsed chat TP pips: "
        f"tp1={chat_tp_pips[0]}, tp2={chat_tp_pips[1]}, tp3={chat_tp_pips[2]}"
    )
    print("Reference price:", reference_price)
    print("Raw entry range:", f"{raw_first}-{raw_second}")
    print("Expanded range:", signal["expanded_range"])
    print("Selected entry mode:", signal["selected_entry_mode"])
    print("Final entry_first:", signal["entry_first"])
    print("Final entry_second:", signal["entry_second"])
    print("Final entry_third:", signal["entry_third"])
    print("SL source:", signal["sl_source"])
    print("Raw SL:", raw_sl)
    print("Expanded raw SL:", signal.get("expanded_sl"))
    print("Final SL sent to MT5:", signal["sl"])
    if tp_source == "layer_config":
        print("TP1 target:", signal["tp1_target"])
        print("TP2 target:", signal["tp2_target"])
        print("Layer 3 / TG-NO-TP: layer config/default")
    else:
        print("Chat TP pips override matching layer TP pips for this signal only")

    # Load runtime layers to determine per-order lot_overrides and order_enabled
    lot_overrides = None
    order_enabled = None
    tp_enabled_overrides = None
    tp_pips_overrides = None
    try:
        layers = load_runtime_layers()
        if layers is None:
            # No layer config: use legacy behavior (all None, all enabled)
            logger.info("Layer config missing; using legacy lot for all 3 orders")
            print("Layer config missing; using legacy lot for all 3 orders")
            lot_overrides = None
            order_enabled = None
        elif len(layers) == 0:
            # Empty layer list: skip signal (fail-safe)
            logger.info("Layer config exists but has no layers; skipping signal")
            print("Layer config exists but has no layers; skipping signal")
            return
        else:
            # Non-empty list: map layers[0:3] to orders[0:3]
            lot_overrides = [None, None, None]
            order_enabled = [True, True, True]
            tp_enabled_overrides = [None, None, None]
            tp_pips_overrides = [None, None, None]
            mapping_parts = []
            
            for order_idx in range(3):
                layer_num = order_idx + 1  # Layer 1, Layer 2, Layer 3
                if len(layers) > order_idx:
                    layer = layers[order_idx]
                    layer_enabled = layer.get("enabled", False)
                    layer_lot = layer.get("lot")
                    layer_name = layer.get("name", f"L{layer_num}")

                    # Per-layer TP overrides
                    tp_enabled = layer.get("tp_enabled", None)
                    tp_pips = layer.get("tp_pips", None)

                    if layer_enabled:
                        # Layer enabled: use its lot
                        lot_overrides[order_idx] = layer_lot
                        order_enabled[order_idx] = True

                        tp_enabled_overrides[order_idx] = tp_enabled
                        if tp_enabled is True:
                            tp_pips_overrides[order_idx] = tp_pips
                        elif tp_enabled is False:
                            tp_pips_overrides[order_idx] = None

                        tp_label = "off" if tp_enabled is False else f"{tp_pips}"
                        mapping_parts.append(
                            f"{layer_name}=enabled lot={layer_lot} tp={tp_label}"
                        )
                    else:
                        # Layer disabled: skip this order only
                        order_enabled[order_idx] = False
                        lot_overrides[order_idx] = None
                        mapping_parts.append(f"{layer_name}=disabled skipped")
                else:
                    # Layer missing: use default lot, enable order
                    lot_overrides[order_idx] = None
                    order_enabled[order_idx] = True
                    mapping_parts.append(f"L{layer_num}=missing default")
            
            mapping_str = ", ".join(mapping_parts)
            logger.info("Layer mapping: %s", mapping_str)
            print(f"Layer mapping: {mapping_str}")

            # Log ignored fields for this phase
            logger.info(
                "Layer BE/comment saved only; runtime uses enabled/lot/TP. MT5 comments remain fixed for BE tracking."
            )
            print(
                "Layer BE/comment saved only; runtime uses enabled/lot/TP. MT5 comments remain fixed for BE tracking."
            )


    except Exception as exc:
        # Exception loading layers: skip signal (fail-safe)
        logger.error("Exception loading runtime layers: %s; skipping signal", exc)
        print(f"Exception loading runtime layers: {exc}; skipping signal")
        return

    if tp_source == "chat_message":
        if tp_enabled_overrides is None:
            tp_enabled_overrides = [None, None, None]
        if tp_pips_overrides is None:
            tp_pips_overrides = [None, None, None]

        chat_override_parts = []
        for order_idx, chat_pips in enumerate(chat_tp_pips):
            layer_num = order_idx + 1
            if chat_pips is None:
                chat_override_parts.append(f"tp{layer_num}=preserve_config")
                continue
            tp_enabled_overrides[order_idx] = True
            tp_pips_overrides[order_idx] = chat_pips
            chat_override_parts.append(f"tp{layer_num}={chat_pips}")

        chat_override_log = ", ".join(chat_override_parts)
        logger.info("Chat TP override result: %s", chat_override_log)
        logger.info(
            "Effective TP overrides after source selection: enabled=%s pips=%s",
            tp_enabled_overrides,
            tp_pips_overrides,
        )
        print(f"Chat TP override result: {chat_override_log}")
        print(
            "Effective TP overrides after source selection: "
            f"enabled={tp_enabled_overrides} pips={tp_pips_overrides}"
        )
    else:
        logger.info("Effective TP source remains layer_config")

    # TEST MODE path
    if TELEGRAM_TEST_MODE is True:
        # Reference: MT5 symbol is fixed in mt5_executor.py; do not redefine it here.

        print("[TELEGRAM TEST MODE]")

        print("Signal received and parsed successfully")
        print(f"Direction: {signal.get('direction', 'Unavailable').upper()}")
        print(f"Symbol: {MT5_SYMBOL}")
        print(f"Stop Loss input: {signal['sl']}")

        try:
            loop = asyncio.get_running_loop()
            check_result = await loop.run_in_executor(
                None,
                check_orders,
                signal["direction"],
                signal["entry_first"],
                signal["entry_second"],
                signal["sl"],
                None,  # lot_override (legacy)
                lot_overrides,  # per-order lots
                order_enabled,  # per-order enabled
                tp_enabled_overrides,  # per-order TP enabled overrides
                tp_pips_overrides,  # per-order TP pips overrides
            )
        except Exception:
            logger.exception("check_orders failed")
            print("[MT5 ORDER_CHECK ONLY]")
            print("No order will be sent")
            return

        print("[MT5 ORDER_CHECK ONLY]")
        print("No order will be sent")
        # Output ringkasan MT5 order_check done above, then return (TEST MODE)


        def _u(x):
            return x if x is not None else "Unavailable"

        broker = check_result.get("broker") if isinstance(check_result, dict) else None

        print("Overall result:")
        print(f"Symbol: {_u(check_result.get('symbol') if isinstance(check_result, dict) else None)}")
        print(f"Direction: {_u(check_result.get('direction') if isinstance(check_result, dict) else None)}")
        print(f"Entry: {_u(check_result.get('entry') if isinstance(check_result, dict) else None)}")
        print(f"Stop Loss: {_u(check_result.get('sl') if isinstance(check_result, dict) else None)}")
        print(f"Current Bid: {_u(check_result.get('current_bid') if isinstance(check_result, dict) else None)}")
        print(f"Current Ask: {_u(check_result.get('current_ask') if isinstance(check_result, dict) else None)}")
        # Summary (prefer per-layer lots if available)
        if isinstance(check_result, dict) and check_result.get("lots_per_order") is not None:
            lots = check_result.get("lots_per_order")
            enabled_orders = check_result.get("enabled_orders")
            # keep print simple and stable
            print(
                f"Lots per order: "
                f"{_u(lots[0] if isinstance(lots, list) and len(lots) > 0 else None)}, "
                f"{_u(lots[1] if isinstance(lots, list) and len(lots) > 1 else None)}, "
                f"{_u(lots[2] if isinstance(lots, list) and len(lots) > 2 else None)}"
            )
            print(
                "Total planned lot: "
                f"{_u(check_result.get('total_planned_lot'))}"
            )
        else:
            # Backward compatibility
            print(f"Lot per order: {_u(check_result.get('volume') if isinstance(check_result, dict) else None)}")
            print(f"Total planned lot: {_u(check_result.get('total_volume') if isinstance(check_result, dict) else None)}")

        print(f"Trade stops level: {_u(broker.get('trade_stops_level') if isinstance(broker, dict) else None)}")
        print(f"Minimum distance: {_u(broker.get('minimum_distance') if isinstance(broker, dict) else None)}")
        print(f"Volume min: {_u(broker.get('volume_min') if isinstance(broker, dict) else None)}")
        print(f"Volume max: {_u(broker.get('volume_max') if isinstance(broker, dict) else None)}")
        print(f"Volume step: {_u(broker.get('volume_step') if isinstance(broker, dict) else None)}")
        overall_error = check_result.get("error") if isinstance(check_result, dict) else None
        overall_error_str = str(overall_error) if overall_error is not None else ""
        if overall_error not in (None, "", "Unavailable") and overall_error_str not in ("Unavailable",):
            print(f"Error: {_u(overall_error)}")


        for order in (check_result.get("orders", []) if isinstance(check_result, dict) else []):
            req = order.get("request") if isinstance(order, dict) else None
            print("Order:")
            print(f"Type: {_u(req.get('type') if isinstance(req, dict) else None)}")
            print(f"Price: {_u(req.get('price') if isinstance(req, dict) else None)}")
            print(f"SL: {_u(req.get('sl') if isinstance(req, dict) else None)}")
            print(f"TP: {_u(req.get('tp') if isinstance(req, dict) else None)}")
            print(f"Volume: {_u(req.get('volume') if isinstance(req, dict) else None)}")
            print(f"Checked: {_u(order.get('checked') if isinstance(order, dict) else None)}")
            print(f"Result: {_u('OK' if order.get('ok') is True else 'FAIL' if order.get('ok') is False else None) if isinstance(order, dict) else 'Unavailable'}")
            print(f"Retcode: {_u(order.get('retcode') if isinstance(order, dict) else None)}")
            print(f"Comment: {_u(order.get('comment') if isinstance(order, dict) else None)}")
            order_error = order.get("error") if isinstance(order, dict) else None
            order_error_str = str(order_error) if order_error is not None else ""
            if order_error not in (None, "", "Unavailable") and order_error_str not in ("Unavailable",):
                print(f"Error: {_u(order_error)}")


        return

    # Real execution path: check if allow_real_order is true before proceeding
    try:
        allow_real_order = load_allow_real_order()
    except Exception as exc:
        logger.error("Exception loading allow_real_order flag: %s; skipping signal", exc)
        print(f"Exception loading allow_real_order flag: {exc}; skipping signal")
        return

    if not allow_real_order:
        logger.warning("REAL mode blocked: allow_real_order is not true in bot_config.json; skipping signal")
        print("REAL mode blocked: allow_real_order is not true; signal skipped")
        return

    direction = signal["direction"]
    sl = signal["sl"]

    print("[REAL MT5 EXECUTION]")

    print(f"Direction: {direction.upper()}")
    print(f"Symbol: {MT5_SYMBOL}")
    print(f"Entry first: {signal['entry_first']}")
    print(f"Entry second: {signal['entry_second']}")
    print(f"Entry third: {signal['entry_third']}")

    print(f"Stop Loss: {sl}")

    loop = asyncio.get_running_loop()
    try:
        tickets = await loop.run_in_executor(
            None,
            place_orders,
            direction,
            signal["entry_first"],
            signal["entry_second"],
            sl,
            None,  # lot_override (legacy)
            lot_overrides,  # per-order lots
            order_enabled,  # per-order enabled
            tp_enabled_overrides,  # per-order TP enabled overrides
            tp_pips_overrides,  # per-order TP pips overrides
        )
    except Exception:
        logger.exception("Gagal mengirim order ke MT5.")
        return

    print(f"MT5 returned tickets: {tickets}")

    if len(tickets) >= 3:
        insert_order(
            tickets[0],
            tickets[1],
            direction,
            signal["entry_first"],
            signal["entry_second"],
            ticket_tp3=tickets[2],
            entry_tp3=signal["entry_third"],
        )

        logger.info("Three pending orders placed successfully")
    elif len(tickets) >= 2:
        insert_order(
            tickets[0],
            tickets[1],
            direction,
            signal["entry_first"],
            signal["entry_second"],
            ticket_tp3=None,
            entry_tp3=signal["entry_third"],
        )

        logger.info("Two pending orders placed successfully")
    elif len(tickets) == 1:
        logger.warning("Partial failure: only 1 pending order placed tickets=%s", tickets)
    else:
        logger.warning("No MT5 orders were placed")



async def main():
    """Start Telegram auth and listen until disconnected."""
    init_db()
    await client.start(phone=PHONE)

    me = await client.get_me()
    username = me.username or " ".join(
        part for part in (me.first_name, me.last_name) if part
    )
    logger.info("Logged in to Telegram as %s", username or me.id)
    logger.info("Mendengarkan sinyal dari chat ID: %s", SOURCE_CHAT_ID)

    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Telegram listener stopped by user")
        print("Telegram listener stopped by user")
