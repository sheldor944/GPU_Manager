FROM python:3.11-slim

WORKDIR /app

# Install system deps for paramiko (SSH)
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Data volume for persistent SQLite DB
VOLUME ["/app/data"]

ENV DATABASE_URL=sqlite:////app/data/gpu_manager.db

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
