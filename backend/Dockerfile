FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (layer caching — rebuild only when requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# CONCEPT: Stateless container — no persistent state inside the container.
# All state lives in external services (PostgreSQL, Redis, MinIO).
# This makes the backend horizontally scalable: run N identical replicas.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
