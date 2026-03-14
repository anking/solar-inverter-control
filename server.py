#!/usr/bin/env python3
"""
Solar Inverter Controller & Web Dashboard
------------------------------------------
Runs the inverter control loop and serves a real-time web dashboard.
Uses FastAPI with WebSocket for live updates and SQLite for history.

Usage:
    uvicorn server:app --host 0.0.0.0 --port 8080
    or: python server.py
"""

import asyncio
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request

import urllib.request
import urllib.error

from config import (
    SERIAL_PORT, BAUD_RATE, SLAVE_ADDRESS,
    POLL_INTERVAL,
    WEB_HOST, WEB_PORT, LOG_FILE,
    MODE_UTI, MODE_SBU, MODE_SUB, MODE_SOL,
    CHARGE_SNU, CHARGE_OSO,
    MODE_NAMES, CHARGE_NAMES,
    MODES_DEFAULTS, load_auto_modes, save_auto_modes,
    WEATHER_REFRESH_INTERVAL,
    load_weather_location, save_weather_location,
    SMART_STORM_WMO_CODES, SMART_BAD_WMO_CODES,
    SMART_CLOUD_HEAVY_PCT, SMART_PRECIP_PROB_HIGH,
    SMART_WIND_HIGH_MPH, SMART_WIND_MODERATE_MPH,
    SMART_SOC_DRAIN_OK, SMART_COOLDOWN_MINUTES, SMART_EVAL_INTERVAL,
    NWS_ALERTS_REFRESH,
)
from inverter import InverterController
from database import (
    init_db, log_reading, update_daily_stats,
    get_history, get_daily_stats, cleanup_old_readings,
)

# =============================================================================
# GLOBALS
# =============================================================================

controller: InverterController = None
latest_status: dict = {}
ws_clients: set = set()
log_clients: set = set()
manual_mode: bool = False
control_task: asyncio.Task = None
log_event_loop: asyncio.AbstractEventLoop = None
auto_modes: dict = {}
active_auto_mode: str = "balanced"
weather_location: dict = {}
weather_cache: dict = {}
weather_last_fetch: float = 0
weather_task: asyncio.Task = None

# NWS alerts state
nws_alerts: list = []
nws_alerts_last_fetch: float = 0

# Smart mode state
smart_enabled: bool = False
smart_effective_profile: str = "balanced"
smart_reason: str = ""
smart_last_switch: float = 0
smart_task: asyncio.Task = None


# =============================================================================
# LOGGING — with WebSocket broadcast to debug panel
# =============================================================================

class WebSocketLogHandler(logging.Handler):
    """Logging handler that pushes log lines to connected debug WS clients."""

    def __init__(self):
        super().__init__()
        from collections import deque
        self.buffer = deque(maxlen=500)  # keep last 500 lines for new connections

    def emit(self, record):
        msg = self.format(record)
        self.buffer.append(msg)
        # Schedule async broadcast if we have clients and an event loop
        if log_clients and log_event_loop and log_event_loop.is_running():
            log_event_loop.call_soon_threadsafe(
                asyncio.ensure_future,
                _broadcast_log(msg)
            )


async def _broadcast_log(msg):
    """Send a log line to all debug panel WS clients."""
    global log_clients
    if not log_clients:
        return
    disconnected = set()
    for ws in log_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            disconnected.add(ws)
    log_clients -= disconnected


ws_log_handler = WebSocketLogHandler()


def setup_logging():
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    logger.addHandler(console)

    ws_log_handler.setLevel(logging.DEBUG)
    ws_log_handler.setFormatter(fmt)
    logger.addHandler(ws_log_handler)

    try:
        from logging.handlers import RotatingFileHandler
        file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=3)
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    except (PermissionError, FileNotFoundError):
        logging.warning(f"Cannot write to {LOG_FILE}, logging to console only")


# =============================================================================
# CONTROL LOOP (runs as asyncio background task)
# =============================================================================

