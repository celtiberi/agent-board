FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AGENT_BOARD_DATA_DIR=/data \
    AGENT_BOARD_BIND=0.0.0.0:8790

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml README.md ./
COPY migrations ./migrations
COPY schemas ./schemas
COPY src ./src

RUN uv sync --no-dev --python 3.12

RUN useradd --create-home --shell /usr/sbin/nologin appuser && \
    mkdir -p /data && chown -R appuser:appuser /data /app

USER appuser

VOLUME ["/data"]
EXPOSE 8790

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8790/healthz')"

CMD ["uv", "run", "--no-dev", "agent-board", "serve"]
