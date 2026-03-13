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

from config import (
    SERIAL_PORT, BAUD_RATE, SLAVE_ADDRESS,
    LOW_THRESHOLD, HIGH_THRESHOLD, POLL_INTERVAL,
    WEB_HOST, WEB_PORT, LOG_FILE,
    MODE_UTI, MODE_SBU, MODE_SUB, MODE_SOL,
    CHARGE_SNU, CHARGE_OSO,
    MODE_NAMES, CHARGE_NAMES,
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

    logging.info("=" * 60)
    logging.info("Solar Controller + Dashboard Starting")
    logging.info(f"  Low threshold:  {LOW_THRESHOLD}% (switch to UTI)")
    logging.info(f"  High threshold: {HIGH_THRESHOLD}% (switch to SBU)")
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
                if startup:
                    # Set initial mode
                    if soc >= HIGH_THRESHOLD:
                        await asyncio.to_thread(controller.set_output_mode, MODE_SBU)
                        await asyncio.sleep(0.5)
                        await asyncio.to_thread(controller.set_charge_mode, CHARGE_OSO)
                    elif soc <= LOW_THRESHOLD:
                        await asyncio.to_thread(controller.set_output_mode, MODE_UTI)
                        await asyncio.sleep(0.5)
                        await asyncio.to_thread(controller.set_charge_mode, CHARGE_SNU)
                    else:
                        await asyncio.to_thread(controller.set_output_mode, MODE_UTI)
                        await asyncio.sleep(0.5)
                        await asyncio.to_thread(controller.set_charge_mode, CHARGE_OSO)
                    startup = False

                # Grid down
                if not grid_present:
                    if not controller.grid_was_down:
                        logging.warning("GRID DOWN - SBU for battery backup")
                        controller.grid_was_down = True
                    if current_mode != MODE_SBU:
                        await asyncio.to_thread(controller.set_output_mode, MODE_SBU)

                elif controller.grid_was_down and grid_present:
                    logging.info("GRID RESTORED - resuming SOC control")
                    controller.grid_was_down = False

                # SOC low
                if grid_present and soc <= LOW_THRESHOLD:
                    if current_mode != MODE_UTI:
                        logging.info(f"SOC {soc}% <= {LOW_THRESHOLD}% → UTI")
                        await asyncio.to_thread(controller.set_output_mode, MODE_UTI)
                        await asyncio.sleep(0.5)
                    if status["charge_mode"] != CHARGE_SNU:
                        logging.info(f"SOC {soc}% <= {LOW_THRESHOLD}% → SNU")
                        await asyncio.to_thread(controller.set_charge_mode, CHARGE_SNU)

                # SOC high
                elif grid_present and soc >= HIGH_THRESHOLD:
                    if current_mode != MODE_SBU:
                        logging.info(f"SOC {soc}% >= {HIGH_THRESHOLD}% → SBU")
                        await asyncio.to_thread(controller.set_output_mode, MODE_SBU)
                        await asyncio.sleep(0.5)
                    if status["charge_mode"] != CHARGE_OSO:
                        logging.info(f"SOC {soc}% >= {HIGH_THRESHOLD}% → OSO")
                        await asyncio.to_thread(controller.set_charge_mode, CHARGE_OSO)

                # Hysteresis
                elif grid_present and LOW_THRESHOLD < soc < HIGH_THRESHOLD:
                    if status["charge_mode"] != CHARGE_OSO:
                        logging.info(f"SOC {soc}% hysteresis → OSO")
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
    global controller, control_task, log_event_loop

    setup_logging()
    log_event_loop = asyncio.get_running_loop()
    init_db()

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
    return JSONResponse({
        "low_threshold": LOW_THRESHOLD,
        "high_threshold": HIGH_THRESHOLD,
        "poll_interval": POLL_INTERVAL,
        "manual_mode": manual_mode,
    })


# --- Manual Control ---

async def _broadcast_manual_update():
    """Push updated manual_mode to all WS clients immediately."""
    if latest_status:
        latest_status["manual_mode"] = manual_mode
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
