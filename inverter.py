"""
Modbus RTU communication with SunGoldPower / SRNE inverters.
Provides InverterController class for reading registers and controlling modes.
"""

import serial
import struct
import time
import logging

from config import (
    REG_SOC, REG_BATTERY_VOLTAGE, REG_BATTERY_CURRENT,
    REG_PV1_VOLTAGE, REG_PV1_CURRENT, REG_PV1_POWER,
    REG_PV2_VOLTAGE, REG_PV2_CURRENT, REG_PV2_POWER,
    REG_TOTAL_CHARGE_POWER, REG_GRID_VOLTAGE, REG_GRID_FREQUENCY,
    REG_OUTPUT_VOLTAGE, REG_LOAD_CURRENT, REG_LOAD_POWER,
    REG_LOAD_APPARENT_POWER, REG_DEVICE_STATE,
    REG_PV_GENERATION_TODAY, REG_LOAD_CONSUMPTION_TODAY,
    REG_GRID_CONSUMPTION_TODAY,
    REG_OUTPUT_MODE, REG_CHARGE_MODE, REG_PASSWORD,
    MODE_NAMES, CHARGE_NAMES, INVERTER_PASSWORD,
)


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

    def _read_with_delay(self, register, delay=0.3):
        """Read a register and sleep to avoid bus contention."""
        val = self.read_register(register)
        time.sleep(delay)
        return val

    def read_basic_status(self):
        """Read core status values needed for control logic. Returns dict or None."""
        soc = self._read_with_delay(REG_SOC)
        grid_voltage_raw = self._read_with_delay(REG_GRID_VOLTAGE)
        output_mode = self._read_with_delay(REG_OUTPUT_MODE)
        charge_mode = self._read_with_delay(REG_CHARGE_MODE)

        if soc is None or grid_voltage_raw is None or output_mode is None:
            return None

        grid_voltage = grid_voltage_raw * 0.1
        grid_present = grid_voltage > 50

        return {
            "soc": soc,
            "grid_voltage": grid_voltage,
            "grid_present": grid_present,
            "output_mode": output_mode,
            "output_mode_name": MODE_NAMES.get(output_mode, str(output_mode)),
            "charge_mode": charge_mode,
            "charge_mode_name": CHARGE_NAMES.get(charge_mode, str(charge_mode)),
        }

    def read_full_status(self):
        """Read all monitoring registers for the dashboard. Returns dict or None."""
        soc = self._read_with_delay(REG_SOC)
        batt_v_raw = self._read_with_delay(REG_BATTERY_VOLTAGE)
        batt_i_raw = self._read_with_delay(REG_BATTERY_CURRENT)
        pv1_power = self._read_with_delay(REG_PV1_POWER)
        pv2_power = self._read_with_delay(REG_PV2_POWER)
        load_power = self._read_with_delay(REG_LOAD_POWER)
        grid_v_raw = self._read_with_delay(REG_GRID_VOLTAGE)
        grid_freq_raw = self._read_with_delay(REG_GRID_FREQUENCY)
        output_v_raw = self._read_with_delay(REG_OUTPUT_VOLTAGE)
        load_current_raw = self._read_with_delay(REG_LOAD_CURRENT)
        total_charge_power = self._read_with_delay(REG_TOTAL_CHARGE_POWER)
        output_mode = self._read_with_delay(REG_OUTPUT_MODE)
        charge_mode = self._read_with_delay(REG_CHARGE_MODE)

        # Daily stats
        pv_gen_today_raw = self._read_with_delay(REG_PV_GENERATION_TODAY)
        load_today_raw = self._read_with_delay(REG_LOAD_CONSUMPTION_TODAY)
        grid_today_raw = self._read_with_delay(REG_GRID_CONSUMPTION_TODAY)

        if soc is None or grid_v_raw is None or output_mode is None:
            return None

        grid_voltage = grid_v_raw * 0.1 if grid_v_raw is not None else 0
        grid_present = grid_voltage > 50

        # Battery current is signed (two's complement for 16-bit)
        batt_current = 0
        if batt_i_raw is not None:
            if batt_i_raw > 32767:
                batt_current = (batt_i_raw - 65536) * 0.1
            else:
                batt_current = batt_i_raw * 0.1

        return {
            "soc": soc,
            "battery_voltage": (batt_v_raw * 0.1) if batt_v_raw is not None else None,
            "battery_current": batt_current,
            "pv1_power": pv1_power if pv1_power is not None else 0,
            "pv2_power": pv2_power if pv2_power is not None else 0,
            "pv_total_power": (pv1_power or 0) + (pv2_power or 0),
            "total_charge_power": total_charge_power if total_charge_power is not None else 0,
            "load_power": load_power if load_power is not None else 0,
            "load_current": (load_current_raw * 0.1) if load_current_raw is not None else 0,
            "grid_voltage": grid_voltage,
            "grid_frequency": (grid_freq_raw * 0.01) if grid_freq_raw is not None else 0,
            "grid_present": grid_present,
            "output_voltage": (output_v_raw * 0.1) if output_v_raw is not None else 0,
            "output_mode": output_mode,
            "output_mode_name": MODE_NAMES.get(output_mode, str(output_mode)),
            "charge_mode": charge_mode,
            "charge_mode_name": CHARGE_NAMES.get(charge_mode, str(charge_mode)),
            "pv_generation_today": (pv_gen_today_raw * 0.1) if pv_gen_today_raw is not None else 0,
            "load_consumption_today": (load_today_raw * 0.1) if load_today_raw is not None else 0,
            "grid_consumption_today": (grid_today_raw * 0.1) if grid_today_raw is not None else 0,
        }
