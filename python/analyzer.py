"""
Schlafschaf v2 – Schlafphasen-Analyzer

Wissenschaftliche Grundlage:
  - Cole-Kripke Algorithmus (Cole et al. 1992, Sleep 15(5):461-9)
  - BCG-basierte Schlafklassifizierung (Missouri C2SHIP Studie)
  - Spektralanalyse via Butterworth-Bandpassfilter (scipy)

Algorithmus-Pipeline:

1. FEATURE EXTRACTION (pro 30-Sekunden-Epoche, 50 Hz = 1500 Samples):

   a) Bewegungs-Score (PIM – Proportional Integral Mode):
      movement_pim = Σ|mag[i] - mag[i-1]| / Epochendauer
      → normiert durch Kalibrierungsskala

   b) Atemfrequenz (0.1–0.5 Hz Butterworth Bandpass):
      - Dominante Frequenz via Welch-PSD → Atemrate in Atemzüge/min
      - RRV (Respiratory Rate Variability): Variationskoeffizient der
        Atemintervalle. Schlüssel-Feature: REM hat hohe RRV (>0.15),
        Tiefschlaf hat niedrige RRV (<0.08)

   c) BCG-Herzschlag (1.0–8.5 Hz Butterworth Bandpass):
      - J-Peaks via scipy.signal.find_peaks
      - IBI-Reihe → RMSSD, SDNN, mittlere Herzfrequenz
      - Nur auswertbar bei ruhigen Epochen (movement_pim < Schwellwert)

   d) Spektrale Energieanteile (FFT):
      - Atemband 0.1–0.5 Hz
      - BCG-Band 0.8–8.5 Hz
      - Bewegungsband 2.0–10 Hz

2. COLE-KRIPKE ALGORITHMUS (adaptiert für Bett-Sensor):
   W = 0.00001 × Σ(Gewicht[i] × Aktivität[t+i]) für i ∈ {-4..+4}
   Gewichte: [106.4, 54.1, 58.3, 74.4, 155.9, 89.6, 92.5, 78.4, 72.1]
   W ≥ 1.0 → AWAKE, W < 1.0 → Sleep (dann sub-klassifizieren)

3. SCHLAF-SUBKLASSIFIZIERUNG (hierarchisch):
   Priorität 1: DEEP_SLEEP → sehr geringe Bewegung + reguläre Atmung + hoher RMSSD
   Priorität 2: REM → 90-min-Zyklus + hohe RRV + elevated LF/HF
   Priorität 3: LIGHT_SLEEP → alles andere

4. HYSTERESE: Min. 2 aufeinanderfolgende Epochen für Phasenwechsel

Referenzen:
  - Cole et al. (1992): Automatic sleep/wake identification from wrist activity. Sleep.
  - Nikkonen et al. (2022): BCG Using Ballistocardiography. arXiv:2202.01038
  - Tataraidze et al. (2020): Sleep Stage Estimation from Bed Leg BCG. MDPI Sensors.
  - Krejcar et al. (2016): Respiratory Rate Variability During Sleep. PMC5027356.

Verwendung:
  python3 analyzer.py [--session <uuid>|--latest]
"""

import argparse
import sys
from datetime import datetime

import numpy as np
from scipy.signal import butter, filtfilt, welch, find_peaks

from database import Database

# ── Konfiguration ─────────────────────────────────────────────────────────────

EPOCH_SECONDS         = 30       # AASM-Standard: 30s Epochen
SAMPLE_HZ             = 50       # Erwartete Arduino-Abtastrate
MIN_SAMPLES_PER_EPOCH = 10       # Epoche verwerfen wenn weniger Samples

# ── Cole-Kripke Gewichte (Cole et al. 1992) ───────────────────────────────────
# 9-Epochen-Fenster: [t-4, t-3, t-2, t-1, t, t+1, t+2, t+3, t+4]
CK_WEIGHTS   = np.array([106.4, 54.1, 58.3, 74.4, 155.9, 89.6, 92.5, 78.4, 72.1])
CK_SCALE     = 0.00001
CK_WAKE_THRESH = 1.0             # W ≥ 1.0 → AWAKE

