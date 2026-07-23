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

---

# Redfish gateway (second subscriber → data-center management API)

`redfish_gateway.py` is a SECOND subscriber on the same MQTT bus. It subscribes
to `voltbridge/telemetry` and re-exposes the rack as a Redfish-style HTTP API —
the DMTF standard AI data centers use for power/thermal/storage management. This
shows the pub/sub payoff: the dashboard is one subscriber, this is another, both
fed by one stream, with zero changes to the bench.

## Run (with broker + bench --mqtt already running)
```
python redfish_gateway.py           # HTTP on :8080, broker localhost:1883
```

## Query from any client (browser, curl, DCIM tool)
```
curl http://localhost:8080/redfish/v1/Chassis/Rack1/Power
curl http://localhost:8080/redfish/v1/Chassis/Rack1/Thermal
curl http://localhost:8080/redfish/v1/Chassis/Rack1/Battery
```
Or open http://localhost:8080/ in a browser for a clickable index.

You'll see live rack power (PowerConsumedWatts), 800V bus voltage, module
temperatures, and energy-storage State-of-Charge — the same telemetry the
dashboard shows, in the management-plane format an operator's tools would poll.

## Scope (honest)
This models the READ / monitoring surface of Redfish. A production BMC also adds
authentication, event subscriptions, PATCH control actions and full DMTF
conformance. It's a representative gateway, not a certified Redfish service.

---

# OCPP gateway (EV side — second subscriber → charging management system)

`ocpp_gateway.py` is the EV-side mirror of the Redfish gateway. It subscribes to
`voltbridge/telemetry` (bench.py --mqtt, EV mode) and reports the charger to a
CSMS (Charging Station Management System) over OCPP 1.6-J — the de facto global
standard for charger-to-backend communication (mandated by EU AFIR and US NEVI).

So: Redfish for the DC rack, OCPP for the EV charger — one bench, both
management standards, both subscribers on the same MQTT bus.

Architecture:
```
bench --MQTT--> broker --MQTT--> ocpp_gateway (charge point) --OCPP/WS--> CSMS
```

## Run (EV mode)
Four windows: broker, bench (EV), the CSMS you watch, and the gateway.
```
# 1. broker (already covered above)
# 2. bench in EV mode, publishing to MQTT
python bench.py --mqtt --duration 600            # --mode ev is the default

# 3. a minimal CSMS to watch (prints incoming OCPP messages)
pip install websockets
python ocpp_csms.py                              # ws://localhost:9000

# 4. the charge-point gateway (MQTT -> OCPP)
python ocpp_gateway.py
```

Watch the CSMS window: you'll see real OCPP 1.6-J messages arrive —
BootNotification, StatusNotification (Available/Preparing/Charging/Faulted),
and MeterValues carrying live Voltage / Current / Power / SoC from the bench.
Inject a fault on the bench (`--fault oc --at 10`) and StatusNotification flips
to "Faulted" — the same event seen through the charging management protocol.

## Scope (honest)
Representative OCPP 1.6-J subset (BootNotification, StatusNotification,
MeterValues, Heartbeat). A production charge point implements the full
transaction/authorization/smart-charging message set and OCPP security
profiles. Representative, not a certified stack.

---

# Anomaly detector (statistical early-warning — third subscriber)

`anomaly_detector.py` is a THIRD subscriber on the telemetry bus. It watches the
live signals and raises EARLY WARNINGS before the bench's hard protection trips,
using transparent statistical process monitoring (not ML):
  * proximity  — reading within 92% of its protection limit
  * projection — linear trend extrapolated to the limit ("~N s to trip")
  * z-score    — statistical outlier vs a rolling window
It re-publishes alerts to `voltbridge/alerts`, so it's both subscriber and
publisher. Tuned conservatively to avoid false alarms on normal transients.

## Run (with broker + bench --mqtt running)
```
python anomaly_detector.py
# optional: watch the alert topic
mosquitto_sub -h localhost -t voltbridge/alerts -v
```
Inject a fault to see it warn first, e.g. over-temp:
```
python bench.py --mode dc --mqtt --fault ot --at 20 --duration 60
```
The detector prints a rising-temperature projection ("projected to reach 85degC
in ~Ns") shortly before the bench's F-OT-04 protection trips.

## Scope (honest)
Classical statistical process control — rolling mean/std, z-score, linear trend.
Transparent and explainable; NOT a trained ML model. It's an early-warning layer
that turns raw telemetry into "act before it trips."

---

---

# ML anomaly detector (unsupervised, covariance / Mahalanobis)

`ml_anomaly_detector.py` scores each telemetry sample against a model of NORMAL
operation and flags **multivariate** outliers — combinations of readings unlike
anything in normal operation, even when each reading is individually in range.
It auto-selects the model by telemetry mode and runs alongside the statistical
detector:

  * `anomaly_detector.py`    -> transparent, per-limit early warning (explainable)
  * `ml_anomaly_detector.py` -> novel multivariate deviations (learned)

## Two models — one per operating envelope
DC and EV have very different "normal", so each has its own model (trained by
`train_ml_anomaly.py`):

  * `ml_anomaly_model_dc.joblib` — 800VDC rack, a single EllipticEnvelope
    (robust covariance / Mahalanobis). Features: v_bus, i_bus, power_kw, temp,
    rect_temp, eff.
  * `ml_anomaly_model_ev.joblib` — EV fast-charge, split into TWO sub-models at
    SoC 80% (constant-current vs constant-voltage), because a charge has two
    genuinely different normal regimes. Pack-agnostic features (v_ratio, i_frac,
    temp, soc, eff) so 400 V and 800 V share one model.

Both: ~1% false-positive rate, 100% detection on held-out synthetic faults.

## Where the training data comes from
It is generated in code (`synth_telemetry.py`) to reproduce the bench's physics
per domain — nothing is downloaded. The models train on NORMAL samples only;
fault samples are used purely to measure detection. The same feature vectors let
you retrain on REAL logged telemetry as it accumulates — no code changes.

## Run
```
pip install scikit-learn numpy joblib paho-mqtt
python train_ml_anomaly.py          # once -> the two .joblib models
python ml_anomaly_detector.py       # subscribes; alerts -> voltbridge/alerts (source=ml)
```
With broker + `bench.py --mqtt` running (DC or EV), inject a fault; the ML
detector flags it (only during the TRANSFER phase). Watch:
`mosquitto_sub -t voltbridge/alerts -v`.

## Scope (honest)
Real unsupervised ML, trained on synthetic normal telemetry (not a labeled
production corpus). It complements — does not replace — the transparent
statistical layer.
