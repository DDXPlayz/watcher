FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PLAYWRIGHT_BROWSERS_PATH=/opt/browsers
WORKDIR /srv

COPY requirements.txt .
# libatomic1: Playwright's bundled Node needs it and the slim image lacks it.
RUN apt-get update && apt-get install -y --no-install-recommends libatomic1 \
 && pip install --no-cache-dir -r requirements.txt \
 && playwright install --with-deps --only-shell chromium \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --system --uid 10001 --create-home watcher \
 && mkdir -p data logs && chown watcher:watcher data logs
COPY app ./app
COPY dashboard ./dashboard
COPY scripts ./scripts
COPY config.yaml .

USER watcher
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=10s --start-period=180s --retries=3 \
  CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8080/health', timeout=8)"]
CMD ["python", "-m", "app.main"]
