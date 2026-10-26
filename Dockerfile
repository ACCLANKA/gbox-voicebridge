FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY api ./api
COPY config ./config
COPY docs ./docs
# Operator tooling ships with the image: scripts/clean-console.py is part of the documented
# deployment path, and without this COPY the runbook fails inside the container
# ("can't open file '/app/scripts/clean-console.py'") unless someone docker-cp's it in.
COPY scripts ./scripts

# SQLite lives here; mount a volume over it for persistence.
RUN mkdir -p /app/data

EXPOSE 8080
CMD ["python", "-m", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8080"]
