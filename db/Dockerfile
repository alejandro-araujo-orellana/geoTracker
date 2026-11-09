FROM timescale/timescaledb-ha:pg15-latest

USER root
RUN apt-get update && \
    apt-get install -y postgresql-15-pgrouting && \
    rm -rf /var/lib/apt/lists/*
USER postgres

