# VoltBridge over MQTT (optional, industrial pub/sub)

The bench streams telemetry over a WebSocket by default (`--ws`). It can *also*
publish to an MQTT broker (`--mqtt`) — the message pattern real industrial /
IoT telemetry uses. WebSocket still works; MQTT is an opt-in upgrade.

## Architecture

```
  bench.py  --(MQTT/TCP 1883)-->  broker  --(MQTT/WebSocket 9001)-->  dashboard (browser)
   publisher                     (Mosquitto)                           subscriber
```

Topics published:
- `voltbridge/telemetry`      — full telemetry JSON (the dashboard subscribes here)
- `voltbridge/frames/can`     — one message per CAN frame
- `voltbridge/frames/pmbus`   — one message per PMBus frame
- `voltbridge/frames/modbus`  — one message per Modbus frame

The per-protocol topics show the event-driven split: each protocol stack is a
publisher; the dashboard, loggers and analytics are independent subscribers.

## 1. Install a broker (Mosquitto)

- Windows: download from https://mosquitto.org/download/ (or `choco install mosquitto`)
- macOS:   `brew install mosquitto`
- Linux:   `sudo apt install mosquitto mosquitto-clients`

## 2. Start the broker with the provided config

Browsers can't speak raw MQTT, so we run two listeners (TCP 1883 for the bench,
WebSocket 9001 for the browser). The `mosquitto.conf` in this folder sets both:

```
mosquitto -c mosquitto.conf -v
```

## 3. Install the Python MQTT client and run the bench with --mqtt

```
python -m pip install paho-mqtt
python bench.py --mode dc --mqtt --duration 300
```
You should see: `MQTT telemetry on localhost:1883  topics: voltbridge/telemetry, voltbridge/frames/#`

(You can run `--ws --mqtt` together to publish to both at once.)

## 4. Point the dashboard at MQTT

Open the dashboard with `?mqtt` in the URL:

```
http://localhost:5173/?mqtt
```
or the deployed site:
```
https://jitharav.github.io/voltbridge/?mqtt
```
Optional custom broker: `?mqtt&broker=localhost:9001` (default is localhost:9001).
The dashboard lazy-loads an MQTT-over-WebSocket client and subscribes to
`voltbridge/telemetry`. Without `?mqtt`, it uses the WebSocket path as before.

## 5. (Nice demo) watch the raw topics in a terminal

```
mosquitto_sub -h localhost -t 'voltbridge/frames/#' -v
```
This prints every PMBus / Modbus / CAN frame as it's published — a clear,
independent proof that the stacks are publishing to a real message bus.

## Notes
- MQTT mode needs the broker running and (for the browser client) internet access
  to load the MQTT-over-WebSocket library from a CDN.
- Nothing here changes the default behaviour: no `--mqtt` flag and no `?mqtt`
  means the bench and dashboard work exactly as before over WebSocket.
