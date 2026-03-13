"""
SQLite database for storing inverter readings and daily statistics.
"""

import sqlite3
import time
from datetime import datetime, timedelta
from config import DB_PATH


def get_connection(db_path=None):
    """Get a SQLite connection with WAL mode for concurrent reads."""
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(db_path=None):
    """Create tables if they don't exist."""
    conn = get_connection(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            soc INTEGER,
            battery_voltage REAL,
            battery_current REAL,
            pv1_power INTEGER,
            pv2_power INTEGER,
            pv_total_power INTEGER,
            load_power INTEGER,
            grid_voltage REAL,
            grid_present INTEGER,
            output_mode INTEGER,
            output_mode_name TEXT,
            charge_mode INTEGER,
            charge_mode_name TEXT,
            total_charge_power INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_readings_timestamp
            ON readings(timestamp);

        CREATE TABLE IF NOT EXISTS daily_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL UNIQUE,
            pv_generation_kwh REAL,
            load_consumption_kwh REAL,
            grid_consumption_kwh REAL
        );

        CREATE INDEX IF NOT EXISTS idx_daily_stats_date
            ON daily_stats(date);
    """)
    conn.commit()
    conn.close()


def log_reading(status, db_path=None):
    """Insert a reading from the inverter status dict."""
    conn = get_connection(db_path)
    conn.execute("""
        INSERT INTO readings (
            timestamp, soc, battery_voltage, battery_current,
            pv1_power, pv2_power, pv_total_power, load_power,
            grid_voltage, grid_present, output_mode, output_mode_name,
            charge_mode, charge_mode_name, total_charge_power
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.now().isoformat(),
        status.get("soc"),
        status.get("battery_voltage"),
        status.get("battery_current"),
        status.get("pv1_power"),
        status.get("pv2_power"),
        status.get("pv_total_power"),
        status.get("load_power"),
        status.get("grid_voltage"),
        1 if status.get("grid_present") else 0,
        status.get("output_mode"),
        status.get("output_mode_name"),
        status.get("charge_mode"),
        status.get("charge_mode_name"),
        status.get("total_charge_power"),
    ))
    conn.commit()
    conn.close()


def update_daily_stats(status, db_path=None):
    """Upsert today's daily stats from inverter registers."""
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_connection(db_path)
    conn.execute("""
        INSERT INTO daily_stats (date, pv_generation_kwh, load_consumption_kwh, grid_consumption_kwh)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            pv_generation_kwh = excluded.pv_generation_kwh,
            load_consumption_kwh = excluded.load_consumption_kwh,
            grid_consumption_kwh = excluded.grid_consumption_kwh
    """, (
        today,
        status.get("pv_generation_today", 0),
        status.get("load_consumption_today", 0),
        status.get("grid_consumption_today", 0),
    ))
    conn.commit()
    conn.close()


def get_history(hours=24, db_path=None):
    """Get readings from the last N hours."""
    since = (datetime.now() - timedelta(hours=hours)).isoformat()
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT * FROM readings WHERE timestamp > ? ORDER BY timestamp",
        (since,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_daily_stats(days=30, db_path=None):
    """Get daily stats for the last N days."""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT * FROM daily_stats WHERE date > ? ORDER BY date",
        (since,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def cleanup_old_readings(days=90, db_path=None):
    """Delete readings older than N days to keep DB size manageable."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    conn = get_connection(db_path)
    conn.execute("DELETE FROM readings WHERE timestamp < ?", (cutoff,))
    conn.commit()
    conn.close()
