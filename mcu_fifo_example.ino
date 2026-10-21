// Firmware tham khảo cho STM32F103 (Arduino STM32 core).
// Đổi chân theo PCB thật. Thử bằng LED và ngắt nguồn khí nén trước khi đấu van.

#include <Arduino.h>

const uint32_t SERIAL_BAUD = 115200;

// Sơ đồ chân gợi ý cho Blue Pill STM32F103C8T6.
const uint32_t SENSOR_PIN = PA1;       // Cảm biến hồng ngoại, active LOW
const uint32_t MOTOR_PWM_PIN = PA0;    // ENA L298N, chân có PWM
const uint32_t MOTOR_IN1_PIN = PB0;
const uint32_t MOTOR_IN2_PIN = PB1;
const uint32_t LINE_RELAY_PIN = PB12;  // Relay cấp phép van/khí nén
const uint32_t P1_PIN = PB13;
const uint32_t P2_PIN = PB14;
const uint32_t REJECT_PIN = PB15;

const uint32_t PISTON_PULSE_MS = 250;
const uint32_t HEARTBEAT_MS = 1000;
const uint32_t SENSOR_DEBOUNCE_MS = 60;
const uint8_t QUEUE_CAPACITY = 32;
const uint8_t DEFAULT_SPEED_PERCENT = 60;

struct Job {
  uint32_t eventId;
  char route[16];
  // Nội dung QR UTF-8 được PC mã hóa Base64 URL-safe, tối đa 72 byte gốc.
  char qrBase64[97];
};

Job jobs[QUEUE_CAPACITY];
uint8_t queueHead = 0;
uint8_t queueTail = 0;
uint8_t queueCount = 0;

uint32_t recentIds[QUEUE_CAPACITY] = {0};
uint8_t recentIndex = 0;

char inputLine[192];
uint16_t inputLength = 0;
bool systemRunning = false;
bool previousSensorState = HIGH;
bool rawSensorState = HIGH;
uint32_t sensorChangedAt = 0;
uint8_t motorSpeedPercent = DEFAULT_SPEED_PERCENT;

int activePistonPin = -1;
uint32_t activeEventId = 0;
uint32_t pistonOffAt = 0;
uint32_t lastHeartbeatAt = 0;

void sendState() {
  Serial.print("STATE,");
  Serial.println(systemRunning ? "RUNNING" : "STOPPED");
}

void sendSensor() {
  Serial.print("SENSOR,");
  Serial.println(previousSensorState == LOW ? 1 : 0);
}

void sendFifo() {
  Serial.print("FIFO,");
  Serial.print(queueCount);
  Serial.print(',');
  Serial.println(QUEUE_CAPACITY);
}

void sendSpeed() {
  Serial.print("SPEED,");
  Serial.println(motorSpeedPercent);
}

void sendStatus(const char *status, uint32_t eventId) {
  Serial.print(status);
  Serial.print(',');
  Serial.println(eventId);
}

void applyMotor() {
  if (!systemRunning || motorSpeedPercent == 0) {
    analogWrite(MOTOR_PWM_PIN, 0);
    digitalWrite(MOTOR_IN1_PIN, LOW);
    digitalWrite(MOTOR_IN2_PIN, LOW);
    return;
  }
  digitalWrite(MOTOR_IN1_PIN, HIGH);
  digitalWrite(MOTOR_IN2_PIN, LOW);
  analogWrite(MOTOR_PWM_PIN, map(motorSpeedPercent, 0, 100, 0, 255));
}

void allActuatorsOff() {
  digitalWrite(P1_PIN, LOW);
  digitalWrite(P2_PIN, LOW);
  digitalWrite(REJECT_PIN, LOW);
  digitalWrite(LINE_RELAY_PIN, LOW);
  activePistonPin = -1;
  activeEventId = 0;
}

bool wasSeen(uint32_t eventId) {
  for (uint8_t i = 0; i < QUEUE_CAPACITY; i++) {
    if (recentIds[i] == eventId) return true;
  }
  return false;
}

