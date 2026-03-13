#!/usr/bin/env python3
"""
SunGoldPower / SRNE Inverter Solar Controller (Standalone)
-----------------------------------------------------------
Controls inverter output mode based on battery SOC to maintain
a solar buffer in the battery while protecting against deep discharge.

This is the standalone version. For the web dashboard + controller,
use server.py instead.

Connection: USB-RS485 adapter → inverter WIFI port (RJ45 pins 7=A, 8=B)
Protocol: Modbus RTU, 9600-8N1, slave address 1
"""

import time
import logging
import signal
import sys
from datetime import datetime

from config import (
    SERIAL_PORT, BAUD_RATE, SLAVE_ADDRESS,
    LOW_THRESHOLD, HIGH_THRESHOLD, POLL_INTERVAL, LOG_FILE, LOG_LEVEL,
    MODE_UTI, MODE_SBU, CHARGE_SNU, CHARGE_OSO,
)
from inverter import InverterController

LOG_LEVEL = logging.INFO


def run_control_loop(controller):
    """Main control loop with SOC-based mode switching."""
    logging.info("=" * 60)
    logging.info("Solar Controller Starting (standalone)")
    logging.info(f"  Low threshold:  {LOW_THRESHOLD}% (switch to UTI)")
    logging.info(f"  High threshold: {HIGH_THRESHOLD}% (switch to SBU)")
    logging.info(f"  Poll interval:  {POLL_INTERVAL}s")
    logging.info("=" * 60)

    # Set initial mode based on current SOC
    status = controller.read_basic_status()
    if status:
        soc = status["soc"]
        if soc >= HIGH_THRESHOLD:
            logging.info(f"Startup: SOC={soc}% >= {HIGH_THRESHOLD}% → SBU + OSO")
            controller.set_output_mode(MODE_SBU)
            time.sleep(0.5)
            controller.set_charge_mode(CHARGE_OSO)
        elif soc <= LOW_THRESHOLD:
            logging.info(f"Startup: SOC={soc}% <= {LOW_THRESHOLD}% → UTI + SNU")
            controller.set_output_mode(MODE_UTI)
            time.sleep(0.5)
            controller.set_charge_mode(CHARGE_SNU)
        else:
            logging.info(f"Startup: SOC={soc}% in hysteresis zone → UTI + OSO")
            controller.set_output_mode(MODE_UTI)
            time.sleep(0.5)
            controller.set_charge_mode(CHARGE_OSO)

    time.sleep(1)
    consecutive_errors = 0
    max_errors = 5

    while True:
        try:
            status = controller.read_basic_status()

            if status is None:
                consecutive_errors += 1
                logging.warning(f"Failed to read status ({consecutive_errors}/{max_errors})")
                if consecutive_errors >= max_errors:
                    logging.error("Too many consecutive errors, reconnecting...")
                    controller.disconnect()
                    time.sleep(5)
                    if not controller.connect():
                        time.sleep(30)
                        continue
                    consecutive_errors = 0
                time.sleep(POLL_INTERVAL)
                continue

            consecutive_errors = 0
            soc = status["soc"]
            grid_present = status["grid_present"]
            current_mode = status["output_mode"]
            controller.current_mode = current_mode

            timestamp = datetime.now().strftime("%H:%M:%S")
            logging.info(
                f"[{timestamp}] SOC={soc}% | "
                f"Grid={'ON' if grid_present else 'OFF'} ({status['grid_voltage']:.1f}V) | "
                f"Mode={status['output_mode_name']} | "
                f"Charge={status['charge_mode_name']}"
            )

            # CASE 1: Grid is down
            if not grid_present:
                if not controller.grid_was_down:
                    logging.warning("GRID DOWN - switching to SBU for battery backup")
                    controller.grid_was_down = True
                if current_mode != MODE_SBU:
                    controller.set_output_mode(MODE_SBU)

            # CASE 2: Grid just came back
            elif controller.grid_was_down and grid_present:
                logging.info("GRID RESTORED - resuming SOC-based control")
                controller.grid_was_down = False

            # CASE 3: SOC low - protect battery
            if grid_present and soc <= LOW_THRESHOLD:
                if current_mode != MODE_UTI:
                    logging.info(f"SOC {soc}% <= {LOW_THRESHOLD}% → UTI")
                    controller.set_output_mode(MODE_UTI)
                    time.sleep(0.5)
                if status["charge_mode"] != CHARGE_SNU:
                    logging.info(f"SOC {soc}% <= {LOW_THRESHOLD}% → SNU")
                    controller.set_charge_mode(CHARGE_SNU)

            # CASE 4: SOC high - use battery
            elif grid_present and soc >= HIGH_THRESHOLD:
                if current_mode != MODE_SBU:
                    logging.info(f"SOC {soc}% >= {HIGH_THRESHOLD}% → SBU")
                    controller.set_output_mode(MODE_SBU)
                    time.sleep(0.5)
                if status["charge_mode"] != CHARGE_OSO:
                    logging.info(f"SOC {soc}% >= {HIGH_THRESHOLD}% → OSO")
                    controller.set_charge_mode(CHARGE_OSO)

            # CASE 5: Hysteresis zone
            elif grid_present and LOW_THRESHOLD < soc < HIGH_THRESHOLD:
                if status["charge_mode"] != CHARGE_OSO:
                    logging.info(f"SOC {soc}% in hysteresis → OSO")
                    controller.set_charge_mode(CHARGE_OSO)

        except KeyboardInterrupt:
            raise
        except Exception as e:
            logging.error(f"Unexpected error: {e}")
            consecutive_errors += 1

        time.sleep(POLL_INTERVAL)


def setup_logging():
    """Configure logging to both file and console."""
    logger = logging.getLogger()
    logger.setLevel(LOG_LEVEL)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(LOG_LEVEL)
    console.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console)

    try:
        file_handler = logging.FileHandler(LOG_FILE)
        file_handler.setLevel(LOG_LEVEL)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        logger.addHandler(file_handler)
    except (PermissionError, FileNotFoundError):
        logging.warning(f"Cannot write to {LOG_FILE}, logging to console only")


def signal_handler(sig, frame):
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
        run_control_loop(controller)
    except KeyboardInterrupt:
        logging.info("\nStopped by user")
    finally:
        controller.disconnect()


if __name__ == "__main__":
    main()
