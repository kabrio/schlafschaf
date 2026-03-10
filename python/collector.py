"""
Schlafschaf v2 – Serial-Daten-Collector (24/7 Betterkennungs-Modus)

Läuft dauerhaft als systemd-Service und erkennt automatisch:
  - Wann eine Person ins Bett geht → Session automatisch starten
  - Wann die Person das Bett verlässt → Session beenden + Analyse starten

Betterkennungs-Algorithmus:
  1. Kalibrierung (leeres Bett):
     - Beim Start: CALIBRATION_SECONDS Sekunden Baseline-Rauschen messen
     - Speichert Baseline-Mittelwert und Standardabweichung in DB
     - Präsenz-Schwellwert = baseline_mean + PRESENCE_SIGMA × baseline_std
  2. Zustandsmaschine:
     EMPTY → CANDIDATE_ENTRY: laufende Varianz > Präsenz-Schwellwert
     CANDIDATE_ENTRY → OCCUPIED: Varianz bleibt erhöht für CONFIRM_ENTRY_SEC
     OCCUPIED → CANDIDATE_EXIT: Varianz sinkt unter Schwellwert
     CANDIDATE_EXIT → EMPTY: Varianz bleibt niedrig für CONFIRM_EXIT_SEC
     (jeder Zustandswechsel zurück zum vorherigen wenn Bedingung nicht hält)
  3. Schritterkennungs-Vorwarnung:
     Wenn EMPTY: Hochfrequente Impulse (> STEP_FREQ_THRESH) in
     kurzer Folge können nahende Person ankündigen → Anticipation-Mode
     (noch kein Session-Start, aber früheres Erkennen möglich)

CSV-Format vom Arduino v2: millis,ax,ay,az,gx,gy,gz  (7 Felder)
Ereigniszeilen:             EVENT,BED_MOTION,<millis>

Verwendung:
  python3 collector.py [--port /dev/ttyACM0] [--baud 115200] [--calibrate]
"""

import argparse
import math
import signal
import sys
import time
from collections import deque
from datetime import datetime
from enum import Enum, auto

import serial
import serial.tools.list_ports

from database import Database
from analyzer import analyze_session

# ── Konfiguration ─────────────────────────────────────────────────────────────

BAUD_RATE             = 115200
SAMPLE_HZ             = 50        # Arduino sendet 50 Hz

# Kalibrierung (leeres Bett)
CALIBRATION_SECONDS   = 60        # Sekunden Baseline messen
PRESENCE_SIGMA        = 4.0       # Schwellwert = mean + SIGMA × std
ACTIVITY_SIGMA        = 6.0       # Aktivitätsskala = mean + SIGMA × std (für Normierung)

# Betterkennungs-Zeiten
CONFIRM_ENTRY_SEC     = 15        # Sekunden erhöhte Aktivität → Person im Bett
CONFIRM_EXIT_SEC      = 300       # Sekunden niedrige Aktivität → Bett verlassen (5 min)
VARIANCE_WINDOW_SEC   = 5         # Fenstergröße für laufende Varianzberechnung

# Schritterkennungs-Vorwarnung
STEP_DETECTION        = True      # Schrittvibrations-Erkennung aktivieren
STEP_IMPULSE_THRESH   = 0.6       # Normierter Spike für Schrittvibration
STEP_MIN_COUNT        = 4         # Mindest-Impulse für Schritterkennung
STEP_WINDOW_SEC       = 3.0       # Zeitfenster für Schritterkennung

# Session-Mindestdauer
MIN_SESSION_SECONDS   = 120       # Unter 2 min → kein Speichern (Rauschen)

# Commit-Intervall
COMMIT_EVERY          = 50        # Jede N-te Zeile committen (bei 50 Hz = 1s)


# ── Zustandsmaschine ─────────────────────────────────────────────────────────

class BedState(Enum):
    CALIBRATING      = auto()
    EMPTY            = auto()
    CANDIDATE_ENTRY  = auto()
    OCCUPIED         = auto()
    CANDIDATE_EXIT   = auto()


