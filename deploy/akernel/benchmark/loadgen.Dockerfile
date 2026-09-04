# syntax=docker/dockerfile:1.7

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /opt/akernel-benchmark

COPY sdk/python /tmp/akernel-sdk
RUN python -m pip install --no-cache-dir /tmp/akernel-sdk && \
    rm -rf /tmp/akernel-sdk

COPY sdk/python/benchmarks/cluster_throughput.py ./cluster_throughput.py

ENTRYPOINT ["python", "/opt/akernel-benchmark/cluster_throughput.py"]
