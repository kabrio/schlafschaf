"""
Schlafschaf – SQLite Datenbankmodul
Läuft auf dem Arduino Uno Q (Debian Linux)
"""

import sqlite3
import time
import uuid
import os

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'schlafschaf.db')


class Database:
    def __init__(self, path: str = DB_PATH):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                start_time REAL NOT NULL,
                end_time REAL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS raw_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                timestamp REAL NOT NULL,
                sound INTEGER NOT NULL,
                ax INTEGER,
                ay INTEGER,
                az INTEGER,
                gx INTEGER,
                gy INTEGER,
                gz INTEGER,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            );

            CREATE TABLE IF NOT EXISTS epochs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                start_time REAL NOT NULL,
                end_time REAL NOT NULL,
                stage TEXT NOT NULL,
                movement_score REAL,
                sound_score REAL,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            );

            CREATE INDEX IF NOT EXISTS idx_raw_session ON raw_data(session_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_epochs_session ON epochs(session_id, start_time);
        """)
        self.conn.commit()

    def create_session(self) -> str:
        session_id = str(uuid.uuid4())
        now = time.time()
        self.conn.execute(
            "INSERT INTO sessions (id, start_time, created_at) VALUES (?, ?, ?)",
            (session_id, now, now)
        )
        self.conn.commit()
        return session_id

    def end_session(self, session_id: str):
        self.conn.execute(
            "UPDATE sessions SET end_time = ? WHERE id = ?",
            (time.time(), session_id)
        )
        self.conn.commit()

    def insert_raw(self, session_id: str, timestamp: float, sound: int,
                   ax: int, ay: int, az: int, gx: int, gy: int, gz: int):
        self.conn.execute(
            """INSERT INTO raw_data
               (session_id, timestamp, sound, ax, ay, az, gx, gy, gz)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, timestamp, sound, ax, ay, az, gx, gy, gz)
        )
        # Commit every 10 rows für Performance
        if self.conn.in_transaction:
            pass  # Batched commit via explicit flush

    def flush(self):
        self.conn.commit()

    def insert_epoch(self, session_id: str, start_time: float, end_time: float,
                     stage: str, movement_score: float, sound_score: float):
        self.conn.execute(
            """INSERT INTO epochs
               (session_id, start_time, end_time, stage, movement_score, sound_score)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (session_id, start_time, end_time, stage, movement_score, sound_score)
        )
        self.conn.commit()

    def delete_epochs_for_session(self, session_id: str):
        self.conn.execute("DELETE FROM epochs WHERE session_id = ?", (session_id,))
        self.conn.commit()

    def get_raw_for_session(self, session_id: str) -> list:
        cur = self.conn.execute(
            "SELECT * FROM raw_data WHERE session_id = ? ORDER BY timestamp",
            (session_id,)
        )
        return [dict(row) for row in cur.fetchall()]

    def get_epochs_for_session(self, session_id: str) -> list:
        cur = self.conn.execute(
            "SELECT * FROM epochs WHERE session_id = ? ORDER BY start_time",
            (session_id,)
        )
        return [dict(row) for row in cur.fetchall()]

    def list_sessions(self) -> list:
        cur = self.conn.execute(
            "SELECT * FROM sessions ORDER BY start_time DESC"
        )
        return [dict(row) for row in cur.fetchall()]

    def get_session(self, session_id: str) -> dict | None:
        cur = self.conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def get_latest_session(self) -> dict | None:
        cur = self.conn.execute(
            "SELECT * FROM sessions ORDER BY start_time DESC LIMIT 1"
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def close(self):
        self.conn.commit()
        self.conn.close()
