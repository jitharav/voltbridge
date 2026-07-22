# VoltBridge bench + gateways + anomaly detector
# One image runs any of the Python services; docker-compose picks the command.
FROM python:3.12-slim

WORKDIR /app

# deps: real protocol stacks + MQTT/WebSocket transports
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt paho-mqtt websockets

# app code (bench, stacks, gateways, anomaly detector, DBC)
COPY . .

# default just prints help; docker-compose overrides `command:` per service
CMD ["python", "bench.py", "--help"]
