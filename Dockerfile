FROM python:3.11-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libsqlite3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/
RUN pip install --no-cache-dir --user -r requirements.txt

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

USER vextuser

EXPOSE 10000

CMD ["sh", "-c", "python3 -m uvicorn vext_open_bank.main:app --host 0.0.0.0 --port ${PORT:-10000}"]
