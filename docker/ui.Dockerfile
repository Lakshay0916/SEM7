# UI: thin Streamlit client. Contains no pipeline code - every action is an HTTP call to the API.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements-ui.txt .
RUN pip install -r requirements-ui.txt

# Only the shared data contracts (pydantic models) are copied from the backend package.
COPY app/__init__.py app/__init__.py
COPY app/models/ app/models/
COPY ui/ ui/
COPY .streamlit/ .streamlit/

RUN useradd --create-home --uid 1000 sem7 && chown -R sem7:sem7 /app
USER sem7

ENV SEM7_API_URL=http://api:8000
EXPOSE 8502
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8502/_stcore/health')"

CMD ["streamlit", "run", "ui/app.py", "--server.address=0.0.0.0", "--server.port=8502"]
