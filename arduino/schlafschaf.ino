/**
 * Schlafschaf v3 – Arduino Sleep Tracker
 *
 * ZWECK:
 *   MPU-6050 misst Bewegungen, die durch die Matratze übertragen werden.
 *   50 Hz Abtastrate für gute Bewegungsauflösung (Schlafphasen-Analyse
 *   auf Python-Seite via Aktigraphie / Cole-Kripke-Algorithmus).
 *
 * MODI:
 *   NORMAL    (Standard): 50 Hz, alle Daten werden per CSV gesendet
 *   PAUSED    (Batterie): MPU schläft, keine Daten, nur Befehlsempfang
 *   LOWPOWER  (Batterie): 1 Hz, nur Spike-Events, kein CSV-Stream
 *             → Überbrückt Phasen, in denen Tracking nicht nötig ist
 *
 * BEFEHLE (von Python über Serial):
 *   PAUSE\n      → Wechsel in PAUSED-Modus (MPU schläft, spart ~3 mA)
 *   RESUME\n     → Zurück zu NORMAL
 *   LOWPOWER\n   → 1 Hz Sampling, nur Spike-Events (spart ~80% CPU-Last)
 *
 * CSV-FORMAT: millis,ax,ay,az,gx,gy,gz   (7 Felder)
 * EVENTS:     EVENT,BED_MOTION,<millis>
 * STATUS:     PAUSED / RESUMED / READY / READY_NO_MPU
 *
 * HARDWARE:
 *   MPU-6050 SDA → A4 (Arduino Uno) / SDA (Uno Q)
 *   MPU-6050 SCL → A5 (Arduino Uno) / SCL (Uno Q)
 *   MPU-6050 VCC → 3.3V
 *   MPU-6050 AD0 → GND (Adresse 0x68)
 *   LED_PIN  13  → Status-LED (optional)
 *
 * MPU-6050 KONFIGURATION:
 *   Accel:  ±2g  (16384 LSB/g) – maximale Empfindlichkeit
 *   Gyro:   ±250°/s (131 LSB/°/s)
 *   DLPF:   44 Hz Bandbreite (CFG=3)
 *   Rate:   50 Hz (SMPLRT_DIV=19)
 */

#include <Wire.h>

// I2C-Adresse
#define MPU_ADDR     0x68

// Register
#define REG_PWR      0x6B
#define REG_WHO      0x75
#define REG_SMPLRT   0x19
#define REG_CONFIG   0x1A
#define REG_GCONFIG  0x1B
#define REG_ACONFIG  0x1C
#define REG_ACCEL    0x3B

// Normal-Modus: 50 Hz
#define SAMPLE_HZ_NORMAL    50
#define SAMPLE_MS_NORMAL    (1000 / SAMPLE_HZ_NORMAL)   // 20 ms

// LowPower-Modus: 1 Hz (nur Spike-Events, kein Stream)
#define SAMPLE_HZ_LOWPOWER  1
#define SAMPLE_MS_LOWPOWER  1000

// Spike-Erkennung (Bewegungspuls)
#define SPIKE_DELTA_THRESH  3000    // raw (L1-Norm)
#define SPIKE_CONFIRM_COUNT 3       // Anzahl Spikes für Event
#define SPIKE_WINDOW_MS     2000    // Zeitfenster in ms

// Status-LED
#define LED_PIN 13

// ── Modus-Enum ───────────────────────────────────────────────────────────────
enum Mode { NORMAL, PAUSED, LOWPOWER };
static Mode current_mode = NORMAL;
static bool mpu_ok       = false;

// ── MPU-6050: Aufwecken ──────────────────────────────────────────────────────
bool mpuWake() {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(REG_PWR);
  Wire.write(0x00);  // Sleep-Bit löschen
  return Wire.endTransmission(true) == 0;
}

// ── MPU-6050: Schlafen ───────────────────────────────────────────────────────
void mpuSleep() {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(REG_PWR);
  Wire.write(0x40);  // Sleep-Bit setzen → ~6 µA
  Wire.endTransmission(true);
}

// ── MPU-6050: Vollständig initialisieren ─────────────────────────────────────
bool initMPU() {
  if (!mpuWake()) return false;
  delay(100);

  // WHO_AM_I prüfen
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

  // DLPF = 3: 44 Hz Bandbreite
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(REG_CONFIG);
  Wire.write(0x03);
  Wire.endTransmission(true);

  // Gyro: ±250°/s
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(REG_GCONFIG);
  Wire.write(0x00);
  Wire.endTransmission(true);

  // Accel: ±2g (maximale Empfindlichkeit)
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
  Wire.read(); Wire.read();  // Temperatur
  *gx = (Wire.read() << 8) | Wire.read();
  *gy = (Wire.read() << 8) | Wire.read();
  *gz = (Wire.read() << 8) | Wire.read();
  return true;
}

// ── Spike-Erkennung ──────────────────────────────────────────────────────────
static unsigned long spike_times[SPIKE_CONFIRM_COUNT];
static uint8_t  spike_count    = 0;
static int32_t  last_magnitude = -1;

