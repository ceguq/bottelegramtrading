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
MT5_LOGIN = 371863329  # Your MT5 account login number from your broker
MT5_PASSWORD = "sw34LOG2311@"  # Your MT5 account password
MT5_SERVER = "ValetaxIntl-Live2"  # Your MT5 broker server name exactly as shown in MT5

MONITOR_INTERVAL = 5

TP1_COMMENT = "TG-TP1"
TP2_COMMENT = "TG-TP2"
TP3_COMMENT = "TG-NO-TP"
BE_COMMENT = "TG-TP2-BE"
BE_TP3_COMMENT = "TG-NO-TP-BE"
NEAR_ENTRY_MIN_PIPS = 5
NEAR_ENTRY_MAX_PIPS = 10
MISS_ENTRY_TP1_PIPS = TP1_PIPS
MISSED_ENTRY_CANCEL_REASON = "missed_entry_reached_tp1_after_near_entry"


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


def _get_positions_by_magic_comment() -> Tuple[list, list, list]:
    """Active positions for this bot (best-effort)."""
    pos_tp1 = []
    pos_tp2 = []
    pos_tp3 = []
    positions = mt5.positions_get(symbol=SYMBOL)
    if not positions:
        return pos_tp1, pos_tp2, pos_tp3

    for p in positions:
        if getattr(p, "magic", None) != MAGIC:
            continue
        comment = getattr(p, "comment", "")
        if comment == TP1_COMMENT:
            pos_tp1.append(p)
        elif comment == TP2_COMMENT:
            pos_tp2.append(p)
        elif comment == TP3_COMMENT:
            pos_tp3.append(p)

    return pos_tp1, pos_tp2, pos_tp3


def _position_ids_from_history_order(pending_ticket) -> set[int]:
    ticket_int = _to_int_or_none(pending_ticket)
    if ticket_int is None:
        return set()

    position_ids = set()
    orders = mt5.history_orders_get(ticket=ticket_int)
    if not orders:
        return position_ids

    for order in orders:
        for field_name in ("position_id", "position"):
            position_id = _to_int_or_none(getattr(order, field_name, None))
            if position_id not in (None, 0):
                position_ids.add(position_id)

    return position_ids


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


def _get_cleanup_ticket_state(label: str, ticket, all_positions: list) -> dict:
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

    orders = mt5.orders_get(ticket=ticket_int)
    if orders is None:
        state["status"] = "orders_lookup_failed"
        state["ok_to_cleanup"] = False
        return state

    pending_order = None
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
    # BUY: BE SL may equal entry, but must stay below current Bid.
    if direction.lower() == "buy":
        if be_sl > entry:
            return False
        if be_sl >= bid:
            return False
        return True

    # SELL: BE SL may equal entry, but must stay above current Ask.
    if direction.lower() == "sell":
        if be_sl < entry:
            return False
        if be_sl <= ask:
            return False
        return True

    return False


def _order_check_and_send_sl_tp(request: dict) -> bool:
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
    if retcode != 0:
        logger.error(
            "order_check BE SLTP failed: retcode=%s comment=%s last_error=%s request=%s",
            retcode,
            comment,
            mt5.last_error(),
            request,
        )
        return False

    result = mt5.order_send(request)
    if result is None:
        logger.error(
            "order_send BE SLTP failed: result None last_error=%s request=%s",
            mt5.last_error(),
            request,
        )
        return False

    result_retcode = getattr(result, "retcode", None)
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

    ok = _order_check_and_send_sl_tp(request)
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


def _runaway_tp1_reached(direction: str, current_price: float, entry: float) -> bool:
    if direction == "buy":
        return current_price >= entry + MISS_ENTRY_TP1_PIPS * PIP
    if direction == "sell":
        return current_price <= entry - MISS_ENTRY_TP1_PIPS * PIP
    return False