# ── Hilfsfunktionen ──────────────────────────────────────────────────────────

def find_arduino_port() -> str | None:
    """Automatisch den Arduino-Serial-Port finden."""
    ports = serial.tools.list_ports.comports()
    for p in ports:
        desc = (p.description or '').lower()
        if any(kw in desc for kw in ['arduino', 'stm32', 'acm', 'usb serial', 'ch340', 'cp210']):
            return p.device
    # Fallback: erster verfügbarer Port
    if ports:
        return ports[0].device
    return None


def parse_line(line: str) -> tuple | None:
    """
    CSV-Zeile parsen: millis,ax,ay,az,gx,gy,gz (7 Felder)
    Gibt None zurück bei Fehler oder Kommentarzeilen.
    """
    line = line.strip()
    if not line or line.startswith('#') or line.startswith('READY') or line.startswith('EVENT'):
        return None
    parts = line.split(',')
    if len(parts) != 7:
        return None
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def magnitude(ax: int, ay: int, az: int) -> float:
    """Euklidische Magnitude der Beschleunigung (raw-Einheiten)."""
    return math.sqrt(ax * ax + ay * ay + az * az)


def running_variance(window: deque) -> float:
    """Varianz der Werte in einem deque-Puffer."""
    n = len(window)
    if n < 2:
        return 0.0
    mean = sum(window) / n
    return sum((x - mean) ** 2 for x in window) / (n - 1)


# ── Haupt-Collector ──────────────────────────────────────────────────────────

