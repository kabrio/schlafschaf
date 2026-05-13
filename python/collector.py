"""
Schlafschaf v3 – Serial-Daten-Collector

GRUNDKONZEPT (Sensor im Bett):
  Der Modulino Movement (LSM6DSOX) liegt irgendwo im Bett und misst
  Körperbewegungen, die durch die Matratze übertragen werden.

  Was der Sensor misst: Bewegungs-Intensität und -Muster (Aktigraphie)
  Was er NICHT messen kann: Atemfrequenz oder Herzschlag
  (Signal zu schwach bei indirekter Messung durch die Matratze)

SCHLAFPHASEN via Bewegungsmuster:
  Einschlafen:    Bewegungsintensität nimmt über ~15–30 min ab
  Leichtschlaf:   Gelegentliche Repositionierungen (alle 5–20 min)
  Tiefschlaf:     Sehr wenig Bewegung, lange Ruhephasen (20–45 min)
  REM:            Etwas mehr Bewegung als Tiefschlaf
  Aufwachen:      Zunehmende Bewegungsfrequenz und -intensität

TIEFSCHLAF vs. LEERES BETT (das Kernproblem):
  Problem:  Tiefschlaf und leeres Bett sehen im Rohdaten-Stream identisch
            aus – beide haben sehr niedrige Varianz.
  Lösung:   Unterscheidung durch KONTEXT und EXIT-SIGNAL:
  1. Kontext: Einmal SESSION gestartet = Person ist im Bett.
              Ruhige Phase = Tiefschlaf, nicht „Bett leer".
  2. Exit-Signal: Bett verlassen hat eine typische Signatur:
              Bewegungspuls (Aufstehen/Hinaussteigen) gefolgt von
              anhaltender Stille. Tiefschlaf hat diesen Puls nicht!

VERLASSEN-ERKENNUNG (zweistufig):
  MIT Puls:   Bewegungspuls (>2.5× Aktivitäts-Schwelle) erkannt,
              danach Stille für 3–10 min (Tag/Nacht) → Session endet
  OHNE Puls:  Sehr lange Stille als Fallback (z.B. leise rausgegangen):
              10 min (Tag) / 45 min (Nacht)

BETRIEBSMODI:
  MAINS   (Netzbetrieb):  Immer aktiv, volle Funktionen
  BATTERY (Akku-Betrieb): Nur im Schlaffenster aktiv (Standard: 23:00–09:00)
                          Arduino pausiert außerhalb → tagelanger Akkubetrieb
                          Verwendung: --mode battery [--sleep-start 23 --sleep-end 9]

Verwendung:
  python3 collector.py [--port /var/run/arduino-router.sock] [--mode mains|battery]
                       [--sleep-start 23] [--sleep-end 9]
"""

import argparse
import math
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from enum import Enum, auto

import numpy as np
import serial
import serial.tools.list_ports

from database import Database
from analyzer import analyze_session


# ── Betriebsmodi ──────────────────────────────────────────────────────────────

class OperatingMode(Enum):
    MAINS   = 'mains'    # Netzbetrieb: immer aktiv
    BATTERY = 'battery'  # Akkubetrieb: nur im Schlaffenster aktiv


# ── Konfiguration ─────────────────────────────────────────────────────────────

BAUD_RATE              = 115200
SAMPLE_HZ              = 50        # Samples pro Sekunde

# Kalibrierung
CALIBRATION_SECONDS    = 60
PRESENCE_SIGMA         = 4.0       # mean + Nσ für Eintrittserkennung
ACTIVITY_SIGMA         = 6.0       # mean + Nσ für Aktivitäts-Schwelle

# Kurzzeit-Varianzfenster (für schnelle Entry-/Activity-Erkennung)
VARIANCE_WINDOW_SEC    = 5         # 5s = 250 Samples bei 50 Hz

# Eintritts-Bestätigung
CONFIRM_ENTRY_SEC      = 20        # 20s erhöhte Aktivität → Bett belegt

# ── Exit-Erkennung: zweistufig (MIT und OHNE Bewegungspuls) ──────────────────
#
# Bett verlassen hat eine typische Signatur im Sensor:
#   Bewegungspuls (Aufstehen, typisch 0.5–3 Sekunden)
#   → dann Stille (Person geht weg)
#
# Mit Puls: kürzere Bestätigung nötig (klares Signal)
# Ohne Puls: sehr lange Stille (konservativ, Schutz vor Tiefschlaf-Fehlalarm)

