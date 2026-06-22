"""
Breakeven monitor for the XAUUSD MT5 auto-trading bot.

Implements TP1 -> BE(TP2) safely:
- Pairing strictly starts from DB-persisted tickets (ticket_tp1, ticket_tp2, ticket_tp3)
- TP1 proof uses history:
  * history_orders_get(ticket=<pending_tp1_ticket>) to get position_id
  * history_deals_get(position=<tp1_position_id>) to find an exit deal
  * only consider Take Profit closes: reason==DEAL_REASON_TP (defensive via getattr)
  * require profit > 0
- TP2 BE uses actual open price of TP2 position: tp2_position.price_open
- TP3 BE uses DB entry_tp3 when available and does not set TP
- Before modifying SLTP:
  * run mt5.order_check(request)
  * only if check passes, run mt5.order_send(request)
- Anti-repeat: only process rows from get_pending_orders() (be_moved=0)
  * after success: mark_be_moved(order_id, tp2_position_ticket=..., tp3_position_ticket=...)

No automation is executed during editing.
"""

import logging
import time
from typing import Optional, Tuple

import MetaTrader5 as mt5

from db import (
    get_pending_orders,
    init_db,
    mark_be_moved,
    mark_near_entry_seen,
    mark_pending_cancelled,
)


from mt5_lock import mt5_process_lock
from mt5_executor import MAGIC, PIP, SLIPPAGE, SYMBOL, TP1_PIPS, cancel_pending_order_group

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# IMPORTANT: keep connection credentials as they were in the original be_monitor.py
MT5_LOGIN = 2171043269  # real
#MT5_LOGIN = 371863329  # demo


MT5_PASSWORD = "sw34LOG2311@"  # Your MT5 account password


MT5_SERVER = "ValetaxIntl-Live7"  # real
#MT5_SERVER = "ValetaxIntl-Live2"  # demo


# Load safe operational values from bot_config.json via bot_settings.py.
# IMPORTANT: avoid duplicating PIP/TP constants here; those come from mt5_executor.
from bot_settings import load_settings

settings = load_settings()

MONITOR_INTERVAL = settings.monitor_interval


TP1_COMMENT = "TG-TP1"
TP2_COMMENT = "TG-TP2"
TP3_COMMENT = "TG-NO-TP"
BE_COMMENT = "TG-TP2-BE"
BE_TP3_COMMENT = "TG-NO-TP-BE"
NEAR_ENTRY_MIN_PIPS = 0
NEAR_ENTRY_MAX_PIPS = 20
MISS_ENTRY_RUNAWAY_CANCEL_PIPS = TP1_PIPS
MISSED_ENTRY_CANCEL_REASON = "missed_entry_reached_tp1_after_near_entry"
MISS_ENTRY_RUNAWAY_CANCEL_REASON = "missed_entry_runaway_without_fill"
POSITION_MATCH_TOLERANCE_PIPS = 3
TP1_PROOF_TOLERANCE_PIPS = 3


def connect():
    """Initialize MT5, log in, and log account name and balance."""
    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")

    if not mt5.login(MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER):
        error = mt5.last_error()
        mt5.shutdown()
        raise RuntimeError(f"MT5 login failed: {error}")

    account_info = mt5.account_info()
    if account_info is None:
        raise RuntimeError(f"Could not read MT5 account info: {mt5.last_error()}")

    logger.info(
        "Connected to MT5 account=%s name=%s balance=%.2f",
        account_info.login,
        getattr(account_info, "name", ""),
        account_info.balance,
    )


def disconnect():
    """Disconnect from MT5."""
    mt5.shutdown()


def _safe_symbol_digits() -> Optional[int]:
    info = mt5.symbol_info(SYMBOL)
    return getattr(info, "digits", None) if info else None


def _normalize_price(price: float, digits: int) -> float:
    return round(float(price), digits)


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


def _get_all_bot_pending_orders() -> list:
    orders = mt5.orders_get(symbol=SYMBOL)
    if orders is None:
        logger.error("orders_get failed for symbol=%s error=%s", SYMBOL, mt5.last_error())
        return []
    return [order for order in orders if _matches_bot_trade(order)]


def _get_all_bot_positions() -> list:
    positions = mt5.positions_get(symbol=SYMBOL)
    if positions is None:
        logger.error("positions_get failed for symbol=%s error=%s", SYMBOL, mt5.last_error())
        return []
    return [position for position in positions if _matches_bot_trade(position)]


def _get_positions_by_magic_comment(all_positions: list | None = None) -> Tuple[list, list, list]:
    """Active positions for this bot (best-effort)."""
    pos_tp1 = []
    pos_tp2 = []
    pos_tp3 = []
    positions = all_positions if all_positions is not None else _get_all_bot_positions()
    if not positions:
        return pos_tp1, pos_tp2, pos_tp3

    for p in positions:
        comment = getattr(p, "comment", "")
        if comment == TP1_COMMENT:
            pos_tp1.append(p)
        elif comment == TP2_COMMENT:
            pos_tp2.append(p)
        elif comment == TP3_COMMENT:
            pos_tp3.append(p)

    return pos_tp1, pos_tp2, pos_tp3


def _find_pending_order_in_list(ticket, pending_orders: list):
    ticket_int = _to_int_or_none(ticket)
    if ticket_int is None:
        return None

    for order in pending_orders:
        if getattr(order, "ticket", None) == ticket_int:
            return order

    return None


def _trade_summary(obj) -> dict | None:
    if obj is None:
        return None

    return {
        "ticket": getattr(obj, "ticket", None),
        "identifier": getattr(obj, "identifier", None),
        "position_id": getattr(obj, "position_id", None),
        "type": getattr(obj, "type", None),
        "reason": getattr(obj, "reason", None),
        "price_open": getattr(obj, "price_open", None),
        "price": getattr(obj, "price", None),
        "sl": getattr(obj, "sl", None),
        "tp": getattr(obj, "tp", None),
        "profit": getattr(obj, "profit", None),
        "magic": getattr(obj, "magic", None),
        "symbol": getattr(obj, "symbol", None),
        "comment": getattr(obj, "comment", None),
        "time": getattr(obj, "time", None),
        "time_done": getattr(obj, "time_done", None),
        "entry": getattr(obj, "entry", None),
        "state": getattr(obj, "state", None),
    }


def _summarize_trades(items: list) -> list[dict]:
    return [_trade_summary(item) for item in items]


def _get_history_orders_for_ticket(ticket) -> list:
    ticket_int = _to_int_or_none(ticket)
    if ticket_int is None:
        return []

    orders = mt5.history_orders_get(ticket=ticket_int)
    if not orders:
        return []

    return [
        order
        for order in orders
        if getattr(order, "ticket", None) == ticket_int and _matches_bot_trade(order)
    ]