class Collector:
    def __init__(self, port: str, db: Database, auto_analyze: bool = True):
        self.port       = port
        self.db         = db
        self.auto_analyze = auto_analyze
        self.running    = True

        # Aktuelle Session
        self.session_id:   str | None   = None
        self.session_start: float | None = None
        self.row_count   = 0
        self.commit_count = 0

        # Kalibrierung
        calib = db.get_latest_calibration()
        if calib:
            self.baseline_mean     = calib['baseline_mean']
            self.baseline_std      = calib['baseline_std']
            self.presence_thresh   = calib['presence_thresh']
            self.activity_scale    = calib['activity_scale']
            self.calibrated        = True
            print(f"Kalibrierung geladen: mean={self.baseline_mean:.1f}, "
                  f"std={self.baseline_std:.1f}, "
                  f"presence_thresh={self.presence_thresh:.1f}")
        else:
            self.baseline_mean     = 0.0
            self.baseline_std      = 0.0
            self.presence_thresh   = 0.0
            self.activity_scale    = 1.0
            self.calibrated        = False

        # Zustandsmaschine
        self.state = BedState.CALIBRATING if not self.calibrated else BedState.EMPTY

        # Laufende Fenster
        window_size = VARIANCE_WINDOW_SEC * SAMPLE_HZ
        self.var_window    = deque(maxlen=int(window_size))
        self.calib_buffer  = []            # Für Kalibrierung
        self.calib_start   = None

        # Zustandsübergänge
        self.candidate_start: float | None = None

        # Schrittvibrations-Erkennung
        self.step_times: list[float] = []   # Zeitstempel der letzten Impulse
        self.last_mag  = -1.0

        # Millis-Synchronisierung
        self.wall_start:   float | None = None
        self.millis_start: int   | None = None

        # Fortschrittsanzeige
        self.display_counter = 0

    def wall_time(self, millis: int) -> float:
        """Arduino-Millisekunden in Unix-Timestamp umrechnen."""
        if self.wall_start is None:
            self.wall_start   = time.time()
            self.millis_start = millis
        return self.wall_start + (millis - self.millis_start) / 1000.0

    def _start_session(self, ts: float):
        self.session_id    = self.db.create_session(start_time=ts)
        self.session_start = ts
        self.row_count     = 0
        now_str = datetime.fromtimestamp(ts).strftime('%H:%M:%S')
        print(f"\n[{now_str}] BETT BELEGT → Session {self.session_id[:8]} gestartet")

    def _end_session(self, ts: float):
        if self.session_id is None:
            return
        duration = ts - (self.session_start or ts)
        if duration < MIN_SESSION_SECONDS:
            # Zu kurz → Session löschen (Fehlalarm)
            self.db.conn.execute(
                "DELETE FROM raw_data WHERE session_id = ?", (self.session_id,)
            )
            self.db.conn.execute(
                "DELETE FROM sessions WHERE id = ?", (self.session_id,)
            )
            self.db.flush()
            print(f"\nSession {self.session_id[:8]} verworfen (zu kurz: {duration:.0f}s)")
        else:
            self.db.end_session(self.session_id, end_time=ts)
            now_str = datetime.fromtimestamp(ts).strftime('%H:%M:%S')
            print(f"\n[{now_str}] BETT LEER → Session {self.session_id[:8]} "
                  f"beendet ({duration/60:.1f} min)")
            if self.auto_analyze and self.row_count > 100:
                print("Analysiere Session...")
                try:
                    analyze_session(self.session_id, self.db)
                except Exception as e:
                    print(f"Analyse-Fehler: {e}")
        self.session_id    = None
        self.session_start = None
        self.row_count     = 0

    def _calibrate(self, mag: float, ts: float):
        """Kalibrierungsdaten sammeln (leeres Bett)."""
        if self.calib_start is None:
            self.calib_start = ts
            print(f"Kalibrierung gestartet ({CALIBRATION_SECONDS}s)...", flush=True)

        self.calib_buffer.append(mag)
        elapsed = ts - self.calib_start

        # Fortschritt
        if len(self.calib_buffer) % (SAMPLE_HZ * 5) == 0:
            remaining = CALIBRATION_SECONDS - elapsed
            print(f"  Kalibrierung: {elapsed:.0f}/{CALIBRATION_SECONDS}s "
                  f"(noch {remaining:.0f}s)...", flush=True)

        if elapsed >= CALIBRATION_SECONDS:
            import statistics
            mean = statistics.mean(self.calib_buffer)
            std  = statistics.stdev(self.calib_buffer)
            presence_thresh = mean + PRESENCE_SIGMA * std
            activity_scale  = mean + ACTIVITY_SIGMA * std

            self.baseline_mean   = mean
            self.baseline_std    = std
            self.presence_thresh = presence_thresh
            self.activity_scale  = activity_scale
            self.calibrated      = True

            self.db.save_calibration(
                baseline_mean   = mean,
                baseline_std    = std,
                presence_thresh = presence_thresh,
                activity_scale  = activity_scale,
                sample_rate     = SAMPLE_HZ,
                notes           = f"Auto-Kalibrierung {datetime.now().strftime('%d.%m.%Y %H:%M')}"
            )
            print(f"Kalibrierung abgeschlossen:")
            print(f"  Baseline:         {mean:.1f} ± {std:.1f} raw")
            print(f"  Präsenz-Schwellw: {presence_thresh:.1f}")
            print(f"  Aktivitätsskala:  {activity_scale:.1f}")
            self.state = BedState.EMPTY
            self.calib_buffer = []

    def _detect_footstep(self, mag: float, ts: float) -> bool:
        """
        Schrittvibrations-Erkennung: hochfrequente Impulse, die durch Schritte
        in der Nähe des Bettes entstehen.
        Gibt True zurück wenn Schritte erkannt wurden.
        """
        if not STEP_DETECTION or self.last_mag < 0:
            self.last_mag = mag
            return False

        # Normierter Sprung
        scale = max(self.activity_scale, 1.0)
        delta = abs(mag - self.last_mag) / scale
        self.last_mag = mag

        if delta > STEP_IMPULSE_THRESH:
            self.step_times.append(ts)

        # Alte Einträge entfernen
        cutoff = ts - STEP_WINDOW_SEC
        self.step_times = [t for t in self.step_times if t > cutoff]

        return len(self.step_times) >= STEP_MIN_COUNT

    def process_sample(self, millis: int,
                       ax: int, ay: int, az: int,
                       gx: int, gy: int, gz: int):
        """Einen Sample verarbeiten und Zustandsmaschine aktualisieren."""
        ts  = self.wall_time(millis)
        mag = magnitude(ax, ay, az)

        # ── Kalibrierung ────────────────────────────────────────────────────
        if self.state == BedState.CALIBRATING:
            self._calibrate(mag, ts)
            return

        # ── Laufende Varianz aktualisieren ──────────────────────────────────
        self.var_window.append(mag)
        var = running_variance(self.var_window)
        is_active = var > (self.presence_thresh ** 2 / self.baseline_std
                           if self.baseline_std > 0 else self.presence_thresh)

        # ── Zustandsübergänge ────────────────────────────────────────────────

        if self.state == BedState.EMPTY:
            # Schrittvibrations-Vorwarnung
            footstep = self._detect_footstep(mag, ts)
            if footstep:
                now_str = datetime.fromtimestamp(ts).strftime('%H:%M:%S')
                print(f"\n[{now_str}] Schritte erkannt (Vorwarnung)...", flush=True)
                self.step_times = []  # Reset nach Meldung

            if is_active:
                self.state           = BedState.CANDIDATE_ENTRY
                self.candidate_start = ts

        elif self.state == BedState.CANDIDATE_ENTRY:
            if not is_active:
                # Fehlalarm – zurück zu EMPTY
                self.state = BedState.EMPTY
                self.candidate_start = None
            elif ts - self.candidate_start >= CONFIRM_ENTRY_SEC:
                # Bestätigt: Person im Bett
                self.state = BedState.OCCUPIED
                self._start_session(self.candidate_start)
                self.candidate_start = None

        elif self.state == BedState.OCCUPIED:
            # Daten speichern
            if self.session_id:
                self.db.insert_raw(
                    self.session_id, ts,
                    ax, ay, az, gx, gy, gz
                )
                self.row_count  += 1
                self.commit_count += 1
                if self.commit_count >= COMMIT_EVERY:
                    self.db.flush()
                    self.commit_count = 0

            if not is_active:
                self.state           = BedState.CANDIDATE_EXIT
                self.candidate_start = ts

        elif self.state == BedState.CANDIDATE_EXIT:
            if is_active:
                # Person hat sich nochmal bewegt – zurück zu OCCUPIED
                self.state           = BedState.OCCUPIED
                self.candidate_start = None
            elif ts - self.candidate_start >= CONFIRM_EXIT_SEC:
                # Bestätigt: Bett verlassen
                self.state = BedState.EMPTY
                self._end_session(self.candidate_start)
                self.candidate_start = None

        # ── Fortschrittsanzeige (jede Sekunde) ──────────────────────────────
        self.display_counter += 1
        if self.display_counter >= SAMPLE_HZ:
            self.display_counter = 0
            ts_str = datetime.fromtimestamp(ts).strftime('%H:%M:%S')
            state_str = {
                BedState.EMPTY:           'LEER     ',
                BedState.CANDIDATE_ENTRY: 'KOMMT... ',
                BedState.OCCUPIED:        'BELEGT   ',
                BedState.CANDIDATE_EXIT:  'GEHT?... ',
            }.get(self.state, '?        ')
            var_disp = math.sqrt(max(var, 0))
            print(
                f"\r[{ts_str}] Bett: {state_str} | "
                f"Varianz: {var_disp:7.1f} | "
                f"Schwellw: {self.presence_thresh:.1f} | "
                f"Samples: {self.row_count}",
                end='', flush=True
            )

    def run(self, baud: int = BAUD_RATE):
        """Haupt-Leseschleife."""
        ser = None
        try:
            ser = serial.Serial(self.port, baud, timeout=3)
        except serial.SerialException as e:
            print(f"FEHLER: Serial-Port konnte nicht geöffnet werden: {e}")
            sys.exit(1)

        print(f"Port:  {self.port} ({baud} baud)")
        print(f"Modus: 24/7 Betterkennungs-Collector")
        if self.calibrated:
            print(f"Status: Kalibrierung vorhanden → direkt starten")
        else:
            print(f"Status: Keine Kalibrierung → {CALIBRATION_SECONDS}s Baseline messen")
        print("Warte auf Arduino... (Ctrl+C zum Beenden)\n")

        # Auf READY warten
        while True:
            raw = ser.readline()
            if not raw:
                continue
            line = raw.decode('utf-8', errors='ignore').strip()
            if line.startswith('READY'):
                mpu_ok = "MPU-6050 OK" if line == "READY" else "MPU-6050 NICHT GEFUNDEN"
                print(f"Arduino bereit ({mpu_ok})")
                break

        # Haupt-Loop
        while self.running:
            try:
                raw = ser.readline()
                if not raw:
                    continue
                line = raw.decode('utf-8', errors='ignore').strip()

                # Ereigniszeilen (Arduino-seitige Bewegungs-Events)
                if line.startswith('EVENT,BED_MOTION,'):
                    now_str = datetime.now().strftime('%H:%M:%S')
                    print(f"\n[{now_str}] Arduino-Event: Starke Bewegung erkannt",
                          flush=True)
                    continue

                parsed = parse_line(line)
                if parsed is None:
                    continue

                millis, ax, ay, az, gx, gy, gz = parsed
                self.process_sample(millis, ax, ay, az, gx, gy, gz)

            except serial.SerialException as e:
                print(f"\nSerial-Fehler: {e}")
                print("Warte 5s, dann neu verbinden...")
                time.sleep(5)
                try:
                    ser.close()
                    ser = serial.Serial(self.port, baud, timeout=3)
                    print("Neu verbunden.")
                except serial.SerialException:
                    print("Neuverbindung fehlgeschlagen. Versuche weiter...")
            except Exception as e:
                print(f"\nFehler: {e}")

        # Aufräumen
        if self.session_id:
            self.db.flush()
            self.db.end_session(self.session_id)
        self.db.flush()
        if ser and ser.is_open:
            ser.close()
        print("\nCollector beendet.")


