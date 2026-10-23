FROM python:3.11-slim

WORKDIR /app

# tzdata so the TZ env var (used by the scheduler) resolves correctly
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Environment configuration
ENV PORT=8000 PYTHONUNBUFFERED=1
EXPOSE 8000

# Start command
CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
