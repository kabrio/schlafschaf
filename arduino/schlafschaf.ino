/**
 * Schlafschaf – Arduino Uno Q Sleep Tracker
 *
 * Liest KY-038 Mikrofon (A0) und MPU-6050 Beschleunigungssensor (I2C)
 * und sendet CSV-Daten über Serial an die Linux-Seite des Uno Q.
 *
 * Hardware:
 *   KY-038 AO  → A0
 *   MPU-6050 SDA → A4 (SDA)
 *   MPU-6050 SCL → A5 (SCL)
 *   MPU-6050 VCC → 3.3V
 *
 * CSV-Format: millis,sound,ax,ay,az,gx,gy,gz
 * Baudrate: 115200
 */

#include <Wire.h>

// MPU-6050 I2C Adresse
#define MPU_ADDR 0x68

// MPU-6050 Register
#define REG_PWR_MGMT_1 0x6B
#define REG_WHO_AM_I   0x75
#define REG_ACCEL_XOUT 0x3B

bool mpu_ok = false;

// MPU-6050 initialisieren
bool initMPU() {
  // Wake-up: PWR_MGMT_1 = 0x00
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(REG_PWR_MGMT_1);
  Wire.write(0x00);
  if (Wire.endTransmission(true) != 0) return false;

  delay(100);

  // WHO_AM_I prüfen
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(REG_WHO_AM_I);
  Wire.endTransmission(false);
  Wire.requestFrom(MPU_ADDR, 1, true);
  if (!Wire.available()) return false;
  uint8_t who = Wire.read();
  return (who == 0x68);
}

// 6 Rohwerte (ax, ay, az, gx, gy, gz) lesen
bool readMPU(int16_t* ax, int16_t* ay, int16_t* az,
             int16_t* gx, int16_t* gy, int16_t* gz) {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(REG_ACCEL_XOUT);
  if (Wire.endTransmission(false) != 0) return false;

  Wire.requestFrom(MPU_ADDR, 14, true);
  if (Wire.available() < 14) return false;

  *ax = (Wire.read() << 8) | Wire.read();
  *ay = (Wire.read() << 8) | Wire.read();
  *az = (Wire.read() << 8) | Wire.read();
  // Temperatur (2 Bytes) überspringen
  Wire.read(); Wire.read();
  *gx = (Wire.read() << 8) | Wire.read();
  *gy = (Wire.read() << 8) | Wire.read();
  *gz = (Wire.read() << 8) | Wire.read();

  return true;
}

void setup() {
  Serial.begin(115200);
  while (!Serial) delay(10);

  Wire.begin();
  delay(200);

  mpu_ok = initMPU();

  if (mpu_ok) {
    Serial.println("READY");
  } else {
    Serial.println("READY_NO_MPU");
  }
}

void loop() {
  unsigned long ts = millis();
  int sound = analogRead(A0);

  int16_t ax = -1, ay = -1, az = -1;
  int16_t gx = -1, gy = -1, gz = -1;

  if (mpu_ok) {
    if (!readMPU(&ax, &ay, &az, &gx, &gy, &gz)) {
      // MPU-Fehler: Neuinitialisierung versuchen
      mpu_ok = initMPU();
    }
  }

  // CSV ausgeben: millis,sound,ax,ay,az,gx,gy,gz
  Serial.print(ts);
  Serial.print(',');
  Serial.print(sound);
  Serial.print(',');
  Serial.print(ax);
  Serial.print(',');
  Serial.print(ay);
  Serial.print(',');
  Serial.print(az);
  Serial.print(',');
  Serial.print(gx);
  Serial.print(',');
  Serial.print(gy);
  Serial.print(',');
  Serial.println(gz);

  delay(950);  // ~1 Hz (950ms + Verarbeitungszeit ≈ 1000ms)
}