# ── Kalibrierungs-Hilfsfunktion ───────────────────────────────────────────────

def run_calibration_only(port: str, baud: int, db: Database):
    """Nur Kalibrierung durchführen (--calibrate Flag)."""
    print(f"KALIBRIERUNGSMODUS: {CALIBRATION_SECONDS}s Baseline messen")
    print("Bitte sicherstellen, dass das Bett LEER ist!\n")
    time.sleep(3)

    collector = Collector(port, db, auto_analyze=False)
    collector.calibrated = False
    collector.state      = BedState.CALIBRATING
    collector.run(baud)


# ── Entry-Point ───────────────────────────────────────────────────────────────

def main():
    print("""
╔══════════════════════════════════════════╗
║   Schlafschaf v2 – Sleep Tracker         ║
║   24/7 Betterkennungs-Modus              ║
╚══════════════════════════════════════════╝
""")

    parser = argparse.ArgumentParser(description='Schlafschaf Serial Collector v2')
    parser.add_argument('--port',      help='Serial-Port (z.B. /dev/ttyACM0)')
    parser.add_argument('--baud',      type=int, default=BAUD_RATE)
    parser.add_argument('--db',        default=None, help='Pfad zur SQLite-DB')
    parser.add_argument('--calibrate', action='store_true',
                        help='Nur Kalibrierung durchführen (leeres Bett!)')
    parser.add_argument('--no-analyze', action='store_true',
                        help='Keine automatische Analyse nach Session-Ende')
    args = parser.parse_args()

    port = args.port or find_arduino_port()
    if not port:
        print("FEHLER: Kein Arduino-Port gefunden. Mit --port angeben.")
        sys.exit(1)

    db        = Database(args.db) if args.db else Database()
    collector = Collector(port, db, auto_analyze=not args.no_analyze)

    # Signal-Handler
    def on_exit(sig=None, frame=None):
        print("\n\nBeende Collector...")
        collector.running = False

    signal.signal(signal.SIGINT,  on_exit)
    signal.signal(signal.SIGTERM, on_exit)

    if args.calibrate:
        run_calibration_only(port, args.baud, db)
    else:
        collector.run(args.baud)

    db.close()


if __name__ == '__main__':
    main()
