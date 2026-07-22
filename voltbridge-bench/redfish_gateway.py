#!/usr/bin/env python3
"""
VoltBridge Redfish gateway
==========================
A SECOND subscriber on the MQTT telemetry bus. It subscribes to
`voltbridge/telemetry` (published by bench.py --mqtt) and re-exposes the live
rack data as a Redfish-style management API over HTTP — the DMTF standard that
AI data centers use for power/thermal/storage management.

This demonstrates the pub/sub payoff: the dashboard is one subscriber, this
gateway is another, both fed by the same stream — zero changes to the bench.

SCOPE (honest): this models the READ / monitoring surface of Redfish. A
production BMC also implements authentication (sessions/tokens), event
subscriptions, PATCH control actions and full DMTF schema conformance. This is
a representative gateway, not a certified Redfish service.

Run:
    pip install paho-mqtt          # (already installed for the bench)
    python redfish_gateway.py      # broker localhost:1883, HTTP on :8080

Query (browser or curl):
    http://localhost:8080/redfish/v1/
    http://localhost:8080/redfish/v1/Chassis/Rack1
    http://localhost:8080/redfish/v1/Chassis/Rack1/Power
    http://localhost:8080/redfish/v1/Chassis/Rack1/Thermal
    http://localhost:8080/redfish/v1/Chassis/Rack1/Battery
"""
import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TELEMETRY_TOPIC = "voltbridge/telemetry"

# shared latest telemetry (updated by the MQTT thread, read by HTTP threads)
_lock = threading.Lock()
_latest = {}       # last telemetry dict
_msg_count = 0     # how many telemetry messages we've received


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
        global _msg_count
        try:
            data = json.loads(msg.payload.decode())
        except Exception:
            return
        with _lock:
            _latest.clear()
            _latest.update(data)
            _msg_count += 1

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(host, port, keepalive=30)
    client.loop_start()
    return client


# ---------- Redfish resource builders ----------

def _snapshot():
    with _lock:
        return dict(_latest), _msg_count


def _health(t):
    if t.get("fault"):
        return "Critical"
    if (t.get("temp") or 0) > 80 or (t.get("rect_temp") or 0) > 80:
        return "Warning"
    return "OK"


def _power_state(t):
    phase = t.get("phase", "IDLE")
    on = bool(t.get("contactor")) and phase in ("PRECHARGE", "TRANSFER")
    return "On" if on else "Off"


def res_service_root():
    return {
        "@odata.id": "/redfish/v1/",
        "@odata.type": "#ServiceRoot.v1_15_0.ServiceRoot",
        "Id": "RootService",
        "Name": "VoltBridge Redfish Service (representative)",
        "RedfishVersion": "1.15.0",
        "Chassis": {"@odata.id": "/redfish/v1/Chassis"},
        "Oem": {"VoltBridge": {"Note": "Read/monitoring surface fed from the MQTT telemetry bus"}},
    }


def res_chassis_collection():
    return {
        "@odata.id": "/redfish/v1/Chassis",
        "@odata.type": "#ChassisCollection.ChassisCollection",
        "Name": "Chassis Collection",
        "Members@odata.count": 1,
        "Members": [{"@odata.id": "/redfish/v1/Chassis/Rack1"}],
    }


def res_chassis(t):
    return {
        "@odata.id": "/redfish/v1/Chassis/Rack1",
        "@odata.type": "#Chassis.v1_22_0.Chassis",
        "Id": "Rack1",
        "Name": "AI 800VDC Rack",
        "ChassisType": "RackMount",
        "PowerState": _power_state(t),
        "Status": {"State": "Enabled", "Health": _health(t)},
        "Power": {"@odata.id": "/redfish/v1/Chassis/Rack1/Power"},
        "Thermal": {"@odata.id": "/redfish/v1/Chassis/Rack1/Thermal"},
        "Oem": {"VoltBridge": {
            "@odata.id": "/redfish/v1/Chassis/Rack1/Battery",
            "Phase": t.get("phase"),
            "Mode": t.get("mode"),
            "EndToEndEfficiencyPercent": t.get("e2e_eff"),
            "BaselineEfficiencyPercent": t.get("baseline_eff"),
            "EfficiencyGainPercent": t.get("eff_gain"),
            "Fault": t.get("fault"),
        }},
    }


def res_power(t):
    rack_kw = t.get("rack_power_kw") or t.get("power_kw") or 0
    grid_kw = t.get("grid_power_kw") or rack_kw
    return {
        "@odata.id": "/redfish/v1/Chassis/Rack1/Power",
        "@odata.type": "#Power.v1_7_1.Power",
        "Id": "Power",
        "Name": "Power",
        "PowerControl": [{
            "@odata.id": "/redfish/v1/Chassis/Rack1/Power#/PowerControl/0",
            "MemberId": "0",
            "Name": "Rack Power Control",
            "PowerConsumedWatts": round(rack_kw * 1000),
            "PowerRequestedWatts": round(grid_kw * 1000),
            "PowerLimit": {"LimitInWatts": 900000, "LimitException": "LogEventOnly"},
            "Status": {"State": "Enabled", "Health": _health(t)},
        }],
        "Voltages": [{
            "@odata.id": "/redfish/v1/Chassis/Rack1/Power#/Voltages/0",
            "MemberId": "0",
            "Name": "800VDC Bus",
            "ReadingVolts": t.get("v_bus"),
            "UpperThresholdCritical": 900,
            "Status": {"State": "Enabled", "Health": _health(t)},
        }],
        "Oem": {"VoltBridge": {
            "BusCurrentAmps": t.get("i_bus"),
            "RackPowerkW": rack_kw,
            "GridPowerkW": grid_kw,
            "LosskW": t.get("loss_kw"),
            "TrayPowerkW": t.get("trays"),
            "TrayCount": t.get("n_trays"),
        }},
    }


