FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DEMO_DATA_DIR=/tmp/claude-learn-demo
WORKDIR /app

COPY requirements-web.txt .
RUN pip install --no-cache-dir -r requirements-web.txt

COPY learner/ learner/
COPY webapp/ webapp/

# Cloud Run sets PORT.
CMD exec uvicorn webapp.app:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*'