EXIT_BURST_FACTOR      = 2.5       # activity_scale × Faktor → Puls erkannt
EXIT_BURST_WINDOW_SEC  = 60        # Zeitfenster in dem ein Puls "frisch" gilt

# Bestätigung MIT erkanntem Bewegungspuls:
CONFIRM_EXIT_BURST_DAY   = 180     # 3 min Stille nach Puls (Tag)
CONFIRM_EXIT_BURST_NIGHT = 600     # 10 min Stille nach Puls (Nacht)

# Bestätigung OHNE Bewegungspuls (Fallback, sehr konservativ):
CONFIRM_EXIT_NOBURST_DAY   = 600   # 10 min andauernde Stille (Tag)
CONFIRM_EXIT_NOBURST_NIGHT = 2700  # 45 min andauernde Stille (Nacht)
#   → Schutz vor Fehlalarm: Tiefschlaf kann 20–40 min ohne Bewegung sein!

# Nacht-Definition (für zeitabhängige Timeouts)
NIGHT_START_HOUR       = 22        # 22:00 Uhr
NIGHT_END_HOUR         = 9         # 09:00 Uhr

# Batteriemodus: Standard-Schlaffenster
DEFAULT_SLEEP_START    = 23        # 23:00 Uhr
DEFAULT_SLEEP_END      = 9         # 09:00 Uhr

# Arduino-Befehle (Batteriemodus)
CMD_PAUSE              = b'PAUSE\n'    # Arduino pausiert Sampling
CMD_RESUME             = b'RESUME\n'  # Arduino nimmt Sampling wieder auf
CMD_LOW_POWER          = b'LOWPOWER\n'  # Arduino: 1 Hz statt 50 Hz

# Hintergrund-Analyse
INCREMENTAL_ANALYSIS_MIN = 30      # Alle 30 min vorläufige Analyse
MIN_SAMPLES_FOR_ANALYSIS = 3000    # Mindestdaten für Analyse (60s bei 50 Hz)

# Session-Mindestdauer
MIN_SESSION_SECONDS    = 120

# SQLite-Commit-Intervall
COMMIT_EVERY           = 50        # Samples pro Commit-Batch


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

def is_night() -> bool:
    h = datetime.now().hour
    return h >= NIGHT_START_HOUR or h < NIGHT_END_HOUR


def in_tracking_window(sleep_start: int, sleep_end: int) -> bool:
    """Batteriemodus: Ist es gerade im aktiven Schlaf-Trackingfenster?"""
    h = datetime.now().hour
    if sleep_start > sleep_end:     # z.B. 23:00–09:00 (über Mitternacht)
        return h >= sleep_start or h < sleep_end
    else:                           # z.B. 22:00–07:00
        return sleep_start <= h < sleep_end


def minutes_until_window(sleep_start: int) -> float:
    """Wie viele Minuten bis das Trackingfenster beginnt?"""
    now = datetime.now()
    target = now.replace(hour=sleep_start, minute=0, second=0, microsecond=0)
    diff = (target - now).total_seconds()
    if diff < 0:
        diff += 86400
    return diff / 60.0