async def control_loop():
    """Main control loop — reads inverter, logs data, controls modes, pushes to WS."""
    global latest_status, controller

    mode_cfg = auto_modes["modes"][active_auto_mode]
    logging.info("=" * 60)
    logging.info("Solar Controller + Dashboard Starting")
    logging.info(f"  Auto mode:      {active_auto_mode}{' (SMART)' if smart_enabled else ''}")
    logging.info(f"  Low threshold:  {mode_cfg['low']}% (grid charge → SUB/SNU)")
    logging.info(f"  Charge target:  {mode_cfg['charge']}% (stop grid charge → UTI/OSO)")
    logging.info(f"  High threshold: {mode_cfg['high']}% (use battery → SBU/OSO)")
    logging.info(f"  Poll interval:  {POLL_INTERVAL}s")
    logging.info("=" * 60)

    consecutive_errors = 0
    max_errors = 5
    last_cleanup = datetime.now()
    startup = True

    while True:
        try:
            # Read full status (blocking I/O in thread to not block async loop)
            status = await asyncio.to_thread(controller.read_full_status)

            if status is None:
                consecutive_errors += 1
                logging.warning(f"Failed to read status ({consecutive_errors}/{max_errors})")
                if consecutive_errors >= max_errors:
                    logging.error("Too many errors, reconnecting...")
                    await asyncio.to_thread(controller.disconnect)
                    await asyncio.sleep(5)
                    connected = await asyncio.to_thread(controller.connect)
                    if not connected:
                        await asyncio.sleep(30)
                        continue
                    consecutive_errors = 0
                await asyncio.sleep(POLL_INTERVAL)
                continue

            consecutive_errors = 0
            status["manual_mode"] = manual_mode
            status["timestamp"] = datetime.now().isoformat()
            status["active_auto_mode"] = active_auto_mode
            status["auto_modes"] = auto_modes["modes"]
            status["smart_enabled"] = smart_enabled
            status["smart_effective"] = smart_effective_profile
            status["smart_reason"] = smart_reason
            status["nws_alerts"] = nws_alerts
            latest_status = status

            # Log to database
            try:
                await asyncio.to_thread(log_reading, status)
                await asyncio.to_thread(update_daily_stats, status)
            except Exception as e:
                logging.error(f"DB error: {e}")

            # Periodic cleanup (once per day)
            if (datetime.now() - last_cleanup).total_seconds() > 86400:
                await asyncio.to_thread(cleanup_old_readings, 90)
                last_cleanup = datetime.now()

            soc = status["soc"]
            grid_present = status["grid_present"]
            current_mode = status["output_mode"]
            controller.current_mode = current_mode

            timestamp = datetime.now().strftime("%H:%M:%S")
            logging.info(
                f"[{timestamp}] SOC={soc}% | "
                f"Grid={'ON' if grid_present else 'OFF'} ({status['grid_voltage']:.1f}V) | "
                f"Mode={status['output_mode_name']} | "
                f"Charge={status['charge_mode_name']} | "
                f"PV={status['pv_total_power']}W | "
                f"Load={status['load_power']}W"
                f"{' [MANUAL]' if manual_mode else ''}"
            )

            # --- CONTROL LOGIC (skip if manual override) ---
            if not manual_mode:
                # Read thresholds dynamically from active auto mode
                mcfg = auto_modes["modes"][active_auto_mode]
                low_t = mcfg["low"]
                chg_t = mcfg["charge"]
                hi_t = mcfg["high"]

                if startup:
                    if soc >= hi_t:
                        await asyncio.to_thread(controller.set_output_mode, MODE_SBU)
                        await asyncio.sleep(0.5)
                        await asyncio.to_thread(controller.set_charge_mode, CHARGE_OSO)
                    elif soc <= chg_t:
                        logging.info(f"Startup: SOC {soc}% <= {chg_t}% → SUB/SNU (grid charging)")
                        await asyncio.to_thread(controller.set_output_mode, MODE_SUB)
                        await asyncio.sleep(0.5)
                        await asyncio.to_thread(controller.set_charge_mode, CHARGE_SNU)
                    else:
                        await asyncio.to_thread(controller.set_output_mode, MODE_UTI)
                        await asyncio.sleep(0.5)
                        await asyncio.to_thread(controller.set_charge_mode, CHARGE_OSO)
                    startup = False

                # Grid down — battery backup
                if not grid_present:
                    if not controller.grid_was_down:
                        logging.warning("GRID DOWN — SBU for battery backup")
                        controller.grid_was_down = True
                    if current_mode != MODE_SBU:
                        await asyncio.to_thread(controller.set_output_mode, MODE_SBU)

                elif controller.grid_was_down and grid_present:
                    logging.info("GRID RESTORED — resuming SOC control")
                    controller.grid_was_down = False

                # SOC low — actively charge from grid
                if grid_present and soc <= low_t:
                    if current_mode != MODE_SUB:
                        logging.info(f"SOC {soc}% <= {low_t}% → SUB (grid charging)")
                        await asyncio.to_thread(controller.set_output_mode, MODE_SUB)
                        await asyncio.sleep(0.5)
                    if status["charge_mode"] != CHARGE_SNU:
                        logging.info(f"SOC {soc}% <= {low_t}% → SNU")
                        await asyncio.to_thread(controller.set_charge_mode, CHARGE_SNU)

                # SOC charging — keep SUB/SNU until topped off
                elif grid_present and low_t < soc < chg_t:
                    if current_mode == MODE_SUB:
                        pass  # keep charging until charge threshold
                    elif current_mode != MODE_UTI:
                        await asyncio.to_thread(controller.set_output_mode, MODE_UTI)
                        await asyncio.sleep(0.5)
                        if status["charge_mode"] != CHARGE_OSO:
                            await asyncio.to_thread(controller.set_charge_mode, CHARGE_OSO)

                # SOC in middle zone — hysteresis: keep current mode
                # If already SBU (draining from high), stay SBU until charge threshold
                # If already UTI (charged from low), stay UTI until high threshold
                elif grid_present and chg_t <= soc < hi_t:
                    if current_mode == MODE_SBU:
                        pass  # keep draining until charge threshold
                    elif current_mode != MODE_UTI:
                        logging.info(f"SOC {soc}% >= {chg_t}% → UTI (charged)")
                        await asyncio.to_thread(controller.set_output_mode, MODE_UTI)
                        await asyncio.sleep(0.5)
                    if status["charge_mode"] != CHARGE_OSO:
                        logging.info(f"SOC {soc}% in middle zone → OSO")
                        await asyncio.to_thread(controller.set_charge_mode, CHARGE_OSO)

                # SOC high — use battery
                elif grid_present and soc >= hi_t:
                    if current_mode != MODE_SBU:
                        logging.info(f"SOC {soc}% >= {hi_t}% → SBU")
                        await asyncio.to_thread(controller.set_output_mode, MODE_SBU)
                        await asyncio.sleep(0.5)
                    if status["charge_mode"] != CHARGE_OSO:
                        logging.info(f"SOC {soc}% >= {hi_t}% → OSO")
                        await asyncio.to_thread(controller.set_charge_mode, CHARGE_OSO)

            # Broadcast to WebSocket clients
            await broadcast_status(status)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.error(f"Control loop error: {e}")
            consecutive_errors += 1

        await asyncio.sleep(POLL_INTERVAL)


