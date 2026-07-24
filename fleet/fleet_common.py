#!/usr/bin/env python3
"""Shared helpers for the VoltBridge fleet PoC — telemetry generation and
health classification for a virtual node. Kept dependency-light (numpy only)
so it runs anywhere, including the in-process scaling harness."""
import numpy as np

TELEMETRY_TOPIC_PREFIX = "voltbridge/fleet"     # MQTT:  voltbridge/fleet/<node>/telemetry
KAFKA_TOPIC = "voltbridge.telemetry"


def gen_telemetry(node_id, tick, rng):
    """One telemetry record for a virtual node at a given tick. Half the fleet
    runs DC-rack mode, half EV-charge mode — representative, not physics-exact."""
    mode = "dc" if node_id % 2 == 0 else "ev"
    if mode == "dc":
        v = 800 + rng.normal(0, 2)
        i = 1300 + rng.normal(0, 15)
        temp = 64 + rng.normal(0, 1.2)
        soc = None
        eff = 95.3 + rng.normal(0, 0.1)
    else:
        v = 800.0
        i = max(40.0, 600 - tick * 3) + rng.normal(0, 8)   # CC→CV taper
        temp = 45 + rng.normal(0, 1.5)
        soc = float(min(100, 20 + tick * 2))
        eff = 97.0 + rng.normal(0, 0.1)
    rec = {
        "node": f"vb-{node_id:04d}", "mode": mode, "t": int(tick),
        "v_bus": round(float(v), 1), "i_bus": round(float(i), 1),
        "power_kw": round(float(v * i / 1000.0), 1),
        "temp": round(float(temp), 1), "soc": soc, "eff": round(float(eff), 2),
    }
    rec["health"] = classify(rec)
    return rec


def classify(rec):
    if rec["temp"] > 85 or rec["v_bus"] > 900 or rec["i_bus"] > 1800:
        return "fault"
    if rec["temp"] > 78:
        return "warning"
    return "healthy"


def fleet_aggregate(latest_by_node):
    """Roll up per-node latest records into fleet KPIs."""
    n = len(latest_by_node)
    if n == 0:
        return {"nodes": 0, "total_power_mw": 0.0, "healthy": 0, "warning": 0, "fault": 0, "mean_temp": 0.0}
    total_kw = sum(r["power_kw"] for r in latest_by_node.values())
    temps = [r["temp"] for r in latest_by_node.values()]
    h = sum(1 for r in latest_by_node.values() if r["health"] == "healthy")
    w = sum(1 for r in latest_by_node.values() if r["health"] == "warning")
    f = sum(1 for r in latest_by_node.values() if r["health"] == "fault")
    return {"nodes": n, "total_power_mw": round(total_kw / 1000.0, 2),
            "healthy": h, "warning": w, "fault": f,
            "mean_temp": round(sum(temps) / n, 1)}
