#!/usr/bin/env python3
"""
VoltBridge anomaly detector  (statistical early-warning)
========================================================
A THIRD subscriber on the MQTT telemetry bus (alongside the dashboard and the
Redfish/OCPP gateways). It watches the live signals and raises EARLY WARNINGS
before the bench's hard protection trips — using lightweight statistical process
monitoring, not a trained model:

  * proximity   — reading within 92% of its protection limit
  * projection  — linear trend extrapolated to the limit ("~N s to trip")
  * z-score     — statistical outlier vs a rolling window

It also re-publishes alerts to `voltbridge/alerts`, so it's both a subscriber
and a publisher — the pub/sub fan-out in action. This is deliberately
conservative (tuned to avoid false alarms on normal load transients / warm-up).

HONEST FRAMING: this is classical statistical process control (rolling mean/std,
z-score, linear trend) — transparent and explainable. It is NOT machine learning
and makes no trained-model claims; it's an early-warning layer that turns raw
telemetry into "act before it trips."

Run (with broker + bench --mqtt running):
    python anomaly_detector.py
    # optionally watch the alert topic in another window:
    mosquitto_sub -h localhost -t voltbridge/alerts -v
"""
import argparse
import json
import threading
import time
from collections import deque

TELEMETRY_TOPIC = "voltbridge/telemetry"
ALERT_TOPIC = "voltbridge/alerts"

# key -> (label, unit, static_limit, direction). i_bus limit is mode-dependent.
SIGNAL_META = {
    "temp":     ("Module temp",     "degC", 85.0, "high"),
    "rect_temp":("Rectifier temp",  "degC", 85.0, "high"),
    "v_bus":    ("Bus voltage",     "V",    900.0, "high"),
    "i_bus":    ("Bus current",     "A",    None,  "high"),
    "iso_mohm": ("Insulation",      "MOhm", 0.1,  "low"),
}


class AnomalyMonitor:
    """Pure statistical core — no networking, unit-testable."""

    def __init__(self, window=40, z_thresh=4.0, prox=0.92,
                 low_prox_factor=3.0, horizon=6.0, cooldown=6.0):
        self.window = window
        self.z_thresh = z_thresh
        self.prox = prox                    # high-side: within prox*limit
        self.low_prox_factor = low_prox_factor  # low-side: within limit*factor
        self.horizon = horizon              # seconds ahead for trend projection
        self.cooldown = cooldown            # per (signal,kind) alert de-dup
        self.hist = {}                      # key -> deque[(t,val)]
        self.last = {}                      # (key,kind) -> t

    def _limit(self, key, tel):
        if key == "i_bus":
            return 1320.0 if tel.get("mode") == "dc" else 680.0
        return SIGNAL_META[key][2]

    def _emit(self, alerts, key, kind, t, severity, message):
        k = (key, kind)
        if t - self.last.get(k, -1e9) < self.cooldown:
            return
        self.last[k] = t
        alerts.append({"t": round(t, 1), "signal": key, "kind": kind,
                       "severity": severity, "message": message})

    def update(self, tel):
        """Feed one telemetry dict; return a list of alert dicts (possibly empty)."""
        t = float(tel.get("t", 0.0) or 0.0)
        phase = tel.get("phase", "IDLE")
        analyse_trend = phase == "TRANSFER"   # avoid warm-up / precharge ramps
        alerts = []
        for key, (label, unit, _static, direction) in SIGNAL_META.items():
            val = tel.get(key)
            if val is None:
                continue
            limit = self._limit(key, tel)
            dq = self.hist.setdefault(key, deque(maxlen=self.window))
            dq.append((t, val))
            if limit is None:
                continue

            # --- proximity to limit ---
            if direction == "high" and val >= self.prox * limit:
                self._emit(alerts, key, "proximity", t, "warning",
                           f"{label} {val:.1f}{unit} within {int(self.prox*100)}% of limit {limit:.0f}{unit}")
            if direction == "low" and 0 < val <= limit * self.low_prox_factor:
                self._emit(alerts, key, "proximity", t, "warning",
                           f"{label} {val:.3f}{unit} approaching limit {limit:.2f}{unit}")

            if not analyse_trend or len(dq) < 8:
                continue

            # --- trend projection (time-to-limit); skip i_bus (normal transients) ---
            (t0, v0), (t1, v1) = dq[0], dq[-1]
            dtw = t1 - t0
            if key != "i_bus" and dtw > 0:
                slope = (v1 - v0) / dtw
                if direction == "high" and slope > 1e-6 and val >= 0.6 * limit:
                    eta = (limit - val) / slope
                    if 0 < eta <= self.horizon:
                        self._emit(alerts, key, "projection", t, "warning",
                                   f"{label} rising ~{slope:.1f}{unit}/s — projected to reach "
                                   f"{limit:.0f}{unit} in ~{eta:.1f}s")
                if direction == "low" and slope < -1e-9 and val <= limit * (2 * self.low_prox_factor):
                    eta = (val - limit) / (-slope)
                    if 0 < eta <= self.horizon:
                        self._emit(alerts, key, "projection", t, "warning",
                                   f"{label} falling — projected to reach {limit:.2f}{unit} in ~{eta:.1f}s")

            # --- z-score outlier (smooth signals only; guarded against tiny-noise) ---
            if key in ("temp", "rect_temp", "v_bus", "iso_mohm") and len(dq) >= self.window // 2:
                vals = [v for _, v in dq]
                m = sum(vals) / len(vals)
                sd = (sum((v - m) ** 2 for v in vals) / len(vals)) ** 0.5
                if sd > 1e-6 and abs(val - m) > 0.02 * limit:
                    z = (val - m) / sd
                    if abs(z) >= self.z_thresh:
                        self._emit(alerts, key, "zscore", t, "info",
                                   f"{label} statistical outlier (z={z:.1f}, reading {val:.2f}{unit})")
        return alerts


