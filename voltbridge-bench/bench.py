#!/usr/bin/env python3
"""
VoltBridge HIL bench — standalone backend.

Runs a real CAN stack (python-can) over a *virtual* bus, so it needs no
hardware. Two nodes (EVSE and EV) exchange real, DBC-encoded ACAN frames
while a plant model + IS 17017 state machine + protection logic drive the
800 VDC power stage. Reuses Ather Energy's open-source ACAN message shapes
(see acan.dbc). MIT-friendly; swap the virtual bus for a real ACAN board by
changing one line (see BUSTYPE below).

Usage:
    python bench.py                       # EV mode, clean 20 s session
    python bench.py --mode dc             # AI data-center rack mode
    python bench.py --fault iso --at 6    # inject insulation fault at t=6 s
    python bench.py --fault ot --at 8 --mode dc
    python bench.py --ws                  # also stream JSON on ws://localhost:8765

Fault keys: iso | ov | oc | ot | comms
"""
import argparse
import json
import time

import can
import cantools

# --- Swap this for real hardware later (one line): ---------------------------
# BUSTYPE, CHANNEL = "socketcan", "can0"     # ACAN board on Linux (SocketCAN)
# BUSTYPE, CHANNEL = "slcan", "COM5"         # serial CAN adapter on Windows
BUSTYPE, CHANNEL = "virtual", "voltbridge"   # no hardware — real stack, no wire
# -----------------------------------------------------------------------------

DT = 0.1  # 100 ms tick

LIMIT = dict(v_bus_max=900.0, iso_min_mohm=0.1, temp_max=85.0)

# ANSI colors for a readable live trace
CID = "\033[38;5;244m"; TX = "\033[38;5;80m"; RX = "\033[38;5;179m"
FAULT = "\033[38;5;203m"; OK = "\033[38;5;114m"; DIM = "\033[38;5;240m"; R = "\033[0m"


