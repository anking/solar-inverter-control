"""
MQTT bridge for publishing telemetry to the cloud backend and
receiving remote commands. All methods are no-ops when MQTT is
not configured, so the controller works identically offline.
"""

import json
import logging
import threading
import uuid

import paho.mqtt.client as mqtt

from config import load_mqtt_config, save_mqtt_config


def _get_mac_address() -> str:
    """Return the device MAC address as a dash-separated lowercase string."""
    mac_int = uuid.getnode()
    mac_hex = f"{mac_int:012x}"
    return "-".join(mac_hex[i:i+2] for i in range(0, 12, 2))


class MqttBridge:
    """Manages MQTT connection, telemetry publishing, and command reception."""

    def __init__(self):
        self._client: mqtt.Client | None = None
        self._connected = False
        self._command_handler = None
        self._lock = threading.Lock()
        self.mac = _get_mac_address()
        self._config = load_mqtt_config()

    @property
    def configured(self) -> bool:
        return bool(self._config.get("host"))

    @property
    def connected(self) -> bool:
        return self._connected

    def get_status(self) -> dict:
        return {
            "configured": self.configured,
            "connected": self._connected,
            "host": self._config.get("host", ""),
            "port": self._config.get("port", 1883),
            "mac_address": self.mac,
        }

    def register_command_handler(self, handler):
        """Register async callback: handler(action: str, params: dict) -> dict"""
        self._command_handler = handler

    def connect(self):
        """Connect to MQTT broker if configured. Safe to call repeatedly."""
        if not self.configured:
            return

        host = self._config["host"]
        port = int(self._config.get("port", 1883))

        try:
            self._client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                client_id=f"solar-pi-{self.mac}",
            )

            # Last Will and Testament
            lwt_topic = f"devices/{self.mac}/status"
            lwt_payload = json.dumps({"online": False, "ts": ""})
            self._client.will_set(lwt_topic, lwt_payload, qos=1, retain=True)

            # Optional auth
            username = self._config.get("username")
            password = self._config.get("password")
            if username:
                self._client.username_pw_set(username, password)

            self._client.on_connect = self._on_connect
            self._client.on_disconnect = self._on_disconnect
            self._client.on_message = self._on_message

            self._client.connect(host, port, keepalive=60)
            self._client.loop_start()
            logging.info(f"MQTT: connecting to {host}:{port} as {self.mac}")
        except Exception as e:
            logging.error(f"MQTT: connection failed: {e}")
            self._connected = False

    def disconnect(self):
        """Cleanly disconnect from broker."""
        if self._client and self._connected:
            # Publish offline status
            topic = f"devices/{self.mac}/status"
            self._client.publish(topic, json.dumps({"online": False}), qos=1, retain=True)
            self._client.loop_stop()
            self._client.disconnect()
            self._connected = False
            logging.info("MQTT: disconnected")

    def configure(self, host: str, port: int = 1883, username: str = "", password: str = ""):
        """Update MQTT config, save, and reconnect."""
        self.disconnect()
        self._config = {
            "host": host,
            "port": port,
            "username": username,
            "password": password,
        }
        save_mqtt_config(self._config)
        self.connect()

    def clear_config(self):
        """Remove MQTT config and disconnect."""
        self.disconnect()
        self._config = {"host": "", "port": 1883, "username": "", "password": ""}
        save_mqtt_config(self._config)

    def publish_telemetry(self, status: dict):
        """Publish a telemetry snapshot. No-op if not connected."""
        if not self._connected or not self._client:
            return
        try:
            topic = f"devices/{self.mac}/telemetry"
            payload = json.dumps(status, default=str)
            self._client.publish(topic, payload, qos=0)
        except Exception as e:
            logging.error(f"MQTT: telemetry publish failed: {e}")

    # ---- paho callbacks ----

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            self._connected = True
            logging.info("MQTT: connected to broker")

            # Publish online status (retained)
            topic = f"devices/{self.mac}/status"
            client.publish(topic, json.dumps({"online": True}), qos=1, retain=True)

            # Subscribe to commands
            cmd_topic = f"devices/{self.mac}/commands"
            client.subscribe(cmd_topic, qos=1)
            logging.info(f"MQTT: subscribed to {cmd_topic}")
        else:
            logging.error(f"MQTT: connect failed rc={rc}")
            self._connected = False

    def _on_disconnect(self, client, userdata, flags, rc, properties=None):
        self._connected = False
        if rc != 0:
            logging.warning(f"MQTT: unexpected disconnect rc={rc}, will auto-reconnect")

    def _on_message(self, client, userdata, msg):
        """Handle incoming command messages."""
        try:
            payload = json.loads(msg.payload.decode())
            cmd_id = payload.get("id", "")
            action = payload.get("action", "")
            params = payload.get("params", {})

            logging.info(f"MQTT: received command {action} (id={cmd_id})")

            if self._command_handler:
                # Run the handler in a thread (it may be async)
                import asyncio

                async def _run():
                    try:
                        result = await self._command_handler(action, params)
                        response = {
                            "id": cmd_id,
                            "success": True,
                            "data": result,
                            "error": None,
                        }
                    except Exception as e:
                        response = {
                            "id": cmd_id,
                            "success": False,
                            "data": None,
                            "error": str(e),
                        }

                    resp_topic = f"devices/{self.mac}/responses"
                    client.publish(resp_topic, json.dumps(response), qos=1)

                # Schedule the coroutine on the main event loop
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(_run())
                except RuntimeError:
                    # No running loop in this thread — use run_coroutine_threadsafe
                    from server import log_event_loop
                    if log_event_loop:
                        asyncio.run_coroutine_threadsafe(_run(), log_event_loop)

        except Exception as e:
            logging.error(f"MQTT: error handling command: {e}")
