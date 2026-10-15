# ---------------------------------------------------------------------------
# Stage 1: System dependencies (base image)
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommend \
        curl \
        ca-certificates \
        gnupg \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Stage 2: Build Python venv (isolated, reproducible)
# ---------------------------------------------------------------------------
FROM base AS builder

WORKDIR /build

COPY requirements.txt .

# Create venv and install dependencies into it
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip && \
    /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------------
# Stage 3: Final runtime image
# ---------------------------------------------------------------------------
FROM base AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Copy the venv from builder
COPY --from=builder /opt/venv /opt/venv

# Copy application source
COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini ./
COPY .env.example ./.env

# Create non-root user
RUN useradd --create-home --shell /bin/bash appuser && \
    chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fs http://localhost:8000/api/health || exit 1

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