def _get_history_deals_for_position_ids(position_ids: set[int]) -> list:
    return _get_history_deals_for_position_ids_filtered(position_ids, include_non_bot=False)


def _get_history_deals_for_position_ids_filtered(
    position_ids: set[int],
    include_non_bot: bool = False,
) -> list:
    deals = []
    seen_tickets = set()
    for position_id in position_ids:
        if position_id in (None, 0):
            continue
        history_deals = mt5.history_deals_get(position=position_id)
        if not history_deals:
            continue
        for deal in history_deals:
            ticket = getattr(deal, "ticket", None)
            if ticket in seen_tickets:
                continue
            if ticket is not None:
                seen_tickets.add(ticket)
            if include_non_bot or _matches_bot_trade(deal):
                deals.append(deal)
    return deals


def _position_ids_from_history_order(pending_ticket) -> set[int]:
    ticket_int = _to_int_or_none(pending_ticket)
    if ticket_int is None:
        return set()

    position_ids = set()
    orders = _get_history_orders_for_ticket(ticket_int)
    if not orders:
        return position_ids

    for order in orders:
        for field_name in ("position_id", "position"):
            position_id = _to_int_or_none(getattr(order, field_name, None))
            if position_id not in (None, 0):
                position_ids.add(position_id)

    return position_ids


def _build_ticket_state(label: str, ticket, pending_orders: list, all_positions: list) -> dict:
    ticket_int = _to_int_or_none(ticket)
    state = {
        "label": label,
        "ticket": ticket_int if ticket_int is not None else ticket,
        "status": "skipped_no_ticket",
        "pending_order": None,
        "pending_order_state": None,
        "active_position": None,
        "active_position_state": None,
        "history_orders": [],
        "history_order_states": [],
        "history_position_ids": set(),
        "history_deals": [],
        "history_deal_states": [],
        "all_history_deals": [],
        "all_history_deal_states": [],
    }

    if ticket_int is None:
        return state

    pending_order = _find_pending_order_in_list(ticket_int, pending_orders)
    history_orders = _get_history_orders_for_ticket(ticket_int)
    position_ids = set()
    for order in history_orders:
        for field_name in ("position_id", "position"):
            position_id = _to_int_or_none(getattr(order, field_name, None))
            if position_id not in (None, 0):
                position_ids.add(position_id)

    active_position = _find_active_position_for_ticket(ticket_int, all_positions)
    history_deals = _get_history_deals_for_position_ids(position_ids)
    all_history_deals = _get_history_deals_for_position_ids_filtered(
        position_ids,
        include_non_bot=True,
    )

    if active_position is not None:
        status = "active_position"
    elif pending_order is not None:
        status = "pending"
    elif position_ids or history_deals or all_history_deals:
        status = "history_position"
    elif history_orders:
        status = "history_no_position"
    else:
        status = "not_found"

    state.update(
        {
            "status": status,
            "pending_order": pending_order,
            "pending_order_state": _trade_summary(pending_order),
            "active_position": active_position,
            "active_position_state": _trade_summary(active_position),
            "history_orders": history_orders,
            "history_order_states": _summarize_trades(history_orders),
            "history_position_ids": position_ids,
            "history_deals": history_deals,
            "history_deal_states": _summarize_trades(history_deals),
            "all_history_deals": all_history_deals,
            "all_history_deal_states": _summarize_trades(all_history_deals),
        }
    )
    return state


def _position_identity_values(position) -> set[int]:
    values = set()
    for field_name in ("ticket", "identifier", "position_id", "position"):
        value = _to_int_or_none(getattr(position, field_name, None))
        if value not in (None, 0):
            values.add(value)
    return values


def _find_active_position_for_ticket(pending_ticket, all_positions: list):
    ticket_int = _to_int_or_none(pending_ticket)
    if ticket_int is None:
        return None

    candidate_ids = {ticket_int}
    candidate_ids.update(_position_ids_from_history_order(ticket_int))

    for position in all_positions:
        if candidate_ids.intersection(_position_identity_values(position)):
            return position

    return None


def _expected_position_type(direction: str):
    if direction == "buy":
        return getattr(mt5, "POSITION_TYPE_BUY", 0)
    if direction == "sell":
        return getattr(mt5, "POSITION_TYPE_SELL", 1)
    return None


def _position_direction_matches(position, direction: str) -> bool:
    expected_type = _expected_position_type(direction)
    if expected_type is None:
        return False
    return getattr(position, "type", None) == expected_type


def _price_close_to_entry(price, entry, tolerance_pips: float = POSITION_MATCH_TOLERANCE_PIPS) -> bool:
    if price is None or entry is None:
        return False
    return abs(float(price) - float(entry)) <= tolerance_pips * PIP


def _fallback_position_eligible(position, expected_comment: str, expected_entry: float, direction: str) -> bool:
    return (
        position is not None
        and _matches_bot_trade(position)
        and getattr(position, "comment", None) == expected_comment
        and _position_direction_matches(position, direction)
        and _price_close_to_entry(getattr(position, "price_open", None), expected_entry)
    )


def _fallback_position_candidates(
    all_positions: list,
    expected_comment: str,
    expected_entry: float,
    direction: str,
) -> list:
    candidates = [
        position
        for position in all_positions
        if _fallback_position_eligible(position, expected_comment, expected_entry, direction)
    ]
    return sorted(
        candidates,
        key=lambda position: abs(float(getattr(position, "price_open", 0.0)) - float(expected_entry)),
    )


def _find_position_for_layer_from_snapshot(
    layer_name: str,
    pending_ticket,
    expected_comment: str,
    expected_entry: float,
    direction: str,
    all_positions: list,
) -> dict:
    by_ticket = _find_active_position_for_ticket(pending_ticket, all_positions)
    if by_ticket is not None:
        unsafe_reason = None
        if not _position_direction_matches(by_ticket, direction):
            unsafe_reason = f"{layer_name}_position_direction_mismatch"

        return {
            "layer": layer_name,
            "position": by_ticket,
            "position_state": _trade_summary(by_ticket),
            "position_id": getattr(by_ticket, "identifier", getattr(by_ticket, "ticket", None)),
            "match_method": "ticket_or_history_position_id",
            "fallback_eligible": _fallback_position_eligible(
                by_ticket,
                expected_comment,
                expected_entry,
                direction,
            ),
            "unsafe_reason": unsafe_reason,
        }

    candidates = _fallback_position_candidates(
        all_positions,
        expected_comment,
        expected_entry,
        direction,
    )
    if len(candidates) == 1:
        position = candidates[0]
        return {
            "layer": layer_name,
            "position": position,
            "position_state": _trade_summary(position),
            "position_id": getattr(position, "identifier", getattr(position, "ticket", None)),
            "match_method": "comment_price_fallback",
            "fallback_eligible": True,
            "unsafe_reason": None,
        }

    unsafe_reason = None
    if len(candidates) > 1:
        unsafe_reason = f"{layer_name}_ambiguous_comment_price_matches"

    return {
        "layer": layer_name,
        "position": None,
        "position_state": None,
        "position_id": None,
        "match_method": "not_found",
        "fallback_eligible": False,
        "unsafe_reason": unsafe_reason,
    }


