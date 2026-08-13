FROM python:3.11-slim

ARG COBENCH_REPOSITORY=https://github.com/sunnweiwei/CO-Bench.git
ARG COBENCH_COMMIT=2f6620ba5cc8d80d6fefc121b5ee2c4dcec29f33

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       build-essential git libglpk-dev glpk-utils \
    && rm -rf /var/lib/apt/lists/*

RUN git clone "${COBENCH_REPOSITORY}" /opt/co-bench \
    && git -C /opt/co-bench checkout "${COBENCH_COMMIT}" \
    && pip install --no-cache-dir -r /opt/co-bench/requirements.txt

WORKDIR /tmp
