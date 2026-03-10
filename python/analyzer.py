"""
Schlafschaf – Schlafphasen-Analyzer
Klassifiziert Schlafphasen aus Roh-Sensordaten in 30-Sekunden-Epochen.

Algorithmus:
  - Bewegungsscore: Mittlere Änderung der Beschleunigungsmagnitude (normiert 0–1)
  - Klangscore: 75. Perzentile der Soundwerte (normiert 0–1)
  - Phasen: AWAKE, LIGHT_SLEEP, DEEP_SLEEP, REM

Verwendung:
  python3 analyzer.py [--session <uuid>|--latest]
"""

import argparse
import sys
from datetime import datetime, timedelta

import numpy as np

from database import Database

EPOCH_SECONDS = 30
STAGE_COLORS = {
    'AWAKE':       'rot',
    'REM':         'gelb',
    'LIGHT_SLEEP': 'grün',
    'DEEP_SLEEP':  'blau',
}


def compute_movement_score(rows: list) -> float:
    """Mittlere Änderung der 3D-Beschleunigungsmagnitude, normiert 0–1."""
    valid = [r for r in rows if r['ax'] != -1]
    if len(valid) < 2:
        return 0.0
    magnitudes = np.array([
        np.sqrt(r['ax']**2 + r['ay']**2 + r['az']**2)
        for r in valid
    ])
    deltas = np.abs(np.diff(magnitudes))
    return float(np.mean(deltas) / 32768.0)


def compute_sound_score(rows: list) -> float:
    """75. Perzentile der Soundwerte, normiert 0–1."""
    if not rows:
        return 0.0
    sounds = np.array([r['sound'] for r in rows])
    return float(np.percentile(sounds, 75) / 1023.0)


def classify_epoch(movement_score: float, sound_score: float,
                   sleep_onset: float | None, epoch_start: float) -> str:
    """Schlafphase für eine Epoche bestimmen."""
    if movement_score > 0.30 or sound_score > 0.60:
        return 'AWAKE'

    if movement_score < 0.05 and sound_score < 0.15:
        return 'DEEP_SLEEP'

    # REM tritt in 90-Minuten-Zyklen gegen Ende des Zyklus auf
    if sleep_onset is not None:
        minutes_asleep = (epoch_start - sleep_onset) / 60.0
        if minutes_asleep > 0 and (minutes_asleep % 90) > 75:
            return 'REM'

    return 'LIGHT_SLEEP'


def find_sleep_onset(stages: list[str]) -> int | None:
    """Index der ersten von 3 aufeinanderfolgenden Nicht-AWAKE-Epochen."""
    count = 0
    for i, stage in enumerate(stages):
        if stage != 'AWAKE':
            count += 1
            if count >= 3:
                return i - 2
        else:
            count = 0
    return None


def analyze_session(session_id: str, db: Database) -> list[dict]:
    """Schlafphasen für eine Session analysieren und in DB speichern."""
    rows = db.get_raw_for_session(session_id)
    if not rows:
        print(f"FEHLER: Keine Rohdaten für Session {session_id}")
        return []

    # In Epochen aufteilen
    epoch_rows = []
    current_epoch = []
    epoch_start_ts = rows[0]['timestamp']

    for row in rows:
        current_epoch.append(row)
        if row['timestamp'] - epoch_start_ts >= EPOCH_SECONDS:
            epoch_rows.append((epoch_start_ts, row['timestamp'], list(current_epoch)))
            epoch_start_ts = row['timestamp']
            current_epoch = []

    # Letzte unvollständige Epoche hinzufügen (mindestens 10 Samples)
    if len(current_epoch) >= 10:
        epoch_rows.append((epoch_start_ts, current_epoch[-1]['timestamp'], current_epoch))

    if not epoch_rows:
        print("FEHLER: Zu wenige Daten für Epochenanalyse.")
        return []

    # Scores berechnen
    epoch_data = []
    for start, end, epoch_rows_data in epoch_rows:
        mv = compute_movement_score(epoch_rows_data)
        sv = compute_sound_score(epoch_rows_data)
        epoch_data.append({'start': start, 'end': end, 'movement': mv, 'sound': sv})

    # Vorläufige Phasen (ohne sleep_onset)
    stages = []
    for ep in epoch_data:
        stage = classify_epoch(ep['movement'], ep['sound'], None, ep['start'])
        stages.append(stage)

    # Sleep-Onset finden und REM-Klassifizierung verfeinern
    onset_idx = find_sleep_onset(stages)
    sleep_onset_ts = epoch_data[onset_idx]['start'] if onset_idx is not None else None

    final_stages = []
    for i, ep in enumerate(epoch_data):
        stage = classify_epoch(ep['movement'], ep['sound'], sleep_onset_ts, ep['start'])
        final_stages.append(stage)

    # Epochen in DB speichern
    db.delete_epochs_for_session(session_id)
    result = []
    for i, ep in enumerate(epoch_data):
        stage = final_stages[i]
        db.insert_epoch(
            session_id,
            ep['start'], ep['end'],
            stage, ep['movement'], ep['sound']
        )
        result.append({
            'start': ep['start'],
            'end': ep['end'],
            'stage': stage,
            'movement_score': ep['movement'],
            'sound_score': ep['sound'],
        })

    return result


