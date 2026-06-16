"""
SQLite state helper for the XAUUSD Telegram/MT5 bot.

Both telegram_listener.py and be_monitor.py use this file to share active
order state through active_orders.db while running as separate processes.
"""

import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timezone


logger = logging.getLogger(__name__)

DB_PATH = "active_orders.db"


def _ensure_entry_columns(conn):
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(active_orders)").fetchall()
    }

    if "entry_tp1" not in columns:
        conn.execute("ALTER TABLE active_orders ADD COLUMN entry_tp1 REAL")

    if "entry_tp2" not in columns:
        conn.execute("ALTER TABLE active_orders ADD COLUMN entry_tp2 REAL")

    if "tp1_closed" not in columns:
        conn.execute(
            "ALTER TABLE active_orders ADD COLUMN tp1_closed INTEGER NOT NULL DEFAULT 0"
        )

    if "tp1_closed_by_tp" not in columns:
        conn.execute(
            "ALTER TABLE active_orders ADD COLUMN tp1_closed_by_tp INTEGER NOT NULL DEFAULT 0"
        )

    if "tp1_profit_positive" not in columns:
        conn.execute(
            "ALTER TABLE active_orders ADD COLUMN tp1_profit_positive INTEGER NOT NULL DEFAULT 0"
        )

    if "tp2_position_ticket" not in columns:
        conn.execute("ALTER TABLE active_orders ADD COLUMN tp2_position_ticket INTEGER")

    if "ticket_tp3" not in columns:
        conn.execute("ALTER TABLE active_orders ADD COLUMN ticket_tp3 INTEGER")

    if "entry_tp3" not in columns:
        conn.execute("ALTER TABLE active_orders ADD COLUMN entry_tp3 REAL")

    if "tp3_position_ticket" not in columns:
        conn.execute("ALTER TABLE active_orders ADD COLUMN tp3_position_ticket INTEGER")

    if "near_entry_seen" not in columns:
        conn.execute(
            "ALTER TABLE active_orders ADD COLUMN near_entry_seen INTEGER DEFAULT 0"
        )

    if "min_distance_to_entry_pips" not in columns:
        conn.execute(
            "ALTER TABLE active_orders ADD COLUMN min_distance_to_entry_pips REAL"
        )

    if "pending_cancelled" not in columns:
        conn.execute(
            "ALTER TABLE active_orders ADD COLUMN pending_cancelled INTEGER DEFAULT 0"
        )

    if "cancel_reason" not in columns:
        conn.execute("ALTER TABLE active_orders ADD COLUMN cancel_reason TEXT")

    if "cancelled_at" not in columns:
        conn.execute("ALTER TABLE active_orders ADD COLUMN cancelled_at TEXT")


def init_db():
    """Create the active_orders table if it does not already exist."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS active_orders (
                  id          INTEGER PRIMARY KEY AUTOINCREMENT,
                  ticket_tp1  INTEGER NOT NULL,
                  ticket_tp2  INTEGER NOT NULL,
                  direction   TEXT NOT NULL,
                  entry       REAL NOT NULL,
                  be_moved    INTEGER NOT NULL DEFAULT 0,
                  tp1_closed INTEGER NOT NULL DEFAULT 0,
                  tp1_closed_by_tp INTEGER NOT NULL DEFAULT 0,
                  tp1_profit_positive INTEGER NOT NULL DEFAULT 0,
                  tp2_position_ticket INTEGER,
                  ticket_tp3 INTEGER,
                  entry_tp3 REAL,
                  tp3_position_ticket INTEGER,
                  near_entry_seen INTEGER DEFAULT 0,
                  min_distance_to_entry_pips REAL,
                  pending_cancelled INTEGER DEFAULT 0,
                  cancel_reason TEXT,
                  cancelled_at TEXT
                )

                """
            )
            _ensure_entry_columns(conn)

    logger.info("Database initialized: %s", DB_PATH)


