#!/usr/bin/env python3
"""
VoltBridge HIL bench - standalone backend (no hardware).

Runs a real CAN stack (python-can) over a *virtual* bus. Two modes share one
800 VDC power stage and one internal control/protection spine (ACAN):

  EV mode  - EV DC fast-charge modeled per IS 17017 (acan.dbc).
  DC mode  - AI data-center rack on the NVIDIA 800 VDC architecture
             (datacenter.dbc): AC/DC rectifier -> 800 VDC bus -> per-tray
             DC-DC -> GPU trays, with synchronized load transients buffered
             by rack energy storage, and live efficiency vs a 54 V baseline.

Usage:
    python bench.py                          # EV, clean 20 s session
    python bench.py --mode dc                # NVIDIA 800VDC rack
    python bench.py --mode dc --duration 30  # watch several load transients
    python bench.py --fault iso --at 6       # insulation trip (EV)
    python bench.py --mode dc --fault ot --at 10
    python bench.py --mode dc --ws           # stream telemetry to the dashboard

Fault keys: iso | ov | oc | ot | comms
"""
import argparse
import json
import time

import can
import cantools

from dc_stack import DCStack

# --- Swap for real hardware later (one line): --------------------------------
# BUSTYPE, CHANNEL = "socketcan", "can0"     # ACAN board on Linux (SocketCAN)
# BUSTYPE, CHANNEL = "slcan", "COM5"         # serial CAN adapter on Windows
BUSTYPE, CHANNEL = "virtual", "voltbridge"   # no hardware - real stack, no wire
# -----------------------------------------------------------------------------

DT = 0.1
LIMIT = dict(v_bus_max=900.0, iso_min_mohm=0.1, temp_max=85.0)

# NVIDIA-style 800VDC rack config
N_TRAYS = 8
P_TRAY_NOM = 132.0             # kW per compute tray  -> ~1.06 MW rack
RECT_EFF, DIST_EFF, DCDC_EFF = 0.985, 0.995, 0.975   # 800VDC chain stages
BASELINE_54V_EFF = 90.5       # legacy 54V end-to-end (%)
STORAGE_CAP_KWH = 5.0         # rack-level buffer
TRANSIENT_PERIOD = 4.0        # s between synchronized training-step bursts
TRANSIENT_DUR = 0.6           # s each burst lasts
TRANSIENT_GAIN = 0.28         # +28% load during a burst

CID = "\033[38;5;244m"; TX = "\033[38;5;80m"; RX = "\033[38;5;179m"
FAULT = "\033[38;5;203m"; OK = "\033[38;5;114m"; DIM = "\033[38;5;240m"; R = "\033[0m"


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


