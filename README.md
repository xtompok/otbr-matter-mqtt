# Thread/Matter air quality sensor → MQTT

Reads an IKEA **Alpstuga** air quality sensor (Matter over Thread) and publishes
its measurements to MQTT, using an **ESP32-C6** as the Thread radio.

```
IKEA Alpstuga ──Thread(802.15.4)──> ESP32-C6 (OpenThread RCP firmware, USB)
                                        │
              ┌─ isolated network namespace ("netns" container) ─┐
              │  otbr (OpenThread Border Router)                 │
              │     │ wpan0 (IPv6)                               │
              │  matter-server (Matter controller, ws :5580)     │
              │     │                                            │
              │  mqtt-bridge                                     │
              └─────┼────────────────────────────────────────────┘
                    └──> MQTT broker (the stack's only output)
```

The whole Matter/Thread side runs inside a private network namespace shared by
the three containers (`network_mode: service:netns`). **No ports are published
on the host** and nothing on the LAN can see the Thread network or the Matter
fabric; the only external traffic is the bridge's outbound connection to your
MQTT broker (plus image pulls / a one-time certificate fetch by matter-server).
The optional bundled mosquitto is the single exception — it publishes `1883`.

Consequence: phone apps / Home Assistant cannot join this fabric, and
commissioning is done via CLI (see below). That is intentional.

## MQTT topics

Retained, updated whenever the sensor reports (typically every few seconds):

| topic                          | example | unit |
|--------------------------------|---------|------|
| `airquality/4/temperature_c`   | 26.34   | °C |
| `airquality/4/humidity_pct`    | 57.8    | % |
| `airquality/4/co2_ppm`         | 774.0   | ppm |
| `airquality/4/pm25_ugm3`       | 2.0     | µg/m³ |
| `airquality/4/air_quality`     | good    | good/fair/moderate/poor/very_poor/extremely_poor |
| `airquality/bridge/status`     | online  | LWT |

`4` is the Matter node id; prefix configurable via `MQTT_TOPIC_PREFIX`.

## Deploying with Portainer

1. **Seed the state on the server first** — copy the `data/` directory from the
   machine where the sensor was commissioned to an absolute path on the server,
   e.g. `/opt/airquality/data` (it must contain `otbr/` and `matter/`; without
   it a new Thread network is formed and the sensor must be re-paired). Stop
   the stack on the old machine — the sensor can only follow one border router.
2. Plug the ESP32-C6 into the server and enable IPv6 forwarding:
   `sysctl net.ipv6.conf.all.forwarding=1` (persist in /etc/sysctl.d/).
3. Portainer → **Stacks → Add stack → Repository**:
   - Repository URL: this repo (for a private repo, add a GitHub token under
     *Authentication*)
   - Compose path: `docker-compose.yml`
   - Environment variables:

     | name | value |
     |------|-------|
     | `MQTT_HOST` | your broker's address (**required**; not `127.0.0.1` — the bridge is namespaced). `host.docker.internal` = broker on the docker host (must listen on 0.0.0.0); `mosquitto` = the bundled broker |
     | `DATA_DIR` | `/opt/airquality/data` (absolute path from step 1) |
     | `MQTT_PORT` / `MQTT_USERNAME` / `MQTT_PASSWORD` / `MQTT_TOPIC_PREFIX` | as needed |
     | `COMPOSE_PROFILES` | `broker` — only to run the bundled mosquitto |

4. Deploy. The bridge image is built from `bridge/` during deployment.

BlueZ on the host is only needed to commission additional devices.

## Deploying manually (docker compose CLI)

Copy `.env.example` to `.env`, set at least `MQTT_HOST`, then:

```sh
docker compose up -d --build
```

Migration between machines = move this directory including `data/`, plug the
ESP into the new machine (the `/dev/serial/by-id/...` path is tied to the
chip's MAC, so it is identical everywhere), same host prerequisites as above.

## Flashing a (new) ESP32-C6

Prebuilt binaries are in `firmware/` (ESP-IDF v5.3 `ot_rcp` example with
`CONFIG_OPENTHREAD_RCP_USB_SERIAL_JTAG=y`, i.e. Spinel over the native USB
port — no second UART cable needed):

```sh
docker run --rm --device=/dev/ttyACM0 -v "$PWD/firmware":/fw espressif/idf:release-v5.3 \
  bash -c 'cd /fw && python -m esptool --chip esp32c6 -p /dev/ttyACM0 -b 460800 \
    --before default_reset --after hard_reset write_flash --flash_mode dio \
    --flash_size 2MB --flash_freq 80m \
    0x0 bootloader.bin 0x8000 partition-table.bin 0x10000 esp_ot_rcp.bin'
```

## Commissioning another Matter device

`tools/ws_cmd.py` talks to the matter-server websocket. The device must be in
pairing mode (IKEA: 4 quick presses of the pairing button) and within
Bluetooth range of the host.

```sh
# one-time after a fresh matter-server storage: hand over Thread credentials
DS=$(docker exec otbr ot-ctl dataset active -x | head -1 | tr -d '\r')
docker cp tools/ws_cmd.py matter-server:/tmp/ws_cmd.py
docker exec matter-server python /tmp/ws_cmd.py set_thread_dataset "{\"dataset\": \"$DS\"}"

# pair (code from the device label, dashes allowed)
docker exec matter-server python /tmp/ws_cmd.py commission_with_code '{"code": "1234-567-8901"}' 240

# inspect a node
docker exec matter-server python /tmp/ws_cmd.py get_node '{"node_id": 4}'
```

New measurement clusters (PM1/PM10/TVOC/NO₂/CO/radon/…) are already mapped in
`bridge/bridge.py` and will appear as topics automatically.

## First-time Thread network setup

Only needed if `data/otbr` is ever wiped:

```sh
docker exec otbr sh -c 'ot-ctl dataset init new && ot-ctl dataset commit active && ot-ctl ifconfig up && ot-ctl thread start'
```

Then re-run the `set_thread_dataset` step above and re-commission devices.

## Saving the data to InfluxDB (mattermqtt2influx)

`mattermqtt2influx/` contains a standalone consumer that subscribes to the
bridge's topics and writes them to an InfluxDB 2.x bucket: measurement
`matter/airquality` (the first two topic segments — set the bridge's
`MQTT_TOPIC_PREFIX=matter/airquality`), tag `unit` = sensor node id, and one
field per metric (`co2_ppm=774.0`, `air_quality="good"`, …). It runs as a
systemd **user unit** under an unprivileged account, from `~/mattermqtt2influx`.
Install on the server, as that user:

```sh
sudo apt install python3-paho-mqtt     # the only dependency
cp -r mattermqtt2influx ~/
cd ~/mattermqtt2influx
cp mattermqtt2influx.env.example mattermqtt2influx.env
chmod 600 mattermqtt2influx.env        # holds the Influx token
$EDITOR mattermqtt2influx.env          # broker + Influx URL/org/bucket/token
mkdir -p ~/.config/systemd/user
cp mattermqtt2influx.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now mattermqtt2influx
sudo loginctl enable-linger "$USER"    # keep it running without a login session
```

Check it with `systemctl --user status mattermqtt2influx` /
`journalctl --user -u mattermqtt2influx`.

Points are batched and flushed every 5 s; on Influx outages they are retained
and retried (up to 10k points).

## Debugging inside the namespace

Nothing is reachable from the host, but `docker exec` still works:

```sh
docker exec otbr ot-ctl state                # Thread role (router/leader/child)
docker exec otbr wget -qO- localhost:8081/node # OTBR REST API
docker logs matter-mqtt-bridge               # published values
```
