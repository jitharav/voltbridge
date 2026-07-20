"""
Data-center protocol controller for the bench.

Wires together the real PMBus stack (power components) and the real Modbus stack
(battery / BESS), performs genuine master<->device transactions each cycle, logs
them, and falls back gracefully to lightweight emitters if pymodbus is absent.

    EV  -> real CAN            (python-can, in bench.py)
    DC  -> real PMBus + Modbus (this module)
"""

from pmbus_stack import (VirtualSMBus, PMBusDevice, PMBusMaster, CMD, STATUS,
                         linear16_encode, linear11_encode)

CID = "\033[38;5;244m"; TXP = "\033[38;5;222m"; TXM = "\033[38;5;115m"
DIM = "\033[38;5;240m"; FAULT = "\033[38;5;203m"; OK = "\033[38;5;114m"; R = "\033[0m"

RECT = 0x42            # rectifier / rack power stage
TRAY0 = 0x50           # first GPU-tray DC-DC


class DCStack:
    def __init__(self, n_trays=8, log=True, want_modbus_tcp=True, frame_sink=None):
        self.log = log
        self.sink = frame_sink
        # ---- PMBus: real virtual-SMBus with a rectifier + per-tray devices ----
        self.smbus = VirtualSMBus()
        self.rect = PMBusDevice(RECT, "rectifier")
        self.smbus.attach(self.rect)
        self.trays = []
        for k in range(n_trays):
            d = PMBusDevice(TRAY0 + k, f"tray{k}")
            self.smbus.attach(d)
            self.trays.append(d)
        self.pm = PMBusMaster(self.smbus, verify_pec=True)
        self._tray = 0

        # ---- Modbus: real pymodbus server+client, or fallback emitter ----
        self.modbus_real = False
        self.srv = self.ems = None
        self.fallback = None
        if want_modbus_tcp:
            try:
                from modbus_stack import BatteryModbusServer, EMSClient
                self.srv = BatteryModbusServer()
                self.srv.start()
                self.ems = EMSClient()
                self.ems.connect()
                self.modbus_real = True
            except Exception as e:
                self._note(f"Modbus TCP unavailable ({e}); using in-process emitter")
        if not self.modbus_real:
            from dc_protocols import Modbus
            self.fallback = Modbus(slave=0x01)

    # ---------------- logging helpers ----------------
    def _emit(self, proto, id_, name, data, dir="tx"):
        if self.sink:
            self.sink({"proto": proto, "id": id_, "name": name, "data": data, "dir": dir})

    def _note(self, msg):
        if self.log:
            print(f"{DIM}  note: {msg}{R}")

    def _pm_log(self, t, addr, cmd, value, data, pec, unit):
        name = {v: k for k, v in CMD.items()}.get(cmd, f"CMD_{cmd:02X}")
        bytes_s = " ".join(f"{b:02X}" for b in data)
        self._emit("PMBus", f"@0x{addr:02X}", name, bytes_s)
        if self.log:
            print(f"{DIM}{t:5.1f}{R} {TXP}PMBus{R} {CID}@0x{addr:02X}{R} {TXP}{name:<18}{R} "
                  f"{DIM}(0x{cmd:02X}) [{bytes_s}] PEC {pec:02X}{R} -> {value:.1f} {unit}")

    def _mb_log(self, t, text, fault=False, frame=None):
        if frame:
            self._emit("MODBUS", "uid=1", frame[0], frame[1], "tx")
        if not self.log:
            return
        col = FAULT if fault else TXM
        print(f"{DIM}{t:5.1f}{R} {col}MODBUS{R} {CID}slave=0x01{R} {col}{text}{R}")

    # ---------------- lifecycle ----------------
    def startup(self, t):
        v, d, p = self.pm.read_vin(RECT)
        self._pm_log(t, RECT, CMD["READ_VIN"], v, d, p, "V")

    def precharge(self, t, vout):
        self.rect.set_telemetry(vout=vout)
        v, d, p = self.pm.read_vout(RECT)
        self._pm_log(t, RECT, CMD["READ_VOUT"], v, d, p, "V")

    def ems_set_limit(self, t, kw):
        if self.modbus_real:
            ok = self.ems.set_power_limit(kw)
            self._mb_log(t, f"FC06 write 40001 = {kw} kW (EMS power limit) {'ACK' if ok else 'ERR'}",
                         frame=("FC06 40001", f"{kw} kW"))
        else:
            self._mb_log(t, f"FC06 write 40001 = {kw} kW (EMS power limit)",
                         frame=("FC06 40001", f"{kw} kW"))

    # ---------------- per-cycle telemetry ----------------
    def telemetry(self, t, vout, iout, pout, temp, soc, storage_kw, alarm, tray_load_kw):
        # ---- PMBus: real reads on the rectifier (with PEC) ----
        self.rect.set_telemetry(vout=vout, iout=iout, pout=pout, temp=temp)
        for cmd, decfn, unit in (
            (CMD["READ_VOUT"], self.pm.read_vout, "V"),
            (CMD["READ_IOUT"], self.pm.read_iout, "A"),
            (CMD["READ_POUT"], self.pm.read_pout, "W"),
            (CMD["READ_TEMPERATURE_1"], self.pm.read_temperature, "degC"),
        ):
            val, data, pec = decfn(RECT)
            self._pm_log(t, RECT, cmd, val, data, pec, unit)
        # ---- PMBus: one GPU-tray DC-DC per cycle ----
        k = self._tray % len(self.trays)
        self._tray += 1
        self.trays[k].set_telemetry(pout=tray_load_kw * 1000)
        val, data, pec = self.pm.read_pout(TRAY0 + k)
        self._pm_log(t, TRAY0 + k, CMD["READ_POUT"], val, data, pec, "W")

        # ---- Modbus: publish battery telemetry, EMS reads it back ----
        if self.modbus_real:
            cur = storage_kw * 1000.0 / max(1.0, vout)
            self.srv.publish(soc=soc, voltage=vout, current=cur, temp=temp,
                             storage_kw=storage_kw, alarm=alarm)
            b = self.ems.read_battery()
            self._mb_log(t, f"FC04 read 30001+6 {b['raw']} -> SoC={b['soc']}% "
                            f"Storage={b['storage_kw']}kW alarm={b['alarm']}",
                         frame=("FC04 30001+6", f"SoC {b['soc']}% {b['storage_kw']}kW"))
        else:
            self.fallback.read_input_regs(t, 0, [
                round(soc, 1), round(vout, 1),
                round(storage_kw * 1000 / max(1, vout), 1),
                round(temp, 1), round(storage_kw, 1), alarm])
            self._emit("MODBUS", "uid=1", "FC04 30001+6", f"SoC {round(soc)}% {round(storage_kw)}kW")

    # ---------------- fault ----------------
    def fault(self, t, code, name, kind):
        bit = {"ov": "VOUT", "oc": "IOUT_OC", "ot": "TEMPERATURE",
               "iso": "CML", "comms": "CML"}.get(kind, "NONE_OF_ABOVE")
        self.rect.assert_status(bit)
        s, data, pec = self.pm.read_status_word(RECT)
        self._emit("PMBus", f"@0x{RECT:02X}", "STATUS_WORD", f"{data[0]:02X} {data[1]:02X}")
        if self.log:
            print(f"{DIM}{t:5.1f}{R} {FAULT}PMBus @0x{RECT:02X} STATUS_WORD (0x79) "
                  f"[{data[0]:02X} {data[1]:02X}] PEC {pec:02X} -> 0x{s:04X} {name}{R}")
        if self.modbus_real:
            self.srv.publish(soc=0, voltage=0, current=0, temp=0, storage_kw=0, alarm=2)
            self.ems.read_battery()
        self._mb_log(t, f"Alarm_Flags set (bit1 fault) -> {code} {name}", fault=True,
                     frame=("Alarm_Flags", "fault"))

    def close(self):
        if self.modbus_real:
            try:
                self.ems.close()
                self.srv.stop()
            except Exception:
                pass