def _get_cleanup_ticket_state(
    label: str,
    ticket,
    all_positions: list,
    pending_orders: list | None = None,
) -> dict:
    ticket_int = _to_int_or_none(ticket)
    state = {
        "label": label,
        "ticket": ticket,
        "status": "skipped_no_ticket",
        "ok_to_cleanup": True,
        "position_ids": set(),
        "active_position_ticket": None,
    }

    if ticket_int is None:
        return state

    state["ticket"] = ticket_int
    position_ids = _position_ids_from_history_order(ticket_int)
    state["position_ids"] = position_ids

    active_position = _find_active_position_for_ticket(ticket_int, all_positions)
    if active_position is not None:
        state["status"] = "active_position"
        state["ok_to_cleanup"] = False
        state["active_position_ticket"] = getattr(active_position, "ticket", None)
        return state

    if position_ids:
        state["status"] = "position_history"
        state["ok_to_cleanup"] = False
        return state

    pending_order = None
    if pending_orders is not None:
        pending_order = _find_pending_order_in_list(ticket_int, pending_orders)

    if pending_order is None:
        orders = mt5.orders_get(ticket=ticket_int)
        if orders is None:
            state["status"] = "orders_lookup_failed"
            state["ok_to_cleanup"] = False
            return state

        for order in orders:
            if getattr(order, "ticket", None) == ticket_int:
                pending_order = order
                break

    if pending_order is None:
        state["status"] = "already_gone_without_position"
        return state

    if not _matches_bot_trade(pending_order):
        state["status"] = "not_bot_order"
        state["ok_to_cleanup"] = False
        return state

    state["status"] = "pending"
    return state


def _find_position_by_comment(position_list: list, expected_comment: str):
    for p in position_list:
        if getattr(p, "comment", None) == expected_comment:
            return p
    return None


def _get_position_id_from_pending_ticket(pending_ticket: int) -> Optional[int]:
    """Get position_id using history_orders_get(ticket=<pending_ticket>)."""
    if pending_ticket is None:
        return None

    orders = mt5.history_orders_get(ticket=pending_ticket)
    if not orders:
        return None

    # Defensive: try common fields
    for o in orders:
        pos_id = getattr(o, "position_id", None)
        if pos_id is not None:
            return pos_id

    # Fallback: some builds may store position as 'position'
    for o in orders:
        pos_id = getattr(o, "position", None)
        if pos_id is not None:
            return pos_id

    return None


def _tp1_closed_by_take_profit_profit_positive(tp1_position_id: int) -> bool:
    """Verify TP1 closed by Take Profit with positive profit."""
    if tp1_position_id is None:
        return False

    deals = mt5.history_deals_get(position=tp1_position_id)
    if not deals:
        return False

    # We must use reason + exit deal.
    deal_entry_out = {getattr(mt5, "DEAL_ENTRY_OUT", None), getattr(mt5, "DEAL_ENTRY_OUT_BY", None)}
    deal_reason_tp = getattr(mt5, "DEAL_REASON_TP", None)

    total_profit_positive = False
    has_tp_exit = False

    for d in deals:
        entry = getattr(d, "entry", None)
        reason = getattr(d, "reason", None)
        profit = getattr(d, "profit", None)

        # Determine if this deal is an exit.
        is_exit = entry in deal_entry_out if entry is not None else False

        # Determine TP reason.
        is_tp_reason = (deal_reason_tp is not None and reason == deal_reason_tp)

        # Some brokers may not provide DEAL_REASON_TP constant, but we still have reason field.
        # If constant missing, be strict and require exact match string contains 'tp'.
        if not is_tp_reason and deal_reason_tp is None and reason is not None:
            is_tp_reason = "tp" in str(reason).lower()

        if is_exit and is_tp_reason:
            has_tp_exit = True
            if profit is not None and profit > 0:
                total_profit_positive = True

    return has_tp_exit and total_profit_positive


def _get_symbol_trade_stops_level() -> Tuple[Optional[float], Optional[int]]:
    info = mt5.symbol_info(SYMBOL)
    if not info:
        return None, None
    point = getattr(info, "point", None)
    trade_stops_level = getattr(info, "trade_stops_level", None)
    digits = getattr(info, "digits", None)

    if point is None or trade_stops_level is None:
        return None, digits
    return trade_stops_level * point, digits


def _validate_be_sl(be_sl: float, direction: str, bid: float, ask: float, entry: float) -> bool:
    # BUY: BE SL must stay below current Bid.
    if direction.lower() == "buy":
        if be_sl >= bid:
            return False
        return True

    # SELL: BE SL must stay above current Ask.
    if direction.lower() == "sell":
        if be_sl <= ask:
            return False
        return True

    return False


def _order_check_and_send_sl_tp(request: dict) -> bool:
    logger.info("order_check BE SLTP request=%s", request)
    check_result = mt5.order_check(request)
    if check_result is None:
        logger.error(
            "order_check BE SLTP failed: returned None. comment=%s request=%s last_error=%s",
            request.get("comment"),
            request,
            mt5.last_error(),
        )
        return False

    retcode = getattr(check_result, "retcode", None)
    comment = getattr(check_result, "comment", None)
    logger.info(
        "order_check BE SLTP result: retcode=%s comment=%s last_error=%s request=%s",
        retcode,
        comment,
        mt5.last_error(),
        request,
    )
    if retcode != 0:
        logger.error(
            "order_check BE SLTP failed: retcode=%s comment=%s last_error=%s request=%s",
            retcode,
            comment,
            mt5.last_error(),
            request,
        )
        return False

    logger.info("order_send BE SLTP request=%s", request)
    result = mt5.order_send(request)
    if result is None:
        logger.error(
            "order_send BE SLTP failed: result None last_error=%s request=%s",
            mt5.last_error(),
            request,
        )
        return False

    result_retcode = getattr(result, "retcode", None)
    logger.info(
        "order_send BE SLTP result: retcode=%s comment=%s last_error=%s request=%s",
        result_retcode,
        getattr(result, "comment", None),
        mt5.last_error(),
        request,
    )
    if result_retcode not in {mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED}:
        logger.error(
            "order_send BE SLTP failed: retcode=%s comment=%s last_error=%s request=%s",
            result_retcode,
            getattr(result, "comment", None),
            mt5.last_error(),
            request,
        )
        return False

    return True