# ── Frequenzbänder [Hz] ───────────────────────────────────────────────────────
BAND_RESP_LO  = 0.10   # Atemfrequenz untere Grenze (6 Atemzüge/min)
BAND_RESP_HI  = 0.50   # Atemfrequenz obere Grenze (30 Atemzüge/min)
BAND_BCG_LO   = 1.00   # BCG/Herzschlag untere Grenze
BAND_BCG_HI   = 8.50   # BCG/Herzschlag obere Grenze (Nyquist 25 Hz)
BAND_MOVE_LO  = 2.00   # Körperbewegung untere Grenze
BAND_MOVE_HI  = 10.0   # Körperbewegung obere Grenze

# ── Schlaf-Subklassifizierungs-Schwellwerte ───────────────────────────────────
DEEP_MOVE_THRESH   = 0.10    # Normierte Aktivität < 10% → Tiefschlaf-Kandidat
LIGHT_MOVE_THRESH  = 0.30    # Normierte Aktivität > 30% → kein REM
DEEP_RESP_ENERGY   = 0.25    # Min. 25% Energie im Atemband für Tiefschlaf
DEEP_RMSSD_MULT    = 1.20    # RMSSD > 120% des Session-Medians → Tiefschlaf

RRV_REM_THRESH     = 0.15    # RRV > 0.15 → REM-Kandidat (CoV der Atemintervalle)
RRV_DEEP_THRESH    = 0.08    # RRV < 0.08 → Tiefschlaf-Kandidat

# ── REM 90-Minuten-Zyklen ─────────────────────────────────────────────────────
REM_CYCLE_MINUTES  = 90
REM_WINDOW_MINUTES = 25      # ±12.5 min um jede 90-min-Marke

# ── BCG Herzschlag-Erkennung ──────────────────────────────────────────────────
BCG_MIN_HR_BPM   = 33        # 33 BPM Minimum
BCG_MAX_HR_BPM   = 180       # 180 BPM Maximum
BCG_MIN_DIST     = None      # Wird aus SAMPLE_HZ berechnet
BCG_MIN_SAMPLES  = 50        # Mindest-Samples für BCG-Analyse (1s bei 50 Hz)

# ── Hysterese ──────────────────────────────────────────────────────────────────
HYSTERESIS_N = 2


# ── Signalverarbeitung ────────────────────────────────────────────────────────

def butter_bandpass(lo: float, hi: float, fs: float, order: int = 4):
    """Butterworth-Bandpassfilter-Koeffizienten."""
    nyq = fs / 2.0
    lo_norm = lo / nyq
    hi_norm = hi / nyq
    # Sicherstellen dass Grenzen gültig sind
    lo_norm = max(0.001, min(lo_norm, 0.999))
    hi_norm = max(0.001, min(hi_norm, 0.999))
    if lo_norm >= hi_norm:
        return None, None
    return butter(order, [lo_norm, hi_norm], btype='band')


def bandpass_filter(signal: np.ndarray, lo: float, hi: float, fs: float) -> np.ndarray | None:
    """Butterworth-Bandpassfilter anwenden (forward-backward für Null-Phasenversatz)."""
    if len(signal) < 20:
        return None
    b, a = butter_bandpass(lo, hi, fs)
    if b is None:
        return None
    try:
        return filtfilt(b, a, signal)
    except Exception:
        return None


def accel_magnitude(rows: list) -> np.ndarray:
    """L2-Norm der Beschleunigung (echter euklidischer Betrag)."""
    ax = np.array([r['ax'] or 0 for r in rows], dtype=float)
    ay = np.array([r['ay'] or 0 for r in rows], dtype=float)
    az = np.array([r['az'] or 0 for r in rows], dtype=float)
    return np.sqrt(ax**2 + ay**2 + az**2)


# ── Feature-Extraktion ────────────────────────────────────────────────────────

def feature_movement(magnitudes: np.ndarray, calib_scale: float) -> float:
    """
    PIM (Proportional Integral Mode): Summe der absoluten Magnitude-Änderungen.
    Normiert durch Kalibrierungsskala.
    """
    if len(magnitudes) < 2:
        return 0.0
    diffs = np.abs(np.diff(magnitudes))
    raw_pim = float(np.sum(diffs))
    return raw_pim / max(calib_scale, 1.0)


