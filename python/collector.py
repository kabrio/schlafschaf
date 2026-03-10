"""
Schlafschaf – Serial-Daten-Collector
Liest CSV-Daten vom Arduino MCU (STM32U585) über die Serial-Bridge
und speichert sie in SQLite.

Läuft auf dem Arduino Uno Q (Debian Linux / QRB2210)

Verwendung:
  python3 collector.py [--port /dev/ttyACM0] [--baud 115200]
"""

import argparse
import signal
import sys
import time
from datetime import datetime

import serial
import serial.tools.list_ports

from database import Database


BANNER = """
╔══════════════════════════════════════╗
║   Schlafschaf – Sleep Tracker        ║
║   Arduino Uno Q Edition              ║
╚══════════════════════════════════════╝
"""


def find_arduino_port() -> str | None:
    """Automatisch den Arduino-Serial-Port finden."""
    ports = serial.tools.list_ports.comports()
    for p in ports:
        if any(kw in (p.description or '').lower()
               for kw in ['arduino', 'stm32', 'acm', 'usb serial']):
            return p.device
    # Fallback: erster verfügbarer Port
    if ports:
        return ports[0].device
    return None


def parse_line(line: str) -> tuple | None:
    """CSV-Zeile parsen: millis,sound,ax,ay,az,gx,gy,gz"""
    parts = line.strip().split(',')
    if len(parts) != 8:
        return None
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def main():
    print(BANNER)

    parser = argparse.ArgumentParser(description='Schlafschaf Serial Collector')
    parser.add_argument('--port', help='Serial-Port (z.B. /dev/ttyACM0)')
    parser.add_argument('--baud', type=int, default=115200)
    parser.add_argument('--db', default=None, help='Pfad zur SQLite-Datenbank')
    args = parser.parse_args()

    port = args.port or find_arduino_port()
    if not port:
        print("FEHLER: Kein Arduino-Port gefunden. Mit --port angeben.")
        sys.exit(1)

    print(f"Port:     {port} ({args.baud} baud)")

    db = Database(args.db) if args.db else Database()
    session_id = db.create_session()
    print(f"Session:  {session_id}")
    print(f"Gestartet: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")
    print("Warte auf Arduino... (Ctrl+C zum Beenden)\n")

    session_start: float | None = None
    millis_at_first: int | None = None
    row_count = 0
    commit_interval = 10

    def on_exit(sig=None, frame=None):
        print(f"\n\nBeendet. {row_count} Samples gespeichert.")
        db.end_session(session_id)
        db.flush()
        db.close()
        print(f"Session {session_id} abgeschlossen.")
        sys.exit(0)

    signal.signal(signal.SIGINT, on_exit)
    signal.signal(signal.SIGTERM, on_exit)

    try:
        ser = serial.Serial(port, args.baud, timeout=5)
    except serial.SerialException as e:
        print(f"FEHLER: Serial-Port konnte nicht geöffnet werden: {e}")
        sys.exit(1)

    # Auf READY warten
    while True:
        raw = ser.readline()
        if not raw:
            continue
        line = raw.decode('utf-8', errors='ignore').strip()
        if line.startswith('READY'):
            mpu_status = "MPU-6050 OK" if line == "READY" else "MPU-6050 NICHT GEFUNDEN"
            print(f"Arduino bereit ({mpu_status})")
            break

    # Daten lesen
    while True:
        raw = ser.readline()
        if not raw:
            continue

        line = raw.decode('utf-8', errors='ignore').strip()
        parsed = parse_line(line)
        if parsed is None:
            continue

        millis, sound, ax, ay, az, gx, gy, gz = parsed

        # Zeitstempel berechnen (Debian-Systemzeit als Basis)
        now = time.time()
        if session_start is None:
            session_start = now
            millis_at_first = millis
            # Session-Startzeit in DB aktualisieren
            db.conn.execute(
                "UPDATE sessions SET start_time = ? WHERE id = ?",
                (session_start, session_id)
            )

        timestamp = session_start + (millis - millis_at_first) / 1000.0

        db.insert_raw(session_id, timestamp, sound, ax, ay, az, gx, gy, gz)
        row_count += 1

        if row_count % commit_interval == 0:
            db.flush()

        # Fortschrittsanzeige
        ts_str = datetime.fromtimestamp(timestamp).strftime('%H:%M:%S')
        move_raw = (ax**2 + ay**2 + az**2) ** 0.5 if ax != -1 else 0
        move_score = min(move_raw / 32768.0, 1.0)
        print(
            f"\r[{ts_str}] "
            f"Sound: {sound:4d} | "
            f"Bewegung: {move_score:.3f} | "
            f"Samples: {row_count}",
            end='', flush=True
        )


if __name__ == '__main__':
    main()
