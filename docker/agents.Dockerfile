# syntax=docker/dockerfile:1
# Agent services: Google ADK (agent-adk) and CrewAI (agent-crew), each an A2A server. Same image, different
# command. Holds the heavy agent frameworks so the api/ui images stay unchanged.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10 \
    CREWAI_TRACING_ENABLED=false \
    CREWAI_DISABLE_TELEMETRY=true

# git: MCP tools read baseline code from the sandbox repositories.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements-api.txt requirements-ui.txt requirements-agents.txt ./
# Large dependency set: keep downloads in a BuildKit cache and retry slow mirrors instead of failing.
RUN --mount=type=cache,target=/root/.cache/pip \
    PIP_NO_CACHE_DIR=0 pip install --retries 10 --timeout 120 -r requirements-agents.txt

COPY pytest.ini .
COPY app/ app/
COPY models/ models/
COPY data/ data/

RUN useradd --create-home --uid 1000 sem7 \
    && mkdir -p /app/workspace \
    && chown -R sem7:sem7 /app
USER sem7
ENV HOME=/home/sem7

EXPOSE 8101 8102
HEALTHCHECK --interval=10s --timeout=5s --start-period=40s --retries=6 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://localhost:{os.environ[\"A2A_PORT\"]}/health')"

CMD ["python", "-m", "app.a2a_agents.adk_service"]
