#!/usr/bin/env python3
"""
VoltBridge CSMS + Charge Point Monitor  (OCPP 1.6-J)
====================================================
A minimal Charging Station Management System for the demo. It:
  * accepts an OCPP 1.6-J WebSocket connection from ocpp_gateway.py
  * replies with valid CALLRESULTs
  * serves a live "Charge Point Monitor" web page (status + meter values +
    a scrolling OCPP message feed) so the EV side is as visual as the Redfish
    console on the DC side.

Run:
    pip install websockets
    python ocpp_csms.py            # OCPP on ws://localhost:9000, UI on http://localhost:9100
Then start the charge point:
    python ocpp_gateway.py

SCOPE (honest): representative CSMS for demonstration — acknowledges the core
messages. A production CSMS implements the full OCPP message set, auth, smart
charging and persistence.
"""
import argparse
import asyncio
import json
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---- shared state (updated by the async WS handler, read by the HTTP threads) ----
_lock = threading.Lock()
_state = {
    "connected": False,
    "cp": None,
    "boot": None,
    "status": "—",
    "error": "NoError",
    "meter": {"Voltage": None, "Current.Import": None, "Power.Active.Import": None, "SoC": None},
    "messages": deque(maxlen=40),   # {clock, action, summary}
    "count": 0,
}


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def result_for(action, payload):
    if action == "BootNotification":
        return {"currentTime": _now_iso(), "interval": 300, "status": "Accepted"}
    if action == "Heartbeat":
        return {"currentTime": _now_iso()}
    if action == "Authorize":
        return {"idTagInfo": {"status": "Accepted"}}
    if action == "StartTransaction":
        return {"transactionId": 1, "idTagInfo": {"status": "Accepted"}}
    if action == "StopTransaction":
        return {"idTagInfo": {"status": "Accepted"}}
    return {}


def _summary(action, payload):
    if action == "StatusNotification":
        return payload.get("status", "")
    if action == "BootNotification":
        return f"{payload.get('chargePointVendor','')} {payload.get('chargePointModel','')}".strip()
    if action == "MeterValues":
        d = {}
        for mv in payload.get("meterValue", []):
            for sv in mv.get("sampledValue", []):
                d[sv.get("measurand")] = sv.get("value")
        return f"V={d.get('Voltage','?')} A={d.get('Current.Import','?')} SoC={d.get('SoC','?')}%"
    return ""


def _apply(action, payload):
    with _lock:
        _state["count"] += 1
        _state["messages"].appendleft({
            "clock": time.strftime("%H:%M:%S"),
            "action": action,
            "summary": _summary(action, payload),
        })
        if action == "BootNotification":
            _state["boot"] = f"{payload.get('chargePointVendor','')} {payload.get('chargePointModel','')}".strip()
        elif action == "StatusNotification":
            _state["status"] = payload.get("status", "—")
            _state["error"] = payload.get("errorCode", "NoError")
        elif action == "MeterValues":
            for mv in payload.get("meterValue", []):
                for sv in mv.get("sampledValue", []):
                    m = sv.get("measurand")
                    if m in _state["meter"]:
                        _state["meter"][m] = sv.get("value")


# ---- OCPP WebSocket server ----

async def handler(ws, path=None):
    cid = (path or getattr(ws, "path", "/") or "/").strip("/") or "CP"
    with _lock:
        _state["connected"] = True
        _state["cp"] = cid
    print(f"[CSMS] charge point connected: {cid}")
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if isinstance(msg, list) and len(msg) == 4 and msg[0] == 2:
                _, uid, action, payload = msg
                print(f"[CSMS] <- {action}: {json.dumps(payload)}")
                _apply(action, payload)
                await ws.send(json.dumps([3, uid, result_for(action, payload)]))
    except Exception as e:
        print(f"[CSMS] {cid} disconnected: {e}")
    finally:
        with _lock:
            _state["connected"] = False


# ---- HTTP monitor UI ----

