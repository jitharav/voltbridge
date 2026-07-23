#!/usr/bin/env python3
"""
Train the ML anomaly models — unsupervised, covariance-based (Mahalanobis
distance) outlier detectors that learn the shape of NORMAL operation per
operating envelope, and flag out-of-distribution samples.

    ml_anomaly_model_dc.joblib   800VDC rack — one model (steady state)
    ml_anomaly_model_ev.joblib   EV fast-charge — TWO sub-models split at
                                 SoC 80% (constant-current vs constant-voltage),
                                 because a charge has two genuinely different
                                 'normal' regimes.

    pip install scikit-learn numpy joblib
    python train_ml_anomaly.py            # trains and saves BOTH

Unsupervised (no fault labels) — each model learns its domain's normal as a
robust multivariate Gaussian; anything far in Mahalanobis distance is an anomaly.
Fault samples are used only to report detection rate. These complement (do not
replace) the transparent statistical detector in anomaly_detector.py.
"""
import joblib
from sklearn.covariance import EllipticEnvelope
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline

from synth_telemetry import (DOMAINS, FEATURES_DC, FEATURES_EV,
                             normal_samples_dc, fault_samples_dc,
                             normal_samples_ev, fault_samples_ev,
                             EV_SOC_SPLIT, EV_SOC_INDEX)

MODEL_PATHS = {"dc": "ml_anomaly_model_dc.joblib", "ev": "ml_anomaly_model_ev.joblib"}


def _ee(seed):
    return make_pipeline(
        StandardScaler(),
        EllipticEnvelope(contamination=0.01, support_fraction=0.9, random_state=seed),
    )


def train_dc(seed=0, n=6000):
    return {"kind": "single", "model": _ee(seed).fit(normal_samples_dc(n, seed)),
            "features": FEATURES_DC, "domain": "dc"}


def train_ev(seed=0, n=9000):
    X = normal_samples_ev(n, seed)
    cc = X[:, EV_SOC_INDEX] < EV_SOC_SPLIT
    return {"kind": "split", "domain": "ev", "features": FEATURES_EV,
            "soc_index": EV_SOC_INDEX, "soc_split": EV_SOC_SPLIT,
            "cc": _ee(seed).fit(X[cc]), "cv": _ee(seed).fit(X[~cc])}


def predict(bundle, X):
    """Unified predict: +1 normal, -1 anomaly. Handles single or split models."""
    import numpy as np
    if bundle["kind"] == "single":
        return bundle["model"].predict(X)
    out = np.ones(len(X), dtype=int)
    idx, split = bundle["soc_index"], bundle["soc_split"]
    m = X[:, idx] < split
    if m.any():
        out[m] = bundle["cc"].predict(X[m])
    if (~m).any():
        out[~m] = bundle["cv"].predict(X[~m])
    return out


def evaluate(bundle, normal_fn, fault_fn, seed=42):
    fpr = float((predict(bundle, normal_fn(2000, seed)) == -1).mean())
    det = float((predict(bundle, fault_fn(2000, seed + 1)) == -1).mean())
    return fpr, det


if __name__ == "__main__":
    dc = train_dc()
    fpr, det = evaluate(dc, normal_samples_dc, fault_samples_dc)
    print(f"[dc] {FEATURES_DC}")
    print(f"[dc] false-positive rate: {fpr*100:.1f}%   detection rate: {det*100:.1f}%")
    joblib.dump(dc, MODEL_PATHS["dc"]); print(f"[dc] saved {MODEL_PATHS['dc']}\n")

    ev = train_ev()
    fpr, det = evaluate(ev, normal_samples_ev, fault_samples_ev)
    print(f"[ev] {FEATURES_EV}  (split at SoC {EV_SOC_SPLIT:.0f}%: CC / CV)")
    print(f"[ev] false-positive rate: {fpr*100:.1f}%   detection rate: {det*100:.1f}%")
    joblib.dump(ev, MODEL_PATHS["ev"]); print(f"[ev] saved {MODEL_PATHS['ev']}")
