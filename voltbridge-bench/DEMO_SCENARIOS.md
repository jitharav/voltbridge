# VoltBridge — Demo-Day Use Cases

Real, runnable scenarios that exercise the actual protocol stacks. Each is a
single command; narrate the log as it scrolls. Run the test suite first to prove
the stacks are real:

```
python test_protocols.py        # 20 checks: LINEAR codecs, PEC, PMBus, Modbus
```

---

## UC-1 — Nominal rack telemetry (PMBus + Modbus, live)
```
python bench.py --mode dc --duration 30
```
**Show:** the rectifier answering real PMBus reads (`READ_VOUT` in LINEAR16 →
`40 06` = 800 V, `READ_IOUT`/`READ_POUT`/`READ_TEMPERATURE_1` in LINEAR11), each
line **PEC-checked**; per-tray DC-DC polled `@0x50…0x57`; and the EMS reading the
battery over **real Modbus-TCP** (`FC04 30001+6`).
**Say:** "Three real stacks — CAN on the EV side, PMBus on the power silicon,
Modbus on the battery. The PEC column means every PMBus frame is checksum-verified."

## UC-2 — EMS commands a power limit (bidirectional Modbus)
Watch the startup of transfer in any DC run:
```
python bench.py --mode dc --duration 20
```
**Show:** `MODBUS … FC06 write 40001 = 900 kW (EMS power limit) ACK` — the EMS
client writing a setpoint the BESS server accepts.
**Say:** "It's not just polling — the EMS writes back. That's a real FC06
transaction the battery controller acknowledges."

## UC-3 — Load transient buffered by storage (Modbus telemetry)
```
python bench.py --mode dc --duration 40
```
**Show:** during a training-step transient, the Modbus read shows
`Storage=264…296 kW  alarm=1` while the grid draw stays flat.
**Say:** "The GPU step load hits; the battery discharges ~290 kW to absorb it —
you can read it live over Modbus. That's the 800 VDC-plus-storage thesis."

## UC-4 — Over-temperature protection trip (PMBus STATUS_WORD)
```
python bench.py --mode dc --fault ot --at 10 --duration 13
```
**Show:** temperature climbs in the PMBus `READ_TEMPERATURE_1` reads, then
`PMBus @0x42 STATUS_WORD (0x79) [04 00] … 0x0004` asserts the OT bit, Modbus
`Alarm_Flags set`, and `PROTECTION TRIP F-OT-04`.
**Say:** "The fault surfaces exactly as it would on real hardware — a PMBus
status word with the temperature bit set — and the protection reacts in software."

## UC-5 — Other faults
```
python bench.py --mode dc --fault ov --at 10 --duration 13   # overvoltage  (VOUT bit)
python bench.py --mode dc --fault oc --at 10 --duration 13   # overcurrent  (IOUT_OC bit)
```

## UC-6 — EV side is genuinely CAN (contrast)
```
python bench.py --duration 8
python bench.py --fault iso --at 6 --duration 10
```
**Say:** "Switch to EV and it's a real CAN stack — python-can decoding a DBC.
Different domain, different native protocol, same bench."

## UC-7 — Connected dashboard (the showstopper)
Terminal 1:
```
pip install websockets
python bench.py --mode dc --ws --duration 120
```
Terminal 2:
```
npm run dev          # in the dashboard folder; switch to AI Data Center, press START
```
The dashboard's RACK BUS panel mirrors the PMBus/Modbus traffic and the NVIDIA
panel animates from the bench's live telemetry.

---

### Robustness notes for demo day
- Modbus binds loopback `127.0.0.1:5020`; if it can't (or `pymodbus` is missing)
  the bench auto-falls back and PMBus stays real. Force it with `--sim-protocols`.
- Run `python test_protocols.py` in front of the judges if they doubt it's real —
  20 green checks including PEC tamper detection.