def _find_position_for_pending_ticket(order_id: int, layer_name: str, pending_ticket, position_list: list):
    position_id = _get_position_id_from_pending_ticket(pending_ticket)
    if position_id is None:
        logger.info("order_id=%s: %s masih pending atau belum menjadi posisi.", order_id, layer_name)
        return None, None

    for p in position_list:
        p_pos_id = getattr(p, "position_id", None)
        if p_pos_id is not None and p_pos_id == position_id:
            return position_id, p

    for p in position_list:
        if getattr(p, "ticket", None) == position_id:
            return position_id, p

    logger.info(
        "order_id=%s: %s posisi belum ditemukan/terpasang untuk position_id=%s.",
        order_id,
        layer_name,
        position_id,
    )
    return position_id, None


def _deal_is_exit(deal) -> bool:
    entry = getattr(deal, "entry", None)
    exit_values = {
        getattr(mt5, "DEAL_ENTRY_OUT", None),
        getattr(mt5, "DEAL_ENTRY_OUT_BY", None),
    }
    exit_values.discard(None)
    if entry in exit_values:
        return True

    profit = getattr(deal, "profit", None)
    comment = str(getattr(deal, "comment", "")).lower()
    return profit not in (None, 0) and ("tp" in comment or "sl" in comment)


def _deal_closed_by_tp(deal) -> bool:
    reason = getattr(deal, "reason", None)
    deal_reason_tp = getattr(mt5, "DEAL_REASON_TP", None)
    if deal_reason_tp is not None and reason == deal_reason_tp:
        return True
    return "tp" in str(getattr(deal, "comment", "")).lower()


def _deal_is_manual_close(deal) -> bool:
    if not _deal_is_exit(deal):
        return False

    reason = getattr(deal, "reason", None)
    manual_reasons = {
        getattr(mt5, "DEAL_REASON_CLIENT", None),
        getattr(mt5, "DEAL_REASON_MOBILE", None),
        getattr(mt5, "DEAL_REASON_WEB", None),
    }
    manual_reasons.discard(None)

    deal_magic = getattr(deal, "magic", None)
    comment = str(getattr(deal, "comment", "") or "").strip()

    return (
        reason in manual_reasons
        or deal_magic not in (None, MAGIC)
        or (deal_magic == 0 and comment == "")
    )


def _ticket_states_have_pending_or_active(snapshot: dict) -> bool:
    for state in (snapshot.get("ticket_states") or {}).values():
        if state.get("pending_order") is not None:
            return True
        if state.get("active_position") is not None:
            return True
    return False


def _ticket_states_have_history_for_all_tickets(snapshot: dict) -> bool:
    ticket_states = snapshot.get("ticket_states") or {}
    ticket_keys = ("TP1", "TP2", "TP3")
    for layer_name in ticket_keys:
        state = ticket_states.get(layer_name) or {}
        if state.get("ticket") is None:
            continue
        if not (
            state.get("history_orders")
            or state.get("history_deals")
            or state.get("all_history_deals")
        ):
            return False
    return True


def _manual_exit_deals_from_snapshot(snapshot: dict) -> list:
    manual_deals = []
    seen_tickets = set()
    for state in (snapshot.get("ticket_states") or {}).values():
        for deal in state.get("all_history_deals") or []:
            ticket = getattr(deal, "ticket", None)
            if ticket in seen_tickets:
                continue
            if ticket is not None:
                seen_tickets.add(ticket)
            if _deal_is_manual_close(deal):
                manual_deals.append(deal)
    return manual_deals


def _exit_deals_from_snapshot(snapshot: dict) -> list:
    exit_deals = []
    seen_tickets = set()
    for state in (snapshot.get("ticket_states") or {}).values():
        for deal in state.get("all_history_deals") or state.get("history_deals") or []:
            ticket = getattr(deal, "ticket", None)
            if ticket in seen_tickets:
                continue
            if ticket is not None:
                seen_tickets.add(ticket)
            if _deal_is_exit(deal):
                exit_deals.append(deal)
    return exit_deals


def _retire_reason_for_inactive_mt5_row(snapshot: dict) -> tuple[str | None, dict]:
    if _ticket_states_have_pending_or_active(snapshot):
        return None, {"reason": "pending_or_active_still_exists"}

    if not _ticket_states_have_history_for_all_tickets(snapshot):
        return None, {"reason": "history_not_available_for_all_tickets"}

    manual_deals = _manual_exit_deals_from_snapshot(snapshot)
    if manual_deals:
        return "manual_closed_in_mt5", {
            "manual_exit_deals": _summarize_trades(manual_deals),
        }

    exit_deals = _exit_deals_from_snapshot(snapshot)
    if exit_deals:
        return "closed_in_mt5_no_active_positions", {
            "exit_deals": _summarize_trades(exit_deals),
        }

    return "manual_removed_pending_in_mt5", {
        "history_orders": _snapshot_ticket_log_payload(snapshot.get("ticket_states") or {}),
    }


def _attempt_retire_inactive_mt5_row(order_row: dict, snapshot: dict) -> tuple[bool, str]:
    order_id = order_row.get("id")
    if int(order_row.get("pending_cancelled") or 0) == 1:
        return True, "already_marked_pending_cancelled"

    retire_reason, details = _retire_reason_for_inactive_mt5_row(snapshot)
    if retire_reason is None:
        return False, details.get("reason", "not_inactive")

    mark_pending_cancelled(order_id, retire_reason)
    logger.info(
        "order_id=%s: retired inactive MT5 row. reason=%s details=%s",
        order_id,
        retire_reason,
        details,
    )
    return True, retire_reason


def _tp1_history_proof(snapshot: dict) -> dict:
    tp1_state = (snapshot.get("ticket_states") or {}).get("TP1") or {}
    history_orders = tp1_state.get("history_orders") or []
    history_deals = tp1_state.get("history_deals") or []
    target_price = snapshot.get("tp1_target_price")

    if not history_orders and not history_deals:
        return {"ok": False, "method": "history", "reason": "tp1_history_not_found"}

    tp1_comment_seen = any(getattr(order, "comment", None) == TP1_COMMENT for order in history_orders)
    tp1_comment_seen = tp1_comment_seen or any(
        getattr(deal, "comment", None) == TP1_COMMENT for deal in history_deals
    )
    if not tp1_comment_seen:
        return {"ok": False, "method": "history", "reason": "tp1_history_comment_not_found"}

    exit_deals = [deal for deal in history_deals if _deal_is_exit(deal)]
    if not exit_deals:
        return {"ok": False, "method": "history", "reason": "tp1_exit_deal_not_found"}

    for deal in exit_deals:
        if _deal_closed_by_tp(deal):
            return {
                "ok": True,
                "method": "history_closed_by_tp",
                "reason": "tp1_closed_by_tp",
                "deal": _trade_summary(deal),
            }

    for deal in exit_deals:
        profit = getattr(deal, "profit", None)
        price = getattr(deal, "price", None)
        if (
            profit is not None
            and profit > 0
            and target_price is not None
            and _price_close_to_entry(price, target_price, TP1_PROOF_TOLERANCE_PIPS)
        ):
            return {
                "ok": True,
                "method": "history_positive_profit_near_tp1",
                "reason": "tp1_positive_profit_near_target",
                "deal": _trade_summary(deal),
            }

    return {
        "ok": False,
        "method": "history",
        "reason": "tp1_history_found_but_not_tp_or_positive_near_target",
        "exit_deals": _summarize_trades(exit_deals),
    }


