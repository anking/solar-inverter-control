# Solar Inverter Controller — Deployment Guide

## Prerequisites

- Raspberry Pi with Raspbian/Raspberry Pi OS
- Python 3.9+
- USB-RS485 adapter connected to inverter WIFI port (RJ45 pins 7=A, 8=B)
- `pi` user in the `dialout` group (for serial access)

Check serial access:
```bash
groups pi  # should include 'dialout'
# If not:
sudo usermod -aG dialout pi
# Then log out and back in
```

## Installation

```bash
# Clone or copy the project
cd /home/pi
git clone <your-repo-url> solar-inverter-control
cd solar-inverter-control

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

## Configuration

All settings use environment variables with sensible defaults.
You can override them in the systemd service file or a `.env` file.

| Variable | Default | Description |
|---|---|---|
| `SOLAR_SERIAL_PORT` | `/dev/ttyUSB0` | USB-RS485 device path |
| `SOLAR_BAUD_RATE` | `9600` | Serial baud rate |
| `SOLAR_SLAVE_ADDRESS` | `1` | Modbus slave address |
| `SOLAR_INVERTER_PASSWORD` | `6666` | Inverter write password |
| `SOLAR_LOW_THRESHOLD` | `82` | SOC % to switch to UTI |
| `SOLAR_HIGH_THRESHOLD` | `92` | SOC % to switch to SBU |
| `SOLAR_POLL_INTERVAL` | `10` | Seconds between reads |
| `SOLAR_WEB_HOST` | `0.0.0.0` | Web server bind address |
| `SOLAR_WEB_PORT` | `8080` | Web server port |
| `SOLAR_DB_PATH` | `/home/pi/solar_data.db` | SQLite database path |
| `SOLAR_LOG_FILE` | `/home/pi/solar_controller.log` | Log file path |

## Quick Test

```bash
# Test that the web server starts (will show serial error if no inverter connected)
source venv/bin/activate
python server.py
# Visit http://<pi-ip>:8080 in your browser
# Ctrl+C to stop
```

## Systemd Service (Auto-Start)

### Install the service

```bash
# Copy the service file
sudo cp systemd/solar-controller.service /etc/systemd/system/

# Reload systemd
sudo systemctl daemon-reload

# Enable auto-start on boot
sudo systemctl enable solar-controller

# Start the service now
sudo systemctl start solar-controller
```

### Manage the service

```bash
# Check status
sudo systemctl status solar-controller

# View live logs
journalctl -u solar-controller -f

# View recent logs
journalctl -u solar-controller --since "1 hour ago"

# Restart after code changes
sudo systemctl restart solar-controller

# Stop the service
sudo systemctl stop solar-controller

# Disable auto-start
sudo systemctl disable solar-controller
```

### Override environment variables

To change settings without editing the service file:

```bash
sudo systemctl edit solar-controller
```

This opens an editor. Add overrides like:

```ini
[Service]
Environment=SOLAR_LOW_THRESHOLD=80
Environment=SOLAR_HIGH_THRESHOLD=95
Environment=SOLAR_POLL_INTERVAL=15
```

Save and restart:
```bash
sudo systemctl restart solar-controller
```

## Accessing the Dashboard

Open a browser and go to:

```
http://<pi-ip-address>:8080
```

To find your Pi's IP:
```bash
hostname -I
```

## Firewall (if applicable)

If you have `ufw` enabled:
```bash
sudo ufw allow 8080/tcp
```

## Standalone Controller (No Dashboard)

If you only need the control loop without the web dashboard:

```bash
source venv/bin/activate
python solar_controller.py
```

## Troubleshooting

**Serial connection fails:**
```bash
# Check if the USB adapter is detected
ls -la /dev/ttyUSB*
# Check if pi user has dialout group
groups
# Test with mbpoll
mbpoll -m rtu -b 9600 -P none -a 1 -r 257 /dev/ttyUSB0
```

**Dashboard not loading:**
```bash
# Check if the service is running
sudo systemctl status solar-controller
# Check for port conflicts
sudo ss -tlnp | grep 8080
```

**Database issues:**
```bash
# Check DB exists and has data
sqlite3 /home/pi/solar_data.db "SELECT COUNT(*) FROM readings;"
sqlite3 /home/pi/solar_data.db "SELECT * FROM readings ORDER BY timestamp DESC LIMIT 5;"
```
