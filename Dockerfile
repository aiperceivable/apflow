FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy project files needed for installation
COPY pyproject.toml README.md ./
COPY src/ ./src/

# Install apflow with all optional executors
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir ".[all]"

# Create data directory for SQLite storage (standalone mode)
RUN mkdir -p /app/.data

# Create non-root user
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app

USER appuser

# Expose port for A2A server
EXPOSE 8000

# Default: start Python (users provide their own entry script)
CMD ["python"]
