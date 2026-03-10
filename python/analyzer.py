"""
Schlafschaf v2 – Schlafphasen-Analyzer

Algorithmus-Übersicht:

1. FEATURE EXTRACTION (pro 30-Sekunden-Epoche bei 50 Hz = 1500 Samples/Epoche):
   a) Aktivitätszähler (Aktigraphie-Style):
      - Für jedes aufeinanderfolgende Sample-Paar:
        activity += |mag[i] - mag[i-1]| / calib_scale
      - Sum über Epoche = epoch_activity_count

   b) Spektralanalyse via FFT (Numpy):
      - Atembewegung:   0.1 – 0.5 Hz  (5–30 /min)
      - Herzschlag BCG: 0.8 – 2.0 Hz  (48–120 bpm)
      - Körperbewegung: 2.0 – 10 Hz   (Umdrehen, Aufsetzen)
      - Jede Bande: relative Energie = sum(|FFT|²) / total_energy

   c) Zero-Crossing Rate (ZCR) des demeaned Magnitudesignals:
      - Niedrig bei Tiefschlaf (ruhige, gleichmäßige BCG)
      - Hoch bei Aufwachen/Umdrehen

2. COLE-KRIPKE ALGORITHMUS (adaptiert für Bett-Sensor):
   Originalformel (Cole et al., 1992):
     W = 0.00001 × (106.4×A[-4] + 54.1×A[-3] + 58.3×A[-2] + 74.4×A[-1]
                  + 155.9×A[0] + 89.6×A[+1] + 92.5×A[+2] + 78.4×A[+3] + 72.1×A[+4])
   Wobei A[t] = Aktivitätszähler der Epoche t (normiert)
   W ≥ 1.0 → AWAKE
   W < 1.0 → SLEEP (dann sub-klassifizieren)

3. SCHLAF-SUBKLASSIFIZIERUNG:
   DEEP_SLEEP:  Sehr niedrige Aktivität + hohe Atemenergie-Dominanz
   LIGHT_SLEEP: Mittlere Aktivität oder gemischtes Spektrum
   REM:         Leicht erhöhte Aktivität + niedrige Atemenergie + 90-min-Zyklen

4. AUTO-KALIBRIERUNG:
   - Lädt Baseline aus DB (vom Collector gemessen)
   - Normiert Aktivitätszähler relativ zu Baseline-Rauschen
   - Ohne Kalibrierung: adaptive Normierung aus den leisen Session-Epochen

Verwendung:
  python3 analyzer.py [--session <uuid>|--latest]
"""

import argparse
import sys
from datetime import datetime

import numpy as np

from database import Database

# ── Konfiguration ─────────────────────────────────────────────────────────────

EPOCH_SECONDS   = 30       # Standard: 30s Epochen
SAMPLE_HZ       = 50       # Erwartete Abtastrate (tolerant gegenüber Abweichungen)
MIN_SAMPLES_PER_EPOCH = 10  # Epoche verwerfen wenn zu wenige Samples

# Cole-Kripke Gewichte (Original: Cole et al. 1992, Sleep 15(5):461-9)
CK_WEIGHTS = np.array([106.4, 54.1, 58.3, 74.4, 155.9, 89.6, 92.5, 78.4, 72.1])
CK_SCALE   = 0.00001

# Aktivitätszähler-Schwellwerte (nach Cole-Kripke-Normierung)
CK_WAKE_THRESH = 1.0       # W ≥ 1.0 → AWAKE

# Schlaf-Subklassifizierung (normierte Aktivität 0–1)
DEEP_ACTIVITY_THRESH  = 0.10   # Unter diesem Wert → DEEP_SLEEP Kandidat
LIGHT_ACTIVITY_THRESH = 0.30   # Über diesem Wert  → LIGHT_SLEEP (nicht REM)

# Spektrale Frequenzbänder [Hz]
BAND_RESP_LO  = 0.1    # Atemfrequenz untere Grenze
BAND_RESP_HI  = 0.5    # Atemfrequenz obere Grenze
BAND_BCG_LO   = 0.8    # Herzschlag BCG untere Grenze
BAND_BCG_HI   = 2.0    # Herzschlag BCG obere Grenze
BAND_MOVE_LO  = 2.0    # Körperbewegung untere Grenze
BAND_MOVE_HI  = 10.0   # Körperbewegung obere Grenze (Nyquist 25 Hz)

