"""
Real Modbus stack for the battery / energy-storage system, built on pymodbus.

  * BatteryModbusServer - a real Modbus-TCP server (the BESS) exposing input
    registers (telemetry) and holding registers (setpoints), running in a thread.
  * EMSClient - a real Modbus-TCP client (the facility EMS / UPS controller) that
    polls telemetry and can issue FC06 setpoint writes.

Transport is a loopback TCP socket (127.0.0.1) - real Modbus framing on the wire.
Swap the host/port for a field device (or ModbusSerialClient for RS-485) and the
register map is unchanged.

Input register map (read, FC04), address 30001+:
    0  State of charge          %          (unsigned)
    1  Pack voltage             V x10       (unsigned)
    2  Pack current             A x10       (signed: + discharge / - charge)
    3  Cell temperature         degC x10    (signed)
    4  Storage power            kW          (signed: + discharge / - charge)
    5  Alarm flags              bitfield    (bit0 = buffering, bit1 = fault)
Holding register map (read/write, FC03/FC06), address 40001+:
    0  Power limit setpoint     kW          (EMS -> BESS)
"""

import threading
import time

try:
    from pymodbus.datastore import (ModbusServerContext, ModbusSlaveContext,
                                    ModbusSequentialDataBlock)
    from pymodbus.server import StartTcpServer, ServerStop
    from pymodbus.client import ModbusTcpClient
    HAVE_PYMODBUS = True
except Exception:
    HAVE_PYMODBUS = False

HOST, PORT = "127.0.0.1", 5020
IR, HR = 4, 3  # function codes: input regs, holding regs


def _u16(v):
    """Encode a possibly-signed integer into an unsigned 16-bit register."""
    v = int(round(v))
    return v & 0xFFFF


def _s16(v):
    """Decode an unsigned 16-bit register as signed."""
    return v - 0x10000 if v >= 0x8000 else v


class BatteryModbusServer:
    def __init__(self, host=HOST, port=PORT):
        if not HAVE_PYMODBUS:
            raise RuntimeError("pymodbus not installed")
        self.host, self.port = host, port
        self.store = ModbusSlaveContext(
            ir=ModbusSequentialDataBlock(0, [0] * 16),
            hr=ModbusSequentialDataBlock(0, [900] + [0] * 15),  # default power limit 900 kW
        )
        self.ctx = ModbusServerContext(slaves=self.store, single=True)
        self._thread = None

    def start(self, timeout=3.0):
        self._thread = threading.Thread(
            target=lambda: StartTcpServer(context=self.ctx, address=(self.host, self.port)),
            daemon=True)
        self._thread.start()
        # wait until the port accepts a connection
        deadline = time.time() + timeout
        import socket
        while time.time() < deadline:
            try:
                s = socket.create_connection((self.host, self.port), timeout=0.3)
                s.close()
                return True
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("Modbus server did not start")

    def publish(self, soc, voltage, current, temp, storage_kw, alarm):
        self.store.setValues(IR, 0, [
            _u16(soc), _u16(voltage * 10), _u16(current * 10),
            _u16(temp * 10), _u16(storage_kw), _u16(alarm),
        ])

    def power_limit(self):
        return self.store.getValues(HR, 0, 1)[0]

    def stop(self):
        try:
            ServerStop()
        except Exception:
            pass


class EMSClient:
    def __init__(self, host=HOST, port=PORT):
        if not HAVE_PYMODBUS:
            raise RuntimeError("pymodbus not installed")
        self.client = ModbusTcpClient(host, port=port)

    def connect(self):
        return self.client.connect()

    def read_battery(self):
        rr = self.client.read_input_registers(0, 6, slave=1)
        if rr.isError():
            raise RuntimeError("Modbus read error")
        r = rr.registers
        return {
            "soc": r[0],
            "voltage": r[1] / 10.0,
            "current": _s16(r[2]) / 10.0,
            "temp": _s16(r[3]) / 10.0,
            "storage_kw": _s16(r[4]),
            "alarm": r[5],
            "raw": r,
        }

    def set_power_limit(self, kw):
        """EMS issues an FC06 write to the BESS power-limit setpoint."""
        wr = self.client.write_register(0, _u16(kw), slave=1)
        return not wr.isError()

    def close(self):
        self.client.close()
