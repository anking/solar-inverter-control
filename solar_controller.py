#!/usr/bin/env python3
"""
SunGoldPower / SRNE Inverter Solar Controller
----------------------------------------------
Controls inverter output mode based on battery SOC to maintain
a solar buffer in the battery while protecting against deep discharge.

Algorithm:
  - When SOC >= HIGH_THRESHOLD (92%): Switch to SBU (solar+battery first)
    Battery can discharge to power loads, absorbing excess solar.
  - When SOC <= LOW_THRESHOLD (82%): Switch to UTI (grid first)
    Battery stops discharging, solar still charges it back up.
  - When grid is down: Always SBU (use battery for backup)
  - When grid returns: Resume normal SOC-based logic

Hysteresis between 82-92% prevents rapid mode switching.

Connection: USB-RS485 adapter → inverter WIFI port (RJ45 pins 7=A, 8=B)
Protocol: Modbus RTU, 9600-8N1, slave address 1
"""

import serial
import struct
import time
import logging
import signal
import sys
from datetime import datetime

# =============================================================================
# CONFIGURATION - Adjust these to your needs
# =============================================================================

SERIAL_PORT = "/dev/ttyUSB0"
BAUD_RATE = 9600
SLAVE_ADDRESS = 1

# SOC thresholds (%)
LOW_THRESHOLD = 82      # Switch to UTI (grid first) when SOC drops to this
HIGH_THRESHOLD = 92     # Switch back to SBU (battery first) when SOC reaches this

# Polling interval (seconds)
POLL_INTERVAL = 10

# Password for writing registers (your inverter uses 6666)
INVERTER_PASSWORD = 6666

# Register addresses
REG_SOC = 0x0100           # Battery SOC (read)
REG_GRID_VOLTAGE = 0x0213  # Grid voltage (read, 0.1V units)
REG_DEVICE_STATE = 0x0210  # Device state (read)
REG_OUTPUT_MODE = 0xE204   # Output mode (read/write)
REG_CHARGE_MODE = 0xE20F   # Charge mode (read/write)
REG_PASSWORD = 0xE203      # Password input (write)

# Mode values
MODE_UTI = 1    # Grid first (battery protected)
MODE_SBU = 2    # Solar/Battery/Utility
MODE_SUB = 3    # Mixed (default)
MODE_SOL = 0    # Solar first

CHARGE_SNU = 2  # PV + Grid charging
CHARGE_OSO = 3  # PV only charging

MODE_NAMES = {0: "SOL", 1: "UTI", 2: "SBU", 3: "SUB"}
CHARGE_NAMES = {2: "SNU", 3: "OSO"}

# Logging
LOG_FILE = "/home/pi/solar_controller.log"
LOG_LEVEL = logging.INFO


# =============================================================================
# MODBUS FUNCTIONS
# =============================================================================

