#!/usr/bin/env python3
"""Offline self-test for the ML anomaly model — trains on synthetic normal,
then asserts a low false-positive rate on unseen normal telemetry and a high
detection rate on unseen faults. Deterministic (seeded); no broker needed.
Used by CI."""
import sys

try:
    from train_ml_anomaly import train, evaluate
    from synth_telemetry import normal_samples, fault_samples
except ImportError as e:
    print(f"skip: ML deps not available ({e})")
    sys.exit(0)

checks = 0


def ok(cond, msg):
    global checks
    checks += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)
    print(f"ok: {msg}")


def main():
    model = train(seed=0)

    # held-out (different seeds) evaluation
    fpr = float((model.predict(normal_samples(3000, seed=7)) == -1).mean())
    det = float((model.predict(fault_samples(3000, seed=8)) == -1).mean())
    print(f"false-positive rate: {fpr*100:.1f}%   detection rate: {det*100:.1f}%")

    ok(fpr < 0.05, f"false-positive rate under 5% (got {fpr*100:.1f}%)")
    ok(det > 0.95, f"detection rate over 95% (got {det*100:.1f}%)")

    # a clearly-normal point is an inlier; a clear over-voltage point is an outlier
    import numpy as np
    normal_pt = np.array([[800.0, 1320.0, 1056.0, 64.0, 63.2, 95.3]])
    ov_pt = np.array([[940.0, 1320.0, 1240.0, 64.0, 63.2, 95.3]])
    ok(int(model.predict(normal_pt)[0]) == 1, "nominal point classified normal")
    ok(int(model.predict(ov_pt)[0]) == -1, "over-voltage point classified anomaly")

    print(f"\nAll {checks} ML anomaly checks passed.")


if __name__ == "__main__":
    main()
