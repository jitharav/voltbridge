#!/usr/bin/env python3
"""
Fleet consumer — consumes the Kafka telemetry topic, writes to a time-series
database (TimescaleDB/Postgres), maintains per-node latest state + fleet KPIs,
and serves a small fleet dashboard (aggregate health + per-node table) on :8090.

Scale horizontally by running multiple instances in the same Kafka consumer group
(GROUP env) — partitions are shared across instances. That is the linear-scale path.

    pip install kafka-python psycopg2-binary
    KAFKA=localhost:9092 DATABASE_URL=postgresql://... python fleet_consumer.py
"""
import json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from fleet_common import KAFKA_TOPIC, fleet_aggregate

LATEST = {}
LOCK = threading.Lock()

def db_connect(url):
    try:
        import psycopg2
    except ImportError:
        print("[consumer] psycopg2 not installed — running WITHOUT TSDB (in-memory only)")
        return None
    for attempt in range(30):
        try:
            conn = psycopg2.connect(url); conn.autocommit = True
            cur = conn.cursor()
            cur.execute("""CREATE TABLE IF NOT EXISTS telemetry (
                ts TIMESTAMPTZ NOT NULL DEFAULT now(), node TEXT, mode TEXT,
                v_bus DOUBLE PRECISION, i_bus DOUBLE PRECISION, power_kw DOUBLE PRECISION,
                temp DOUBLE PRECISION, soc DOUBLE PRECISION, eff DOUBLE PRECISION, health TEXT);""")
            try:
                cur.execute("SELECT create_hypertable('telemetry','ts',if_not_exists=>TRUE);")
            except Exception:
                pass                                # plain Postgres if Timescale absent
            print("[consumer] TSDB connected"); return conn
        except Exception as e:
            print(f"[consumer] DB not ready ({e}); retry {attempt+1}/30"); time.sleep(2)
    return None

def consume():
    kafka = os.environ.get("KAFKA", "localhost:9092")
    group = os.environ.get("GROUP", "fleet-consumers")
    db_url = os.environ.get("DATABASE_URL")
    try:
        from kafka import KafkaConsumer
    except ImportError:
        print("needs kafka-python"); sys.exit(1)
    conn = db_connect(db_url) if db_url else None
    cur = conn.cursor() if conn else None

    consumer = None
    for attempt in range(30):
        try:
            consumer = KafkaConsumer(
                KAFKA_TOPIC, bootstrap_servers=kafka, group_id=group,
                value_deserializer=lambda v: json.loads(v.decode()),
                auto_offset_reset="latest", enable_auto_commit=True)
            break
        except Exception as e:
            print(f"[consumer] Kafka not ready ({e}); retry {attempt+1}/30"); time.sleep(2)
    if consumer is None:
        print("[consumer] could not connect to Kafka"); sys.exit(1)
    print(f"[consumer] consuming {KAFKA_TOPIC} (group {group}) from {kafka}")

    n = 0
    for msg in consumer:
        rec = msg.value
        with LOCK:
            LATEST[rec["node"]] = rec
        if cur:
            try:
                cur.execute("""INSERT INTO telemetry(node,mode,v_bus,i_bus,power_kw,temp,soc,eff,health)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (rec["node"], rec["mode"], rec["v_bus"], rec["i_bus"], rec["power_kw"],
                     rec["temp"], rec.get("soc"), rec["eff"], rec["health"]))
            except Exception:
                pass
        n += 1
        if n % 2000 == 0:
            print(f"[consumer] stored {n} records")

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        with LOCK:
            agg = fleet_aggregate(LATEST)
            nodes = sorted(LATEST.values(), key=lambda r: r["node"])[:200]
        if self.path.startswith("/api"):
            body = json.dumps({"fleet": agg, "nodes": nodes}).encode()
            self.send_response(200); self.send_header("Content-Type","application/json")
            self.end_headers(); self.wfile.write(body); return
        rows = "".join(
            f"<tr><td>{r['node']}</td><td>{r['mode']}</td><td>{r['power_kw']}</td>"
            f"<td>{r['temp']}</td><td class='{r['health']}'>{r['health']}</td></tr>" for r in nodes)
        html = f"""<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=2>
<title>VoltBridge Fleet</title><style>
body{{font-family:Segoe UI,Arial;margin:24px;background:#0b0f14;color:#e9eef3}}
h1{{color:#2dd4a7}} .kpi{{display:inline-block;margin:6px 22px 14px 0}}
.kpi b{{font-size:26px;color:#f2b138}} table{{border-collapse:collapse;width:100%;margin-top:10px}}
td,th{{border-bottom:1px solid #223;padding:5px 10px;text-align:left;font-size:13px}}
.healthy{{color:#2dd4a7}} .warning{{color:#f0902e}} .fault{{color:#ff5470}}</style>
<h1>VoltBridge — Fleet Dashboard</h1>
<div class=kpi>nodes<br><b>{agg['nodes']}</b></div>
<div class=kpi>total power<br><b>{agg['total_power_mw']} MW</b></div>
<div class=kpi>healthy<br><b>{agg['healthy']}</b></div>
<div class=kpi>warning<br><b>{agg['warning']}</b></div>
<div class=kpi>fault<br><b>{agg['fault']}</b></div>
<div class=kpi>mean temp<br><b>{agg['mean_temp']} C</b></div>
<table><tr><th>node</th><th>mode</th><th>power kW</th><th>temp C</th><th>health</th></tr>{rows}</table>
<p style='color:#5f6b78'>Aggregated from Kafka · showing up to 200 nodes · auto-refresh 2s</p>"""
        self.send_response(200); self.send_header("Content-Type","text/html")
        self.end_headers(); self.wfile.write(html.encode())

def main():
    threading.Thread(target=consume, daemon=True).start()
    port = int(os.environ.get("PORT", "8090"))
    print(f"[consumer] fleet dashboard on http://localhost:{port}/")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()

if __name__ == "__main__":
    main()
