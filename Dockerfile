# SEM7 self-healing CI/CD - dashboard + pipeline in one image.
# Build:  docker build -t sem7-selfheal .
# Run:    docker compose up        (dashboard on http://localhost:8502)
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# git is required: the pipeline creates sandbox repositories and repair branches.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so code changes don't invalidate the pip layer.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Run as an unprivileged user; workspace/ holds sandboxes and the SQLite store.
RUN useradd --create-home --uid 1000 sem7 \
    && mkdir -p /app/workspace \
    && chown -R sem7:sem7 /app
USER sem7

EXPOSE 8502
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8502/_stcore/health')"

CMD ["streamlit", "run", "ui/app.py", "--server.address=0.0.0.0", "--server.port=8502"]
