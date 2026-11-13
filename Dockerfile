# Shop Mini App: one FastAPI process (storefront + admin + API).
#
#   docker compose build        # runs the whole test suite; a failing test fails the build
#   docker compose up -d
#
# The image holds code only. Data lives outside it (compose.yaml mounts):
#   /data      SQLite database + its backups
#   /uploads   product photos + thumbnails
# Secrets come from the environment (.env next to compose.yaml), never baked in.

FROM python:3.13-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install -r backend/requirements.txt
# Repo layout is kept (code finds brands/, admin/, storefront files relative to backend/).
COPY . .

# --- test stage: same image + the suite. The final stage copies a marker from
# here, so BuildKit cannot skip it.
FROM base AS test
RUN cd backend && python -m pytest -q -p no:cacheprovider && touch /tmp/tests-passed

# --- runtime
FROM base AS runtime
COPY --from=test /tmp/tests-passed /tmp/tests-passed
RUN useradd --system --uid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin shop \
    && mkdir -p /data /uploads && chown shop:shop /data /uploads \
    && rm -rf backend/tests
ENV DATABASE_PATH=/data/shop.sqlite3 \
    UPLOADS_DIR=/uploads
USER shop
WORKDIR /app/backend
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/', timeout=4).status == 200 else 1)"]
# ONE worker: the rate limiter and the notification retry loop live in memory.
# Port 8000 is reachable only inside the compose network (Caddy), so trusting
# X-Forwarded-For from any peer there is safe.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-server-header"]