def _attempt_missed_entry_cleanup(order_row: dict, all_positions: list) -> bool:
    order_id = order_row["id"]

    if int(order_row.get("pending_cancelled") or 0) == 1:
        logger.info("order_id=%s: missed-entry cleanup skip: already marked cancelled.", order_id)
        return False

    if int(order_row.get("be_moved") or 0) == 1:
        logger.info("order_id=%s: missed-entry cleanup skip: BE already moved.", order_id)
        return False

    direction = str(order_row.get("direction", "")).lower()
    if direction not in {"buy", "sell"}:
        logger.info("order_id=%s: missed-entry cleanup skip: invalid direction=%s", order_id, direction)
        return False

    legacy_entry = order_row.get("entry")
    entry = float(order_row.get("entry_tp1") if order_row.get("entry_tp1") is not None else legacy_entry)

    ticket_states = [
        _get_cleanup_ticket_state(TP1_COMMENT, order_row.get("ticket_tp1"), all_positions),
        _get_cleanup_ticket_state(TP2_COMMENT, order_row.get("ticket_tp2"), all_positions),
        _get_cleanup_ticket_state(TP3_COMMENT, order_row.get("ticket_tp3"), all_positions),
    ]
    unsafe_states = [state for state in ticket_states if not state.get("ok_to_cleanup")]
    if unsafe_states:
        logger.info(
            "order_id=%s: missed-entry cleanup skip: ticket state not safe. states=%s",
            order_id,
            _cleanup_state_log_payload(ticket_states),
        )
        return False

    tick = mt5.symbol_info_tick(SYMBOL)
    bid = getattr(tick, "bid", None) if tick else None
    ask = getattr(tick, "ask", None) if tick else None
    current_price = ask if direction == "buy" else bid

    if current_price is None or current_price <= 0:
        logger.info(
            "order_id=%s: missed-entry cleanup skip: market price unavailable. bid=%s ask=%s",
            order_id,
            bid,
            ask,
        )
        return False

    distance_to_entry_pips = abs(float(current_price) - entry) / PIP
    if NEAR_ENTRY_MIN_PIPS <= distance_to_entry_pips <= NEAR_ENTRY_MAX_PIPS:
        mark_near_entry_seen(order_id, distance_to_entry_pips)
        logger.info(
            "order_id=%s: missed-entry near-entry seen. direction=%s entry=%s current_price=%s distance_pips=%.2f previous_min_distance=%s states=%s",
            order_id,
            direction,
            entry,
            current_price,
            distance_to_entry_pips,
            order_row.get("min_distance_to_entry_pips"),
            _cleanup_state_log_payload(ticket_states),
        )

    near_entry_seen = int(order_row.get("near_entry_seen") or 0) == 1
    runaway_reached = _runaway_tp1_reached(direction, float(current_price), entry)
    if not near_entry_seen:
        if runaway_reached:
            logger.info(
                "order_id=%s: missed-entry cleanup not armed: TP1 runaway reached before near-entry was seen. direction=%s entry=%s current_price=%s",
                order_id,
                direction,
                entry,
                current_price,
            )
        return False

    if not runaway_reached:
        return False

    logger.info(
        "order_id=%s: missed-entry cleanup candidate: near_entry_seen=1 direction=%s entry=%s current_price=%s tp1_runaway_pips=%s states=%s",
        order_id,
        direction,
        entry,
        current_price,
        MISS_ENTRY_TP1_PIPS,
        _cleanup_state_log_payload(ticket_states),
    )
    return True


def _cancel_missed_entry_candidate(order_row: dict) -> None:
    order_id = order_row.get("id")
    result = cancel_pending_order_group(order_row, reason=MISSED_ENTRY_CANCEL_REASON)

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
        mark_pending_cancelled(order_id, MISSED_ENTRY_CANCEL_REASON)
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