async def broadcast_status(status):
    """Send status to all connected WebSocket clients."""
    global ws_clients
    if not ws_clients:
        return
    data = json.dumps(status)
    disconnected = set()
    for ws in ws_clients:
        try:
            await ws.send_text(data)
        except Exception:
            disconnected.add(ws)
    ws_clients -= disconnected


# =============================================================================
# WEATHER — Open-Meteo (free, no key) + Zippopotam.us for geocoding
# =============================================================================

async def _http_get_json(url: str):
    """Non-blocking HTTP GET returning parsed JSON, or None on error."""
    try:
        def _fetch():
            req = urllib.request.Request(url, headers={"User-Agent": "SolarDashboard/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        return await asyncio.to_thread(_fetch)
    except Exception as e:
        logging.warning(f"HTTP fetch failed ({url[:80]}): {e}")
        return None


async def _fetch_weather():
    """Fetch hourly forecast from Open-Meteo and cache it."""
    global weather_cache, weather_last_fetch
    global smart_effective_profile, smart_reason, smart_last_switch, active_auto_mode
    lat = weather_location.get("lat")
    lon = weather_location.get("lon")
    if not lat or not lon:
        return
    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}"
        f"&hourly=temperature_2m,relative_humidity_2m,cloud_cover,"
        f"precipitation_probability,precipitation,weather_code,wind_speed_10m"
        f"&temperature_unit=fahrenheit"
        f"&wind_speed_unit=mph"
        f"&precipitation_unit=inch"
        f"&timezone=auto"
        f"&forecast_days=2"
    )
    data = await _http_get_json(url)
    if data and "hourly" in data:
        weather_cache = data
        weather_last_fetch = time.time()
        logging.info(f"Weather updated for {weather_location.get('city', '?')}, {weather_location.get('state', '?')}")
        # Re-evaluate smart mode immediately when new weather arrives
        if smart_enabled:
            current_soc = latest_status.get("soc", 50) if latest_status else 50
            new_profile, reason = evaluate_smart_mode(current_soc)
            if new_profile != smart_effective_profile:
                old = smart_effective_profile
                smart_effective_profile = new_profile
                smart_reason = reason
                smart_last_switch = time.time()
                active_auto_mode = new_profile
                logging.info(f"[SMART] Weather update: {old} → {new_profile}: {reason}")
                await _broadcast_manual_update()
            else:
                smart_reason = reason


async def _fetch_nws_alerts():
    """Fetch active weather alerts from the National Weather Service API."""
    global nws_alerts, nws_alerts_last_fetch
    global smart_effective_profile, smart_reason, smart_last_switch, active_auto_mode
    lat = weather_location.get("lat")
    lon = weather_location.get("lon")
    if not lat or not lon:
        return
    url = f"https://api.weather.gov/alerts/active?point={lat},{lon}&status=actual"
    data = await _http_get_json(url)
    if data and "features" in data:
        alerts = []
        for f in data["features"]:
            p = f.get("properties", {})
            alerts.append({
                "event": p.get("event", "Unknown"),
                "severity": p.get("severity", "Unknown"),
                "urgency": p.get("urgency", "Unknown"),
                "headline": p.get("headline", ""),
                "description": p.get("description", ""),
                "instruction": p.get("instruction", ""),
                "expires": p.get("expires", ""),
            })
        nws_alerts = alerts
        nws_alerts_last_fetch = time.time()
        if alerts:
            logging.info(f"NWS alerts active: {', '.join(a['event'] for a in alerts)}")
            # Re-evaluate smart mode when alerts arrive
            if smart_enabled:
                current_soc = latest_status.get("soc", 50) if latest_status else 50
                new_profile, reason = evaluate_smart_mode(current_soc)
                if new_profile != smart_effective_profile:
                    old = smart_effective_profile
                    smart_effective_profile = new_profile
                    smart_reason = reason
                    smart_last_switch = time.time()
                    active_auto_mode = new_profile
                    logging.info(f"[SMART] NWS alert triggered: {old} → {new_profile}: {reason}")
                    await _broadcast_manual_update()
    else:
        nws_alerts = []
        nws_alerts_last_fetch = time.time()


