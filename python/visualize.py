"""
Schlafschaf v2 – Schlafphasen-Visualisierung

Panels:
  1. Hypnogramm (Schlafphasen)
  2. Bewegungs-/Aktivitätsscore
  3. Spektrale Energie (Atmung, Herzschlag BCG, Bewegung)

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
import matplotlib.dates as mdates
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
    parser = argparse.ArgumentParser(description='Schlafschaf Visualizer v2')
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
    raw    = db.get_raw_for_session(session['id'])
    db.close()

    if not epochs:
        print("FEHLER: Keine Epochen. Zuerst analyzer.py ausführen.")
        sys.exit(1)

    fig, axes = plt.subplots(3, 1, figsize=(14, 9),
                             gridspec_kw={'height_ratios': [3, 1.5, 1.5]})
    fig.patch.set_facecolor('#1a1a2e')
    for ax in axes:
        ax.set_facecolor('#16213e')
        ax.tick_params(colors='#eee')
        ax.spines[:].set_color('#444')

    ax1, ax2, ax3 = axes

    # ── Panel 1: Hypnogramm ───────────────────────────────────────────────────
    for ep in epochs:
        y      = STAGE_Y[ep['stage']]
        color  = STAGE_COLORS[ep['stage']]
        x_s    = datetime.fromtimestamp(ep['start_time'])
        x_e    = datetime.fromtimestamp(ep['end_time'])
        ax1.fill_betweenx([y, y + 0.85], [x_s, x_s], [x_e, x_e],
                          color=color, alpha=0.85)

    ax1.set_yticks([0.42, 1.42, 2.42, 3.42])
    ax1.set_yticklabels(
        [STAGE_LABELS_DE['DEEP_SLEEP'], STAGE_LABELS_DE['LIGHT_SLEEP'],
         STAGE_LABELS_DE['REM'], STAGE_LABELS_DE['AWAKE']],
        color='#eee', fontsize=9
    )
    ax1.set_ylim(-0.1, 4.1)
    ax1.set_title('Schlafphasen (Hypnogramm)', color='#eee', fontsize=12, pad=8)
    ax1.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    ax1.tick_params(axis='x', colors='#999', labelsize=8)

    # Legende
    patches = [mpatches.Patch(color=STAGE_COLORS[s], label=STAGE_LABELS_DE[s])
               for s in ['AWAKE', 'REM', 'LIGHT_SLEEP', 'DEEP_SLEEP']]
    ax1.legend(handles=patches, loc='upper right', fontsize=8,
               facecolor='#1a1a2e', edgecolor='#444', labelcolor='#eee')

    # ── Panel 2: Aktivitäts-/Bewegungsscore ──────────────────────────────────
    if epochs:
        ep_times  = [datetime.fromtimestamp((ep['start_time'] + ep['end_time']) / 2)
                     for ep in epochs]
        movements = [ep['movement_score'] or 0.0 for ep in epochs]
        ax2.fill_between(ep_times, movements, alpha=0.75, color='#9b59b6')
        ax2.plot(ep_times, movements, color='#ce9dff', linewidth=0.8, alpha=0.9)

        # Schwellwert-Linien
        ax2.axhline(1.0, color='#e74c3c', linestyle='--', linewidth=0.8,
                    label='Wake-Grenze (Cole-Kripke)')
        ax2.axhline(0.10, color='#3498db', linestyle='--', linewidth=0.8,
                    label='Tiefschlaf-Grenze')
        ax2.set_ylabel('Aktivitäts-\nscore (norm.)', color='#eee', fontsize=8)
        ax2.set_ylim(0, max(max(movements) * 1.3, 1.2))
        ax2.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
        ax2.tick_params(axis='x', colors='#999', labelsize=7)
        ax2.tick_params(axis='y', colors='#999', labelsize=7)
        ax2.legend(fontsize=7, facecolor='#1a1a2e', edgecolor='#444', labelcolor='#eee')
        ax2.set_title('Aktivitätsscore (Cole-Kripke Basis)', color='#eee',
                      fontsize=10, pad=4)

    # ── Panel 3: Spektrale Energie (Atmung / BCG / Bewegung) ─────────────────
    # sound_score enthält BCG-Energie (umgewidmet in v2)
    if epochs and epochs[0].get('sound_score') is not None:
        ep_times  = [datetime.fromtimestamp((ep['start_time'] + ep['end_time']) / 2)
                     for ep in epochs]
        bcg_vals  = [ep.get('sound_score') or 0.0 for ep in epochs]
        move_vals = [max(0.0, (ep['movement_score'] or 0.0) * 0.5) for ep in epochs]

        ax3.fill_between(ep_times, bcg_vals, alpha=0.7, color='#1abc9c',
                         label='Herzschlag BCG (0.8–2 Hz)')
        ax3.fill_between(ep_times, move_vals, alpha=0.5, color='#e67e22',
                         label='Bewegungsenergie')
        ax3.set_ylabel('Spektral-\nenergie', color='#eee', fontsize=8)
        ax3.set_ylim(0, 1.0)
        ax3.legend(fontsize=7, facecolor='#1a1a2e', edgecolor='#444', labelcolor='#eee')
        ax3.set_title('Spektrale Energie (BCG & Bewegung)', color='#eee',
                      fontsize=10, pad=4)
    elif raw:
        # Fallback: Rohe Magnitudezeitreihe (subsampled)
        step = max(1, len(raw) // 2000)
        raw_times = [datetime.fromtimestamp(r['timestamp']) for r in raw[::step]]
        mags = [
            ((r['ax'] or 0)**2 + (r['ay'] or 0)**2 + (r['az'] or 0)**2) ** 0.5
            for r in raw[::step]
        ]
        ax3.fill_between(raw_times, mags, alpha=0.6, color='#1abc9c')
        ax3.set_ylabel('Magnitude\n(raw)', color='#eee', fontsize=8)
        ax3.set_title('Beschleunigungsmagnitude', color='#eee', fontsize=10, pad=4)

    ax3.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    ax3.set_xlabel('Uhrzeit', color='#eee', fontsize=9)
    ax3.tick_params(axis='x', colors='#999', labelsize=7)
    ax3.tick_params(axis='y', colors='#999', labelsize=7)

    start_str = datetime.fromtimestamp(session['start_time']).strftime('%d.%m.%Y %H:%M')
    fig.suptitle(f'Schlafschaf – {start_str}', color='#eee', fontsize=13, y=0.98)
    plt.tight_layout(rect=[0, 0, 1, 0.97])
    plt.savefig(args.output, dpi=150, bbox_inches='tight',
                facecolor=fig.get_facecolor())
    print(f"Chart gespeichert: {args.output}")


if __name__ == '__main__':
    main()