# Tiefschlaf: Atemenergie-Anteil muss dominant sein
DEEP_RESP_ENERGY_MIN = 0.30    # Min. 30% Energie im Atemband
DEEP_BCG_ENERGY_MIN  = 0.05    # Min. BCG-Signal vorhanden (Herzschlag sichtbar)

# REM: 90-Minuten-Zyklus
REM_CYCLE_MINUTES  = 90
REM_WINDOW_MINUTES = 25    # ±12.5 min um 90-min-Marke

# Hysterese
HYSTERESIS_N = 2           # Min. aufeinanderfolgende Epochen für Phasenwechsel


# ── Feature Extraction ────────────────────────────────────────────────────────

def accel_magnitude(rows: list) -> np.ndarray:
    """Euklidische Magnitude der Beschleunigung für eine Liste von Samples."""
    ax = np.array([r['ax'] or 0 for r in rows], dtype=float)
    ay = np.array([r['ay'] or 0 for r in rows], dtype=float)
    az = np.array([r['az'] or 0 for r in rows], dtype=float)
    return np.sqrt(ax**2 + ay**2 + az**2)


def activity_count(magnitudes: np.ndarray, calib_scale: float) -> float:
    """
    Aktigraphie-Aktivitätszähler: Summe der absoluten Magnitude-Änderungen,
    normiert durch Kalibrierungsskala.
    """
    if len(magnitudes) < 2:
        return 0.0
    diffs = np.abs(np.diff(magnitudes))
    return float(np.sum(diffs)) / max(calib_scale, 1.0)


def spectral_features(magnitudes: np.ndarray, sample_rate: float) -> dict:
    """
    FFT-basierte Spektralanalyse der Magnitudezeitreihe.
    Gibt relative Energieanteile in den Frequenzbändern zurück.
    """
    n = len(magnitudes)
    if n < 10:
        return {'resp': 0.0, 'bcg': 0.0, 'move': 0.0, 'dominant': 'move'}

    # Signal demeanen (DC-Anteil entfernen)
    sig = magnitudes - np.mean(magnitudes)

    # FFT
    fft_vals = np.fft.rfft(sig)
    freqs    = np.fft.rfftfreq(n, d=1.0 / sample_rate)
    power    = np.abs(fft_vals) ** 2

    total_power = np.sum(power)
    if total_power < 1e-10:
        return {'resp': 0.0, 'bcg': 0.0, 'move': 0.0, 'dominant': 'move'}

    def band_energy(lo: float, hi: float) -> float:
        mask = (freqs >= lo) & (freqs < hi)
        return float(np.sum(power[mask])) / total_power

    e_resp = band_energy(BAND_RESP_LO, BAND_RESP_HI)
    e_bcg  = band_energy(BAND_BCG_LO,  BAND_BCG_HI)
    e_move = band_energy(BAND_MOVE_LO, BAND_MOVE_HI)

    dominant = max([('resp', e_resp), ('bcg', e_bcg), ('move', e_move)],
                   key=lambda x: x[1])[0]

    return {'resp': e_resp, 'bcg': e_bcg, 'move': e_move, 'dominant': dominant}


def zero_crossing_rate(magnitudes: np.ndarray) -> float:
    """Zero-Crossing Rate des demeanten Signals (normiert 0–1)."""
    if len(magnitudes) < 2:
        return 0.0
    sig = magnitudes - np.mean(magnitudes)
    crossings = np.sum(np.diff(np.sign(sig)) != 0)
    return float(crossings) / len(sig)


# ── Kalibrierungs-Skalierung ──────────────────────────────────────────────────

def adaptive_calib_scale(all_magnitudes: np.ndarray) -> float:
    """
    Ohne Kalibrierungs-DB: Schätze Kalibrierungsskala aus der Session.
    Nutze das 10. Perzentil der Epochen-Aktivitäten als "ruhige" Baseline.
    """
    return max(float(np.percentile(all_magnitudes, 5)), 1.0)


