FROM docker.io/frappe/bench:latest
USER root
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg build-essential pkg-config \
    && rm -rf /var/lib/apt/lists/*
USER frappe
