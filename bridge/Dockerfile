FROM python:3.12-alpine

RUN pip install --no-cache-dir aiohttp paho-mqtt>=2.0

COPY bridge.py /app/bridge.py

CMD ["python", "-u", "/app/bridge.py"]