class RouterBridgePort:
    """
    Drop-in replacement for serial.Serial that reads/writes via the
    arduino-router monitor TCP port (127.0.0.1:7500).
    Used when collector.py runs on the QRB2210 Linux side of the Uno Q.
    The sketch must use Monitor.print/println() (Router Bridge Monitor).

    The arduino-router exposes a raw TCP port at 127.0.0.1:7500 where
    Monitor data flows as plain text — no msgpack needed here.
    """

    SOCKET_PATH = '/var/run/arduino-router.sock'
    MONITOR_HOST = '127.0.0.1'
    MONITOR_PORT = 7500

    def __init__(self, socket_path: str = SOCKET_PATH):
        import socket as _socket
        self._sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        self._sock.connect((self.MONITOR_HOST, self.MONITOR_PORT))
        self._sock.settimeout(30.0)
        self._buf    = b''
        self._lock   = threading.Lock()
        self.is_open = True

    def write(self, data: bytes):
        if isinstance(data, str):
            data = data.encode()
        try:
            with self._lock:
                self._sock.sendall(data)
        except OSError as e:
            raise serial.SerialException(f"monitor write error: {e}")

    def readline(self) -> bytes:
        while b'\n' not in self._buf:
            try:
                chunk = self._sock.recv(4096)
                if not chunk:
                    raise serial.SerialException("monitor connection closed")
                self._buf += chunk
            except TimeoutError:
                pass  # no data yet, keep waiting
        idx = self._buf.index(b'\n') + 1
        line, self._buf = self._buf[:idx], self._buf[idx:]
        return line

    def reset_input_buffer(self):
        with self._lock:
            self._buf = b''
        self._sock.settimeout(0.1)
        try:
            while self._sock.recv(4096):
                pass
        except OSError:
            pass
        self._sock.settimeout(30.0)

    def close(self):
        if self.is_open:
            self.is_open = False
            try:
                self._sock.close()
            except Exception:
                pass


def find_arduino_port() -> str | None:
    # On-device (QRB2210 Linux): use the Router Bridge unix socket.
    # The sketch uses Monitor (= Router Bridge), not raw UART.
    import os
    if os.path.exists(RouterBridgePort.SOCKET_PATH):
        return RouterBridgePort.SOCKET_PATH
    # Host machine: USB-attached Arduino
    ports = serial.tools.list_ports.comports()
    for p in ports:
        desc = (p.description or '').lower()
        if any(kw in desc for kw in ['arduino', 'stm32', 'acm', 'usb serial', 'ch340', 'cp210']):
            return p.device
    return ports[0].device if ports else None


def parse_line(line: str) -> tuple | None:
    """CSV parsen: millis,ax,ay,az,gx,gy,gz
    millis ist int (ms), ax/ay/az/gx/gy/gz sind float (g / dps, Modulino LSM6DSOX).
    """
    line = line.strip()
    if not line or line.startswith('#') or line.startswith('READY') \
            or line.startswith('EVENT') or line.startswith('SLEEPING') \
            or line.startswith('PAUSED') or line.startswith('RESUMED'):
        return None
    parts = line.split(',')
    if len(parts) != 7:
        return None
    try:
        millis = int(parts[0])
        ax, ay, az, gx, gy, gz = (float(p) for p in parts[1:])
        return (millis, ax, ay, az, gx, gy, gz)
    except ValueError:
        return None


def magnitude(ax: float, ay: float, az: float) -> float:
    return math.sqrt(ax * ax + ay * ay + az * az)


def running_variance(window: deque) -> float:
    n = len(window)
    if n < 2:
        return 0.0
    arr = list(window)
    mean = sum(arr) / n
    return sum((x - mean) ** 2 for x in arr) / (n - 1)


def setup_power_saving():
    """CPU-Governor auf 'powersave' setzen (Qualcomm QRB2210, Linux)."""
    import glob
    set_count = 0
    for gov_path in glob.glob('/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor'):
        try:
            with open(gov_path, 'w') as f:
                f.write('powersave')
            set_count += 1
        except Exception:
            pass
    if set_count:
        print(f"CPU-Governor: powersave ({set_count} Kern(e))")
    else:
        try:
            subprocess.run(['cpupower', 'frequency-set', '-g', 'powersave'],
                           capture_output=True, timeout=5)
            print("CPU-Governor: powersave (via cpupower)")
        except Exception:
            print("CPU-Governor: nicht gesetzt (kein root oder cpupower)")


# ── Bett-Zustandsmaschine ─────────────────────────────────────────────────────

class BedState(Enum):
    CALIBRATING      = auto()
    EMPTY            = auto()
    CANDIDATE_ENTRY  = auto()
    OCCUPIED         = auto()
    CANDIDATE_EXIT   = auto()


# ── Hintergrund-Analyse ───────────────────────────────────────────────────────

