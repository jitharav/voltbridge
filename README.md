# VoltBridge — 800VDC Hardware-in-the-Loop Bench

[![CI](https://github.com/jitharav/voltbridge/actions/workflows/ci.yml/badge.svg)](https://github.com/jitharav/voltbridge/actions/workflows/ci.yml)
[![Deploy](https://github.com/jitharav/voltbridge/actions/workflows/deploy.yml/badge.svg)](https://github.com/jitharav/voltbridge/actions/workflows/deploy.yml)

**Live dashboard:** https://jitharav.github.io/voltbridge/  (standalone simulation)

One 800&nbsp;VDC HIL validation bench, **two converging domains** — **EV fast-charging** and
**AI data-center racks** — proving that the same 800&nbsp;VDC power stage and the same
validation methodology serve both. Real protocol stacks over virtual transports, a live
instrument dashboard, a message bus with multiple management subscribers, and CI/CD.

---

## Why 800&nbsp;VDC, why both domains

Raising the bus to 800&nbsp;V (from 400&nbsp;V EVs / 48–54&nbsp;V racks) delivers the same power at
roughly half the current — losses scale with current², so cabling, heat and cooling all drop.
EV packs already productionised 400/800&nbsp;V; AI racks are now following (OCP *Diablo* at ±400&nbsp;V
bipolar, NVIDIA's monopolar 800&nbsp;V), reusing the EV power-electronics supply chain. VoltBridge
validates that shared 800&nbsp;VDC stage for both.

---

## Three real protocol stacks

The bench speaks real, decoded protocols over virtual transports (no hardware required):

| Domain | Protocol | Implementation |
| --- | --- | --- |
| EV internal control/protection | **CAN** | `python-can` + a DBC (`acan.dbc`), message-convention level |
| DC power components (rectifier, DC-DC) | **PMBus / SMBus** | LINEAR11/16 encoding, CRC-8 **PEC** checked |
| DC battery / BESS | **Modbus-TCP** | `pymodbus`, FC04 reads + FC06 writes |

A 25-check suite (`test_protocols.py`) exercises all three.

---

## Architecture — one stream, many subscribers

```
                                     ┌─→ Dashboard  (human UI, MQTT-over-WebSocket)
  bench.py ──MQTT──► broker ─────────┼─→ Redfish gateway ──► DC management clients   (Redfish  — data center)
  (CAN/PMBus/Modbus) (Mosquitto)     ├─→ OCPP gateway ─────► CSMS                     (OCPP 1.6 — EV charging)
                                     └─→ Anomaly detector ─► voltbridge/alerts        (statistical early-warning)
```

The bench publishes telemetry once; the dashboard, both management gateways, and the anomaly
detector are independent subscribers. Add another (a logger, a database, more dashboards) with
zero changes to the bench — that's the point of the pub/sub design.

---

## What's real vs representative (honest scoping)

**Real:** the three protocol stacks and their encodings/checks; the live physics (CC-CV charging,
thermal, efficiency, protection trips); MQTT pub/sub; the statistical early-warning maths; CI/CD.

**Representative (modelled, not certified):** the Redfish gateway models the read/monitoring
surface (a production BMC adds auth, events, control, full DMTF conformance); the OCPP gateway is
a 1.6-J subset (BootNotification, StatusNotification, MeterValues, Heartbeat); the CAN payloads
are convention-level; the anomaly layer is classical statistics, **not** a trained ML model.
Vehicle/chip specs are approximate published figures, unpublished ones marked *(est.)*.

---

## Run the dashboard (standalone)

Requires Node.js 18+.

```bash
npm install
npm run dev            # open the URL Vite prints (default http://localhost:5173)
npm run build          # static bundle for offline presenting
```

In the dashboard: pick a **vehicle** (EV) or **AI accelerator** (Data Center), press **START**,
watch the sequence run, and click an **Inject fault** button to trip protection.

---

## Run the full live stack (bench → bus → subscribers)

All commands from `voltbridge-bench/`. Install extras: `pip install -r requirements.txt paho-mqtt websockets`.

```bash
# 1. broker (TCP 1883 for the bench, WebSocket 9001 for the browser)
mosquitto -c mosquitto.conf -v

# 2. bench, publishing real telemetry to MQTT   (--mode ev is default; use --mode dc for the rack)
python bench.py --mode dc --mqtt --duration 600

# 3a. dashboard as a subscriber:  open  http://localhost:5173/?mqtt
# 3b. Redfish gateway (DC management API):
python redfish_gateway.py           # http://localhost:8080/redfish/v1/
# 3c. OCPP gateway (EV charging protocol) + a CSMS to watch:
python ocpp_csms.py                 # ws://localhost:9000
python ocpp_gateway.py
# 3d. statistical early-warning:
python anomaly_detector.py          # alerts also on topic  voltbridge/alerts
```

See `voltbridge-bench/MQTT_SETUP.md` for the full walkthrough and `curl` examples.

### Fault injection (CLI)

```bash
python bench.py --mode dc --mqtt --fault iso --at 8     # insulation
python bench.py --mode dc --mqtt --fault ov  --at 8     # overvoltage
python bench.py --mode dc --mqtt --fault oc  --at 8     # overcurrent
python bench.py --mode dc --mqtt --fault ot  --at 20    # over-temp (anomaly detector warns first)
python bench.py --mode dc --mqtt --fault comms --at 8   # comms loss
```

---

## Testing & CI

Every push runs, across **Windows + Linux × Python 3.9 / 3.11 / 3.12**:

```
test_protocols.py   # 25 checks — CAN + PMBus (PEC) + Modbus
test_gateway.py     # Redfish resource builders
test_ocpp.py        # OCPP 1.6-J message builders + status mapping
test_anomaly.py     # statistical early-warning (incl. no-false-alarm on steady state)
```

plus a compile check of every module and a dashboard build. Green CI auto-deploys the dashboard
to GitHub Pages.

---

## Repo layout

```
voltbridge/
  index.html  package.json  vite.config.js
  src/
    main.jsx                # React entry
    VoltBridge.jsx          # the whole dashboard (single component)
  voltbridge-bench/
    bench.py                # the HIL bench (physics + protocols + MQTT/WS)
    pmbus_stack.py  modbus_stack.py  dc_stack.py  dc_protocols.py  acan.dbc
    redfish_gateway.py      # DC management API subscriber (Redfish)
    ocpp_gateway.py  ocpp_csms.py    # EV charging protocol subscriber (OCPP 1.6-J)
    anomaly_detector.py     # statistical early-warning subscriber
    mosquitto.conf          # broker config (TCP 1883 + WebSocket 9001)
    test_protocols.py  test_gateway.py  test_ocpp.py  test_anomaly.py
    requirements.txt  MQTT_SETUP.md  DEMO_SCENARIOS.md
  .github/workflows/        # ci.yml + deploy.yml
```

---

## Notes

- Protection thresholds live in the bench's `LIMIT` object; physics constants in `step()`.
- The live MQTT/gateway demo runs **locally** (an HTTPS page can't open an insecure `ws://`);
  the deployed GitHub Pages site runs the standalone simulation.
- Specs and payloads are representative — swap in your real DBC / device map to target specific hardware.
