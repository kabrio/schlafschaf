"""
Schlafschaf v2 – Serial-Daten-Collector (24/7 Betterkennungs-Modus)

Kritisches Problem und Lösung:
  ────────────────────────────────────────────────────────────────────
  PROBLEM: Tiefschlaf ≈ leeres Bett (beide haben sehr niedrige Varianz)
           → Reine Varianz-Schwellwerte können Tiefschlaf fälschlicherweise
             als "Bett verlassen" interpretieren!

  LÖSUNG:  Atemfrequenz-Präsenz als primären Belegungs-Indikator nutzen.
           - Leeres Bett:  <3% Energie in 0.1–0.5 Hz Band (nur Sensor-Rauschen)
           - Tiefschlaf:  >10% Energie in 0.1–0.5 Hz Band (Atembewegung)
           - Leichtschlaf: >20% Energie (deutliche Atembewegung)
           - REM/Wach:     variable, Bewegungsenergie dominiert

  Zusätzlich: Zeit-bewusste Exit-Erkennung
           - Nacht (22:00–09:00): 30 Minuten Exit-Bestätigung
           - Tag: 5 Minuten Exit-Bestätigung
           → Verhindert Fehlalarm bei Tiefschlaf-Phasen um 2 Uhr nachts!
  ────────────────────────────────────────────────────────────────────

Betterkennungs-Zustandsmaschine:
  CALIBRATING → EMPTY → CANDIDATE_ENTRY → OCCUPIED → CANDIDATE_EXIT → EMPTY
                                    ↑_______________|

Belegungsprüfung (Priorität):
  1. Atemfrequenz-Energie (primär): FFT in 0.1–0.5 Hz über 30s Fenster
  2. Varianz (sekundär, nur für Entry-Erkennung): schnelles 5s Fenster
  Exit: BEIDE müssen fehlen + zeitabhängige Wartezeit

Hintergrund-Analyse:
  Alle INCREMENTAL_ANALYSIS_MIN Minuten wird die aktive Session vorläufig
  analysiert → Ergebnis sofort nach dem Aufwachen verfügbar.

CPU-Energiesparmodus:
  Beim Start wird der Linux CPU-Governor auf 'powersave' gesetzt.
  Spart ~30-50% Leistungsaufnahme bei kontinuierlichem Betrieb.

Verwendung:
  python3 collector.py [--port /dev/ttyACM0] [--baud 115200] [--calibrate]
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

# ── Konfiguration ─────────────────────────────────────────────────────────────

BAUD_RATE              = 115200
SAMPLE_HZ              = 50

# Kalibrierung (leeres Bett)
CALIBRATION_SECONDS    = 60
PRESENCE_SIGMA         = 4.0       # Varianz-Schwellwert: mean + 4σ
ACTIVITY_SIGMA         = 6.0

# Atemfrequenz-Belegungserkennung (Kern-Feature)
RESP_BAND_LO           = 0.10      # Hz
RESP_BAND_HI           = 0.50      # Hz
RESP_WINDOW_SEC        = 30        # Fenster für Atemanalyse (1500 Samples bei 50 Hz)
RESP_OCCUPIED_THRESH   = 0.05      # Min. 5% Energie in Atemband → Person anwesend
RESP_EXIT_THRESH       = 0.03      # Unter 3% → kein Atembewegung erkennbar

# Varianz-Fenster (für schnelle Entry-Erkennung, nicht für Exit)
VARIANCE_WINDOW_SEC    = 5

# Bettein-/-austritts-Zeiten
CONFIRM_ENTRY_SEC      = 20        # 20s erhöhte Aktivität → Bett belegt

# Zeit-bewusste Exit-Erkennung (Schlüssel-Feature!)
CONFIRM_EXIT_DAY_SEC   = 300       # 5 min tagsüber
CONFIRM_EXIT_NIGHT_SEC = 1800      # 30 min nachts (kein Fehlalarm bei Tiefschlaf!)
NIGHT_START_HOUR       = 22        # 22:00 Uhr
NIGHT_END_HOUR         = 9         # 09:00 Uhr

# Schrittvibrationsvorwarnung
STEP_DETECTION         = True
STEP_IMPULSE_THRESH    = 0.6
STEP_MIN_COUNT         = 4
STEP_WINDOW_SEC        = 3.0

# Hintergrund-Analyse
INCREMENTAL_ANALYSIS_MIN = 30      # Alle 30 Minuten vorläufige Analyse
MIN_SAMPLES_FOR_ANALYSIS = 3000    # Min. 60s Daten (bei 50 Hz) für Analyse

# Session-Mindestdauer
MIN_SESSION_SECONDS    = 120

# SQLite Commit-Intervall
COMMIT_EVERY           = 50        # Jede N-te Zeile (bei 50 Hz = 1s)


# ── Zustandsmaschine ──────────────────────────────────────────────────────────

class BedState(Enum):
    CALIBRATING      = auto()
    EMPTY            = auto()
    CANDIDATE_ENTRY  = auto()
    OCCUPIED         = auto()
    CANDIDATE_EXIT   = auto()


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

def is_night() -> bool:
    """Ist es gerade Nacht (22:00–09:00)?"""
    h = datetime.now().hour
    return h >= NIGHT_START_HOUR or h < NIGHT_END_HOUR


def get_confirm_exit_sec() -> int:
    """Zeit-bewusste Exit-Bestätigungszeit."""
    return CONFIRM_EXIT_NIGHT_SEC if is_night() else CONFIRM_EXIT_DAY_SEC


def find_arduino_port() -> str | None:
    ports = serial.tools.list_ports.comports()
    for p in ports:
        desc = (p.description or '').lower()
        if any(kw in desc for kw in ['arduino', 'stm32', 'acm', 'usb serial', 'ch340', 'cp210']):
            return p.device
    return ports[0].device if ports else None


def parse_line(line: str) -> tuple | None:
    """CSV parsen: millis,ax,ay,az,gx,gy,gz (7 Felder)"""
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
    return math.sqrt(ax * ax + ay * ay + az * az)


def respiratory_energy_ratio(magnitudes: np.ndarray, fs: float) -> float:
    """
    Anteil der Energie im Atemfrequenzband (0.1–0.5 Hz) an der Gesamtenergie.

    Dies ist der primäre Unterschied zwischen leerem Bett und Tiefschlaf:
    - Leeres Bett:  <3%  (nur Sensor-Eigenrauschen, kein biologisches Signal)
    - Tiefschlaf:  >10%  (regelmäßige, tiefe Atembewegung klar erkennbar)
    - Leichtschlaf: >20% (deutliche Atembewegung)
    - REM/Wach:    variabel, oft Bewegungsenergie dominiert

    Benötigt mindestens 5 Sekunden Daten für sinnvolle FFT-Auflösung.
    Die Frequenzauflösung beträgt 1/N·fs Hz, also bei 30s: 1/30 = 0.033 Hz.
    """
    n = len(magnitudes)
    if n < int(fs * 5):
        return 0.0

    # DC-Anteil entfernen (Gravitations-Offset ist konstant)
    sig = magnitudes - np.mean(magnitudes)
    if np.std(sig) < 1e-10:
        return 0.0

    fft_vals = np.fft.rfft(sig)
    freqs    = np.fft.rfftfreq(n, d=1.0 / fs)
    power    = np.abs(fft_vals) ** 2
    total    = float(np.sum(power))

    if total < 1e-10:
        return 0.0

    resp_mask = (freqs >= RESP_BAND_LO) & (freqs <= RESP_BAND_HI)
    return float(np.sum(power[resp_mask])) / total


def running_variance(window: deque) -> float:
    n = len(window)
    if n < 2:
        return 0.0
    mean = sum(window) / n
    return sum((x - mean) ** 2 for x in window) / (n - 1)


# ── CPU-Energiesparmodus ──────────────────────────────────────────────────────

def setup_power_saving():
    """
    Qualcomm/Linux CPU-Governor auf 'powersave' setzen.
    Spart ~30-50% Leistungsaufnahme im Dauerbetrieb.
    Erfordert root-Rechte oder entsprechende udev-Regeln.
    """
    governors_path = '/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor'
    try:
        with open(governors_path, 'w') as f:
            f.write('powersave')
        # Alle CPU-Kerne
        import glob
        for gov_path in glob.glob('/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor'):
            try:
                with open(gov_path, 'w') as f:
                    f.write('powersave')
            except Exception:
                pass
        print("CPU-Governor: powersave (alle Kerne)")
    except PermissionError:
        # Ohne root: cpupower als Fallback
        try:
            result = subprocess.run(
                ['cpupower', 'frequency-set', '-g', 'powersave'],
                capture_output=True, timeout=5
            )
            if result.returncode == 0:
                print("CPU-Governor: powersave (via cpupower)")
            else:
                print("CPU-Governor: powersave nicht gesetzt (kein root)")
        except FileNotFoundError:
            print("CPU-Governor: cpupower nicht gefunden – manuell setzen oder als root starten")
    except Exception as e:
        print(f"CPU-Governor: Fehler – {e}")


# ── Hintergrund-Analyse ───────────────────────────────────────────────────────

class IncrementalAnalyzer(threading.Thread):
    """
    Führt alle INCREMENTAL_ANALYSIS_MIN Minuten eine vorläufige Analyse durch.
    Läuft als Daemon-Thread – beendet sich automatisch mit dem Hauptprozess.

    Vorteil: Direkt nach dem Aufwachen ist eine aktuelle Analyse verfügbar,
    ohne auf das Session-Ende warten zu müssen.
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
                    db = Database(self.db_path)
                    count = db.count_raw_for_session(sid)
                    if count >= MIN_SAMPLES_FOR_ANALYSIS:
                        now_str = datetime.now().strftime('%H:%M')
                        print(f"\n[{now_str}] Hintergrund-Analyse ({count} Samples)...",
                              flush=True)
                        analyze_session(sid, db)
                        print(f"[{now_str}] Hintergrund-Analyse abgeschlossen.",
                              flush=True)
                    db.close()
                except Exception as e:
                    print(f"\n[Hintergrund] Analysefehler: {e}", flush=True)