def res_thermal(t):
    temps = []
    if t.get("rect_temp") is not None:
        temps.append({
            "@odata.id": "/redfish/v1/Chassis/Rack1/Thermal#/Temperatures/0",
            "MemberId": "0", "Name": "Rectifier",
            "ReadingCelsius": t.get("rect_temp"),
            "UpperThresholdCritical": 85,
            "Status": {"State": "Enabled",
                       "Health": "Critical" if (t.get("rect_temp") or 0) > 85 else "OK"},
        })
    if t.get("temp") is not None:
        temps.append({
            "@odata.id": "/redfish/v1/Chassis/Rack1/Thermal#/Temperatures/1",
            "MemberId": "1", "Name": "Power Module",
            "ReadingCelsius": t.get("temp"),
            "UpperThresholdCritical": 85,
            "Status": {"State": "Enabled",
                       "Health": "Critical" if (t.get("temp") or 0) > 85 else "OK"},
        })
    return {
        "@odata.id": "/redfish/v1/Chassis/Rack1/Thermal",
        "@odata.type": "#Thermal.v1_7_1.Thermal",
        "Id": "Thermal",
        "Name": "Thermal",
        "Temperatures": temps,
    }


def res_battery(t):
    soc = t.get("storage_soc")
    sp = t.get("storage_power")
    buffering = bool(t.get("buffering"))
    return {
        "@odata.id": "/redfish/v1/Chassis/Rack1/Battery",
        "@odata.type": "#Battery.v1_2_0.Battery",
        "Id": "Battery",
        "Name": "Rack Energy Storage (BESS)",
        "StateOfChargePercent": soc,
        "Status": {"State": "Enabled", "Health": "Warning" if buffering else "OK"},
        "Oem": {"VoltBridge": {
            "StoragePowerkW": sp,
            "Buffering": buffering,
            "Note": "Positive = discharging to absorb GPU load transients; negative = recharging",
        }},
    }


ROUTES = {
    "/redfish/v1": lambda t: res_service_root(),
    "/redfish/v1/": lambda t: res_service_root(),
    "/redfish/v1/Chassis": lambda t: res_chassis_collection(),
    "/redfish/v1/Chassis/Rack1": res_chassis,
    "/redfish/v1/Chassis/Rack1/Power": res_power,
    "/redfish/v1/Chassis/Rack1/Thermal": res_thermal,
    "/redfish/v1/Chassis/Rack1/Battery": res_battery,
}

LANDING = """<!doctype html><html><head><meta charset=utf-8>
<title>VoltBridge Redfish gateway</title>
<style>body{font-family:monospace;background:#0b0f14;color:#e9eef3;padding:24px}
a{color:#2dd4a7}h1{color:#f2b138}.m{color:#8fa}</style></head><body>
<h1>VoltBridge Redfish gateway</h1>
<p class=m>Second subscriber on the MQTT telemetry bus, re-exposing the rack as a Redfish-style API.</p>
<ul>
<li><a href="/redfish/v1/">/redfish/v1/</a> — service root</li>
<li><a href="/redfish/v1/Chassis/Rack1">/redfish/v1/Chassis/Rack1</a> — chassis + health</li>
<li><a href="/redfish/v1/Chassis/Rack1/Power">/redfish/v1/Chassis/Rack1/Power</a> — voltage, power, limit</li>
<li><a href="/redfish/v1/Chassis/Rack1/Thermal">/redfish/v1/Chassis/Rack1/Thermal</a> — temperatures</li>
<li><a href="/redfish/v1/Chassis/Rack1/Battery">/redfish/v1/Chassis/Rack1/Battery</a> — storage SoC</li>
</ul></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet default logging
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")  # allow browser fetch
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/" or path == "":
            return self._send(200, LANDING, "text/html; charset=utf-8")
        # normalise: allow with/without trailing slash
        key = path if path in ROUTES else path + "/"
        builder = ROUTES.get(path) or ROUTES.get(key)
        if not builder:
            return self._send(404, json.dumps({
                "error": {"code": "Base.1.0.ResourceNotFound",
                          "message": f"No such resource: {path}"}}))
        t, n = _snapshot()
        if n == 0:
            return self._send(503, json.dumps({
                "error": {"code": "Base.1.0.ServiceUnavailable",
                          "message": "No telemetry received yet — is the bench running with --mqtt?"}}))
        obj = builder(t)
        return self._send(200, json.dumps(obj, indent=2))


def main():
    ap = argparse.ArgumentParser(description="VoltBridge Redfish gateway (MQTT subscriber -> Redfish API)")
    ap.add_argument("--broker", default="localhost:1883", help="MQTT broker host:port (default localhost:1883)")
    ap.add_argument("--port", type=int, default=8080, help="HTTP port to serve Redfish on (default 8080)")
    args = ap.parse_args()

    try:
        _start_mqtt(args.broker)
    except Exception as e:
        print(f"[mqtt] connect to {args.broker} failed: {e}")
        print("       start the broker and bench first, then rerun.")
        return

    srv = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"[http] Redfish gateway on http://localhost:{args.port}/redfish/v1/")
    print(f"[http] landing page:      http://localhost:{args.port}/")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")


if __name__ == "__main__":
    main()
