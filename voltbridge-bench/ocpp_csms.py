#!/usr/bin/env python3
"""
VoltBridge minimal CSMS  (OCPP 1.6-J)
=====================================
A tiny Charging Station Management System for the demo. It accepts an OCPP 1.6-J
WebSocket connection from ocpp_gateway.py (the charge point), prints every OCPP
message it receives, and replies with valid CALLRESULTs. This is the window you
WATCH to prove real OCPP-format communication driven by live bench telemetry.

Run:
    pip install websockets
    python ocpp_csms.py            # ws://localhost:9000  (waits for the charge point)

Then start the charge point:
    python ocpp_gateway.py

SCOPE (honest): a representative CSMS for demonstration — it acknowledges the
core messages. A production CSMS implements the full OCPP message set, auth,
smart charging, and persistence.
"""
import argparse
import asyncio
import json
from datetime import datetime, timezone


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def result_for(action, payload):
    """Build a valid OCPP 1.6 CALLRESULT payload for a given action."""
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
    # StatusNotification, MeterValues, DataTransfer -> empty CALLRESULT
    return {}


async def handler(ws, path=None):
    cid = (path or getattr(ws, "path", "/") or "/").strip("/") or "CP"
    print(f"[CSMS] charge point connected: {cid}")
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            # OCPP-J CALL: [2, uniqueId, action, payload]
            if isinstance(msg, list) and len(msg) == 4 and msg[0] == 2:
                _, uid, action, payload = msg
                print(f"[CSMS] <- {action}: {json.dumps(payload)}")
                await ws.send(json.dumps([3, uid, result_for(action, payload)]))
    except Exception as e:
        print(f"[CSMS] {cid} disconnected: {e}")


async def main(port):
    import websockets
    async with websockets.serve(handler, "0.0.0.0", port, subprotocols=["ocpp1.6"]):
        print(f"[CSMS] OCPP 1.6-J server on ws://localhost:{port}  (waiting for charge point)")
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="VoltBridge minimal OCPP 1.6-J CSMS")
    ap.add_argument("--port", type=int, default=9000, help="WebSocket port (default 9000)")
    args = ap.parse_args()
    try:
        asyncio.run(main(args.port))
    except KeyboardInterrupt:
        print("\nshutting down")
