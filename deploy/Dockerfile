# Container image for the karaoke library API (read-only Postgres backend).
# Deliberately excludes desktop tooling: playback lives in the host-side
# control API (karaoke.ctrl_api), not in this image.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    KARAOKE_DATA_DIR=/data

WORKDIR /app

# Only the runtime deps the library API actually needs. The full project
# dependency set (torch, whisper, librosa...) is irrelevant here and would
# balloon the image.
RUN pip install --no-cache-dir \
        "fastapi>=0.110.0" \
        "uvicorn[standard]>=0.28.0" \
        "requests>=2.32" \
        "rich>=13.7" \
        "psycopg[binary,pool]>=3.0.0"

COPY src/karaoke /app/karaoke

RUN useradd --create-home --uid 10001 karaoke \
    && mkdir -p /data \
    && chown -R karaoke:karaoke /app /data
USER karaoke

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"

CMD ["python", "-m", "uvicorn", "karaoke.api:app", "--host", "0.0.0.0", "--port", "8000"]