async def weather_loop():
    """Periodically refresh weather data and NWS alerts."""
    while True:
        if weather_location.get("lat"):
            now = time.time()
            if now - weather_last_fetch >= WEATHER_REFRESH_INTERVAL:
                await _fetch_weather()
            if now - nws_alerts_last_fetch >= NWS_ALERTS_REFRESH:
                await _fetch_nws_alerts()
        await asyncio.sleep(60)


# =============================================================================
# SMART MODE — weather-based automatic profile switching
# =============================================================================

def _get_sunrise_sunset_hours():
    """Estimate daylight hours from weather data timezone, fallback to 6-20."""
    # Simple approximation: usable solar is roughly 7am-6pm
    return 7, 18


def _score_weather_window(hours_ahead=6):
    """
    Analyze the next N hours of weather forecast.
    Returns a dict with scoring info for smart mode decisions.
    """
    if not weather_cache or "hourly" not in weather_cache:
        return None

    h = weather_cache["hourly"]
    times = h.get("time", [])
    codes_all = h.get("weather_code", [])
    clouds_all = h.get("cloud_cover", [])
    precip_all = h.get("precipitation_probability", [])
    if not times:
        return None

    wind_all = h.get("wind_speed_10m", [])

    # Use the minimum length across all arrays to avoid index errors
    max_len = min(len(times), len(codes_all), len(clouds_all), len(precip_all), len(wind_all)) if wind_all else min(len(times), len(codes_all), len(clouds_all), len(precip_all))
    if max_len == 0:
        return None

    now = datetime.now()

    # Find current hour index
    cur_idx = 0
    for i in range(max_len):
        try:
            if datetime.fromisoformat(times[i]) <= now:
                cur_idx = i
            else:
                break
        except (ValueError, TypeError):
            break

    end_idx = min(cur_idx + hours_ahead, max_len)
    if end_idx <= cur_idx:
        return None

    window = range(cur_idx, end_idx)

    codes = [codes_all[i] for i in window]
    clouds = [clouds_all[i] for i in window]
    precip_probs = [precip_all[i] for i in window]
    winds = [wind_all[i] for i in window] if wind_all else []

    storm_hours = sum(1 for c in codes if c in SMART_STORM_WMO_CODES)
    bad_hours = sum(1 for c in codes if c in SMART_BAD_WMO_CODES)
    avg_cloud = sum(clouds) / len(clouds) if clouds else 50
    max_precip_prob = max(precip_probs) if precip_probs else 0
    avg_precip_prob = sum(precip_probs) / len(precip_probs) if precip_probs else 0
    max_wind = max(winds) if winds else 0
    high_wind_hours = sum(1 for w in winds if w >= SMART_WIND_HIGH_MPH) if winds else 0

    # Count upcoming sunny hours (low cloud, during daylight)
    sun_rise, sun_set = _get_sunrise_sunset_hours()
    sunny_hours = 0
    for j, i in enumerate(window):
        hr = datetime.fromisoformat(times[i]).hour
        if sun_rise <= hr < sun_set and clouds[j] < 40:
            sunny_hours += 1

    # NWS severe alerts
    severe_alerts = [a for a in nws_alerts if a["severity"] in ("Extreme", "Severe")]

    return {
        "storm_hours": storm_hours,
        "bad_hours": bad_hours,
        "avg_cloud": avg_cloud,
        "max_precip_prob": max_precip_prob,
        "avg_precip_prob": avg_precip_prob,
        "max_wind": max_wind,
        "high_wind_hours": high_wind_hours,
        "sunny_hours": sunny_hours,
        "window_hours": len(list(window)),
        "severe_alerts": len(severe_alerts),
    }


