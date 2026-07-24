#!/usr/bin/env python3
"""
Fleet scaling harness — runs the aggregation pipeline IN-PROCESS (no external
infrastructure) so it produces real throughput/latency numbers anywhere.

It spawns N virtual nodes publishing telemetry into a queue, and an aggregator
that maintains per-node latest state and fleet KPIs — the same logic the Kafka
consumer runs. It reports how ingest throughput and end-to-end latency behave as
the node count ramps.

    python scale_test.py                 # default ramp
    python scale_test.py --nodes 10 100 500 --hz 5 --secs 3

NOTE ON SCALING: a single Python consumer is one worker (GIL-bound), so it has a
ceiling. The point of the Kafka design is that the topic is *partitioned* — you
add consumer instances to scale horizontally past one worker. This harness shows
the single-worker curve; the compose/k8s pipeline shows the horizontal path.
"""
import argparse, queue, statistics, threading, time
import numpy as np
from fleet_common import gen_telemetry, fleet_aggregate


def run_once(n_nodes, hz, secs):
    q = queue.Queue(maxsize=500000)
    latest = {}
    lat = []
    processed = [0]
    stop = threading.Event()

    def aggregator():
        while not stop.is_set() or not q.empty():
            try:
                rec = q.get(timeout=0.05)
            except queue.Empty:
                continue
            lat.append(time.perf_counter() - rec["_enq"])
            latest[rec["node"]] = rec
            processed[0] += 1
            if processed[0] % 5000 == 0:
                fleet_aggregate(latest)          # exercise the rollup periodically

    t = threading.Thread(target=aggregator)
    t.start()

    rng = np.random.default_rng(0)
    ticks = int(hz * secs)
    interval = 1.0 / hz
    produced = 0
    start = time.perf_counter()
    for tk in range(ticks):
        t0 = time.perf_counter()
        for nid in range(n_nodes):
            rec = gen_telemetry(nid, tk, rng)
            rec["_enq"] = time.perf_counter()
            q.put(rec)
            produced += 1
        dt = time.perf_counter() - t0
        if dt < interval:
            time.sleep(interval - dt)
    stop.set()
    t.join()
    dur = time.perf_counter() - start
    lat_sorted = sorted(lat)
    return {
        "nodes": n_nodes,
        "offered_msg_s": round(n_nodes * hz),
        "produced": produced,
        "processed": processed[0],
        "throughput_msg_s": round(processed[0] / dur),
        "lat_ms_mean": round(1000 * statistics.mean(lat), 2),
        "lat_ms_p95": round(1000 * lat_sorted[max(0, int(len(lat) * 0.95) - 1)], 2),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nodes", type=int, nargs="+", default=[10, 50, 100, 250, 500, 1000])
    ap.add_argument("--hz", type=int, default=5)
    ap.add_argument("--secs", type=int, default=3)
    ap.add_argument("--check", action="store_true",
                    help="CI mode: assert the pipeline ingests correctly, no benchmark thresholds")
    args = ap.parse_args()

    if args.check:
        # Fast, machine-independent correctness check for CI.
        r = run_once(100, hz=5, secs=1)
        loss = 1.0 - (r["processed"] / max(1, r["produced"]))
        print(f"[check] produced={r['produced']} processed={r['processed']} "
              f"loss={loss*100:.2f}% lat_p95={r['lat_ms_p95']}ms")
        assert r["produced"] > 0, "no telemetry produced"
        assert loss < 0.02, f"too much telemetry lost in aggregation: {loss*100:.1f}%"
        print("ok: fleet aggregation ingests telemetry without loss")
        return

    print(f"Fleet scaling harness — {args.hz} Hz/node, {args.secs}s per run\n")
    hdr = f"{'nodes':>6} {'offered/s':>10} {'ingested/s':>11} {'lat_mean_ms':>12} {'lat_p95_ms':>11}"
    print(hdr); print("-" * len(hdr))
    rows = []
    for n in args.nodes:
        r = run_once(n, args.hz, args.secs)
        rows.append(r)
        print(f"{r['nodes']:>6} {r['offered_msg_s']:>10} {r['throughput_msg_s']:>11} "
              f"{r['lat_ms_mean']:>12} {r['lat_ms_p95']:>11}")
    # save CSV
    import csv
    with open("scale_results.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
    print("\nsaved scale_results.csv")


if __name__ == "__main__":
    main()