# ── Haupt-Collector ───────────────────────────────────────────────────────────

class Collector:
    def __init__(self, port: str, db: Database, auto_analyze: bool = True):
        self.port          = port
        self.db            = db
        self.auto_analyze  = auto_analyze
        self.running       = True

        # Session
        self.session_id:    str | None   = None
        self.session_start: float | None = None
        self.row_count     = 0
        self.commit_count  = 0

        # Kalibrierung laden
        calib = db.get_latest_calibration()
        if calib:
            self.baseline_mean     = calib['baseline_mean']
            self.baseline_std      = calib['baseline_std']
            self.presence_thresh   = calib['presence_thresh']
            self.activity_scale    = calib['activity_scale']
            self.resp_baseline     = calib.get('resp_baseline', 0.03)
            self.calibrated        = True
            print(f"Kalibrierung geladen: mean={self.baseline_mean:.1f}, "
                  f"std={self.baseline_std:.1f}, "
                  f"presence_thresh={self.presence_thresh:.1f}, "
                  f"resp_baseline={self.resp_baseline:.3f}")
        else:
            self.baseline_mean     = 0.0
            self.baseline_std      = 0.0
            self.presence_thresh   = 0.0
            self.activity_scale    = 1.0
            self.resp_baseline     = 0.02
            self.calibrated        = False

        # Zustandsmaschine
        self.state = BedState.CALIBRATING if not self.calibrated else BedState.EMPTY

        # Datenpuffer
        # Kurzes Fenster (5s) für schnelle Entry-Erkennung via Varianz
        self.var_window    = deque(maxlen=int(VARIANCE_WINDOW_SEC * SAMPLE_HZ))
        # Langes Fenster (30s) für Atemfrequenz-Präsenz-Prüfung
        self.resp_window   = deque(maxlen=int(RESP_WINDOW_SEC * SAMPLE_HZ))

        # Kalibrierungspuffer
        self.calib_buffer  = []
        self.calib_start   = None

        # Zustandsübergangs-Zeitstempel
        self.candidate_start: float | None = None
        self.last_exit_check_ts: float = 0.0

        # Schritterkennung
        self.step_times: list[float] = []
        self.last_mag = -1.0

        # Millis-Sync
        self.wall_start:   float | None = None
        self.millis_start: int   | None = None

        # Hintergrund-Analyse
        self.bg_analyzer = IncrementalAnalyzer(db.path)
        self.bg_analyzer.start()

        # Anzeige
        self.display_counter = 0
        self.last_resp_ratio = 0.0

    def wall_time(self, millis: int) -> float:
        if self.wall_start is None:
            self.wall_start   = time.time()
            self.millis_start = millis
        return self.wall_start + (millis - self.millis_start) / 1000.0

    def _is_resp_occupied(self) -> bool:
        """
        Primäre Belegungsprüfung: Atemfrequenz-Energie im 30s-Fenster.

        Tiefschlaf hat immer >10% Atemfrequenz-Energie – leeres Bett nie!
        Diese Methode schützt vor Fehlalarmen bei langen Tiefschlafphasen.
        """
        if len(self.resp_window) < int(RESP_WINDOW_SEC * SAMPLE_HZ * 0.5):
            # Zu wenig Daten im Fenster – noch kein sicheres Urteil
            return True  # Im Zweifel: belegt (konservativ)
        mags = np.array(self.resp_window)
        ratio = respiratory_energy_ratio(mags, SAMPLE_HZ)
        self.last_resp_ratio = ratio
        return ratio > RESP_EXIT_THRESH

    def _is_variance_active(self) -> bool:
        """Sekundäre Prüfung: Kurzzeitige Varianz (für Entry-Erkennung)."""
        var = running_variance(self.var_window)
        thresh_sq = (self.presence_thresh ** 2 / self.baseline_std
                     if self.baseline_std > 0 else self.presence_thresh)
        return var > thresh_sq

    def _start_session(self, ts: float):
        self.session_id    = self.db.create_session(start_time=ts)
        self.session_start = ts
        self.row_count     = 0
        self.bg_analyzer.set_session(self.session_id)
        now_str = datetime.fromtimestamp(ts).strftime('%H:%M:%S')
        print(f"\n[{now_str}] BETT BELEGT → Session {self.session_id[:8]} gestartet")

    def _end_session(self, ts: float):
        if self.session_id is None:
            return
        self.bg_analyzer.set_session(None)
        duration = ts - (self.session_start or ts)

        if duration < MIN_SESSION_SECONDS:
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
                  f"beendet ({duration/60:.1f} min, {self.row_count} Samples)")
            if self.auto_analyze and self.row_count >= MIN_SAMPLES_FOR_ANALYSIS:
                print("Abschluss-Analyse...")
                try:
                    analyze_session(self.session_id, self.db)
                    print("Analyse abgeschlossen.")
                except Exception as e:
                    print(f"Analyse-Fehler: {e}")

        self.session_id    = None
        self.session_start = None
        self.row_count     = 0

    def _calibrate(self, mag: float, ts: float):
        """60-Sekunden Baseline-Kalibrierung bei leerem Bett."""
        if self.calib_start is None:
            self.calib_start = ts
            print(f"Kalibrierung gestartet ({CALIBRATION_SECONDS}s) – "
                  f"Bitte sicherstellen, dass das Bett LEER ist!", flush=True)

        self.calib_buffer.append(mag)
        elapsed = ts - self.calib_start

        if len(self.calib_buffer) % (SAMPLE_HZ * 10) == 0:
            print(f"  Kalibrierung: {elapsed:.0f}/{CALIBRATION_SECONDS}s...", flush=True)

        if elapsed >= CALIBRATION_SECONDS:
            import statistics
            buf  = np.array(self.calib_buffer)
            mean = float(np.mean(buf))
            std  = float(np.std(buf))

            # Atemenergie-Baseline im leeren Bett (muss < RESP_EXIT_THRESH sein)
            resp_baseline = respiratory_energy_ratio(buf, SAMPLE_HZ)

            presence_thresh = mean + PRESENCE_SIGMA * std
            activity_scale  = mean + ACTIVITY_SIGMA * std

            self.baseline_mean   = mean
            self.baseline_std    = std
            self.presence_thresh = presence_thresh
            self.activity_scale  = activity_scale
            self.resp_baseline   = resp_baseline
            self.calibrated      = True

            # In DB speichern (Tabelle hat noch kein resp_baseline Feld – als note)
            self.db.save_calibration(
                baseline_mean   = mean,
                baseline_std    = std,
                presence_thresh = presence_thresh,
                activity_scale  = activity_scale,
                sample_rate     = SAMPLE_HZ,
                notes           = (f"Auto-Kalibrierung {datetime.now().strftime('%d.%m.%Y %H:%M')}, "
                                   f"resp_baseline={resp_baseline:.4f}")
            )

            print(f"Kalibrierung abgeschlossen:")
            print(f"  Baseline:         {mean:.1f} ± {std:.1f} raw")
            print(f"  Präsenz-Schwellw: {presence_thresh:.1f}")
            print(f"  Atemenergie leer: {resp_baseline:.4f} (Bett leer = kein Atmen)")
            print(f"  Aktivitätsskala:  {activity_scale:.1f}")

            if resp_baseline > RESP_EXIT_THRESH:
                print(f"  WARNUNG: Atemenergie-Baseline ({resp_baseline:.4f}) > "
                      f"Exit-Schwellwert ({RESP_EXIT_THRESH})!")
                print(f"  Mögliche Ursache: Umgebungsvibrationen (Lüfter, Straße).")
                print(f"  → RESP_EXIT_THRESH in collector.py erhöhen oder Sensor befestigen.")

            self.state        = BedState.EMPTY
            self.calib_buffer = []

    def _detect_footstep(self, mag: float, ts: float) -> bool:
        """Schrittvibrations-Vorwarnung (kurze Hochfrequenz-Impulse)."""
        if not STEP_DETECTION or self.last_mag < 0:
            self.last_mag = mag
            return False
        scale = max(self.activity_scale, 1.0)
        delta = abs(mag - self.last_mag) / scale
        self.last_mag = mag
        if delta > STEP_IMPULSE_THRESH:
            self.step_times.append(ts)
        cutoff = ts - STEP_WINDOW_SEC
        self.step_times = [t for t in self.step_times if t > cutoff]
        return len(self.step_times) >= STEP_MIN_COUNT

    def process_sample(self, millis: int,
                       ax: int, ay: int, az: int,
                       gx: int, gy: int, gz: int):
        ts  = self.wall_time(millis)
        mag = magnitude(ax, ay, az)

        # ── Kalibrierung ─────────────────────────────────────────────────────
        if self.state == BedState.CALIBRATING:
            self._calibrate(mag, ts)
            return

        # ── Datenpuffer aktualisieren ────────────────────────────────────────
        self.var_window.append(mag)
        self.resp_window.append(mag)

        # ── Zustandsübergänge ────────────────────────────────────────────────

        if self.state == BedState.EMPTY:
            footstep = self._detect_footstep(mag, ts)
            if footstep:
                now_str = datetime.fromtimestamp(ts).strftime('%H:%M:%S')
                print(f"\n[{now_str}] Schritte erkannt (Vorwarnung)...", flush=True)
                self.step_times = []

            # Entry: Varianz-basiert (schnell)
            if self._is_variance_active():
                self.state           = BedState.CANDIDATE_ENTRY
                self.candidate_start = ts

        elif self.state == BedState.CANDIDATE_ENTRY:
            if not self._is_variance_active():
                # Fehlalarm
                self.state           = BedState.EMPTY
                self.candidate_start = None
            elif ts - self.candidate_start >= CONFIRM_ENTRY_SEC:
                # Bestätigt: Person im Bett
                self.state = BedState.OCCUPIED
                self._start_session(self.candidate_start)
                self.candidate_start = None

        elif self.state == BedState.OCCUPIED:
            # Daten speichern
            if self.session_id:
                self.db.insert_raw(self.session_id, ts, ax, ay, az, gx, gy, gz)
                self.row_count  += 1
                self.commit_count += 1
                if self.commit_count >= COMMIT_EVERY:
                    self.db.flush()
                    self.commit_count = 0

            # Exit-Prüfung: NUR wenn Varianz niedrig
            # (verhindert ständige teure FFT-Berechnung)
            if not self._is_variance_active():
                # Nur alle 5 Sekunden die Atemfrequenz prüfen (spart CPU)
                if ts - self.last_exit_check_ts >= 5.0:
                    self.last_exit_check_ts = ts
                    resp_present = self._is_resp_occupied()
                    if not resp_present:
                        # Weder Varianz noch Atemfrequenz → Bett-Verlassen-Kandidat
                        if self.candidate_start is None:
                            self.candidate_start = ts
                            exit_sec = get_confirm_exit_sec()
                            period_str = "Nacht" if is_night() else "Tag"
                            now_str = datetime.fromtimestamp(ts).strftime('%H:%M:%S')
                            print(f"\n[{now_str}] Möglicher Bett-Austritt erkannt "
                                  f"({period_str}: {exit_sec//60} min Bestätigung nötig)...",
                                  flush=True)
                        elif ts - self.candidate_start >= get_confirm_exit_sec():
                            # Zeit abgelaufen: Bett verlassen bestätigt
                            self.state = BedState.CANDIDATE_EXIT
                    else:
                        # Atmung erkannt → noch im Bett (z.B. Tiefschlaf!)
                        if self.candidate_start is not None:
                            now_str = datetime.fromtimestamp(ts).strftime('%H:%M:%S')
                            print(f"\n[{now_str}] Atmung erkannt – Person schläft noch "
                                  f"(RespE={self.last_resp_ratio:.3f}). "
                                  f"Kein Austritt.", flush=True)
                        self.candidate_start = None
            else:
                # Varianz wieder aktiv → Kandidat zurücksetzen
                self.candidate_start = None

        elif self.state == BedState.CANDIDATE_EXIT:
            # Doppelte Prüfung: Atembewegung zurückgekehrt?
            resp_present = self._is_resp_occupied()
            if resp_present or self._is_variance_active():
                # Jemand ist noch/wieder im Bett
                now_str = datetime.fromtimestamp(ts).strftime('%H:%M:%S')
                print(f"\n[{now_str}] Rückkehr erkannt – Bett wieder belegt.",
                      flush=True)
                self.state           = BedState.OCCUPIED
                self.candidate_start = None
            else:
                # Bett verlassen bestätigt
                self.state = BedState.EMPTY
                self._end_session(ts)
                self.candidate_start = None

        # ── Fortschrittsanzeige (jede Sekunde) ───────────────────────────────
        self.display_counter += 1
        if self.display_counter >= SAMPLE_HZ:
            self.display_counter = 0
            ts_str = datetime.fromtimestamp(ts).strftime('%H:%M:%S')
            night_marker = ' N' if is_night() else ' T'
            state_str = {
                BedState.EMPTY:           'LEER     ',
                BedState.CANDIDATE_ENTRY: 'KOMMT... ',
                BedState.OCCUPIED:        'BELEGT   ',
                BedState.CANDIDATE_EXIT:  'GEHT?... ',
            }.get(self.state, '?        ')
            print(
                f"\r[{ts_str}{night_marker}] {state_str} | "
                f"RespE: {self.last_resp_ratio:.3f} | "
                f"Samples: {self.row_count}",
                end='', flush=True
            )

    def run(self, baud: int = BAUD_RATE):
        """Haupt-Leseschleife mit Reconnect-Logik."""
        try:
            ser = serial.Serial(self.port, baud, timeout=3)
        except serial.SerialException as e:
            print(f"FEHLER: {e}")
            sys.exit(1)

        print(f"Port:  {self.port} ({baud} baud)")
        if self.calibrated:
            print(f"Status: Kalibrierung vorhanden")
        else:
            print(f"Status: Keine Kalibrierung → {CALIBRATION_SECONDS}s Baseline messen")
        print(f"Exit-Timeout: {CONFIRM_EXIT_DAY_SEC}s (Tag) / "
              f"{CONFIRM_EXIT_NIGHT_SEC}s (Nacht)")
        print("Warte auf Arduino... (Ctrl+C zum Beenden)\n")

        # Auf READY warten
        while True:
            raw = ser.readline()
            if not raw:
                continue
            line = raw.decode('utf-8', errors='ignore').strip()
            if line.startswith('READY'):
                mpu_ok = "OK" if line == "READY" else "NICHT GEFUNDEN"
                print(f"Arduino bereit (MPU-6050: {mpu_ok})")
                break

        # Haupt-Loop
        while self.running:
            try:
                raw = ser.readline()
                if not raw:
                    continue
                line = raw.decode('utf-8', errors='ignore').strip()

                if line.startswith('EVENT,BED_MOTION,'):
                    now_str = datetime.now().strftime('%H:%M:%S')
                    print(f"\n[{now_str}] Arduino: Starke Bewegung", flush=True)
                    continue

                parsed = parse_line(line)
                if parsed is None:
                    continue

                millis, ax, ay, az, gx, gy, gz = parsed
                self.process_sample(millis, ax, ay, az, gx, gy, gz)

            except serial.SerialException as e:
                print(f"\nSerial-Fehler: {e} – Reconnect in 5s...")
                time.sleep(5)
                try:
                    ser.close()
                    ser = serial.Serial(self.port, baud, timeout=3)
                    print("Neu verbunden.")
                except serial.SerialException:
                    pass
            except Exception as e:
                print(f"\nFehler: {e}")

        # Aufräumen
        self.bg_analyzer.stop()
        if self.session_id:
            self.db.flush()
            self.db.end_session(self.session_id)
        self.db.flush()
        if ser and ser.is_open:
            ser.close()
        print("\nCollector beendet.")