class Bench:
    def __init__(self, mode="ev", fault=None, fault_at=None, ws=False, sim_proto=False):
        self.mode = mode
        self.is_dc = mode == "dc"
        self._sim_proto = sim_proto
        self.frame_buf = []   # protocol frame records streamed to the dashboard
        if self.is_dc:
            # data center: real PMBus (power) + real Modbus (battery)
            self.db = None
            self.bus_a = self.bus_b = None
            self.dc = DCStack(n_trays=N_TRAYS, log=True,
                              want_modbus_tcp=not sim_proto,
                              frame_sink=self._emit_frame)
            self._ems_limit_sent = False
        else:
            # EV: real python-can stack + DBC
            self.db = cantools.database.load_file("acan.dbc")
            self.bus_a = can.Bus(interface=BUSTYPE, channel=CHANNEL)   # EVSE side
            self.bus_b = can.Bus(interface=BUSTYPE, channel=CHANNEL)   # EV side
            self.pmbus = self.modbus = None

        self.t = 0.0
        self.phase = "HANDSHAKE"
        self.phase_t = 0.0
        self.v_bus = 0.0
        self.i_bus = 0.0
        self.temp = 25.0
        self.eff = 0.0
        self.iso = 999.0
        self.contactor = False
        self.injected = fault
        self.fault_at = fault_at
        self.fault_code = None
        self.msg_acc = 0.0
        self.ws = ws

        # EV state
        self.soc = 22.0
        # DC state
        self.trays = [0.0] * N_TRAYS      # per-tray load %
        self.rack_power = 0.0             # kW (tray side)
        self.grid_power = 0.0             # kW (rectifier/grid side)
        self.storage_soc = 80.0          # %
        self.storage_power = 0.0         # kW  (+ discharge, - charge)
        self.buffering = False
        self.e2e_eff = 0.0
        self.loss_kw = 0.0
        self.rect_temp = 25.0
        self._tray_idx = 0

        self.i_max = 300.0
        self.oc_limit = 1600.0 if self.is_dc else 340.0
        self.durations = dict(HANDSHAKE=2.0, INSULATION=2.6, PRECHARGE=1.6, SHUTDOWN=2.0)

    # ---- CAN -----------------------------------------------------------------
    def send(self, node, name, signals):
        msg = self.db.get_message_by_name(name)
        data = msg.encode(signals, strict=False)
        frame = can.Message(arbitration_id=msg.frame_id, data=data, is_extended_id=False)
        (self.bus_a if node in ("EVSE", "PSU", "BMS") else self.bus_b).send(frame)
        self._log(node, msg, frame)

    def _drain(self):
        if self.is_dc:
            return
        for bus in (self.bus_a, self.bus_b):
            while bus.recv(timeout=0) is not None:
                pass

    def _emit_frame(self, rec):
        rec.setdefault("t", round(self.t, 1))
        self.frame_buf.append(rec)
        if len(self.frame_buf) > 80:
            self.frame_buf = self.frame_buf[-80:]

    def _log(self, node, msg, frame):
        try:
            decoded = self.db.decode_message(msg.frame_id, frame.data)
            sig = " ".join(f"{k}={_fmt(v)}" for k, v in decoded.items())
        except Exception:
            sig = frame.data.hex(" ")
        up = node in ("EVSE", "PSU", "BMS")
        arrow = "TX" if up else "RX"
        col = FAULT if msg.name == "Emergency_Stop" else (TX if up else RX)
        raw = frame.data.hex(" ").upper()
        print(f"{DIM}{self.t:5.1f}{R} {col}{arrow}{R} {CID}0x{msg.frame_id:03X}{R} "
              f"{col}{msg.name:<18}{R} {DIM}[{raw}]{R} {sig}")
        self._emit_frame({"proto": "CAN", "id": f"0x{msg.frame_id:03X}",
                          "name": msg.name, "data": raw, "dir": "tx" if up else "rx"})

    # ---- protection ----------------------------------------------------------
    def trip(self, code, name):
        if self.phase == "FAULT":
            return
        self.phase, self.phase_t = "FAULT", 0.0
        self.fault_code = code
        self.contactor = False
        if self.is_dc:
            self.dc.fault(self.t, code, name, self.injected or "")
        else:
            self.send("EVSE", "Emergency_Stop",
                      {"Fault_Code": _code_num(code), "Active": 1})
        print(f"\n{FAULT}  PROTECTION TRIP  {code} - {name}{R}")
        print(f"{FAULT}     contactor OPEN - power interrupted{R}\n")

    def check_protection(self):
        if self.phase in ("FAULT", "IDLE"):
            return
        if self.iso < LIMIT["iso_min_mohm"]:
            self.trip("F-ISO-01", "Insulation resistance low (IMD)")
        elif self.v_bus > LIMIT["v_bus_max"]:
            self.trip("F-OV-02", "DC bus overvoltage")
        elif self.i_bus > self.oc_limit:
            self.trip("F-OC-03", "Overcurrent / bus protection")
        elif self.temp > LIMIT["temp_max"]:
            self.trip("F-OT-04", "Power module over-temperature")

    # ---- main step -----------------------------------------------------------
    def step(self):
        self.t += DT
        self.phase_t += DT
        d = self.durations

        if self.injected and self.fault_at is not None and self.t >= self.fault_at:
            self.fault_at = None
        armed = self.injected if self.fault_at is None else None
        if self.phase == "FAULT":
            armed = None

        # phase transitions (shared)
        if self.phase == "HANDSHAKE" and self.phase_t >= d["HANDSHAKE"]:
            self.phase, self.phase_t = "INSULATION", 0.0
        elif self.phase == "INSULATION" and self.phase_t >= d["INSULATION"]:
            self.phase, self.phase_t = "PRECHARGE", 0.0
            self.contactor = True
            self._on_precharge()
        elif self.phase == "PRECHARGE" and self.phase_t >= d["PRECHARGE"]:
            self.phase, self.phase_t = "TRANSFER", 0.0
        elif self.phase == "SHUTDOWN" and self.phase_t >= d["SHUTDOWN"]:
            self.phase = "DONE"

        # bus voltage (shared): ramp on precharge, hold 800 on transfer
        v_target = 0.0
        if self.phase == "PRECHARGE":
            v_target = 800 * min(1.0, self.phase_t / d["PRECHARGE"])
        elif self.phase == "TRANSFER":
            v_target = 800.0
        if armed == "ov" and self.phase == "TRANSFER":
            v_target = 935.0
        slew = 2600 if self.phase == "FAULT" else 900
        self.v_bus += _clamp(v_target - self.v_bus, -slew * DT, slew * DT)

        # mode-specific physics + frames
        if self.is_dc:
            self._step_dc(armed)
        else:
            self._step_ev(armed)

        # insulation monitor (shared)
        if armed == "iso":
            self.iso += _clamp(0.03 - self.iso, -6000 * DT, 0)
        elif self.phase in ("INSULATION", "TRANSFER"):
            self.iso += _clamp(540 - self.iso, -800 * DT, 800 * DT)
        else:
            self.iso += _clamp(999 - self.iso, -400 * DT, 400 * DT)

        self.check_protection()
        self._drain()
        return self.telemetry()

    # ---- EV path (IS 17017) --------------------------------------------------
    def _on_precharge_ev(self):
        self.send("EVSE", "Precharge_Cmd", {"Enable": 1})
        self.send("EVSE", "Charge_Parameters",
                  {"Limit_Voltage": 800, "Limit_Current": int(self.oc_limit)})

    def _step_ev(self, armed):
        i_target = 0.0
        if self.phase == "TRANSFER" and self.contactor:
            i = self.i_max
            if self.soc >= 80:
                i = self.i_max * max(0.08, 1 - (self.soc - 80) / 20 * 0.9)
            i_target = i * min(1.0, self.phase_t / 1.0)
            self.soc = min(100.0, self.soc + i_target * DT / 90)
        if armed == "oc" and self.phase == "TRANSFER":
            i_target = 470
        islew = 4200 if (self.phase == "FAULT" or not self.contactor) else 1400
        self.i_bus += _clamp(i_target - self.i_bus, -islew * DT, islew * DT)
        if not self.contactor:
            self.i_bus = max(0.0, self.i_bus - 3000 * DT)

        heat = (abs(self.v_bus * self.i_bus) / 240_000) * 60
        cool = (self.temp - 25) * 0.28 - (130 if armed == "ot" else 0)
        self.temp = max(25.0, self.temp + (heat - cool) * DT)
        lf = abs(self.v_bus * self.i_bus) / 240_000
        self.eff = (_clamp(97.4 - lf * 1.6 - max(0, self.temp - 60) * 0.05, 90, 99.4)
                    if self.phase == "TRANSFER" else 0.0)

        comms_ok = not (armed == "comms" and self.phase == "TRANSFER")
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
                        "SoC": int(self.soc), "Module_Temp": int(min(self.temp, 255))})
        if armed == "comms" and self.phase == "TRANSFER" and self.phase_t > 1.3:
            self.trip("F-COM-05", "ACAN communication timeout")
        if self.phase == "TRANSFER" and self.soc >= 99.5:
            self.send("EVSE", "Stop_Charge", {"Reason": 2})
            self.phase, self.phase_t, self.contactor = "SHUTDOWN", 0.0, False

    # ---- DC path (NVIDIA 800VDC rack) ---------------------------------------
    def _on_precharge_dc(self):
        self.dc.precharge(self.t, self.v_bus)

    def _in_transient(self):
        if self.phase != "TRANSFER" or self.phase_t < 1.5:
            return False
        return (self.t % TRANSIENT_PERIOD) < TRANSIENT_DUR

    def _step_dc(self, armed):
        base = 0.0
        if self.phase == "TRANSFER" and self.contactor:
            base = 100 * min(1.0, self.phase_t / 1.5)
        spike = TRANSIENT_GAIN * 100 if self._in_transient() else 0.0
        self.buffering = spike > 0 and self.storage_soc > 10

        for k in range(N_TRAYS):
            target = _clamp(base + spike, 0, 130)
            self.trays[k] += _clamp(target - self.trays[k], -250 * DT, 250 * DT)

        avg_load = sum(self.trays) / N_TRAYS
        self.rack_power = N_TRAYS * (avg_load / 100.0) * P_TRAY_NOM
        nominal = N_TRAYS * (min(base, 100) / 100.0) * P_TRAY_NOM
        spike_delta = max(0.0, self.rack_power - nominal)

        # energy storage buffers transients so the grid draw stays flat
        if self.buffering and spike_delta > 0:
            self.storage_power = spike_delta                # discharge to rack
            self.grid_power = nominal
            self.storage_soc = max(0.0, self.storage_soc - spike_delta * DT / (STORAGE_CAP_KWH * 36))
        elif self.storage_soc < 80 and self.phase == "TRANSFER":
            charge = min(40.0, (80 - self.storage_soc) * 4)
            self.storage_power = -charge                    # recharge
            self.grid_power = self.rack_power + charge
            self.storage_soc = min(80.0, self.storage_soc + charge * DT / (STORAGE_CAP_KWH * 36))
        else:
            self.storage_power = 0.0
            self.grid_power = self.rack_power

        if armed == "oc" and self.phase == "TRANSFER":
            self.grid_power = 1500  # forced overcurrent event

        # bus current is measured at the rectifier (grid side) - storage flattens it
        i_target = self.grid_power * 1000 / max(1.0, self.v_bus) if self.phase in ("TRANSFER",) else 0.0
        islew = 4200 if (self.phase == "FAULT" or not self.contactor) else 2000
        self.i_bus += _clamp(i_target - self.i_bus, -islew * DT, islew * DT)
        if not self.contactor:
            self.i_bus = max(0.0, self.i_bus - 6000 * DT)

        # thermal (rectifier + representative module temp)
        lf = self.grid_power / (N_TRAYS * P_TRAY_NOM)
        heat = lf * 40
        cool = (self.temp - 25) * 1.0 - (150 if armed == "ot" else 0)
        self.temp = max(25.0, self.temp + (heat - cool) * DT)
        self.rect_temp = 25 + lf * 38

        # efficiency: 800VDC chain vs 54V baseline
        if self.phase == "TRANSFER":
            chain = RECT_EFF * DIST_EFF * DCDC_EFF * 100
            self.e2e_eff = _clamp(chain - max(0, self.temp - 60) * 0.04, 90, 99)
            self.eff = self.e2e_eff
            self.loss_kw = self.rack_power * (1 - self.e2e_eff / 100)
        else:
            self.e2e_eff = self.eff = self.loss_kw = 0.0

        # Protocol telemetry: real PMBus (power) + real Modbus (battery)
        if self.phase == "TRANSFER":
            if not self._ems_limit_sent and self.phase_t > 0.4:
                self.dc.ems_set_limit(self.t, 900)   # EMS FC06 setpoint at power-up
                self._ems_limit_sent = True
            self.msg_acc += DT
            if self.msg_acc >= 0.5:
                self.msg_acc = 0.0
                self._tray_idx = (self._tray_idx + 1) % N_TRAYS
                k = self._tray_idx
                self.dc.telemetry(
                    self.t, self.v_bus, self.i_bus, self.rack_power * 1000,
                    self.rect_temp, self.storage_soc, self.storage_power,
                    1 if self.buffering else 0, (self.trays[k] / 100) * P_TRAY_NOM)

    # ---- shared entry points -------------------------------------------------
    def _on_precharge(self):
        self._on_precharge_dc() if self.is_dc else self._on_precharge_ev()

    def start_frames(self):
        if self.is_dc:
            self.dc.startup(self.t)
        else:
            self.send("EVSE", "EVSE_Handshake",
                      {"ProtocolVersion": 2, "EVSE_MaxVoltage": 1000, "EVSE_MaxCurrent": int(self.oc_limit)})
            self.send("EV", "EV_Handshake",
                      {"ProtocolVersion": 2, "Pack_Voltage": 800, "Target_SoC": 100})

    def telemetry(self):
        t = dict(t=round(self.t, 1), mode=self.mode, phase=self.phase,
                 v_bus=round(self.v_bus, 1), i_bus=round(self.i_bus, 1),
                 power_kw=round(self.v_bus * self.i_bus / 1000, 1),
                 temp=round(self.temp, 1), eff=round(self.eff, 1),
                 iso_mohm=round(self.iso, 3), contactor=self.contactor, fault=self.fault_code)
        if self.is_dc:
            t.update(rack_power_kw=round(self.rack_power, 1),
                     grid_power_kw=round(self.grid_power, 1),
                     trays=[round((l / 100) * P_TRAY_NOM, 1) for l in self.trays],
                     n_trays=N_TRAYS, e2e_eff=round(self.e2e_eff, 2),
                     baseline_eff=BASELINE_54V_EFF,
                     eff_gain=round(self.e2e_eff - BASELINE_54V_EFF, 2),
                     loss_kw=round(self.loss_kw, 1),
                     storage_soc=round(self.storage_soc, 1),
                     storage_power=round(self.storage_power, 1),
                     buffering=self.buffering, rect_temp=round(self.rect_temp, 1))
        else:
            t.update(soc=round(self.soc, 1))
        if self.frame_buf:
            t["frames"] = self.frame_buf
            self.frame_buf = []
        return t

    def close(self):
        if self.is_dc:
            self.dc.close()
        else:
            self.bus_a.shutdown()
            self.bus_b.shutdown()


