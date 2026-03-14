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
    REG_LOAD_APPARENT_POWER, REG_LOAD_PERCENT, REG_DEVICE_STATE,
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
                timeout=1
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

    def _send_receive(self, request, expected_bytes=7):
        """Send a Modbus request and return the response."""
        try:
            self.ser.reset_input_buffer()
            self.ser.write(request)
            time.sleep(0.1)
            response = self.ser.read(max(expected_bytes, 7))
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

    def read_registers(self, start_register, count):
        """Read multiple consecutive holding registers. Returns dict {register: value} or None."""
        msg = bytes([self.slave_addr, 0x03,
                     (start_register >> 8) & 0xFF, start_register & 0xFF,
                     (count >> 8) & 0xFF, count & 0xFF])
        msg += crc16(msg)

        expected_bytes = 3 + count * 2 + 2  # addr + fc + bytecount + data + crc
        resp = self._send_receive(msg, expected_bytes)

        if len(resp) >= 3 + count * 2 and resp[1] == 0x03 and resp[2] == count * 2:
            result = {}
            for i in range(count):
                reg = start_register + i
                val = (resp[3 + i * 2] << 8) | resp[3 + i * 2 + 1]
                result[reg] = val
            return result
        elif len(resp) >= 3 and resp[1] == 0x83:
            logging.warning(f"Bulk read error at 0x{start_register:04X} x{count}: exception code {resp[2]}")
            return None
        else:
            logging.warning(f"Bad bulk response at 0x{start_register:04X} x{count}: {resp.hex() if resp else 'empty'}")
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

    def _read_with_delay(self, register, delay=0.05):
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
        """Read all monitoring registers for the dashboard using bulk reads. Returns dict or None."""
        # Bulk read 1: 0x0100–0x0111 (18 regs) — SOC, battery, PV1, charge power, PV2
        blk1 = self.read_registers(0x0100, 18)
        time.sleep(0.05)

        # Bulk read 2: 0x0210–0x021F (16 regs) — device state, grid, output, load
        blk2 = self.read_registers(0x0210, 16)
        time.sleep(0.05)

        # Bulk read 3: 0xE204 and 0xE20F — output mode and charge mode
        # These are 12 regs apart (0xE204–0xE20F), read the range
        blk3 = self.read_registers(0xE204, 12)
        time.sleep(0.05)

        # Bulk read 4: 0xF02F–0xF03D (15 regs) — daily stats
        blk4 = self.read_registers(0xF02F, 15)

        if blk1 is None or blk2 is None or blk3 is None:
            return None

        def g(blk, reg, default=0):
            """Get register value from a bulk read block."""
            if blk is None:
                return default
            return blk.get(reg, default)

        soc = g(blk1, REG_SOC)
        batt_v_raw = g(blk1, REG_BATTERY_VOLTAGE)
        batt_i_raw = g(blk1, REG_BATTERY_CURRENT)
        pv1_power = g(blk1, REG_PV1_POWER)
        pv2_power = g(blk1, REG_PV2_POWER)
        total_charge_power = g(blk1, REG_TOTAL_CHARGE_POWER)

        grid_v_raw = g(blk2, REG_GRID_VOLTAGE)
        grid_freq_raw = g(blk2, REG_GRID_FREQUENCY)
        output_v_raw = g(blk2, REG_OUTPUT_VOLTAGE)
        load_current_raw = g(blk2, REG_LOAD_CURRENT)
        load_power = g(blk2, REG_LOAD_POWER)

        output_mode = g(blk3, REG_OUTPUT_MODE)
        charge_mode = g(blk3, REG_CHARGE_MODE)

        pv_gen_today_raw = g(blk4, REG_PV_GENERATION_TODAY) if blk4 else 0
        load_today_raw = g(blk4, REG_LOAD_CONSUMPTION_TODAY) if blk4 else 0
        grid_today_raw = g(blk4, REG_GRID_CONSUMPTION_TODAY) if blk4 else 0

        grid_voltage = grid_v_raw * 0.1
        grid_present = grid_voltage > 50

        # Battery current is signed (two's complement for 16-bit)
        if batt_i_raw > 32767:
            batt_current = (batt_i_raw - 65536) * 0.1
        else:
            batt_current = batt_i_raw * 0.1

        return {
            "soc": soc,
            "battery_voltage": batt_v_raw * 0.1,
            "battery_current": batt_current,
            "pv1_power": pv1_power,
            "pv2_power": pv2_power,
            "pv_total_power": pv1_power + pv2_power,
            "total_charge_power": total_charge_power,
            "load_power": load_power,
            "load_percent": g(blk2, REG_LOAD_PERCENT),
            "load_current": load_current_raw * 0.1,
            "grid_voltage": grid_voltage,
            "grid_frequency": grid_freq_raw * 0.01,
            "grid_present": grid_present,
            "output_voltage": output_v_raw * 0.1,
            "output_mode": output_mode,
            "output_mode_name": MODE_NAMES.get(output_mode, str(output_mode)),
            "charge_mode": charge_mode,
            "charge_mode_name": CHARGE_NAMES.get(charge_mode, str(charge_mode)),
            "pv_generation_today": pv_gen_today_raw * 0.1,
            "load_consumption_today": load_today_raw * 0.1,
            "grid_consumption_today": grid_today_raw * 0.1,
            # Extra registers available in blk1/blk2 for future use
            "pv1_voltage": g(blk1, REG_PV1_VOLTAGE) * 0.1,
            "pv1_current": g(blk1, REG_PV1_CURRENT) * 0.1,
            "pv2_voltage": g(blk1, REG_PV2_VOLTAGE) * 0.1,
            "pv2_current": g(blk1, REG_PV2_CURRENT) * 0.1,
            "load_apparent_power": g(blk2, REG_LOAD_APPARENT_POWER),
            "device_state": g(blk2, REG_DEVICE_STATE),
        }