class IncrementalAnalyzer(threading.Thread):
    """
    Daemon-Thread: Führt alle 30 Minuten eine vorläufige Analyse durch.
    Vorteil: Sofort nach dem Aufwachen ist ein aktuelles Ergebnis verfügbar.
    """
    daemon = True

    def __init__(self, db_path: str, interval_min: int = INCREMENTAL_ANALYSIS_MIN):
        super().__init__(name='IncrementalAnalyzer')
        self.db_path      = db_path
        self.interval_sec = interval_min * 60
        self.session_id: str | None = None
        self._stop        = threading.Event()

    def set_session(self, session_id: str | None):
        self.session_id = session_id

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.wait(self.interval_sec):
            sid = self.session_id
            if sid:
                try:
                    db    = Database(self.db_path)
                    count = db.count_raw_for_session(sid)
                    if count >= MIN_SAMPLES_FOR_ANALYSIS:
                        now_str = datetime.now().strftime('%H:%M')
                        print(f"\n[{now_str}] Hintergrund-Analyse ({count} Samples)...",
                              flush=True)
                        analyze_session(sid, db)
                        print(f"[{now_str}] Analyse aktualisiert.", flush=True)
                    db.close()
                except Exception as e:
                    print(f"\n[Hintergrund] Analysefehler: {e}", flush=True)


# ── Haupt-Collector ───────────────────────────────────────────────────────────