# ── Cole-Kripke Klassifizierung ───────────────────────────────────────────────

def cole_kripke_wake_score(activity_counts: np.ndarray) -> np.ndarray:
    """
    Berechnet Cole-Kripke Wake-Score für alle Epochen.
    Randbehandlung: Epochen am Anfang/Ende mit verfügbaren Nachbarn auffüllen.
    """
    n = len(activity_counts)
    scores = np.zeros(n)

    # Padding mit Randwerten
    padded = np.pad(activity_counts, (4, 4), mode='edge')

    for i in range(n):
        # Fenster: [i-4 ... i+4] (9 Epochen)
        window = padded[i:i + 9]
        scores[i] = CK_SCALE * float(np.dot(CK_WEIGHTS, window))

    return scores


# ── Schlaf-Subklassifizierung ─────────────────────────────────────────────────

def classify_sleep_stage(norm_activity: float, spectral: dict,
                          minutes_from_onset: float) -> str:
    """
    Subklassifizierung einer als 'SLEEP' erkannten Epoche.

    Reihenfolge der Kriterien:
    1. DEEP_SLEEP: sehr geringe Aktivität + dominante Atemkomponente
    2. REM:        90-Minuten-Zyklen + leicht erhöhte Aktivität + keine dominante Atmung
    3. LIGHT_SLEEP: alles andere
    """
    # Tiefschlaf: kaum Bewegung, Atmung dominiert, BCG sichtbar
    if (norm_activity < DEEP_ACTIVITY_THRESH
            and spectral['resp'] > DEEP_RESP_ENERGY_MIN
            and spectral['bcg'] > DEEP_BCG_ENERGY_MIN):
        return 'DEEP_SLEEP'

    # REM: tritt in 90-min-Zyklen auf
    # Nach Schlafbeginn: erste REM-Phase ~90 min, dann alle ~90 min
    if minutes_from_onset > 0:
        cycle_pos = minutes_from_onset % REM_CYCLE_MINUTES
        half_win  = REM_WINDOW_MINUTES / 2.0
        in_rem_window = (cycle_pos > (REM_CYCLE_MINUTES - half_win)
                         or cycle_pos < half_win)
        if (in_rem_window
                and norm_activity < LIGHT_ACTIVITY_THRESH
                and spectral['resp'] < DEEP_RESP_ENERGY_MIN):  # Weniger ruhige Atmung als Tiefschlaf
            return 'REM'

    return 'LIGHT_SLEEP'


# ── Hysterese-Filter ─────────────────────────────────────────────────────────

def apply_hysteresis(stages: list[str], n: int = HYSTERESIS_N) -> list[str]:
    """
    Verhindert schnelle Phasenwechsel: Ein neuer Zustand muss n aufeinanderfolgende
    Epochen anhalten, bevor er als bestätigt gilt.
    """
    if not stages:
        return stages

    result  = [stages[0]]
    pending = stages[0]
    run_len = 1

    for stage in stages[1:]:
        if stage == pending:
            run_len += 1
        else:
            pending = stage
            run_len = 1

        if run_len >= n:
            result.append(pending)
        else:
            result.append(result[-1])

    return result


# ── Haupt-Analyse ─────────────────────────────────────────────────────────────