// Gibt true zurück wenn ein Bewegungs-Event ausgelöst wird
bool checkSpike(int32_t magnitude, unsigned long now_ms) {
  if (last_magnitude < 0) {
    last_magnitude = magnitude;
    return false;
  }

  int32_t delta  = abs(magnitude - last_magnitude);
  last_magnitude = magnitude;

  if (delta > SPIKE_DELTA_THRESH) {
    spike_times[spike_count % SPIKE_CONFIRM_COUNT] = now_ms;
    spike_count++;

    if (spike_count >= SPIKE_CONFIRM_COUNT) {
      uint8_t oldest_idx  = spike_count % SPIKE_CONFIRM_COUNT;
      unsigned long oldest = spike_times[oldest_idx];
      if (now_ms - oldest <= (unsigned long)SPIKE_WINDOW_MS) {
        spike_count    = 0;
        last_magnitude = -1;
        return true;
      }
    }
  }
  return false;
}

// ── Serial-Befehle verarbeiten ────────────────────────────────────────────────
// Puffer für eingehende Befehle (max. 16 Zeichen)
static char  cmd_buf[16];
static uint8_t cmd_len = 0;

void handleSerialInput() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (cmd_len > 0) {
        cmd_buf[cmd_len] = '\0';
        processCommand(cmd_buf);
        cmd_len = 0;
      }
    } else if (cmd_len < sizeof(cmd_buf) - 1) {
      cmd_buf[cmd_len++] = c;
    }
  }
}

void processCommand(const char* cmd) {
  if (strcmp(cmd, "PAUSE") == 0) {
    current_mode = PAUSED;
    mpuSleep();                    // MPU in Schlafmodus → ~6 µA statt ~3.8 mA
    digitalWrite(LED_PIN, LOW);
    Serial.println(F("PAUSED"));

  } else if (strcmp(cmd, "RESUME") == 0) {
    current_mode = NORMAL;
    if (mpu_ok) {
      // MPU wieder vollständig initialisieren (nach Sleep)
      mpu_ok = initMPU();
      delay(50);
    }
    last_magnitude = -1;
    spike_count    = 0;
    digitalWrite(LED_PIN, HIGH);
    delay(200);
    digitalWrite(LED_PIN, LOW);
    Serial.println(F("RESUMED"));

  } else if (strcmp(cmd, "LOWPOWER") == 0) {
    current_mode = LOWPOWER;
    Serial.println(F("LOWPOWER"));
  }
}

// ── Setup ────────────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  while (!Serial) delay(10);

  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  Wire.begin();
  Wire.setClock(400000L);
  delay(200);

  for (uint8_t i = 0; i < 3; i++) {
    mpu_ok = initMPU();
    if (mpu_ok) break;
    delay(300);
  }

  if (mpu_ok) {
    Serial.println(F("READY"));
    for (uint8_t i = 0; i < 3; i++) {
      digitalWrite(LED_PIN, HIGH); delay(100);
      digitalWrite(LED_PIN, LOW);  delay(100);
    }
  } else {
    Serial.println(F("READY_NO_MPU"));
    for (uint8_t i = 0; i < 6; i++) {
      digitalWrite(LED_PIN, HIGH); delay(50);
      digitalWrite(LED_PIN, LOW);  delay(50);
    }
  }
}

// ── Haupt-Loop ───────────────────────────────────────────────────────────────
static unsigned long next_sample_ms  = 0;
static bool          bed_occupied    = false;

void loop() {
  // Serial-Befehle haben immer Vorrang
  handleSerialInput();

  // Im PAUSED-Modus: nichts senden, nur auf Befehle warten
  if (current_mode == PAUSED) {
    delay(50);
    return;
  }

  unsigned long now      = millis();
  unsigned long interval = (current_mode == LOWPOWER)
                           ? SAMPLE_MS_LOWPOWER
                           : SAMPLE_MS_NORMAL;

  if (now < next_sample_ms) return;
  next_sample_ms = now + interval;

  int16_t ax = 0, ay = 0, az = 0, gx = 0, gy = 0, gz = 0;
  if (mpu_ok) {
    if (!readMPU(&ax, &ay, &az, &gx, &gy, &gz)) {
      mpu_ok = initMPU();
      return;
    }
  }

  // L1-Magnitude (schnell, kein sqrt nötig für Spike-Erkennung)
  int32_t mag = (int32_t)abs(ax) + abs(ay) + abs(az);

  bool spike = checkSpike(mag, now);
  if (spike) {
    Serial.print(F("EVENT,BED_MOTION,"));
    Serial.println(now);
    bed_occupied = !bed_occupied;
    digitalWrite(LED_PIN, bed_occupied ? HIGH : LOW);
  }

  // Im LOWPOWER-Modus: keinen kontinuierlichen CSV-Stream senden
  // (spart ~90% der seriellen Übertragungsarbeit)
  if (current_mode == LOWPOWER) return;

  // NORMAL: vollständige CSV-Zeile senden
  Serial.print(now);
  Serial.print(',');
  Serial.print(ax); Serial.print(',');
  Serial.print(ay); Serial.print(',');
  Serial.print(az); Serial.print(',');
  Serial.print(gx); Serial.print(',');
  Serial.print(gy); Serial.print(',');
  Serial.println(gz);
}