def _market_crossed_tp1(snapshot: dict) -> tuple[bool, str | None, float | None]:
    direction = snapshot.get("direction")
    target = snapshot.get("tp1_target_price")
    if direction == "buy":
        price = snapshot.get("bid")
        return price is not None and target is not None and price >= target, "bid", price
    if direction == "sell":
        price = snapshot.get("ask")
        return price is not None and target is not None and price <= target, "ask", price
    return False, None, None


def _tp1_safe_fallback_proof(snapshot: dict) -> dict:
    tp1_state = (snapshot.get("ticket_states") or {}).get("TP1") or {}
    if tp1_state.get("pending_order") is not None:
        return {"ok": False, "method": "fallback", "reason": "tp1_pending_still_exists"}

    unsafe_reasons = snapshot.get("unsafe_reasons") or []
    if unsafe_reasons:
        return {
            "ok": False,
            "method": "fallback",
            "reason": "unsafe_position_mismatch",
            "unsafe_reasons": unsafe_reasons,
        }

    position_matches = snapshot.get("position_matches") or {}
    matched_layers = []
    for layer_name in ("TP2", "TP3"):
        match = position_matches.get(layer_name) or {}
        if match.get("position") is not None and match.get("fallback_eligible") is True:
            matched_layers.append(
                {
                    "layer": layer_name,
                    "match_method": match.get("match_method"),
                    "position": match.get("position_state"),
                }
            )

    if not matched_layers:
        return {
            "ok": False,
            "method": "fallback",
            "reason": "no_matching_active_tp2_tp3_position",
        }

    crossed, price_source, price = _market_crossed_tp1(snapshot)
    if not crossed:
        return {
            "ok": False,
            "method": "fallback",
            "reason": "market_has_not_crossed_tp1_target",
            "price_source": price_source,
            "price": price,
            "tp1_target_price": snapshot.get("tp1_target_price"),
        }

    return {
        "ok": True,
        "method": "fallback_active_position_market_cross",
        "reason": "tp1_pending_gone_active_layers_market_crossed",
        "matched_layers": matched_layers,
        "price_source": price_source,
        "price": price,
        "tp1_target_price": snapshot.get("tp1_target_price"),
    }


def _send_be_for_position(
    order_id: int,
    layer_name: str,
    position,
    be_sl: float,
    comment: str,
    include_tp: bool,
) -> tuple[bool, Optional[int], Optional[float]]:
    pos_ticket = getattr(position, "ticket", None)
    if pos_ticket is None:
        logger.info("order_id=%s: %s position ticket missing.", order_id, layer_name)
        return False, None, None

    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": SYMBOL,
        "position": pos_ticket,
        "sl": be_sl,
        "magic": MAGIC,
        "comment": comment,
        "deviation": SLIPPAGE,
    }

    existing_tp = None
    if include_tp:
        existing_tp = getattr(position, "tp", None)
        if existing_tp is not None:
            request["tp"] = existing_tp
    else:
        existing_tp = 0.0
        request["tp"] = 0.0

    ok = _order_check_and_send_sl_tp(request)
    if ok:
        logger.info(
            "order_id=%s: %s BE SLTP success position_ticket=%s new_sl=%s preserved_tp=%s",
            order_id,
            layer_name,
            pos_ticket,
            be_sl,
            existing_tp,
        )
    else:
        logger.error(
            "order_id=%s: %s BE SLTP failed position_ticket=%s new_sl=%s preserved_tp=%s",
            order_id,
            layer_name,
            pos_ticket,
            be_sl,
            existing_tp,
        )
    return ok, pos_ticket, existing_tp


def _cleanup_state_log_payload(ticket_states: list[dict]) -> list[dict]:
    payload = []
    for state in ticket_states:
        payload.append(
            {
                "label": state.get("label"),
                "ticket": state.get("ticket"),
                "status": state.get("status"),
                "ok_to_cleanup": state.get("ok_to_cleanup"),
                "position_ids": sorted(state.get("position_ids") or []),
                "active_position_ticket": state.get("active_position_ticket"),
            }
        )
    return payload


def _row_float(order_row: dict, key: str, fallback_key: str | None = None) -> float | None:
    value = order_row.get(key)
    if value is None and fallback_key is not None:
        value = order_row.get(fallback_key)
    if value is None:
        return None
    return float(value)


def _tp1_target_price(direction: str, entry_tp1: float) -> float | None:
    if direction == "buy":
        return entry_tp1 + TP1_PIPS * PIP
    if direction == "sell":
        return entry_tp1 - TP1_PIPS * PIP
    return None


def _snapshot_ticket_log_payload(ticket_states: dict) -> dict:
    payload = {}
    for layer_name, state in ticket_states.items():
        payload[layer_name] = {
            "label": state.get("label"),
            "ticket": state.get("ticket"),
            "status": state.get("status"),
            "pending_order": state.get("pending_order_state"),
            "active_position": state.get("active_position_state"),
            "history_position_ids": sorted(state.get("history_position_ids") or []),
            "history_orders": state.get("history_order_states"),
            "history_deals": state.get("history_deal_states"),
            "all_history_deals": state.get("all_history_deal_states"),
        }
    return payload


def _position_match_log_payload(position_matches: dict) -> dict:
    payload = {}
    for layer_name, match in position_matches.items():
        payload[layer_name] = {
            "match_method": match.get("match_method"),
            "fallback_eligible": match.get("fallback_eligible"),
            "unsafe_reason": match.get("unsafe_reason"),
            "position_id": match.get("position_id"),
            "position": match.get("position_state"),
        }
    return payload


