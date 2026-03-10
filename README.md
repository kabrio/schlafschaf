# Schlafschaf 🐑

Klang- und bewegungsbasierter Schlaftracker für den **Arduino Uno Q**.
Analysiert Schlafdaten lokal auf dem Board und synchronisiert mit Apple Health.

## Systemarchitektur

```
KY-038 Mikrofon ──┐
MPU-6050        ──┤──→ STM32U585 MCU (Arduino Sketch)
                         │ Interne Serial-Bridge
                         ↓
                  QRB2210 Linux (Debian)
                  ├── collector.py  → SQLite
                  ├── analyzer.py   → Schlafphasen
                  ├── web_server.py → HTTP API :8080
                         │ WiFi
                         ↓
                  iPhone – iOS App → Apple Health
```

## Hardware

### Komponenten
- Arduino Uno Q (Qualcomm QRB2210 + STM32U585, WiFi 5)
- KY-038 Schallsensor-Modul (~2€)
- MPU-6050 Beschleunigungs-/Gyroskopsensor (~3€)

### Verkabelung

| Sensor | Sensor-Pin | Arduino-Pin |
|--------|-----------|-------------|
| KY-038 | AO        | A0          |
| KY-038 | VCC       | 5V          |
| KY-038 | GND       | GND         |
| MPU-6050 | SDA     | A4 (SDA)    |
| MPU-6050 | SCL     | A5 (SCL)    |
| MPU-6050 | VCC     | 3.3V        |
| MPU-6050 | GND     | GND         |

## Setup

### 1. Arduino Sketch flashen

1. Arduino App Lab oder Arduino IDE 2.x öffnen
2. Board: **Arduino Uno Q** wählen
3. `arduino/schlafschaf.ino` öffnen
4. Hochladen

Keine externe Library nötig – nur das eingebaute `Wire.h`.

### 2. Python auf dem Uno Q (Debian-Seite)

Per SSH auf den Uno Q verbinden:
```bash
ssh arduino@<uno-q-ip>
```

Repository klonen:
```bash
git clone <repo-url> ~/schlafschaf
cd ~/schlafschaf
```

Abhängigkeiten installieren:
```bash
sudo apt update && sudo apt install -y python3-serial python3-numpy python3-matplotlib
pip3 install flask
mkdir -p data/exports data/charts
```

### 3. Schlaftracking starten

#### Manuell:
```bash
cd ~/schlafschaf/python

# Datensammlung starten (über Nacht laufen lassen)
python3 collector.py

# Am Morgen: Analyse starten
python3 analyzer.py --latest

# Diagramm generieren
python3 visualize.py --latest --output data/charts/letzte-nacht.png

# JSON für iOS App exportieren
python3 export.py --latest --output data/exports/letzte-nacht.json

# Web-Server starten (für iOS App-Zugriff)
python3 web_server.py
```

#### Als systemd Service (automatischer Start):
```bash
sudo cp python/schlafschaf.service /etc/systemd/system/
sudo cp python/schlafschaf-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now schlafschaf
sudo systemctl enable --now schlafschaf-web
```

Logs anzeigen:
```bash
journalctl -u schlafschaf -f
```

### 4. iOS App einrichten

1. `ios/SchlafSchaf/` in Xcode öffnen
2. **Bundle Identifier** ändern (z.B. `com.deinname.schlafschaf`)
3. Signing: eigenes Apple Developer Team wählen
4. Auf iPhone deployen (kostenloser Account reicht für 7-Tage-Tests)

**Hinweis:** Das HealthKit-Entitlement benötigt einen Apple Developer Account.

### 5. Schlafdaten mit Apple Health synchronisieren

In der iOS App:
1. IP-Adresse des Uno Q eingeben (im lokalen WLAN)
2. "Sessions laden" tippen
3. Session auswählen → "Sync" tippen
4. In der Apple Health App → Schlaf → Schlafdaten prüfen

Oder JSON-Datei direkt importieren:
- `data/exports/letzte-nacht.json` per AirDrop auf iPhone senden
- In Schlafschaf-App öffnen → "→ Health" tippen

## Schlafphasen-Algorithmus

### Epochen
- Einteilung in **30-Sekunden-Epochen**
- Pro Epoche: Bewegungsscore + Klangscore

### Bewegungsscore
```
magnitude = √(ax² + ay² + az²)
score = mean(|Δmagnitude|) / 32768
```

### Klangscore
```
score = percentile(sound_values, 75) / 1023
```

### Klassifizierung

| Bedingung | Phase |
|-----------|-------|
| Bewegung > 0.30 oder Klang > 0.60 | WACH |
| Bewegung < 0.05 und Klang < 0.15 | TIEFSCHLAF |
| 75.–90. Minute im 90-min-Zyklus | REM |
| Sonst | LEICHTSCHLAF |

## API-Endpunkte (Web-Server)

| Methode | Pfad | Beschreibung |
|---------|------|-------------|
| GET | `/sessions` | Alle Sessions auflisten |
| GET | `/sessions/<uuid>` | Session-Details |
| GET | `/sessions/<uuid>/export` | JSON herunterladen |
| GET | `/sessions/<uuid>/chart` | PNG-Chart herunterladen |
| POST | `/sessions/<uuid>/analyze` | Analyse auslösen |

## Dateistruktur

```
schlafschaf/
├── arduino/
│   └── schlafschaf.ino       STM32 Arduino Sketch
├── python/
│   ├── collector.py          Serial-Datensammlung
│   ├── analyzer.py           Schlafphasen-Analyse
│   ├── database.py           SQLite-Wrapper
│   ├── export.py             JSON-Export
│   ├── visualize.py          Matplotlib-Charts
│   ├── web_server.py         HTTP-API für iOS
│   ├── schlafschaf.service   systemd Collector
│   ├── schlafschaf-web.service systemd Web-Server
│   └── requirements.txt
├── ios/
│   └── SchlafSchaf/          SwiftUI iOS App
│       ├── SchlafSchafApp.swift
│       ├── ContentView.swift
│       ├── HealthKitManager.swift
│       ├── SleepDataImporter.swift
│       ├── Info.plist
│       └── SchlafSchaf.entitlements
└── data/                     (gitignored)
    ├── schlafschaf.db
    ├── exports/
    └── charts/
```

## Fehlerbehebung

**Serial-Port nicht gefunden:**
```bash
ls /dev/tty*  # Verfügbare Ports anzeigen
python3 collector.py --port /dev/ttyACM0
```

**MPU-6050 nicht erkannt:**
- VCC → 3.3V (nicht 5V!)
- Kabel prüfen (SDA/SCL nicht vertauscht)
- I2C-Scanner: `i2cdetect -y 1` (Adresse 0x68 muss erscheinen)

**HealthKit-Fehler:**
- iPhone-Einstellungen → Datenschutz → Gesundheit → Schlafschaf → Schreiben aktivieren

## Lizenz

MIT License – Frei zur Verwendung und Modifikation.