def feature_respiration(magnitudes: np.ndarray, fs: float) -> dict:
    """
    Atemfrequenz und Respiratory Rate Variability (RRV) via Bandpassfilter.

    RRV ist der wichtigste Einzelindikator für REM-Schlaf:
    - REM:        RRV > 0.15 (unregelmäßige Atmung wie im Wachzustand)
    - Tiefschlaf: RRV < 0.08 (sehr regelmäßige, langsame Atmung)
    - Leichtschlaf: RRV 0.08–0.15

    Referenz: Krejcar et al. (2016), PMC5027356
    """
    result = {'rate_bpm': 0.0, 'rrv': 0.5, 'resp_energy': 0.0, 'resp_filtered': None}

    resp = bandpass_filter(magnitudes, BAND_RESP_LO, BAND_RESP_HI, fs)
    if resp is None or len(resp) < 20:
        return result

    result['resp_filtered'] = resp

    # Dominante Atemfrequenz via Welch-PSD
    nperseg = min(len(resp), int(fs * 10))  # 10-Sekunden-Segmente
    freqs, psd = welch(resp, fs=fs, nperseg=nperseg)
    mask = (freqs >= BAND_RESP_LO) & (freqs <= BAND_RESP_HI)
    if not np.any(mask):
        return result

    dominant_freq = freqs[mask][np.argmax(psd[mask])]
    result['rate_bpm'] = float(dominant_freq * 60.0)

    # Atemspitzen finden für RRV-Berechnung
    # Min. Abstand: 60/(max_rate) Sekunden = 60/30 = 2s → 2*fs Samples
    min_dist = int(60.0 / 30.0 * fs)  # 30 Atemzüge/min Maximum
    threshold = float(np.std(resp) * 0.3)
    peaks, _ = find_peaks(resp, distance=min_dist, height=threshold)

    if len(peaks) >= 3:
        intervals = np.diff(peaks) / fs   # Atemintervalle in Sekunden
        mean_interval = float(np.mean(intervals))
        std_interval  = float(np.std(intervals))
        result['rrv'] = std_interval / max(mean_interval, 0.001)  # Variationskoeffizient

    # Energieanteil im Atemband
    total_power = float(np.sum(psd))
    resp_power  = float(np.sum(psd[mask]))
    result['resp_energy'] = resp_power / max(total_power, 1e-10)

    return result


def feature_bcg(magnitudes: np.ndarray, fs: float, calib_scale: float) -> dict:
    """
    BCG (Ballistokardiogramm) Herzschlag-Analyse.
    Nur auswertbar bei ruhigen Epochen (geringe Körperbewegung).

    Extrahiert J-Peaks (äquivalent zu R-Peaks im EKG) aus dem
    1.0–8.5 Hz Bandpasssignal.

    Referenz: Nikkonen et al. (2022), arXiv:2202.01038
    """
    result = {
        'hr_bpm': 0.0,
        'rmssd': 0.0,
        'sdnn': 0.0,
        'pnn50': 0.0,
        'bcg_energy': 0.0,
        'beat_count': 0,
        'valid': False,
    }

    if len(magnitudes) < BCG_MIN_SAMPLES:
        return result

    # BCG-Bandpassfilter
    bcg = bandpass_filter(magnitudes, BAND_BCG_LO, BAND_BCG_HI, fs)
    if bcg is None:
        return result

    # Spektrale BCG-Energie (immer berechnen)
    try:
        nperseg = min(len(bcg), int(fs * 10))
        freqs, psd = welch(bcg, fs=fs, nperseg=nperseg)
        all_freqs, all_psd = welch(magnitudes - np.mean(magnitudes), fs=fs, nperseg=nperseg)
        bcg_mask  = (freqs >= BAND_BCG_LO) & (freqs <= BAND_BCG_HI)
        total_pow = float(np.sum(all_psd))
        bcg_pow   = float(np.sum(psd[bcg_mask]))
        result['bcg_energy'] = bcg_pow / max(total_pow, 1e-10)
    except Exception:
        pass

    # J-Peak-Erkennung
    min_dist = int(60.0 / BCG_MAX_HR_BPM * fs)   # Minimum-Abstand zwischen Peaks
    max_dist = int(60.0 / BCG_MIN_HR_BPM * fs)   # Maximum-Abstand zwischen Peaks

    threshold = float(np.std(bcg) * 0.5)
    try:
        peaks, _ = find_peaks(bcg, distance=min_dist, height=threshold)
    except Exception:
        return result

    if len(peaks) < 3:
        return result

    # Inter-Beat-Intervalle (IBI) in Sekunden
    ibis = np.diff(peaks) / fs
    # Physiologisch plausible IBIs filtern (33–180 BPM)
    ibis = ibis[(ibis >= 60.0/BCG_MAX_HR_BPM) & (ibis <= 60.0/BCG_MIN_HR_BPM)]

    if len(ibis) < 2:
        return result

    # HRV-Features
    mean_ibi = float(np.mean(ibis))
    result['hr_bpm']    = 60.0 / mean_ibi
    result['sdnn']      = float(np.std(ibis) * 1000.0)     # ms
    result['rmssd']     = float(np.sqrt(np.mean(np.diff(ibis)**2)) * 1000.0)  # ms
    result['pnn50']     = float(np.sum(np.abs(np.diff(ibis)) > 0.05) / len(ibis) * 100.0)
    result['beat_count'] = len(peaks)
    result['valid']     = True

    return result


