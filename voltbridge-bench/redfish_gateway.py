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
<meta name=viewport content="width=device-width, initial-scale=1">
<title>VoltBridge — Rack Management Console</title>
<style>
:root{--bg:#0b0f14;--panel:#121820;--line:#2a3b4d;--text:#e9eef3;--muted:#a3b4c2;
--gold:#f2b138;--green:#2dd4a7;--amp:#7fd4ff;--volt:#f2b138;--warn:#f0902e;--crit:#ff5470;--power:#b98cff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font-family:ui-monospace,"Cascadia Code",Consolas,monospace;padding:22px}
h1{font-family:system-ui,sans-serif;font-size:22px;margin:0 0 2px;letter-spacing:.5px}
h1 b{color:var(--gold)}
.sub{color:var(--muted);font-size:12px;margin-bottom:16px}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;vertical-align:middle}
.banner{border-radius:10px;padding:12px 16px;font-size:15px;font-weight:700;margin-bottom:16px;
border:1px solid var(--line);letter-spacing:1px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card h2{font-family:system-ui,sans-serif;font-size:11px;letter-spacing:2px;color:var(--muted);
text-transform:uppercase;margin:0 0 10px}
.big{font-size:30px;font-weight:700;font-family:system-ui,sans-serif;line-height:1}
.unit{font-size:13px;color:var(--muted);margin-left:4px}
.row{display:flex;justify-content:space-between;font-size:13px;margin-top:8px;color:var(--muted)}
.row b{color:var(--text);font-weight:600}
.bar{height:8px;background:#0b0f14;border-radius:5px;overflow:hidden;margin-top:6px;border:1px solid var(--line)}
.bar>i{display:block;height:100%;border-radius:5px;transition:width .3s ease,background .3s ease}
.pill{display:inline-block;font-size:11px;padding:3px 9px;border-radius:20px;border:1px solid var(--line)}
a{color:var(--green)}
.foot{margin-top:16px;font-size:11px;color:var(--muted)}
</style></head><body>
<h1>VOLT<b>BRIDGE</b> · 800VDC Rack Management Console</h1>
<div class=sub><span id=dot class=dot></span><span id=conn>connecting…</span>
 · live via Redfish API · <a href="/redfish/v1/Chassis/Rack1" target=_blank>view raw JSON</a></div>
<div id=banner class=banner>—</div>
<div class=grid>
  <div class=card><h2>Power</h2>
    <div><span id=rackkw class=big>—</span><span class=unit>kW rack</span></div>
    <div class=row><span>Bus voltage</span><b><span id=vbus>—</span> V</b></div>
    <div class=bar><i id=vbar style="background:var(--volt)"></i></div>
    <div class=row><span>Bus current</span><b><span id=ibus>—</span> A</b></div>
    <div class=bar><i id=ibar style="background:var(--amp)"></i></div>
  </div>
  <div class=card><h2>Thermal</h2>
    <div class=row><span>Rectifier</span><b><span id=rt>—</span> °C</b></div>
    <div class=bar><i id=rtbar></i></div>
    <div class=row><span>Power module</span><b><span id=mt>—</span> °C</b></div>
    <div class=bar><i id=mtbar></i></div>
    <div class=row style="margin-top:10px"><span>Critical limit</span><b>85 °C</b></div>
  </div>
  <div class=card><h2>Energy Storage</h2>
    <div><span id=soc class=big>—</span><span class=unit>% SoC</span></div>
    <div class=bar><i id=socbar style="background:var(--green)"></i></div>
    <div class=row><span>Storage power</span><b><span id=sp>—</span> kW</b></div>
    <div class=row><span id=bufpill class=pill>idle</span></div>
  </div>
  <div class=card><h2>Efficiency</h2>
    <div><span id=eff class=big>—</span><span class=unit>% end-to-end</span></div>
    <div class=row><span>54V baseline</span><b><span id=base>—</span> %</b></div>
    <div class=row><span>Gain</span><b id=gainwrap style="color:var(--green)">—</b></div>
    <div class=row style="margin-top:8px"><span>Phase</span><b id=phase>—</b></div>
  </div>
</div>
<div class=foot>Second subscriber on the MQTT telemetry bus · re-exposes the rack over Redfish (DMTF) ·
representative read/monitoring surface.</div>
<script>
const $=id=>document.getElementById(id);
const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
function tcolor(f){return f>=0.9?'var(--crit)':f>=0.82?'var(--warn)':'var(--green)'}
async function j(u){const r=await fetch(u);if(!r.ok)throw new Error(r.status);return r.json()}
async function tick(){
 try{
  const [ch,pw,th,ba]=await Promise.all([
    j('/redfish/v1/Chassis/Rack1'),j('/redfish/v1/Chassis/Rack1/Power'),
    j('/redfish/v1/Chassis/Rack1/Thermal'),j('/redfish/v1/Chassis/Rack1/Battery')]);
  $('dot').style.background='var(--green)';$('conn').textContent='LIVE — connected to bench';
  // health banner
  const h=(ch.Status&&ch.Status.Health)||'OK';
  const col=h==='Critical'?'var(--crit)':h==='Warning'?'var(--warn)':'var(--green)';
  const b=$('banner');b.style.color=col;b.style.borderColor=col;
  b.textContent=(h==='OK'?'● HEALTHY':'● '+h.toUpperCase())+
    ' · '+(ch.PowerState||'')+(ch.Oem&&ch.Oem.VoltBridge&&ch.Oem.VoltBridge.Fault?(' · FAULT '+ch.Oem.VoltBridge.Fault):'');
  // power
  const oem=(pw.Oem&&pw.Oem.VoltBridge)||{};
  const v=pw.Voltages&&pw.Voltages[0]?pw.Voltages[0].ReadingVolts:null;
  const rackkw=oem.RackPowerkW!=null?oem.RackPowerkW:(pw.PowerControl&&pw.PowerControl[0]?pw.PowerControl[0].PowerConsumedWatts/1000:null);
  const i=oem.BusCurrentAmps;
  $('rackkw').textContent=rackkw!=null?rackkw.toFixed(0):'—';
  $('vbus').textContent=v!=null?v.toFixed(0):'—';$('vbar').style.width=clamp((v||0)/900*100,0,100)+'%';
  const ilim=(ch.Oem&&ch.Oem.VoltBridge&&ch.Oem.VoltBridge.Mode==='dc')?1320:680;
  $('ibus').textContent=i!=null?i.toFixed(0):'—';$('ibar').style.width=clamp((i||0)/ilim*100,0,100)+'%';
  // thermal
  const temps={};(th.Temperatures||[]).forEach(t=>temps[t.Name]=t.ReadingCelsius);
  const rt=temps['Rectifier'],mt=temps['Power Module'];
  $('rt').textContent=rt!=null?rt.toFixed(1):'—';$('mt').textContent=mt!=null?mt.toFixed(1):'—';
  $('rtbar').style.width=clamp((rt||0)/85*100,0,100)+'%';$('rtbar').style.background=tcolor((rt||0)/85);
  $('mtbar').style.width=clamp((mt||0)/85*100,0,100)+'%';$('mtbar').style.background=tcolor((mt||0)/85);
  // storage
  const soc=ba.StateOfChargePercent,bo=(ba.Oem&&ba.Oem.VoltBridge)||{};
  $('soc').textContent=soc!=null?soc.toFixed(0):'—';$('socbar').style.width=clamp(soc||0,0,100)+'%';
  $('sp').textContent=bo.StoragePowerkW!=null?bo.StoragePowerkW.toFixed(0):'—';
  const bp=$('bufpill');
  if(bo.Buffering){bp.textContent='● BUFFERING';bp.style.color='var(--gold)';bp.style.borderColor='var(--gold)';}
  else{bp.textContent='idle';bp.style.color='var(--muted)';bp.style.borderColor='var(--line)';}
  // efficiency
  const co=(ch.Oem&&ch.Oem.VoltBridge)||{};
  $('eff').textContent=co.EndToEndEfficiencyPercent!=null?co.EndToEndEfficiencyPercent:'—';
  $('base').textContent=co.BaselineEfficiencyPercent!=null?co.BaselineEfficiencyPercent:'—';
  const g=co.EfficiencyGainPercent;
  const gw=$('gainwrap');
  if(co.Phase==='FAULT'||g==null||g<=0){gw.textContent='—';gw.style.color='var(--muted)';}
  else{gw.textContent='+'+g+' %';gw.style.color='var(--green)';}
  $('phase').textContent=co.Phase||'—';
 }catch(e){
  $('dot').style.background='var(--crit)';
  $('conn').textContent='waiting for bench (start bench.py --mode dc --mqtt)';
 }
}
tick();setInterval(tick,1000);
</script></body></html>"""


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
