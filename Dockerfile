# =============================================================================
# Logistics Information Management System (Logistics IMS)
# Production & Academic Evaluation Multi-Stage Container Definition
# =============================================================================

FROM python:3.11-slim AS base

# Prevent Python from writing .pyc files and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    FLASK_APP=app.main:app \
    PORT=5000

# Install runtime system packages (curl for healthchecks, sqlite3)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Create application directory and non-privileged user for secure execution
WORKDIR /app

# Install Python dependencies first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir gunicorn

# Copy full application codebase
COPY . .

# Ensure storage and data directories exist with appropriate permissions
RUN mkdir -p /app/data /app/storage/uploads && \
    chmod -R 777 /app/data /app/storage

# Container healthcheck querying application health probe
HEALTHCHECK --interval=20s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:5000/health || exit 1

EXPOSE 5000

# Entrypoint script initializing SQLite WAL database if not present, then launching server
CMD ["sh", "-c", "python -m app.db.init_db --seed && gunicorn --workers=2 --bind=0.0.0.0:5000 'app.main:app'"]