void rememberId(uint32_t eventId) {
  recentIds[recentIndex] = eventId;
  recentIndex = (recentIndex + 1) % QUEUE_CAPACITY;
}

bool enqueueJob(uint32_t eventId, const char *route, const char *qrBase64) {
  if (queueCount >= QUEUE_CAPACITY) return false;
  Job &job = jobs[queueTail];
  job.eventId = eventId;
  strncpy(job.route, route, sizeof(job.route) - 1);
  job.route[sizeof(job.route) - 1] = '\0';
  strncpy(job.qrBase64, qrBase64, sizeof(job.qrBase64) - 1);
  job.qrBase64[sizeof(job.qrBase64) - 1] = '\0';
  queueTail = (queueTail + 1) % QUEUE_CAPACITY;
  queueCount++;
  return true;
}

bool dequeueJob(Job &job) {
  if (queueCount == 0) return false;
  job = jobs[queueHead];
  queueHead = (queueHead + 1) % QUEUE_CAPACITY;
  queueCount--;
  return true;
}

int pinForRoute(const char *route) {
  if (strcmp(route, "P1") == 0) return P1_PIN;
  if (strcmp(route, "P2") == 0) return P2_PIN;
  if (strcmp(route, "REJECT") == 0) return REJECT_PIN;
  if (strcmp(route, "HOLD") == 0) return -2;
  return -1;
}

void reportAllStatus() {
  sendState();
  sendSensor();
  sendFifo();
  sendSpeed();
}

void stopSystem() {
  systemRunning = false;
  applyMotor();
  if (activeEventId != 0) {
    Serial.print("ERR,");
    Serial.print(activeEventId);
    Serial.println(",STOPPED_DURING_ACTUATION");
  }
  allActuatorsOff();
  sendState();
  Serial.println("ACKCTRL,STOP");
}

void handleQueueCommand(char *idText, char *route, char *qrBase64) {
  if (!idText || !route || !qrBase64) {
    Serial.println("ERR,0,BAD_QUEUE_FORMAT");
    return;
  }

  uint32_t eventId = strtoul(idText, nullptr, 10);
  if (eventId == 0) {
    Serial.println("ERR,0,BAD_EVENT_ID");
    return;
  }
  if (wasSeen(eventId)) {
    sendStatus("ACK", eventId);
    sendFifo();
    return;
  }
  if (pinForRoute(route) == -1) {
    Serial.print("ERR,");
    Serial.print(eventId);
    Serial.println(",NO_ROUTE");
    return;
  }
  if (!enqueueJob(eventId, route, qrBase64)) {
    Serial.print("ERR,");
    Serial.print(eventId);
    Serial.println(",QUEUE_FULL");
    return;
  }

  rememberId(eventId);
  sendStatus("ACK", eventId);
  sendFifo();
}

void handleCommand(char *line) {
  char *command = strtok(line, ",");
  if (!command) return;

  if (strcmp(command, "HELLO") == 0) {
    Serial.println("READY");
    reportAllStatus();
  } else if (strcmp(command, "QUEUE") == 0) {
    char *idText = strtok(nullptr, ",");
    char *route = strtok(nullptr, ",");
    char *qrBase64 = strtok(nullptr, ",");
    handleQueueCommand(idText, route, qrBase64);
  } else if (strcmp(command, "START") == 0) {
    systemRunning = true;
    applyMotor();
    sendState();
    Serial.println("ACKCTRL,START");
  } else if (strcmp(command, "STOP") == 0) {
    stopSystem();
  } else if (strcmp(command, "SPEED") == 0) {
    char *valueText = strtok(nullptr, ",");
    if (!valueText) {
      Serial.println("ERR,0,BAD_SPEED");
      return;
    }
    int value = atoi(valueText);
    if (value < 0 || value > 100) {
      Serial.println("ERR,0,BAD_SPEED");
      return;
    }
    motorSpeedPercent = (uint8_t)value;
    applyMotor();
    sendSpeed();
    Serial.println("ACKCTRL,SPEED");
  } else if (strcmp(command, "GET_STATUS") == 0) {
    reportAllStatus();
  } else {
    Serial.println("ERR,0,BAD_COMMAND");
  }
}

