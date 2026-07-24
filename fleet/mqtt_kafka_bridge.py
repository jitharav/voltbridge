#!/usr/bin/env python3
"""
MQTT -> Kafka bridge (the aggregation point).
Subscribes to every node's MQTT telemetry at the edge and produces it to a
single partitioned Kafka topic, keyed by node id so a node's stream stays ordered
within its partition. This is the seam where edge (MQTT) meets platform (Kafka).

    pip install paho-mqtt kafka-python
    MQTT_BROKER=localhost:1883 KAFKA=localhost:9092 python mqtt_kafka_bridge.py
"""
import json, os, sys, time
from fleet_common import TELEMETRY_TOPIC_PREFIX, KAFKA_TOPIC

def main():
    mqtt_broker = os.environ.get("MQTT_BROKER", "localhost:1883")
    kafka = os.environ.get("KAFKA", "localhost:9092")
    try:
        import paho.mqtt.client as mqtt
        from kafka import KafkaProducer
    except ImportError as e:
        print(f"needs paho-mqtt + kafka-python ({e})"); sys.exit(1)

    producer = None
    for attempt in range(30):                      # wait for Kafka to be ready
        try:
            producer = KafkaProducer(
                bootstrap_servers=kafka,
                value_serializer=lambda v: json.dumps(v).encode(),
                key_serializer=lambda k: k.encode(),
                linger_ms=20, acks=1)
            break
        except Exception as e:
            print(f"[bridge] Kafka not ready ({e}); retry {attempt+1}/30"); time.sleep(2)
    if producer is None:
        print("[bridge] could not connect to Kafka"); sys.exit(1)
    print(f"[bridge] Kafka connected ({kafka}) -> topic {KAFKA_TOPIC}")

    stats = {"n": 0}
    host, _, port = mqtt_broker.partition(":"); port = int(port or 1883)
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
    except (AttributeError, TypeError):
        client = mqtt.Client()

    def on_connect(c, u, f, rc):
        c.subscribe(f"{TELEMETRY_TOPIC_PREFIX}/#")
        print(f"[bridge] subscribed to {TELEMETRY_TOPIC_PREFIX}/# on {mqtt_broker}")

    def on_message(c, u, msg):
        try:
            rec = json.loads(msg.payload.decode())
        except Exception:
            return
        node = rec.get("node", "unknown")
        producer.send(KAFKA_TOPIC, key=node, value=rec)
        stats["n"] += 1
        if stats["n"] % 2000 == 0:
            print(f"[bridge] forwarded {stats['n']} records -> Kafka")

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(host, port, keepalive=30)
    client.loop_forever()

if __name__ == "__main__":
    main()
