#!/usr/bin/env python3
"""
VoltBridge OCPP gateway  (EV side)
==================================
A SECOND subscriber on the MQTT telemetry bus, mirroring the Redfish gateway on
the DC side. It subscribes to `voltbridge/telemetry` (bench.py --mqtt, EV mode)
and reports the charger to a CSMS (Charging Station Management System) using
OCPP 1.6-J — the de facto global standard for charger <-> backend communication
(EU AFIR, US NEVI both mandate it).

This shows the convergence story: one bench, two management standards —
Redfish for the data-center rack, OCPP for the EV charger — both fed from the
same stream, zero changes to the bench.

Architecture:
    bench --MQTT--> broker --MQTT--> ocpp_gateway (charge point) --OCPP/WS--> CSMS

Run (with broker + bench --mqtt + a CSMS already running):
    pip install paho-mqtt websockets
    python ocpp_csms.py          # in one window: a minimal CSMS to watch
    python ocpp_gateway.py       # in another: the charge point

SCOPE (honest): this implements a representative OCPP 1.6-J subset
(BootNotification, StatusNotification, MeterValues, Heartbeat). A production
charge point also implements the full transaction/auth/smart-charging message
set and OCPP security profiles. Representative gateway, not a certified stack.
"""
import argparse
import json
import threading
from datetime import datetime, timezone

TELEMETRY_TOPIC = "voltbridge/telemetry"

_lock = threading.Lock()
_latest = {}
_count = 0


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------- pure OCPP message builders (unit-testable, no network) ----------

def ocpp_status(phase):
    """Map bench lifecycle phase -> OCPP 1.6 connector status."""
    return {
        "IDLE": "Available",
        "HANDSHAKE": "Preparing",
        "INSULATION": "Preparing",
        "PRECHARGE": "Preparing",
        "TRANSFER": "Charging",
        "COMPLETE": "Finishing",
        "SHUTDOWN": "Finishing",
        "FAULT": "Faulted",
    }.get(phase, "Available")


def boot_notification_payload():
    return {"chargePointVendor": "VoltBridge", "chargePointModel": "HIL-Bench-EV"}


def status_notification_payload(status, error_code="NoError"):
    return {"connectorId": 1, "status": status, "errorCode": error_code, "timestamp": _now_iso()}


def meter_values_payload(t, ts=None):
    ts = ts or _now_iso()
    sv = []

    def add(measurand, value, unit):
        if value is not None:
            sv.append({"measurand": measurand, "value": f"{value}", "unit": unit})

    add("Voltage", t.get("v_bus"), "V")
    add("Current.Import", t.get("i_bus"), "A")
    add("Power.Active.Import", t.get("power_kw"), "kW")
    add("SoC", t.get("soc"), "Percent")
    return {"connectorId": 1, "transactionId": 1,
            "meterValue": [{"timestamp": ts, "sampledValue": sv}]}


def call_frame(uid, action, payload):
    """OCPP-J CALL message: [MessageTypeId=2, UniqueId, Action, Payload]."""
    return [2, uid, action, payload]


# ---------- MQTT subscriber ----------

def _start_mqtt(broker):
    import paho.mqtt.client as mqtt
    host, _, port = broker.partition(":")
    port = int(port or 1883)
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
    except (AttributeError, TypeError):
        client = mqtt.Client()

    def on_connect(c, u, flags, rc):
        c.subscribe(TELEMETRY_TOPIC)
        print(f"[mqtt] connected to {broker}, subscribed to {TELEMETRY_TOPIC}")

    def on_message(c, u, msg):
        global _count
        try:
            data = json.loads(msg.payload.decode())
        except Exception:
            return
        with _lock:
            _latest.clear()
            _latest.update(data)
            _count += 1

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(host, port, keepalive=30)
    client.loop_start()
    return client


# ---------- OCPP charge-point client ----------

async def _call(ws, uid, action, payload):
    await ws.send(json.dumps(call_frame(uid, action, payload)))
    print(f"[CP] -> {action}: {json.dumps(payload)}")
    try:
        import asyncio
        await asyncio.wait_for(ws.recv(), timeout=5)  # consume the CALLRESULT
    except Exception:
        pass


async def run_ocpp(csms_url):
    import asyncio
    import websockets
    uid = [0]

    def nid():
        uid[0] += 1
        return str(uid[0])

    while True:
        try:
            async with websockets.connect(csms_url, subprotocols=["ocpp1.6"]) as ws:
                print(f"[CP] connected to CSMS {csms_url}")
                await _call(ws, nid(), "BootNotification", boot_notification_payload())
                last_status = None
                hb = 0
                while True:
                    with _lock:
                        t = dict(_latest)
                        n = _count
                    if n > 0:
                        status = ocpp_status(t.get("phase", "IDLE"))
                        if status != last_status:
                            await _call(ws, nid(), "StatusNotification",
                                        status_notification_payload(
                                            status,
                                            "OtherError" if status == "Faulted" else "NoError"))
                            last_status = status
                        if t.get("phase") == "TRANSFER":
                            await _call(ws, nid(), "MeterValues", meter_values_payload(t))
                    hb += 1
                    if hb % 10 == 0:
                        await _call(ws, nid(), "Heartbeat", {})
                    await asyncio.sleep(1)
        except Exception as e:
            print(f"[CP] CSMS connection issue: {e}; retrying in 3s")
            await asyncio.sleep(3)


def main():
    ap = argparse.ArgumentParser(description="VoltBridge OCPP gateway (MQTT subscriber -> OCPP charge point)")
    ap.add_argument("--broker", default="localhost:1883", help="MQTT broker host:port (default localhost:1883)")
    ap.add_argument("--csms", default="ws://localhost:9000/CP1", help="CSMS OCPP WebSocket URL (default ws://localhost:9000/CP1)")
    args = ap.parse_args()

    try:
        _start_mqtt(args.broker)
    except Exception as e:
        print(f"[mqtt] connect to {args.broker} failed: {e}")
        print("       start the broker and bench (--mqtt) first, then rerun.")
        return

    import asyncio
    print(f"[CP] OCPP 1.6-J charge point; reporting to CSMS at {args.csms}")
    try:
        asyncio.run(run_ocpp(args.csms))
    except KeyboardInterrupt:
        print("\nshutting down")


if __name__ == "__main__":
    main()
