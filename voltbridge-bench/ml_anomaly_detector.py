#!/usr/bin/env python3
"""
VoltBridge ML anomaly detector  (unsupervised, covariance / Mahalanobis)
========================================================================
A subscriber on the MQTT telemetry bus that scores each DC-rack sample against a
model of NORMAL operation (trained by train_ml_anomaly.py) and flags multivariate
outliers — cases where the *combination* of readings is unlike anything seen in
normal operation, even if each reading is individually in range.

It runs ALONGSIDE anomaly_detector.py (the transparent statistical one):
  * statistical detector → explainable, per-limit early warning
  * ML detector          → novel multivariate deviations

Alerts are re-published to voltbridge/alerts (tagged source=ml).

Run:
    pip install scikit-learn numpy joblib paho-mqtt
    python train_ml_anomaly.py        # once, to create ml_anomaly_model.joblib
    python ml_anomaly_detector.py

SCOPE (honest): unsupervised outlier detection trained on synthetic normal
telemetry that reproduces the bench's steady-state physics. Retrain on real
logged telemetry (same feature vector) as it accumulates — no code changes.
DC-rack mode; an EV model would be a second, similarly-trained instance.
"""
import argparse
import json
import os

TELEMETRY_TOPIC = "voltbridge/telemetry"
ALERT_TOPIC = "voltbridge/alerts"
MODEL_PATH = os.path.join(os.path.dirname(__file__), "ml_anomaly_model.joblib")


def features_from(tel, feature_names):
    """Build the model's feature vector from a telemetry dict (DC mode).
    Returns a list of floats, or None if a field is missing."""
    src = {
        "v_bus": tel.get("v_bus"),
        "i_bus": tel.get("i_bus"),
        "power_kw": tel.get("power_kw"),
        "temp": tel.get("temp"),
        "rect_temp": tel.get("rect_temp"),
        "eff": tel.get("e2e_eff", tel.get("eff")),
    }
    row = [src[f] for f in feature_names]
    if any(v is None for v in row):
        return None
    return row


def main():
    ap = argparse.ArgumentParser(description="VoltBridge ML anomaly detector (MQTT subscriber)")
    ap.add_argument("--broker", default="localhost:1883", help="MQTT broker host:port")
    ap.add_argument("--model", default=MODEL_PATH, help="path to ml_anomaly_model.joblib")
    ap.add_argument("--cooldown", type=float, default=5.0, help="seconds between repeated alerts")
    args = ap.parse_args()

    try:
        import joblib
        import numpy as np
    except ImportError:
        print("needs scikit-learn + joblib + numpy (pip install scikit-learn joblib numpy)")
        return
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        print("needs paho-mqtt (pip install paho-mqtt)")
        return

    if not os.path.exists(args.model):
        print(f"model not found: {args.model}\nrun:  python train_ml_anomaly.py")
        return
    bundle = joblib.load(args.model)
    model, feats = bundle["model"], bundle["features"]
    print(f"[ml] loaded model · features: {feats}")

    state = {"last": -1e9, "n": 0, "flags": 0}
    host, _, port = args.broker.partition(":")
    port = int(port or 1883)
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
    except (AttributeError, TypeError):
        client = mqtt.Client()

    def on_connect(c, u, flags, rc):
        c.subscribe(TELEMETRY_TOPIC)
        print(f"[ml] connected to {args.broker}; scoring {TELEMETRY_TOPIC} (DC mode)")

    def on_message(c, u, msg):
        try:
            tel = json.loads(msg.payload.decode())
        except Exception:
            return
        if tel.get("mode") != "dc":
            return                              # this model is the DC-rack model
        row = features_from(tel, feats)
        if row is None:
            return
        state["n"] += 1
        X = np.array([row], dtype=float)
        pred = int(model.predict(X)[0])          # -1 = anomaly, 1 = normal
        score = float(model.score_samples(X)[0]) # higher = more normal
        t = float(tel.get("t", 0.0) or 0.0)
        if pred == -1:
            state["flags"] += 1
            if t - state["last"] >= args.cooldown:
                state["last"] = t
                alert = {"t": round(t, 1), "source": "ml", "kind": "multivariate_outlier",
                         "severity": "warning", "score": round(score, 3),
                         "message": f"ML: multivariate outlier (score {score:.2f}) — "
                                    f"v={row[0]:.0f}V i={row[1]:.0f}A temp={row[3]:.1f}C eff={row[5]:.2f}%"}
                print(f"[ML-WARN] t={alert['t']}s  {alert['message']}")
                try:
                    c.publish(ALERT_TOPIC, json.dumps(alert), qos=0)
                except Exception:
                    pass
        if state["n"] % 200 == 0:
            print(f"[ml] scored {state['n']} samples · {state['flags']} anomalies flagged")

    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect(host, port, keepalive=30)
    except Exception as e:
        print(f"[ml] connect failed: {e}; start broker + bench --mode dc --mqtt first")
        return
    print("[ml] alerts re-published to voltbridge/alerts (source=ml)")
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        print("\nshutting down")


if __name__ == "__main__":
    main()
