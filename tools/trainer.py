#!/usr/bin/env python3
"""
Schlafschaf – ML Training Tool (nur für Entwickler)

Dieses Tool dient ausschließlich der Entwicklung:
  1. Trainingsdaten vom Arduino aufzeichnen
  2. Aufnahmen interaktiv labeln (matplotlib)
  3. ML-Modell trainieren und evaluieren
  4. Modell für collector.py exportieren

─────────────────────────────────────────────────────────────────────────────
MODELL-WAHL: Warum MiniROCKET?

  Für Matratzen-Sensor-Daten gibt es keine verwendbaren vortrainierten Modelle:
  - HAR-Datensätze (UCI, PAMAP2, OPPORTUNITY) sind körpergetragen (Handgelenk/
    Taille/Brust) – fundamental anderes Signal als Matratzen-Vibration.
  - SleepKit (Ambiq): Handgelenk-PPG/Accel, nicht übertragbar.
  - Mattress-spezifische open-source Modelle: nicht verfügbar.

  MiniROCKET (Dempster et al. 2021, arXiv:2012.08791) ist ideal:
  - Funktioniert mit 50–200 gelabelten Beispielen (kein Deep Learning nötig)
  - Kein Hyperparameter-Tuning
  - Trainiert in Sekunden (keine GPU nötig)
  - State-of-the-art Genauigkeit auf kleinen Zeitreihen-Datensätzen
  - Open Source: pip install sktime

  Fallback: RandomForest + handgefertigte Features (nur sklearn nötig,
  immer verfügbar, etwas schwächer aber robust)
─────────────────────────────────────────────────────────────────────────────

KLASSEN:
  BED_ENTRY    – Person legt sich ins Bett (Zielereignis!)
  BED_EXIT     – Person verlässt das Bett (Zielereignis!)
  SLEEP_STILL  – Schlafende Person, kaum Bewegung (Negativ-Klasse)
  AWAKE_IN_BED – Wach im Bett, Umdrehen etc. (Negativ-Klasse)
  EMPTY        – Leeres Bett (Negativ-Klasse)

WORKFLOW:
  # 1. Aufnahme starten (Arduino muss verbunden sein)
  python3 trainer.py record --name test01

  # 2. Aufnahme labeln (matplotlib öffnet sich)
  python3 trainer.py label --name test01

  # 3. Alle Aufnahmen anzeigen
  python3 trainer.py list

  # 4. Modell trainieren
  python3 trainer.py train [--model rocket|rf]

  # 5. Modell evaluieren
  python3 trainer.py eval

  # 6. Modell exportieren (dann in collector.py verwendbar)
  python3 trainer.py export
"""

import argparse
import csv
import json
import math
import os
import sys
import time
import warnings
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np

# ── Verzeichnisse ─────────────────────────────────────────────────────────────

TOOL_DIR       = Path(__file__).parent
DATA_DIR       = TOOL_DIR / 'data'
RECORDINGS_DIR = DATA_DIR / 'recordings'
MODELS_DIR     = DATA_DIR / 'models'