def feature_spectral_energy(magnitudes: np.ndarray, fs: float) -> dict:
    """Relative Energieanteile in den drei Frequenzbändern."""
    sig = magnitudes - np.mean(magnitudes)
    if len(sig) < 10:
        return {'resp': 0.0, 'bcg': 0.0, 'move': 0.0}

    try:
        nperseg = min(len(sig), int(fs * 10))
        freqs, psd = welch(sig, fs=fs, nperseg=nperseg)
        total = float(np.sum(psd))
        if total < 1e-10:
            return {'resp': 0.0, 'bcg': 0.0, 'move': 0.0}

        def band(lo, hi):
            mask = (freqs >= lo) & (freqs < hi)
            return float(np.sum(psd[mask])) / total

        return {
            'resp': band(BAND_RESP_LO, BAND_RESP_HI),
            'bcg':  band(BAND_BCG_LO,  BAND_BCG_HI),
            'move': band(BAND_MOVE_LO, BAND_MOVE_HI),
        }
    except Exception:
        return {'resp': 0.0, 'bcg': 0.0, 'move': 0.0}


# ── Cole-Kripke Wake-Score ────────────────────────────────────────────────────

def cole_kripke_scores(activity_counts: np.ndarray) -> np.ndarray:
    """
    Cole-Kripke Wake-Score für alle Epochen.
    Fenster: [t-4 ... t+4] (9 Epochen), Randbehandlung durch Padding.
    """
    padded = np.pad(activity_counts, (4, 4), mode='edge')
    scores = np.zeros(len(activity_counts))
    for i in range(len(activity_counts)):
        scores[i] = CK_SCALE * float(np.dot(CK_WEIGHTS, padded[i:i+9]))
    return scores


# ── Schlaf-Subklassifizierung ─────────────────────────────────────────────────

