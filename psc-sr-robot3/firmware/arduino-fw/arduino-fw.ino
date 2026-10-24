#include <Arduino.h>

// PSC Robotics Arduino firmware.
//
// Works on Arduino boards whose `Serial` port is the USB port the robot is
// plugged into: Uno (R3 and R4), Nano, Nano Every, Mega 2560, Leonardo,
// Micro, Due (Programming Port), Zero (Programming Port), Nano 33 IoT and
// MKR boards.
//
// The robot library numbers pins like an Uno: 0-13 are digital pins and
// 14-19 are A0-A5. Pins 14-19 are mapped to the board's own A0-A5, which are
// numbered differently on boards such as the Mega, Due and Leonardo.

// We communicate with the Arduino at 115200 baud.
#define SERIAL_BAUD 115200

#define FW_VER 6

// The voltage that analogRead's full scale represents, in millivolts.
#if defined(ARDUINO_ARCH_SAM) || defined(ARDUINO_ARCH_SAMD) || defined(ARDUINO_ARCH_MBED) || defined(ARDUINO_ARCH_RP2040)
#define ANALOG_REFERENCE_MV 3300
#else
#define ANALOG_REFERENCE_MV 5000
#endif

int board_pin(int pin) {
  // Map the library's Uno-style pin numbers to this board's pin numbers.
  if (pin >= 14 && pin <= 19) {
    return A0 + (pin - 14);
  }
  return pin;
}

void setup() {
  Serial.begin(SERIAL_BAUD);
}

int read_pin() {
  // Convert the ASCII character to a pin number.
  // a -> 0, b -> 1, c -> 2, etc. A0-A5 (14-19) become this board's A0-A5.
  while (!Serial.available());
  int pin = Serial.read();
  return board_pin((int)(pin - 'a'));
}

long read_fixed_number(int digits) {
  long value = 0;
  for (int index = 0; index < digits; index++) {
    while (!Serial.available());
    int character = Serial.read();
    if (character < '0' || character > '9') {
      value = -1;
    } else if (value >= 0) {
      value = (value * 10) + (character - '0');
    }
  }
  return value;
}

void command_read() {
  int pin = read_pin();
  // Read from the expected pin.
  int level = digitalRead(pin);
  // Send back the result indicator.
  if (level == HIGH) {
    Serial.write('h');
  } else {
    Serial.write('l');
  }
}

void command_analog_read() {
  int pin = read_pin();
  int value = analogRead(pin);
  Serial.print(value);
}

void command_write(int level) {
  int pin = read_pin();
  digitalWrite(pin, level);
}

void command_mode(int mode) {
  int pin = read_pin();
  pinMode(pin, mode);
}

void command_ultrasound() {
  int pulse = read_pin();
  int echo = read_pin();

  // config pins to correct modes
  pinMode(pulse, OUTPUT);
  pinMode(echo, INPUT);

  // provide pulse to trigger reading
  digitalWrite(pulse, LOW);
  delayMicroseconds(2);
  digitalWrite(pulse, HIGH);
  delayMicroseconds(5);
  digitalWrite(pulse, LOW);

  // measure the echo time on the echo pin
  long duration = pulseIn(echo, HIGH, 60000);
  Serial.print(microsecondsToMm(duration));
}

void command_stepper_move() {
  while (!Serial.available());
  int step_logical = Serial.read() - 'a';
  while (!Serial.available());
  int direction_logical = Serial.read() - 'a';
  int direction = read_fixed_number(1);
  long steps = read_fixed_number(5);
  long step_delay_us = read_fixed_number(5);

  if (step_logical < 2 || step_logical > 19 || direction_logical < 2 || direction_logical > 19 ||
      step_logical == direction_logical || (direction != 0 && direction != 1) ||
      steps < 1 || steps > 99999 || step_delay_us < 2 || step_delay_us > 10000) {
    Serial.print("NACK:Invalid stepper parameters");
    return;
  }

  int step_pin = board_pin(step_logical);
  int direction_pin = board_pin(direction_logical);
  pinMode(step_pin, OUTPUT);
  pinMode(direction_pin, OUTPUT);
  digitalWrite(direction_pin, direction == 1 ? HIGH : LOW);

  for (long step = 0; step < steps; step++) {
    digitalWrite(step_pin, HIGH);
    delayMicroseconds(2);
    digitalWrite(step_pin, LOW);
    delayMicroseconds(step_delay_us);
  }
  Serial.print("OK");
}

void command_pwm_write() {
  // Set a pin's PWM duty cycle, from 0 (always low) to 255 (always high), for
  // DC motor drivers such as the DRV8871 (version 6 and later).
  while (!Serial.available());
  int logical = Serial.read() - 'a';
  long duty = read_fixed_number(3);

  if (logical < 2 || logical > 19 || duty < 0 || duty > 255) {
    Serial.print("NACK:Invalid PWM parameters");
    return;
  }

  int pin = board_pin(logical);
#ifdef digitalPinHasPWM
  if (duty != 0 && duty != 255 && !digitalPinHasPWM(pin)) {
    Serial.print("NACK:Pin ");
    Serial.print(logical);
    Serial.print(" does not support PWM");
    return;
  }
#endif

  pinMode(pin, OUTPUT);
  if (duty == 0) {
    digitalWrite(pin, LOW);  // Also turns off any PWM on the pin
  } else if (duty == 255) {
    digitalWrite(pin, HIGH);
  } else {
    analogWrite(pin, (int)duty);
  }
  Serial.print("OK");
}

long microsecondsToMm(long microseconds) {
  // The speed of sound is 340 m/s or 29 microseconds per centimeter.
  // The ping travels out and back, so to find the distance we need half
  // 10 x (us / 29 / 2)
  return (5 * microseconds / 29);
}

void loop() {
  // Fetch all commands that are in the buffer
  while (Serial.available()) {
    int selected_command = Serial.read();
    // Do something different based on what we got:
    switch (selected_command) {
      case 'a':
        command_analog_read();
        break;
      case 'r':
        command_read();
        break;
      case 'l':
        command_write(LOW);
        break;
      case 'h':
        command_write(HIGH);
        break;
      case 'i':
        command_mode(INPUT);
        break;
      case 'o':
        command_mode(OUTPUT);
        break;
      case 'p':
        command_mode(INPUT_PULLUP);
        break;
      case 'u':
        command_ultrasound();
        break;
      case 's':
        command_stepper_move();
        break;
      case 'w':
        command_pwm_write();
        break;
      case 'v':
        Serial.print("SRcustom:");
        Serial.print(FW_VER);
        break;
      case 'm':
        // The analog reference voltage in millivolts (version 5 and later).
        Serial.print(ANALOG_REFERENCE_MV);
        break;
      default:
        // A problem here: we do not know how to handle the command!
        // Just ignore this for now.
        break;
    }
    Serial.print("\n");
  }
}
