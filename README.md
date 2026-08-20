# Thread/Matter air quality sensor → MQTT

Reads an IKEA **Alpstuga** air quality sensor (Matter over Thread) and publishes
its measurements to MQTT, using an **ESP32-C6** as the Thread radio.

```
IKEA Alpstuga ──Thread(802.15.4)──> ESP32-C6 (OpenThread RCP firmware, USB)
                                        │
                                   otbr container (OpenThread Border Router)
                                        │ wpan0 (IPv6)
                                   matter-server container (Matter controller)
                                        │ websocket :5580
                                   mqtt-bridge container
                                        │
                                   MQTT broker (mosquitto or your own)
```

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

`4` is the Matter node id; prefix configurable via `MQTT_TOPIC_PREFIX` in `.env`.

## Deploying with Portainer

1. **Seed the state on the server first** — copy the `data/` directory from the
   machine where the sensor was commissioned to an absolute path on the server,
   e.g. `/opt/airquality/data` (it must contain `otbr/` and `matter/`; without
   it a new Thread network is formed and the sensor must be re-paired). Stop
   the stack on the old machine.
2. Plug the ESP32-C6 into the server and enable IPv6 forwarding:
   `sysctl net.ipv6.conf.all.forwarding=1` (persist in /etc/sysctl.d/).
3. Portainer → **Stacks → Add stack → Repository**:
   - Repository URL: this repo (for a private repo, add a GitHub token under
     *Authentication*)
   - Compose path: `docker-compose.yml`
   - Environment variables:

     | name | value |
     |------|-------|
     | `OTBR_INFRA_IF` | server LAN interface, e.g. `eth0` (**required**) |
     | `DATA_DIR` | `/opt/airquality/data` (absolute path from step 1) |
     | `MQTT_HOST` | your broker, or leave unset with the bundled one |
     | `COMPOSE_PROFILES` | `broker` — only to run the bundled mosquitto |

4. Deploy. The bridge image is built from `bridge/` during deployment.

Notes: all services use host networking, so nothing needs port mapping;
matter-server listens on `:5580` on all interfaces — firewall it if the server
is exposed. BlueZ on the host is only needed to commission additional devices.

## Deploying manually (docker compose CLI)

1. Copy this whole directory to the server **including `data/`** — it holds the
   Thread network credentials (`data/otbr`) and the Matter fabric with the
   already-commissioned sensor (`data/matter`). With it, no re-pairing is needed.
2. Plug the ESP32-C6 into the server (the single USB-C / native USB port).
   The `/dev/serial/by-id/...` path in `.env` is tied to the chip's MAC, so it
   is the same on every machine.
3. Copy `.env.example` to `.env` and edit it: set `OTBR_INFRA_IF` to the server's LAN interface
   (`ip route show default`), and `MQTT_HOST` if you already run a broker.
4. Host prerequisites: `sysctl net.ipv6.conf.all.forwarding=1` (persist in
   /etc/sysctl.d/), and BlueZ running only if you want to commission new
   devices there.
5. Start:

   ```sh
   docker compose --profile broker up -d --build    # with bundled mosquitto
   docker compose up -d --build                     # broker elsewhere
   ```

Stop the stack on the old machine first — two border routers with the same
dataset on different LANs is not a supported setup, and the sensor can only
follow one.

## Ports used (host network)

- `5580` matter-server websocket (all interfaces — firewall it if the server is exposed)
- `8080`/`8081` OTBR web UI / REST (127.0.0.1 only)
- `1883` mosquitto (only with `--profile broker`)

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