def analyze_session(session_id: str, db: Database) -> list[dict]:
    """
    Schlafphasen für eine Session analysieren und in DB speichern.

    Ablauf:
    1. Rohdaten laden
    2. Kalibrierungsskala bestimmen (aus DB oder adaptiv)
    3. 30s-Epochen bilden
    4. Features pro Epoche berechnen
    5. Cole-Kripke-Score berechnen → Wake/Sleep Entscheidung
    6. Sleep-Subklassifizierung (Deep/Light/REM)
    7. Hysterese anwenden
    8. Schlafbeginn (Sleep Onset) bestimmen
    9. REM-Klassifizierung mit relativem Zeitbezug verfeinern
    10. In DB speichern
    """
    rows = db.get_raw_for_session(session_id)
    if not rows:
        print(f"FEHLER: Keine Rohdaten für Session {session_id}")
        return []

    print(f"  Rohdaten: {len(rows)} Samples")

    # ── Kalibrierungsskala bestimmen ─────────────────────────────────────────
    calib = db.get_latest_calibration()
    if calib:
        calib_scale = calib['activity_scale']
        print(f"  Kalibrierung: scale={calib_scale:.1f} (aus DB)")
    else:
        # Adaptive Schätzung aus der Session selbst
        all_mags    = accel_magnitude(rows)
        calib_scale = adaptive_calib_scale(all_mags)
        print(f"  Kalibrierung: scale={calib_scale:.1f} (adaptiv)")

    # ── Epochen bilden ────────────────────────────────────────────────────────
    epoch_starts = []
    epoch_rows_list = []
    session_start_ts = rows[0]['timestamp']
    current_epoch    = []
    epoch_t          = session_start_ts

    for row in rows:
        if row['timestamp'] - epoch_t >= EPOCH_SECONDS:
            if len(current_epoch) >= MIN_SAMPLES_PER_EPOCH:
                epoch_starts.append(epoch_t)
                epoch_rows_list.append(current_epoch)
            epoch_t       = row['timestamp']
            current_epoch = []
        current_epoch.append(row)

    # Letzte Epoche
    if len(current_epoch) >= MIN_SAMPLES_PER_EPOCH:
        epoch_starts.append(epoch_t)
        epoch_rows_list.append(current_epoch)

    if not epoch_starts:
        print("FEHLER: Zu wenige Samples für Epochenanalyse.")
        return []

    n_epochs = len(epoch_starts)
    print(f"  Epochen: {n_epochs} × {EPOCH_SECONDS}s")

    # ── Features pro Epoche berechnen ─────────────────────────────────────────
    activity_counts_raw = np.zeros(n_epochs)
    spectral_list       = []
    epoch_ends          = []

    for i, (epoch_rows_data, t_start) in enumerate(zip(epoch_rows_list, epoch_starts)):
        mags    = accel_magnitude(epoch_rows_data)
        t_end   = epoch_rows_data[-1]['timestamp']
        epoch_ends.append(t_end)

        # Echte Abtastrate dieser Epoche schätzen
        duration = t_end - t_start
        sr       = len(mags) / max(duration, 1.0)

        act      = activity_count(mags, calib_scale)
        spec     = spectral_features(mags, sr)

        activity_counts_raw[i] = act
        spectral_list.append(spec)

    # ── Cole-Kripke Wake-Score ────────────────────────────────────────────────
    ck_scores = cole_kripke_wake_score(activity_counts_raw)

    # ── Erste Klassifizierung (Wake/Sleep) ────────────────────────────────────
    raw_stages = []
    for i in range(n_epochs):
        if ck_scores[i] >= CK_WAKE_THRESH:
            raw_stages.append('AWAKE')
        else:
            raw_stages.append('SLEEP')

    # ── Sleep Onset bestimmen ─────────────────────────────────────────────────
    onset_idx = None
    run = 0
    for i, s in enumerate(raw_stages):
        if s == 'SLEEP':
            run += 1
            if run >= 3:
                onset_idx = i - 2
                break
        else:
            run = 0

    sleep_onset_ts = epoch_starts[onset_idx] if onset_idx is not None else None
    if sleep_onset_ts:
        onset_str = datetime.fromtimestamp(sleep_onset_ts).strftime('%H:%M')
        print(f"  Schlafbeginn: {onset_str} (Epoche {onset_idx})")
    else:
        print("  Schlafbeginn: nicht erkannt")

    # ── Normierte Aktivität für Subklassifizierung ────────────────────────────
    max_act     = max(float(np.max(activity_counts_raw)), 1.0)
    norm_acts   = activity_counts_raw / max_act

    # ── Vollständige Klassifizierung ──────────────────────────────────────────
    final_stages = []
    for i in range(n_epochs):
        if raw_stages[i] == 'AWAKE':
            final_stages.append('AWAKE')
            continue

        minutes_from_onset = 0.0
        if sleep_onset_ts is not None:
            minutes_from_onset = (epoch_starts[i] - sleep_onset_ts) / 60.0

        stage = classify_sleep_stage(
            norm_acts[i],
            spectral_list[i],
            minutes_from_onset
        )
        final_stages.append(stage)

    # ── Hysterese anwenden ────────────────────────────────────────────────────
    final_stages = apply_hysteresis(final_stages, HYSTERESIS_N)

    # ── In DB speichern ───────────────────────────────────────────────────────
    db.delete_epochs_for_session(session_id)
    result = []
    for i in range(n_epochs):
        t_start  = epoch_starts[i]
        t_end    = epoch_ends[i]
        stage    = final_stages[i]
        act_norm = float(norm_acts[i])
        spec     = spectral_list[i]

        # movement_score: normierte Aktivität (0–1)
        # sound_score:    BCG-Energieanteil (0–1) – kein Mikrofon mehr, aber Feld erhalten
        db.insert_epoch(
            session_id,
            t_start, t_end,
            stage,
            movement_score  = act_norm,
            sound_score     = spec['bcg'],   # BCG-Energie statt Klang
            activity_count  = float(activity_counts_raw[i])
        )
        result.append({
            'start':          t_start,
            'end':            t_end,
            'stage':          stage,
            'movement_score': act_norm,
            'bcg_energy':     spec['bcg'],
            'resp_energy':    spec['resp'],
            'activity_count': float(activity_counts_raw[i]),
        })

    return result


