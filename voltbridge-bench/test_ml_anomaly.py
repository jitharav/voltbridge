#!/usr/bin/env python3
"""Offline self-test for BOTH ML anomaly models (DC rack + EV charge). Trains on
synthetic normal, asserts low false-positive rate on unseen normal and high
detection on unseen faults, for each domain. Deterministic; no broker. CI-used."""
import sys

try:
    import numpy as np
    from train_ml_anomaly import train_dc, train_ev, predict
    from synth_telemetry import (normal_samples_dc, fault_samples_dc,
                                 normal_samples_ev, fault_samples_ev)
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
    # ---- DC ----
    dc = train_dc(seed=0)
    fpr = float((predict(dc, normal_samples_dc(3000, 7)) == -1).mean())
    det = float((predict(dc, fault_samples_dc(3000, 8)) == -1).mean())
    print(f"[dc] FPR={fpr*100:.1f}%  detection={det*100:.1f}%")
    ok(fpr < 0.05, f"DC false-positive rate under 5% (got {fpr*100:.1f}%)")
    ok(det > 0.95, f"DC detection over 95% (got {det*100:.1f}%)")
    ok(int(predict(dc, np.array([[800., 1320., 1056., 64., 63.2, 95.3]]))[0]) == 1,
       "DC nominal point normal")
    ok(int(predict(dc, np.array([[940., 1320., 1240., 64., 63.2, 95.3]]))[0]) == -1,
       "DC over-voltage point anomaly")

    # ---- EV ----
    ev = train_ev(seed=0)
    fpr = float((predict(ev, normal_samples_ev(3000, 7)) == -1).mean())
    det = float((predict(ev, fault_samples_ev(3000, 8)) == -1).mean())
    print(f"[ev] FPR={fpr*100:.1f}%  detection={det*100:.1f}%")
    ok(fpr < 0.05, f"EV false-positive rate under 5% (got {fpr*100:.1f}%)")
    ok(det > 0.95, f"EV detection over 95% (got {det*100:.1f}%)")
    # nominal CC point (soc 50, full current) normal; not-tapering (soc 95, full current) anomaly
    ok(int(predict(ev, np.array([[1.00, 1.00, 52., 50., 95.9]]))[0]) == 1,
       "EV nominal CC point normal")
    ok(int(predict(ev, np.array([[1.00, 0.95, 60., 95., 96.0]]))[0]) == -1,
       "EV not-tapering point anomaly")

    print(f"\nAll {checks} ML anomaly checks passed (DC + EV).")


if __name__ == "__main__":
    main()
