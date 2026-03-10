"""
Schlafschaf – Schlafphasen-Visualisierung
Erstellt ein Matplotlib-Chart der Schlafphasen.

Verwendung:
  python3 visualize.py [--session <uuid>|--latest] [--output chart.png]
"""

import argparse
import sys
from datetime import datetime

import matplotlib
matplotlib.use('Agg')  # Kein Display nötig (läuft auf Uno Q ohne Monitor)
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

from database import Database

STAGE_Y = {
    'AWAKE':       3,
    'REM':         2,
    'LIGHT_SLEEP': 1,
    'DEEP_SLEEP':  0,
}
STAGE_COLORS = {
    'AWAKE':       '#e74c3c',
    'REM':         '#f39c12',
    'LIGHT_SLEEP': '#2ecc71',
    'DEEP_SLEEP':  '#3498db',
}
STAGE_LABELS_DE = {
    'AWAKE':       'Wach',
    'REM':         'REM',
    'LIGHT_SLEEP': 'Leichtschlaf',
    'DEEP_SLEEP':  'Tiefschlaf',
}


def main():
    parser = argparse.ArgumentParser(description='Schlafschaf Visualizer')
    parser.add_argument('--session', help='Session-UUID')
    parser.add_argument('--latest', action='store_true')
    parser.add_argument('--output', '-o', default='schlafschaf_chart.png')
    parser.add_argument('--db', default=None)
    args = parser.parse_args()

    db = Database(args.db) if args.db else Database()

    if args.latest:
        session = db.get_latest_session()
        if not session:
            print("FEHLER: Keine Sessions.")
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
    raw = db.get_raw_for_session(session['id'])
    db.close()

    if not epochs:
        print("FEHLER: Keine Epochen. Zuerst analyzer.py ausführen.")
        sys.exit(1)

    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(14, 8),
                                          gridspec_kw={'height_ratios': [3, 1, 1]})
    fig.patch.set_facecolor('#1a1a2e')
    for ax in (ax1, ax2, ax3):
        ax.set_facecolor('#16213e')
        ax.tick_params(colors='#eee')
        ax.spines[:].set_color('#444')

    # Panel 1: Schlafphasen-Hypnogramm
    for ep in epochs:
        y = STAGE_Y[ep['stage']]
        color = STAGE_COLORS[ep['stage']]
        x_start = datetime.fromtimestamp(ep['start_time'])
        x_end = datetime.fromtimestamp(ep['end_time'])
        ax1.fill_betweenx([y, y + 0.9],
                          [x_start, x_start],
                          [x_end, x_end],
                          color=color, alpha=0.85)

    ax1.set_yticks([0.45, 1.45, 2.45, 3.45])
    ax1.set_yticklabels([STAGE_LABELS_DE['DEEP_SLEEP'],
                         STAGE_LABELS_DE['LIGHT_SLEEP'],
                         STAGE_LABELS_DE['REM'],
                         STAGE_LABELS_DE['AWAKE']], color='#eee', fontsize=9)
    ax1.set_ylim(-0.1, 4.1)
    ax1.set_title('Schlafphasen', color='#eee', fontsize=12, pad=8)
    ax1.xaxis.set_major_formatter(
        matplotlib.dates.DateFormatter('%H:%M')
    )
    ax1.tick_params(axis='x', colors='#999', labelsize=8)

    # Legende
    patches = [mpatches.Patch(color=STAGE_COLORS[s], label=STAGE_LABELS_DE[s])
               for s in ['AWAKE', 'REM', 'LIGHT_SLEEP', 'DEEP_SLEEP']]
    ax1.legend(handles=patches, loc='upper right', fontsize=8,
               facecolor='#1a1a2e', edgecolor='#444', labelcolor='#eee')

    # Panel 2: Bewegungsscore
    if epochs:
        times = [datetime.fromtimestamp((ep['start_time'] + ep['end_time']) / 2)
                 for ep in epochs]
        movements = [ep['movement_score'] or 0 for ep in epochs]
        ax2.fill_between(times, movements, alpha=0.7, color='#9b59b6')
        ax2.set_ylabel('Bewegung', color='#eee', fontsize=8)
        ax2.set_ylim(0, max(movements) * 1.2 if max(movements) > 0 else 1)
        ax2.xaxis.set_major_formatter(matplotlib.dates.DateFormatter('%H:%M'))
        ax2.tick_params(axis='x', colors='#999', labelsize=7)
        ax2.tick_params(axis='y', colors='#999', labelsize=7)

    # Panel 3: Klangpegel
    if raw:
        raw_times = [datetime.fromtimestamp(r['timestamp']) for r in raw[::5]]
        raw_sounds = [r['sound'] for r in raw[::5]]
        ax3.fill_between(raw_times, raw_sounds, alpha=0.7, color='#1abc9c')
        ax3.set_ylabel('Klang', color='#eee', fontsize=8)
        ax3.set_xlabel('Uhrzeit', color='#eee', fontsize=9)
        ax3.set_ylim(0, 1023)
        ax3.xaxis.set_major_formatter(matplotlib.dates.DateFormatter('%H:%M'))
        ax3.tick_params(axis='x', colors='#999', labelsize=7)
        ax3.tick_params(axis='y', colors='#999', labelsize=7)

    start_str = datetime.fromtimestamp(session['start_time']).strftime('%d.%m.%Y %H:%M')
    fig.suptitle(f'Schlafschaf – {start_str}', color='#eee', fontsize=13, y=0.98)
    plt.tight_layout()
    plt.savefig(args.output, dpi=150, bbox_inches='tight',
                facecolor=fig.get_facecolor())
    print(f"Chart gespeichert: {args.output}")


if __name__ == '__main__':
    main()
