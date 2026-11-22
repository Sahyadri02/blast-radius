# Same base image the organizers labeled with.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
# sqlglot/ source at the pinned commit eb11aec4 (MIT, from github.com/tobymao/sqlglot),
# used to read the original files that a patch changes. No network needed.
COPY sqlglot-src.tar.gz /tmp/
RUN mkdir -p /opt/sqlglot && tar -xzf /tmp/sqlglot-src.tar.gz -C /opt/sqlglot \
    && rm /tmp/sqlglot-src.tar.gz \
    && python -m compileall -q /opt/sqlglot/sqlglot \
    && chmod -R a+rX /opt/sqlglot
COPY selector.py map.json.gz fp.py brtrack.py /app/
# Per-table fingerprint of the original import-time state, computed once at build time.
RUN PYTHONHASHSEED=0 python -B /app/fp.py --detail /opt/sqlglot > /app/fp_detail.json \
    && chmod -R a+rX /app
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "/app/selector.py"]
