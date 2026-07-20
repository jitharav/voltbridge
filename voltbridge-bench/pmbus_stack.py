"""
Real PMBus / SMBus protocol stack over a virtual bus.

This is a genuine protocol implementation, not a display formatter:

  * PMBus command codes (READ_VOUT, READ_IOUT, READ_POUT, READ_TEMPERATURE_1,
    STATUS_WORD, VOUT_COMMAND, OPERATION ...)
  * LINEAR11 encoding for currents/power/temperature
  * LINEAR16 (with VOUT_MODE exponent) for output voltage
  * SMBus Packet Error Checking (PEC) -- real CRC-8 over the transaction bytes
  * Master <-> device request/response transaction model, addressed by device

Transport is a virtual in-memory SMBus (no physical I2C), the same philosophy
as python-can's virtual CAN bus. Swap VirtualSMBus for a real I2C/SMBus adapter
(e.g. smbus2 on a Raspberry Pi) and the master/device code is unchanged.
"""

# ----------------------------- command codes -----------------------------
CMD = {
    "OPERATION": 0x01, "VOUT_COMMAND": 0x21, "VOUT_MODE": 0x20,
    "STATUS_WORD": 0x79, "READ_VIN": 0x88, "READ_IIN": 0x89,
    "READ_VOUT": 0x8B, "READ_IOUT": 0x8C, "READ_TEMPERATURE_1": 0x8D,
    "READ_POUT": 0x96, "READ_PIN": 0x97,
}
NAME = {v: k for k, v in CMD.items()}

# STATUS_WORD fault bits (subset of the PMBus spec)
STATUS = {
    "VOUT": 1 << 15, "IOUT_OC": 1 << 14, "VIN_UV": 1 << 13,
    "TEMPERATURE": 1 << 2, "CML": 1 << 1, "NONE_OF_ABOVE": 1 << 0,
    "OFF": 1 << 6,
}

VOUT_MODE_EXP = -1  # LINEAR16 exponent for VOUT (0.5 V resolution)


# ----------------------------- codecs -----------------------------
def linear11_encode(value):
    best = None
    for n in range(-16, 16):
        y = round(value / (2.0 ** n))
        if -1024 <= y <= 1023:
            err = abs(y * (2.0 ** n) - value)
            if best is None or err < best[0]:
                best = (err, y, n)
    _, y, n = best
    return (((n & 0x1F) << 11) | (y & 0x7FF)) & 0xFFFF


def linear11_decode(raw):
    n = raw >> 11
    if n > 15:
        n -= 32
    y = raw & 0x7FF
    if y > 1023:
        y -= 2048
    return y * (2.0 ** n)


def linear16_encode(value, exp=VOUT_MODE_EXP):
    m = round(value / (2.0 ** exp))
    return max(0, min(0xFFFF, m))


def linear16_decode(raw, exp=VOUT_MODE_EXP):
    return raw * (2.0 ** exp)


def crc8_pec(data):
    """SMBus PEC: CRC-8, polynomial 0x07, init 0x00."""
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if (crc & 0x80) else (crc << 1) & 0xFF
    return crc


class PMBusError(Exception):
    pass


# ----------------------------- device (slave) -----------------------------
class PMBusDevice:
    """A PMBus power device (rectifier, DC-DC ...). Holds telemetry, answers reads."""

    def __init__(self, address, name="device"):
        self.address = address
        self.name = name
        self.vout = 0.0
        self.iout = 0.0
        self.pout = 0.0
        self.temp = 25.0
        self.vin = 0.0
        self.status = 0
        self.operation = 0x80  # on

    def set_telemetry(self, vout=None, iout=None, pout=None, temp=None, vin=None):
        if vout is not None: self.vout = vout
        if iout is not None: self.iout = iout
        if pout is not None: self.pout = pout
        if temp is not None: self.temp = temp
        if vin is not None: self.vin = vin

    def assert_status(self, bit_name):
        self.status |= STATUS.get(bit_name, STATUS["NONE_OF_ABOVE"])

    def clear_status(self):
        self.status = 0

    def read_word(self, command):
        """Return the raw 16-bit value for a command (device side of a read)."""
        if command == CMD["READ_VOUT"]:
            return linear16_encode(self.vout)
        if command == CMD["READ_IOUT"]:
            return linear11_encode(self.iout)
        if command == CMD["READ_POUT"]:
            return linear11_encode(self.pout)
        if command == CMD["READ_TEMPERATURE_1"]:
            return linear11_encode(self.temp)
        if command == CMD["READ_VIN"]:
            return linear11_encode(self.vin)
        if command == CMD["STATUS_WORD"]:
            return self.status & 0xFFFF
        raise PMBusError(f"unsupported read command 0x{command:02X}")

    def write_word(self, command, raw):
        if command == CMD["VOUT_COMMAND"]:
            self.vout = linear16_decode(raw)
        elif command == CMD["OPERATION"]:
            self.operation = raw & 0xFF
        else:
            raise PMBusError(f"unsupported write command 0x{command:02X}")


# ----------------------------- virtual SMBus -----------------------------
class VirtualSMBus:
    """In-memory SMBus. Routes transactions to devices by address, adds PEC."""

    def __init__(self):
        self.devices = {}

    def attach(self, device):
        self.devices[device.address] = device

    def _dev(self, addr):
        d = self.devices.get(addr)
        if d is None:
            raise PMBusError(f"no device at 0x{addr:02X} (NACK)")
        return d

    def read_word_pec(self, addr, command):
        """SMBus Read Word with PEC. Returns (raw16, data_bytes, pec)."""
        d = self._dev(addr)
        raw = d.read_word(command)
        lo, hi = raw & 0xFF, (raw >> 8) & 0xFF
        pec = crc8_pec([(addr << 1) | 0, command, (addr << 1) | 1, lo, hi])
        return raw, [lo, hi], pec

    def write_word_pec(self, addr, command, raw):
        d = self._dev(addr)
        d.write_word(command, raw)
        return crc8_pec([(addr << 1) | 0, command, raw & 0xFF, (raw >> 8) & 0xFF])


# ----------------------------- master -----------------------------
class PMBusMaster:
    """PMBus master (the BMC / power-manager). Issues transactions, checks PEC."""

    def __init__(self, bus, verify_pec=True):
        self.bus = bus
        self.verify_pec = verify_pec

    def _read(self, addr, command, decode):
        raw, data, pec = self.bus.read_word_pec(addr, command)
        if self.verify_pec:
            expect = crc8_pec([(addr << 1) | 0, command, (addr << 1) | 1] + data)
            if pec != expect:
                raise PMBusError(f"PEC mismatch on 0x{command:02X}")
        return decode(raw), data, pec

    def read_vout(self, addr):        return self._read(addr, CMD["READ_VOUT"], linear16_decode)
    def read_iout(self, addr):        return self._read(addr, CMD["READ_IOUT"], linear11_decode)
    def read_pout(self, addr):        return self._read(addr, CMD["READ_POUT"], linear11_decode)
    def read_temperature(self, addr): return self._read(addr, CMD["READ_TEMPERATURE_1"], linear11_decode)
    def read_vin(self, addr):         return self._read(addr, CMD["READ_VIN"], linear11_decode)
    def read_status_word(self, addr): return self._read(addr, CMD["STATUS_WORD"], lambda r: r)

    def write_vout(self, addr, volts):
        return self.bus.write_word_pec(addr, CMD["VOUT_COMMAND"], linear16_encode(volts))
