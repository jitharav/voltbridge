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
from fleet_common import KAFKA_TOPIC, fleet_aggregate, split_by_mode

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
            snapshot = dict(LATEST)
        dc, ev = split_by_mode(snapshot)
        agg_all = fleet_aggregate(snapshot)
        agg_dc = fleet_aggregate(dc)
        agg_ev = fleet_aggregate(ev)

        if self.path.startswith("/api"):
            body = json.dumps({
                "fleet": agg_all,
                "dc": {"summary": agg_dc, "nodes": sorted(dc.values(), key=lambda r: r["node"])[:200]},
                "ev": {"summary": agg_ev, "nodes": sorted(ev.values(), key=lambda r: r["node"])[:200]},
            }).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.end_headers(); self.wfile.write(body); return

        html = self._page(agg_all, agg_dc, dc, agg_ev, ev)
        self.send_response(200); self.send_header("Content-Type", "text/html")
        self.end_headers(); self.wfile.write(html.encode())

    def _section(self, title, sub, accent, agg, nodes_dict):
        nodes = sorted(nodes_dict.values(), key=lambda r: -r["power_kw"])[:80]
        maxp = max((r["power_kw"] for r in nodes), default=1) or 1
        total = max(1, agg["nodes"])
        hp = 100 * agg["healthy"] / total
        wp = 100 * agg["warning"] / total
        fp = 100 * agg["fault"] / total
        rows = ""
        for r in nodes:
            w = int(100 * r["power_kw"] / maxp)
            soc = f"<span class=soc>SoC {r['soc']:.0f}%</span>" if r.get("soc") is not None else "<span class=soc></span>"
            rows += (f"<div class=row><span class=nid>{r['node']}</span>"
                     f"<span class=bar><span class=fill style='width:{w}%;background:{accent}'></span></span>"
                     f"<span class=val>{r['power_kw']:.0f} kW</span>"
                     f"{soc}<span class=temp>{r['temp']:.0f}&deg;C</span>"
                     f"<span class='dot {r['health']}'></span></div>")
        return f"""<div class=card>
  <div class=chead><span class=ctitle style="color:{accent}">{title}</span><span class=csub>{sub}</span></div>
  <div class=kpis>
    <div class=kpi><div class=kv style="color:{accent}">{agg['nodes']}</div><div class=kl>nodes</div></div>
    <div class=kpi><div class=kv style="color:{accent}">{agg['total_power_mw']}</div><div class=kl>MW total</div></div>
    <div class=kpi><div class=kv style="color:{accent}">{agg['mean_temp']}</div><div class=kl>&deg;C mean</div></div>
  </div>
  <div class=health><span style="width:{hp}%" class=hh></span><span style="width:{wp}%" class=hw></span><span style="width:{fp}%" class=hf></span></div>
  <div class=hlabels><span class=lh>{agg['healthy']} healthy</span><span class=lw>{agg['warning']} warning</span><span class=lf>{agg['fault']} fault</span></div>
  <div class=colhead><span class=nid>node</span><span class=barh>power</span><span class=val>kW</span><span class=soc>SoC</span><span class=temp>temp</span><span class=doth></span></div>
  <div class=list>{rows}</div>
</div>"""

    def _page(self, agg_all, agg_dc, dc, agg_ev, ev):
        DC = "#3aa0ff"; EV = "#2dd4a7"
        dc_sec = self._section("DC RACKS", "data-center &middot; Redfish domain", DC, agg_dc, dc)
        ev_sec = self._section("EV CHARGERS", "fast-charge &middot; OCPP domain", EV, agg_ev, ev)
        return f"""<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=2>
<title>VoltBridge Fleet</title><style>
*{{box-sizing:border-box}} body{{font-family:'Segoe UI',Arial;margin:0;background:#0b0f14;color:#e9eef3}}
.top{{display:flex;align-items:center;gap:28px;padding:18px 26px;border-bottom:1px solid #1c2530;flex-wrap:wrap}}
.brand{{font-size:22px;font-weight:700;letter-spacing:1px}} .brand span{{color:#2dd4a7}}
.tot{{margin-left:auto;text-align:right}} .tot b{{font-size:30px;color:#f2b138}} .tot small{{display:block;color:#5f6b78;font-size:11px;letter-spacing:1px}}
.chip{{font-size:12px;color:#9fb0c0;border:1px solid #1c2530;border-radius:20px;padding:5px 12px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:18px;padding:20px 26px}}
.card{{background:#111823;border:1px solid #1c2530;border-radius:12px;padding:16px 18px}}
.chead{{display:flex;align-items:baseline;gap:10px;margin-bottom:12px}}
.ctitle{{font-size:16px;font-weight:700;letter-spacing:1px}} .csub{{font-size:11px;color:#5f6b78;letter-spacing:1px}}
.kpis{{display:flex;gap:26px;margin-bottom:14px}} .kv{{font-size:26px;font-weight:700}} .kl{{font-size:11px;color:#5f6b78;letter-spacing:1px}}
.health{{display:flex;height:10px;border-radius:6px;overflow:hidden;background:#1c2530}}
.hh{{background:#2dd4a7}} .hw{{background:#f0902e}} .hf{{background:#ff5470}}
.hlabels{{display:flex;gap:16px;margin:7px 0 12px;font-size:11px}} .lh{{color:#2dd4a7}} .lw{{color:#f0902e}} .lf{{color:#ff5470}}
.list{{max-height:360px;overflow-y:auto;padding-right:4px}}
.colhead{{display:flex;align-items:center;gap:10px;padding:2px 0 6px;font-size:10px;letter-spacing:1px;text-transform:uppercase;color:#5f6b78;border-bottom:1px solid #223}}
.colhead .bar,.colhead .barh{{flex:1}} .barh{{color:#5f6b78}}
.colhead .doth{{width:9px;flex:none}}
.row{{display:flex;align-items:center;gap:10px;padding:4px 0;font-size:12px;border-bottom:1px solid #161f2a}}
.nid{{width:74px;color:#9fb0c0;font-family:Consolas,monospace}}
.bar{{flex:1;height:8px;background:#1c2530;border-radius:4px;overflow:hidden}} .fill{{display:block;height:100%;border-radius:4px}}
.val{{width:62px;text-align:right;font-family:Consolas,monospace}}
.soc{{width:64px;text-align:right;color:#7f8ea0;font-family:Consolas,monospace}}
.temp{{width:44px;text-align:right;color:#9fb0c0;font-family:Consolas,monospace}}
.dot{{width:9px;height:9px;border-radius:50%;flex:none}} .dot.healthy{{background:#2dd4a7}} .dot.warning{{background:#f0902e}} .dot.fault{{background:#ff5470}}
.foot{{color:#5f6b78;font-size:11px;padding:0 26px 22px}}
@media(max-width:820px){{.grid{{grid-template-columns:1fr}}}}
</style>
<div class=top>
  <div class=brand>Volt<span>Bridge</span> &mdash; Fleet</div>
  <span class=chip>{agg_all['nodes']} nodes</span>
  <span class=chip>MQTT &rarr; Kafka &rarr; TimescaleDB</span>
  <div class=tot><b>{agg_all['total_power_mw']} MW</b><small>TOTAL FLEET POWER</small></div>
</div>
<div class=grid>{dc_sec}{ev_sec}</div>
<div class=foot>Live aggregation from Kafka &middot; per-node power bars, health, temperature &middot; auto-refresh 2s &middot; showing up to 80 nodes per domain</div>"""

def main():
    threading.Thread(target=consume, daemon=True).start()
    port = int(os.environ.get("PORT", "8090"))
    print(f"[consumer] fleet dashboard on http://localhost:{port}/")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()

if __name__ == "__main__":
    main()
