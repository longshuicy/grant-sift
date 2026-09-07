FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 1000 app \
    && useradd -u 1000 -g app -m -s /bin/bash app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY grant_sift grant_sift
COPY config/sources.yaml config/prefilter.yaml config/roster.example.yaml config/
COPY web web
COPY scripts scripts
COPY run.py .
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh scripts/daily.sh \
    && mkdir -p /data \
    && chown -R app:app /app /data
# Real roster is mounted at deploy time (ConfigMap) or copied locally;
# never baked into the image.

ENV GRANT_SIFT_DB=/data/grant-sift.db \
    PYTHONUNBUFFERED=1

VOLUME ["/data"]
EXPOSE 8080
USER app

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "run.py", "serve", "--host", "0.0.0.0", "--port", "8080"]
