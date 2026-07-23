#!/usr/bin/env python3
"""
Synthetic telemetry generators for the ML anomaly models (DC rack + EV charge).

WHERE THE DATA COMES FROM: it is generated here in code — nothing is downloaded.
An unsupervised anomaly model learns the shape of *normal* operation, and we do
not yet have a corpus of logged bench runs, so we synthesise normal samples that
reproduce the bench's own physics for each domain (see bench.py). The models
train on NORMAL ONLY; the fault sets are used purely to measure detection rate.
The same feature vectors let you retrain on REAL logged telemetry later with no
code changes — the data's authority comes from the bench physics, not a dataset.

DC rack (steady, near full load) — one model:
    features: v_bus, i_bus, power_kw, temp, rect_temp, eff
EV charge — a CC-CV trajectory with two regimes, so the model is split at
SoC 80% (constant-current vs constant-voltage). Pack-agnostic: voltage as a
ratio to pack, current as a fraction of a pack-typical maximum, so 400 V and
800 V share one 'normal' cloud.
    features: v_ratio, i_frac, temp, soc, eff        (soc also routes CC vs CV)
"""
import numpy as np

FEATURES_DC = ["v_bus", "i_bus", "power_kw", "temp", "rect_temp", "eff"]
FEATURES_EV = ["v_ratio", "i_frac", "temp", "soc", "eff"]
FEATURES = FEATURES_DC          # backward-compatible alias (DC was first)
EV_SOC_SPLIT = 80.0             # CC below, CV at/above
EV_SOC_INDEX = 3               # index of soc in FEATURES_EV


def _clip(x, a, b):
    return np.minimum(np.maximum(x, a), b)


def _imax_typ(pack):
    """Pack-typical peak current used to normalise i_frac (reference, not the
    actual per-car max) so 400 V and 800 V charges share one cloud."""
    return np.where(pack == 400.0, 550.0, 370.0)


# --------------------------------------------------------------------------- DC
def normal_samples_dc(n, seed=0):
    rng = np.random.default_rng(seed)
    lf = _clip(rng.normal(1.0, 0.05, n), 0.82, 1.12)
    v = rng.normal(800.0, 2.0, n)
    i = _clip(1320.0 * lf + rng.normal(0, 10, n), 1150, 1420)
    power = v * i / 1000.0
    temp = _clip(42.0 + 22.0 * lf + rng.normal(0, 0.6, n), 55, 74)
    rect = temp - rng.normal(0.8, 0.3, n)
    eff = _clip(95.3 - (lf - 1.0) * 1.0 + rng.normal(0, 0.08, n), 94.5, 96.0)
    return np.column_stack([v, i, power, temp, rect, eff])


def fault_samples_dc(n, seed=1):
    rng = np.random.default_rng(seed)
    X = normal_samples_dc(n, seed + 99).copy()
    kind = rng.integers(0, 5, n)
    for k in range(n):
        t = kind[k]
        if t == 0:
            X[k, 3] = rng.uniform(88, 112); X[k, 4] = X[k, 3] - rng.uniform(0, 3); X[k, 5] = rng.uniform(90, 94)
        elif t == 1:
            X[k, 0] = rng.uniform(905, 965); X[k, 2] = X[k, 0] * X[k, 1] / 1000.0
        elif t == 2:
            X[k, 1] = rng.uniform(1650, 1950); X[k, 2] = X[k, 0] * X[k, 1] / 1000.0
        elif t == 3:
            X[k, 5] = rng.uniform(80, 88)
        else:
            X[k, 3] = rng.uniform(78, 87); X[k, 4] = X[k, 3] - rng.uniform(0, 2); X[k, 5] = rng.uniform(88, 92)
    return X


# --------------------------------------------------------------------------- EV
def normal_samples_ev(n, seed=0):
    """Normal EV fast-charge along the CC-CV curve, pack-agnostic."""
    rng = np.random.default_rng(seed)
    pack = rng.choice([400.0, 800.0], n)
    i_max = np.where(pack == 400.0, rng.uniform(450, 625, n), rng.uniform(300, 440, n))
    soc = rng.uniform(20, 100, n)
    cc = soc < EV_SOC_SPLIT
    frac = np.where(cc, 1.0, np.maximum(0.08, 1 - (soc - 80) / 20 * 0.9)) * rng.uniform(0.95, 1.02, n)
    i_frac = (i_max * frac) / _imax_typ(pack)
    v_ratio = (pack * rng.normal(1.0, 0.004, n)) / pack
    temp = _clip(30 + frac * 18 + (soc / 100.0) * 8 + rng.normal(0, 0.7, n), 28, 66)
    eff = _clip(97.3 - frac * 1.4 + rng.normal(0, 0.1, n), 95.5, 97.6)
    return np.column_stack([v_ratio, i_frac, temp, soc, eff])


def fault_samples_ev(n, seed=1):
    rng = np.random.default_rng(seed)
    X = normal_samples_ev(n, seed + 99).copy()
    kind = rng.integers(0, 5, n)
    for k in range(n):
        t = kind[k]
        if t == 0:      # over-voltage vs pack
            X[k, 0] = rng.uniform(1.12, 1.25)
        elif t == 1:    # over-current
            X[k, 1] = rng.uniform(1.35, 2.2)
        elif t == 2:    # over-temperature
            X[k, 2] = rng.uniform(72, 95)
        elif t == 3:    # efficiency collapse
            X[k, 4] = rng.uniform(88, 94)
        else:           # not tapering: high current while near full
            X[k, 3] = rng.uniform(88, 99); X[k, 1] = rng.uniform(0.7, 1.1)
    return X


DOMAINS = {
    "dc": (normal_samples_dc, fault_samples_dc, FEATURES_DC),
    "ev": (normal_samples_ev, fault_samples_ev, FEATURES_EV),
}

# backward-compatible names used by the original DC-only scripts
normal_samples = normal_samples_dc
fault_samples = fault_samples_dc