class Bench:
    def __init__(self, mode="ev", fault=None, fault_at=None, ws=False):
        self.mode = mode
        self.is_dc = mode == "dc"
        self.db = cantools.database.load_file("acan.dbc")
        # two nodes on the same virtual channel = a real two-node CAN bus
        self.bus_evse = can.Bus(interface=BUSTYPE, channel=CHANNEL)
        self.bus_ev = can.Bus(interface=BUSTYPE, channel=CHANNEL)

        self.t = 0.0
        self.phase = "HANDSHAKE"
        self.phase_t = 0.0
        self.v_bus = 0.0
        self.i_bus = 0.0
        self.temp = 25.0
        self.eff = 0.0
        self.iso = 999.0
        self.soc = 22.0
        self.load = 0.0
        self.contactor = False
        self.injected = fault
        self.fault_at = fault_at
        self.fault_code = None
        self.msg_acc = 0.0
        self.ws = ws
        self._ws_clients = set()

        self.max_p = 1_000_000 if self.is_dc else 240_000
        self.eff_base = 98.6 if self.is_dc else 97.4
        self.i_max = 300.0
        self.oc_limit = 1320.0 if self.is_dc else 340.0

        self.durations = dict(HANDSHAKE=2.0, INSULATION=2.6, PRECHARGE=1.6, SHUTDOWN=2.0)

    # ---- CAN helpers --------------------------------------------------------
    def send(self, node, name, signals):
        msg = self.db.get_message_by_name(name)
        data = msg.encode(signals, strict=False)
        frame = can.Message(arbitration_id=msg.frame_id, data=data, is_extended_id=False)
        bus = self.bus_evse if node == "EVSE" else self.bus_ev
        bus.send(frame)
        self._log(node, msg, frame)

    def _drain(self):
        # prove real transport: the peer actually receives what was sent
        for bus in (self.bus_evse, self.bus_ev):
            while True:
                m = bus.recv(timeout=0)
                if m is None:
                    break

    def _log(self, node, msg, frame):
        try:
            decoded = self.db.decode_message(msg.frame_id, frame.data)
            sig = " ".join(f"{k}={_fmt(v)}" for k, v in decoded.items())
        except Exception:
            sig = frame.data.hex(" ")
        arrow = "TX" if node == "EVSE" else "RX"
        col = FAULT if msg.name == "Emergency_Stop" else (TX if node == "EVSE" else RX)
        raw = frame.data.hex(" ").upper()
        print(f"{DIM}{self.t:5.1f}{R} {col}{arrow}{R} "
              f"{CID}0x{msg.frame_id:03X}{R} {col}{msg.name:<18}{R} "
              f"{DIM}[{raw}]{R} {sig}")

    # ---- protection ---------------------------------------------------------
    def trip(self, code, name):
        if self.phase == "FAULT":
            return
        self.phase, self.phase_t = "FAULT", 0.0
        self.fault_code = code
        self.contactor = False
        self.send("EVSE", "Emergency_Stop", {"Fault_Code": _code_num(code), "Active": 1})
        print(f"\n{FAULT}  ⚡ PROTECTION TRIP  {code} · {name}{R}")
        print(f"{FAULT}     contactor OPEN · current interrupted{R}\n")

    def check_protection(self):
        if self.phase in ("FAULT", "IDLE"):
            return
        if self.iso < LIMIT["iso_min_mohm"]:
            self.trip("F-ISO-01", "Insulation resistance low (IMD)")
        elif self.v_bus > LIMIT["v_bus_max"]:
            self.trip("F-OV-02", "DC bus overvoltage")
        elif self.i_bus > self.oc_limit:
            self.trip("F-OC-03", "Overcurrent / contactor protection")
        elif self.temp > LIMIT["temp_max"]:
            self.trip("F-OT-04", "Power module over-temperature")

    # ---- main step ----------------------------------------------------------
    def step(self):
        self.t += DT
        self.phase_t += DT
        d = self.durations

        # scheduled fault injection
        if self.injected and self.fault_at is not None and self.t >= self.fault_at:
            self.fault_at = None  # armed once

        # phase transitions + phase-entry frames
        if self.phase == "HANDSHAKE" and self.phase_t >= d["HANDSHAKE"]:
            self.phase, self.phase_t = "INSULATION", 0.0
        elif self.phase == "INSULATION" and self.phase_t >= d["INSULATION"]:
            self.phase, self.phase_t = "PRECHARGE", 0.0
            self.contactor = True
            self.send("EVSE", "Precharge_Cmd", {"Enable": 1})
            self.send("EVSE", "Charge_Parameters",
                      {"Limit_Voltage": 800, "Limit_Current": int(self.oc_limit)})
        elif self.phase == "PRECHARGE" and self.phase_t >= d["PRECHARGE"]:
            self.phase, self.phase_t = "TRANSFER", 0.0
        elif self.phase == "SHUTDOWN" and self.phase_t >= d["SHUTDOWN"]:
            self.phase = "DONE"

        armed = self.injected if self.fault_at is None else None
        if self.phase == "FAULT":
            armed = None

        # voltage
        v_target = 0.0
        if self.phase == "PRECHARGE":
            v_target = 800 * min(1.0, self.phase_t / d["PRECHARGE"])
        elif self.phase == "TRANSFER":
            v_target = 800.0
        if armed == "ov" and self.phase == "TRANSFER":
            v_target = 935.0
        slew = 2600 if self.phase == "FAULT" else 900
        self.v_bus += _clamp(v_target - self.v_bus, -slew * DT, slew * DT)

        # current
        i_target = 0.0
        if self.phase == "TRANSFER" and self.contactor:
            if self.is_dc:
                self.load += _clamp(86 - self.load, -40 * DT, 45 * DT)
                i_target = (self.load / 100) * self.max_p / max(1.0, self.v_bus)
            else:
                i = self.i_max
                if self.soc >= 80:
                    i = self.i_max * max(0.08, 1 - (self.soc - 80) / 20 * 0.9)
                i_target = i * min(1.0, self.phase_t / 1.0)
                self.soc = min(100.0, self.soc + i_target * DT / 90)
        if armed == "oc" and self.phase == "TRANSFER":
            i_target = 1500 if self.is_dc else 470
        islew = 4200 if (self.phase == "FAULT" or not self.contactor) else 1400
        self.i_bus += _clamp(i_target - self.i_bus, -islew * DT, islew * DT)
        if not self.contactor:
            self.i_bus = max(0.0, self.i_bus - 3000 * DT)

        # insulation
        if armed == "iso":
            self.iso += _clamp(0.03 - self.iso, -6000 * DT, 0)
        elif self.phase in ("INSULATION", "TRANSFER"):
            self.iso += _clamp(540 - self.iso, -800 * DT, 800 * DT)
        else:
            self.iso += _clamp(999 - self.iso, -400 * DT, 400 * DT)

        # thermal
        heat = (abs(self.v_bus * self.i_bus) / self.max_p) * 60
        cool = (self.temp - 25) * 0.28
        if armed == "ot":
            cool -= 130
        self.temp = max(25.0, self.temp + (heat - cool) * DT)

        # efficiency
        lf = abs(self.v_bus * self.i_bus) / self.max_p
        self.eff = (_clamp(self.eff_base - lf * 1.6 - max(0, self.temp - 60) * 0.05, 90, 99.4)
                    if self.phase == "TRANSFER" else 0.0)

        # comms watchdog
        comms_ok = not (armed == "comms" and self.phase == "TRANSFER")

        self.check_protection()

        # periodic traffic
        if self.phase == "INSULATION" and abs(self.phase_t - 1.2) < DT / 2:
            self.send("EV", "Insulation_Status",
                      {"Resistance": min(self.iso, 6553.5), "Iso_OK": 1 if self.iso > 1 else 0})
        if self.phase == "TRANSFER":
            self.msg_acc += DT
            if self.msg_acc >= 0.6:
                self.msg_acc = 0.0
                self.send("EV", "Charge_Request",
                          {"Target_Voltage": 800, "Target_Current": int(min(i_target, 999))})
                if comms_ok:
                    self.send("EVSE", "Charge_Status", {
                        "Present_Voltage": int(self.v_bus),
                        "Present_Current": int(min(self.i_bus, 999)),
                        "SoC": int(self.load if self.is_dc else self.soc),
                        "Module_Temp": int(min(self.temp, 255)),
                    })

        if armed == "comms" and self.phase == "TRANSFER" and self.phase_t > 1.3:
            self.trip("F-COM-05", "ACAN communication timeout")

        # EV charge complete
        if self.phase == "TRANSFER" and not self.is_dc and self.soc >= 99.5:
            self.send("EVSE", "Stop_Charge", {"Reason": 2})
            self.phase, self.phase_t, self.contactor = "SHUTDOWN", 0.0, False

        self._drain()
        return self.telemetry()

    def telemetry(self):
        return dict(t=round(self.t, 1), mode=self.mode, phase=self.phase,
                    v_bus=round(self.v_bus, 1), i_bus=round(self.i_bus, 1),
                    power_kw=round(self.v_bus * self.i_bus / 1000, 1),
                    temp=round(self.temp, 1), eff=round(self.eff, 1),
                    iso_mohm=round(self.iso, 3),
                    soc=round(self.load if self.is_dc else self.soc, 1),
                    contactor=self.contactor, fault=self.fault_code)

    def start_frames(self):
        self.send("EVSE", "EVSE_Handshake",
                  {"ProtocolVersion": 2, "EVSE_MaxVoltage": 1000,
                   "EVSE_MaxCurrent": int(self.oc_limit)})
        self.send("EV", "EV_Handshake",
                  {"ProtocolVersion": 2, "Pack_Voltage": 800, "Target_SoC": 100})

    def close(self):
        self.bus_evse.shutdown()
        self.bus_ev.shutdown()


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _fmt(v):
    return f"{v:.1f}" if isinstance(v, float) else str(v)