def insert_order(
    ticket_tp1,
    ticket_tp2,
    direction,
    entry_first,
    entry_second,
    ticket_tp3=None,
    entry_tp3=None,
):
    """Save a new active order group to SQLite."""
    entry_third = entry_tp3 if entry_tp3 is not None else entry_second
    with closing(sqlite3.connect(DB_PATH)) as conn:
        with conn:
            _ensure_entry_columns(conn)
            conn.execute(
                """
                INSERT INTO active_orders (
                    ticket_tp1,
                    ticket_tp2,
                    direction,
                    entry,
                    entry_tp1,
                    entry_tp2,
                    ticket_tp3,
                    entry_tp3
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticket_tp1,
                    ticket_tp2,
                    direction,
                    entry_first,
                    entry_first,
                    entry_second,
                    ticket_tp3,
                    entry_third,
                ),
            )

    logger.info("Order saved to DB: tp1=%s tp2=%s tp3=%s", ticket_tp1, ticket_tp2, ticket_tp3)


def get_pending_orders() -> list[dict]:
    """Return active orders that have not yet had TP2 moved to breakeven."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        with conn:
            _ensure_entry_columns(conn)
            rows = conn.execute(
                """
                SELECT id,
                       ticket_tp1,
                       ticket_tp2,
                       direction,
                       entry,
                       COALESCE(entry_tp1, entry) AS entry_tp1,
                       COALESCE(entry_tp2, entry) AS entry_tp2,
                       ticket_tp3,
                       COALESCE(entry_tp3, entry) AS entry_tp3,
                       be_moved,
                       tp1_closed,
                       tp1_closed_by_tp,
                       tp1_profit_positive,
                       tp2_position_ticket,
                       tp3_position_ticket,
                       COALESCE(near_entry_seen, 0) AS near_entry_seen,
                       min_distance_to_entry_pips,
                       COALESCE(pending_cancelled, 0) AS pending_cancelled,
                       cancel_reason,
                       cancelled_at
                FROM active_orders

                WHERE be_moved = 0
                  AND COALESCE(pending_cancelled, 0) = 0
                ORDER BY id
                """
            ).fetchall()

    logger.debug("Fetched %s pending orders from DB.", len(rows))
    return [dict(row) for row in rows]


def get_latest_active_order() -> dict | None:
    """Return the newest active order group that has not moved to BE yet."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        with conn:
            _ensure_entry_columns(conn)
            row = conn.execute(
                """
                SELECT id,
                       ticket_tp1,
                       ticket_tp2,
                       direction,
                       entry,
                       COALESCE(entry_tp1, entry) AS entry_tp1,
                       COALESCE(entry_tp2, entry) AS entry_tp2,
                       ticket_tp3,
                       COALESCE(entry_tp3, entry) AS entry_tp3,
                       be_moved,
                       tp1_closed,
                       tp1_closed_by_tp,
                       tp1_profit_positive,
                       tp2_position_ticket,
                       tp3_position_ticket,
                       COALESCE(near_entry_seen, 0) AS near_entry_seen,
                       min_distance_to_entry_pips,
                       COALESCE(pending_cancelled, 0) AS pending_cancelled,
                       cancel_reason,
                       cancelled_at
                FROM active_orders
                WHERE be_moved = 0
                  AND COALESCE(pending_cancelled, 0) = 0
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()

    if row is None:
        logger.info("No latest active order found for SL update.")
        return None

    return dict(row)


def mark_be_moved(order_id, tp2_position_ticket=None, tp3_position_ticket=None):
    """Mark an order row as already moved to breakeven."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        with conn:
            _ensure_entry_columns(conn)
            if tp2_position_ticket is None and tp3_position_ticket is None:
                conn.execute(
                    "UPDATE active_orders SET be_moved = 1 WHERE id = ?",
                    (order_id,),
                )
            else:
                conn.execute(
                    """
                    UPDATE active_orders
                    SET be_moved = 1,
                        tp2_position_ticket = COALESCE(tp2_position_ticket, ?),
                        tp3_position_ticket = COALESCE(tp3_position_ticket, ?)
                    WHERE id = ?
                    """,
                    (tp2_position_ticket, tp3_position_ticket, order_id),
                )

    logger.info("Order id=%s marked as BE moved.", order_id)


def mark_near_entry_seen(order_id, distance_pips):
    """Mark that price came near entry and keep the closest observed distance."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        with conn:
            _ensure_entry_columns(conn)
            conn.execute(
                """
                UPDATE active_orders
                SET near_entry_seen = 1,
                    min_distance_to_entry_pips =
                        CASE
                            WHEN min_distance_to_entry_pips IS NULL THEN ?
                            WHEN ? < min_distance_to_entry_pips THEN ?
                            ELSE min_distance_to_entry_pips
                        END
                WHERE id = ?
                """,
                (distance_pips, distance_pips, distance_pips, order_id),
            )

    logger.info(
        "Order id=%s marked near-entry seen. distance_pips=%.2f",
        order_id,
        distance_pips,
    )


def mark_pending_cancelled(order_id, reason):
    """Mark an order group as cancelled because pending setup is no longer valid."""
    cancelled_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with closing(sqlite3.connect(DB_PATH)) as conn:
        with conn:
            _ensure_entry_columns(conn)
            conn.execute(
                """
                UPDATE active_orders
                SET pending_cancelled = 1,
                    cancel_reason = ?,
                    cancelled_at = ?
                WHERE id = ?
                """,
                (reason, cancelled_at, order_id),
            )

    logger.info(
        "Order id=%s marked pending-cancelled. reason=%s cancelled_at=%s",
        order_id,
        reason,
        cancelled_at,
    )


def mark_tp1_status(order_id, closed: bool, closed_by_tp: bool, profit_positive: bool):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        with conn:
            _ensure_entry_columns(conn)
            conn.execute(
                """
                UPDATE active_orders
                SET tp1_closed = ?,
                    tp1_closed_by_tp = ?,
                    tp1_profit_positive = ?
                WHERE id = ?
                """,
                (1 if closed else 0, 1 if closed_by_tp else 0, 1 if profit_positive else 0, order_id),
            )

    logger.info(
        "Order id=%s TP1 status updated: closed=%s closed_by_tp=%s profit_positive=%s",
        order_id,
        closed,
        closed_by_tp,
        profit_positive,
    )
