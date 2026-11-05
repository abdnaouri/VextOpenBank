# ==============================================================================
# Multi-stage hardened Dockerfile for VextOpenBank
# Runs FastAPI as a non-privileged user with minimal attack surface
# ==============================================================================

FROM python:3.11-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libsqlite3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt* /app/
# If requirements.txt doesn't exist, install standard FastAPI stack
RUN if [ -f requirements.txt ]; then \
        pip install --no-cache-dir --user -r requirements.txt; \
    else \
        pip install --no-cache-dir --user fastapi uvicorn[standard] httpx requests pydantic beautifulsoup4; \
    fi

FROM python:3.11-slim AS runner

WORKDIR /app

# Create unprivileged application user
RUN groupadd -g 10001 vextgroup && \
    useradd -u 10001 -g vextgroup -s /sbin/nologin -d /app vextuser

# Copy installed Python packages from builder
COPY --from=builder /root/.local /home/vextuser/.local
ENV PATH=/home/vextuser/.local/bin:$PATH
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Copy application source
COPY --chown=vextuser:vextgroup vext_open_bank /app/vext_open_bank
COPY --chown=vextuser:vextgroup run_api.py /app/run_api.py 2>/dev/null || true

USER vextuser

EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/auth/status')" || exit 1

CMD ["python3", "-m", "uvicorn", "vext_open_bank.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--proxy-headers", "--forwarded-allow-ips", "*"]