class Collector:
    def __init__(self, port: str, db: Database,
                 mode: OperatingMode = OperatingMode.MAINS,
                 sleep_start: int = DEFAULT_SLEEP_START,
                 sleep_end:   int = DEFAULT_SLEEP_END,
                 auto_analyze: bool = True):
        self.port         = port
        self.db           = db
        self.mode         = mode
        self.sleep_start  = sleep_start
        self.sleep_end    = sleep_end
        self.auto_analyze = auto_analyze
        self.running      = True
        self._ser: serial.Serial | None = None

        # Session
        self.session_id:    str | None   = None
        self.session_start: float | None = None
        self.row_count     = 0
        self.commit_count  = 0

        # Kalibrierung laden
        calib = db.get_latest_calibration()
        if calib:
            self.baseline_mean   = calib['baseline_mean']
            self.baseline_std    = calib['baseline_std']
            self.presence_thresh = calib['presence_thresh']
            self.activity_scale  = calib['activity_scale']
            self.calibrated      = True
            print(f"Kalibrierung: mean={self.baseline_mean:.1f}, "
                  f"std={self.baseline_std:.1f}, "
                  f"presence_thresh={self.presence_thresh:.1f}")
        else:
            self.baseline_mean   = 0.0
            self.baseline_std    = 0.0
            self.presence_thresh = 0.0
            self.activity_scale  = 1.0
            self.calibrated      = False

        # Zustand
        self.state = BedState.CALIBRATING if not self.calibrated else BedState.EMPTY

        # Datenpuffer (5s Kurzzeit-Fenster für Varianz/Aktivität)
        self.var_window = deque(maxlen=int(VARIANCE_WINDOW_SEC * SAMPLE_HZ))

        # Kalibrierungspuffer
        self.calib_buffer: list[float] = []
        self.calib_start: float | None = None

        # Zeitstempel für Zustandsübergänge
        self.candidate_start: float | None = None

        # Exit-Puls-Tracking
        # Zeitpunkt des letzten signifikanten Bewegungspulses (Aufsteh-Signal)
        self.last_exit_burst_ts: float | None = None

        # Millis-Synchronisation
        self.wall_start:   float | None = None
        self.millis_start: int   | None = None

        # Hintergrund-Analyse
        self.bg_analyzer = IncrementalAnalyzer(db.path)
        self.bg_analyzer.start()

        # Anzeige
        self.display_counter = 0
        self.last_var        = 0.0

    # ── Zeitumrechnung ─────────────────────────────────────────────────────────

    def wall_time(self, millis: int) -> float:
        if self.wall_start is None:
            self.wall_start   = time.time()
            self.millis_start = millis
        return self.wall_start + (millis - self.millis_start) / 1000.0

    # ── Aktivitäts-Prüfung ────────────────────────────────────────────────────

    def _is_active(self) -> bool:
        """Erhöhte Bewegung (über Kalibrierungs-Schwellwert)?"""
        var = running_variance(self.var_window)
        return var > (self.presence_thresh ** 2 / max(self.baseline_std, 1.0))

    def _is_burst(self, mag: float) -> bool:
        """
        Exit-Puls: Signifikante Einzelbewegung wie beim Aufstehen.
        Deutlich höher als normale Schlafbewegungen.
        """
        return mag > self.activity_scale * EXIT_BURST_FACTOR

    def _burst_is_fresh(self, ts: float) -> bool:
        """Gab es kürzlich (< EXIT_BURST_WINDOW_SEC) einen Exit-Puls?"""
        if self.last_exit_burst_ts is None:
            return False
        return (ts - self.last_exit_burst_ts) < EXIT_BURST_WINDOW_SEC

    def _confirm_exit_sec(self, ts: float) -> int:
        """
        Benötigte Stille-Dauer für Exit-Bestätigung.
        Hängt ab von: Tageszeit UND ob ein Exit-Puls erkannt wurde.
        """
        night = is_night()
        if self._burst_is_fresh(ts):
            return CONFIRM_EXIT_BURST_NIGHT if night else CONFIRM_EXIT_BURST_DAY
        else:
            return CONFIRM_EXIT_NOBURST_NIGHT if night else CONFIRM_EXIT_NOBURST_DAY

    # ── Session-Management ────────────────────────────────────────────────────

    def _start_session(self, ts: float):
        self.session_id    = self.db.create_session(start_time=ts)
        self.session_start = ts
        self.row_count     = 0
        self.bg_analyzer.set_session(self.session_id)
        now_str = datetime.fromtimestamp(ts).strftime('%H:%M:%S')
        print(f"\n[{now_str}] BETT BELEGT → Session {self.session_id[:8]}")

    def _end_session(self, ts: float):
        if self.session_id is None:
            return
        self.bg_analyzer.set_session(None)
        duration = ts - (self.session_start or ts)

        if duration < MIN_SESSION_SECONDS:
            self.db.conn.execute("DELETE FROM raw_data WHERE session_id = ?",
                                 (self.session_id,))
            self.db.conn.execute("DELETE FROM sessions WHERE id = ?",
                                 (self.session_id,))
            self.db.flush()
            print(f"\nSession {self.session_id[:8]} verworfen "
                  f"(zu kurz: {duration:.0f}s)")
        else:
            self.db.end_session(self.session_id, end_time=ts)
            now_str = datetime.fromtimestamp(ts).strftime('%H:%M:%S')
            print(f"\n[{now_str}] BETT LEER → Session {self.session_id[:8]} "
                  f"beendet ({duration / 60:.1f} min, {self.row_count} Samples)")
            if self.auto_analyze and self.row_count >= MIN_SAMPLES_FOR_ANALYSIS:
                print("Abschluss-Analyse...", flush=True)
                try:
                    analyze_session(self.session_id, self.db)
                    print("Analyse abgeschlossen.")
                except Exception as e:
                    print(f"Analyse-Fehler: {e}")

        self.session_id    = None
        self.session_start = None
        self.row_count     = 0

    # ── Kalibrierung ──────────────────────────────────────────────────────────

    def _calibrate(self, mag: float, ts: float):
        if self.calib_start is None:
            self.calib_start = ts
            print(f"Kalibrierung ({CALIBRATION_SECONDS}s) – "
                  f"Bett muss LEER sein!", flush=True)

        self.calib_buffer.append(mag)
        elapsed = ts - self.calib_start

        if len(self.calib_buffer) % (SAMPLE_HZ * 10) == 0:
            print(f"  {elapsed:.0f}/{CALIBRATION_SECONDS}s...", flush=True)

        if elapsed >= CALIBRATION_SECONDS:
            buf  = np.array(self.calib_buffer)
            mean = float(np.mean(buf))
            std  = float(np.std(buf))

            self.baseline_mean   = mean
            self.baseline_std    = std
            self.presence_thresh = mean + PRESENCE_SIGMA * std
            self.activity_scale  = mean + ACTIVITY_SIGMA * std
            self.calibrated      = True

            self.db.save_calibration(
                baseline_mean   = mean,
                baseline_std    = std,
                presence_thresh = self.presence_thresh,
                activity_scale  = self.activity_scale,
                sample_rate     = SAMPLE_HZ,
                notes           = (f"Auto-Kalibrierung "
                                   f"{datetime.now().strftime('%d.%m.%Y %H:%M')}"),
            )

            print(f"Kalibrierung abgeschlossen:\n"
                  f"  Baseline:        {mean:.1f} ± {std:.1f}\n"
                  f"  Eintritts-SW:    {self.presence_thresh:.1f}\n"
                  f"  Exit-Puls-SW:    {self.activity_scale * EXIT_BURST_FACTOR:.1f}\n"
                  f"  Aktivitätsskala: {self.activity_scale:.1f}")

            self.state        = BedState.EMPTY
            self.calib_buffer = []

    # ── Haupt-Verarbeitung ────────────────────────────────────────────────────

    def process_sample(self, millis: int,
                       ax: float, ay: float, az: float,
                       gx: float, gy: float, gz: float):
        ts  = self.wall_time(millis)
        mag = magnitude(ax, ay, az)

        if self.state == BedState.CALIBRATING:
            self._calibrate(mag, ts)
            return

        self.var_window.append(mag)
        active = self._is_active()

        # ── Exit-Puls erkennen (immer, auch im OCCUPIED-Zustand) ─────────────
        if self.state == BedState.OCCUPIED and self._is_burst(mag):
            self.last_exit_burst_ts = ts

        # ── Zustandsübergänge ────────────────────────────────────────────────

        if self.state == BedState.EMPTY:
            if active:
                self.state           = BedState.CANDIDATE_ENTRY
                self.candidate_start = ts

        elif self.state == BedState.CANDIDATE_ENTRY:
            if not active:
                self.state           = BedState.EMPTY
                self.candidate_start = None
            elif ts - self.candidate_start >= CONFIRM_ENTRY_SEC:
                self.state = BedState.OCCUPIED
                self._start_session(self.candidate_start)
                self.candidate_start    = None
                self.last_exit_burst_ts = None   # frischer Start

        elif self.state == BedState.OCCUPIED:
            # Daten in DB speichern
            if self.session_id:
                self.db.insert_raw(self.session_id, ts, ax, ay, az, gx, gy, gz)
                self.row_count  += 1
                self.commit_count += 1
                if self.commit_count >= COMMIT_EVERY:
                    self.db.flush()
                    self.commit_count = 0

            if not active:
                # Keine Bewegung → Stille-Timer starten/laufen lassen
                if self.candidate_start is None:
                    self.candidate_start = ts
                    needed = self._confirm_exit_sec(ts)
                    burst_info = ("nach Puls" if self._burst_is_fresh(ts)
                                  else "ohne Puls")
                    period     = "Nacht" if is_night() else "Tag"
                    now_str    = datetime.fromtimestamp(ts).strftime('%H:%M:%S')
                    print(f"\n[{now_str}] Stille erkannt ({burst_info}, {period}: "
                          f"{needed // 60} min Bestätigung)...", flush=True)
                else:
                    # Prüfen ob Timeout erreicht
                    needed = self._confirm_exit_sec(self.candidate_start)
                    if ts - self.candidate_start >= needed:
                        self.state = BedState.CANDIDATE_EXIT
            else:
                # Bewegung → Stille-Timer zurücksetzen
                if self.candidate_start is not None:
                    self.candidate_start = None

        elif self.state == BedState.CANDIDATE_EXIT:
            if active:
                # Person wieder aktiv → noch im Bett (z.B. war kurz aufgestanden)
                now_str = datetime.fromtimestamp(ts).strftime('%H:%M:%S')
                print(f"\n[{now_str}] Rückkehr ins Bett.", flush=True)
                self.state           = BedState.OCCUPIED
                self.candidate_start = None
                self.last_exit_burst_ts = None
            else:
                # Bett verlassen bestätigt
                self.state = BedState.EMPTY
                self._end_session(ts)
                self.candidate_start    = None
                self.last_exit_burst_ts = None

        # ── Fortschrittsanzeige (jede Sekunde) ───────────────────────────────
        self.display_counter += 1
        self.last_var = running_variance(self.var_window)
        if self.display_counter >= SAMPLE_HZ:
            self.display_counter = 0
            ts_str     = datetime.fromtimestamp(ts).strftime('%H:%M:%S')
            night_mark = 'N' if is_night() else 'T'
            mode_mark  = 'B' if self.mode == OperatingMode.BATTERY else 'M'
            state_str  = {
                BedState.EMPTY:           'LEER     ',
                BedState.CANDIDATE_ENTRY: 'KOMMT... ',
                BedState.OCCUPIED:        'BELEGT   ',
                BedState.CANDIDATE_EXIT:  'GEHT?... ',
            }.get(self.state, '?        ')
            # Normalisierte Aktivität (0.0–1.0 relativ zur Eintrittsschwelle)
            thresh_sq  = (self.presence_thresh ** 2
                          / max(self.baseline_std, 1.0))
            activity   = min(self.last_var / max(thresh_sq, 1e-6), 9.99)
            burst_mark = '*' if self._burst_is_fresh(ts) else ' '
            print(
                f"\r[{ts_str} {night_mark}/{mode_mark}] {state_str} | "
                f"Akt: {activity:4.1f}{burst_mark} | "
                f"Samples: {self.row_count}",
                end='', flush=True,
            )

    # ── Batteriemodus-Verwaltung ──────────────────────────────────────────────

    def _send_cmd(self, cmd: bytes):
        """Sendet einen Steuerbefehl an den Arduino."""
        if self._ser and self._ser.is_open:
            try:
                self._ser.write(cmd)
            except Exception as e:
                print(f"\n[Arduino] Sendefehler: {e}", flush=True)

    def _battery_wait_for_window(self):
        """
        Batteriemodus: Schläft bis das Trackingfenster beginnt.
        Sendet während der Wartezeit einen PAUSE-Befehl an den Arduino.
        """
        self._send_cmd(CMD_PAUSE)
        time.sleep(0.5)

        while self.running:
            if in_tracking_window(self.sleep_start, self.sleep_end):
                break
            mins = minutes_until_window(self.sleep_start)
            now_str = datetime.now().strftime('%H:%M')
            print(f"\r[{now_str}] Batteriemodus: Tracking beginnt um "
                  f"{self.sleep_start:02d}:00 (in {mins:.0f} min)  ",
                  end='', flush=True)
            time.sleep(60)  # Jede Minute prüfen

        if self.running:
            print(f"\n[{datetime.now().strftime('%H:%M')}] Schlaffenster beginnt. "
                  f"Tracking aktiv.", flush=True)
            self._send_cmd(CMD_RESUME)
            time.sleep(1)
            # Eingangspuffer leeren
            if self._ser:
                self._ser.reset_input_buffer()

    # ── Haupt-Loop ────────────────────────────────────────────────────────────

    def _is_ondevice_port(self) -> bool:
        return self.port == RouterBridgePort.SOCKET_PATH

    def run(self, baud: int = BAUD_RATE):
        try:
            if self._is_ondevice_port():
                self._ser = RouterBridgePort(self.port)
            else:
                self._ser = serial.Serial(self.port, baud, timeout=3)
        except Exception as e:
            print(f"FEHLER: {e}")
            sys.exit(1)

        if self._is_ondevice_port():
            print(f"Port:  Router Bridge ({self.port})")
        else:
            print(f"Port:  {self.port} ({baud} baud)")
        print(f"Modus: {self.mode.value.upper()}", end='')
        if self.mode == OperatingMode.BATTERY:
            print(f" (Tracking: {self.sleep_start:02d}:00–{self.sleep_end:02d}:00)", end='')
        print()
        if self.calibrated:
            print("Kalibrierung: vorhanden")
        else:
            print(f"Kalibrierung: {CALIBRATION_SECONDS}s messen (Bett muss leer sein!)")
        print("Ctrl+C zum Beenden\n")

        # Auf Arduino-READY warten
        deadline = time.time() + 15
        while time.time() < deadline:
            raw = self._ser.readline()
            if not raw:
                continue
            line = raw.decode('utf-8', errors='ignore').strip()
            if line.startswith('READY'):
                print(f"Arduino bereit.")
                break

        # Batteriemodus: ggf. auf Tracking-Fenster warten
        if self.mode == OperatingMode.BATTERY:
            if not in_tracking_window(self.sleep_start, self.sleep_end):
                self._battery_wait_for_window()

        # ── Haupt-Schleife ────────────────────────────────────────────────────
        while self.running:

            # Batteriemodus: Ende des Trackingfensters prüfen
            if self.mode == OperatingMode.BATTERY:
                if not in_tracking_window(self.sleep_start, self.sleep_end):
                    now_str = datetime.now().strftime('%H:%M')
                    print(f"\n[{now_str}] Schlaffenster beendet. "
                          f"Arduino pausiert.", flush=True)
                    if self.session_id:
                        self.db.flush()
                        self._end_session(time.time())
                    self._battery_wait_for_window()
                    continue

            try:
                raw = self._ser.readline()
                if not raw:
                    continue
                line = raw.decode('utf-8', errors='ignore').strip()

                if line.startswith('EVENT,BED_MOTION,'):
                    now_str = datetime.now().strftime('%H:%M:%S')
                    print(f"\n[{now_str}] Arduino: Starke Bewegung erkannt",
                          flush=True)
                    continue
                if line in ('PAUSED', 'RESUMED', 'SLEEPING'):
                    print(f"\n[Arduino] {line}", flush=True)
                    continue

                parsed = parse_line(line)
                if parsed is None:
                    continue

                millis, ax, ay, az, gx, gy, gz = parsed
                self.process_sample(millis, ax, ay, az, gx, gy, gz)

            except serial.SerialException as e:
                print(f"\nSerial-Fehler: {e} – Reconnect in 5s...", flush=True)
                time.sleep(5)
                try:
                    self._ser.close()
                    if self._is_ondevice_port():
                        self._ser = RouterBridgePort(self.port)
                    else:
                        self._ser = serial.Serial(self.port, baud, timeout=3)
                    print("Neu verbunden.", flush=True)
                except Exception:
                    pass
            except Exception as e:
                print(f"\nFehler: {e}", flush=True)

        # ── Aufräumen ──────────────────────────────────────────────────────────
        self.bg_analyzer.stop()
        if self.session_id:
            self.db.flush()
            self._end_session(time.time())
        self.db.flush()
        if self._ser and self._ser.is_open:
            self._ser.close()
        print("\nCollector beendet.")


