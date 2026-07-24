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
                                     ├─→ Anomaly detector ─► voltbridge/alerts        (statistical early-warning)
                                     └─→ ML anomaly detector ► voltbridge/alerts      (unsupervised, DC + EV models)
```

The bench publishes telemetry once; the dashboard, both management gateways, and the two anomaly
detectors are independent subscribers. Add another (a logger, a database, more dashboards) with
zero changes to the bench — that's the point of the pub/sub design.

---

## What's real vs representative (honest scoping)

**Real:** the three protocol stacks and their encodings/checks; the live physics (CC-CV charging,
thermal, efficiency, protection trips); MQTT pub/sub; the statistical early-warning maths; the
unsupervised ML anomaly models (scikit-learn); CI/CD.

**Representative (modelled, not certified):** the Redfish gateway models the read/monitoring
surface (a production BMC adds auth, events, control, full DMTF conformance); the OCPP gateway is
a 1.6-J subset (BootNotification, StatusNotification, MeterValues, Heartbeat); the CAN payloads
are convention-level. The ML anomaly models are genuine unsupervised learning, but trained on
**synthetic** normal telemetry that reproduces the bench physics (not a labelled production
corpus) — retrainable on real logged telemetry with the same feature vectors, no code changes.
Vehicle/chip specs are approximate published figures, unpublished ones marked *(est.)*.

---

## Anomaly detection — two complementary layers

VoltBridge runs two independent anomaly subscribers on the same telemetry bus:

- **Statistical early-warning** (`anomaly_detector.py`) — transparent, explainable, per-limit:
  proximity-to-limit, linear trend projection ("projected to reach limit in ~N s"), and z-score
  outliers. No training, no black box — every alert is traceable to a threshold.
- **Unsupervised ML** (`ml_anomaly_detector.py`) — catches **multivariate** outliers: combinations
  of readings unlike normal operation, even when each reading is individually in range.

The ML layer uses **one model per operating envelope**, because DC and EV have very different
"normal":

| Domain | Model | Features | Held-out result |
| --- | --- | --- | --- |
| DC rack | single EllipticEnvelope (robust covariance / Mahalanobis) | v_bus, i_bus, power_kw, temp, rect_temp, eff | ~1% FPR, 100% detection |
| EV charge | two sub-models split at SoC 80% (CC / CV) | v_ratio, i_frac, temp, soc, eff (pack-agnostic) | ~1% FPR, 100% detection |

The detector auto-selects the model by telemetry `mode`. Training data is **generated in code**
(`synth_telemetry.py`) to reproduce the bench physics per domain — nothing is downloaded; models
train on normal only, and can be retrained on real logged telemetry with the same feature vectors
and no code changes. The trained models (`ml_anomaly_model_dc.joblib`, `ml_anomaly_model_ev.joblib`)
are committed, so the detector runs without retraining; run `python train_ml_anomaly.py` to
regenerate them.

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
# 3e. ML anomaly detector (unsupervised; auto-selects DC or EV model by mode):
python ml_anomaly_detector.py       # needs scikit-learn; alerts on voltbridge/alerts (source=ml)
```

See `voltbridge-bench/MQTT_SETUP.md` for the full walkthrough and `curl` examples.

### Run the whole stack with Docker (one command)

The broker + bench + gateways + both anomaly detectors are containerised. The
bench runs one mode at a time, so services are grouped into profiles. The ML
detector uses a separate image (`Dockerfile.ml`) so scikit-learn doesn't bloat
the lean real-time services. From `voltbridge-bench/`:

```bash
# DC / data-center rack  → Redfish console at http://localhost:8080/
docker compose --profile dc up --build
CHIP=tpu docker compose --profile dc up            # pick an accelerator

# EV / fast-charge       → OCPP monitor at http://localhost:9100/
docker compose --profile ev up --build
VEHICLE=models docker compose --profile ev up      # pick a car
```

The broker also exposes WebSocket on `localhost:9001`, so the host dashboard
(`npm run dev` → `http://localhost:5173/?mqtt`) connects to the containerised
broker unchanged. Run one profile at a time (a single bench owns the telemetry
topic).

> The full stack has been verified running locally via Docker Desktop (WSL 2
> backend) as well as building in CI. On Windows, Docker Desktop needs the
> "Virtual Machine Platform" feature and WSL 2 (`wsl --install`); on a corporate
> VM this requires nested virtualization to be available. If Docker cannot start
> in your environment, the manual multi-terminal setup above is a complete
> substitute, and CI still builds the images.

### Run on a phone / tablet (PWA)

The dashboard can be installed as a Progressive Web App. In `standalone` mode it
runs entirely in the browser (no backend), so it works offline and is ideal for a
portable demo. Build and serve, then use the browser's *Add to Home screen* /
*Install app*:

```bash
npm run build && npm run preview -- --host   # open the printed Network URL on the phone
```

PWA assets live in `public/` (`manifest.webmanifest`, `sw.js`, `icons/`) with a
mobile stylesheet in `src/mobile.css`; see `voltbridge-mobile-pwa/README_MOBILE.md`.

### Fleet / cloud-native scaling (`fleet/`)

Each bench is a node. The `fleet/` module is a working demonstrator of how nodes
aggregate at the edge and scale into a cloud backend: **MQTT (edge) → aggregation
bridge → Kafka → TimescaleDB → fleet dashboard**, with a multi-node simulator, a
scaling harness, Docker Compose, and Kubernetes manifests.