def _build_order_state_snapshot(order_row: dict, pending_orders: list, all_positions: list) -> dict:
    direction = str(order_row.get("direction", "")).lower()
    legacy_entry = _row_float(order_row, "entry", "entry_tp1")
    entry_tp1 = _row_float(order_row, "entry_tp1", "entry")
    entry_tp2 = _row_float(order_row, "entry_tp2", "entry")
    entry_tp3 = _row_float(order_row, "entry_tp3", "entry")

    tick = mt5.symbol_info_tick(SYMBOL)
    bid = getattr(tick, "bid", None) if tick else None
    ask = getattr(tick, "ask", None) if tick else None
    current_price_source = None
    current_price = None
    if direction == "buy":
        current_price_source = "ask"
        current_price = ask
    elif direction == "sell":
        current_price_source = "bid"
        current_price = bid

    distance_to_entry_pips = None
    if current_price is not None and legacy_entry is not None:
        distance_to_entry_pips = abs(float(current_price) - legacy_entry) / PIP

    ticket_states = {
        "TP1": _build_ticket_state(
            TP1_COMMENT,
            order_row.get("ticket_tp1"),
            pending_orders,
            all_positions,
        ),
        "TP2": _build_ticket_state(
            TP2_COMMENT,
            order_row.get("ticket_tp2"),
            pending_orders,
            all_positions,
        ),
        "TP3": _build_ticket_state(
            TP3_COMMENT,
            order_row.get("ticket_tp3"),
            pending_orders,
            all_positions,
        ),
    }

    position_matches = {
        "TP2": _find_position_for_layer_from_snapshot(
            "TP2",
            order_row.get("ticket_tp2"),
            TP2_COMMENT,
            entry_tp2,
            direction,
            all_positions,
        ),
        "TP3": _find_position_for_layer_from_snapshot(
            "TP3",
            order_row.get("ticket_tp3"),
            TP3_COMMENT,
            entry_tp3,
            direction,
            all_positions,
        ),
    }
    unsafe_reasons = [
        match.get("unsafe_reason")
        for match in position_matches.values()
        if match.get("unsafe_reason")
    ]

    cleanup_ticket_states = [
        _get_cleanup_ticket_state(
            TP1_COMMENT,
            order_row.get("ticket_tp1"),
            all_positions,
            pending_orders,
        ),
        _get_cleanup_ticket_state(
            TP2_COMMENT,
            order_row.get("ticket_tp2"),
            all_positions,
            pending_orders,
        ),
        _get_cleanup_ticket_state(
            TP3_COMMENT,
            order_row.get("ticket_tp3"),
            all_positions,
            pending_orders,
        ),
    ]

    return {
        "order_id": order_row.get("id"),
        "direction": direction,
        "entry": legacy_entry,
        "entry_tp1": entry_tp1,
        "entry_tp2": entry_tp2,
        "entry_tp3": entry_tp3,
        "ticket_tp1": order_row.get("ticket_tp1"),
        "ticket_tp2": order_row.get("ticket_tp2"),
        "ticket_tp3": order_row.get("ticket_tp3"),
        "bid": bid,
        "ask": ask,
        "current_price_source": current_price_source,
        "current_price": current_price,
        "distance_to_entry_pips": distance_to_entry_pips,
        "near_entry_seen": int(order_row.get("near_entry_seen") or 0) == 1,
        "tp1_target_price": None
        if entry_tp1 is None
        else _tp1_target_price(direction, entry_tp1),
        "runaway_threshold_price": None
        if legacy_entry is None
        else _runaway_threshold_price(direction, legacy_entry),
        "ticket_states": ticket_states,
        "cleanup_ticket_states": cleanup_ticket_states,
        "position_matches": position_matches,
        "unsafe_reasons": unsafe_reasons,
    }


def _log_monitor_decision(snapshot: dict, decision: str, reason: str, extra: dict | None = None) -> None:
    logger.info(
        "order_id=%s monitor decision=%s reason=%s direction=%s entry=%s bid=%s ask=%s current_price_source=%s current_price=%s tp1_target_price=%s runaway_threshold_price=%s distance_to_entry_pips=%s near_entry_seen=%s ticket_states=%s position_matches=%s extra=%s",
        snapshot.get("order_id"),
        decision,
        reason,
        snapshot.get("direction"),
        snapshot.get("entry"),
        snapshot.get("bid"),
        snapshot.get("ask"),
        snapshot.get("current_price_source"),
        snapshot.get("current_price"),
        snapshot.get("tp1_target_price"),
        snapshot.get("runaway_threshold_price"),
        snapshot.get("distance_to_entry_pips"),
        snapshot.get("near_entry_seen"),
        _snapshot_ticket_log_payload(snapshot.get("ticket_states") or {}),
        _position_match_log_payload(snapshot.get("position_matches") or {}),
        extra,
    )


def _runaway_tp1_reached(direction: str, current_price: float, entry: float) -> bool:
    if direction == "buy":
        return current_price >= entry + MISS_ENTRY_RUNAWAY_CANCEL_PIPS * PIP
    if direction == "sell":
        return current_price <= entry - MISS_ENTRY_RUNAWAY_CANCEL_PIPS * PIP
    return False


def _runaway_threshold_price(direction: str, entry: float) -> float | None:
    if direction == "buy":
        return entry + MISS_ENTRY_RUNAWAY_CANCEL_PIPS * PIP
    if direction == "sell":
        return entry - MISS_ENTRY_RUNAWAY_CANCEL_PIPS * PIP
    return None


def _attempt_missed_entry_cleanup(order_row: dict, snapshot: dict) -> tuple[str | None, str]:
    order_id = order_row["id"]

    if int(order_row.get("pending_cancelled") or 0) == 1:
        logger.info("order_id=%s: missed-entry cleanup skip: already marked cancelled.", order_id)
        return None, "already_marked_pending_cancelled"

    if int(order_row.get("be_moved") or 0) == 1:
        logger.info("order_id=%s: missed-entry cleanup skip: BE already moved.", order_id)
        return None, "be_already_moved"

    direction = snapshot.get("direction")
    if direction not in {"buy", "sell"}:
        logger.info("order_id=%s: missed-entry cleanup skip: invalid direction=%s", order_id, direction)
        return None, "invalid_direction"

    entry = snapshot.get("entry")
    if entry is None:
        logger.info("order_id=%s: missed-entry cleanup skip: missing entry.", order_id)
        return None, "missing_entry"

    ticket_states = snapshot.get("cleanup_ticket_states") or []
    unsafe_states = [state for state in ticket_states if not state.get("ok_to_cleanup")]
    if unsafe_states:
        logger.info(
            "order_id=%s: missed-entry cleanup skip: ticket state not safe. states=%s",
            order_id,
            _cleanup_state_log_payload(ticket_states),
        )
        return None, "cleanup_unsafe_active_or_history_fill"

    bid = snapshot.get("bid")
    ask = snapshot.get("ask")
    current_price = snapshot.get("current_price")

    if current_price is None or current_price <= 0:
        logger.info(
            "order_id=%s: missed-entry cleanup skip: market price unavailable. bid=%s ask=%s",
            order_id,
            bid,
            ask,
        )
        return None, "market_price_unavailable"

    distance_to_entry_pips = snapshot.get("distance_to_entry_pips")
    near_entry_seen = int(order_row.get("near_entry_seen") or 0) == 1
    runaway_threshold = snapshot.get("runaway_threshold_price")
    if distance_to_entry_pips <= NEAR_ENTRY_MAX_PIPS:
        mark_near_entry_seen(order_id, distance_to_entry_pips)
        near_entry_seen = True
        logger.info(
            "order_id=%s: missed-entry near-entry seen. direction=%s entry=%s current_price=%s distance_pips=%.2f runaway_threshold=%s near_entry_seen=%s previous_min_distance=%s states=%s",
            order_id,
            direction,
            entry,
            current_price,
            distance_to_entry_pips,
            runaway_threshold,
            near_entry_seen,
            order_row.get("min_distance_to_entry_pips"),
            _cleanup_state_log_payload(ticket_states),
        )

    runaway_reached = _runaway_tp1_reached(direction, float(current_price), entry)
    if not runaway_reached:
        logger.info(
            "order_id=%s: missed-entry cleanup skip: runaway threshold not reached. direction=%s entry=%s current_price_source=%s current_price=%s distance_pips=%.2f runaway_threshold=%s near_entry_seen=%s states=%s",
            order_id,
            direction,
            entry,
            snapshot.get("current_price_source"),
            current_price,
            distance_to_entry_pips,
            runaway_threshold,
            near_entry_seen,
            _cleanup_state_log_payload(ticket_states),
        )
        return None, "runaway_threshold_not_reached"

    if not near_entry_seen:
        logger.info(
            "order_id=%s: missed-entry cleanup skip: runaway reached but near-entry has not been seen. direction=%s entry=%s current_price_source=%s current_price=%s distance_pips=%.2f runaway_threshold=%s states=%s",
            order_id,
            direction,
            entry,
            snapshot.get("current_price_source"),
            current_price,
            distance_to_entry_pips,
            runaway_threshold,
            _cleanup_state_log_payload(ticket_states),
        )
        return None, "runaway_reached_but_near_entry_not_seen"

    logger.info(
        "order_id=%s: missed-entry runaway cleanup candidate: direction=%s entry=%s current_price_source=%s current_price=%s distance_pips=%.2f runaway_threshold=%s runaway_cancel_pips=%s near_entry_seen=%s states=%s",
        order_id,
        direction,
        entry,
        snapshot.get("current_price_source"),
        current_price,
        distance_to_entry_pips,
        runaway_threshold,
        MISS_ENTRY_RUNAWAY_CANCEL_PIPS,
        near_entry_seen,
        _cleanup_state_log_payload(ticket_states),
    )
    return MISS_ENTRY_RUNAWAY_CANCEL_REASON, "cleanup_candidate"


