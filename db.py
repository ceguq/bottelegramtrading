"""
SQLite state helper for the XAUUSD Telegram/MT5 bot.

Both telegram_listener.py and be_monitor.py use this file to share active
order state through active_orders.db while running as separate processes.
"""

import logging
import sqlite3
from contextlib import closing


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
                  tp2_position_ticket INTEGER
                )

                """
            )
            _ensure_entry_columns(conn)

    logger.info("Database initialized: %s", DB_PATH)


def insert_order(ticket_tp1, ticket_tp2, direction, entry_first, entry_second):
    """Save a new active order pair to SQLite."""
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
                    entry_tp2
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    ticket_tp1,
                    ticket_tp2,
                    direction,
                    entry_first,
                    entry_first,
                    entry_second,
                ),
            )

    logger.info("Order saved to DB: tp1=%s tp2=%s", ticket_tp1, ticket_tp2)


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
                       tp1_closed,
                       tp1_closed_by_tp,
                       tp1_profit_positive,
                       tp2_position_ticket
                FROM active_orders

                WHERE be_moved = 0
                ORDER BY id
                """
            ).fetchall()

    logger.debug("Fetched %s pending orders from DB.", len(rows))
    return [dict(row) for row in rows]


def mark_be_moved(order_id, tp2_position_ticket=None):
    """Mark an order row as already moved to breakeven."""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        with conn:
            if tp2_position_ticket is None:
                conn.execute(
                    "UPDATE active_orders SET be_moved = 1 WHERE id = ?",
                    (order_id,),
                )
            else:
                conn.execute(
                    """
                    UPDATE active_orders
                    SET be_moved = 1,
                        tp2_position_ticket = COALESCE(tp2_position_ticket, ?)
                    WHERE id = ?
                    """,
                    (tp2_position_ticket, order_id),
                )

    logger.info("Order id=%s marked as BE moved.", order_id)


def mark_tp1_status(order_id, closed: bool, closed_by_tp: bool, profit_positive: bool):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        with conn:
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