MONITOR_HTML = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>VoltBridge — Charge Point Monitor</title>
<style>
:root{--bg:#0b0f14;--panel:#121820;--line:#2a3b4d;--text:#e9eef3;--muted:#a3b4c2;
--gold:#f2b138;--green:#2dd4a7;--amp:#7fd4ff;--volt:#f2b138;--warn:#f0902e;--crit:#ff5470;--power:#b98cff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font-family:ui-monospace,"Cascadia Code",Consolas,monospace;padding:22px}
h1{font-family:system-ui,sans-serif;font-size:22px;margin:0 0 2px}h1 b{color:var(--gold)}
.sub{color:var(--muted);font-size:12px;margin-bottom:16px}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;vertical-align:middle}
.banner{border-radius:10px;padding:12px 16px;font-size:15px;font-weight:700;margin-bottom:16px;
border:1px solid var(--line);letter-spacing:1px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin-bottom:16px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card h2{font-family:system-ui,sans-serif;font-size:11px;letter-spacing:2px;color:var(--muted);
text-transform:uppercase;margin:0 0 8px}
.big{font-size:28px;font-weight:700;font-family:system-ui,sans-serif;line-height:1}
.unit{font-size:12px;color:var(--muted);margin-left:3px}
.feed{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.feed h2{font-family:system-ui,sans-serif;font-size:11px;letter-spacing:2px;color:var(--muted);
text-transform:uppercase;margin:0 0 8px}
.msg{font-size:12px;padding:4px 0;border-bottom:1px solid #1b2733;display:flex;gap:10px}
.msg .clk{color:var(--muted)}.msg .act{color:var(--green);min-width:150px}
.foot{margin-top:14px;font-size:11px;color:var(--muted)}
</style></head><body>
<h1>VOLT<b>BRIDGE</b> · Charge Point Monitor</h1>
<div class=sub><span id=dot class=dot></span><span id=conn>waiting…</span> · OCPP 1.6-J CSMS</div>
<div id=banner class=banner>—</div>
<div class=grid>
  <div class=card><h2>Voltage</h2><div><span id=v class=big>—</span><span class=unit>V</span></div></div>
  <div class=card><h2>Current</h2><div><span id=i class=big>—</span><span class=unit>A</span></div></div>
  <div class=card><h2>Power</h2><div><span id=p class=big>—</span><span class=unit>kW</span></div></div>
  <div class=card><h2>State of Charge</h2><div><span id=soc class=big>—</span><span class=unit>%</span></div></div>
</div>
<div class=feed><h2>OCPP message feed</h2><div id=msgs></div></div>
<div class=foot>Charge point reporting over OCPP 1.6-J — the global EV charging management standard —
fed from the same MQTT telemetry bus.</div>
<script>
const $=id=>document.getElementById(id);
function bcolor(s){return s==='Charging'?'var(--green)':s==='Faulted'?'var(--crit)':
 s==='Preparing'?'var(--warn)':s==='Finishing'?'var(--green)':'var(--muted)'}
async function tick(){
 try{
  const s=await (await fetch('/status')).json();
  $('dot').style.background=s.connected?'var(--green)':'var(--crit)';
  $('conn').textContent=s.connected?('connected — '+(s.cp||'CP')+(s.boot?(' · '+s.boot):'')):'waiting for charge point';
  const st=s.status||'—';
  const b=$('banner');const c=bcolor(st);b.style.color=c;b.style.borderColor=c;
  b.textContent='● '+st.toUpperCase()+(s.error&&s.error!=='NoError'?(' · '+s.error):'');
  const m=s.meter||{};
  $('v').textContent=m.Voltage!=null?m.Voltage:'—';
  $('i').textContent=m['Current.Import']!=null?m['Current.Import']:'—';
  $('p').textContent=m['Power.Active.Import']!=null?m['Power.Active.Import']:'—';
  $('soc').textContent=m.SoC!=null?m.SoC:'—';
  $('msgs').innerHTML=(s.messages||[]).map(x=>
    '<div class=msg><span class=clk>'+x.clock+'</span><span class=act>'+x.action+
    '</span><span>'+(x.summary||'')+'</span></div>').join('');
 }catch(e){$('dot').style.background='var(--crit)';$('conn').textContent='monitor offline';}
}
tick();setInterval(tick,1000);
</script></body></html>"""


class MonitorHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/status":
            with _lock:
                snap = {
                    "connected": _state["connected"], "cp": _state["cp"], "boot": _state["boot"],
                    "status": _state["status"], "error": _state["error"],
                    "meter": dict(_state["meter"]), "messages": list(_state["messages"]),
                    "count": _state["count"],
                }
            return self._send(200, json.dumps(snap), "application/json")
        return self._send(200, MONITOR_HTML, "text/html; charset=utf-8")


def _start_http(port):
    srv = ThreadingHTTPServer(("0.0.0.0", port), MonitorHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[CSMS] Charge Point Monitor UI on http://localhost:{port}/")


async def main(ws_port, http_port):
    import websockets
    _start_http(http_port)
    async with websockets.serve(handler, "0.0.0.0", ws_port, subprotocols=["ocpp1.6"]):
        print(f"[CSMS] OCPP 1.6-J server on ws://localhost:{ws_port}  (waiting for charge point)")
        await asyncio.Future()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="VoltBridge minimal OCPP 1.6-J CSMS + monitor UI")
    ap.add_argument("--port", type=int, default=9000, help="OCPP WebSocket port (default 9000)")
    ap.add_argument("--http-port", type=int, default=9100, help="monitor UI HTTP port (default 9100)")
    args = ap.parse_args()
    try:
        asyncio.run(main(args.port, args.http_port))
    except KeyboardInterrupt:
        print("\nshutting down")