def crc16(data):
    """Calculate Modbus CRC16."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return struct.pack('<H', crc)


class InverterController:
    def __init__(self, port, baud, slave_addr):
        self.port = port
        self.baud = baud
        self.slave_addr = slave_addr
        self.ser = None
        self.authenticated = False
        self.current_mode = None
        self.grid_was_down = False

    def connect(self):
        """Open serial connection."""
        try:
            self.ser = serial.Serial(
                self.port,
                self.baud,
                bytesize=8,
                parity='N',
                stopbits=1,
                timeout=2
            )
            logging.info(f"Connected to {self.port} at {self.baud} baud")
            return True
        except serial.SerialException as e:
            logging.error(f"Failed to connect: {e}")
            return False

    def disconnect(self):
        """Close serial connection."""
        if self.ser and self.ser.is_open:
            self.ser.close()
            logging.info("Disconnected")

    def _send_receive(self, request):
        """Send a Modbus request and return the response."""
        try:
            self.ser.reset_input_buffer()
            self.ser.write(request)
            time.sleep(0.5)
            response = self.ser.read(100)
            return response
        except serial.SerialException as e:
            logging.error(f"Serial error: {e}")
            return b''

    def read_register(self, register):
        """Read a single holding register. Returns value or None on error."""
        msg = bytes([self.slave_addr, 0x03,
                     (register >> 8) & 0xFF, register & 0xFF,
                     0x00, 0x01])
        msg += crc16(msg)

        resp = self._send_receive(msg)

        if len(resp) == 7 and resp[1] == 0x03:
            return (resp[3] << 8) | resp[4]
        elif len(resp) >= 3 and resp[1] == 0x83:
            logging.warning(f"Read error for 0x{register:04X}: exception code {resp[2]}")
            return None
        else:
            logging.warning(f"Bad response reading 0x{register:04X}: {resp.hex() if resp else 'empty'}")
            return None

    def write_register(self, register, value):
        """Write a single holding register. Returns True on success."""
        msg = bytes([self.slave_addr, 0x06,
                     (register >> 8) & 0xFF, register & 0xFF,
                     (value >> 8) & 0xFF, value & 0xFF])
        msg += crc16(msg)

        resp = self._send_receive(msg)

        if len(resp) >= 2 and resp[1] == 0x06:
            return True
        elif len(resp) >= 3 and resp[1] == 0x86:
            logging.error(f"Write error for 0x{register:04X}={value}: exception code {resp[2]}")
            return False
        else:
            logging.error(f"Bad response writing 0x{register:04X}: {resp.hex() if resp else 'empty'}")
            return False

    def authenticate(self):
        """Send password to unlock write access."""
        if self.write_register(REG_PASSWORD, INVERTER_PASSWORD):
            self.authenticated = True
            logging.debug("Authentication successful")
            return True
        else:
            self.authenticated = False
            logging.error("Authentication failed")
            return False

    def set_output_mode(self, mode):
        """Set the inverter output mode (UTI/SBU/SUB/SOL)."""
        if not self.authenticate():
            return False
        time.sleep(0.3)

        if self.write_register(REG_OUTPUT_MODE, mode):
            mode_name = MODE_NAMES.get(mode, str(mode))
            logging.info(f"Output mode changed to {mode_name} ({mode})")
            self.current_mode = mode
            return True
        return False

    def set_charge_mode(self, mode):
        """Set the charge mode (SNU/OSO)."""
        if not self.authenticate():
            return False
        time.sleep(0.3)

        if self.write_register(REG_CHARGE_MODE, mode):
            mode_name = CHARGE_NAMES.get(mode, str(mode))
            logging.info(f"Charge mode changed to {mode_name} ({mode})")
            return True
        return False

    def read_status(self):
        """Read all relevant status values. Returns dict or None on error."""
        soc = self.read_register(REG_SOC)
        time.sleep(0.3)
        grid_voltage_raw = self.read_register(REG_GRID_VOLTAGE)
        time.sleep(0.3)
        output_mode = self.read_register(REG_OUTPUT_MODE)
        time.sleep(0.3)
        charge_mode = self.read_register(REG_CHARGE_MODE)
        time.sleep(0.3)

        if soc is None or grid_voltage_raw is None or output_mode is None:
            return None

        grid_voltage = grid_voltage_raw * 0.1
        grid_present = grid_voltage > 50  # > 5V means grid is present

        return {
            "soc": soc,
            "grid_voltage": grid_voltage,
            "grid_present": grid_present,
            "output_mode": output_mode,
            "output_mode_name": MODE_NAMES.get(output_mode, str(output_mode)),
            "charge_mode": charge_mode,
            "charge_mode_name": CHARGE_NAMES.get(charge_mode, str(charge_mode)),
        }

    def run_control_loop(self):
        """Main control loop."""
        logging.info("=" * 60)
        logging.info("Solar Controller Starting")
        logging.info(f"  Low threshold:  {LOW_THRESHOLD}% (switch to UTI)")
        logging.info(f"  High threshold: {HIGH_THRESHOLD}% (switch to SBU)")
        logging.info(f"  Poll interval:  {POLL_INTERVAL}s")
        logging.info("=" * 60)

        # Set initial mode based on current SOC
        status = self.read_status()
        if status:
            soc = status["soc"]
            if soc >= HIGH_THRESHOLD:
                logging.info(f"Startup: SOC={soc}% >= {HIGH_THRESHOLD}% → SBU + OSO (use battery, solar-only charge)")
                self.set_output_mode(MODE_SBU)
                time.sleep(0.5)
                self.set_charge_mode(CHARGE_OSO)
            elif soc <= LOW_THRESHOLD:
                logging.info(f"Startup: SOC={soc}% <= {LOW_THRESHOLD}% → UTI + SNU (protect battery, grid+solar charge)")
                self.set_output_mode(MODE_UTI)
                time.sleep(0.5)
                self.set_charge_mode(CHARGE_SNU)
            else:
                logging.info(f"Startup: SOC={soc}% in hysteresis zone → UTI + OSO (charge from solar only)")
                self.set_output_mode(MODE_UTI)
                time.sleep(0.5)
                self.set_charge_mode(CHARGE_OSO)

        time.sleep(1)
        consecutive_errors = 0
        max_errors = 5

        while True:
            try:
                status = self.read_status()

                if status is None:
                    consecutive_errors += 1
                    logging.warning(f"Failed to read status ({consecutive_errors}/{max_errors})")
                    if consecutive_errors >= max_errors:
                        logging.error("Too many consecutive errors, reconnecting...")
                        self.disconnect()
                        time.sleep(5)
                        if not self.connect():
                            time.sleep(30)
                            continue
                        consecutive_errors = 0
                    time.sleep(POLL_INTERVAL)
                    continue

                consecutive_errors = 0
                soc = status["soc"]
                grid_present = status["grid_present"]
                current_mode = status["output_mode"]
                self.current_mode = current_mode

                timestamp = datetime.now().strftime("%H:%M:%S")
                logging.info(
                    f"[{timestamp}] SOC={soc}% | "
                    f"Grid={'ON' if grid_present else 'OFF'} ({status['grid_voltage']:.1f}V) | "
                    f"Mode={status['output_mode_name']} | "
                    f"Charge={status['charge_mode_name']}"
                )

                # --- CONTROL LOGIC ---

                # CASE 1: Grid is down - full battery backup
                if not grid_present:
                    if not self.grid_was_down:
                        logging.warning("GRID DOWN - switching to SBU for battery backup")
                        self.grid_was_down = True
                    if current_mode != MODE_SBU:
                        self.set_output_mode(MODE_SBU)

                # CASE 2: Grid just came back
                elif self.grid_was_down and grid_present:
                    logging.info("GRID RESTORED - resuming SOC-based control")
                    self.grid_was_down = False
                    # Fall through to SOC logic below

                # CASE 3: Grid present, SOC is low - protect battery, charge with grid+solar
                if grid_present and soc <= LOW_THRESHOLD:
                    if current_mode != MODE_UTI:
                        logging.info(
                            f"SOC {soc}% <= {LOW_THRESHOLD}% → "
                            f"Switching to UTI (protect battery)"
                        )
                        self.set_output_mode(MODE_UTI)
                        time.sleep(0.5)
                    if status["charge_mode"] != CHARGE_SNU:
                        logging.info(
                            f"SOC {soc}% <= {LOW_THRESHOLD}% → "
                            f"Switching to SNU (grid+solar charging)"
                        )
                        self.set_charge_mode(CHARGE_SNU)

                # CASE 4: Grid present, SOC is high - use battery, solar-only charge
                elif grid_present and soc >= HIGH_THRESHOLD:
                    if current_mode != MODE_SBU:
                        logging.info(
                            f"SOC {soc}% >= {HIGH_THRESHOLD}% → "
                            f"Switching to SBU (battery + solar first)"
                        )
                        self.set_output_mode(MODE_SBU)
                        time.sleep(0.5)
                    if status["charge_mode"] != CHARGE_OSO:
                        logging.info(
                            f"SOC {soc}% >= {HIGH_THRESHOLD}% → "
                            f"Switching to OSO (solar-only charging)"
                        )
                        self.set_charge_mode(CHARGE_OSO)

                # CASE 5: Grid present, in hysteresis zone
                # Keep current output mode, but ensure charge is solar-only
                elif grid_present and soc > LOW_THRESHOLD and soc < HIGH_THRESHOLD:
                    if status["charge_mode"] != CHARGE_OSO:
                        logging.info(
                            f"SOC {soc}% in hysteresis zone → "
                            f"Switching to OSO (solar-only charging)"
                        )
                        self.set_charge_mode(CHARGE_OSO)

            except KeyboardInterrupt:
                raise
            except Exception as e:
                logging.error(f"Unexpected error: {e}")
                consecutive_errors += 1

            time.sleep(POLL_INTERVAL)


# =============================================================================
# MAIN
# =============================================================================

def setup_logging():
    """Configure logging to both file and console."""
    logger = logging.getLogger()
    logger.setLevel(LOG_LEVEL)

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(LOG_LEVEL)
    console.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console)

    # File handler
    try:
        file_handler = logging.FileHandler(LOG_FILE)
        file_handler.setLevel(LOG_LEVEL)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        logger.addHandler(file_handler)
    except PermissionError:
        logging.warning(f"Cannot write to {LOG_FILE}, logging to console only")


def signal_handler(sig, frame):
    """Handle Ctrl+C gracefully."""
    logging.info("\nShutting down solar controller...")
    sys.exit(0)


def main():
    setup_logging()
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    controller = InverterController(SERIAL_PORT, BAUD_RATE, SLAVE_ADDRESS)

    if not controller.connect():
        logging.error("Could not connect to inverter. Exiting.")
        sys.exit(1)

    try:
        controller.run_control_loop()
    except KeyboardInterrupt:
        logging.info("\nStopped by user")
    finally:
        controller.disconnect()


if __name__ == "__main__":
    main()