# ── Entry-Point ───────────────────────────────────────────────────────────────

def main():
    print("""
╔══════════════════════════════════════════╗
║   Schlafschaf v3 – Sleep Tracker         ║
║   Bewegungsbasierte Schlafanalyse        ║
╚══════════════════════════════════════════╝
""")

    parser = argparse.ArgumentParser(description='Schlafschaf Collector v3')
    parser.add_argument('--port',         help='Serial-Port (z.B. /dev/ttyACM0)')
    parser.add_argument('--baud',         type=int, default=BAUD_RATE)
    parser.add_argument('--db',           default=None)
    parser.add_argument('--mode',         choices=['mains', 'battery'],
                        default='mains',
                        help='mains = immer aktiv, battery = nur im Schlaffenster')
    parser.add_argument('--sleep-start',  type=int, default=DEFAULT_SLEEP_START,
                        help='Beginn Schlaffenster (Stunde, 0–23, Standard: 23)')
    parser.add_argument('--sleep-end',    type=int, default=DEFAULT_SLEEP_END,
                        help='Ende Schlaffenster (Stunde, 0–23, Standard: 9)')
    parser.add_argument('--calibrate',    action='store_true',
                        help='Neu kalibrieren (Bett muss leer sein!)')
    parser.add_argument('--no-analyze',   action='store_true',
                        help='Keine automatische Analyse')
    parser.add_argument('--no-powersave', action='store_true',
                        help='CPU-Energiesparmodus nicht setzen')
    args = parser.parse_args()

    if not args.no_powersave:
        setup_power_saving()

    port = args.port or find_arduino_port()
    if not port:
        print("FEHLER: Kein Arduino-Port gefunden. Mit --port angeben.")
        sys.exit(1)

    mode = OperatingMode(args.mode)
    db   = Database(args.db) if args.db else Database()

    collector = Collector(
        port         = port,
        db           = db,
        mode         = mode,
        sleep_start  = args.sleep_start,
        sleep_end    = args.sleep_end,
        auto_analyze = not args.no_analyze,
    )

    if args.calibrate:
        collector.calibrated = False
        collector.state      = BedState.CALIBRATING

    def on_exit(sig=None, frame=None):
        print("\n\nBeende Collector...")
        collector.running = False

    signal.signal(signal.SIGINT,  on_exit)
    signal.signal(signal.SIGTERM, on_exit)

    collector.run(args.baud)
    db.close()


if __name__ == '__main__':
    main()