def evaluate_smart_mode(current_soc: int) -> tuple:
    """
    Evaluate weather conditions and return (profile_name, reason).
    Returns the best auto mode profile based on upcoming weather.
    """
    score = _score_weather_window(6)
    if score is None:
        return "balanced", "No weather data available"

    now = datetime.now()
    sun_rise, sun_set = _get_sunrise_sunset_hours()
    is_daytime = sun_rise <= now.hour < sun_set
    hours_of_sun_left = max(0, sun_set - now.hour) if is_daytime else 0

    # === PRIORITY 0: Active NWS severe/extreme alert → pure_backup ===
    if score["severe_alerts"] > 0:
        alert_names = ", ".join(a["event"] for a in nws_alerts if a["severity"] in ("Extreme", "Severe"))
        return "pure_backup", f"NWS ALERT: {alert_names}"

    # === PRIORITY 1: Severe storm incoming → pure_backup ===
    if score["storm_hours"] >= 1:
        return "pure_backup", f"Storm warning: {score['storm_hours']}h of thunderstorms in next 6h"

    # === PRIORITY 2: High winds → pure_backup (outage risk) ===
    if score["high_wind_hours"] >= 1:
        return "pure_backup", f"High wind warning: {score['max_wind']:.0f} mph gusts, {score['high_wind_hours']}h of dangerous winds"

    # === PRIORITY 3: Bad weather + high precip → pure_backup ===
    if score["bad_hours"] >= 2 and score["max_precip_prob"] >= SMART_PRECIP_PROB_HIGH:
        return "pure_backup", f"Severe weather: {score['bad_hours']}h of heavy precip, {score['max_precip_prob']}% probability"

    # === PRIORITY 4: Moderate winds + precipitation → balanced (conservative) ===
    if score["max_wind"] >= SMART_WIND_MODERATE_MPH and score["avg_precip_prob"] >= 40:
        return "balanced", f"Windy with rain likely ({score['max_wind']:.0f} mph, precip {score['avg_precip_prob']:.0f}%)"

    # === PRIORITY 5: Sustained heavy cloud + precip likely → balanced (conservative) ===
    if score["avg_cloud"] >= SMART_CLOUD_HEAVY_PCT and score["avg_precip_prob"] >= 50:
        return "balanced", f"Overcast with rain likely (cloud {score['avg_cloud']:.0f}%, precip {score['avg_precip_prob']:.0f}%)"

    # === PRIORITY 6: Good solar conditions → max_solar (drain battery to harvest) ===
    if (is_daytime
            and score["sunny_hours"] >= 3
            and hours_of_sun_left >= 4
            and current_soc >= SMART_SOC_DRAIN_OK
            and score["avg_cloud"] < 40):
        return "max_solar", f"{score['sunny_hours']}h of sun ahead, SOC {current_soc}% — drain to harvest solar"

    # === DEFAULT: balanced ===
    return "balanced", f"Normal conditions (cloud {score['avg_cloud']:.0f}%, {score['sunny_hours']}h sun)"


async def smart_mode_loop():
    """Periodically evaluate weather and switch effective profile."""
    global smart_effective_profile, smart_reason, smart_last_switch, active_auto_mode

    # Short initial delay to let weather fetch complete first
    await asyncio.sleep(30)

    while True:
        if not smart_enabled:
            await asyncio.sleep(60)
            continue

        if not weather_cache:
            await asyncio.sleep(30)
            continue

        current_soc = latest_status.get("soc", 50) if latest_status else 50
        new_profile, reason = evaluate_smart_mode(current_soc)

        # Cooldown: don't switch too frequently (unless upgrading to pure_backup)
        elapsed = time.time() - smart_last_switch
        if (new_profile != smart_effective_profile
                and elapsed < SMART_COOLDOWN_MINUTES * 60
                and new_profile != "pure_backup"):
            await asyncio.sleep(SMART_EVAL_INTERVAL)
            continue

        if new_profile != smart_effective_profile:
            old = smart_effective_profile
            smart_effective_profile = new_profile
            smart_reason = reason
            smart_last_switch = time.time()
            active_auto_mode = new_profile
            auto_modes["active_mode"] = new_profile
            await asyncio.to_thread(save_auto_modes, auto_modes)
            logging.info(f"[SMART] {old} → {new_profile}: {reason}")
            await _broadcast_manual_update()
        else:
            smart_reason = reason

        await asyncio.sleep(SMART_EVAL_INTERVAL)


# =============================================================================
# FASTAPI APP
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    global controller, control_task, log_event_loop, auto_modes, active_auto_mode
    global weather_location, weather_task, smart_task, smart_enabled

    setup_logging()
    log_event_loop = asyncio.get_running_loop()
    init_db()

    auto_modes = load_auto_modes()
    saved_mode = auto_modes.get("active_mode", "balanced")
    if saved_mode == "smart":
        smart_enabled = True
        active_auto_mode = smart_effective_profile  # will be re-evaluated once weather loads
    else:
        active_auto_mode = saved_mode

    weather_location = load_weather_location()
    weather_task = asyncio.create_task(weather_loop())
    smart_task = asyncio.create_task(smart_mode_loop())

    controller = InverterController(SERIAL_PORT, BAUD_RATE, SLAVE_ADDRESS)
    connected = controller.connect()

    if connected:
        control_task = asyncio.create_task(control_loop())
    else:
        logging.error("Could not connect to inverter — dashboard will run without live data")

    yield

    # Shutdown
    for task in [control_task, weather_task, smart_task]:
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    if controller:
        controller.disconnect()


