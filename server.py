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

from config import (
    SERIAL_PORT, BAUD_RATE, SLAVE_ADDRESS,
    POLL_INTERVAL,
    WEB_HOST, WEB_PORT, LOG_FILE,
    MODE_UTI, MODE_SBU, MODE_SUB, MODE_SOL,
    CHARGE_SNU, CHARGE_OSO,
    MODE_NAMES, CHARGE_NAMES,
    MODES_DEFAULTS, load_auto_modes, save_auto_modes,
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
        file_handler = logging.FileHandler(LOG_FILE)
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
    logging.info(f"  Auto mode:      {active_auto_mode}")
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

                # SOC topped off — grid powers load, solar-only charging
                elif grid_present and chg_t <= soc < hi_t:
                    if current_mode != MODE_UTI:
                        logging.info(f"SOC {soc}% >= {chg_t}% → UTI (charged)")
                        await asyncio.to_thread(controller.set_output_mode, MODE_UTI)
                        await asyncio.sleep(0.5)
                    if status["charge_mode"] != CHARGE_OSO:
                        logging.info(f"SOC {soc}% >= {chg_t}% → OSO")
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
# FASTAPI APP
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    global controller, control_task, log_event_loop, auto_modes, active_auto_mode

    setup_logging()
    log_event_loop = asyncio.get_running_loop()
    init_db()

    auto_modes = load_auto_modes()
    active_auto_mode = auto_modes.get("active_mode", "balanced")

    controller = InverterController(SERIAL_PORT, BAUD_RATE, SLAVE_ADDRESS)
    connected = controller.connect()

    if connected:
        control_task = asyncio.create_task(control_loop())
    else:
        logging.error("Could not connect to inverter — dashboard will run without live data")

    yield

    # Shutdown
    if control_task:
        control_task.cancel()
        try:
            await control_task
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
    })


# --- Manual Control ---

async def _broadcast_manual_update():
    """Push updated mode state to all WS clients immediately."""
    if latest_status:
        latest_status["manual_mode"] = manual_mode
        latest_status["active_auto_mode"] = active_auto_mode
        latest_status["auto_modes"] = auto_modes["modes"]
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

VALID_AUTO_MODES = {"balanced", "max_solar", "pure_backup", "custom"}


@app.get("/api/auto-modes")
async def get_auto_modes():
    return JSONResponse({
        "active_mode": active_auto_mode,
        "modes": auto_modes["modes"],
    })


@app.post("/api/auto-modes/active")
async def set_active_auto_mode(request: Request):
    global active_auto_mode
    body = await request.json()
    mode = body.get("mode", "")
    if mode not in VALID_AUTO_MODES:
        return JSONResponse({"error": f"Invalid mode: {mode}"}, status_code=400)
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