def _attempt_be_for_row(
    order_row: dict,
    tp1_position_list: list,
    tp2_position_list: list,
    tp3_position_list: list,
) -> None:
    order_id = order_row["id"]
    direction = order_row["direction"].lower()
    legacy_entry = order_row.get("entry")
    entry_tp1 = float(order_row.get("entry_tp1") if order_row.get("entry_tp1") is not None else legacy_entry)
    entry_tp2 = float(order_row.get("entry_tp2") if order_row.get("entry_tp2") is not None else legacy_entry)
    entry_tp3 = float(order_row.get("entry_tp3") if order_row.get("entry_tp3") is not None else legacy_entry)
    ticket_tp1 = order_row["ticket_tp1"]
    ticket_tp2 = order_row["ticket_tp2"]
    ticket_tp3 = order_row.get("ticket_tp3")

    # 1) TP1 must have already been triggered into a position AND closed by TP.
    tp1_position_id = _get_position_id_from_pending_ticket(ticket_tp1)
    if tp1_position_id is None:
        logger.info("order_id=%s: TP1 masih pending atau belum menjadi posisi.", order_id)
        return

    if not _tp1_closed_by_take_profit_profit_positive(tp1_position_id):
        logger.info(
            "order_id=%s: TP1 belum terbukti closed by Take Profit with positive profit. tp1_position_id=%s",
            order_id,
            tp1_position_id,
        )
        return

    # TP1 is proven closed by TP + positive profit.

    # 2) Find TP2 position corresponding to ticket_tp2.
    tp2_position_id, tp2_position = _find_position_for_pending_ticket(
        order_id,
        "TP2",
        ticket_tp2,
        tp2_position_list,
    )
    if tp2_position is None:
        return

    tp2_pos_ticket = getattr(tp2_position, "ticket", None)
    tp2_open = getattr(tp2_position, "price_open", None)
    if tp2_pos_ticket is None or tp2_open is None:
        logger.info("order_id=%s: TP2 position incomplete (ticket/price_open missing).", order_id)
        return

    digits = _safe_symbol_digits()
    if digits is None:
        logger.error("order_id=%s: cannot read symbol digits.", order_id)
        return

    tp2_be_sl = _normalize_price(tp2_open, digits)

    tick = mt5.symbol_info_tick(SYMBOL)
    bid = getattr(tick, "bid", None) if tick else None
    ask = getattr(tick, "ask", None) if tick else None

    if bid is None or ask is None:
        logger.info("order_id=%s: market tick unavailable.", order_id)
        return

    if not _validate_be_sl(be_sl=tp2_be_sl, direction=direction, bid=bid, ask=ask, entry=entry_tp2):
        logger.info(
            "order_id=%s: Skip TP2 BE: BE SL invalid vs direction/market (be_sl=%s entry_tp2=%s bid=%s ask=%s)",
            order_id,
            tp2_be_sl,
            entry_tp2,
            bid,
            ask,
        )
        return

    minimum_distance, _ = _get_symbol_trade_stops_level()
    def _too_close_to_market(be_sl: float) -> bool:
        if minimum_distance is None:
            return False
        if direction == "buy":
            return abs(bid - be_sl) < minimum_distance
        if direction == "sell":
            return abs(ask - be_sl) < minimum_distance
        return False

    if _too_close_to_market(tp2_be_sl):
        logger.info("order_id=%s: Skip TP2 BE: too close vs trade_stops_level.", order_id)
        return

    tp3_position_id = None
    tp3_position = None
    tp3_pos_ticket = None
    tp3_be_sl = None
    if ticket_tp3 is not None:
        tp3_position_id, tp3_position = _find_position_for_pending_ticket(
            order_id,
            "TP3",
            ticket_tp3,
            tp3_position_list,
        )
        if tp3_position is None:
            return

        tp3_be_sl = _normalize_price(entry_tp3, digits)
        if not _validate_be_sl(be_sl=tp3_be_sl, direction=direction, bid=bid, ask=ask, entry=entry_tp3):
            logger.info(
                "order_id=%s: Skip TP3 BE: BE SL invalid vs direction/market (be_sl=%s entry_tp3=%s bid=%s ask=%s)",
                order_id,
                tp3_be_sl,
                entry_tp3,
                bid,
                ask,
            )
            return

        if _too_close_to_market(tp3_be_sl):
            logger.info("order_id=%s: Skip TP3 BE: too close vs trade_stops_level.", order_id)
            return

    logger.info(
        "Attempt BE: order_id=%s\n"
        "  Direction=%s\n"
        "  TP1 ticket(pending)=%s TP1 pos_id=%s (TP closed proof OK)\n"
        "  TP2 ticket(pending)=%s TP2 pos_id=%s\n"
        "  TP3 ticket(pending)=%s TP3 pos_id=%s\n"
        "  TP2 position ticket=%s\n"
        "  entry_tp1=%s entry_tp2=%s entry_tp3=%s tp2_open=%s tp2_be_sl=%s tp3_be_sl=%s",
        order_id,
        direction,
        ticket_tp1,
        tp1_position_id,
        ticket_tp2,
        tp2_position_id,
        ticket_tp3,
        tp3_position_id,
        tp2_pos_ticket,
        entry_tp1,
        entry_tp2,
        entry_tp3,
        tp2_open,
        tp2_be_sl,
        tp3_be_sl,
    )

    ok, tp2_pos_ticket, existing_tp = _send_be_for_position(
        order_id,
        "TP2",
        tp2_position,
        tp2_be_sl,
        BE_COMMENT,
        include_tp=True,
    )
    if not ok:
        logger.info("TP2 BE SLTP failed: order_id=%s", order_id)
        return

    if ticket_tp3 is not None:
        ok, tp3_pos_ticket, _ = _send_be_for_position(
            order_id,
            "TP3",
            tp3_position,
            tp3_be_sl,
            BE_TP3_COMMENT,
            include_tp=False,
        )
        if not ok:
            logger.info("TP3 BE SLTP failed: order_id=%s", order_id)
            return

    mark_be_moved(
        order_id,
        tp2_position_ticket=tp2_pos_ticket,
        tp3_position_ticket=tp3_pos_ticket,
    )

    logger.info(
        "Break Even applied: order_id=%s tp1_ticket=%s tp2_position_ticket=%s tp3_position_ticket=%s entry_tp1=%s entry_tp2=%s entry_tp3=%s tp2_open=%s tp2_new_sl=%s tp3_new_sl=%s tp2_existing_tp=%s",
        order_id,
        ticket_tp1,
        tp2_pos_ticket,
        tp3_pos_ticket,
        entry_tp1,
        entry_tp2,
        entry_tp3,
        tp2_open,
        tp2_be_sl,
        tp3_be_sl,
        existing_tp,
    )


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

                        tp1_positions, tp2_positions, tp3_positions = _get_positions_by_magic_comment()
                        all_positions = tp1_positions + tp2_positions + tp3_positions

                        # Only consider bot-tagged positions; pairing is still anchored by DB tickets.
                        for order_row in orders:
                            try:
                                if _attempt_missed_entry_cleanup(order_row, all_positions):
                                    cleanup_candidates.append(order_row)
                                    continue

                                if not tp1_positions:
                                    logger.info(
                                        "order_id=%s: TP1 masih pending (no active TP1 positions).",
                                        order_row["id"],
                                    )
                                _attempt_be_for_row(
                                    order_row,
                                    tp1_positions,
                                    tp2_positions,
                                    tp3_positions,
                                )
                            except Exception:
                                logger.exception("BE monitor exception for order_id=%s", order_row.get("id"))
                    finally:
                        disconnect()

                for order_row in cleanup_candidates:
                    try:
                        _cancel_missed_entry_candidate(order_row)
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