app = FastAPI(title="Solar Inverter Dashboard", lifespan=lifespan)

static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# --- Pages ---

@app.get("/")
async def dashboard():
    return FileResponse(str(static_dir / "index.html"))


# --- API ---

@app.get("/api/status")
async def api_status():
    status = dict(latest_status) if latest_status else {}
    status["manual_mode"] = manual_mode
    return JSONResponse(status if status else {"error": "no data yet", "manual_mode": manual_mode})


@app.get("/api/history")
async def api_history(hours: int = 24):
    hours = min(hours, 168)  # cap at 7 days
    data = await asyncio.to_thread(get_history, hours)
    return JSONResponse(data)


@app.get("/api/daily")
async def api_daily(days: int = 30):
    days = min(days, 365)
    data = await asyncio.to_thread(get_daily_stats, days)
    return JSONResponse(data)


@app.get("/api/config")
async def api_config():
    mcfg = auto_modes["modes"][active_auto_mode]
    return JSONResponse({
        "low_threshold": mcfg["low"],
        "charge_threshold": mcfg["charge"],
        "high_threshold": mcfg["high"],
        "poll_interval": POLL_INTERVAL,
        "manual_mode": manual_mode,
        "active_auto_mode": active_auto_mode,
        "auto_modes": auto_modes["modes"],
        "smart_enabled": smart_enabled,
        "smart_effective": smart_effective_profile,
        "smart_reason": smart_reason,
    })


# --- Manual Control ---

async def _broadcast_manual_update():
    """Push updated mode state to all WS clients immediately."""
    if latest_status:
        latest_status["manual_mode"] = manual_mode
        latest_status["active_auto_mode"] = active_auto_mode
        latest_status["auto_modes"] = auto_modes["modes"]
        latest_status["smart_enabled"] = smart_enabled
        latest_status["smart_effective"] = smart_effective_profile
        latest_status["smart_reason"] = smart_reason
        latest_status["nws_alerts"] = nws_alerts
        await broadcast_status(latest_status)


@app.post("/api/mode/auto")
async def set_auto_mode():
    global manual_mode
    manual_mode = False
    logging.info("Switched to AUTO mode")
    await _broadcast_manual_update()
    return JSONResponse({"manual_mode": False})


@app.post("/api/mode/manual")
async def set_manual_mode():
    global manual_mode
    manual_mode = True
    logging.info("Switched to MANUAL mode")
    await _broadcast_manual_update()
    return JSONResponse({"manual_mode": True})


@app.post("/api/mode/output/{mode}")
async def set_output_mode(mode: str):
    mode_map = {"sol": MODE_SOL, "uti": MODE_UTI, "sbu": MODE_SBU, "sub": MODE_SUB}
    mode_val = mode_map.get(mode.lower())
    if mode_val is None:
        return JSONResponse({"error": f"Invalid mode: {mode}"}, status_code=400)
    if not manual_mode:
        return JSONResponse({"error": "Switch to manual mode first"}, status_code=400)

    success = await asyncio.to_thread(controller.set_output_mode, mode_val)
    if success and latest_status:
        latest_status["output_mode"] = mode_val
        latest_status["output_mode_name"] = MODE_NAMES.get(mode_val, str(mode_val))
        await broadcast_status(latest_status)
    return JSONResponse({"success": success, "mode": mode.upper()})


@app.post("/api/mode/charge/{mode}")
async def set_charge_mode(mode: str):
    mode_map = {"snu": CHARGE_SNU, "oso": CHARGE_OSO}
    mode_val = mode_map.get(mode.lower())
    if mode_val is None:
        return JSONResponse({"error": f"Invalid charge mode: {mode}"}, status_code=400)
    if not manual_mode:
        return JSONResponse({"error": "Switch to manual mode first"}, status_code=400)

    success = await asyncio.to_thread(controller.set_charge_mode, mode_val)
    if success and latest_status:
        latest_status["charge_mode"] = mode_val
        latest_status["charge_mode_name"] = CHARGE_NAMES.get(mode_val, str(mode_val))
        await broadcast_status(latest_status)
    return JSONResponse({"success": success, "mode": mode.upper()})


# --- Auto Mode Profiles ---

VALID_AUTO_MODES = {"balanced", "max_solar", "pure_backup", "custom", "smart"}


@app.get("/api/auto-modes")
async def get_auto_modes():
    return JSONResponse({
        "active_mode": "smart" if smart_enabled else active_auto_mode,
        "modes": auto_modes["modes"],
        "smart_enabled": smart_enabled,
        "smart_effective": smart_effective_profile,
        "smart_reason": smart_reason,
    })


