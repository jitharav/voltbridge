# VoltBridge ML anomaly detector image.
# Kept separate from the main lean image because scikit-learn/numpy/scipy are
# large — only this analytics service needs them; the real-time services stay lean.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt paho-mqtt scikit-learn numpy joblib

COPY . .

# default: run the ML anomaly detector (compose overrides broker host)
CMD ["python", "ml_anomaly_detector.py"]
