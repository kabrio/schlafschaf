# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Schlafschaf** ("Sleep Sheep") is a non-wearable sleep tracker using an Arduino Uno Q (STM32U585 MCU + Linux QRB2210) with an MPU-6050 accelerometer mounted on the bed frame. It detects sleep stages via mattress vibrations and syncs to Apple Health via a SwiftUI iOS app.

## Architecture

```
Arduino (MPU-6050, 50 Hz CSV) → collector.py (bed state FSM, SQLite) → analyzer.py (sleep stages) → web_server.py (REST :8080) → iOS App (HealthKit)
```

### Component Responsibilities

- **`arduino/schlafschaf.ino`** — Sensor acquisition via Modulino Movement (LSM6DSOX, Qwiic), serial command handling (PAUSE/RESUME/LOWPOWER), three operating modes
- **`python/collector.py`** — Bed occupancy state machine, session management, incremental background analysis every 30 min
- **`python/analyzer.py`** — Sleep stage classification: Cole-Kripke wake/sleep + RRV-based deep/REM/light distinction
- **`python/database.py`** — SQLite ORM with schema migration support
- **`python/web_server.py`** — REST API (Flask) for iOS; endpoints: GET `/sessions`, `/sessions/<uuid>`, `/sessions/<uuid>/export`, POST `/analyze`
- **`python/visualize.py`** — Dark-theme 3-panel hypnogram (matplotlib)
- **`python/export.py`** — JSON export with ISO 8601 timestamps for iOS
- **`tools/trainer.py`** — ML training tool (MiniROCKET/RandomForest) for bed entry/exit detection

### Data Flow

1. Arduino streams CSV: `millis,ax,ay,az,gx,gy,gz` at 115200 baud
2. Collector writes to `raw_data` table; bed state machine creates/closes sessions
3. Analyzer groups raw data into 30-second epochs, runs classification, writes `epochs` table
4. iOS downloads JSON from web server, imports to HealthKit

### Bed State Machine (collector.py)

```
CALIBRATING → EMPTY → CANDIDATE_ENTRY → OCCUPIED → CANDIDATE_EXIT → EMPTY
```

Exit confirmation distinguishes **deep sleep** (40+ min stillness) from **empty bed** using exit pulse detection:
- With pulse: 3 min (day) / 10 min (night) confirmation
- Without pulse: 10 min (day) / 45 min (night) confirmation

### Sleep Classification (analyzer.py)

1. **Cole-Kripke** (Cole et al. 1992): `W = 0.00001 × Σ(weight[i] × activity[t+i])` — W ≥ 1.0 → AWAKE
2. **Subclassification** (hierarchical):
   - DEEP_SLEEP: movement < 10% + RRV < 0.08 + high RMSSD
   - REM: near 90-min cycle boundary + RRV > 0.15 + movement < 30%
   - LIGHT_SLEEP: all other sleep epochs
3. Hysteresis: 2 consecutive matching epochs required for stage transition

Frequency bands: respiration 0.1–0.5 Hz, BCG/heart 1.0–8.5 Hz, movement 2.0–10.0 Hz

## Running the System

### Manual Operation
```bash
# Collect overnight (on Arduino Uno Q via SSH)
python3 python/collector.py [--mode mains|battery] [--port /dev/ttyACM0]

# Analyze latest session
python3 python/analyzer.py --latest

# Visualize
python3 python/visualize.py --latest --output data/charts/sleep.png

# Export JSON for iOS
python3 python/export.py --latest --output data/exports/sleep.json

# Start web server
python3 python/web_server.py --port 8080
```

### Systemd (24/7 on-device)
```bash
sudo systemctl status schlafschaf         # collector daemon
sudo systemctl status schlafschaf-web     # web server
journalctl -u schlafschaf -f              # live logs
```

### ML Training Tool
```bash
python3 tools/trainer.py record --name session01  # record labeled data
python3 tools/trainer.py label --name session01   # interactive labeling
python3 tools/trainer.py train [--model rocket|rf]
python3 tools/trainer.py export
```

## SQLite Schema

Tables: `sessions`, `raw_data`, `epochs`, `calibrations`. The `database.py` module handles schema migrations — always use it rather than raw SQL.

`raw_data` columns `ax/ay/az/gx/gy/gz` are `REAL` (values in g / dps from LSM6DSOX). Old sessions recorded with MPU-6050 have raw integer LSB values in these columns — re-analysis of those sessions will produce incorrect results since the scale is incompatible. Calibrations from MPU-6050 era (values in LSB, e.g. ~32000) are also incompatible; run `--calibrate` after switching sensors.

## Dependencies

Main: `numpy`, `scipy`, `matplotlib`, `flask`, `pyserial` (apt), `sqlite3` (stdlib)
ML tools only: `sktime`, `scikit-learn` (see `tools/requirements.txt`)

## Arduino Library

The Arduino sketch uses the `Arduino_Modulino` library (install via Library Manager). The Modulino Movement module connects via Qwiic (I2C, 3.3V, address 0x6A). Read pattern: `movement.available()` → `movement.update()` → `getX/Y/Z()` (accel, g) + `getRoll/Pitch/Yaw()` (gyro, dps).

## Data Structure Evolution

When modifying database schemas or epoch/session data structures, implement backward-compatible loading that fills missing fields with sensible defaults (e.g., missing `movement_score` → 0, missing `sample_rate` → 50.0).
