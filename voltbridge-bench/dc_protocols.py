"""
Data-center protocol layer for the VoltBridge bench.

Two protocol-accurate emitters, modeled over a virtual transport:

  PMBus  - power-conversion components (rectifier, DC-DC, rack power).
           Real PMBus command codes + LINEAR11 data encoding.
  Modbus - battery / energy-storage system (BESS).
           Real Modbus-RTU input-register reads (FC 0x04) with CRC-16.

This mirrors how a real 800 VDC rack is wired: PMBus on the power silicon,
Modbus at the battery-system boundary. Transport is virtual (no I2C / RS-485),
same philosophy as the virtual CAN bus used for EV mode.
"""

# ---- colors (match bench.py) ----
CID = "\033[38;5;244m"; TXP = "\033[38;5;222m"; TXM = "\033[38;5;115m"
DIM = "\033[38;5;240m"; FAULT = "\033[38;5;203m"; R = "\033[0m"


# ============================ PMBus ============================
# Real PMBus command codes (subset)
PMBUS_CMD = {
    0x88: "READ_VIN", 0x89: "READ_IIN", 0x8B: "READ_VOUT", 0x8C: "READ_IOUT",
    0x8D: "READ_TEMPERATURE_1", 0x96: "READ_POUT", 0x97: "READ_PIN",
    0x79: "STATUS_WORD",
}


def linear11_encode(value):
    """PMBus LINEAR11: value = Y * 2^N, Y = 11-bit signed, N = 5-bit signed."""
    best = None
    for n in range(-16, 16):
        y = round(value / (2 ** n))
        if -1024 <= y <= 1023:
            err = abs(y * (2 ** n) - value)
            if best is None or err < best[0]:
                best = (err, y, n)
    _, y, n = best
    raw = ((n & 0x1F) << 11) | (y & 0x7FF)
    return raw & 0xFFFF


def linear11_decode(raw):
    n = raw >> 11
    if n > 15:
        n -= 32
    y = raw & 0x7FF
    if y > 1023:
        y -= 2048
    return y * (2 ** n)


class PMBus:
    """Emits PMBus command/response transactions from power components."""

    def __init__(self, log=True):
        self.log = log

    def read(self, t, addr, cmd_code, value, unit=""):
        raw = linear11_encode(value)
        lo, hi = raw & 0xFF, (raw >> 8) & 0xFF
        name = PMBUS_CMD.get(cmd_code, f"CMD_{cmd_code:02X}")
        if self.log:
            print(f"{DIM}{t:5.1f}{R} {TXP}PMBus{R} {CID}@0x{addr:02X}{R} "
                  f"{TXP}{name:<18}{R} {DIM}(0x{cmd_code:02X}) [LIN11 {hi:02X} {lo:02X}]{R} "
                  f"-> {linear11_decode(raw):.1f} {unit}")

    def status(self, t, addr, word, label):
        if self.log:
            print(f"{DIM}{t:5.1f}{R} {FAULT}PMBus{R} {CID}@0x{addr:02X}{R} "
                  f"{FAULT}STATUS_WORD{R} {DIM}(0x79) [{(word>>8)&0xFF:02X} {word&0xFF:02X}]{R} "
                  f"-> {FAULT}{label}{R}")


# ============================ Modbus ============================
def _crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if (crc & 1) else (crc >> 1)
    return crc & 0xFFFF


# Battery-system input-register map (FC 0x04), addresses are 0-based offsets
MODBUS_REG = {
    0: ("SoC", "%", 1),
    1: ("Pack_Voltage", "V", 0.1),
    2: ("Pack_Current", "A", 0.1),
    3: ("Cell_Temp", "degC", 0.1),
    4: ("Storage_Power", "kW", 1),
    5: ("Alarm_Flags", "", 1),
}


class Modbus:
    """Emits Modbus-RTU input-register reads from the battery/BESS."""

    def __init__(self, slave=0x01, log=True):
        self.slave = slave
        self.log = log

    def read_input_regs(self, t, start, values):
        """FC 0x04 read: build a real RTU response frame with CRC-16."""
        count = len(values)
        raw16 = []
        for i, v in enumerate(values):
            scale = MODBUS_REG.get(start + i, ("", "", 1))[2]
            reg = int(round(v / scale)) & 0xFFFF
            raw16.append(reg)
        body = bytes([self.slave, 0x04, count * 2])
        for reg in raw16:
            body += bytes([(reg >> 8) & 0xFF, reg & 0xFF])
        crc = _crc16_modbus(body)
        frame = body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])
        if self.log:
            names = ", ".join(
                f"{MODBUS_REG.get(start+i,('reg','',1))[0]}={values[i]:g}"
                f"{MODBUS_REG.get(start+i,('','',1))[1]}"
                for i in range(count))
            reg_addr = 30001 + start
            print(f"{DIM}{t:5.1f}{R} {TXM}MODBUS{R} {CID}slave=0x{self.slave:02X}{R} "
                  f"{TXM}FC04 read {reg_addr}+{count}{R} "
                  f"{DIM}[{frame.hex(' ').upper()}]{R} -> {names}")

    def alarm(self, t, label):
        if self.log:
            print(f"{DIM}{t:5.1f}{R} {FAULT}MODBUS{R} {CID}slave=0x{self.slave:02X}{R} "
                  f"{FAULT}Alarm_Flags set{R} -> {FAULT}{label}{R}")
