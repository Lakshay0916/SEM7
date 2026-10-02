# Backend API: pipeline, risk model, self-healing loop. Talks to PostgreSQL.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# git: the pipeline creates sandbox repositories and ai/repair/* branches.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements-api.txt .
RUN pip install -r requirements-api.txt

COPY pytest.ini .
COPY app/ app/
COPY demo_projects/ demo_projects/
COPY models/ models/
COPY data/ data/
COPY scripts/ scripts/
COPY tests/ tests/

RUN useradd --create-home --uid 1000 sem7 \
    && mkdir -p /app/workspace \
    && chown -R sem7:sem7 /app
USER sem7

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