# ---------- MQTT wiring ----------

def main():
    ap = argparse.ArgumentParser(description="VoltBridge statistical anomaly detector (MQTT subscriber)")
    ap.add_argument("--broker", default="localhost:1883", help="MQTT broker host:port (default localhost:1883)")
    args = ap.parse_args()

    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        print("this needs 'paho-mqtt' (pip install paho-mqtt)")
        return

    monitor = AnomalyMonitor()
    state = {"last_beat": 0.0, "alerts": 0}
    host, _, port = args.broker.partition(":")
    port = int(port or 1883)
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
    except (AttributeError, TypeError):
        client = mqtt.Client()

    def on_connect(c, u, flags, rc):
        c.subscribe(TELEMETRY_TOPIC)
        print(f"[monitor] connected to {args.broker}; watching {TELEMETRY_TOPIC}")
        print("[monitor] statistical early-warning: proximity · trend projection · z-score")

    def on_message(c, u, msg):
        try:
            tel = json.loads(msg.payload.decode())
        except Exception:
            return
        alerts = monitor.update(tel)
        for a in alerts:
            state["alerts"] += 1
            tag = "WARN" if a["severity"] == "warning" else "info"
            print(f"[{tag}] t={a['t']}s  {a['message']}")
            try:
                c.publish(ALERT_TOPIC, json.dumps(a), qos=0)
            except Exception:
                pass
        # quiet heartbeat so the window shows it's alive and nominal
        t = float(tel.get("t", 0.0) or 0.0)
        if t - state["last_beat"] >= 10:
            state["last_beat"] = t
            if not alerts:
                print(f"[monitor] t={t:.0f}s  all signals nominal ({state['alerts']} alerts so far)")

    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect(host, port, keepalive=30)
    except Exception as e:
        print(f"[monitor] connect to {args.broker} failed: {e}; start the broker + bench --mqtt first")
        return
    print("[monitor] alerts re-published to voltbridge/alerts  (mosquitto_sub -t voltbridge/alerts -v)")
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        print("\nshutting down")


if __name__ == "__main__":
    main()
