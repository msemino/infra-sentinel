# Sentinel runs on the Python standard library only, so the image stays tiny.
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY sentinel ./sentinel
COPY mocks ./mocks
COPY sample_data ./sample_data

RUN pip install --no-cache-dir . && useradd -m sentinel
USER sentinel

# Default command runs one watchdog cycle. The compose file loops it for the demo.
CMD ["python", "-m", "sentinel.cli"]
