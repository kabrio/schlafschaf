"""
Schlafschaf – JSON-Exporter
Exportiert analysierte Schlafdaten als JSON für die iOS App.

Verwendung:
  python3 export.py [--session <uuid>|--latest] [--output sleep.json]
"""

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta

from database import Database


def unix_to_iso(ts: float) -> str:
    """Unix-Timestamp in ISO 8601 mit lokalem Timezone-Offset konvertieren."""
    dt = datetime.fromtimestamp(ts).astimezone()
    return dt.isoformat()


def build_export(session: dict, epochs: list[dict]) -> dict:
    """JSON-Export-Struktur aufbauen."""
    stage_minutes = {'AWAKE': 0.0, 'LIGHT_SLEEP': 0.0, 'DEEP_SLEEP': 0.0, 'REM': 0.0}
    for ep in epochs:
        duration_min = (ep['end_time'] - ep['start_time']) / 60.0
        stage_minutes[ep['stage']] = stage_minutes.get(ep['stage'], 0.0) + duration_min

    end_ts = session['end_time'] or (epochs[-1]['end_time'] if epochs else session['start_time'])
    duration_min = (end_ts - session['start_time']) / 60.0

    return {
        "version": "1.0",
        "device": "Arduino Uno Q",
        "exported_at": unix_to_iso(
            __import__('time').time()
        ),
        "session": {
            "id": session['id'],
            "start_time": unix_to_iso(session['start_time']),
            "end_time": unix_to_iso(end_ts),
            "duration_minutes": round(duration_min, 1),
        },
        "summary": {
            "awake_minutes": round(stage_minutes.get('AWAKE', 0), 1),
            "light_sleep_minutes": round(stage_minutes.get('LIGHT_SLEEP', 0), 1),
            "deep_sleep_minutes": round(stage_minutes.get('DEEP_SLEEP', 0), 1),
            "rem_minutes": round(stage_minutes.get('REM', 0), 1),
        },
        "epochs": [
            {
                "start": unix_to_iso(ep['start_time']),
                "end": unix_to_iso(ep['end_time']),
                "stage": ep['stage'],
                "movement_score": round(ep['movement_score'] or 0.0, 4),
                "bcg_energy": round(ep['sound_score'] or 0.0, 4),
            }
            for ep in epochs
        ],
    }


def main():
    parser = argparse.ArgumentParser(description='Schlafschaf JSON Exporter')
    parser.add_argument('--session', help='Session-UUID')
    parser.add_argument('--latest', action='store_true')
    parser.add_argument('--output', '-o', help='Ausgabedatei (Standard: stdout)')
    parser.add_argument('--db', default=None)
    args = parser.parse_args()

    db = Database(args.db) if args.db else Database()

    if args.latest:
        session = db.get_latest_session()
        if not session:
            print("FEHLER: Keine Sessions in der Datenbank.")
            sys.exit(1)
    elif args.session:
        session = db.get_session(args.session)
        if not session:
            print(f"FEHLER: Session {args.session} nicht gefunden.")
            sys.exit(1)
    else:
        parser.print_help()
        sys.exit(1)

    epochs = db.get_epochs_for_session(session['id'])
    if not epochs:
        print("WARNUNG: Keine analysierten Epochen. Zuerst analyzer.py ausführen.")
        sys.exit(1)

    export_data = build_export(session, epochs)
    json_str = json.dumps(export_data, ensure_ascii=False, indent=2)

    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(json_str)
        print(f"Exportiert nach: {args.output}")
        print(f"Session: {session['id']}")
        print(f"Epochen: {len(epochs)}")
    else:
        print(json_str)

    db.close()


if __name__ == '__main__':
    main()