def classify_sleep_stage(norm_activity: float,
                          resp: dict, bcg: dict, spectral: dict,
                          minutes_from_onset: float,
                          session_rmssd_median: float) -> str:
    """
    Hierarchische Schlaf-Subklassifizierung für als 'SLEEP' erkannte Epochen.

    Reihenfolge (Priorität):
    1. DEEP_SLEEP: geringe Aktivität + reguläre Atmung (niedrige RRV) + hoher RMSSD
    2. REM:        90-min-Zyklus + hohe RRV + charakteristisches Spektrum
    3. LIGHT_SLEEP: alles andere

    Die Trennung DEEP vs. LIGHT basiert primär auf RRV (dem validierte
    Schlüssel-Feature aus Krejcar et al. 2016).
    """
    rrv        = resp.get('rrv', 0.5)
    resp_energy = spectral.get('resp', 0.0)
    rmssd      = bcg.get('rmssd', 0.0)

    # ── Tiefschlaf ──────────────────────────────────────────────────────────
    # Kriterien: kaum Bewegung + sehr reguläre Atmung (RRV < Schwellwert)
    # Optional: hoher RMSSD wenn BCG-Daten vorhanden
    is_deep_activity = norm_activity < DEEP_MOVE_THRESH
    is_deep_rrv      = rrv < RRV_DEEP_THRESH
    is_deep_resp     = resp_energy > DEEP_RESP_ENERGY
    is_deep_rmssd    = (bcg.get('valid', False)
                        and session_rmssd_median > 0
                        and rmssd > session_rmssd_median * DEEP_RMSSD_MULT)

    if is_deep_activity and (is_deep_rrv or is_deep_resp or is_deep_rmssd):
        return 'DEEP_SLEEP'

    # ── REM ─────────────────────────────────────────────────────────────────
    # Kriterien: 90-min-Zyklus + hohe RRV + moderate Aktivität
    # REM hat ähnliche Aktivität wie Leichtschlaf, aber unregelmäßige Atmung
    if minutes_from_onset > 0 and norm_activity < LIGHT_MOVE_THRESH:
        cycle_pos = minutes_from_onset % REM_CYCLE_MINUTES
        half_win  = REM_WINDOW_MINUTES / 2.0
        in_rem_window = (cycle_pos > (REM_CYCLE_MINUTES - half_win)
                         or cycle_pos < half_win)

        if in_rem_window and rrv > RRV_REM_THRESH:
            return 'REM'

    return 'LIGHT_SLEEP'


# ── Hysterese ─────────────────────────────────────────────────────────────────

def apply_hysteresis(stages: list[str], n: int = HYSTERESIS_N) -> list[str]:
    """Phasenwechsel erst nach n aufeinanderfolgenden übereinstimmenden Epochen."""
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
        result.append(pending if run_len >= n else result[-1])
    return result


# ── Haupt-Analyse ─────────────────────────────────────────────────────────────

