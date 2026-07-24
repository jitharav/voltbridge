#!/usr/bin/env python3
"""
Fleet simulator — spawns N virtual VoltBridge nodes, each publishing telemetry
to MQTT at a fixed rate. One process drives the whole simulated fleet.

    pip install paho-mqtt numpy
    NODES=100 HZ=5 MQTT_BROKER=localhost:1883 python fleet_simulator.py
"""
import json, os, sys, time
import numpy as np
from fleet_common import gen_telemetry, TELEMETRY_TOPIC_PREFIX

def main():
    nodes = int(os.environ.get("NODES", "100"))
    hz = float(os.environ.get("HZ", "5"))
    mqtt_broker = os.environ.get("MQTT_BROKER", "localhost:1883")
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        print("needs paho-mqtt"); sys.exit(1)

    host, _, port = mqtt_broker.partition(":"); port = int(port or 1883)
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
    except (AttributeError, TypeError):
        client = mqtt.Client()
    for attempt in range(30):
        try:
            client.connect(host, port, keepalive=30); break
        except Exception as e:
            print(f"[sim] broker not ready ({e}); retry {attempt+1}/30"); time.sleep(2)
    client.loop_start()
    print(f"[sim] {nodes} nodes @ {hz} Hz -> {mqtt_broker} ({TELEMETRY_TOPIC_PREFIX}/<node>/telemetry)")

    rng = np.random.default_rng(0)
    interval = 1.0 / hz
    tick = 0
    sent = 0
    try:
        while True:
            t0 = time.perf_counter()
            for nid in range(nodes):
                rec = gen_telemetry(nid, tick, rng)
                client.publish(f"{TELEMETRY_TOPIC_PREFIX}/{rec['node']}/telemetry",
                               json.dumps(rec), qos=0)
                sent += 1
            tick += 1
            if tick % 20 == 0:
                print(f"[sim] tick {tick} · {sent} messages published")
            dt = time.perf_counter() - t0
            if dt < interval:
                time.sleep(interval - dt)
    except KeyboardInterrupt:
        client.loop_stop(); print("\n[sim] stopped")

if __name__ == "__main__":
    main()
