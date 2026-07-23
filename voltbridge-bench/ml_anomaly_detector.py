#!/usr/bin/env python3
"""
VoltBridge ML anomaly detector  (unsupervised, covariance / Mahalanobis)
========================================================================
A subscriber on the MQTT telemetry bus that scores each sample against a model
of NORMAL operation and flags multivariate outliers — combinations of readings
unlike anything seen in normal operation, even when each reading is individually
in range. It auto-selects the model by telemetry mode:

    mode = dc  ->  ml_anomaly_model_dc.joblib   (single model, 800VDC rack)
    mode = ev  ->  ml_anomaly_model_ev.joblib   (two sub-models, CC / CV charge)

Runs alongside anomaly_detector.py (transparent statistical layer). Alerts are
re-published to voltbridge/alerts (tagged source=ml).

    pip install scikit-learn numpy joblib paho-mqtt
    python train_ml_anomaly.py          # once -> the two .joblib models
    python ml_anomaly_detector.py

SCOPE (honest): unsupervised outlier detection trained on synthetic normal
telemetry that reproduces the bench physics per domain. Retrain on real logged
telemetry (same feature vectors) as it accumulates — no code changes.
"""
import argparse
import json
import os

TELEMETRY_TOPIC = "voltbridge/telemetry"
ALERT_TOPIC = "voltbridge/alerts"
HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_FILES = {"dc": "ml_anomaly_model_dc.joblib", "ev": "ml_anomaly_model_ev.joblib"}
IMAX_TYP = {400: 550.0, 800: 370.0}   # matches synth_telemetry (i_frac normaliser)


def features_dc(tel):
    row = [tel.get("v_bus"), tel.get("i_bus"), tel.get("power_kw"),
           tel.get("temp"), tel.get("rect_temp"), tel.get("e2e_eff", tel.get("eff"))]
    return None if any(v is None for v in row) else row


def features_ev(tel):
    pack = tel.get("pack_v")
    v, i, soc = tel.get("v_bus"), tel.get("i_bus"), tel.get("soc")
    temp, eff = tel.get("temp"), tel.get("eff")
    if None in (pack, v, i, soc, temp, eff) or not pack:
        return None
    imax = IMAX_TYP.get(int(round(pack / 400.0) * 400), 460.0)   # 400 or 800 bucket
    return [v / pack, i / imax, temp, soc, eff]


def predict_one(bundle, row):
    """Return (pred, score) for a single feature row using a loaded bundle."""
    import numpy as np
    X = np.array([row], dtype=float)
    if bundle["kind"] == "single":
        m = bundle["model"]
        return int(m.predict(X)[0]), float(m.score_samples(X)[0])
    # split model (EV): pick CC or CV by soc
    idx, split = bundle["soc_index"], bundle["soc_split"]
    sub = bundle["cc"] if X[0, idx] < split else bundle["cv"]
    return int(sub.predict(X)[0]), float(sub.score_samples(X)[0])


def main():
    ap = argparse.ArgumentParser(description="VoltBridge ML anomaly detector (MQTT subscriber)")
    ap.add_argument("--broker", default="localhost:1883", help="MQTT broker host:port")
    ap.add_argument("--cooldown", type=float, default=5.0, help="seconds between repeated alerts")
    args = ap.parse_args()

    try:
        import joblib
        import numpy as np  # noqa: F401  (used in predict_one)
    except ImportError:
        print("needs scikit-learn + joblib + numpy (pip install scikit-learn joblib numpy)")
        return
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        print("needs paho-mqtt (pip install paho-mqtt)")
        return

    models = {}
    for mode, fn in MODEL_FILES.items():
        path = os.path.join(HERE, fn)
        if os.path.exists(path):
            models[mode] = joblib.load(path)
    if not models:
        print("no models found. run:  python train_ml_anomaly.py")
        return
    print(f"[ml] loaded models: {', '.join(sorted(models))}")

    state = {"last": {"dc": -1e9, "ev": -1e9}, "n": 0, "flags": 0}
    host, _, port = args.broker.partition(":")
    port = int(port or 1883)
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
    except (AttributeError, TypeError):
        client = mqtt.Client()

    def on_connect(c, u, flags, rc):
        c.subscribe(TELEMETRY_TOPIC)
        print(f"[ml] connected to {args.broker}; scoring {TELEMETRY_TOPIC}")

    def on_message(c, u, msg):
        try:
            tel = json.loads(msg.payload.decode())
        except Exception:
            return
        mode = tel.get("mode")
        if mode not in models:
            return
        if tel.get("phase") != "TRANSFER":       # only score during energy transfer
            return
        row = features_dc(tel) if mode == "dc" else features_ev(tel)
        if row is None:
            return
        state["n"] += 1
        pred, score = predict_one(models[mode], row)
        t = float(tel.get("t", 0.0) or 0.0)
        if pred == -1:
            state["flags"] += 1
            if t - state["last"][mode] >= args.cooldown:
                state["last"][mode] = t
                if mode == "dc":
                    detail = f"v={row[0]:.0f}V i={row[1]:.0f}A temp={row[3]:.1f}C eff={row[5]:.2f}%"
                else:
                    detail = f"v/pack={row[0]:.2f} i_frac={row[1]:.2f} temp={row[2]:.1f}C soc={row[3]:.0f}% eff={row[4]:.2f}%"
                alert = {"t": round(t, 1), "source": "ml", "mode": mode,
                         "kind": "multivariate_outlier", "severity": "warning",
                         "score": round(score, 3),
                         "message": f"ML[{mode}]: multivariate outlier (score {score:.2f}) — {detail}"}
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
        print(f"[ml] connect failed: {e}; start broker + bench --mqtt first")
        return
    print("[ml] alerts re-published to voltbridge/alerts (source=ml)")
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        print("\nshutting down")


if __name__ == "__main__":
    main()
