"""
Test suite for the VoltBridge protocol stacks.  Run:  python test_protocols.py
Covers LINEAR codecs, SMBus PEC (incl. tamper detection), PMBus master/device
transactions, and a real Modbus server<->client round-trip with signed values
and an FC06 write.
"""
import sys
from pmbus_stack import (linear11_encode, linear11_decode, linear16_encode,
                         linear16_decode, crc8_pec, VirtualSMBus, PMBusDevice,
                         PMBusMaster, CMD, STATUS, PMBusError)

passed = failed = 0
def check(name, cond):
    global passed, failed
    if cond: passed += 1; print(f"  PASS  {name}")
    else: failed += 1; print(f"  FAIL  {name}")

def nack_detected(m):
    try: m.read_vout(0x77); return False
    except PMBusError: return True

def tamper_detected(bus, m):
    orig = bus.read_word_pec
    def mitm(addr, cmd):
        raw, data, pec = orig(addr, cmd)
        return raw, [data[0] ^ 0xFF, data[1]], pec   # flip a data byte, keep PEC
    bus.read_word_pec = mitm
    try:
        m.read_vout(0x42); ok = False
    except PMBusError:
        ok = True
    bus.read_word_pec = orig
    return ok

print("LINEAR codecs")
check("LINEAR11 800 -> 0x0320", linear11_encode(800) == 0x0320)
check("LINEAR11 round-trip 352", abs(linear11_decode(linear11_encode(352)) - 352) < 0.5)
check("LINEAR11 round-trip 1.056M", abs(linear11_decode(linear11_encode(1056000)) - 1056000)/1056000 < 0.01)
check("LINEAR16 VOUT 800 round-trip", abs(linear16_decode(linear16_encode(800)) - 800) < 0.5)

print("SMBus PEC (CRC-8)")
check("crc8 deterministic", crc8_pec([0x84,0x8B,0x85,0x40,0x06]) == crc8_pec([0x84,0x8B,0x85,0x40,0x06]))
check("crc8 changes with data", crc8_pec([0x84,0x8B,0x85,0x40,0x06]) != crc8_pec([0x84,0x8B,0x85,0x41,0x06]))

print("PMBus master/device transactions")
bus = VirtualSMBus(); dev = PMBusDevice(0x42,"rectifier"); bus.attach(dev)
m = PMBusMaster(bus)
dev.set_telemetry(vout=800, iout=352, pout=1056000, temp=61.0, vin=415)
check("READ_VOUT == 800", abs(m.read_vout(0x42)[0] - 800) < 0.5)
check("READ_IOUT == 352", abs(m.read_iout(0x42)[0] - 352) < 1)
check("READ_POUT ~ 1.056 MW", abs(m.read_pout(0x42)[0] - 1056000)/1056000 < 0.01)
check("READ_TEMP == 61", abs(m.read_temperature(0x42)[0] - 61) < 1)
check("NACK on missing device", nack_detected(m))

print("STATUS_WORD fault bits")
dev.assert_status("TEMPERATURE")
check("temp fault bit set", bool(m.read_status_word(0x42)[0] & STATUS["TEMPERATURE"]))
dev.clear_status()
check("status clears", m.read_status_word(0x42)[0] == 0)

print("VOUT_COMMAND write + PEC tamper")
m.write_vout(0x42, 810)
check("write_vout applied", abs(dev.vout - 810) < 0.5)
check("corrupted data byte rejected", tamper_detected(bus, m))

print("Modbus round-trip (real pymodbus TCP)")
try:
    from modbus_stack import BatteryModbusServer, EMSClient
    srv = BatteryModbusServer(port=5099); srv.start()
    ems = EMSClient(port=5099); ems.connect()
    srv.publish(soc=80, voltage=800, current=0, temp=27.4, storage_kw=0, alarm=0)
    b = ems.read_battery()
    check("read SoC 80", b["soc"] == 80)
    check("read voltage 800", abs(b["voltage"] - 800) < 0.1)
    srv.publish(soc=79, voltage=800, current=-40, temp=30, storage_kw=-40, alarm=0)
    b = ems.read_battery()
    check("signed current -40 A", abs(b["current"] + 40) < 0.1)
    check("signed storage -40 kW", b["storage_kw"] == -40)
    ems.set_power_limit(750)
    check("FC06 write readback 750", srv.power_limit() == 750)
    ems.close(); srv.stop()
except Exception as e:
    check(f"pymodbus available ({e})", False)

print("EV / CAN stack (python-can + acan.dbc)")
try:
    import can, cantools
    db = cantools.database.load_file("acan.dbc")
    names = {m.name for m in db.messages}
    check("acan.dbc has EVSE_Handshake", "EVSE_Handshake" in names)
    check("acan.dbc has Charge_Status", "Charge_Status" in names)
    msg = db.get_message_by_name("EVSE_Handshake")
    data = msg.encode({"ProtocolVersion": 2, "EVSE_MaxVoltage": 1000, "EVSE_MaxCurrent": 340}, strict=False)
    dec = db.decode_message(msg.frame_id, data)
    check("DBC encode/decode round-trip", dec["EVSE_MaxVoltage"] == 1000 and dec["EVSE_MaxCurrent"] == 340)
    from bench import Bench, BUSTYPE, CHANNEL
    b = Bench(mode="ev")
    mon = can.Bus(interface=BUSTYPE, channel=CHANNEL)
    b.send("EVSE", "EVSE_Handshake", {"ProtocolVersion": 2, "EVSE_MaxVoltage": 1000, "EVSE_MaxCurrent": 340})
    frame = mon.recv(timeout=1.0)
    check("frame transmitted on CAN bus", frame is not None)
    if frame is not None:
        d = db.decode_message(frame.arbitration_id, frame.data)
        check("received CAN frame decodes (1000 V)", d["EVSE_MaxVoltage"] == 1000)
    mon.shutdown(); b.close()
except Exception as e:
    check(f"EV/CAN stack ({e})", False)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
