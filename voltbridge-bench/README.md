# VoltBridge HIL Bench — standalone backend

A software hardware-in-the-loop bench for an **800 VDC** power stage. No hardware
required. Two modes share one 800 VDC power stage, each running its domain's
**real protocol stack over a virtual transport**:

- **EV mode** — DC fast-charge modeled per IS 17017, over a **real CAN stack**
  (`python-can` + `acan.dbc`).
- **DC mode** — AI data-center rack (NVIDIA 800 VDC), with a **real PMBus/SMBus
  stack** on the power components and a **real Modbus stack** (`pymodbus`) on the
  battery / BESS.

## Install & run (Windows / macOS / Linux)

Python 3.9+.

```bash
pip install -r requirements.txt
python bench.py                       # EV mode (real CAN)
python bench.py --mode dc             # data-center mode (real PMBus + Modbus)
python bench.py --mode dc --fault ot --at 10
python bench.py --mode dc --ws        # + stream telemetry to the dashboard
python test_protocols.py              # run the protocol test suite (20 checks)
```

Fault keys: `iso` `ov` `oc` `ot` `comms`.

## Protocols — one real stack per domain

| Subsystem | Protocol | Implementation |
|---|---|---|
| EV charging (internal) | **CAN** | `python-can` + `acan.dbc` — real library |
| DC power (rectifier, DC-DC, trays) | **PMBus / SMBus** | `pmbus_stack.py` — command codes, LINEAR11/16, **CRC-8 PEC**, master↔device transactions |
| DC battery / energy storage | **Modbus-TCP** | `modbus_stack.py` — real `pymodbus` server (BESS) + client (EMS), FC04 reads + FC06 writes |

All three are **real protocol stacks over virtual transports** (virtual CAN bus,
in-memory SMBus, loopback TCP). To go to hardware, swap the transport: a CAN
adapter/ACAN board, an I²C/SMBus adapter (e.g. `smbus2`), and RS-485/TCP for
Modbus — the stack code above is unchanged.

**PMBus PEC:** every PMBus read is checksum-verified (SMBus Packet Error
Checking, CRC-8). Corrupted frames raise `PMBusError` — see the test suite.

**Modbus (bidirectional):** the EMS client polls battery telemetry (FC04 input
registers) *and* issues a power-limit setpoint to the BESS (FC06 write).

## Ports

- Modbus-TCP server binds `127.0.0.1:5020` (loopback only — no admin, no LAN exposure).
- `--ws` telemetry binds `ws://localhost:8765`.

If `pymodbus` is missing or the port can't bind, the bench prints a note and
falls back to a lightweight in-process Modbus emitter (PMBus stays real). You can
force the fallback with `--sim-protocols`.

## Dashboard streaming (purist mode)

With `--ws`, the telemetry payload includes a `frames` array carrying the bench's
**actual protocol transactions** (CAN / PMBus / Modbus records — real bytes). When
the dashboard connects, it shows "LIVE" and renders *these real frames* in its log
instead of its own local simulation, becoming a true viewer of the bench's
protocol traffic. Disconnected, it falls back to a representative local sim.

## Files

- `bench.py` — engine: physics, state machines, protection, protocol I/O.
- `acan.dbc` — EV / IS 17017 CAN message database.
- `pmbus_stack.py` — real PMBus/SMBus stack (codecs, PEC, master/device).
- `modbus_stack.py` — real Modbus stack (battery server + EMS client) via pymodbus.
- `dc_stack.py` — wires PMBus + Modbus into the bench, with logging + fallback.
- `dc_protocols.py` — lightweight Modbus emitter used only as fallback.
- `test_protocols.py` — 20-check test suite.
- `DEMO_SCENARIOS.md` — scripted demo-day use cases.
- `requirements.txt` — dependencies.

## Credit

CAN conventions modeled on Ather Energy's open-source ACAN (MIT):
https://github.com/AtherEnergy/ACAN — interface conventions only, not their firmware.
