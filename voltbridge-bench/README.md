# VoltBridge HIL Bench — standalone backend

A software hardware-in-the-loop bench for an **800 VDC** power stage. It runs a
**real CAN stack** (`python-can`) over a **virtual bus**, so it needs **no
hardware**. Two nodes (EVSE and EV) exchange real, DBC-encoded ACAN frames while
a plant model + IS 17017 state machine + protection logic drive the power stage.

This is a companion to the VoltBridge dashboard. It runs independently — the
dashboard stays self-contained; this shows the real CAN backend underneath.

## What's real here

- Real `python-can` bus and real `cantools` encode/decode — the frames on screen
  are genuine CAN frames, not print statements.
- Two-node conversation (EVSE ⇄ EV) proving actual transport.
- Protection logic that **reacts** to thresholds (insulation, overvoltage,
  overcurrent, over-temperature, comms timeout), not scripted animations.

## Install & run (Windows / macOS / Linux)

Requires Python 3.9+.

```bash
pip install -r requirements.txt
python bench.py                       # EV mode, clean 20 s session
python bench.py --mode dc             # AI data-center rack mode
python bench.py --fault iso --at 6    # insulation fault at t = 6 s
python bench.py --fault ot --at 8 --mode dc
```

Fault keys: `iso` (insulation) · `ov` (overvoltage) · `oc` (overcurrent) ·
`ot` (over-temperature) · `comms` (ACAN timeout).

## Files

- `bench.py` — the engine: physics, IS 17017 state machine, protection, CAN I/O.
- `acan.dbc` — the CAN message database (ACAN-style frames, decoded live).
- `requirements.txt` — dependencies.

## Going to real hardware later

The bus is defined in one place near the top of `bench.py`:

```python
BUSTYPE, CHANNEL = "virtual", "voltbridge"   # no hardware
# BUSTYPE, CHANNEL = "socketcan", "can0"     # ACAN board on Linux (SocketCAN)
# BUSTYPE, CHANNEL = "slcan", "COM5"         # serial CAN adapter on Windows
```

Change that one line and the same code runs against a physical bus — e.g. an
Ather **ACAN** CAN-to-USB bridge on Linux (SocketCAN). Nothing else changes.

## Optional: stream to the dashboard

`python bench.py --ws` (after `pip install websockets`) streams telemetry JSON on
`ws://localhost:8765`, ready for the dashboard to connect to.

## Credit

CAN frame shapes are modeled on Ather Energy's open-source **ACAN** project
(MIT-licensed): https://github.com/AtherEnergy/ACAN. This bench does not include
Ather's firmware or hardware; it reuses the message-level conventions.