def _fmt(v):
    return f"{v:.1f}" if isinstance(v, float) else str(v)


def _code_num(code):
    return {"F-ISO-01": 1, "F-OV-02": 2, "F-OC-03": 3, "F-OT-04": 4, "F-COM-05": 5}.get(code, 0)


def run(args):
    b = Bench(mode=args.mode, fault=args.fault, fault_at=args.at, ws=args.ws,
              sim_proto=args.sim_protocols)
    if b.is_dc:
        title = "AI DATA-CENTER RACK - NVIDIA 800VDC ARCHITECTURE"
        info = f"chain: grid AC -> rectifier -> 800VDC bus -> DC-DC -> {N_TRAYS} GPU trays (~{N_TRAYS*P_TRAY_NOM:.0f} kW)"
    else:
        title = "EV FAST-CHARGE - IS 17017"
        info = "external link modeled: CCS2 / ISO 15118 (PLC)"
    print(f"\n{OK}VoltBridge HIL bench{R}  -  {title}")
    if b.is_dc:
        mb = "real Modbus-TCP (pymodbus)" if b.dc.modbus_real else "Modbus emitter (fallback)"
        print(f"{DIM}protocols: real PMBus/SMBus (power, PEC-checked) + {mb} (battery){R}")
    else:
        print(f"{DIM}bus: {BUSTYPE}/{CHANNEL}  -  real CAN stack (python-can)  -  DBC: acan.dbc{R}")
    print(f"{DIM}{info}{R}")
    if b.injected:
        print(f"{FAULT}scheduled fault: {b.injected} at t={b.fault_at}s{R}")
    print(f"{DIM}{'-'*82}{R}")

    push = _start_ws(b) if args.ws else None
    b.start_frames()
    try:
        while b.phase != "DONE" and b.t < args.duration:
            tel = b.step()
            if push:
                push(tel)
            time.sleep(DT)
    except KeyboardInterrupt:
        pass
    finally:
        t = b.telemetry()
        print(f"{DIM}{'-'*82}{R}")
        if b.is_dc:
            print(f"final  phase={t['phase']}  bus={t['v_bus']}V  rack={t.get('rack_power_kw')}kW  "
                  f"grid={t.get('grid_power_kw')}kW  e2e={t.get('e2e_eff')}%  "
                  f"(+{t.get('eff_gain')}% vs 54V)  storage={t.get('storage_soc')}%  fault={t['fault']}")
        else:
            print(f"final  phase={t['phase']}  V={t['v_bus']}  I={t['i_bus']}  P={t['power_kw']}kW  "
                  f"eff={t['eff']}%  fault={t['fault']}")
        b.close()


