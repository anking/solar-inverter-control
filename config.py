"""
Shared configuration for solar inverter controller and dashboard.
Values can be overridden via environment variables.
"""

import json
import os

# =============================================================================
# SERIAL / MODBUS
# =============================================================================

SERIAL_PORT = os.environ.get("SOLAR_SERIAL_PORT", "/dev/ttyUSB0")
BAUD_RATE = int(os.environ.get("SOLAR_BAUD_RATE", "9600"))
SLAVE_ADDRESS = int(os.environ.get("SOLAR_SLAVE_ADDRESS", "1"))
INVERTER_PASSWORD = int(os.environ.get("SOLAR_INVERTER_PASSWORD", "6666"))

# =============================================================================
# CONTROL THRESHOLDS
# =============================================================================

LOW_THRESHOLD = int(os.environ.get("SOLAR_LOW_THRESHOLD", "82"))
CHARGE_THRESHOLD = int(os.environ.get("SOLAR_CHARGE_THRESHOLD", "85"))
HIGH_THRESHOLD = int(os.environ.get("SOLAR_HIGH_THRESHOLD", "92"))

# =============================================================================
# TIMING
# =============================================================================

POLL_INTERVAL = int(os.environ.get("SOLAR_POLL_INTERVAL", "10"))

# =============================================================================
# WEB SERVER
# =============================================================================

WEB_HOST = os.environ.get("SOLAR_WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.environ.get("SOLAR_WEB_PORT", "8080"))

# =============================================================================
# DATABASE
# =============================================================================

DB_PATH = os.environ.get("SOLAR_DB_PATH", "/home/pi/solar_data.db")

# =============================================================================
# LOGGING
# =============================================================================

LOG_FILE = os.environ.get("SOLAR_LOG_FILE", "/home/pi/solar_controller.log")

# =============================================================================
# REGISTER ADDRESSES
# =============================================================================

# Monitoring registers (read, FC 03)
REG_SOC = 0x0100
REG_BATTERY_VOLTAGE = 0x0101
REG_BATTERY_CURRENT = 0x0102
REG_PV1_VOLTAGE = 0x0107
REG_PV1_CURRENT = 0x0108
REG_PV1_POWER = 0x0109
REG_TOTAL_CHARGE_POWER = 0x010E
REG_PV2_VOLTAGE = 0x010F
REG_PV2_CURRENT = 0x0110
REG_PV2_POWER = 0x0111
REG_DEVICE_STATE = 0x0210
REG_GRID_VOLTAGE = 0x0213
REG_GRID_FREQUENCY = 0x0215
REG_OUTPUT_VOLTAGE = 0x0216
REG_LOAD_CURRENT = 0x0219
REG_LOAD_POWER = 0x021B
REG_LOAD_APPARENT_POWER = 0x021C

# Daily stats registers
REG_PV_GENERATION_TODAY = 0xF02F
REG_LOAD_CONSUMPTION_TODAY = 0xF030
REG_GRID_CONSUMPTION_TODAY = 0xF03D

# Settings registers (read/write, FC 06 after auth)
REG_OUTPUT_MODE = 0xE204
REG_CHARGE_MODE = 0xE20F
REG_PASSWORD = 0xE203

# =============================================================================
# MODE VALUES
# =============================================================================

MODE_SOL = 0
MODE_UTI = 1
MODE_SBU = 2
MODE_SUB = 3

CHARGE_SNU = 2
CHARGE_OSO = 3

MODE_NAMES = {0: "SOL", 1: "UTI", 2: "SBU", 3: "SUB"}
CHARGE_NAMES = {2: "SNU", 3: "OSO"}

# =============================================================================
# AUTO MODE PROFILES
# =============================================================================

MODES_FILE = os.environ.get(
    "SOLAR_MODES_FILE",
    os.path.join(os.path.dirname(DB_PATH), "auto_modes.json"),
)

MODES_DEFAULTS = {
    "active_mode": "balanced",
    "modes": {
        "balanced":    {"low": 82, "charge": 85, "high": 92},
        "max_solar":   {"low": 30, "charge": 35, "high": 92},
        "pure_backup": {"low": 95, "charge": 99, "high": 100},
        "custom":      {"low": 50, "charge": 60, "high": 80},
    },
}


def load_auto_modes():
    """Load auto mode profiles from JSON file, falling back to defaults."""
    try:
        with open(MODES_FILE, "r") as f:
            data = json.load(f)
        # Ensure all required modes exist
        for name, defaults in MODES_DEFAULTS["modes"].items():
            if name not in data.get("modes", {}):
                data.setdefault("modes", {})[name] = dict(defaults)
        if "active_mode" not in data:
            data["active_mode"] = MODES_DEFAULTS["active_mode"]
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        data = json.loads(json.dumps(MODES_DEFAULTS))  # deep copy
        save_auto_modes(data)
        return data


def save_auto_modes(data):
    """Save auto mode profiles to JSON file atomically."""
    tmp = MODES_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, MODES_FILE)
