/**
 * Schlafschaf v3 – Arduino Sleep Tracker
 *
 * ZWECK:
 *   Modulino Movement (LSM6DSOX) misst Bewegungen, die durch die Matratze
 *   übertragen werden. 50 Hz Abtastrate für gute Bewegungsauflösung
 *   (Schlafphasen-Analyse auf Python-Seite via Aktigraphie / Cole-Kripke).
 *
 * MODI:
 *   NORMAL    (Standard): 50 Hz, alle Daten werden per CSV gesendet
 *   PAUSED    (Batterie): kein Sampling, nur Befehlsempfang
 *   LOWPOWER  (Batterie): 1 Hz, nur Spike-Events, kein CSV-Stream
 *             → Überbrückt Phasen, in denen Tracking nicht nötig ist
 *
 * BEFEHLE (von Python über Monitor):
 *   PAUSE\n      → Wechsel in PAUSED-Modus
 *   RESUME\n     → Zurück zu NORMAL
 *   LOWPOWER\n   → 1 Hz Sampling, nur Spike-Events
 *
 * CSV-FORMAT: millis,ax,ay,az,gx,gy,gz   (Werte in g / dps)
 * EVENTS:     EVENT,BED_MOTION,<millis>
 * STATUS:     PAUSED / RESUMED / READY / READY_NO_IMU
 *
 * HARDWARE:
 *   Modulino Movement → Qwiic-Anschluss (I2C, 3.3V)
 *   LSM6DSOX I2C-Adresse: 0x6A (Standard)
 *   LED_PIN  13  → Status-LED (optional)
 *
 * ARDUINO UNO Q:
 *   Nutzt Monitor (Router Bridge) statt Serial.
 *   collector.py verbindet sich via TCP 127.0.0.1:7500 auf dem QRB2210.
 */

#include "Arduino_RouterBridge.h"
#include <Modulino.h>

ModulinoMovement movement;

// Normal-Modus: 50 Hz
#define SAMPLE_HZ_NORMAL    50
#define SAMPLE_MS_NORMAL    (1000 / SAMPLE_HZ_NORMAL)   // 20 ms

// LowPower-Modus: 1 Hz (nur Spike-Events, kein Stream)
#define SAMPLE_HZ_LOWPOWER  1
#define SAMPLE_MS_LOWPOWER  1000

// Spike-Erkennung (Bewegungspuls) – Schwellwert in g (L1-Norm der Accel-Magnitude)
#define SPIKE_DELTA_THRESH  0.15f
#define SPIKE_CONFIRM_COUNT 3
#define SPIKE_WINDOW_MS     2000

// Status-LED
#define LED_PIN 13

// ── Modus-Enum ───────────────────────────────────────────────────────────────
enum Mode { NORMAL, PAUSED, LOWPOWER };
static Mode current_mode = NORMAL;
static bool imu_ok       = false;

// ── Spike-Erkennung ──────────────────────────────────────────────────────────
static unsigned long spike_times[SPIKE_CONFIRM_COUNT];
static uint8_t  spike_count      = 0;
static float    last_magnitude   = -1.0f;

bool checkSpike(float magnitude, unsigned long now_ms) {
  if (last_magnitude < 0.0f) {
    last_magnitude = magnitude;
    return false;
  }
  float delta    = fabsf(magnitude - last_magnitude);
  last_magnitude = magnitude;
  if (delta > SPIKE_DELTA_THRESH) {
    spike_times[spike_count % SPIKE_CONFIRM_COUNT] = now_ms;
    spike_count++;
    if (spike_count >= SPIKE_CONFIRM_COUNT) {
      uint8_t oldest_idx   = spike_count % SPIKE_CONFIRM_COUNT;
      unsigned long oldest = spike_times[oldest_idx];
      if (now_ms - oldest <= (unsigned long)SPIKE_WINDOW_MS) {
        spike_count    = 0;
        last_magnitude = -1.0f;
        return true;
      }
    }
  }
  return false;
}

// ── Monitor-Befehle verarbeiten ───────────────────────────────────────────────
static char    cmd_buf[16];
static uint8_t cmd_len = 0;

void handleMonitorInput() {
  while (Monitor.available()) {
    char c = (char)Monitor.read();
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
    digitalWrite(LED_PIN, LOW);
    Monitor.println(F("PAUSED"));

  } else if (strcmp(cmd, "RESUME") == 0) {
    current_mode = NORMAL;
    if (!imu_ok) {
      imu_ok = movement.begin();
    }
    last_magnitude = -1.0f;
    spike_count    = 0;
    digitalWrite(LED_PIN, HIGH);
    delay(200);
    digitalWrite(LED_PIN, LOW);
    Monitor.println(F("RESUMED"));

  } else if (strcmp(cmd, "LOWPOWER") == 0) {
    current_mode = LOWPOWER;
    Monitor.println(F("LOWPOWER"));
  }
}

// ── Setup ────────────────────────────────────────────────────────────────────
void setup() {
  // Monitor.begin() initialises the Router Bridge (ttyHS1 → QRB2210 Linux).
  // Must be called before Modulino.begin() so Bridge is ready for I2C routing.
  Monitor.begin(115200);

  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  Modulino.begin();
  delay(200);

  for (uint8_t i = 0; i < 3; i++) {
    imu_ok = movement.begin();
    if (imu_ok) break;
    delay(300);
  }

  if (imu_ok) {
    Monitor.println(F("READY"));
    for (uint8_t i = 0; i < 3; i++) {
      digitalWrite(LED_PIN, HIGH); delay(100);
      digitalWrite(LED_PIN, LOW);  delay(100);
    }
  } else {
    Monitor.println(F("READY_NO_IMU"));
    for (uint8_t i = 0; i < 6; i++) {
      digitalWrite(LED_PIN, HIGH); delay(50);
      digitalWrite(LED_PIN, LOW);  delay(50);
    }
  }
}

// ── Haupt-Loop ───────────────────────────────────────────────────────────────
static unsigned long next_sample_ms = 0;
static bool          bed_occupied   = false;

void loop() {
  handleMonitorInput();

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

  float ax = 0.0f, ay = 0.0f, az = 0.0f;
  float gx = 0.0f, gy = 0.0f, gz = 0.0f;

  if (imu_ok && movement.available()) {
    movement.update();
    ax = movement.getX();
    ay = movement.getY();
    az = movement.getZ();
    gx = movement.getRoll();
    gy = movement.getPitch();
    gz = movement.getYaw();
  }

  float mag = fabsf(ax) + fabsf(ay) + fabsf(az);

  bool spike = checkSpike(mag, now);
  if (spike) {
    Monitor.print(F("EVENT,BED_MOTION,"));
    Monitor.println(now);
    bed_occupied = !bed_occupied;
    digitalWrite(LED_PIN, bed_occupied ? HIGH : LOW);
  }

  if (current_mode == LOWPOWER) return;

  // NORMAL: vollständige CSV-Zeile senden (Werte in g / dps)
  Monitor.print(now);
  Monitor.print(',');
  Monitor.print(ax, 5); Monitor.print(',');
  Monitor.print(ay, 5); Monitor.print(',');
  Monitor.print(az, 5); Monitor.print(',');
  Monitor.print(gx, 3); Monitor.print(',');
  Monitor.print(gy, 3); Monitor.print(',');
  Monitor.println(gz, 3);
}
