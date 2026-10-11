FROM python:3.12-slim

# TZ kreves for at «hent klokka 07» og alle tidsstempler skal være norsk tid.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata curl \
    && rm -rf /var/lib/apt/lists/*
ENV TZ=Europe/Oslo PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

# Datamappen er et volum; denne katalogen eies av appbrukeren (uid 1000).
RUN useradd --uid 1000 --create-home app \
    && mkdir -p /data && chown -R app:app /data /app
USER app

EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/helse || exit 1

CMD ["uvicorn", "app.web:app", "--host", "0.0.0.0", "--port", "8000"]
