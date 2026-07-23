#!/usr/bin/env python3
"""
Train the ML anomaly model — an unsupervised, covariance-based (Mahalanobis
distance) outlier detector that learns the shape of NORMAL 800VDC-rack
operation and flags out-of-distribution samples.

    pip install scikit-learn numpy joblib
    python train_ml_anomaly.py            # writes ml_anomaly_model.joblib

The model is unsupervised (no fault labels needed) — it learns "normal" only, as
a robust multivariate Gaussian; anything far from that cloud in Mahalanobis
distance is an anomaly. Fault samples are used here solely to report detection
rate. This complements (does not replace) the transparent statistical detector
in anomaly_detector.py — that one gives explainable, per-limit early warning;
this one catches novel multivariate deviations.
"""
import joblib
from sklearn.covariance import EllipticEnvelope
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline

from synth_telemetry import normal_samples, fault_samples, FEATURES

MODEL_PATH = "ml_anomaly_model.joblib"


def train(seed=0, n=6000):
    """Fit StandardScaler + EllipticEnvelope (robust covariance / Mahalanobis)
    on NORMAL samples only."""
    Xn = normal_samples(n, seed)
    model = make_pipeline(
        StandardScaler(),
        EllipticEnvelope(contamination=0.01, support_fraction=0.9, random_state=seed),
    )
    model.fit(Xn)
    return model


def evaluate(model, seed=42):
    Xn = normal_samples(2000, seed)
    Xf = fault_samples(2000, seed + 1)
    fpr = float((model.predict(Xn) == -1).mean())   # normal flagged as anomaly (bad)
    det = float((model.predict(Xf) == -1).mean())   # faults flagged as anomaly (good)
    return fpr, det


if __name__ == "__main__":
    model = train()
    fpr, det = evaluate(model)
    print(f"features: {FEATURES}")
    print(f"false-positive rate on normal: {fpr*100:.1f}%")
    print(f"detection rate on faults:      {det*100:.1f}%")
    joblib.dump({"model": model, "features": FEATURES}, MODEL_PATH)
    print(f"saved {MODEL_PATH}")