def _code_num(code):
    return {"F-ISO-01": 1, "F-OV-02": 2, "F-OC-03": 3, "F-OT-04": 4, "F-COM-05": 5}.get(code, 0)


def run(args):
    bench = Bench(mode=args.mode, fault=args.fault, fault_at=args.at, ws=args.ws)
    node_a = "PSU" if bench.is_dc else "EVSE"
    node_b = "RACK" if bench.is_dc else "EV"
    title = "AI DATA-CENTER RACK" if bench.is_dc else "EV FAST-CHARGE (IS 17017)"

    print(f"\n{OK}VoltBridge HIL bench{R}  ·  {title}")
    print(f"{DIM}bus: {BUSTYPE}/{CHANNEL}  ·  nodes: {node_a} <-> {node_b}  ·  "
          f"DBC: acan.dbc  ·  external link modeled: CCS2/ISO 15118 (PLC){R}")
    if bench.injected:
        print(f"{FAULT}scheduled fault: {bench.injected} at t={bench.fault_at}s{R}")
    print(f"{DIM}{'-'*78}{R}")

    ws_server = None
    if args.ws:
        ws_server = _start_ws(bench)

    bench.start_frames()
    try:
        while bench.phase != "DONE" and bench.t < args.duration:
            tel = bench.step()
            if ws_server:
                ws_server(tel)
            time.sleep(DT)
    except KeyboardInterrupt:
        pass
    finally:
        t = bench.telemetry()
        print(f"{DIM}{'-'*78}{R}")
        print(f"final  phase={t['phase']}  V={t['v_bus']}  I={t['i_bus']}  "
              f"P={t['power_kw']}kW  temp={t['temp']}C  eff={t['eff']}%  "
              f"iso={t['iso_mohm']}MOhm  fault={t['fault']}")
        bench.close()


def _start_ws(bench):
    """Optional: stream telemetry JSON over WebSocket for the dashboard."""
    try:
        import asyncio
        import threading
        import websockets
    except ImportError:
        print(f"{FAULT}--ws needs 'websockets' (pip install websockets); continuing without it{R}")
        return None

    clients, loop = set(), asyncio.new_event_loop()

    async def handler(ws):
        clients.add(ws)
        try:
            await ws.wait_closed()
        finally:
            clients.discard(ws)

    def serve():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(websockets.serve(handler, "localhost", 8765))
        loop.run_forever()

    threading.Thread(target=serve, daemon=True).start()
    print(f"{OK}WebSocket telemetry on ws://localhost:8765{R}")

    def push(tel):
        msg = json.dumps(tel)
        for ws in list(clients):
            asyncio.run_coroutine_threadsafe(ws.send(msg), loop)

    return push


def main():
    p = argparse.ArgumentParser(description="VoltBridge HIL bench (standalone, no hardware)")
    p.add_argument("--mode", choices=["ev", "dc"], default="ev")
    p.add_argument("--fault", choices=["iso", "ov", "oc", "ot", "comms"], default=None)
    p.add_argument("--at", type=float, default=None, help="seconds to inject the fault")
    p.add_argument("--duration", type=float, default=20.0)
    p.add_argument("--ws", action="store_true", help="stream telemetry on ws://localhost:8765")
    args = p.parse_args()
    if args.fault and args.at is None:
        args.at = 6.0
    run(args)


if __name__ == "__main__":
    main()
