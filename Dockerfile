# ---------- Stage 1: builder ----------
FROM python:3.11-slim AS builder
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential gcc \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# uvloop + httptools are required by the runtime CMD below.
# Install them here so the runtime stage gets them from /install.
RUN pip install --prefix=/install -r requirements.txt \
 && pip install --prefix=/install "uvloop>=0.19" "httptools>=0.6"

# ---------- Stage 2: runtime ----------
FROM python:3.11-slim AS runtime

# Non-root user. UID/GID fixed so volumes mounted at deploy time stay stable.
RUN groupadd -r -g 1001 axelr \
 && useradd  -r -u 1001 -g axelr -d /app -s /sbin/nologin axelr

# tini = PID 1 (reaps zombies, forwards signals).
# ca-certificates = TLS roots.
# NOTE: bubblewrap is NOT installed — sandbox_exec.py uses setrlimit + setsid,
#       it does not shell out to bwrap. Re-add if you wire bwrap in.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /install /usr/local
COPY --chown=axelr:axelr . .

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    OMP_NUM_THREADS=1 \
    ORT_NUM_THREADS=1 \
    MALLOC_ARENA_MAX=2 \
    WEB_CONCURRENCY=1 \
    AXELR_COGNITION_LEVEL=2 \
    WORKSPACE_ROOT=/app

USER axelr
EXPOSE 8000

# Hit the lightweight, DB-free liveness route.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=5 \
    CMD python -c "import httpx,sys; \
r=httpx.get('http://127.0.0.1:8000/api/health/ping', timeout=3); \
sys.exit(0 if r.status_code==200 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--loop", "uvloop", \
     "--http", "httptools", \
     "--limit-concurrency", "32", \
     "--backlog", "64", \
     "--timeout-keep-alive", "5", \
     "--no-access-log"]