for d in (RECORDINGS_DIR, MODELS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# ── Konfiguration ─────────────────────────────────────────────────────────────

BAUD_RATE    = 115200
SAMPLE_HZ    = 50

# Fenster für Feature-Extraktion und Modell-Inferenz
WINDOW_SEC   = 3.0                          # 3 Sekunden
WINDOW_SAMP  = int(WINDOW_SEC * SAMPLE_HZ)  # 150 Samples
STEP_SEC     = 0.5                          # 50% Überlappung
STEP_SAMP    = int(STEP_SEC * SAMPLE_HZ)    # 25 Samples

# Label-Klassen
LABELS = {
    'E': 'BED_ENTRY',
    'X': 'BED_EXIT',
    'S': 'SLEEP_STILL',
    'A': 'AWAKE_IN_BED',
    'B': 'EMPTY',
}
LABEL_COLORS = {
    'BED_ENTRY':    '#2ecc71',  # grün
    'BED_EXIT':     '#e74c3c',  # rot
    'SLEEP_STILL':  '#3498db',  # blau
    'AWAKE_IN_BED': '#f39c12',  # orange
    'EMPTY':        '#95a5a6',  # grau
}

MODEL_FILE   = MODELS_DIR / 'model.pkl'
INFO_FILE    = MODELS_DIR / 'model_info.json'


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

def recording_csv(name: str) -> Path:
    return RECORDINGS_DIR / f'{name}.csv'

def labels_json(name: str) -> Path:
    return RECORDINGS_DIR / f'{name}.labels.json'

def load_recording(name: str) -> tuple[np.ndarray, np.ndarray]:
    """Lädt CSV → (times_sec, data[N,6]) wobei data = [ax,ay,az,gx,gy,gz]."""
    path = recording_csv(name)
    if not path.exists():
        print(f"Aufnahme '{name}' nicht gefunden: {path}")
        sys.exit(1)
    rows = []
    with open(path, newline='') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split(',')
            if len(parts) != 7:
                continue
            try:
                rows.append([float(p) for p in parts])
            except ValueError:
                continue
    if not rows:
        print(f"Keine Daten in {path}")
        sys.exit(1)
    arr   = np.array(rows)
    t_ms  = arr[:, 0]
    t_sec = (t_ms - t_ms[0]) / 1000.0
    data  = arr[:, 1:]   # ax,ay,az,gx,gy,gz
    return t_sec, data

def magnitude(data: np.ndarray) -> np.ndarray:
    """Euklidische Magnitude der Beschleunigung (Spalten 0,1,2)."""
    return np.sqrt(np.sum(data[:, :3] ** 2, axis=1))

def load_labels(name: str) -> list[dict]:
    path = labels_json(name)
    if not path.exists():
        return []
    with open(path) as f:
        return json.load(f)

def save_labels(name: str, labels: list[dict]):
    with open(labels_json(name), 'w') as f:
        json.dump(labels, f, indent=2)
    print(f"  → {len(labels)} Labels gespeichert.")

def list_recordings() -> list[str]:
    return sorted(p.stem for p in RECORDINGS_DIR.glob('*.csv'))


# ── Feature-Extraktion ────────────────────────────────────────────────────────

def features_for_window(window: np.ndarray) -> np.ndarray:
    """
    Handgefertigte Features für ein Zeitfenster [WINDOW_SAMP × 6].
    Wird für den RandomForest-Fallback verwendet.
    MiniROCKET verwendet stattdessen die Rohdaten direkt.

    Features pro Kanal (ax,ay,az,gx,gy,gz) + Magnitude:
      mean, std, min, max, range, RMS, energy, IQR,
      zero-crossing-rate, peak-count, dominant-fft-freq-bin
    → 11 × 7 = 77 Features total
    """
    feats = []
    mag   = np.sqrt(np.sum(window[:, :3] ** 2, axis=1, keepdims=True))
    channels = np.hstack([window, mag])  # 7 Kanäle

    for ch in range(7):
        sig  = channels[:, ch].astype(float)
        mean = np.mean(sig)
        std  = np.std(sig)
        mn   = np.min(sig)
        mx   = np.max(sig)
        rms  = np.sqrt(np.mean(sig ** 2))
        energy    = np.sum(sig ** 2) / len(sig)
        iqr       = float(np.percentile(sig, 75) - np.percentile(sig, 25))
        centered  = sig - mean
        zcr       = np.sum(np.diff(np.sign(centered)) != 0) / len(centered)
        peaks     = int(np.sum(
            (sig[1:-1] > sig[:-2]) & (sig[1:-1] > sig[2:])
        ))
        fft_mag   = np.abs(np.fft.rfft(centered))
        dom_freq  = float(np.argmax(fft_mag[1:]) + 1)  # ignoriere DC

        feats.extend([
            mean, std, mn, mx, mx - mn,
            rms, energy, iqr, zcr, peaks, dom_freq
        ])

    return np.array(feats)


def extract_windows_and_labels(
    recordings: list[str],
    label_filter: list[str] | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Extrahiert Fenster aus allen gelabelten Aufnahmen.

    Returns:
      X_raw   : [N, WINDOW_SAMP, 6]  – Rohdaten für MiniROCKET
      X_feat  : [N, 77]              – handgefertigte Features für RF
      y       : [N]                  – Label-Strings
    """
    X_raw_list  = []
    X_feat_list = []
    y_list      = []

    for name in recordings:
        lbls = load_labels(name)
        if not lbls:
            continue

        t_sec, data = load_recording(name)

        for lbl in lbls:
            if label_filter and lbl['label'] not in label_filter:
                continue

            # Zeitbereich → Sample-Indices
            i_start = int(np.searchsorted(t_sec, lbl['start']))
            i_end   = int(np.searchsorted(t_sec, lbl['end']))

            # Gleitendes Fenster über den gelabelten Bereich
            pos = i_start
            while pos + WINDOW_SAMP <= i_end:
                win = data[pos : pos + WINDOW_SAMP]  # [WINDOW_SAMP, 6]
                if win.shape[0] == WINDOW_SAMP:
                    X_raw_list.append(win)
                    X_feat_list.append(features_for_window(win))
                    y_list.append(lbl['label'])
                pos += STEP_SAMP

    if not X_raw_list:
        return np.empty((0,)), np.empty((0,)), np.empty((0,), dtype=str)

    X_raw  = np.array(X_raw_list,  dtype=np.float32)   # [N, 150, 6]
    X_feat = np.array(X_feat_list, dtype=np.float64)   # [N, 77]
    y      = np.array(y_list)
    return X_raw, X_feat, y


# ── Subkommandos ──────────────────────────────────────────────────────────────

# ── record ────────────────────────────────────────────────────────────────────

def cmd_record(args):
    """Rohdaten vom Arduino aufzeichnen."""
    import serial
    import serial.tools.list_ports

    name = args.name
    out  = recording_csv(name)
    if out.exists() and not args.overwrite:
        print(f"Aufnahme '{name}' existiert bereits. --overwrite zum Überschreiben.")
        sys.exit(1)

    # Port finden
    port = args.port
    if not port:
        ports = serial.tools.list_ports.comports()
        for p in ports:
            desc = (p.description or '').lower()
            if any(kw in desc for kw in ['arduino', 'stm32', 'acm', 'usb serial']):
                port = p.device
                break
        if not port and ports:
            port = ports[0].device
    if not port:
        print("Kein Arduino-Port gefunden. Mit --port angeben.")
        sys.exit(1)

    print(f"Port:    {port}")
    print(f"Ausgabe: {out}")
    print(f"Dauer:   {args.duration}s  (0 = unbegrenzt, Ctrl+C zum Stoppen)")
    print()

    try:
        ser = serial.Serial(port, BAUD_RATE, timeout=3)
    except serial.SerialException as e:
        print(f"Fehler: {e}")
        sys.exit(1)

    # Warte auf READY
    deadline = time.time() + 10
    while time.time() < deadline:
        line = ser.readline().decode('utf-8', errors='ignore').strip()
        if line.startswith('READY'):
            print("Arduino bereit.\n")
            break

    count        = 0
    start_wall   = time.time()
    millis_start = None

    print("Aufnahme läuft... (Ctrl+C zum Stoppen)")
    print("─" * 50)

    with open(out, 'w', newline='') as f:
        f.write(f'# Schlafschaf Trainingsaufnahme\n')
        f.write(f'# Name: {name}\n')
        f.write(f'# Datum: {datetime.now().isoformat()}\n')
        f.write(f'# Format: millis,ax,ay,az,gx,gy,gz\n')
        writer = csv.writer(f)

        try:
            while True:
                raw = ser.readline()
                if not raw:
                    continue
                line = raw.decode('utf-8', errors='ignore').strip()
                parts = line.split(',')
                if len(parts) != 7:
                    continue
                try:
                    vals = [int(p) for p in parts]
                except ValueError:
                    continue

                writer.writerow(vals)
                count += 1

                if millis_start is None:
                    millis_start = vals[0]

                elapsed = time.time() - start_wall
                if count % SAMPLE_HZ == 0:
                    mag = math.sqrt(vals[1]**2 + vals[2]**2 + vals[3]**2)
                    print(f"\r  {elapsed:6.1f}s | {count:6d} Samples | "
                          f"Mag: {mag:7.0f}", end='', flush=True)

                if args.duration > 0 and elapsed >= args.duration:
                    break

        except KeyboardInterrupt:
            pass

    ser.close()
    elapsed = time.time() - start_wall
    print(f"\n\nAufnahme beendet: {count} Samples, {elapsed:.1f}s")
    print(f"Gespeichert: {out}")
    print(f"\nNächster Schritt: python3 trainer.py label --name {name}")


# ── label ─────────────────────────────────────────────────────────────────────

def cmd_label(args):
    """Interaktiver Labeler mit matplotlib."""
    try:
        import matplotlib
        matplotlib.use('TkAgg' if args.backend == 'tk' else 'Qt5Agg'
                       if args.backend == 'qt' else matplotlib.get_backend())
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
    except ImportError:
        print("matplotlib nicht verfügbar: pip install matplotlib")
        sys.exit(1)

    name   = args.name
    t_sec, data = load_recording(name)
    mag    = magnitude(data)
    labels = load_labels(name)

    print(f"\nAufnahme:  {name}  ({len(t_sec)} Samples, {t_sec[-1]:.1f}s)")
    print(f"Labels:    {len(labels)}")
    print()
    print("Bedienung:")
    print("  1. Klicke ZWEI Punkte im Plot → definiert Zeitbereich")
    print("  2. Gib Kürzel im Terminal ein:")
    for key, lbl in LABELS.items():
        print(f"     {key} = {lbl}  ({LABEL_COLORS[lbl]})")
    print("  D = letztes Label löschen  |  Q = fertig & speichern")
    print()

    fig, axes = plt.subplots(2, 1, figsize=(16, 8), sharex=True,
                              gridspec_kw={'height_ratios': [3, 1]})
    fig.suptitle(f'Schlafschaf Labeler – {name}', fontsize=12)

    def redraw():
        for ax in axes:
            ax.clear()

        # Oben: Magnitude
        axes[0].plot(t_sec, mag, color='#2980b9', linewidth=0.6, alpha=0.8)
        axes[0].set_ylabel('Magnitude (raw)')
        axes[0].grid(True, alpha=0.25)

        # Unten: einzelne Achsen
        axes[1].plot(t_sec, data[:, 0], 'r-', linewidth=0.4, alpha=0.6, label='ax')
        axes[1].plot(t_sec, data[:, 1], 'g-', linewidth=0.4, alpha=0.6, label='ay')
        axes[1].plot(t_sec, data[:, 2], 'b-', linewidth=0.4, alpha=0.6, label='az')
        axes[1].set_ylabel('Accel XYZ')
        axes[1].set_xlabel('Zeit (s)')
        axes[1].legend(loc='upper right', fontsize=7)
        axes[1].grid(True, alpha=0.25)

        # Labels einzeichnen
        patches = []
        shown   = set()
        for lbl in labels:
            color = LABEL_COLORS.get(lbl['label'], '#9b59b6')
            for ax in axes:
                ax.axvspan(lbl['start'], lbl['end'],
                           alpha=0.35, color=color, linewidth=0)
            if lbl['label'] not in shown:
                patches.append(mpatches.Patch(color=color,
                                              label=lbl['label'],
                                              alpha=0.6))
                shown.add(lbl['label'])

        if patches:
            axes[0].legend(handles=patches, loc='upper right', fontsize=8)

        axes[0].set_title(
            f'{len(labels)} Labels  |  Klicke 2 Punkte → Label eingeben  '
            f'|  D=löschen  Q=fertig',
            fontsize=9,
        )
        fig.tight_layout()
        plt.draw()

    redraw()
    plt.show(block=False)

    try:
        while True:
            print("⟩ Klicke 2 Punkte im Plot... ", end='', flush=True)
            try:
                pts = fig.ginput(2, timeout=0)
            except Exception:
                break
            if not pts:
                break

            t0 = min(pts[0][0], pts[1][0])
            t1 = max(pts[0][0], pts[1][0])

            if t1 - t0 < 0.1:
                print("(zu kurz, ignoriert)")
                continue

            print(f"\n  Bereich: {t0:.2f}s – {t1:.2f}s  ({t1-t0:.1f}s)")
            key_str = ', '.join(f'{k}={v}' for k, v in LABELS.items())
            print(f"  Label ({key_str}, D=löschen, Q=fertig): ", end='', flush=True)

            try:
                key = input().strip().upper()
            except EOFError:
                break

            if key == 'Q':
                break
            elif key == 'D':
                if labels:
                    removed = labels.pop()
                    print(f"  ✗ Gelöscht: {removed['label']} "
                          f"({removed['start']:.1f}s–{removed['end']:.1f}s)")
                    redraw()
                else:
                    print("  Keine Labels vorhanden.")
            elif key in LABELS:
                lbl_name = LABELS[key]
                labels.append({'start': round(t0, 3),
                               'end':   round(t1, 3),
                               'label': lbl_name})
                print(f"  ✓ {lbl_name} ({t0:.2f}s–{t1:.2f}s)")
                redraw()
            else:
                print(f"  Unbekanntes Kürzel: '{key}'")

    except KeyboardInterrupt:
        pass

    save_labels(name, labels)
    plt.close('all')

    # Zusammenfassung
    print()
    counts = Counter(l['label'] for l in labels)
    print("Labels gespeichert:")
    for lbl, n in sorted(counts.items()):
        print(f"  {lbl:20s} {n:3d}×")
    print(f"\nNächster Schritt: python3 trainer.py train")


# ── list ──────────────────────────────────────────────────────────────────────

def cmd_list(args):
    """Alle Aufnahmen anzeigen."""
    recs = list_recordings()
    if not recs:
        print("Keine Aufnahmen vorhanden.")
        print(f"Aufnahmen kommen in: {RECORDINGS_DIR}")
        return

    print(f"{'Name':<25} {'Samples':>8} {'Dauer':>8}  {'Labels':>7}  Klassen")
    print("─" * 75)
    total_windows = 0
    for name in recs:
        try:
            t, _ = load_recording(name)
            n_samples = len(t)
            duration  = f'{t[-1]:.0f}s'
        except Exception:
            n_samples = 0
            duration  = '?'

        lbls    = load_labels(name)
        n_lbls  = len(lbls)
        counts  = Counter(l['label'] for l in lbls)
        class_s = '  '.join(f'{k[:3]}:{v}' for k, v in sorted(counts.items()))
        print(f"  {name:<23} {n_samples:>8} {duration:>8}  {n_lbls:>7}  {class_s}")

    # Gesamte Fenster schätzen
    print()
    all_lbls = []
    for name in recs:
        all_lbls.extend(load_labels(name))
    if all_lbls:
        total_sec    = sum(l['end'] - l['start'] for l in all_lbls)
        est_windows  = int(total_sec / STEP_SEC)
        counts       = Counter(l['label'] for l in all_lbls)
        print(f"Gesamt: {len(all_lbls)} Labels, ~{est_windows} Fenster")
        print("Klassen:")
        for lbl, n in sorted(counts.items()):
            print(f"  {lbl:20s} {n:3d} Labels")

    # Modell-Status
    print()
    if MODEL_FILE.exists() and INFO_FILE.exists():
        with open(INFO_FILE) as f:
            info = json.load(f)
        print(f"Modell: {info.get('model_type')}  "
              f"(trainiert {info.get('trained_at', '?')[:16]}  "
              f"Acc={info.get('accuracy', 0)*100:.1f}%)")
    else:
        print("Modell: noch nicht trainiert")


# ── train ─────────────────────────────────────────────────────────────────────

def cmd_train(args):
    """Modell trainieren."""
    from sklearn.model_selection import StratifiedKFold, cross_val_score
    from sklearn.preprocessing   import LabelEncoder
    from sklearn.pipeline        import Pipeline
    import joblib

    recs = list_recordings()
    print(f"Aufnahmen mit Labels:")
    labeled = [n for n in recs if load_labels(n)]
    for n in labeled:
        lbls = load_labels(n)
        print(f"  {n}: {len(lbls)} Labels")
    if not labeled:
        print("Keine gelabelten Aufnahmen. Erst labeln!")
        sys.exit(1)

    print(f"\nFenster extrahieren (Größe={WINDOW_SEC}s, Schritt={STEP_SEC}s)...")
    X_raw, X_feat, y = extract_windows_and_labels(labeled)

    if len(y) == 0:
        print("Keine Fenster extrahiert (Labels zu kurz oder Daten fehlen).")
        sys.exit(1)

    counts = Counter(y)
    print(f"\n{len(y)} Fenster:")
    for lbl, n in sorted(counts.items()):
        print(f"  {lbl:20s} {n:4d}")

    if len(counts) < 2:
        print("\nMindestens 2 Klassen zum Trainieren nötig.")
        sys.exit(1)

    # Kleine Klassen warnen
    min_count = min(counts.values())
    if min_count < 10:
        print(f"\n⚠ Warnung: Klasse '{min(counts, key=counts.get)}' hat nur "
              f"{min_count} Fenster. Mehr Trainingsdaten sammeln!")

    le = LabelEncoder()
    y_enc = le.fit_transform(y)

    model_type = args.model

    # ── MiniROCKET (bevorzugt) ─────────────────────────────────────────────
    if model_type in ('rocket', 'auto'):
        try:
            from sktime.transformations.panel.rocket import MiniRocketMultivariate
            from sklearn.linear_model import RidgeClassifierCV

            print("\nModell: MiniROCKET + RidgeClassifier")
            print("  (Dempster et al. 2021, funktioniert mit kleinen Datensätzen)")

            # sktime erwartet: (n_instances, n_columns, n_timepoints)
            X_sktime = X_raw.transpose(0, 2, 1)  # [N, 6, 150]

            pipe = Pipeline([
                ('rocket', MiniRocketMultivariate(num_kernels=10_000)),
                ('scaler', __import__('sklearn.preprocessing',
                                      fromlist=['StandardScaler'])
                            .StandardScaler(with_mean=False)),
                ('clf',    RidgeClassifierCV(alphas=np.logspace(-3, 3, 10))),
            ])

            # Cross-Validation
            cv_scores = cross_val_score(
                pipe, X_sktime, y_enc,
                cv=StratifiedKFold(n_splits=min(5, min_count), shuffle=True,
                                   random_state=42),
                scoring='f1_macro',
            )
            print(f"  Cross-Val F1 (macro): {cv_scores.mean():.3f} "
                  f"± {cv_scores.std():.3f}")

            # Finales Modell auf allen Daten
            pipe.fit(X_sktime, y_enc)
            model_obj  = pipe
            input_mode = 'sktime'
            model_type = 'MiniROCKET'

        except ImportError:
            print("  sktime nicht installiert → Fallback auf RandomForest")
            print("  (pip install sktime für MiniROCKET)")
            model_type = 'rf'

    # ── RandomForest (Fallback) ───────────────────────────────────────────
    if model_type == 'rf':
        from sklearn.ensemble      import RandomForestClassifier
        from sklearn.preprocessing import StandardScaler

        print("\nModell: RandomForest + handgefertigte Features")

        pipe = Pipeline([
            ('scaler', StandardScaler()),
            ('clf',    RandomForestClassifier(
                n_estimators = 300,
                max_features = 'sqrt',
                class_weight = 'balanced',
                random_state = 42,
                n_jobs       = -1,
            )),
        ])

        cv_scores = cross_val_score(
            pipe, X_feat, y_enc,
            cv=StratifiedKFold(n_splits=min(5, min_count), shuffle=True,
                               random_state=42),
            scoring='f1_macro',
        )
        print(f"  Cross-Val F1 (macro): {cv_scores.mean():.3f} "
              f"± {cv_scores.std():.3f}")

        pipe.fit(X_feat, y_enc)
        model_obj  = pipe
        input_mode = 'features'
        model_type = 'RandomForest'

    # ── Modell speichern ──────────────────────────────────────────────────
    joblib.dump(
        {'model': model_obj, 'label_encoder': le, 'input_mode': input_mode,
         'window_samp': WINDOW_SAMP, 'step_samp': STEP_SAMP,
         'sample_hz': SAMPLE_HZ},
        MODEL_FILE,
    )

    acc = cv_scores.mean()
    info = {
        'model_type':    model_type,
        'input_mode':    input_mode,
        'trained_at':    datetime.now().isoformat(),
        'n_windows':     int(len(y)),
        'classes':       le.classes_.tolist(),
        'class_counts':  {k: int(v) for k, v in counts.items()},
        'cv_f1_macro':   float(cv_scores.mean()),
        'cv_f1_std':     float(cv_scores.std()),
        'accuracy':      float(acc),
        'window_sec':    WINDOW_SEC,
        'step_sec':      STEP_SEC,
        'sample_hz':     SAMPLE_HZ,
    }
    with open(INFO_FILE, 'w') as f:
        json.dump(info, f, indent=2)

    print(f"\n✓ Modell gespeichert: {MODEL_FILE}")
    print(f"  Typ:      {model_type}")
    print(f"  Klassen:  {le.classes_.tolist()}")
    print(f"  F1 macro: {acc:.3f}")
    print(f"\nNächster Schritt: python3 trainer.py eval")


# ── eval ──────────────────────────────────────────────────────────────────────

def cmd_eval(args):
    """Modell detailliert evaluieren (Konfusionsmatrix, F1 pro Klasse)."""
    import joblib
    from sklearn.metrics import (
        classification_report, confusion_matrix, ConfusionMatrixDisplay
    )
    from sklearn.model_selection import StratifiedKFold

    if not MODEL_FILE.exists():
        print("Kein Modell gefunden. Erst: python3 trainer.py train")
        sys.exit(1)

    with open(INFO_FILE) as f:
        info = json.load(f)

    print(f"Modell:   {info['model_type']}")
    print(f"Trainiert {info['trained_at'][:16]}")
    print()

    recs    = [n for n in list_recordings() if load_labels(n)]
    X_raw, X_feat, y = extract_windows_and_labels(recs)

    saved   = joblib.load(MODEL_FILE)
    le      = saved['label_encoder']
    model   = saved['model']
    mode    = saved['input_mode']

    if mode == 'sktime':
        X = X_raw.transpose(0, 2, 1)
    else:
        X = X_feat

    y_enc  = le.transform(y)
    y_pred = model.predict(X)

    print(classification_report(
        y_enc, y_pred,
        target_names=le.classes_,
        digits=3,
    ))

    cm = confusion_matrix(y_enc, y_pred)
    print("Konfusionsmatrix:")
    print(f"{'':20s}", end='')
    for cls in le.classes_:
        print(f"{cls[:10]:>12}", end='')
    print()
    for i, cls in enumerate(le.classes_):
        print(f"{cls[:20]:20s}", end='')
        for j in range(len(le.classes_)):
            print(f"{cm[i,j]:>12}", end='')
        print()

    # Visualisierung optional
    try:
        import matplotlib.pyplot as plt
        disp = ConfusionMatrixDisplay(
            confusion_matrix=cm,
            display_labels=le.classes_,
        )
        disp.plot(cmap='Blues', xticks_rotation=30)
        plt.title(f'{info["model_type"]} – Konfusionsmatrix')
        plt.tight_layout()
        out = MODELS_DIR / 'confusion_matrix.png'
        plt.savefig(out, dpi=150)
        plt.close()
        print(f"\nKonfusionsmatrix gespeichert: {out}")
    except Exception:
        pass


# ── export ────────────────────────────────────────────────────────────────────

def cmd_export(args):
    """
    Modell exportieren → in collector.py nutzbar.

    Das exportierte Modell ersetzt oder ergänzt die regelbasierte
    Bett-Eintritts-/Austritts-Erkennung in collector.py.
    """
    if not MODEL_FILE.exists():
        print("Kein Modell. Erst: python3 trainer.py train")
        sys.exit(1)

    dest = Path(args.output)
    dest.parent.mkdir(parents=True, exist_ok=True)

    import shutil
    shutil.copy(MODEL_FILE, dest)
    shutil.copy(INFO_FILE, dest.with_suffix('.info.json'))

    with open(INFO_FILE) as f:
        info = json.load(f)

    print(f"Modell exportiert: {dest}")
    print(f"Info-Datei:        {dest.with_suffix('.info.json')}")
    print()
    print("In collector.py einbinden:")
    print(f"  from ml_detector import MLDetector")
    print(f"  detector = MLDetector('{dest}')")
    print(f"  label, confidence = detector.predict(window_150_samples)")
    print()
    print("Klassen:")
    for cls in info.get('classes', []):
        print(f"  {cls}")

    # Kleines Wrapper-Skript generieren
    wrapper_path = dest.parent / 'ml_detector.py'
    _write_ml_detector(wrapper_path, info)
    print(f"\nWrapper-Modul generiert: {wrapper_path}")


def _write_ml_detector(path: Path, info: dict):
    """Generiert ein ml_detector.py-Modul für collector.py."""
    code = f'''\
"""
Schlafschaf – ML Ereigniserkennung
Automatisch generiert von trainer.py am {datetime.now():%Y-%m-%d %H:%M}

Modell: {info["model_type"]}
Trainiert: {info["trained_at"][:16]}
F1 macro: {info["cv_f1_macro"]:.3f}
Klassen: {info["classes"]}
"""
import numpy as np
import joblib
from pathlib import Path

_MODEL_FILE = Path(__file__).parent / 'model.pkl'

WINDOW_SAMP = {info["window_sec"] * info["sample_hz"]:.0f}  # {info["window_sec"]}s @ {info["sample_hz"]} Hz
SAMPLE_HZ   = {info["sample_hz"]}

ENTRY_CLASS = 'BED_ENTRY'
EXIT_CLASS  = 'BED_EXIT'


class MLDetector:
    """
    Lädt das trainierte Modell und klassifiziert 3s-Fenster.

    Verwendung in collector.py:
      detector = MLDetector()
      label, conf = detector.predict(np.array([...]))  # shape [150, 6]
      if label == 'BED_ENTRY' and conf > 0.7:
          ...
    """

    def __init__(self, model_path=None):
        p = Path(model_path) if model_path else _MODEL_FILE
        saved = joblib.load(p)
        self.model    = saved['model']
        self.le       = saved['label_encoder']
        self.mode     = saved['input_mode']

    def predict(self, window: np.ndarray) -> tuple[str, float]:
        """
        window: np.ndarray [WINDOW_SAMP, 6] (ax,ay,az,gx,gy,gz, float32)
        Returns: (label_string, confidence_0_to_1)
        """
        if self.mode == 'sktime':
            X = window.T[np.newaxis, :, :]   # [1, 6, 150]
        else:
            from trainer import features_for_window
            X = features_for_window(window)[np.newaxis, :]

        if hasattr(self.model, 'predict_proba'):
            proba = self.model.predict_proba(X)[0]
            idx   = int(np.argmax(proba))
            conf  = float(proba[idx])
        else:
            idx  = int(self.model.predict(X)[0])
            conf = 1.0

        return self.le.inverse_transform([idx])[0], conf
'''
    with open(path, 'w') as f:
        f.write(code)


# ── plot ──────────────────────────────────────────────────────────────────────

def cmd_plot(args):
    """Aufnahme visualisieren (ohne Labeln)."""
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib nicht verfügbar: pip install matplotlib")
        sys.exit(1)

    name   = args.name
    t_sec, data = load_recording(name)
    mag    = magnitude(data)
    labels = load_labels(name)

    fig, axes = plt.subplots(3, 1, figsize=(16, 10), sharex=True)
    fig.suptitle(f'Schlafschaf – {name}', fontsize=13)

    axes[0].plot(t_sec, mag, color='#2980b9', linewidth=0.6)
    axes[0].set_ylabel('Magnitude')
    axes[0].set_title('Accel-Magnitude')

    axes[1].plot(t_sec, data[:,0], 'r-', lw=0.4, label='ax')
    axes[1].plot(t_sec, data[:,1], 'g-', lw=0.4, label='ay')
    axes[1].plot(t_sec, data[:,2], 'b-', lw=0.4, label='az')
    axes[1].set_ylabel('Accel XYZ')
    axes[1].legend(fontsize=8)

    axes[2].plot(t_sec, data[:,3], 'r-', lw=0.4, label='gx')
    axes[2].plot(t_sec, data[:,4], 'g-', lw=0.4, label='gy')
    axes[2].plot(t_sec, data[:,5], 'b-', lw=0.4, label='gz')
    axes[2].set_ylabel('Gyro XYZ')
    axes[2].set_xlabel('Zeit (s)')
    axes[2].legend(fontsize=8)

    for lbl in labels:
        color = LABEL_COLORS.get(lbl['label'], '#9b59b6')
        for ax in axes:
            ax.axvspan(lbl['start'], lbl['end'], alpha=0.3, color=color)
            ax.text((lbl['start'] + lbl['end']) / 2, ax.get_ylim()[1] * 0.9,
                    lbl['label'][:3], ha='center', fontsize=7, color='black')

    for ax in axes:
        ax.grid(True, alpha=0.2)

    plt.tight_layout()
    out = RECORDINGS_DIR / f'{name}_plot.png'
    plt.savefig(out, dpi=150)
    print(f"Plot gespeichert: {out}")
    plt.show()


# ── Entry-Point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Schlafschaf ML Training Tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest='cmd', required=True)

    # record
    p_rec = sub.add_parser('record', help='Trainingsdaten aufzeichnen')
    p_rec.add_argument('--name',      required=True, help='Name der Aufnahme')
    p_rec.add_argument('--port',      help='Serial-Port (z.B. /dev/ttyACM0)')
    p_rec.add_argument('--duration',  type=float, default=0,
                       help='Aufnahmedauer in Sekunden (0 = unbegrenzt)')
    p_rec.add_argument('--overwrite', action='store_true')

    # label
    p_lbl = sub.add_parser('label', help='Aufnahme interaktiv labeln')
    p_lbl.add_argument('--name',    required=True)
    p_lbl.add_argument('--backend', default='auto',
                       choices=['auto', 'tk', 'qt'],
                       help='matplotlib Backend')

    # list
    sub.add_parser('list', help='Alle Aufnahmen anzeigen')

    # train
    p_tr = sub.add_parser('train', help='Modell trainieren')
    p_tr.add_argument('--model', default='auto',
                      choices=['auto', 'rocket', 'rf'],
                      help='auto = MiniROCKET wenn verfügbar, sonst RF')

    # eval
    sub.add_parser('eval', help='Modell evaluieren')

    # export
    p_ex = sub.add_parser('export', help='Modell exportieren')
    p_ex.add_argument('--output', default=str(Path(__file__).parent.parent /
                                             'python' / 'model.pkl'),
                      help='Zielpfad für exportiertes Modell')

    # plot
    p_pl = sub.add_parser('plot', help='Aufnahme visualisieren')
    p_pl.add_argument('--name', required=True)

    args = parser.parse_args()

    {
        'record': cmd_record,
        'label':  cmd_label,
        'list':   cmd_list,
        'train':  cmd_train,
        'eval':   cmd_eval,
        'export': cmd_export,
        'plot':   cmd_plot,
    }[args.cmd](args)


if __name__ == '__main__':
    main()