def print_summary(session: dict, epochs: list[dict]):
    """Zusammenfassung der Schlafanalyse ausgeben."""
    stage_minutes = {'AWAKE': 0, 'LIGHT_SLEEP': 0, 'DEEP_SLEEP': 0, 'REM': 0}
    for ep in epochs:
        duration = (ep['end'] - ep['start']) / 60.0
        stage_minutes[ep['stage']] = stage_minutes.get(ep['stage'], 0) + duration

    start_dt = datetime.fromtimestamp(session['start_time'])
    end_dt = datetime.fromtimestamp(session['end_time'] or epochs[-1]['end'])
    total_min = (end_dt - start_dt).total_seconds() / 60

    sleep_min = total_min - stage_minutes.get('AWAKE', 0)
    efficiency = (sleep_min / total_min * 100) if total_min > 0 else 0

    print(f"\n{'='*50}")
    print(f"  SCHLAFANALYSE")
    print(f"{'='*50}")
    print(f"  Start:          {start_dt.strftime('%d.%m.%Y %H:%M')}")
    print(f"  Ende:           {end_dt.strftime('%d.%m.%Y %H:%M')}")
    print(f"  Gesamtdauer:    {total_min:.0f} min ({total_min/60:.1f} h)")
    print(f"  Schlafzeit:     {sleep_min:.0f} min")
    print(f"  Schlafeffizienz:{efficiency:.0f}%")
    print(f"{'─'*50}")
    print(f"  Wach:           {stage_minutes.get('AWAKE', 0):.0f} min")
    print(f"  Leichtschlaf:   {stage_minutes.get('LIGHT_SLEEP', 0):.0f} min")
    print(f"  Tiefschlaf:     {stage_minutes.get('DEEP_SLEEP', 0):.0f} min")
    print(f"  REM:            {stage_minutes.get('REM', 0):.0f} min")
    print(f"{'='*50}\n")


def main():
    parser = argparse.ArgumentParser(description='Schlafschaf Analyzer')
    parser.add_argument('--session', help='Session-UUID')
    parser.add_argument('--latest', action='store_true', help='Neueste Session')
    parser.add_argument('--db', default=None)
    args = parser.parse_args()

    db = Database(args.db) if args.db else Database()

    if args.latest:
        session = db.get_latest_session()
        if not session:
            print("FEHLER: Keine Sessions in der Datenbank.")
            sys.exit(1)
        session_id = session['id']
    elif args.session:
        session_id = args.session
        session = db.get_session(session_id)
        if not session:
            print(f"FEHLER: Session {session_id} nicht gefunden.")
            sys.exit(1)
    else:
        parser.print_help()
        sys.exit(1)

    print(f"Analysiere Session: {session_id}")
    epochs = analyze_session(session_id, db)

    if epochs:
        session = db.get_session(session_id)
        print_summary(session, epochs)
        print(f"{len(epochs)} Epochen gespeichert.")

    db.close()


if __name__ == '__main__':
    main()