```bash
cd fleet
python scale_test.py                    # in-process scaling curve (no infra)
docker compose -f docker-compose.fleet.yml up --build   # full pipeline → http://localhost:8090/
NODES=500 docker compose -f docker-compose.fleet.yml up  # bigger fleet
docker compose -f docker-compose.fleet.yml up --scale consumer=3   # horizontal consumers
kubectl apply -f k8s/                   # deploy path (consumer Deployment + HPA)
```

Measured (single machine, in-process harness): ingest tracks offered load to
~1,000 nodes at ~5,000 msg/s with single-digit-to-low-tens-of-ms p95 latency.
Horizontal scale beyond one worker is Kafka partitions + a consumer group.
**Honest scope:** a local demonstrator of the pipeline and its scaling behaviour
using the same components as a hyperscaler deployment — not a deployed managed
cloud system. Third-party components (Apache Kafka — Apache-2.0; TimescaleDB —
Apache-2.0/community; Mosquitto — EPL-2.0/EDL-1.0; kafka-python — Apache-2.0;
paho-mqtt — EPL-2.0/EDL-1.0; psycopg2 — LGPL-3.0; numpy — BSD-3) are used
unmodified; see `fleet/README_FLEET.md` for the full licence table and scope.

### Fault injection (CLI)

```bash
python bench.py --mode dc --mqtt --fault iso --at 8     # insulation
python bench.py --mode dc --mqtt --fault ov  --at 8     # overvoltage
python bench.py --mode dc --mqtt --fault oc  --at 8     # overcurrent
python bench.py --mode dc --mqtt --fault ot  --at 20    # over-temp (anomaly detector warns first)
python bench.py --mode dc --mqtt --fault comms --at 8   # comms loss
```

Both anomaly detectors flag injected faults on `voltbridge/alerts`: the statistical one gives
explainable, per-limit early warning; the ML one flags multivariate outliers. The ML detector
runs in DC or EV mode (it auto-selects the matching model) and scores during the transfer phase.

---

## Testing & CI

Every push runs, across **Windows + Linux × Python 3.9 / 3.11 / 3.12**:

```
test_protocols.py   # 25 checks — CAN + PMBus (PEC) + Modbus
test_gateway.py     # Redfish resource builders
test_ocpp.py        # OCPP 1.6-J message builders + status mapping
test_anomaly.py     # statistical early-warning (incl. no-false-alarm on steady state)
test_ml_anomaly.py  # unsupervised ML models (DC + EV): <5% false-positive, >95% detection
```

plus a compile check of every module, a dashboard build, validation of both Docker
Compose profiles, and a Docker image build of the lean bench image and the ML image.
Green CI auto-deploys the dashboard to GitHub Pages.

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
    synth_telemetry.py      # synthetic telemetry generators (DC + EV) for training
    train_ml_anomaly.py     # trains the unsupervised models -> *.joblib
    ml_anomaly_detector.py  # ML anomaly subscriber (auto-selects DC/EV model)
    ml_anomaly_model_dc.joblib  ml_anomaly_model_ev.joblib   # trained models (committed)
    mosquitto.conf          # broker config (TCP 1883 + WebSocket 9001)
    Dockerfile  Dockerfile.ml  docker-compose.yml   # containerised stack (dc/ev profiles)
    test_protocols.py  test_gateway.py  test_ocpp.py  test_anomaly.py  test_ml_anomaly.py
    requirements.txt  MQTT_SETUP.md  DEMO_SCENARIOS.md  DEMO_RUNBOOK.md
  .github/workflows/        # ci.yml + deploy.yml
```

---

## Notes

- Protection thresholds live in the bench's `LIMIT` object; physics constants in `step()`.
- The live MQTT/gateway demo runs **locally** (an HTTPS page can't open an insecure `ws://`);
  the deployed GitHub Pages site runs the standalone simulation.
- Specs and payloads are representative — swap in your real DBC / device map to target specific hardware.

---

## License

This project's own code is released under the **MIT License** — see [`LICENSE`](LICENSE).

## Third-party & acknowledgements

VoltBridge builds on open-source software, all under permissive licenses (no copyleft
obligations on this project's code):

| Component | Use | License |
| --- | --- | --- |
| React, Vite, Recharts | dashboard UI | MIT |
| MQTT.js | browser MQTT (over WebSocket) | MIT |
| cantools | CAN/DBC handling | MIT |
| pymodbus | Modbus-TCP stack | BSD-3-Clause |
| python-can | CAN transport | LGPL-3.0 (used as an unmodified library) |
| paho-mqtt | MQTT publisher/subscriber | EPL-2.0 / EDL-1.0 |
| Eclipse Mosquitto | MQTT broker | EPL-2.0 / EDL-1.0 |
| scikit-learn, NumPy, SciPy, joblib | unsupervised ML anomaly models | BSD-3-Clause |

**CAN definitions.** The `acan.dbc` message conventions reference **ACAN**, Ather Energy's
open-source CAN interface project (MIT). Used at the message-convention level with attribution;
this is a representative DBC, not an official or complete IS 17017 message set.

**Standards.** OCPP, Redfish, PMBus, Modbus, ISO 15118, IEC 61851 and IS 17017 are referenced by
name only. This project implements **representative subsets** and does **not** reproduce any
copyrighted specification text, tables, or full schemas from those standards bodies (OCA, DMTF,
SMIF, ISO, IEC, BIS).

**Trademarks.** Vehicle and accelerator names (e.g. Tesla, Ferrari, BMW, NVIDIA, AMD) are used
factually to denote real products (nominative use); no brand logos are reproduced. Product figures
are approximate published values, with unpublished ones marked *(est.)*.
