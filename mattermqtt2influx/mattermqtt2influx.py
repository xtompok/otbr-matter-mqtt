#!/usr/bin/env python3
"""Subscribe to the air quality MQTT topics and write them to InfluxDB 2.x.

Topic layout (MQTT_TOPIC_PREFIX of the bridge is two segments):

    matter/airquality/4/co2_ppm  ->  measurement "matter/airquality",
                                     tag unit=4, field co2_ppm

Configuration via environment variables (see mattermqtt2influx.env.example).
Works with both paho-mqtt 1.x (distro packages) and 2.x.
"""

import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import paho.mqtt.client as mqtt

MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USERNAME = os.environ.get("MQTT_USERNAME", "")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")
MQTT_TOPIC = os.environ.get("MQTT_TOPIC", "matter/airquality/#")

INFLUX_URL = os.environ.get("INFLUX_URL", "http://127.0.0.1:8086").rstrip("/")
INFLUX_ORG = os.environ["INFLUX_ORG"]
INFLUX_BUCKET = os.environ["INFLUX_BUCKET"]
INFLUX_TOKEN = os.environ["INFLUX_TOKEN"]

FLUSH_SECONDS = float(os.environ.get("FLUSH_SECONDS", "5"))
MAX_BUFFER = 10000

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("mattermqtt2influx")

buffer = []
buffer_lock = threading.Lock()


def escape_key(value: str) -> str:
    """Escape measurement names, tag values and field keys (line protocol)."""
    return (
        value.replace("\\", "\\\\")
        .replace(",", "\\,")
        .replace(" ", "\\ ")
        .replace("=", "\\=")
    )


def to_line(topic: str, payload: str) -> "str | None":
    parts = topic.split("/")
    if len(parts) != 4:
        return None
    prefix1, prefix2, unit, field = parts
    if unit == "bridge":  # bridge availability topic, not sensor data
        return None
    measurement = escape_key(f"{prefix1}/{prefix2}")
    try:
        value = repr(float(payload))
    except ValueError:
        escaped = payload.replace("\\", "\\\\").replace('"', '\\"')
        value = f'"{escaped}"'
    return (
        f"{measurement},unit={escape_key(unit)} "
        f"{escape_key(field)}={value} {int(time.time())}"
    )


def on_message(_client, _userdata, msg):
    try:
        line = to_line(msg.topic, msg.payload.decode())
    except UnicodeDecodeError:
        return
    if line is None:
        return
    with buffer_lock:
        if len(buffer) < MAX_BUFFER:
            buffer.append(line)


def on_connect(client, *_args, **_kwargs):
    log.info(
        "Connected to MQTT %s:%s, subscribing to %s",
        MQTT_HOST,
        MQTT_PORT,
        MQTT_TOPIC,
    )
    client.subscribe(MQTT_TOPIC)


def flush_loop():
    query = urllib.parse.urlencode(
        {"org": INFLUX_ORG, "bucket": INFLUX_BUCKET, "precision": "s"}
    )
    write_url = f"{INFLUX_URL}/api/v2/write?{query}"
    while True:
        time.sleep(FLUSH_SECONDS)
        with buffer_lock:
            if not buffer:
                continue
            lines = list(buffer)
        request = urllib.request.Request(
            write_url,
            data="\n".join(lines).encode(),
            headers={
                "Authorization": f"Token {INFLUX_TOKEN}",
                "Content-Type": "text/plain; charset=utf-8",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30):
                pass
        except (urllib.error.URLError, OSError) as err:
            body = getattr(err, "read", lambda: b"")()[:200]
            log.warning(
                "Influx write of %d points failed: %s %s", len(lines), err, body
            )
            continue  # keep the points, retry next flush
        with buffer_lock:
            del buffer[: len(lines)]
        log.debug("Wrote %d points", len(lines))


def make_client() -> mqtt.Client:
    if hasattr(mqtt, "CallbackAPIVersion"):  # paho-mqtt >= 2.0
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="mattermqtt2influx",
        )
    else:  # paho-mqtt 1.x
        client = mqtt.Client(client_id="mattermqtt2influx")
    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    return client


def main() -> None:
    threading.Thread(target=flush_loop, daemon=True).start()
    client = make_client()
    while True:
        try:
            client.connect(MQTT_HOST, MQTT_PORT)
            break
        except OSError as err:
            log.warning("MQTT connect failed: %s, retrying", err)
            time.sleep(5)
    client.loop_forever(retry_first_connection=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