void readSerialCommands() {
  while (Serial.available() > 0) {
    char value = (char)Serial.read();
    if (value == '\n') {
      inputLine[inputLength] = '\0';
      if (inputLength > 0) handleCommand(inputLine);
      inputLength = 0;
    } else if (value != '\r') {
      if (inputLength < sizeof(inputLine) - 1) {
        inputLine[inputLength++] = value;
      } else {
        inputLength = 0;
        Serial.println("ERR,0,LINE_TOO_LONG");
      }
    }
  }
}

void processPistonTimeout() {
  if (activePistonPin >= 0 && (int32_t)(millis() - pistonOffAt) >= 0) {
    digitalWrite(activePistonPin, LOW);
    digitalWrite(LINE_RELAY_PIN, LOW);
    sendStatus("DONE", activeEventId);
    activePistonPin = -1;
    activeEventId = 0;
  }
}

void processSensor() {
  bool sample = digitalRead(SENSOR_PIN);
  if (sample != rawSensorState) {
    rawSensorState = sample;
    sensorChangedAt = millis();
  }
  if (rawSensorState == previousSensorState || millis() - sensorChangedAt < SENSOR_DEBOUNCE_MS) {
    return;
  }

  bool oldSensorState = previousSensorState;
  previousSensorState = rawSensorState;
  sendSensor();
  bool fallingEdge = oldSensorState == HIGH && previousSensorState == LOW;

  if (!systemRunning || !fallingEdge || activePistonPin >= 0) return;

  Job job;
  if (!dequeueJob(job)) {
    Serial.println("ERR,0,SENSOR_WITH_EMPTY_QUEUE");
    return;
  }
  sendFifo();

  int pin = pinForRoute(job.route);
  if (pin == -2) {
    sendStatus("DONE", job.eventId);
    return;
  }
  if (pin < 0) {
    Serial.print("ERR,");
    Serial.print(job.eventId);
    Serial.println(",NO_ROUTE");
    return;
  }

  activePistonPin = pin;
  activeEventId = job.eventId;
  Serial.print("ACTIVE,");
  Serial.print(job.eventId);
  Serial.print(',');
  Serial.println(job.route);
  digitalWrite(LINE_RELAY_PIN, HIGH);
  digitalWrite(activePistonPin, HIGH);
  pistonOffAt = millis() + PISTON_PULSE_MS;
}

void sendHeartbeat() {
  if (millis() - lastHeartbeatAt < HEARTBEAT_MS) return;
  lastHeartbeatAt = millis();
  Serial.print("HEARTBEAT,");
  Serial.print(millis());
  Serial.print(',');
  Serial.print(systemRunning ? "RUNNING" : "STOPPED");
  Serial.print(',');
  Serial.print(previousSensorState == LOW ? 1 : 0);
  Serial.print(',');
  Serial.print(queueCount);
  Serial.print(',');
  Serial.println(motorSpeedPercent);
}

void setup() {
  pinMode(SENSOR_PIN, INPUT_PULLUP);
  pinMode(MOTOR_PWM_PIN, OUTPUT);
  pinMode(MOTOR_IN1_PIN, OUTPUT);
  pinMode(MOTOR_IN2_PIN, OUTPUT);
  pinMode(LINE_RELAY_PIN, OUTPUT);
  pinMode(P1_PIN, OUTPUT);
  pinMode(P2_PIN, OUTPUT);
  pinMode(REJECT_PIN, OUTPUT);

  applyMotor();
  allActuatorsOff();
  previousSensorState = digitalRead(SENSOR_PIN);
  rawSensorState = previousSensorState;

  Serial.begin(SERIAL_BAUD);
  delay(100);
  Serial.println("READY");
  reportAllStatus();
}

void loop() {
  readSerialCommands();
  processPistonTimeout();
  processSensor();
  sendHeartbeat();
}