def _start_ws(bench):
    try:
        import asyncio
        import threading
        import websockets
    except ImportError:
        print(f"{FAULT}--ws needs 'websockets' (pip install websockets); continuing without it{R}")
        return None

    clients = set()
    loop = asyncio.new_event_loop()

    async def handler(ws):
        clients.add(ws)
        try:
            await ws.wait_closed()
        finally:
            clients.discard(ws)

    async def _main():
        async with websockets.serve(handler, "localhost", 8765):
            await asyncio.Future()  # serve forever

    def serve():
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_main())
        except Exception as e:
            print(f"{FAULT}WS server stopped: {e}{R}")

    threading.Thread(target=serve, daemon=True).start()
    time.sleep(0.3)  # let the loop come up before first push
    print(f"{OK}WebSocket telemetry on ws://localhost:8765{R}")

    def push(tel):
        msg = json.dumps(tel)
        for ws in list(clients):
            try:
                asyncio.run_coroutine_threadsafe(ws.send(msg), loop)
            except Exception:
                pass
    return push


def main():
    p = argparse.ArgumentParser(description="VoltBridge HIL bench (standalone, no hardware)")
    p.add_argument("--mode", choices=["ev", "dc"], default="ev")
    p.add_argument("--fault", choices=["iso", "ov", "oc", "ot", "comms"], default=None)
    p.add_argument("--at", type=float, default=None)
    p.add_argument("--duration", type=float, default=20.0)
    p.add_argument("--ws", action="store_true")
    p.add_argument("--sim-protocols", action="store_true",
                   help="use lightweight in-process Modbus emitter instead of real pymodbus TCP")
    args = p.parse_args()
    if args.fault and args.at is None:
        args.at = 6.0
    run(args)


if __name__ == "__main__":
    main()
