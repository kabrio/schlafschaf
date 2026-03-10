"""
Schlafschaf v2 – SQLite Datenbankmodul

Änderungen gegenüber v1:
  - Tabelle raw_data: sound-Spalte optional (NULL für neue Daten)
  - Tabelle sessions: bed_occupied, notes-Spalten
  - Neue Tabelle calibrations: Baseline-Rauschpegel des leeren Bettes
  - Migration: bestehende DBs werden automatisch aktualisiert
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
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()
        self._migrate()

    def _init_schema(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                start_time REAL NOT NULL,
                end_time REAL,
                created_at REAL NOT NULL,
                bed_occupied INTEGER NOT NULL DEFAULT 1,
                notes TEXT
            );

            CREATE TABLE IF NOT EXISTS raw_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                timestamp REAL NOT NULL,
                sound INTEGER,
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
                activity_count REAL,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            );

            CREATE TABLE IF NOT EXISTS calibrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                calibrated_at REAL NOT NULL,
                baseline_mean REAL NOT NULL,
                baseline_std REAL NOT NULL,
                presence_thresh REAL NOT NULL,
                activity_scale REAL NOT NULL,
                sample_rate REAL NOT NULL DEFAULT 50.0,
                notes TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_raw_session ON raw_data(session_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_epochs_session ON epochs(session_id, start_time);
        """)
        self.conn.commit()

    def _migrate(self):
        """Bestehende Datenbanken auf neue Schema-Version migrieren."""
        cur = self.conn.execute("PRAGMA table_info(sessions)")
        cols = {row['name'] for row in cur.fetchall()}
        if 'bed_occupied' not in cols:
            self.conn.execute(
                "ALTER TABLE sessions ADD COLUMN bed_occupied INTEGER NOT NULL DEFAULT 1"
            )
            self.conn.commit()
        if 'notes' not in cols:
            self.conn.execute("ALTER TABLE sessions ADD COLUMN notes TEXT")
            self.conn.commit()

        cur = self.conn.execute("PRAGMA table_info(epochs)")
        cols = {row['name'] for row in cur.fetchall()}
        if 'activity_count' not in cols:
            self.conn.execute("ALTER TABLE epochs ADD COLUMN activity_count REAL")
            self.conn.commit()

    # ── Sessions ─────────────────────────────────────────────────────────────

    def create_session(self, start_time: float | None = None) -> str:
        session_id = str(uuid.uuid4())
        now = time.time()
        t = start_time if start_time is not None else now
        self.conn.execute(
            "INSERT INTO sessions (id, start_time, created_at) VALUES (?, ?, ?)",
            (session_id, t, now)
        )
        self.conn.commit()
        return session_id

    def end_session(self, session_id: str, end_time: float | None = None):
        t = end_time if end_time is not None else time.time()
        self.conn.execute(
            "UPDATE sessions SET end_time = ? WHERE id = ?",
            (t, session_id)
        )
        self.conn.commit()

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

    def list_sessions(self) -> list:
        cur = self.conn.execute(
            "SELECT * FROM sessions ORDER BY start_time DESC"
        )
        return [dict(row) for row in cur.fetchall()]

    # ── Rohdaten ─────────────────────────────────────────────────────────────

    def insert_raw(self, session_id: str, timestamp: float,
                   ax: int, ay: int, az: int,
                   gx: int, gy: int, gz: int,
                   sound: int | None = None):
        self.conn.execute(
            """INSERT INTO raw_data
               (session_id, timestamp, sound, ax, ay, az, gx, gy, gz)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, timestamp, sound, ax, ay, az, gx, gy, gz)
        )

    def flush(self):
        self.conn.commit()

    def get_raw_for_session(self, session_id: str) -> list:
        cur = self.conn.execute(
            "SELECT * FROM raw_data WHERE session_id = ? ORDER BY timestamp",
            (session_id,)
        )
        return [dict(row) for row in cur.fetchall()]

    def count_raw_for_session(self, session_id: str) -> int:
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM raw_data WHERE session_id = ?", (session_id,)
        )
        return cur.fetchone()[0]

    # ── Epochen ──────────────────────────────────────────────────────────────

    def insert_epoch(self, session_id: str, start_time: float, end_time: float,
                     stage: str, movement_score: float, sound_score: float = 0.0,
                     activity_count: float = 0.0):
        self.conn.execute(
            """INSERT INTO epochs
               (session_id, start_time, end_time, stage,
                movement_score, sound_score, activity_count)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (session_id, start_time, end_time, stage,
             movement_score, sound_score, activity_count)
        )
        self.conn.commit()

    def delete_epochs_for_session(self, session_id: str):
        self.conn.execute("DELETE FROM epochs WHERE session_id = ?", (session_id,))
        self.conn.commit()

    def get_epochs_for_session(self, session_id: str) -> list:
        cur = self.conn.execute(
            "SELECT * FROM epochs WHERE session_id = ? ORDER BY start_time",
            (session_id,)
        )
        return [dict(row) for row in cur.fetchall()]

    # ── Kalibrierung ─────────────────────────────────────────────────────────

    def save_calibration(self, baseline_mean: float, baseline_std: float,
                         presence_thresh: float, activity_scale: float,
                         sample_rate: float = 50.0, notes: str = None):
        self.conn.execute(
            """INSERT INTO calibrations
               (calibrated_at, baseline_mean, baseline_std,
                presence_thresh, activity_scale, sample_rate, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (time.time(), baseline_mean, baseline_std,
             presence_thresh, activity_scale, sample_rate, notes)
        )
        self.conn.commit()

    def get_latest_calibration(self) -> dict | None:
        cur = self.conn.execute(
            "SELECT * FROM calibrations ORDER BY calibrated_at DESC LIMIT 1"
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def close(self):
        self.conn.commit()
        self.conn.close()