# ── Entry-Point ───────────────────────────────────────────────────────────────

def main():
    print("""
╔══════════════════════════════════════════╗
║   Schlafschaf v2 – Sleep Tracker         ║
║   24/7 Betterkennungs-Modus              ║
╚══════════════════════════════════════════╝
""")

    parser = argparse.ArgumentParser(description='Schlafschaf Collector v2')
    parser.add_argument('--port',       help='Serial-Port (z.B. /dev/ttyACM0)')
    parser.add_argument('--baud',       type=int, default=BAUD_RATE)
    parser.add_argument('--db',         default=None)
    parser.add_argument('--calibrate',  action='store_true',
                        help='Neu kalibrieren (leeres Bett!)')
    parser.add_argument('--no-analyze', action='store_true',
                        help='Keine automatische Analyse')
    parser.add_argument('--no-powersave', action='store_true',
                        help='CPU-Energiesparmodus nicht setzen')
    args = parser.parse_args()

    # CPU-Energiesparmodus
    if not args.no_powersave:
        setup_power_saving()

    port = args.port or find_arduino_port()
    if not port:
        print("FEHLER: Kein Arduino-Port. Mit --port angeben.")
        sys.exit(1)

    db        = Database(args.db) if args.db else Database()
    collector = Collector(port, db, auto_analyze=not args.no_analyze)

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