def analyze_session(session_id: str, db: Database) -> list[dict]:
    """
    Vollständige Schlafphasen-Analyse für eine Session.
    Gibt Liste der analysierten Epochen zurück (oder leere Liste bei Fehler).
    """
    rows = db.get_raw_for_session(session_id)
    if not rows:
        print(f"FEHLER: Keine Rohdaten für Session {session_id}")
        return []

    print(f"  Rohdaten: {len(rows)} Samples")

    # ── Kalibrierungsskala ────────────────────────────────────────────────────
    calib = db.get_latest_calibration()
    if calib:
        calib_scale = float(calib['activity_scale'])
        print(f"  Kalibrierung: scale={calib_scale:.1f} (aus DB)")
    else:
        all_mags    = accel_magnitude(rows)
        calib_scale = float(np.percentile(all_mags, 5))
        calib_scale = max(calib_scale, 1.0)
        print(f"  Kalibrierung: scale={calib_scale:.1f} (adaptiv, 5. Perzentil)")

    # ── Epochen bilden ────────────────────────────────────────────────────────
    epoch_starts     = []
    epoch_rows_list  = []
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
    resp_features       = []
    bcg_features        = []
    spectral_features_list = []
    epoch_ends          = []

    for i, (epoch_rows_data, t_start) in enumerate(zip(epoch_rows_list, epoch_starts)):
        mags    = accel_magnitude(epoch_rows_data)
        t_end   = epoch_rows_data[-1]['timestamp']
        epoch_ends.append(t_end)

        # Echte Abtastrate dieser Epoche
        duration = max(t_end - t_start, 1.0)
        sr       = len(mags) / duration

        # Bewegung
        act = feature_movement(mags, calib_scale)
        activity_counts_raw[i] = act

        # Atmung + RRV
        resp = feature_respiration(mags, sr)
        resp_features.append(resp)

        # BCG (nur bei ruhigen Epochen – spart Rechenzeit)
        if act < calib_scale * 0.5:
            bcg = feature_bcg(mags, sr, calib_scale)
        else:
            bcg = {'hr_bpm': 0.0, 'rmssd': 0.0, 'sdnn': 0.0,
                   'bcg_energy': 0.0, 'beat_count': 0, 'valid': False}
        bcg_features.append(bcg)

        # Spektrale Energie
        spec = feature_spectral_energy(mags, sr)
        spectral_features_list.append(spec)

    # ── Session-weiter RMSSD-Median (für Tiefschlaf-Erkennung) ───────────────
    valid_rmssds = [b['rmssd'] for b in bcg_features if b.get('valid') and b['rmssd'] > 0]
    session_rmssd_median = float(np.median(valid_rmssds)) if valid_rmssds else 0.0
    if session_rmssd_median > 0:
        print(f"  BCG-RMSSD Median: {session_rmssd_median:.1f} ms "
              f"(aus {len(valid_rmssds)} validen Epochen)")

    # ── Cole-Kripke Wake-Score ────────────────────────────────────────────────
    ck_scores = cole_kripke_scores(activity_counts_raw)

    # ── Erste Klassifizierung (Wake vs. Sleep) ────────────────────────────────
    raw_stages = ['AWAKE' if s >= CK_WAKE_THRESH else 'SLEEP' for s in ck_scores]

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
        print("  Schlafbeginn: nicht erkannt (Session zu kurz oder viel Bewegung)")

    # ── Normierte Aktivität für Subklassifizierung ────────────────────────────
    max_act   = max(float(np.max(activity_counts_raw)), 1.0)
    norm_acts = activity_counts_raw / max_act

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
            norm_activity       = norm_acts[i],
            resp                = resp_features[i],
            bcg                 = bcg_features[i],
            spectral            = spectral_features_list[i],
            minutes_from_onset  = minutes_from_onset,
            session_rmssd_median = session_rmssd_median,
        )
        final_stages.append(stage)

    # ── Hysterese ─────────────────────────────────────────────────────────────
    final_stages = apply_hysteresis(final_stages, HYSTERESIS_N)

    # ── In DB speichern ───────────────────────────────────────────────────────
    db.delete_epochs_for_session(session_id)
    result = []
    for i in range(n_epochs):
        t_start  = epoch_starts[i]
        t_end    = epoch_ends[i]
        stage    = final_stages[i]
        act_norm = float(norm_acts[i])
        bcg_e    = bcg_features[i].get('bcg_energy', 0.0)

        db.insert_epoch(
            session_id,
            t_start, t_end,
            stage,
            movement_score  = act_norm,
            sound_score     = bcg_e,      # Feld umgewidmet: BCG-Energie
            activity_count  = float(activity_counts_raw[i])
        )
        result.append({
            'start':          t_start,
            'end':            t_end,
            'stage':          stage,
            'movement_score': act_norm,
            'rrv':            resp_features[i].get('rrv', 0.0),
            'resp_rate':      resp_features[i].get('rate_bpm', 0.0),
            'rmssd':          bcg_features[i].get('rmssd', 0.0),
            'hr_bpm':         bcg_features[i].get('hr_bpm', 0.0),
            'bcg_energy':     bcg_e,
            'activity_count': float(activity_counts_raw[i]),
        })

    return result


# ── Zusammenfassung ───────────────────────────────────────────────────────────

def print_summary(session: dict, epochs: list[dict]):
    stage_minutes = {'AWAKE': 0.0, 'LIGHT_SLEEP': 0.0, 'DEEP_SLEEP': 0.0, 'REM': 0.0}
    for ep in epochs:
        dur = (ep.get('end', ep.get('end_time', 0))
               - ep.get('start', ep.get('start_time', 0))) / 60.0
        stage_minutes[ep['stage']] = stage_minutes.get(ep['stage'], 0.0) + dur

    start_dt = datetime.fromtimestamp(session['start_time'])
    end_ts   = session['end_time'] or (epochs[-1].get('end') or epochs[-1].get('end_time'))
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
        print_summary(session, epochs)
        print(f"{len(epochs)} Epochen gespeichert.")

    db.close()


if __name__ == '__main__':
    main()
