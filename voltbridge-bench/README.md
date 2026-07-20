# VoltBridge HIL Bench — standalone backend

A software hardware-in-the-loop bench for an **800 VDC** power stage. It needs
**no hardware**. Two modes share one 800 VDC power stage:

- **EV mode** — EV DC fast-charge modeled per IS 17017, over a **real CAN stack**
  (`python-can` + `acan.dbc`).
- **DC mode** — AI data-center rack on the NVIDIA 800 VDC architecture, speaking the
  protocols a real rack actually uses: **PMBus** on the power components and
  **Modbus-RTU** on the battery.

Companion to the VoltBridge dashboard; runs independently.

## Install & run (Windows / macOS / Linux)

Requires Python 3.9+.

```bash
pip install -r requirements.txt
python bench.py                       # EV mode, clean session
python bench.py --mode dc             # NVIDIA 800VDC AI-rack mode
python bench.py --mode dc --duration 30
python bench.py --fault iso --at 6    # insulation fault (EV)
python bench.py --mode dc --fault ot --at 10
python bench.py --mode dc --ws        # stream telemetry to the dashboard
```

Fault keys: `iso` `ov` `oc` `ot` `comms`.

## Protocols — one bus per domain (as real hardware is wired)

| Subsystem | Protocol | Notes |
|---|---|---|
| EV charging (internal) | **CAN** | real `python-can` stack + `acan.dbc` |
| DC power components (rectifier, DC-DC) | **PMBus** | real command codes (READ_VOUT/IOUT/POUT...) + LINEAR11 encoding |
| DC battery / energy storage | **Modbus-RTU** | function code 0x04 input registers + CRC-16 |

EV mode runs a **real CAN stack**. The data-center protocols are **protocol-accurate
modeled transactions** (correct framing and encoding) over a virtual transport —
the same philosophy as the virtual CAN bus. In production the transport becomes
physical: CAN over a real adapter, PMBus over I2C, Modbus over RS-485/TCP.

## Files

- `bench.py` — the engine: physics, state machines, protection, protocol I/O.
- `acan.dbc` — EV / IS 17017 CAN message database.
- `dc_protocols.py` — PMBus (power) + Modbus (battery) emitters for the rack.
- `requirements.txt` — dependencies.

## Optional: stream to the dashboard

`python bench.py --mode dc --ws` (after `pip install websockets`) streams telemetry
JSON on `ws://localhost:8765` for the dashboard to display live.

## Credit

CAN conventions are modeled on Ather Energy's open-source ACAN project (MIT):
https://github.com/AtherEnergy/ACAN. This bench reuses interface conventions, not
Ather's firmware or hardware.