@app.post("/api/auto-modes/active")
async def set_active_auto_mode(request: Request):
    global active_auto_mode, smart_enabled, smart_effective_profile, smart_reason
    body = await request.json()
    mode = body.get("mode", "")
    if mode not in VALID_AUTO_MODES:
        return JSONResponse({"error": f"Invalid mode: {mode}"}, status_code=400)

    if mode == "smart":
        try:
            # Enable smart mode — it picks the effective profile
            smart_enabled = True
            # Run an immediate evaluation
            current_soc = latest_status.get("soc", 50) if latest_status else 50
            profile, reason = evaluate_smart_mode(current_soc)
            smart_effective_profile = profile
            smart_reason = reason
            active_auto_mode = profile
            auto_modes["active_mode"] = "smart"
            await asyncio.to_thread(save_auto_modes, auto_modes)
            logging.info(f"Smart mode enabled → {profile}: {reason}")
            await _broadcast_manual_update()
            return JSONResponse({
                "active_mode": "smart",
                "smart_effective": profile,
                "smart_reason": reason,
                "thresholds": auto_modes["modes"][profile],
            })
        except Exception as e:
            logging.error(f"Smart mode activation failed: {e}", exc_info=True)
            smart_enabled = False
            return JSONResponse({"error": str(e)}, status_code=500)
    else:
        # Disable smart mode, switch to explicit profile
        smart_enabled = False
        active_auto_mode = mode
        auto_modes["active_mode"] = mode
        await asyncio.to_thread(save_auto_modes, auto_modes)
        logging.info(f"Auto mode switched to: {mode} (low={auto_modes['modes'][mode]['low']}%, charge={auto_modes['modes'][mode]['charge']}%, high={auto_modes['modes'][mode]['high']}%)")
        await _broadcast_manual_update()
        return JSONResponse({"active_mode": mode, "thresholds": auto_modes["modes"][mode]})


@app.post("/api/auto-modes/update")
async def update_auto_mode(request: Request):
    body = await request.json()
    mode = body.get("mode", "")
    if mode not in VALID_AUTO_MODES:
        return JSONResponse({"error": f"Invalid mode: {mode}"}, status_code=400)
    low = int(body.get("low", 0))
    charge = int(body.get("charge", 0))
    high = int(body.get("high", 0))
    if not (10 <= low < charge < high <= 100):
        return JSONResponse({"error": "Must satisfy: 10 <= low < charge < high <= 100"}, status_code=400)
    auto_modes["modes"][mode] = {"low": low, "charge": charge, "high": high}
    await asyncio.to_thread(save_auto_modes, auto_modes)
    logging.info(f"Auto mode '{mode}' updated: low={low}%, charge={charge}%, high={high}%")
    await _broadcast_manual_update()
    return JSONResponse({"mode": mode, "thresholds": auto_modes["modes"][mode]})


@app.post("/api/auto-modes/reset")
async def reset_auto_mode(request: Request):
    body = await request.json()
    mode = body.get("mode", "")
    if mode not in VALID_AUTO_MODES:
        return JSONResponse({"error": f"Invalid mode: {mode}"}, status_code=400)
    auto_modes["modes"][mode] = dict(MODES_DEFAULTS["modes"][mode])
    await asyncio.to_thread(save_auto_modes, auto_modes)
    logging.info(f"Auto mode '{mode}' reset to defaults")
    await _broadcast_manual_update()
    return JSONResponse({"mode": mode, "thresholds": auto_modes["modes"][mode]})


# --- Weather ---