def _cancel_missed_entry_candidate(order_row: dict, reason: str | None = None) -> None:
    order_id = order_row.get("id")
    cancel_reason = reason or MISSED_ENTRY_CANCEL_REASON
    result = cancel_pending_order_group(order_row, reason=cancel_reason)

    for cancellation in result.get("cancellations", []):
        logger.info(
            "order_id=%s: missed-entry cancel result label=%s ticket=%s status=%s ok=%s retcode=%s error=%s",
            order_id,
            cancellation.get("label"),
            cancellation.get("db_ticket"),
            cancellation.get("status"),
            cancellation.get("ok"),
            cancellation.get("retcode"),
            cancellation.get("error"),
        )

    if result.get("ok") is True:
        mark_pending_cancelled(order_id, cancel_reason)
        if cancel_reason == MISS_ENTRY_RUNAWAY_CANCEL_REASON:
            logger.info(
                "order_id=%s: missed-entry runaway cleanup complete and DB marked cancelled.",
                order_id,
            )
        else:
            logger.info(
                "order_id=%s: missed-entry cleanup complete and DB marked cancelled.",
                order_id,
            )
        return

    logger.error(
        "order_id=%s: missed-entry cleanup cancellation failed; DB not marked cancelled. error=%s result=%s",
        order_id,
        result.get("error"),
        result,
    )


