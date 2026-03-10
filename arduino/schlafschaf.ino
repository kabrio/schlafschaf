/**
 * Schlafschaf v2 – Arduino Sleep Tracker
 *
 * Nur MPU-6050 (kein Mikrofon), 50 Hz Abtastrate für BCG/Atembewegungsanalyse.
 * Erkennt Bettein-/Austritt über Vibrations-Spikes.
 *
 * Hardware:
 *   MPU-6050 SDA → A4
 *   MPU-6050 SCL → A5
 *   MPU-6050 VCC → 3.3V (oder 5V wenn Breakout-Regler vorhanden)
 *   MPU-6050 AD0 → GND (Adresse 0x68)
 *
 * CSV-Format: millis,ax,ay,az,gx,gy,gz   (7 Felder, KEIN Sound)
 * Ereigniszeilen: EVENT,BED_ENTRY,<millis>  oder  EVENT,BED_EXIT,<millis>
 * Baudrate: 115200
 *
 * MPU-6050 Konfiguration:
 *   - Bereich Accel:  ±2g    (16384 LSB/g) – maximale Empfindlichkeit
 *   - Bereich Gyro:   ±250°/s (131 LSB/°/s)
 *   - Sample Rate:    50 Hz (SMPLRT_DIV=19, Gyro-Basisrate 1 kHz)
 *   - DLPF:           44 Hz Bandbreite (CFG=3), kein Aliasing bei 50 Hz
 */

#include <Wire.h>

// I2C Adresse
#define MPU_ADDR     0x68

// Register
#define REG_PWR      0x6B   // Power Management 1
#define REG_WHO      0x75   // WHO_AM_I
#define REG_SMPLRT   0x19   // Sample Rate Divider
#define REG_CONFIG   0x1A   // DLPF-Konfiguration
#define REG_GCONFIG  0x1B   // Gyro Konfiguration
#define REG_ACONFIG  0x1C   // Accel Konfiguration
#define REG_ACCEL    0x3B   // Accel XH (erster Daten-Register)

// Abtastrate
#define SAMPLE_HZ    50
#define SAMPLE_MS    (1000 / SAMPLE_HZ)   // 20 ms

// Bettbelegungs-Erkennung
// Spike: Änderung der Beschleunigungsmagnitude (raw) über einem Schwellwert
// → deutet auf Aufsetzen / Hinlegen / Aufstehen hin
#define SPIKE_DELTA_THRESH  3000    // raw-Einheiten (Diff. zweier aufeinander. Magnituden)
#define SPIKE_CONFIRM_COUNT 3       // Spikes innerhalb kurzer Zeit = Event
#define SPIKE_WINDOW_MS     2000    // Zeitfenster für Spike-Zählung (ms)

// Status-LED (optional, Pin 13)
#define LED_PIN 13

bool mpu_ok = false;

// ── MPU-6050 initialisieren ──────────────────────────────────────────────────
bool initMPU() {
  // Aufwecken (Sleep-Bit löschen)
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(REG_PWR);
  Wire.write(0x00);
  if (Wire.endTransmission(true) != 0) return false;
  delay(100);

  // WHO_AM_I prüfen (muss 0x68 sein)
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(REG_WHO);
  Wire.endTransmission(false);
  Wire.requestFrom(MPU_ADDR, 1, true);
  if (!Wire.available() || Wire.read() != 0x68) return false;

  // Sample Rate: 1000 / (1 + 19) = 50 Hz
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(REG_SMPLRT);
  Wire.write(19);
  Wire.endTransmission(true);

  // DLPF = 3: 44 Hz Bandbreite → kein Aliasing bei 50 Hz Abtastrate
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(REG_CONFIG);
  Wire.write(0x03);
  Wire.endTransmission(true);

  // Gyro-Bereich: ±250 °/s (maximale Auflösung)
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(REG_GCONFIG);
  Wire.write(0x00);
  Wire.endTransmission(true);

  // Accel-Bereich: ±2g (16384 LSB/g, maximale Empfindlichkeit für BCG)
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(REG_ACONFIG);
  Wire.write(0x00);
  Wire.endTransmission(true);

  return true;
}

// ── 6 Rohwerte lesen ────────────────────────────────────────────────────────
bool readMPU(int16_t* ax, int16_t* ay, int16_t* az,
             int16_t* gx, int16_t* gy, int16_t* gz) {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(REG_ACCEL);
  if (Wire.endTransmission(false) != 0) return false;
  Wire.requestFrom(MPU_ADDR, 14, true);
  if (Wire.available() < 14) return false;

  *ax = (Wire.read() << 8) | Wire.read();
  *ay = (Wire.read() << 8) | Wire.read();
  *az = (Wire.read() << 8) | Wire.read();
  Wire.read(); Wire.read();  // Temperatur überspringen
  *gx = (Wire.read() << 8) | Wire.read();
  *gy = (Wire.read() << 8) | Wire.read();
  *gz = (Wire.read() << 8) | Wire.read();
  return true;
}

