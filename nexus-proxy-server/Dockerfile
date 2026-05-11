FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC \
    NEXUS_HOST=0.0.0.0 \
    NEXUS_PORT=9800 \
    NEXUS_UPSTREAM_URL=https://example.com/api/optimize \
    NEXUS_WC_SECRET=change-me

COPY server.py .
COPY user-agents.txt .
COPY static/ ./static/

RUN mkdir -p /app/data

EXPOSE 9800

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os, urllib.request; port=os.getenv('NEXUS_PORT', os.getenv('PORT', '9800')); urllib.request.urlopen(f'http://127.0.0.1:{port}/v1/models', timeout=3).read()" || exit 1

CMD ["python", "-u", "server.py"]
