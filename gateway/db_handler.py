"""SQLite backing store for the store-and-forward queue.

One table, telemetry_queue, holds every message the gateway receives from the
local broker. Rows start as 'pending' and are flipped to 'sent' once they have
been forwarded to the cloud broker. Pending rows are read back in insertion
order (FIFO) so the cloud sees the data in the same order it was produced.
"""

import sqlite3
import time


class DBHandler:
    def __init__(self, path="gateway.db"):
        # check_same_thread=False because paho runs callbacks on its own thread.
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._create_table()

    def _create_table(self):
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS telemetry_queue (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                topic     TEXT NOT NULL,
                payload   TEXT NOT NULL,
                status    TEXT NOT NULL DEFAULT 'pending',
                timestamp REAL NOT NULL
            )
            """
        )
        self.conn.commit()

    def enqueue(self, topic, payload, status="pending"):
        """Insert a new message. Returns the row id."""
        cur = self.conn.execute(
            "INSERT INTO telemetry_queue (topic, payload, status, timestamp) "
            "VALUES (?, ?, ?, ?)",
            (topic, payload, status, time.time()),
        )
        self.conn.commit()
        return cur.lastrowid

    def fetch_pending(self, limit=100):
        """Return pending rows oldest-first, up to limit."""
        cur = self.conn.execute(
            "SELECT id, topic, payload FROM telemetry_queue "
            "WHERE status = 'pending' ORDER BY id ASC LIMIT ?",
            (limit,),
        )
        return cur.fetchall()

    def mark_sent(self, row_id):
        self.conn.execute(
            "UPDATE telemetry_queue SET status = 'sent' WHERE id = ?",
            (row_id,),
        )
        self.conn.commit()

    def pending_count(self):
        cur = self.conn.execute(
            "SELECT COUNT(*) AS n FROM telemetry_queue WHERE status = 'pending'"
        )
        return cur.fetchone()["n"]

    def close(self):
        self.conn.close()
