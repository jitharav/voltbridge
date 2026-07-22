#!/usr/bin/env python3
"""Offline self-test for anomaly_detector.py — validates the statistical monitor
catches real early-warning cases AND does not false-alarm on normal steady-state
telemetry (with realistic ripple). No broker/network needed; used by CI."""
import sys
from anomaly_detector import AnomalyMonitor

checks = 0


def ok(cond, msg):
    global checks
    checks += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)
    print(f"ok: {msg}")


def base(t, **over):
    d = {"t": t, "mode": "dc", "phase": "TRANSFER", "v_bus": 800.0,
         "i_bus": 1056.0, "power_kw": 845.0, "temp": 63.0, "rect_temp": 63.0,
         "iso_mohm": 540.0, "contactor": True, "fault": None}
    d.update(over)
    return d


def test_no_false_alarms_steady():
    m = AnomalyMonitor()
    n_alerts = 0
    import math
    for i in range(120):  # ~6s of steady operation with realistic ripple
        t = i * 0.05
        tel = base(t,
                   v_bus=800.0 + 2.0 * math.sin(i / 3.0),      # bus ripple
                   i_bus=1056.0 + 120.0 * math.sin(i / 7.0),   # normal load transients
                   temp=63.0 + 0.3 * math.sin(i / 5.0),
                   rect_temp=63.0 + 0.2 * math.sin(i / 6.0),
                   iso_mohm=540.0)
        n_alerts += len(m.update(tel))
    ok(n_alerts == 0, f"no false alarms on steady operation (got {n_alerts})")


def test_thermal_projection_warns_before_trip():
    m = AnomalyMonitor()
    warned_before = False
    tripped_at = None
    temp = 60.0
    for i in range(200):
        t = i * 0.05
        temp += 0.25            # steady rise toward the 85 degC trip
        if temp >= 85 and tripped_at is None:
            tripped_at = t
        alerts = m.update(base(t, temp=temp))
        if any(a["signal"] == "temp" for a in alerts) and (tripped_at is None):
            warned_before = True
    ok(warned_before, "thermal early-warning fires before the 85 degC trip")


def test_current_proximity_dc():
    m = AnomalyMonitor()
    got = False
    for i in range(20):
        alerts = m.update(base(i * 0.05, i_bus=1260.0))  # > 0.92 * 1320 = 1214
        if any(a["signal"] == "i_bus" and a["kind"] == "proximity" for a in alerts):
            got = True
    ok(got, "bus-current proximity warning near the DC overcurrent limit")


def test_insulation_drop():
    m = AnomalyMonitor()
    got = False
    iso = 540.0
    for i in range(60):
        t = i * 0.05
        if i > 30:
            iso = max(0.05, iso - 60.0)  # insulation collapsing
        alerts = m.update(base(t, iso_mohm=iso))
        if any(a["signal"] == "iso_mohm" for a in alerts):
            got = True
    ok(got, "insulation degradation raises an alert")


def main():
    test_no_false_alarms_steady()
    test_thermal_projection_warns_before_trip()
    test_current_proximity_dc()
    test_insulation_drop()
    print(f"\nAll {checks} anomaly-detector checks passed.")


if __name__ == "__main__":
    main()