def _attempt_be_for_row(order_row: dict, snapshot: dict) -> tuple[str, str]:
    order_id = order_row["id"]
    direction = snapshot.get("direction")
    if direction not in {"buy", "sell"}:
        logger.info("order_id=%s: BE skip: invalid direction=%s", order_id, direction)
        return "safe skip", "invalid_direction"

    digits = _safe_symbol_digits()
    if digits is None:
        logger.error("order_id=%s: cannot read symbol digits.", order_id)
        return "safe skip", "symbol_digits_unavailable"

    bid = snapshot.get("bid")
    ask = snapshot.get("ask")

    if bid is None or ask is None or bid <= 0 or ask <= 0:
        logger.info("order_id=%s: market tick unavailable.", order_id)
        return "safe skip", "market_tick_unavailable"

    minimum_distance, _ = _get_symbol_trade_stops_level()

    def _too_close_to_market(be_sl: float) -> bool:
        if minimum_distance is None:
            return False
        if direction == "buy":
            return abs(bid - be_sl) < minimum_distance
        if direction == "sell":
            return abs(ask - be_sl) < minimum_distance
        return False

    unsafe_reasons = snapshot.get("unsafe_reasons") or []
    if unsafe_reasons:
        logger.info(
            "order_id=%s: BE skip: unsafe position mismatch. unsafe_reasons=%s",
            order_id,
            unsafe_reasons,
        )
        return "safe skip", "unsafe_position_mismatch"

    intended_updates = []
    layer_specs = (
        ("TP2", "entry_tp2", BE_COMMENT, True),
        ("TP3", "entry_tp3", BE_TP3_COMMENT, False),
    )
    for layer_name, entry_key, be_comment, include_tp in layer_specs:
        match = (snapshot.get("position_matches") or {}).get(layer_name) or {}
        position = match.get("position")
        if position is None:
            continue

        position_ticket = getattr(position, "ticket", None)
        position_open = getattr(position, "price_open", None)
        fallback_entry = snapshot.get(entry_key)
        sl_source = "position_open" if position_open is not None else entry_key
        be_source_price = position_open if position_open is not None else fallback_entry
        if position_ticket is None or be_source_price is None:
            logger.info(
                "order_id=%s: %s BE skip: position ticket or BE source missing. match=%s",
                order_id,
                layer_name,
                _position_match_log_payload({layer_name: match}),
            )
            return "safe skip", f"{layer_name.lower()}_position_incomplete"

        be_sl = _normalize_price(be_source_price, digits)
        if not _validate_be_sl(
            be_sl=be_sl,
            direction=direction,
            bid=bid,
            ask=ask,
            entry=float(fallback_entry) if fallback_entry is not None else be_sl,
        ):
            logger.info(
                "order_id=%s: Skip %s BE: BE SL invalid vs direction/market (be_sl=%s source=%s entry=%s bid=%s ask=%s)",
                order_id,
                layer_name,
                be_sl,
                sl_source,
                fallback_entry,
                bid,
                ask,
            )
            return "safe skip", f"{layer_name.lower()}_be_sl_invalid_vs_market"

        if _too_close_to_market(be_sl):
            logger.info(
                "order_id=%s: Skip %s BE: too close vs trade_stops_level. be_sl=%s bid=%s ask=%s minimum_distance=%s",
                order_id,
                layer_name,
                be_sl,
                bid,
                ask,
                minimum_distance,
            )
            return "safe skip", f"{layer_name.lower()}_be_sl_too_close_to_market"

        intended_updates.append(
            {
                "layer": layer_name,
                "position": position,
                "position_ticket": position_ticket,
                "position_id": match.get("position_id"),
                "match_method": match.get("match_method"),
                "be_sl": be_sl,
                "sl_source": sl_source,
                "comment": be_comment,
                "include_tp": include_tp,
            }
        )

    if not intended_updates:
        logger.info(
            "order_id=%s: BE skip: no active TP2/TP3 bot positions found. position_matches=%s",
            order_id,
            _position_match_log_payload(snapshot.get("position_matches") or {}),
        )
        return "safe skip", "no_active_tp2_tp3_positions"

    history_proof = _tp1_history_proof(snapshot)
    if history_proof.get("ok") is True:
        proof = history_proof
    else:
        if history_proof.get("reason") == "tp1_history_found_but_not_tp_or_positive_near_target":
            logger.info(
                "order_id=%s: BE skip: TP1 history found but did not prove TP/positive target close. history_proof=%s",
                order_id,
                history_proof,
            )
            return "safe skip", "tp1_history_not_tp_or_positive_target_close"

        fallback_proof = _tp1_safe_fallback_proof(snapshot)
        if fallback_proof.get("ok") is not True:
            logger.info(
                "order_id=%s: BE skip: TP1 not proven. history_proof=%s fallback_proof=%s",
                order_id,
                history_proof,
                fallback_proof,
            )
            return "safe skip", (
                "tp1_not_proven:"
                f"history={history_proof.get('reason')};"
                f"fallback={fallback_proof.get('reason')}"
            )
        proof = fallback_proof

    logger.info(
        "BE candidate: order_id=%s\n"
        "  Direction=%s\n"
        "  TP1 proof=%s\n"
        "  Intended updates=%s\n"
        "  entry=%s entry_tp1=%s entry_tp2=%s entry_tp3=%s bid=%s ask=%s minimum_distance=%s",
        order_id,
        direction,
        proof,
        [
            {
                "layer": update["layer"],
                "position_ticket": update["position_ticket"],
                "position_id": update["position_id"],
                "match_method": update["match_method"],
                "be_sl": update["be_sl"],
                "sl_source": update["sl_source"],
                "include_tp": update["include_tp"],
            }
            for update in intended_updates
        ],
        snapshot.get("entry"),
        snapshot.get("entry_tp1"),
        snapshot.get("entry_tp2"),
        snapshot.get("entry_tp3"),
        bid,
        ask,
        minimum_distance,
    )

    update_results = []
    for update in intended_updates:
        ok, pos_ticket, existing_tp = _send_be_for_position(
            order_id,
            update["layer"],
            update["position"],
            update["be_sl"],
            update["comment"],
            include_tp=update["include_tp"],
        )
        update_results.append(
            {
                "layer": update["layer"],
                "ok": ok,
                "position_ticket": pos_ticket,
                "new_sl": update["be_sl"],
                "existing_tp": existing_tp,
            }
        )

    if not all(result.get("ok") is True for result in update_results):
        logger.error(
            "BE update did not fully succeed; DB not marked be_moved. order_id=%s results=%s",
            order_id,
            update_results,
        )
        if any(result.get("ok") is True for result in update_results):
            return "BE candidate", "be_partial_update_failed_not_marked"
        return "BE candidate", "be_update_failed_not_marked"

    tp2_pos_ticket = next(
        (result.get("position_ticket") for result in update_results if result.get("layer") == "TP2"),
        None,
    )
    tp3_pos_ticket = next(
        (result.get("position_ticket") for result in update_results if result.get("layer") == "TP3"),
        None,
    )

    mark_be_moved(
        order_id,
        tp2_position_ticket=tp2_pos_ticket,
        tp3_position_ticket=tp3_pos_ticket,
    )

    logger.info(
        "Break Even applied: order_id=%s proof=%s tp1_ticket=%s tp2_position_ticket=%s tp3_position_ticket=%s entry_tp1=%s entry_tp2=%s entry_tp3=%s update_results=%s",
        order_id,
        proof,
        snapshot.get("ticket_tp1"),
        tp2_pos_ticket,
        tp3_pos_ticket,
        snapshot.get("entry_tp1"),
        snapshot.get("entry_tp2"),
        snapshot.get("entry_tp3"),
        update_results,
    )
    return "BE candidate", "be_applied"


def monitor_loop():
    init_db()
    logger.info("BE monitor berjalan. Interval=%s detik", MONITOR_INTERVAL)

    try:
        while True:
            cleanup_candidates = []
            try:
                with mt5_process_lock(timeout=30):
                    connect()
                    try:
                        orders = get_pending_orders()
                        pending_orders = _get_all_bot_pending_orders()
                        all_positions = _get_all_bot_positions()

                        for order_row in orders:
                            try:
                                snapshot = _build_order_state_snapshot(
                                    order_row,
                                    pending_orders,
                                    all_positions,
                                )
                                retired, retire_reason = _attempt_retire_inactive_mt5_row(
                                    order_row,
                                    snapshot,
                                )
                                if retired:
                                    _log_monitor_decision(
                                        snapshot,
                                        "safe skip",
                                        f"retired_inactive_row:{retire_reason}",
                                        extra={"retire_reason": retire_reason},
                                    )
                                    continue

                                cancel_reason, cleanup_status = _attempt_missed_entry_cleanup(
                                    order_row,
                                    snapshot,
                                )
                                if cancel_reason:
                                    _log_monitor_decision(
                                        snapshot,
                                        "cleanup candidate",
                                        cleanup_status,
                                        extra={"cancel_reason": cancel_reason},
                                    )
                                    cleanup_candidates.append((order_row, cancel_reason))
                                    continue

                                be_decision, be_reason = _attempt_be_for_row(
                                    order_row,
                                    snapshot,
                                )
                                _log_monitor_decision(
                                    snapshot,
                                    be_decision,
                                    be_reason,
                                    extra={"cleanup_status": cleanup_status},
                                )
                            except Exception:
                                logger.exception("BE monitor exception for order_id=%s", order_row.get("id"))
                    finally:
                        disconnect()

                for order_row, cancel_reason in cleanup_candidates:
                    try:
                        _cancel_missed_entry_candidate(order_row, cancel_reason)
                    except Exception:
                        logger.exception(
                            "Missed-entry cleanup exception for order_id=%s",
                            order_row.get("id"),
                        )
            except TimeoutError:
                logger.warning("BE monitor skipped cycle: MT5 lock timeout.")
            except Exception:
                logger.exception("BE monitor cycle failed.")

            time.sleep(MONITOR_INTERVAL)

    except KeyboardInterrupt:
        logger.info("BE monitor dihentikan oleh user.")


if __name__ == "__main__":
    monitor_loop()
