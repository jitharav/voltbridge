# VoltBridge Fleet PoC — cloud-native telemetry pipeline

A working demonstrator of how VoltBridge nodes aggregate at the edge and scale
into a cloud-native backend: **MQTT (edge) → bridge → Kafka → time-series DB →
fleet aggregation/dashboard**, with a multi-node simulator, a scaling harness,
Docker Compose, and Kubernetes manifests.

> **Honest scope.** This is a *local demonstrator of the pipeline and its scaling
> behaviour*, not a deployed hyperscaler system. It uses the same components you
> would run in the cloud (MQTT, Kafka, TimescaleDB, Kubernetes) and the identical
> data path; the k8s manifests are the deploy target. Numbers below are measured
> on a single machine — they demonstrate the pipeline scales, not a production SLA.

## The pipeline
```
 N nodes ──MQTT──► broker ──► mqtt_kafka_bridge ──► Kafka (partitioned) ──► consumers ──► TimescaleDB
 (simulator)       (edge)     (aggregation seam)    (durable backbone)       (group)       + fleet dashboard :8090
```
- **Edge:** each node publishes to `voltbridge/fleet/<node>/telemetry` (MQTT).
- **Bridge:** subscribes to all nodes, produces to one Kafka topic keyed by node.
- **Backbone:** Kafka — durable, partitioned, replayable.
- **Consumers:** a Kafka consumer group; add instances to scale horizontally
  (partitions are shared across them). Each writes to TimescaleDB and serves the
  aggregate fleet view.

## Run it (one command — Docker)
```
docker compose -f docker-compose.fleet.yml up --build
# open the fleet dashboard:
#   http://localhost:8090/
```
Scale the simulated fleet:
```
NODES=500 docker compose -f docker-compose.fleet.yml up --build
```
Scale consumers horizontally (the linear-scale path — share Kafka partitions):
```
docker compose -f docker-compose.fleet.yml up --build --scale consumer=3
```
Inspect the time-series data:
```
docker exec -it vbf-tsdb psql -U postgres -d fleet -c "SELECT count(*), max(ts) FROM telemetry;"
```

## Scaling evidence (no infrastructure required)
`scale_test.py` runs the aggregation pipeline **in-process** and measures ingest
throughput and end-to-end latency as the node count ramps — so anyone can
reproduce the curve without Kafka:
```
python scale_test.py
```
Measured on a single machine (5 Hz/node, 3 s/run):

| nodes | offered msg/s | ingested msg/s | mean latency | p95 latency |
|------:|--------------:|---------------:|-------------:|------------:|
|    10 |            50 |             50 |     0.12 ms  |    0.24 ms  |
|   100 |           500 |            499 |     0.72 ms  |    1.19 ms  |
|   500 |         2,500 |          2,491 |     3.34 ms  |    5.77 ms  |
| 1,000 |         5,000 |          4,972 |     4.13 ms  |    7.99 ms  |

Ingest tracks offered load with single-digit-millisecond latency to ~1,000 nodes
on one worker. **Beyond one worker**, the Kafka topic is partitioned, so you add
consumer instances (Compose `--scale consumer=N`, or the k8s HPA below) to scale
throughput horizontally — the standard cloud pattern.

## Deploy to Kubernetes (the hyperscaler path)
```
kubectl apply -f k8s/
kubectl -n voltbridge-fleet get pods
# dashboard via the fleet-dashboard LoadBalancer service
```
`k8s/40-consumer.yaml` runs 3 consumer replicas behind a Service, with a
HorizontalPodAutoscaler (3→20 on CPU). In a real deployment you would swap the
single-node Kafka for a managed cluster (MSK / Confluent / Strimzi) and TimescaleDB
for a managed instance.

## Files
```
fleet_common.py         telemetry generation + health + fleet rollup (shared)
fleet_simulator.py      spawns N virtual nodes -> MQTT
mqtt_kafka_bridge.py    MQTT -> Kafka aggregation bridge
fleet_consumer.py       Kafka -> TimescaleDB + fleet dashboard (:8090)
scale_test.py           in-process scaling harness (-> scale_results.csv)
docker-compose.fleet.yml   broker + kafka + timescaledb + bridge + simulator + consumer
Dockerfile.fleet        image for the fleet services
fleet-mosquitto.conf    broker config
k8s/                    namespace, kafka, broker, bridge+simulator, consumer+HPA
requirements-fleet.txt  paho-mqtt · kafka-python · numpy · psycopg2-binary
```

## What this proves — and what it doesn't
**Proves:** the aggregation bridge and a cloud-native pipeline (MQTT→Kafka→TSDB)
work end to end; ingest scales near-linearly with node count on one worker; the
design scales horizontally via Kafka partitions + a consumer group; and it is
deployable to Kubernetes.
**Does not prove:** production behaviour on a managed hyperscaler cluster at large
N under real network conditions — that is Tier 2/3 (managed Kafka, load test,
SLA-driven tuning) and requires cloud provisioning and sign-off.