// ── Spike-Erkennung ──────────────────────────────────────────────────────────
// Ringpuffer für Spike-Zeitstempel
static unsigned long spike_times[SPIKE_CONFIRM_COUNT];
static uint8_t spike_count = 0;
static int32_t last_magnitude = -1;

// Gibt 1 zurück wenn ein BED_ENTRY/BED_EXIT-Event ausgelöst wird
// (mehrere Spikes in kurzem Zeitfenster)
int8_t checkSpike(int32_t magnitude, unsigned long now_ms) {
  if (last_magnitude < 0) {
    last_magnitude = magnitude;
    return 0;
  }

  int32_t delta = abs(magnitude - last_magnitude);
  last_magnitude = magnitude;

  if (delta > SPIKE_DELTA_THRESH) {
    // Spike erkannt: Zeitstempel im Ringpuffer speichern
    spike_times[spike_count % SPIKE_CONFIRM_COUNT] = now_ms;
    spike_count++;

    if (spike_count >= SPIKE_CONFIRM_COUNT) {
      // Ältesten und neuesten Spike vergleichen
      uint8_t oldest_idx = spike_count % SPIKE_CONFIRM_COUNT;
      unsigned long oldest = spike_times[oldest_idx];
      if (now_ms - oldest <= (unsigned long)SPIKE_WINDOW_MS) {
        // Reset Puffer
        spike_count = 0;
        last_magnitude = -1;
        return 1;  // Event!
      }
    }
  }
  return 0;
}

// ── Setup ────────────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  while (!Serial) delay(10);

  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  Wire.begin();
  Wire.setClock(400000L);  // 400 kHz Fast-Mode
  delay(200);

  // MPU initialisieren (3 Versuche)
  for (uint8_t i = 0; i < 3; i++) {
    mpu_ok = initMPU();
    if (mpu_ok) break;
    delay(300);
  }

  if (mpu_ok) {
    Serial.println(F("READY"));
    // Kurz blinken = OK
    for (uint8_t i = 0; i < 3; i++) {
      digitalWrite(LED_PIN, HIGH); delay(100);
      digitalWrite(LED_PIN, LOW);  delay(100);
    }
  } else {
    Serial.println(F("READY_NO_MPU"));
    // Dauerhaft schnelles Blinken = Fehler
    for (uint8_t i = 0; i < 6; i++) {
      digitalWrite(LED_PIN, HIGH); delay(50);
      digitalWrite(LED_PIN, LOW);  delay(50);
    }
  }
}

// ── Haupt-Loop ───────────────────────────────────────────────────────────────
static unsigned long next_sample_ms = 0;
// Bettbelegungs-Tracking (vereinfacht auf Arduino-Seite)
// Python-Seite macht die eigentliche Entscheidung – Arduino sendet nur Events
static bool bed_likely_occupied = false;

void loop() {
  unsigned long now = millis();

  // Auf nächsten Sample-Zeitpunkt warten
  if (now < next_sample_ms) return;
  next_sample_ms = now + SAMPLE_MS;

  int16_t ax = 0, ay = 0, az = 0, gx = 0, gy = 0, gz = 0;
  if (mpu_ok) {
    if (!readMPU(&ax, &ay, &az, &gx, &gy, &gz)) {
      mpu_ok = initMPU();
      return;
    }
  }

  // Magnitude berechnen (Näherung ohne sqrt für Geschwindigkeit)
  // Für Spike-Erkennung genügt quadratische Magnitude
  // Echter Wert: sqrt(ax²+ay²+az²), hier: max-Approximation für Speed
  int32_t mag = (int32_t)abs(ax) + abs(ay) + abs(az);  // L1-Norm (schnell, kein sqrt)

  // Spike prüfen
  int8_t event = checkSpike(mag, now);
  if (event) {
    // Event-Zeile ausgeben (Python entscheidet ob BED_ENTRY oder BED_EXIT)
    Serial.print(F("EVENT,BED_MOTION,"));
    Serial.println(now);
    bed_likely_occupied = !bed_likely_occupied;
    digitalWrite(LED_PIN, bed_likely_occupied ? HIGH : LOW);
  }

  // Reguläre CSV-Datenzeile
  // Format: millis,ax,ay,az,gx,gy,gz
  Serial.print(now);
  Serial.print(',');
  Serial.print(ax); Serial.print(',');
  Serial.print(ay); Serial.print(',');
  Serial.print(az); Serial.print(',');
  Serial.print(gx); Serial.print(',');
  Serial.print(gy); Serial.print(',');
  Serial.println(gz);
}