@app.get("/api/smart")
async def api_smart():
    current_soc = latest_status.get("soc", 50) if latest_status else 50
    score = _score_weather_window(6)

    # Build decision trace showing how each rule evaluated
    trace = []
    if score:
        severe = [a for a in nws_alerts if a["severity"] in ("Extreme", "Severe")]
        trace.append({
            "rule": "P0: NWS Severe/Extreme Alert",
            "check": f"{len(severe)} active severe alerts",
            "triggered": len(severe) > 0,
            "result": "pure_backup" if len(severe) > 0 else "skip",
        })
        trace.append({
            "rule": "P1: Thunderstorms (WMO 95/96/99)",
            "check": f"{score['storm_hours']}h of storms in next 6h (need >= 1)",
            "triggered": score["storm_hours"] >= 1,
            "result": "pure_backup" if score["storm_hours"] >= 1 else "skip",
        })
        trace.append({
            "rule": f"P2: High Wind >= {SMART_WIND_HIGH_MPH} mph",
            "check": f"max {score['max_wind']:.0f} mph, {score['high_wind_hours']}h dangerous (need >= 1)",
            "triggered": score["high_wind_hours"] >= 1,
            "result": "pure_backup" if score["high_wind_hours"] >= 1 else "skip",
        })
        trace.append({
            "rule": f"P3: Bad Weather + Precip >= {SMART_PRECIP_PROB_HIGH}%",
            "check": f"{score['bad_hours']}h bad WMO (need >= 2), max precip {score['max_precip_prob']}% (need >= {SMART_PRECIP_PROB_HIGH})",
            "triggered": score["bad_hours"] >= 2 and score["max_precip_prob"] >= SMART_PRECIP_PROB_HIGH,
            "result": "pure_backup" if (score["bad_hours"] >= 2 and score["max_precip_prob"] >= SMART_PRECIP_PROB_HIGH) else "skip",
        })
        trace.append({
            "rule": f"P4: Moderate Wind >= {SMART_WIND_MODERATE_MPH} mph + Rain",
            "check": f"max wind {score['max_wind']:.0f} mph, avg precip {score['avg_precip_prob']:.0f}% (need >= 40)",
            "triggered": score["max_wind"] >= SMART_WIND_MODERATE_MPH and score["avg_precip_prob"] >= 40,
            "result": "balanced" if (score["max_wind"] >= SMART_WIND_MODERATE_MPH and score["avg_precip_prob"] >= 40) else "skip",
        })
        trace.append({
            "rule": f"P5: Overcast (cloud >= {SMART_CLOUD_HEAVY_PCT}%) + Rain (>= 50%)",
            "check": f"avg cloud {score['avg_cloud']:.0f}%, avg precip {score['avg_precip_prob']:.0f}%",
            "triggered": score["avg_cloud"] >= SMART_CLOUD_HEAVY_PCT and score["avg_precip_prob"] >= 50,
            "result": "balanced" if (score["avg_cloud"] >= SMART_CLOUD_HEAVY_PCT and score["avg_precip_prob"] >= 50) else "skip",
        })
        now = datetime.now()
        sun_rise, sun_set = _get_sunrise_sunset_hours()
        is_daytime = sun_rise <= now.hour < sun_set
        hours_left = max(0, sun_set - now.hour) if is_daytime else 0
        solar_ok = (is_daytime and score["sunny_hours"] >= 3 and hours_left >= 4
                     and current_soc >= SMART_SOC_DRAIN_OK and score["avg_cloud"] < 40)
        trace.append({
            "rule": "P6: Good Solar Conditions",
            "check": f"daytime={is_daytime}, {score['sunny_hours']}h sun (need >= 3), {hours_left}h left (need >= 4), SOC {current_soc}% (need >= {SMART_SOC_DRAIN_OK}), cloud {score['avg_cloud']:.0f}% (need < 40)",
            "triggered": solar_ok,
            "result": "max_solar" if solar_ok else "skip",
        })
        trace.append({
            "rule": "Default",
            "check": "No higher priority rule triggered",
            "triggered": not any(t["triggered"] for t in trace),
            "result": "balanced",
        })

    return JSONResponse({
        "enabled": smart_enabled,
        "effective_profile": smart_effective_profile,
        "reason": smart_reason,
        "last_switch": smart_last_switch,
        "weather_score": score,
        "current_soc": current_soc,
        "nws_alerts": nws_alerts,
        "decision_trace": trace,
    })


@app.get("/api/weather")
async def api_weather():
    if not weather_location.get("lat"):
        return JSONResponse({"configured": False})
    return JSONResponse({
        "configured": True,
        "location": weather_location,
        "weather": weather_cache,
        "last_fetch": weather_last_fetch,
        "alerts": nws_alerts,
    })


@app.get("/api/alerts")
async def api_alerts():
    return JSONResponse({"alerts": nws_alerts, "last_fetch": nws_alerts_last_fetch})


@app.post("/api/weather/location")
async def set_weather_location_endpoint(request: Request):
    global weather_location, weather_last_fetch
    body = await request.json()
    zip_code = body.get("zip_code", "").strip()
    if not (zip_code.isdigit() and len(zip_code) == 5):
        return JSONResponse({"error": "Enter a valid 5-digit US zip code"}, status_code=400)

    geo = await _http_get_json(f"https://api.zippopotam.us/us/{zip_code}")
    if not geo or "places" not in geo or not geo["places"]:
        return JSONResponse({"error": "Zip code not found"}, status_code=404)

    place = geo["places"][0]
    weather_location = {
        "zip_code": zip_code,
        "lat": float(place["latitude"]),
        "lon": float(place["longitude"]),
        "city": place["place name"],
        "state": place["state abbreviation"],
    }
    await asyncio.to_thread(save_weather_location, weather_location)
    weather_last_fetch = 0
    await _fetch_weather()
    logging.info(f"Weather location set: {weather_location['city']}, {weather_location['state']} ({zip_code})")
    return JSONResponse(weather_location)


# --- WebSocket ---

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    ws_clients.add(ws)
    try:
        # Send current status immediately
        if latest_status:
            await ws.send_text(json.dumps(latest_status))
        # Keep alive — wait for client disconnect
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        ws_clients.discard(ws)


@app.websocket("/ws/logs")
async def websocket_logs(ws: WebSocket):
    """Debug log stream — only active while panel is open."""
    await ws.accept()
    log_clients.add(ws)
    try:
        # Send buffered recent logs so the panel has context
        for line in ws_log_handler.buffer:
            await ws.send_text(line)
        # Keep alive
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        log_clients.discard(ws)


# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host=WEB_HOST, port=WEB_PORT, reload=False)
