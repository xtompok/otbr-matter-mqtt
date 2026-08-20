"""Bridge python-matter-server sensor attributes to MQTT.

Subscribes to the matter-server websocket, publishes every known
measurement attribute to MQTT as a retained per-metric topic:

    <MQTT_TOPIC_PREFIX>/<node_id>/<metric>

Configuration via environment variables (see .env / docker-compose.yml).
"""

import asyncio
import json
import logging
import os
import sys

import aiohttp
import paho.mqtt.client as mqtt

MATTER_WS_URL = os.environ.get("MATTER_WS_URL", "ws://127.0.0.1:5580/ws")
MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USERNAME = os.environ.get("MQTT_USERNAME", "")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")
MQTT_TOPIC_PREFIX = os.environ.get("MQTT_TOPIC_PREFIX", "airquality").rstrip("/")
RECONNECT_SECONDS = 5

AIR_QUALITY_ENUM = {
    0: "unknown",
    1: "good",
    2: "fair",
    3: "moderate",
    4: "poor",
    5: "very_poor",
    6: "extremely_poor",
}

# "<cluster>/<attribute>" -> (metric name, value transform)
# Attribute 0 is MeasuredValue in every measurement cluster.
CLUSTER_MAP = {
    "91/0": ("air_quality", lambda v: AIR_QUALITY_ENUM.get(v, str(v))),
    "1026/0": ("temperature_c", lambda v: round(v / 100, 2)),
    "1029/0": ("humidity_pct", lambda v: round(v / 100, 2)),
    "1036/0": ("co_ppm", float),
    "1037/0": ("co2_ppm", float),
    "1043/0": ("no2_ugm3", float),
    "1045/0": ("ozone_ugm3", float),
    "1066/0": ("pm25_ugm3", float),
    "1067/0": ("formaldehyde_ugm3", float),
    "1068/0": ("pm1_ugm3", float),
    "1069/0": ("pm10_ugm3", float),
    "1070/0": ("tvoc_ugm3", float),
    "1071/0": ("radon_bqm3", float),
}

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("matter2mqtt")


def make_mqtt_client() -> mqtt.Client:
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id="matter2mqtt",
    )
    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    status_topic = f"{MQTT_TOPIC_PREFIX}/bridge/status"
    client.will_set(status_topic, "offline", retain=True)
    client.on_connect = lambda c, *a: c.publish(
        status_topic, "online", retain=True
    )
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    client.connect_async(MQTT_HOST, MQTT_PORT)
    client.loop_start()
    return client


def publish_attribute(client: mqtt.Client, node_id, path: str, value) -> None:
    try:
        _endpoint, cluster, attribute = path.split("/")
    except ValueError:
        return
    mapping = CLUSTER_MAP.get(f"{cluster}/{attribute}")
    if mapping is None or value is None:
        return
    metric, transform = mapping
    try:
        payload = transform(value)
    except (TypeError, ValueError):
        log.warning("Cannot transform %s=%r", path, value)
        return
    topic = f"{MQTT_TOPIC_PREFIX}/{node_id}/{metric}"
    client.publish(topic, str(payload), retain=True)
    log.info("%s = %s", topic, payload)


async def run_once(client: mqtt.Client) -> None:
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(MATTER_WS_URL, heartbeat=30) as ws:
            info = await ws.receive_json()
            log.info(
                "Connected to matter-server (SDK %s)",
                info.get("sdk_version", "?"),
            )
            await ws.send_json(
                {"message_id": "listen", "command": "start_listening"}
            )
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                data = json.loads(msg.data)
                if data.get("message_id") == "listen":
                    for node in data.get("result") or []:
                        for path, value in node.get("attributes", {}).items():
                            publish_attribute(
                                client, node["node_id"], path, value
                            )
                elif data.get("event") == "attribute_updated":
                    node_id, path, value = data["data"]
                    publish_attribute(client, node_id, path, value)


async def main() -> None:
    client = make_mqtt_client()
    while True:
        try:
            await run_once(client)
            log.warning("Websocket closed, reconnecting")
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as err:
            log.warning("Matter-server connection failed: %s", err)
        await asyncio.sleep(RECONNECT_SECONDS)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
