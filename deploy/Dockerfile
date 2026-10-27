# syntax=docker/dockerfile:1
FROM frappe/erpnext:v16.36.1@sha256:13d6d2889b8690b2d6c69cb562d63bd2990f42df8cff92db840043ae69034fef

USER root
COPY deploy/route-files.py /tmp/route-files.py
RUN python3 /tmp/route-files.py && rm /tmp/route-files.py
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg git build-essential pkg-config \
    && rm -rf /var/lib/apt/lists/*
USER frappe
WORKDIR /home/frappe/frappe-bench
COPY --chown=frappe:frappe deploy/suite.env /tmp/kanzlei/deploy/suite.env
COPY --chown=frappe:frappe deploy/*-mandant-access*.patch /tmp/kanzlei/deploy/
COPY --chown=frappe:frappe scripts/fetch-suite.sh /tmp/kanzlei/scripts/fetch-suite.sh
RUN rm -rf sites/assets \
    && ln -s /home/frappe/frappe-bench/assets sites/assets \
    && bash /tmp/kanzlei/scripts/fetch-suite.sh \
    && rm -rf /home/frappe/.cache/yarn /home/frappe/.cache/pip /home/frappe/.cache/uv
COPY --chown=frappe:frappe pyproject.toml README.md license.txt apps/kanzlei_erp/
COPY --chown=frappe:frappe kanzlei_erp/ apps/kanzlei_erp/kanzlei_erp/
# Assets must live in the image's baked directory, outside the persistent sites volume.
RUN ln -sfn /home/frappe/frappe-bench/assets sites/assets \
    && printf '\nkanzlei_erp\n' >> sites/apps.txt \
    && bench pip install -e apps/kanzlei_erp \
    && env/bin/python -c 'from importlib.metadata import version; assert version("jsonschema") == "4.26.0"' \
    && bench build --app kanzlei_erp --production \
    && env/bin/python -c 'import json; from pathlib import Path; assets=json.load(open("assets/assets.json")); assert "kanzlei_navigation.bundle.js" in assets; assert Path("assets/suite/frontend/index.html").is_file()' \
    && env/bin/python -c 'import kanzlei_erp; import sys; assert sys.version_info >= (3, 14)' \
    && rm -rf /home/frappe/.cache/yarn /home/frappe/.cache/pip /home/frappe/.cache/uv

# Changing provenance must not invalidate dependency or application layers.
ARG REVISION
LABEL org.opencontainers.image.source="https://github.com/nsivtsev/kanzlei-erp" \
      org.opencontainers.image.revision="${REVISION}"