# ── Zusammenfassung ───────────────────────────────────────────────────────────

def print_summary(session: dict, epochs: list[dict]):
    stage_minutes = {'AWAKE': 0.0, 'LIGHT_SLEEP': 0.0, 'DEEP_SLEEP': 0.0, 'REM': 0.0}
    for ep in epochs:
        dur = (ep['end'] - ep['start']) / 60.0
        stage_minutes[ep['stage']] = stage_minutes.get(ep['stage'], 0.0) + dur

    start_dt = datetime.fromtimestamp(session['start_time'])
    end_ts   = session['end_time'] or epochs[-1]['end']
    end_dt   = datetime.fromtimestamp(end_ts)
    total    = (end_dt - start_dt).total_seconds() / 60.0
    sleep    = total - stage_minutes.get('AWAKE', 0)
    eff      = (sleep / total * 100) if total > 0 else 0

    print(f"\n{'='*50}")
    print(f"  SCHLAFANALYSE")
    print(f"{'='*50}")
    print(f"  Start:          {start_dt.strftime('%d.%m.%Y %H:%M')}")
    print(f"  Ende:           {end_dt.strftime('%d.%m.%Y %H:%M')}")
    print(f"  Gesamtdauer:    {total:.0f} min ({total/60:.1f} h)")
    print(f"  Schlafzeit:     {sleep:.0f} min")
    print(f"  Schlafeffizienz:{eff:.0f}%")
    print(f"{'─'*50}")
    print(f"  Wach:           {stage_minutes.get('AWAKE', 0):.0f} min")
    print(f"  Leichtschlaf:   {stage_minutes.get('LIGHT_SLEEP', 0):.0f} min")
    print(f"  Tiefschlaf:     {stage_minutes.get('DEEP_SLEEP', 0):.0f} min")
    print(f"  REM:            {stage_minutes.get('REM', 0):.0f} min")
    print(f"{'='*50}\n")


# ── Entry-Point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Schlafschaf Analyzer v2')
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
        session    = db.get_session(session_id)
        if not session:
            print(f"FEHLER: Session {session_id} nicht gefunden.")
            sys.exit(1)
    else:
        parser.print_help()
        sys.exit(1)

    session = db.get_session(session_id)
    print(f"Analysiere Session: {session_id[:8]}...")
    epochs = analyze_session(session_id, db)

    if epochs:
        session = db.get_session(session_id)
        result_epochs = [
            {'start': e['start'], 'end': e['end'], 'stage': e['stage']}
            for e in epochs
        ]
        print_summary(session, result_epochs)
        print(f"{len(epochs)} Epochen gespeichert.")

    db.close()


if __name__ == '__main__':
    main()
