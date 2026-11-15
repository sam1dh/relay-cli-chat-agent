# Relay - CLI Chat Agent: one container with the API, Crawl4AI (+ headless Chromium), sitecrawl and curl.
FROM python:3.12-slim

# Build without Crawl4AI/Chromium for small hosts (e.g. Render free 512 MB):  --build-arg WITH_CRAWL4AI=0
ARG WITH_CRAWL4AI=1
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PYTHONUNBUFFERED=1 \
    RUNS_DIR=/tmp/runs \
    ALLOW_PRIVATE_HOSTS=0 \
    PORT=7860

# curl is one of the approved CLI tools; libatomic1 is needed by Playwright's bundled node driver
RUN apt-get update && apt-get install -y --no-install-recommends curl libatomic1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements-crawl4ai.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# Two layers so a failed browser download doesn't redo the (slow) pip install
RUN if [ "$WITH_CRAWL4AI" = "1" ]; then pip install --no-cache-dir -r requirements-crawl4ai.txt ; fi
RUN if [ "$WITH_CRAWL4AI" = "1" ]; then \
      python -m playwright install --with-deps chromium \
      && chmod -R a+rx /ms-playwright \
      && rm -rf /var/lib/apt/lists/* ; \
    fi

# Run as an unprivileged user (least privilege; also what Hugging Face Spaces expects)
RUN useradd -m -u 1000 appuser
COPY --chown=appuser:appuser . .
USER appuser
EXPOSE 7860

# Hugging Face uses 7860; Render/Railway inject $PORT.
CMD ["sh", "-c", "uvicorn backend.app:app --host 0.0.0.0 --port ${PORT:-7860} --proxy-headers --forwarded-allow-ips='*'"]
