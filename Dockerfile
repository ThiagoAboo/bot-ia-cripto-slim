FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NLTK_DATA=/app/data/nltk_data \
    APP_BASE_DIR=/app

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app/ ./app/
COPY config/ ./config/
COPY README.md ./

RUN mkdir -p /app/data /app/config/models

EXPOSE 8080
VOLUME ["/app/data", "/app/config"]

CMD ["python", "-m", "app.main"]
