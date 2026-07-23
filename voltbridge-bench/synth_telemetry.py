#!/usr/bin/env python3
"""
Synthetic telemetry generator for the ML anomaly model (DC / rack mode).

WHY SYNTHETIC: an unsupervised anomaly model learns the shape of *normal*
operation. We don't yet have a corpus of logged bench runs, so we generate
normal-operation samples that reproduce the bench's real steady-state physics
and correlations (from bench.py) — then, only for evaluation, we generate
out-of-envelope fault samples. The model trains on NORMAL ONLY; the fault set
is used purely to measure detection rate. Once real deployments log telemetry,
the same feature vector lets you retrain on real data with no code changes.

Feature vector (all taken directly from bench telemetry, DC mode):
    v_bus (V) · i_bus (A) · power_kw (kW) · temp (degC) · rect_temp (degC) · eff (%)
"""
import numpy as np

FEATURES = ["v_bus", "i_bus", "power_kw", "temp", "rect_temp", "eff"]


def _clip(x, a, b):
    return np.minimum(np.maximum(x, a), b)


def normal_samples(n, seed=0):
    """Normal 800VDC-rack operation during energy transfer, with realistic
    ripple and correlations (the bench runs near full load; storage buffers
    transients so grid current stays ~flat)."""
    rng = np.random.default_rng(seed)
    lf = _clip(rng.normal(1.0, 0.05, n), 0.82, 1.12)          # load fraction ~ full
    v = rng.normal(800.0, 2.0, n)                             # 800 V bus + ripple
    i = _clip(1320.0 * lf + rng.normal(0, 10, n), 1150, 1420) # grid current (flat, buffered)
    power = v * i / 1000.0                                    # kW = V*A/1000
    temp = _clip(42.0 + 22.0 * lf + rng.normal(0, 0.6, n), 55, 74)   # module ~64 C
    rect = temp - rng.normal(0.8, 0.3, n)                    # rectifier tracks module
    eff = _clip(95.3 - (lf - 1.0) * 1.0 + rng.normal(0, 0.08, n), 94.5, 96.0)  # e2e efficiency
    return np.column_stack([v, i, power, temp, rect, eff])


def fault_samples(n, seed=1):
    """Out-of-envelope samples for EVALUATION ONLY (never used in training).
    A mix of single-signal faults and one subtle multivariate case."""
    rng = np.random.default_rng(seed)
    X = normal_samples(n, seed + 99).copy()      # start from normal, perturb
    kind = rng.integers(0, 5, n)
    for k in range(n):
        t = kind[k]
        if t == 0:      # over-temperature
            X[k, 3] = rng.uniform(88, 112)
            X[k, 4] = X[k, 3] - rng.uniform(0, 3)
            X[k, 5] = rng.uniform(90, 94)
        elif t == 1:    # over-voltage
            X[k, 0] = rng.uniform(905, 965)
            X[k, 2] = X[k, 0] * X[k, 1] / 1000.0
        elif t == 2:    # over-current
            X[k, 1] = rng.uniform(1650, 1950)
            X[k, 2] = X[k, 0] * X[k, 1] / 1000.0
        elif t == 3:    # efficiency collapse
            X[k, 5] = rng.uniform(80, 88)
        else:           # subtle multivariate: v/i look normal, but temp high AND eff low
            X[k, 3] = rng.uniform(78, 87)
            X[k, 4] = X[k, 3] - rng.uniform(0, 2)
            X[k, 5] = rng.uniform(88, 92)
    return X
